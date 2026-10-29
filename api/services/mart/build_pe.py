"""Build the European Parliament tables of one election year in DuckDB (loader side).

Sources (CSV unless noted):
- ``okregi`` (districts), ``wykaz_list`` (official list votes and seats per district), ``kandydaci``
  (candidates with official votes and mandates), 2024 also ``komitety``.
- 2019: ``pe2019-gl-lis-obw.xlsx`` (precincts, protocol, one column per committee) and
  ``wyniki_gl_na_kand_po_obwodach.xlsx`` (one sheet per district: ``Lista nr N - ...`` followed by
  ``<position> SURNAME Given`` columns).
- 2024: ``protokoly_po_obwodach`` (precincts, protocol, and columns
  ``Okreg D - Głosy na listę nr L - ...`` / ``Okreg D - Głosy na kandydata nr P na liście nr L - NAME``).

Checks: 13 districts, elected candidates equal seats, precinct list votes equal official list votes
per district, precinct candidate votes equal official candidate votes, every candidate read from
exactly one column, and precinct valid votes equal the sum of list votes.
"""

from __future__ import annotations

import logging
import re

import duckdb

from api.services.mart.build_common import (
    BuildError,
    Election,
    YearSources,
    column,
    columns_of,
    create_committee_aliases,
    pct_expr,
    python_name_key,
    run_checks,
)
from api.services.mart.build_precincts import precinct_select, unpivot_votes, vote_columns

log = logging.getLogger("mart")

TABLES = (
    "pe_districts",
    "pe_committees",
    "pe_district_lists",
    "pe_candidates",
    "pe_precinct_protocols",
    "pe_precinct_list_votes",
    "pe_precinct_candidate_votes",
)
FAMILY = ("pe",)
DATES = {2019: "2019-05-26", 2024: "2024-06-09"}

CANDIDATES = "**/parlament_eu/kandydaci_csv/*.parquet"
LISTS = "**/parlament_eu/wykaz_list_csv/*.parquet"
DISTRICTS = "**/parlament_eu/okregi_csv/*.parquet"
PRECINCT_LISTS_2019 = "**/parlament_eu/pe2019-gl-lis-obw.parquet"
PRECINCT_CANDIDATES_2019 = "**/parlament_eu/wyniki_gl_na_kand_po_obwodach_xlsx/*__sheet*.parquet"
PROTOCOLS_2024 = "**/parlament_eu/protokoly_po_obwodach_csv/*.parquet"

_LIST_2024 = re.compile(r"^Okreg (\d+) - Głosy na listę nr (\d+) - ")
_CANDIDATE_2024 = re.compile(r"^Okreg (\d+) - Głosy na kandydata nr (\d+) na liście nr (\d+) - (.+)$")
_LIST_2019 = re.compile(r"^Lista nr (\d+) - ")
_CANDIDATE_2019 = re.compile(r"^(\d+) (.+)$")


def available(sources: YearSources) -> bool:
    """Whether the year has staged EP candidates, lists and per-precinct results."""
    precinct = sources.has(PROTOCOLS_2024) or (sources.has(PRECINCT_LISTS_2019) and sources.has(PRECINCT_CANDIDATES_2019))
    return sources.has(CANDIDATES) and sources.has(LISTS) and precinct


def build_pe(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check all EP tables of ``sources.year``; return the loaded election."""
    year = sources.year
    create_committee_aliases(con)
    lists = sources.load("pe_lists", LISTS)
    candidates = sources.load("pe_candidates", CANDIDATES)
    districts = sources.load("pe_districts", DISTRICTS)
    _build_registers(con, year, lists, candidates, districts)

    slots = {(d, l, p): c for c, d, l, p in con.execute(
        "SELECT candidate_id, district_no, list_no, list_position FROM pe_candidates").fetchall()}
    names = {c: python_name_key(n) for c, n in con.execute("SELECT candidate_id, full_name FROM pe_candidates").fetchall()}
    committees = {(d, l): c for d, l, c in con.execute("SELECT district_no, list_no, committee_id FROM pe_district_lists").fetchall()}
    unmapped: list[str] = []

    if sources.has(PROTOCOLS_2024):
        src = sources.load("pe_protocols", PROTOCOLS_2024)
        con.execute(f"CREATE OR REPLACE TABLE prot AS SELECT {precinct_select(con, src)}, CAST(int_(\"Nr okręgu\") AS INT) AS district_no FROM {src}")
        list_map, cand_map = {}, {}
        for header in columns_of(con, src):
            if m := _LIST_2024.match(header):
                key = committees.get((int(m[1]), int(m[2])))
                (list_map.__setitem__(header, key) if key else unmapped.append(header))
            elif m := _CANDIDATE_2024.match(header):
                cid = slots.get((int(m[1]), int(m[3]), int(m[2])))
                if cid and names[cid] == python_name_key(m[4]):
                    cand_map[header] = cid
                elif not _has_votes(con, src, header):
                    log.warning("year %s: column %r has no register entry and no votes (struck-off candidate)", year, header)
                else:
                    unmapped.append(header)
        unpivot_votes(con, src, list_map, "list_votes", "committee_id")
        unpivot_votes(con, src, cand_map, "cand_votes", "candidate_id")
        mapped_candidates = list(cand_map.values())
    else:
        src = sources.load("pe_protocols", PRECINCT_LISTS_2019)
        con.execute(f"CREATE OR REPLACE TABLE prot AS SELECT {precinct_select(con, src)}, CAST(int_(\"Nr okr.\") AS INT) AS district_no FROM {src}")
        by_name = dict(con.execute("SELECT committee_name, committee_id FROM pe_committees").fetchall())
        list_map = {}
        for header in vote_columns(con, src):
            key = by_name.get(con.execute("SELECT strip_signature(?)", [header]).fetchone()[0])
            (list_map.__setitem__(header, key) if key else unmapped.append(header))
        unpivot_votes(con, src, list_map, "list_votes", "committee_id")
        mapped_candidates = _candidate_votes_2019(con, sources, slots, names, unmapped)

    con.execute("CREATE OR REPLACE TABLE pe_unmapped AS SELECT unnest(?::VARCHAR[]) AS header", [unmapped])
    con.execute("CREATE OR REPLACE TABLE pe_cand_columns AS SELECT unnest(?::INT[]) AS candidate_id", [mapped_candidates])
    _assemble(con, year)
    _check(con, year)
    return [Election("pe", year, 1, DATES.get(year), f"European Parliament election {year}")]


def _has_votes(con: duckdb.DuckDBPyConnection, source: str, header: str) -> bool:
    """Whether a wide vote column has any non-zero value (struck-off candidates keep empty columns)."""
    return bool(con.execute(f'SELECT coalesce(sum(int_("{header}")), 0) FROM {source}').fetchone()[0])


def _build_registers(con: duckdb.DuckDBPyConnection, year: int, lists: str, candidates: str, districts: str) -> None:
    """Committees, district lists, candidates and districts from the official registers."""
    name_col = column(con, lists, "Nazwa", "Nazwa Komitetu")
    list_no = column(con, lists, "Numer listy", "Nr listy")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_committees AS
        WITH names AS (SELECT DISTINCT strip_signature({name_col}) AS committee_name FROM {lists})
        SELECT {year} AS year, CAST(row_number() OVER (ORDER BY committee_name) AS INT) AS committee_id, committee_name,
               coalesce((SELECT r.aliases_pg FROM committee_alias_rules r WHERE committee_name LIKE '%' || r.needle || '%'
                         ORDER BY r.priority LIMIT 1), '{{}}') AS aliases,
               committee_name LIKE 'KOALICYJNY%' AS is_coalition
        FROM names
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_district_lists AS
        SELECT {year} AS year, CAST(int_(l."Numer okręgu") AS INT) AS district_no, c.committee_id,
               CAST(int_(l.{list_no}) AS INT) AS list_no, int_(l."Liczba głosów") AS votes,
               {pct_expr(con, lists, column(con, lists, 'Procent głosów w okręgu', 'Procent głosów oddanych w okręgu'))} AS pct_of_district,
               yes_(l."Udział w podziale mandatów") AS in_seat_allocation,
               CAST(coalesce(int_(l."Liczba mandatów"), 0) AS INT) AS seats
        FROM {lists} l JOIN pe_committees c ON c.committee_name = strip_signature(l.{name_col})
        """
    )
    if column(con, candidates, "Nazwisko i imiona", required=False):
        full = '"Nazwisko i imiona"'
    else:
        full = """("Imiona" || ' ' || "Nazwisko")"""
    district = column(con, candidates, "Numer okręgu", "Nr okręgu")
    cand_list = column(con, candidates, "Numer listy", "Nr listy")
    position = column(con, candidates, "Numer na liście", "Pozycja na liście")
    residence = column(con, candidates, "Miejsce zamieszkania", "Miejscowość zamieszkania")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{district}), int_(k.{cand_list}), int_(k.{position})) AS INT) AS candidate_id,
               CAST(int_(k.{district}) AS INT) AS district_no, l.committee_id,
               CAST(int_(k.{cand_list}) AS INT) AS list_no, CAST(int_(k.{position}) AS INT) AS list_position,
               display_name({full}) AS full_name, surname({full}) AS last_name,
               given_names({full}) AS first_names, gender_(k."Płeć") AS gender, k."Zawód" AS occupation,
               k.{residence} AS residence, k."Przynależność do partii" AS party_membership,
               int_(k."Liczba głosów") AS votes,
               {pct_expr(con, candidates, '"Procent głosów oddanych na listę"')} AS pct_of_list,
               yes_({column(con, candidates, 'Czy przyznano mandat', 'Czy uzyskał mandat')}) AS elected
        FROM {candidates} k
        JOIN pe_district_lists l ON l.district_no = int_(k.{district}) AND l.list_no = int_(k.{cand_list})
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_districts AS
        SELECT {year} AS year, CAST(int_(d."Numer okręgu") AS INT) AS district_no, d."Siedziba OKW" AS seat_city,
               CAST((SELECT coalesce(sum(seats), 0) FROM pe_district_lists l WHERE l.district_no = int_(d."Numer okręgu")) AS INT) AS seats,
               CAST(int_(d."Wyborcy") AS INT) AS eligible_voters,
               regexp_replace(d."Opis granic", '[\\s,]+$', '') AS description
        FROM {districts} d
        """
    )


def _candidate_votes_2019(con, sources: YearSources, slots: dict, names: dict, unmapped: list[str]) -> list[int]:
    """2019: unpivot the per-district sheets (sheet N = district N) into ``cand_votes``."""
    files = sources.files(PRECINCT_CANDIDATES_2019)
    mapped: list[int] = []
    parts = []
    for path in files:
        district = int(re.search(r"__sheet(\d+)_", path)[1])
        source = f"read_parquet('{path}', hive_partitioning=false)"
        current_list = None
        mapping = {}
        for header in vote_columns(con, source):
            if m := _LIST_2019.match(header):
                current_list = int(m[1])
            elif header.startswith("Razem"):
                continue
            elif (m := _CANDIDATE_2019.match(header)) and current_list is not None:
                cid = slots.get((district, current_list, int(m[1])))
                if cid and names[cid] == python_name_key(m[2]):
                    mapping[header] = cid
                elif not _has_votes(con, source, header):
                    log.warning("district %s: column %r has no register entry and no votes (struck-off candidate)", district, header)
                else:
                    unmapped.append(f"district {district}: {header}")
        unpivot_votes(con, source, mapping, f"cand_votes_{district}", "candidate_id")
        mapped.extend(mapping.values())
        parts.append(f"SELECT * FROM cand_votes_{district}")
    if not parts:
        raise BuildError(f"year {sources.year}: no per-district EP candidate sheets")
    con.execute("CREATE OR REPLACE TABLE cand_votes AS " + " UNION ALL ".join(parts))
    return mapped


def _assemble(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Final protocol and vote tables with precinct ids."""
    con.execute("CREATE OR REPLACE TABLE prot_ids AS SELECT *, CAST(row_number() OVER (ORDER BY pkey) AS INT) AS precinct_id FROM prot")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_precinct_protocols AS
        SELECT {year} AS year, precinct_id, district_no, teryt_gmina, gmina, powiat, wojewodztwo, precinct_no, address,
               eligible_voters, ballots_issued, valid_ballots, invalid_votes, valid_votes
        FROM prot_ids
        """
    )
    for target, source, key in (("pe_precinct_list_votes", "list_votes", "committee_id"), ("pe_precinct_candidate_votes", "cand_votes", "candidate_id")):
        con.execute(
            f"CREATE OR REPLACE TABLE {target} AS SELECT {year} AS year, p.precinct_id, v.{key}, v.votes FROM {source} v JOIN prot_ids p USING (pkey)"
        )


def _check(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Fail the build unless precinct data reproduce the official EP results."""
    unpublished = "(SELECT DISTINCT district_no FROM pe_precinct_protocols WHERE valid_votes IS NULL)"
    run_checks(
        con,
        year,
        {
            "EP districts != 13": "SELECT abs(count(*) - 13) FROM pe_districts",
            "elected MEPs != seats in district lists": """
                SELECT abs((SELECT count(*) FILTER (WHERE elected) FROM pe_candidates) - (SELECT sum(seats) FROM pe_district_lists))""",
            "candidates without a district list": "SELECT count(*) FROM pe_candidates WHERE committee_id IS NULL",
            "duplicate precincts": "SELECT count(*) - count(DISTINCT pkey) FROM prot",
            "vote rows of unknown precincts": "SELECT count(*) FROM (SELECT pkey FROM list_votes UNION ALL SELECT pkey FROM cand_votes) WHERE pkey NOT IN (SELECT pkey FROM prot)",
            "vote columns that match no list or candidate": "SELECT count(*) FROM pe_unmapped",
            "candidates not read from exactly one column": """
                SELECT count(*) FROM pe_candidates k WHERE (SELECT count(*) FROM pe_cand_columns m WHERE m.candidate_id = k.candidate_id) <> 1""",
            "district lists whose precinct votes differ from the official total": f"""
                SELECT count(*) FROM pe_district_lists d
                LEFT JOIN (SELECT p.district_no, v.committee_id, sum(v.votes) AS votes
                           FROM pe_precinct_list_votes v JOIN pe_precinct_protocols p USING (precinct_id) GROUP BY ALL) s
                  USING (district_no, committee_id)
                WHERE coalesce(s.votes, 0) <> d.votes AND d.district_no NOT IN {unpublished}""",
            "candidates whose precinct votes differ from the official total": f"""
                SELECT count(*) FROM pe_candidates k
                LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM pe_precinct_candidate_votes GROUP BY ALL) s USING (candidate_id)
                WHERE coalesce(s.votes, 0) <> k.votes AND k.district_no NOT IN {unpublished}""",
            "precincts whose valid votes differ from their list votes": """
                SELECT count(*) FROM pe_precinct_protocols p
                LEFT JOIN (SELECT precinct_id, sum(votes) AS votes FROM pe_precinct_list_votes GROUP BY ALL) v USING (precinct_id)
                WHERE p.valid_votes IS NOT NULL AND p.valid_votes <> coalesce(v.votes, 0)""",
        },
    )
