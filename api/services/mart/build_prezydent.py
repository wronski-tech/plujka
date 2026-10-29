"""Build the presidential election tables of one year (both rounds) in DuckDB (loader side).

Sources per round:
- 2020, 2025: one per-precinct file with geography, protocol and one column per candidate
  (``wyniki_gl_na_kand_po_obwodach`` / ``protokoly_po_obwodach``) plus the candidate register
  ``kandydaci`` with official totals.
- 2010: per-precinct files ``pzt2010-wyn-obw`` (domestic, abroad and ships; candidates as 'SURNAME
  Given' headers; no invalid-votes column) and per-gmina totals ``pzt2010-wyn-gmn``, otherwise like 2015.
- 2015: per-precinct files only (``prezydent_2015_tura1.csv``, ``wyniki_tura2.xls``) and per-gmina
  totals (``prez2015_N_tura_gm.xls``); there is no candidate register, so candidates come from the
  column headers. The round 2 file publishes TERYT as 0: its precincts get the TERYT of the round 1
  precinct with the same voivodeship, gmina name and number.

Checks: unique precincts, every candidate read from exactly one column, precinct votes equal the
official candidate totals (2015: equal the per-gmina file), and valid votes equal the sum of
candidate votes.
"""

from __future__ import annotations

import logging

import duckdb

from api.services.mart.build_common import Election, YearSources, column, python_name_key, run_checks
from api.services.mart.build_precincts import PROTOCOL_ALIASES, precinct_select, unpivot_votes, vote_columns

log = logging.getLogger("mart")

TABLES = ("prezydent_candidates", "prezydent_precinct_protocols", "prezydent_precinct_candidate_votes")
FAMILY = ("prezydent",)

SOURCES = {
    2010: {
        r: {"results": f"**/prezydent/{r}/pzt2010-wyn-obw/*.parquet", "gmina_totals": f"**/prezydent/{r}/pzt2010-wyn-gmn.parquet"}
        for r in (1, 2)
    },
    2015: {
        1: {"results": "**/prezydent/1/prezydent_2015_tura1/prezydent_2015_tura1__*.parquet",
            "gmina_totals": "**/prezydent/1/prez2015_1_tura_gm.parquet"},
        2: {"results": "**/prezydent/2/wyniki_tura2/*.parquet",
            "gmina_totals": "**/prezydent/2/prez2015_2_tura_gm.parquet"},
    },
    2020: {
        r: {"results": f"**/prezydent/{r}/wyniki_gl_na_kand_po_obwodach_csv/*.parquet",
            "candidates": f"**/prezydent/{r}/kandydaci_csv/*.parquet"}
        for r in (1, 2)
    },
    2025: {
        1: {"results": "**/prezydent/1/protokoly_po_obwodach_csv*/*.parquet",
            "candidates": "**/prezydent/1/kandydaci_csv*/*.parquet"},
        2: {"results": "**/prezydent/2/protokoly_po_obwodach_w_drugiej_turze_csv*/*.parquet",
            "candidates": "**/prezydent/2/kandydaci_w_drugiej_turze_csv*/*.parquet"},
    },
}

DATES = {
    (2010, 1): "2010-06-20", (2010, 2): "2010-07-04",
    (2015, 1): "2015-05-10", (2015, 2): "2015-05-24",
    (2020, 1): "2020-06-28", (2020, 2): "2020-07-12",
    (2025, 1): "2025-05-18", (2025, 2): "2025-06-01",
}


def available(sources: YearSources) -> bool:
    """Whether both rounds of the year have staged per-precinct results."""
    rounds = SOURCES.get(sources.year)
    return bool(rounds) and all(sources.has(r["results"]) for r in rounds.values())


def _candidates_from_register(con: duckdb.DuckDBPyConnection, year: int, rnd: int, src: str) -> None:
    """``cand_<rnd>`` from the official register (2020: Nazwisko + Imiona; 2025: 'Nazwisko i imiona')."""
    if column(con, src, "Nazwisko i imiona", required=False):
        name = '"Nazwisko i imiona"'
    else:
        name = """("Imiona" || ' ' || "Nazwisko")"""
    elected = column(con, src, "Wybrany", "Czy uzyskał mandat")
    pct = column(con, src, "Procent wszystkich głosów", "Procent głosów")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cand_{rnd} AS
        SELECT {year} AS year, {rnd} AS round,
               CAST(row_number() OVER (ORDER BY surname({name}), {name}) AS INT) AS candidate_id,
               display_name({name}) AS full_name,
               surname({name}) AS last_name, given_names({name}) AS first_names,
               gender_("Płeć") AS gender, "Zawód" AS occupation, "Miejsce zamieszkania" AS residence,
               "Przynależność do partii" AS party_membership, "Nazwa komitetu" AS committee_name,
               int_("Liczba głosów") AS votes, CAST(round(num_({pct}), 2) AS DECIMAL(6, 2)) AS pct,
               yes_({elected}) AS elected, name_key({name}) AS nkey
        FROM {src}
        """
    )


def _candidates_from_headers(con: duckdb.DuckDBPyConnection, year: int, rnd: int, headers: list[str]) -> None:
    """``cand_<rnd>`` from 'Given Names Surname' (2015) or 'SURNAME Given Names' (2010) headers.

    Votes are filled from precinct sums. DuckDB list slices are inclusive, so ``parts[:-2]`` is every
    word but the last (the 2015 surname); 2010 surnames are the upper-case words.
    """
    con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_names (header VARCHAR)")
    con.executemany(f"INSERT INTO cand_{rnd}_names VALUES (?)", [[h] for h in headers])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cand_{rnd} AS
        SELECT {year} AS year, {rnd} AS round,
               CAST(row_number() OVER (ORDER BY last_name, header) AS INT) AS candidate_id,
               first_names || ' ' || last_name AS full_name, last_name, first_names,
               NULL AS gender, NULL AS occupation, NULL AS residence, NULL AS party_membership,
               NULL AS committee_name, CAST(NULL AS BIGINT) AS votes, CAST(NULL AS DECIMAL(6, 2)) AS pct,
               false AS elected, name_key(header) AS nkey
        FROM (
          SELECT header,
                 CASE WHEN surname(header) <> '' THEN surname(header) ELSE upper(parts[-1]) END AS last_name,
                 CASE WHEN surname(header) <> '' THEN given_names(header) ELSE array_to_string(parts[:-2], ' ') END AS first_names
          FROM (SELECT header, str_split(trim(header), ' ') AS parts FROM cand_{rnd}_names)
        )
        """
    )


def _fix_2015_round2_teryt(con: duckdb.DuckDBPyConnection, src: str) -> str:
    """Round 2 of 2015 publishes TERYT 0; take it from the matching round 1 precinct.

    Precincts are matched on (voivodeship, gmina name, number). A few voivodeships have two gminas
    with the same name (e.g. two "gm. Czarna" in podkarpackie); both files list precincts in the same
    order, so ties are paired by their position within the key, and the pairing is checked by the
    number of eligible voters.
    """
    key = """concat_ws('|', lower("Województwo"), "Gmina", CAST(int_("Numer obwodu") AS VARCHAR))"""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE r2_fixed AS
        WITH r1 AS (
          SELECT {key} AS k, row_number() OVER (PARTITION BY {key} ORDER BY rn) AS ord, "TERYT gminy" AS teryt,
                 int_("Liczba wyborców uprawnionych do głosowania") AS eligible
          FROM (SELECT *, row_number() OVER () AS rn FROM src_prez_results_1)
        ), r2 AS (
          SELECT *, {key} AS k, row_number() OVER (PARTITION BY {key} ORDER BY rn) AS ord
          FROM (SELECT *, row_number() OVER () AS rn FROM {src})
        )
        SELECT r2.* EXCLUDE (k, ord, rn) REPLACE (r1.teryt AS "TERYT gminy"),
               abs(r1.eligible - int_(r2."Liczba wyborców uprawnionych do głosowania")) * 1.0 / nullif(r1.eligible, 0) AS eligible_change
        FROM r2 LEFT JOIN r1 USING (k, ord)
        """
    )
    unmatched, tie_drift = con.execute(
        """
        SELECT count(*) FILTER (WHERE "Gmina" IS NOT NULL AND eligible_change IS NULL AND int_("Liczba wyborców uprawnionych do głosowania") > 0),
               max(eligible_change)
        FROM r2_fixed
        """
    ).fetchone()
    log.info("2015 round 2: %s precincts without a round 1 match; max eligible-voter change %.3f", unmatched, tie_drift or 0)
    con.execute("ALTER TABLE r2_fixed DROP COLUMN eligible_change")
    return "r2_fixed"


def build_prezydent(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check both rounds of ``sources.year``; return the loaded elections."""
    year = sources.year
    elections = []
    for rnd, spec in SOURCES[year].items():
        src = sources.load(f"prez_results_{rnd}", spec["results"])
        if year == 2015 and rnd == 2:
            src = _fix_2015_round2_teryt(con, src)
        headers = vote_columns(con, src)
        if "candidates" in spec:
            _candidates_from_register(con, year, rnd, sources.load(f"prez_candidates_{rnd}", spec["candidates"]))
        else:
            _candidates_from_headers(con, year, rnd, headers)
        keys = dict(con.execute(f"SELECT nkey, candidate_id FROM cand_{rnd}").fetchall())
        mapping = {h: keys[python_name_key(h)] for h in headers if python_name_key(h) in keys}
        con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_unmapped AS SELECT unnest(?::VARCHAR[]) AS header",
                    [[h for h in headers if h not in mapping]])
        con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_columns AS SELECT unnest(?::INT[]) AS candidate_id", [list(mapping.values())])
        if column(con, src, *PROTOCOL_ALIASES["invalid_votes"], required=False):
            con.execute(f"CREATE OR REPLACE TABLE prot_{rnd} AS SELECT {precinct_select(con, src)} FROM {src}")
        else:  # 2010 publishes votes cast and valid votes only; invalid votes are the difference.
            protocol = tuple(name for name in PROTOCOL_ALIASES if name != "invalid_votes")
            con.execute(
                f"""CREATE OR REPLACE TABLE prot_{rnd} AS
                    SELECT *, valid_ballots - valid_votes AS invalid_votes FROM (SELECT {precinct_select(con, src, protocol)} FROM {src})"""
            )
        unpivot_votes(con, src, mapping, f"votes_{rnd}", "candidate_id")
        if "gmina_totals" in spec:
            _fill_2015_totals(con, rnd, sources.load(f"prez_gmina_{rnd}", spec["gmina_totals"]))
        _check_round(con, year, rnd)
        elections.append(Election("prezydent", year, rnd, DATES[(year, rnd)], f"Presidential election {year}, round {rnd}"))
    _assemble(con, year)
    return elections


def _fill_2015_totals(con: duckdb.DuckDBPyConnection, rnd: int, gmina_src: str) -> None:
    """2010/2015: candidate totals from precinct sums; keep the per-gmina file's totals for the check."""
    headers = vote_columns(con, gmina_src)
    keys = dict(con.execute(f"SELECT nkey, candidate_id FROM cand_{rnd}").fetchall())
    selects = [
        f"SELECT {keys[python_name_key(h)]} AS candidate_id, sum(int_(\"{h}\")) AS votes FROM {gmina_src}"
        for h in headers if python_name_key(h) in keys
    ]
    con.execute(f"CREATE OR REPLACE TABLE gmina_totals_{rnd} AS " + " UNION ALL ".join(selects))
    con.execute(
        f"""
        UPDATE cand_{rnd} c SET votes = v.votes, pct = CAST(round(100.0 * v.votes / t.total, 2) AS DECIMAL(6, 2))
        FROM (SELECT candidate_id, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v,
             (SELECT sum(votes) AS total FROM votes_{rnd}) t
        WHERE v.candidate_id = c.candidate_id
        """
    )
    # The 2015 round 1 precinct file has no powiat column; take it from the per-gmina file.
    con.execute(
        f"""
        UPDATE prot_{rnd} p SET powiat = g."Powiat"
        FROM {gmina_src} g WHERE lpad(trim(g.{column(con, gmina_src, "TERYT", "Kod gm.")}), 6, '0') = p.teryt_gmina AND p.powiat IS NULL
        """
    )
    # Elected: over 50% of valid votes (round 1) or the most votes in the run-off (round 2).
    con.execute(
        f"""
        UPDATE cand_{rnd} SET elected = CASE WHEN {rnd} = 2 THEN votes = (SELECT max(votes) FROM cand_{rnd}) ELSE pct > 50 END
        """
    )


def _check_round(con: duckdb.DuckDBPyConnection, year: int, rnd: int) -> None:
    """Consistency checks of one round."""
    checks = {
        f"round {rnd}: duplicate precincts": f"SELECT count(*) - count(DISTINCT pkey) FROM prot_{rnd}",
        f"round {rnd}: vote columns that match no candidate": f"SELECT count(*) FROM cand_{rnd}_unmapped",
        f"round {rnd}: candidates not read from exactly one column": f"""
            SELECT count(*) FROM cand_{rnd} c
            WHERE (SELECT count(*) FROM cand_{rnd}_columns m WHERE m.candidate_id = c.candidate_id) <> 1""",
        # Known gap: a few military precincts abroad are published as "-" but counted in the official
        # totals (2020 round 1: 4 precincts). Precinct sums may then fall short, never exceed.
        f"round {rnd}: candidates with more precinct votes than the official total": f"""
            SELECT count(*) FROM cand_{rnd} c
            LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v USING (candidate_id)
            WHERE coalesce(v.votes, 0) > c.votes""",
        f"round {rnd}: candidates short of the official total although every precinct is published": f"""
            SELECT count(*) FROM cand_{rnd} c
            LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v USING (candidate_id)
            WHERE coalesce(v.votes, 0) < c.votes
              AND NOT EXISTS (SELECT 1 FROM prot_{rnd} WHERE valid_votes IS NULL)""",
        f"round {rnd}: precincts whose valid votes differ from the candidate votes": f"""
            SELECT count(*) FROM prot_{rnd} p
            LEFT JOIN (SELECT pkey, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v USING (pkey)
            WHERE p.valid_votes IS NOT NULL AND p.valid_votes <> coalesce(v.votes, 0)""",
        f"round {rnd}: elected candidates (round 2 must have exactly one)": f"""
            SELECT CASE WHEN {rnd} = 2 THEN abs(count(*) FILTER (WHERE elected) - 1) ELSE 0 END FROM cand_{rnd}""",
    }
    if con.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_name = 'gmina_totals_{rnd}'").fetchone()[0]:
        checks[f"round {rnd}: candidates whose precinct votes differ from the per-gmina file"] = f"""
            SELECT count(*) FROM cand_{rnd} c JOIN gmina_totals_{rnd} g USING (candidate_id) WHERE g.votes <> c.votes"""
    run_checks(con, year, checks)
    unpublished, gap = con.execute(
        f"""
        SELECT (SELECT count(*) FROM prot_{rnd} WHERE valid_votes IS NULL),
               (SELECT sum(votes) FROM cand_{rnd}) - (SELECT sum(votes) FROM votes_{rnd})
        """
    ).fetchone()
    if unpublished:
        log.info("year %s round %s: %s unpublished precincts, %s votes only in official totals", year, rnd, unpublished, gap)


def _assemble(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Final DuckDB tables named like the mart tables (rounds stacked; precinct ids per round)."""
    rounds = sorted(SOURCES[year])
    con.execute(
        "CREATE OR REPLACE TABLE prezydent_candidates AS "
        + " UNION ALL ".join(
            f"""SELECT year, round, candidate_id, full_name, last_name, first_names, gender, occupation, residence,
                       party_membership, committee_name, votes, pct, elected,
                       CASE WHEN round = 1 THEN nkey IN (SELECT nkey FROM cand_2) END AS advanced_to_round2
                FROM cand_{r}"""
            for r in rounds
        )
    )
    for r in rounds:
        con.execute(
            f"CREATE OR REPLACE TABLE prot_{r}_ids AS SELECT *, CAST(row_number() OVER (ORDER BY pkey) AS INT) AS precinct_id FROM prot_{r}"
        )
    con.execute(
        "CREATE OR REPLACE TABLE prezydent_precinct_protocols AS "
        + " UNION ALL ".join(
            f"""SELECT {year} AS year, {r} AS round, precinct_id, teryt_gmina, gmina, powiat, wojewodztwo, precinct_no,
                       address, eligible_voters, ballots_issued, valid_ballots, invalid_votes, valid_votes
                FROM prot_{r}_ids"""
            for r in rounds
        )
    )
    con.execute(
        "CREATE OR REPLACE TABLE prezydent_precinct_candidate_votes AS "
        + " UNION ALL ".join(
            f"SELECT {year} AS year, {r} AS round, p.precinct_id, v.candidate_id, v.votes FROM votes_{r} v JOIN prot_{r}_ids p USING (pkey)"
            for r in rounds
        )
    )
