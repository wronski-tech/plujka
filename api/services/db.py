from __future__ import annotations

from typing import Any

import psycopg

from api.services.config import DATABASE_URL
from api.services.kbw_geo import district_expr_sql


def get_connection() -> psycopg.Connection:
    """Get connection."""
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_database() -> None:
    """Init database."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    embedding vector(1536)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS elections (
                    id SERIAL PRIMARY KEY,
                    year INT NOT NULL,
                    type TEXT NOT NULL,
                    UNIQUE (year, type)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id SERIAL PRIMARY KEY,
                    candidate_id INT NOT NULL REFERENCES candidates(id),
                    election_id INT NOT NULL REFERENCES elections(id),
                    votes INT NOT NULL,
                    district TEXT NOT NULL,
                    list_position INT NOT NULL DEFAULT 1
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS elected_candidates (
                    id SERIAL PRIMARY KEY,
                    year INT NOT NULL,
                    district TEXT NOT NULL,
                    committee_name TEXT NOT NULL,
                    candidate_name TEXT NOT NULL,
                    candidate_votes INT NOT NULL,
                    list_position INT,
                    UNIQUE (year, district, committee_name, candidate_name)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS source_files (
                    id SERIAL PRIMARY KEY,
                    election_key TEXT NOT NULL,
                    year INT,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    column_count INT NOT NULL,
                    header JSONB NOT NULL,
                    profiled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS source_columns (
                    id SERIAL PRIMARY KEY,
                    source_file_id INT NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    column_index INT NOT NULL,
                    column_name TEXT NOT NULL,
                    is_committee BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE (source_file_id, column_index)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sejm_aggregate_results (
                    id SERIAL PRIMARY KEY,
                    election_id INT NOT NULL REFERENCES elections(id),
                    geography_level TEXT NOT NULL,
                    sejm_district TEXT NOT NULL DEFAULT '',
                    teryt TEXT NOT NULL DEFAULT '',
                    gmina TEXT NOT NULL DEFAULT '',
                    powiat TEXT NOT NULL DEFAULT '',
                    wojewodztwo TEXT NOT NULL DEFAULT '',
                    committee_name TEXT NOT NULL,
                    metric_value DOUBLE PRECISION NOT NULL,
                    is_percentage BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE (
                        election_id,
                        geography_level,
                        sejm_district,
                        teryt,
                        gmina,
                        powiat,
                        wojewodztwo,
                        committee_name,
                        is_percentage
                    )
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS senate_results (
                    id SERIAL PRIMARY KEY,
                    election_id INT NOT NULL REFERENCES elections(id),
                    senate_district TEXT NOT NULL,
                    symbol_kontrolny TEXT NOT NULL,
                    teryt TEXT NOT NULL DEFAULT '',
                    numer_obwodu TEXT NOT NULL DEFAULT '',
                    gmina TEXT NOT NULL DEFAULT '',
                    powiat TEXT NOT NULL DEFAULT '',
                    wojewodztwo TEXT NOT NULL DEFAULT '',
                    candidate_name TEXT NOT NULL,
                    votes INT NOT NULL,
                    UNIQUE (election_id, symbol_kontrolny, candidate_name)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sejm_candidate_ballots (
                    id SERIAL PRIMARY KEY,
                    year INT NOT NULL,
                    district TEXT NOT NULL,
                    committee_name TEXT NOT NULL,
                    candidate_name TEXT NOT NULL,
                    list_position INT,
                    total_votes INT NOT NULL,
                    UNIQUE (year, district, committee_name, candidate_name)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kbw_dane_files (
                    id SERIAL PRIMARY KEY,
                    rel_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    file_ext TEXT NOT NULL,
                    file_kind TEXT NOT NULL,
                    size_bytes BIGINT,
                    mtime TIMESTAMPTZ,
                    dataset_key TEXT,
                    year INT,
                    csv_delimiter TEXT,
                    encoding_used TEXT,
                    column_count INT,
                    header JSONB,
                    profile_error TEXT,
                    profiled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_dane_files_year ON kbw_dane_files (year);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_dane_files_dataset ON kbw_dane_files (dataset_key);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_dane_files_ext ON kbw_dane_files (file_ext);"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kbw_election_runs (
                    id SERIAL PRIMARY KEY,
                    family TEXT NOT NULL,
                    year INT NOT NULL,
                    round INT NOT NULL DEFAULT 0,
                    slice TEXT NOT NULL DEFAULT '',
                    variant TEXT NOT NULL DEFAULT '',
                    dataset_hint TEXT,
                    UNIQUE (family, year, round, slice, variant)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_runs_family_year ON kbw_election_runs (family, year);"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kbw_facts (
                    id BIGSERIAL PRIMARY KEY,
                    election_run_id INT NOT NULL REFERENCES kbw_election_runs(id) ON DELETE CASCADE,
                    geography JSONB NOT NULL DEFAULT '{}',
                    subject JSONB NOT NULL DEFAULT '{}',
                    metric TEXT NOT NULL,
                    value DOUBLE PRECISION NOT NULL,
                    is_percentage BOOLEAN NOT NULL DEFAULT FALSE,
                    source_file_id INT REFERENCES kbw_dane_files(id) ON DELETE SET NULL
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_facts_run ON kbw_facts (election_run_id);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_facts_gin_geo ON kbw_facts USING gin (geography);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_facts_gin_sub ON kbw_facts USING gin (subject);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_facts_source ON kbw_facts (source_file_id);"
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kbw_facts_subject_column_trgm
                ON kbw_facts USING gin ((subject->>'column') gin_trgm_ops);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kbw_facts_geo_folded_trgm
                ON kbw_facts USING gin ((translate(lower(geography::text), 'ąćęłńóśźż', 'acelnoszz')) gin_trgm_ops);
                """
            )

            # Aggregated Sejm list votes by normalized district (analytics / roadmap § średnioterminowy).
            _dist = district_expr_sql("f")
            cur.execute(
                f"""
                CREATE OR REPLACE VIEW kbw_v_sejm_district_list_agg AS
                SELECT
                  er.id AS election_run_id,
                  er.year,
                  {_dist} AS district,
                  trim(f.subject->>'column') AS list_label,
                  SUM(f.value)::double precision AS votes,
                  bool_or(df.rel_path ILIKE '%csv%') AS has_csv_source
                FROM kbw_facts f
                JOIN kbw_election_runs er ON er.id = f.election_run_id
                JOIN kbw_dane_files df ON df.id = f.source_file_id
                WHERE er.family IN ('sejm', 'sejmsenat')
                  AND f.is_percentage = FALSE
                  AND COALESCE(f.subject->>'kind', '') = 'series'
                  AND (df.rel_path ILIKE '%sejm%' OR df.rel_path ILIKE '%po_obwodach%')
                  AND df.rel_path NOT ILIKE '%proc%'
                  AND trim(f.subject->>'column') ~ '[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]'
                GROUP BY er.id, er.year,
                  {_dist},
                  trim(f.subject->>'column');
                """
            )

            # Future: cross-election person facts (Dutkiewicz, party switches) — loader fills later.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kbw_person_election_fact (
                    id BIGSERIAL PRIMARY KEY,
                    person_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    year INT NOT NULL,
                    election_family TEXT NOT NULL DEFAULT 'sejm',
                    party_list_label TEXT,
                    votes BIGINT,
                    elected BOOLEAN,
                    district TEXT NOT NULL DEFAULT '',
                    kbw_source_file_id INT REFERENCES kbw_dane_files(id) ON DELETE SET NULL,
                    UNIQUE (person_key, year, election_family, district, party_list_label)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_person_year ON kbw_person_election_fact (year);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_person_key ON kbw_person_election_fact (person_key);"
            )

            # Relational slice over mirror-backed rollups (Phase 2); filled via sync from person facts.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kbw_candidates (
                    id BIGSERIAL PRIMARY KEY,
                    election_run_id INT NOT NULL REFERENCES kbw_election_runs(id) ON DELETE CASCADE,
                    person_key TEXT NOT NULL,
                    district TEXT NOT NULL DEFAULT '',
                    list_label TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL,
                    list_position INT,
                    votes BIGINT,
                    UNIQUE (election_run_id, person_key, district, list_label)
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_candidates_run ON kbw_candidates (election_run_id);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_candidates_person ON kbw_candidates (person_key);"
            )

            # One row per candidate-column fact (gmina/obwód …); geography stays on kbw_facts via JOIN.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS kbw_candidate_geo_votes (
                    kbw_fact_id BIGINT PRIMARY KEY REFERENCES kbw_facts(id) ON DELETE CASCADE,
                    election_run_id INT NOT NULL REFERENCES kbw_election_runs(id) ON DELETE CASCADE,
                    person_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    votes BIGINT NOT NULL
                );
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_cgv_run ON kbw_candidate_geo_votes (election_run_id);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_cgv_run_person ON kbw_candidate_geo_votes "
                "(election_run_id, person_key);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_kbw_cgv_person ON kbw_candidate_geo_votes (person_key);"
            )


def kbw_data_ready() -> bool:
    """Return True when at least one KBW fact row exists (import finished or in progress elsewhere)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM kbw_facts LIMIT 1)")
            row = cur.fetchone()
    return bool(row and row[0])


def kbw_health_snapshot() -> dict[str, int]:
    """Approximate live row counts from ``pg_stat_user_tables`` (cheap; refreshed after DML/ANALYZE)."""
    tables = (
        "kbw_facts",
        "kbw_election_runs",
        "kbw_candidate_geo_votes",
        "kbw_person_election_fact",
        "kbw_candidates",
    )
    out: dict[str, int] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT relname, COALESCE(n_live_tup, 0)::bigint
                FROM pg_stat_user_tables
                WHERE relname = ANY(%s)
                """,
                (list(tables),),
            )
            for relname, n in cur.fetchall():
                out[str(relname)] = int(n)
    return out


def kbw_dane_files_catalog_summary() -> dict[str, Any]:
    """Counts from ``kbw_dane_files`` (mirror inventory upserted by ``kbw_catalog.profile_kbw_dane_files``)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*)::bigint FROM kbw_dane_files")
            total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COALESCE(year::text, 'unknown'), COUNT(*)::bigint
                FROM kbw_dane_files
                GROUP BY year
                ORDER BY year NULLS LAST
                """
            )
            by_year = {row[0]: int(row[1]) for row in cur.fetchall()}
            cur.execute(
                """
                SELECT file_kind, COUNT(*)::bigint
                FROM kbw_dane_files
                GROUP BY file_kind
                ORDER BY file_kind
                """
            )
            by_kind = {row[0]: int(row[1]) for row in cur.fetchall()}
    return {
        "total_files": total,
        "by_year": by_year,
        "by_file_kind": by_kind,
    }


def run_sql(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run sql."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc.name for desc in cur.description]
            rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]


def get_latest_election_year(election_type: str = "sejm") -> int | None:
    """Get latest election year."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(year) FROM elections WHERE type = %s", (election_type,))
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def get_latest_elected_candidates_year() -> int | None:
    """Get latest elected candidates year."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(year) FROM elected_candidates")
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def get_latest_kbw_sejm_year() -> int | None:
    """Latest year present in KBW election runs for Sejm (mirror-backed facts)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(year) FROM kbw_election_runs WHERE family IN ('sejm', 'sejmsenat')"
            )
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def default_sejm_year_for_queries() -> int | None:
    """Prefer legacy `elections` year when populated; else KBW mirror years."""
    y = get_latest_election_year("sejm")
    if y is not None:
        return y
    return get_latest_kbw_sejm_year()


def analyze_kbw_tables() -> None:
    """Refresh planner statistics after large imports (ANALYZE)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ANALYZE kbw_facts, kbw_election_runs, kbw_dane_files, "
                "kbw_person_election_fact, kbw_candidates, kbw_candidate_geo_votes;"
            )


def backfill_kbw_candidate_geo_votes_from_facts(year: int | None = None) -> int:
    """Populate ``kbw_candidate_geo_votes`` from raw ``kbw_facts`` rows (Sejm candidate-level files).

    Geography (gmina, obwód, …) remains on ``kbw_facts.geography`` — join there for maps / gmina rollups.
    Idempotent via ``ON CONFLICT`` on ``kbw_fact_id``.
    """
    params: dict[str, Any] = {}
    year_filter = ""
    if year is not None:
        year_filter = " AND er.year = %(year)s::int"
        params["year"] = year
    sql = f"""
        INSERT INTO kbw_candidate_geo_votes (
          kbw_fact_id, election_run_id, person_key, display_name, votes
        )
        SELECT
          f.id,
          f.election_run_id,
          md5(translate(lower(trim(f.subject->>'column')), 'ąćęłńóśźż', 'acelnoszz')) AS person_key,
          trim(f.subject->>'column') AS display_name,
          ROUND(f.value)::bigint AS votes
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.family IN ('sejm', 'sejmsenat')
          AND NOT f.is_percentage
          AND COALESCE(f.subject->>'kind', '') = 'series'
          AND df.rel_path ILIKE '%kandydat%'
          AND df.rel_path ILIKE '%sejm%'
          AND df.rel_path NOT ILIKE '%proc%'
          AND trim(f.subject->>'column') <> ''
          {year_filter}
        ON CONFLICT (kbw_fact_id) DO UPDATE SET
          election_run_id = EXCLUDED.election_run_id,
          person_key = EXCLUDED.person_key,
          display_name = EXCLUDED.display_name,
          votes = EXCLUDED.votes
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
            cur.execute("ANALYZE kbw_candidate_geo_votes;")
    return int(n) if n is not None else 0


def sync_kbw_candidates_from_person_facts(year: int | None = None) -> int:
    """Upsert ``kbw_candidates`` from ``kbw_person_election_fact`` (same grain as person rollup).

    Picks one ``election_run_id`` per (year, family) via ``MIN(id)`` when multiple runs exist.
    """
    params: dict[str, Any] = {}
    year_filter = ""
    if year is not None:
        year_filter = " AND pe.year = %(year)s::int"
        params["year"] = year
    sql = f"""
        INSERT INTO kbw_candidates (
          election_run_id, person_key, district, list_label,
          display_name, list_position, votes
        )
        SELECT
          r.run_id,
          pe.person_key,
          pe.district,
          COALESCE(pe.party_list_label, '') AS list_label,
          pe.display_name,
          NULL::int AS list_position,
          pe.votes
        FROM kbw_person_election_fact pe
        INNER JOIN (
          SELECT year, family, MIN(id) AS run_id
          FROM kbw_election_runs
          WHERE family IN ('sejm', 'sejmsenat')
          GROUP BY year, family
        ) r ON r.year = pe.year AND r.family = pe.election_family
        WHERE 1=1
          {year_filter}
        ON CONFLICT (election_run_id, person_key, district, list_label)
        DO UPDATE SET
          display_name = EXCLUDED.display_name,
          votes = EXCLUDED.votes,
          list_position = COALESCE(EXCLUDED.list_position, kbw_candidates.list_position)
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
            cur.execute("ANALYZE kbw_candidates;")
    return int(n) if n is not None else 0


def backfill_kbw_person_election_facts(year: int | None = None) -> int:
    """Upsert `kbw_person_election_fact` from candidate-column facts (Sejm paths in mirror).

    Aggregates votes per stable ``person_key`` (md5 of folded name), year, family, district.
    Optional ``year`` limits the scan. Returns PostgreSQL rowcount from the INSERT (may include updates).
    """
    d = district_expr_sql("f")
    params: dict[str, Any] = {}
    year_filter = ""
    if year is not None:
        year_filter = " AND er.year = %(year)s::int"
        params["year"] = year
    sql = f"""
        INSERT INTO kbw_person_election_fact (
          person_key, display_name, year, election_family, party_list_label,
          votes, elected, district, kbw_source_file_id
        )
        SELECT
          md5(translate(lower(trim(f.subject->>'column')), 'ąćęłńóśźż', 'acelnoszz')) AS person_key,
          trim(f.subject->>'column') AS display_name,
          er.year,
          CASE WHEN er.family = 'sejmsenat' THEN 'sejmsenat' ELSE 'sejm' END,
          ''::text AS party_list_label,
          SUM(f.value)::bigint,
          NULL::boolean,
          COALESCE({d}, '')::text,
          MIN(f.source_file_id)::int
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.family IN ('sejm', 'sejmsenat')
          AND NOT f.is_percentage
          AND COALESCE(f.subject->>'kind', '') = 'series'
          AND df.rel_path ILIKE '%kandydat%'
          AND df.rel_path ILIKE '%sejm%'
          AND df.rel_path NOT ILIKE '%proc%'
          AND trim(f.subject->>'column') <> ''
          {year_filter}
        GROUP BY
          er.year,
          CASE WHEN er.family = 'sejmsenat' THEN 'sejmsenat' ELSE 'sejm' END,
          COALESCE({d}, ''),
          trim(f.subject->>'column')
        ON CONFLICT (person_key, year, election_family, district, party_list_label)
        DO UPDATE SET
          votes = EXCLUDED.votes,
          display_name = EXCLUDED.display_name,
          kbw_source_file_id = COALESCE(
            EXCLUDED.kbw_source_file_id,
            kbw_person_election_fact.kbw_source_file_id
          )
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            n = cur.rowcount
            cur.execute("ANALYZE kbw_person_election_fact;")
    return int(n) if n is not None else 0
