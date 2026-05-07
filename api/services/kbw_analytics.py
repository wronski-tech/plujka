"""Parameterized analytics SQL on `kbw_facts` (Sejm / sejmsenat).

Heuristics (geography keys, column names) follow KBW CSV headers; may need
year-specific tuning — see docs/ANALYTICS_ARCHITECTURE.md.
"""

from __future__ import annotations

from typing import Any

from api.services.kbw_geo import district_expr_sql


def sql_committee_gap_by_district_from_view(
    *,
    year: int,
    left_pattern: str,
    right_pattern: str,
    prefer_csv_sources: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Same as legacy gap query but reads `kbw_v_sejm_district_list_agg` (roadmap view)."""
    sql = """
        WITH agg AS (
          SELECT district, list_label AS colname, votes::bigint AS votes
          FROM kbw_v_sejm_district_list_agg
          WHERE year = %(year)s::int
            AND (%(prefer_csv)s::bool = FALSE OR has_csv_source)
            AND district IS NOT NULL AND trim(district) <> ''
        ),
        left_v AS (
          SELECT district, SUM(votes)::bigint AS v
          FROM agg
          WHERE colname ILIKE %(left_pattern)s
          GROUP BY district
        ),
        right_v AS (
          SELECT district, SUM(votes)::bigint AS v
          FROM agg
          WHERE colname ILIKE %(right_pattern)s
          GROUP BY district
        ),
        merged AS (
          SELECT
            l.district,
            ABS(l.v - r.v)::bigint AS gap,
            l.v AS left_votes,
            r.v AS right_votes
          FROM left_v l
          INNER JOIN right_v r ON l.district = r.district
        )
        (
          SELECT
            'smallest_gap'::text AS which,
            district,
            gap,
            left_votes,
            right_votes
          FROM merged
          ORDER BY gap ASC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'largest_gap'::text AS which,
            district,
            gap,
            left_votes,
            right_votes
          FROM merged
          ORDER BY gap DESC NULLS LAST
          LIMIT 1
        )
    """
    params: dict[str, Any] = {
        "year": year,
        "left_pattern": left_pattern,
        "right_pattern": right_pattern,
        "prefer_csv": prefer_csv_sources,
    }
    return sql, params


def sql_max_turnout_precinct(*, year: int, limit: int = 5) -> tuple[str, dict[str, Any]]:
    """Precinct-level rows whose metric/name suggests turnout (%); highest first."""
    sql = f"""
        SELECT
          er.year,
          f.geography AS geography,
          f.subject->>'column' AS metric_label,
          f.metric AS metric_slug,
          f.value AS turnout_pct,
          df.rel_path AS source_path
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.year = %(year)s::int
          AND er.family IN ('sejm', 'sejmsenat', 'referendum', 'prezydent')
          AND f.is_percentage = TRUE
          AND (
            lower(COALESCE(f.subject->>'column', '')) LIKE '%%frekw%%'
            OR lower(f.metric) LIKE '%%frekw%%'
          )
          AND (
            df.rel_path ILIKE '%%obw%%'
            OR df.rel_path ILIKE '%%komis%%'
            OR df.rel_path ILIKE '%%obwod%%'
          )
        ORDER BY f.value DESC NULLS LAST
        LIMIT %(limit)s::int
    """
    return sql, {"year": year, "limit": limit}


def sql_committee_gap_by_district(
    *,
    year: int,
    left_pattern: str,
    right_pattern: str,
    prefer_csv_sources: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Smallest / largest committee gap by district — uses `kbw_v_sejm_district_list_agg`."""
    return sql_committee_gap_by_district_from_view(
        year=year,
        left_pattern=left_pattern,
        right_pattern=right_pattern,
        prefer_csv_sources=prefer_csv_sources,
    )


def sql_sejm_mandate_vote_extremes(*, year: int) -> tuple[str, dict[str, Any]]:
    """Min/max personal votes among elected_candidates vs sejm_candidate_ballots losers (same year).

    Requires legacy PKW seed tables populated; empty result if KBW-only without backfill.
    """
    sql = """
        (
          SELECT
            'entered_min_votes'::text AS bucket,
            year,
            district,
            committee_name,
            candidate_name,
            candidate_votes::bigint AS votes
          FROM elected_candidates
          WHERE year = %(year)s::int
          ORDER BY candidate_votes ASC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'entered_max_votes'::text,
            year,
            district,
            committee_name,
            candidate_name,
            candidate_votes::bigint
          FROM elected_candidates
          WHERE year = %(year)s::int
          ORDER BY candidate_votes DESC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'not_entered_min_votes'::text,
            b.year,
            b.district,
            b.committee_name,
            b.candidate_name,
            b.total_votes::bigint
          FROM sejm_candidate_ballots b
          WHERE b.year = %(year)s::int
            AND NOT EXISTS (
              SELECT 1 FROM elected_candidates e
              WHERE e.year = b.year
                AND e.district = b.district
                AND e.candidate_name = b.candidate_name
                AND e.committee_name = b.committee_name
            )
          ORDER BY b.total_votes ASC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'not_entered_max_votes'::text,
            b.year,
            b.district,
            b.committee_name,
            b.candidate_name,
            b.total_votes::bigint
          FROM sejm_candidate_ballots b
          WHERE b.year = %(year)s::int
            AND NOT EXISTS (
              SELECT 1 FROM elected_candidates e
              WHERE e.year = b.year
                AND e.district = b.district
                AND e.candidate_name = b.candidate_name
                AND e.committee_name = b.committee_name
            )
          ORDER BY b.total_votes DESC NULLS LAST
          LIMIT 1
        )
    """
    return sql, {"year": year}


def sql_sejm_mandate_vote_extremes_from_kbw_facts(*, year: int) -> tuple[str, dict[str, Any]]:
    """When PKW tables are empty: min/max candidate vote totals from KBW candidate-level files.

    Four rows — national aggregate min/max (sum over geography per candidate label), and
    district-level aggregate min/max (sum per okręg + candidate). Does **not** infer mandate
    entry; bucket names are prefixed ``kbw_`` so clients can distinguish from PKW-backed rows.
    """
    d = district_expr_sql("f")
    sql = f"""
        WITH cand_nat AS (
          SELECT
            trim(f.subject->>'column') AS candidate_name,
            SUM(f.value)::bigint AS votes
          FROM kbw_facts f
          JOIN kbw_election_runs er ON er.id = f.election_run_id
          JOIN kbw_dane_files df ON df.id = f.source_file_id
          WHERE er.year = %(year)s::int
            AND er.family IN ('sejm', 'sejmsenat')
            AND NOT f.is_percentage
            AND COALESCE(f.subject->>'kind', '') = 'series'
            AND df.rel_path ILIKE '%kandydat%'
            AND df.rel_path ILIKE '%sejm%'
            AND df.rel_path NOT ILIKE '%proc%'
            AND trim(f.subject->>'column') <> ''
          GROUP BY 1
        ),
        cand_dist AS (
          SELECT
            {d} AS district,
            trim(f.subject->>'column') AS candidate_name,
            SUM(f.value)::bigint AS votes
          FROM kbw_facts f
          JOIN kbw_election_runs er ON er.id = f.election_run_id
          JOIN kbw_dane_files df ON df.id = f.source_file_id
          WHERE er.year = %(year)s::int
            AND er.family IN ('sejm', 'sejmsenat')
            AND NOT f.is_percentage
            AND COALESCE(f.subject->>'kind', '') = 'series'
            AND df.rel_path ILIKE '%kandydat%'
            AND df.rel_path ILIKE '%sejm%'
            AND df.rel_path NOT ILIKE '%proc%'
            AND trim(f.subject->>'column') <> ''
          GROUP BY 1, 2
        )
        (
          SELECT
            'kbw_national_min'::text AS bucket,
            %(year)s::int AS year,
            ''::text AS district,
            ''::text AS committee_name,
            candidate_name,
            votes
          FROM cand_nat
          ORDER BY votes ASC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'kbw_national_max'::text,
            %(year)s::int,
            '',
            '',
            candidate_name,
            votes
          FROM cand_nat
          ORDER BY votes DESC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'kbw_district_min'::text,
            %(year)s::int,
            COALESCE(district, '')::text,
            '',
            candidate_name,
            votes
          FROM cand_dist
          ORDER BY votes ASC NULLS LAST
          LIMIT 1
        )
        UNION ALL
        (
          SELECT
            'kbw_district_max'::text,
            %(year)s::int,
            COALESCE(district, '')::text,
            '',
            candidate_name,
            votes
          FROM cand_dist
          ORDER BY votes DESC NULLS LAST
          LIMIT 1
        )
    """
    return sql, {"year": year}


def sql_candidate_geo_votes_detail(
    *,
    year: int,
    candidate_pattern: str,
    gmina_pattern: str | None = None,
    limit: int = 80,
) -> tuple[str, dict[str, Any]]:
    """Per-row candidate votes with ``kbw_facts.geography`` (gmina, obwód, …) via ``kbw_candidate_geo_votes``.

    Faster when ``kbw_candidate_geo_votes`` is populated; otherwise use
    :func:`sql_candidate_geo_votes_detail_from_facts`. ``gmina_pattern`` is optional.
    """
    sql = """
        SELECT
          er.year,
          g.display_name AS candidate_name,
          g.votes,
          f.geography AS geography,
          df.rel_path AS source_path
        FROM kbw_candidate_geo_votes g
        JOIN kbw_facts f ON f.id = g.kbw_fact_id
        JOIN kbw_election_runs er ON er.id = g.election_run_id
        LEFT JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.year = %(year)s::int
          AND g.display_name ILIKE %(candidate_pattern)s
          AND (
            %(gmina_pattern)s::text IS NULL
            OR COALESCE(
              f.geography->>'Gmina',
              f.geography->>'gmina',
              f.geography->>'Nazwa gminy',
              f.geography->>'NAZWA GMINY',
              ''
            ) ILIKE %(gmina_pattern)s
            OR f.geography::text ILIKE %(gmina_pattern)s
          )
        ORDER BY g.votes DESC NULLS LAST
        LIMIT %(limit)s::int
    """
    params: dict[str, Any] = {
        "year": year,
        "candidate_pattern": candidate_pattern,
        "gmina_pattern": gmina_pattern,
        "limit": limit,
    }
    return sql, params


def sql_candidate_geo_votes_detail_from_facts(
    *,
    year: int,
    candidate_pattern: str,
    gmina_pattern: str | None = None,
    limit: int = 80,
) -> tuple[str, dict[str, Any]]:
    """Same rows as :func:`sql_candidate_geo_votes_detail` but scans ``kbw_facts`` directly.

    Uses the same path/file filters as ``backfill_kbw_candidate_geo_votes_from_facts`` — works without
    prior denormalized backfill (slower on large mirrors).
    """
    sql = """
        SELECT
          er.year,
          trim(f.subject->>'column') AS candidate_name,
          ROUND(f.value)::bigint AS votes,
          f.geography AS geography,
          df.rel_path AS source_path
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.year = %(year)s::int
          AND er.family IN ('sejm', 'sejmsenat')
          AND NOT f.is_percentage
          AND COALESCE(f.subject->>'kind', '') = 'series'
          AND df.rel_path ILIKE '%kandydat%'
          AND df.rel_path ILIKE '%sejm%'
          AND df.rel_path NOT ILIKE '%proc%'
          AND trim(f.subject->>'column') <> ''
          AND trim(f.subject->>'column') ILIKE %(candidate_pattern)s
          AND (
            %(gmina_pattern)s::text IS NULL
            OR COALESCE(
              f.geography->>'Gmina',
              f.geography->>'gmina',
              f.geography->>'Nazwa gminy',
              f.geography->>'NAZWA GMINY',
              ''
            ) ILIKE %(gmina_pattern)s
            OR f.geography::text ILIKE %(gmina_pattern)s
          )
        ORDER BY ROUND(f.value)::bigint DESC NULLS LAST
        LIMIT %(limit)s::int
    """
    params: dict[str, Any] = {
        "year": year,
        "candidate_pattern": candidate_pattern,
        "gmina_pattern": gmina_pattern,
        "limit": limit,
    }
    return sql, params


def sql_coalition_candidate_vote_sum(
    *,
    year: int,
    committee_pattern: str,
    limit_files_hint: int = 1,
) -> tuple[str, dict[str, Any]]:
    """Sum vote facts from candidate-level files where column names belong to one committee (heuristic).

    Restricts to paths suggesting per-candidate Sejm results; sums all numeric series columns
    matching the committee name fragment.
    """
    _ = limit_files_hint
    sql = f"""
        SELECT
          %(committee_pattern)s::text AS committee_filter,
          COUNT(*)::bigint AS fact_rows,
          SUM(f.value)::bigint AS vote_sum
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.year = %(year)s::int
          AND er.family IN ('sejm', 'sejmsenat')
          AND f.is_percentage = FALSE
          AND COALESCE(f.subject->>'kind', '') = 'series'
          AND (
            df.rel_path ILIKE '%%kandydat%%'
            OR df.rel_path ILIKE '%%kandydatow%%'
          )
          AND df.rel_path ILIKE '%%sejm%%'
          AND trim(f.subject->>'column') ILIKE %(committee_pattern)s
    """
    return sql, {"year": year, "committee_pattern": committee_pattern}
