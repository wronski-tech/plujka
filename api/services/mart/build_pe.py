"""Build the European Parliament tables of one election year in DuckDB (loader side).

Sources (CSV unless noted):
- ``okregi`` (districts), ``wykaz_list`` (official list votes and seats per district), ``kandydaci``
  (candidates with official votes and mandates), 2024 also ``komitety``.
- 2019: ``pe2019-gl-lis-obw.xlsx`` (precincts, protocol, one column per committee) and
  ``wyniki_gl_na_kand_po_obwodach.xlsx`` (one sheet per district: ``Lista nr N - ...`` followed by
  ``<position> SURNAME Given`` columns).
- 2014: no list register; ``pe2014kand.xls`` (candidates with votes and mandates),
  ``pe2014-gl-lis-okr`` (official list votes per district), ``pe2014-gl-lis-obw`` (precincts, protocol,
  ``<list no> - ...`` columns), ``obwody`` (powiat names) and ``pe2014-kand-obw`` (one candidate file per
  regional commission, list blocks as in the 2011 Sejm files).
- 2024: ``protokoly_po_obwodach`` (precincts, protocol, and columns
  ``Okreg D - Głosy na listę nr L - ...`` / ``Okreg D - Głosy na kandydata nr P na liście nr L - NAME``).
- 2004, 2009: results were published per gmina only. ``pe2004-kand`` / ``kandpe2009`` (candidates
  with official votes and mandates) and ``pe2004-lis-gm`` / ``pe2009-lis-gm`` (protocol and list votes
  per gmina; columns are committee names in 2004, ``'<list no> - <committee>'`` in 2009). Each gmina
  is loaded as one row of the precinct tables (``precinct_no`` 0) and there are no per-precinct
  candidate votes; see ``_build_gmina_layout``.

Checks: 13 districts, elected candidates equal seats, precinct list votes equal official list votes
per district, precinct candidate votes equal official candidate votes, every candidate read from
exactly one column, and precinct valid votes equal the sum of list votes.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import duckdb

from api.services.mart.build_common import (
    BuildError,
    Election,
    YearSources,
    column,
    columns_of,
    create_committee_aliases,
    parse_list_blocks,
    pct_expr,
    python_name_key,
    quote_ident,
    run_checks,
    sql_literal,
)
from api.services.mart.build_precincts import GEO_ALIASES, precinct_select, unpivot_votes, vote_columns

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
DATES = {2004: "2004-06-13", 2009: "2009-06-07", 2014: "2014-05-25", 2019: "2019-05-26", 2024: "2024-06-09"}
# Seats of Poland where the files themselves give no independent count (seats come from candidates).
EXPECTED_SEATS = {2004: 54, 2009: 50, 2014: 51}

CANDIDATES = "**/parlament_eu/kandydaci_csv/*.parquet"
LISTS = "**/parlament_eu/wykaz_list_csv/*.parquet"
DISTRICTS = "**/parlament_eu/okregi_csv/*.parquet"
PRECINCT_LISTS_2019 = "**/parlament_eu/pe2019-gl-lis-obw.parquet"
PRECINCT_CANDIDATES_2019 = "**/parlament_eu/wyniki_gl_na_kand_po_obwodach_xlsx/*__sheet*.parquet"
PROTOCOLS_2024 = "**/parlament_eu/protokoly_po_obwodach_csv/*.parquet"
CANDIDATES_2014 = "**/parlament_eu/pe2014kand.parquet"
LISTS_2014 = "**/parlament_eu/pe2014-gl-lis-okr/*.parquet"
PRECINCT_LISTS_2014 = "**/parlament_eu/pe2014-gl-lis-obw/*.parquet"
PRECINCT_CANDIDATES_2014 = "**/parlament_eu/pe2014-kand-obw/*.parquet"
REGISTER_2014 = "**/parlament_eu/obwody-*/*.parquet"
# Lists (district, list number) of the gmina years with a candidate struck off after the ballots were
# printed: the register skips that position, and votes marked for the candidate count for the list
# only, so the list has more votes than its registered candidates.
STRUCK_OFF = {2004: {(8, 3)}, 2009: {(4, 3), (6, 4), (10, 8)}}
# (candidates, gmina list votes, powiat list votes) of the years published per gmina.
GMINA_LAYOUT = {
    2004: ("**/parlament_eu/pe2004-kand.parquet", "**/parlament_eu/pe2004-lis-gm.parquet", "**/parlament_eu/pe2004-lis-pow.parquet"),
    2009: ("**/parlament_eu/kandpe2009.parquet", "**/parlament_eu/pe2009/pe2009-lis-gm__*.parquet",
           "**/parlament_eu/pe2009/pe2009-lis-pow__*.parquet"),
}

_LIST_2024 = re.compile(r"^Okreg (\d+) - Głosy na listę nr (\d+) - ")
_CANDIDATE_2024 = re.compile(r"^Okreg (\d+) - Głosy na kandydata nr (\d+) na liście nr (\d+) - (.+)$")
_LIST_2019 = re.compile(r"^Lista nr (\d+) - ")
_CANDIDATE_2019 = re.compile(r"^(\d+) (.+)$")


def available(sources: YearSources) -> bool:
    """Whether the year has staged EP candidates, lists and per-precinct results."""
    precinct = sources.has(PROTOCOLS_2024) or (sources.has(PRECINCT_LISTS_2019) and sources.has(PRECINCT_CANDIDATES_2019))
    layout_2014 = all(sources.has(p) for p in (CANDIDATES_2014, LISTS_2014, PRECINCT_LISTS_2014, PRECINCT_CANDIDATES_2014))
    layout_gmina = sources.year in GMINA_LAYOUT and all(sources.has(p) for p in GMINA_LAYOUT[sources.year])
    return (sources.has(CANDIDATES) and sources.has(LISTS) and precinct) or layout_2014 or layout_gmina


def build_pe(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check all EP tables of ``sources.year``; return the loaded election."""
    year = sources.year
    create_committee_aliases(con, sources.year)
    if year in GMINA_LAYOUT:
        _build_gmina_layout(con, year, sources)
        _assemble(con, year)
        _check(con, year, candidate_precincts=False)
        return [Election("pe", year, 1, DATES.get(year), f"European Parliament election {year}")]
    if sources.has(CANDIDATES_2014):
        _build_registers_2014(con, year, sources.load("pe_candidates", CANDIDATES_2014), sources.load("pe_lists", LISTS_2014))
    else:
        lists = sources.load("pe_lists", LISTS)
        candidates = sources.load("pe_candidates", CANDIDATES)
        districts = sources.load("pe_districts", DISTRICTS)
        _build_registers(con, year, lists, candidates, districts)

    slots = {(d, l, p): c for c, d, l, p in con.execute(
        "SELECT candidate_id, district_no, list_no, list_position FROM pe_candidates").fetchall()}
    names = {c: python_name_key(n) for c, n in con.execute("SELECT candidate_id, full_name FROM pe_candidates").fetchall()}
    committees = {(d, l): c for d, l, c in con.execute("SELECT district_no, list_no, committee_id FROM pe_district_lists").fetchall()}
    unmapped: list[str] = []
    # Result files each candidate is read from exactly once: (part, district_no), NULL = every district.
    parts: list[tuple[int, int | None]] = [(0, None)]

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
        mapped_candidates = [(cid, 0) for cid in cand_map.values()]
    elif sources.has(PRECINCT_LISTS_2014):
        src = sources.load("pe_protocols", PRECINCT_LISTS_2014)
        con.execute(f"CREATE OR REPLACE TABLE prot AS SELECT {precinct_select(con, src)}, CAST(int_(\"Nr okr.\") AS INT) AS district_no FROM {src}")
        register = sources.load("pe_register", REGISTER_2014)
        con.execute(
            f"""UPDATE prot p SET powiat = o."Powiat" FROM {register} o
                WHERE pkey(o."TERYT gminy", o."Nr obwodu") = p.pkey AND p.teryt_gmina IS NOT NULL"""
        )
        # List numbers are national; vote columns are '<list no> - <committee>'.
        by_list = {list_no: cid for (_district, list_no), cid in committees.items()}
        if len(by_list) != len(set(committees.values())):
            raise BuildError(f"year {year}: a list number is shared by several committees")
        list_map = {}
        for header in vote_columns(con, src):
            key = by_list.get(int(m[1])) if (m := re.match(r"^(\d+) - ", header)) else None
            (list_map.__setitem__(header, key) if key else unmapped.append(header))
        unpivot_votes(con, src, list_map, "list_votes", "committee_id")
        mapped_candidates, parts = _candidate_votes_2014(con, sources, unmapped)
    else:
        src = sources.load("pe_protocols", PRECINCT_LISTS_2019)
        con.execute(f"CREATE OR REPLACE TABLE prot AS SELECT {precinct_select(con, src)}, CAST(int_(\"Nr okr.\") AS INT) AS district_no FROM {src}")
        by_name = dict(con.execute("SELECT committee_name, committee_id FROM pe_committees").fetchall())
        list_map = {}
        for header in vote_columns(con, src):
            key = by_name.get(con.execute("SELECT strip_signature(?)", [header]).fetchone()[0])
            (list_map.__setitem__(header, key) if key else unmapped.append(header))
        unpivot_votes(con, src, list_map, "list_votes", "committee_id")
        mapped_candidates = [(cid, 0) for cid in _candidate_votes_2019(con, sources, slots, names, unmapped)]

    con.execute("CREATE OR REPLACE TABLE pe_unmapped AS SELECT unnest(?::VARCHAR[]) AS header", [unmapped])
    con.execute(
        "CREATE OR REPLACE TABLE pe_cand_columns AS SELECT unnest(?::INT[]) AS candidate_id, unnest(?::INT[]) AS part",
        [[c for c, _ in mapped_candidates], [p for _, p in mapped_candidates]],
    )
    con.execute(
        "CREATE OR REPLACE TABLE pe_result_parts AS SELECT unnest(?::INT[]) AS part, unnest(?::INT[]) AS district_no",
        [[p for p, _ in parts], [d for _, d in parts]],
    )
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
    # 2024 'TERYT m. z.'; 2019 'Kod TERYT' (next to 'Gmina miejsca zamieszkania').
    residence_teryt = column(con, candidates, "TERYT m. z.", "Kod TERYT", required=False)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{district}), int_(k.{cand_list}), int_(k.{position})) AS INT) AS candidate_id,
               CAST(int_(k.{district}) AS INT) AS district_no, l.committee_id,
               CAST(int_(k.{cand_list}) AS INT) AS list_no, CAST(int_(k.{position}) AS INT) AS list_position,
               display_name({full}) AS full_name, surname({full}) AS last_name,
               given_names({full}) AS first_names, gender_(k."Płeć") AS gender, k."Zawód" AS occupation,
               k.{residence} AS residence,
               {f"clean_teryt(k.{residence_teryt})" if residence_teryt else "CAST(NULL AS VARCHAR)"} AS residence_teryt,
               k."Przynależność do partii" AS party_membership,
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


def _build_registers_2014(con: duckdb.DuckDBPyConnection, year: int, candidates: str, lists: str) -> None:
    """2014: committees and candidates from ``pe2014kand``; official list votes, valid votes, voters and
    OKW seats per district from ``pe2014-gl-lis-okr`` (columns ``'<list no> - <committee>'``).

    There is no list register: a district list exists where it has candidates, its seats are its
    elected candidates, and a committee took part in seat allocation iff it won any seat.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_committees AS
        WITH names AS (SELECT DISTINCT strip_signature("Komitet") AS committee_name FROM {candidates})
        SELECT {year} AS year, CAST(row_number() OVER (ORDER BY committee_name) AS INT) AS committee_id, committee_name,
               coalesce((SELECT r.aliases_pg FROM committee_alias_rules r WHERE committee_name LIKE '%' || r.needle || '%'
                         ORDER BY r.priority LIMIT 1), '{{}}') AS aliases,
               committee_name LIKE 'KOALICYJNY%' AS is_coalition
        FROM names
        """
    )
    con.execute("CREATE OR REPLACE TABLE official_list_votes (district_no INT, list_no INT, votes BIGINT)")
    for col in columns_of(con, lists):
        if m := re.match(r"^(\d+) - ", col):
            con.execute(
                f"""INSERT INTO official_list_votes SELECT int_("Nr okr."), {int(m[1])}, int_({quote_ident(col)})
                    FROM {lists} WHERE int_({quote_ident(col)}) IS NOT NULL"""
            )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_district_lists AS
        WITH cand AS (
          SELECT int_(k."Nr okr.") AS district_no, int_(k."Nr listy") AS list_no, any_value(c.committee_id) AS committee_id,
                 count(*) FILTER (WHERE yes_(k."Mandat")) AS seats
          FROM {candidates} k JOIN pe_committees c ON c.committee_name = strip_signature(k."Komitet")
          GROUP BY ALL
        )
        SELECT {year} AS year, CAST(c.district_no AS INT) AS district_no, c.committee_id, CAST(c.list_no AS INT) AS list_no,
               o.votes,
               CAST(round(100.0 * o.votes / int_(d."Głosy ważne"), 2) AS DECIMAL(6, 2)) AS pct_of_district,
               sum(c.seats) OVER (PARTITION BY c.committee_id) > 0 AS in_seat_allocation,
               CAST(c.seats AS INT) AS seats
        FROM cand c
        LEFT JOIN official_list_votes o USING (district_no, list_no)
        LEFT JOIN {lists} d ON int_(d."Nr okr.") = c.district_no
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr okr."), int_(k."Nr listy"), int_(k."Poz.")) AS INT) AS candidate_id,
               CAST(int_(k."Nr okr.") AS INT) AS district_no, l.committee_id,
               CAST(int_(k."Nr listy") AS INT) AS list_no, CAST(int_(k."Poz.") AS INT) AS list_position,
               trim(k."Imiona") || ' ' || upper(trim(k."Nazwisko")) AS full_name, upper(trim(k."Nazwisko")) AS last_name,
               trim(k."Imiona") AS first_names, gender_(k."Płeć") AS gender, k."Zawód" AS occupation,
               k."Miejsce zamieszkania" AS residence, CAST(NULL AS VARCHAR) AS residence_teryt,
               NULLIF(trim(k."Partia"), '') AS party_membership,
               int_(k."Gł. na kand.") AS votes,
               CAST(round(100.0 * int_(k."Gł. na kand.") / NULLIF(int_(k."Gł. na listę"), 0), 2) AS DECIMAL(6, 2)) AS pct_of_list,
               yes_(k."Mandat") AS elected
        FROM {candidates} k
        JOIN pe_district_lists l ON l.district_no = int_(k."Nr okr.") AND l.list_no = int_(k."Nr listy")
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_districts AS
        SELECT {year} AS year, CAST(int_(d."Nr okr.") AS INT) AS district_no, d."Siedziba OKW" AS seat_city,
               CAST((SELECT coalesce(sum(seats), 0) FROM pe_district_lists l WHERE l.district_no = int_(d."Nr okr.")) AS INT) AS seats,
               CAST(int_(d."Liczba uprawnionych do głosowania") AS INT) AS eligible_voters,
               CAST(NULL AS VARCHAR) AS description
        FROM {lists} d
        """
    )


def _build_gmina_layout(con: duckdb.DuckDBPyConnection, year: int, sources: YearSources) -> None:
    """2004/2009: registers from the candidate file, protocol and list votes from the gmina table.

    Each gmina (Warsaw districts in 2009, Warsaw as a whole in 2004, abroad and the two ship gminas)
    becomes one ``prot`` row with ``precinct_no`` 0. A district list exists where it has candidates;
    its votes are the sum over the gminas of its district, its seats the elected candidates, and a
    committee took part in seat allocation iff it won any seat. Checked in ``_check`` against the
    candidates' votes and the powiat table.
    """
    candidates_pattern, gmina_pattern, powiat_pattern = GMINA_LAYOUT[year]
    raw_candidates = sources.load("pe_candidates", candidates_pattern)
    gminas = sources.load("pe_gminas", gmina_pattern)
    powiats = sources.load("pe_powiats", powiat_pattern)
    raw_district = column(con, raw_candidates, "Nr okr.", "Okręg")
    # 2004: one row (Joanna Paul, Poznań list 14) has 'P' as its district number; the OKW seat
    # ('Siedziba OKW') identifies the district, so such rows take the district of their OKW.
    seat = column(con, raw_candidates, "Siedziba OKW", required=False)
    fixed = (
        f"""coalesce(int_(k.{raw_district}), (SELECT any_value(int_(o.{raw_district})) FROM {raw_candidates} o
                                              WHERE o.{seat} = k.{seat} AND int_(o.{raw_district}) IS NOT NULL))"""
        if seat else f"int_(k.{raw_district})"
    )
    # Kept as text like every staged column (the code below parses it with int_).
    con.execute(f"CREATE OR REPLACE TABLE pe_candidates_src AS SELECT k.*, CAST({fixed} AS VARCHAR) AS district_no FROM {raw_candidates} k")
    for name, published, district in con.execute(
        f'SELECT "Imiona" || \' \' || {column(con, raw_candidates, "Nazwisko", "Nawisko")}, {raw_district}, district_no '
        f"FROM pe_candidates_src WHERE int_({raw_district}) IS NULL"
    ).fetchall():
        log.warning("year %s: candidate %s has district number %r; district %s taken from the OKW seat", year, name, published, district)
    candidates = "pe_candidates_src"
    k_district = '"district_no"'
    k_list = column(con, candidates, "Lista", "Nr listy")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_committees AS
        WITH names AS (SELECT DISTINCT strip_signature("Komitet") AS committee_name FROM {candidates})
        SELECT {year} AS year, CAST(row_number() OVER (ORDER BY committee_name) AS INT) AS committee_id, committee_name,
               coalesce((SELECT r.aliases_pg FROM committee_alias_rules r WHERE committee_name LIKE '%' || r.needle || '%'
                         ORDER BY r.priority LIMIT 1), '{{}}') AS aliases,
               committee_name LIKE 'KOALICYJNY%' AS is_coalition
        FROM names
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_cand_lists AS
        SELECT DISTINCT CAST(int_(k.{k_district}) AS INT) AS district_no, CAST(int_(k.{k_list}) AS INT) AS list_no, c.committee_id
        FROM {candidates} k JOIN pe_committees c ON c.committee_name = strip_signature(k."Komitet")
        """
    )
    # Vote columns: committee names (2004; list numbers repeat across districts) or '<list no> - <name>'
    # (2009; national list numbers).
    by_name = dict(con.execute("SELECT committee_name, committee_id FROM pe_committees").fetchall())
    # A list number shared by several committees (2004: 20) cannot identify a '<list no> - ' column.
    by_list = dict(con.execute(
        "SELECT list_no, any_value(committee_id) FROM pe_cand_lists GROUP BY 1 HAVING count(DISTINCT committee_id) = 1"
    ).fetchall())

    def committee_of(header: str) -> int | None:
        if m := re.match(r"^(\d+) - ", header):
            return by_list.get(int(m[1]))
        return by_name.get(con.execute("SELECT strip_signature(?)", [header]).fetchone()[0])

    def list_map_of(source: str) -> dict[str, int]:
        valid = column(con, source, "Liczba głosów ważnych", "Głosy ważne").strip('"')
        headers = [h for h in columns_of(con, source)[columns_of(con, source).index(valid) + 1:] if h != "year"]
        mapping = {h: cid for h in headers if (cid := committee_of(h))}
        unmapped.extend(f"{source}: {h}" for h in headers if h not in mapping)
        return mapping

    unmapped: list[str] = []
    teryt = column(con, gminas, "TERYT gminy", "TERYT")
    district = column(con, gminas, "Nr okr.", "Okręg")
    valid_ballots = column(con, gminas, "Liczba kart ważnych", "Głosy oddane")
    valid_votes = column(con, gminas, "Liczba głosów ważnych", "Głosy ważne")
    # 2009 writes gmina names as 'Bolesławiec, m.' / 'Bolesławiec, gm.'; other years 'm. Bolesławiec'.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE prot AS
        SELECT pkey({teryt}, '0') AS pkey, clean_teryt({teryt}) AS teryt_gmina, 0 AS precinct_no,
               regexp_replace(trim("Gmina"), '^(.*), (m\\.|gm\\.)$', '\\2 \\1') AS gmina,
               CASE WHEN clean_teryt({teryt}) IS NOT NULL THEN "Powiat" END AS powiat,
               CAST(NULL AS VARCHAR) AS address, voivodeship(clean_teryt({teryt})) AS wojewodztwo,
               CAST(int_({column(con, gminas, "Liczba uprawnionych do głosowania", "Upr.")}) AS INT) AS eligible_voters,
               CAST(int_({column(con, gminas, "Liczba kart wydanych", "Wydane karty")}) AS INT) AS ballots_issued,
               CAST(int_({valid_ballots}) AS INT) AS valid_ballots,
               CAST(int_({valid_ballots}) - int_({valid_votes}) AS INT) AS invalid_votes,
               CAST(int_({valid_votes}) AS INT) AS valid_votes,
               CAST(int_({district}) AS INT) AS district_no
        FROM {gminas}
        """
    )
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE gmina_pkeys AS SELECT pkey({teryt}, '0') AS pkey, CAST(int_({district}) AS INT) AS district_no FROM {gminas}"
    )
    # Precinct number 0 stands for the whole gmina (pkey '<teryt>/0', as in ``prot``).
    unpivot_votes(con, f"""(SELECT *, '0' AS "Nr obwodu" FROM {gminas})""", list_map_of(gminas), "list_votes_all", "committee_id")
    # '×' marks a committee without a list in the district; it parses as NULL and is dropped here.
    con.execute(
        """
        CREATE OR REPLACE TABLE list_votes AS
        SELECT v.* FROM list_votes_all v JOIN gmina_pkeys g USING (pkey)
        JOIN pe_cand_lists l ON l.district_no = g.district_no AND l.committee_id = v.committee_id
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE pe_list_votes_outside_lists AS
        SELECT v.* FROM list_votes_all v JOIN gmina_pkeys g USING (pkey)
        WHERE v.votes <> 0 AND NOT EXISTS (
            SELECT 1 FROM pe_cand_lists l WHERE l.district_no = g.district_no AND l.committee_id = v.committee_id)
        """
    )
    # Powiat table, summed per district and committee, for the check.
    p_district = column(con, powiats, "Nr okr.", "Okręg")
    selects = [
        f"SELECT CAST(int_({p_district}) AS INT) AS district_no, {cid} AS committee_id, sum(int_({quote_ident(h)})) AS votes FROM {powiats} GROUP BY 1"
        for h, cid in list_map_of(powiats).items()
    ]
    con.execute("CREATE OR REPLACE TABLE pe_powiat_list_votes AS " + " UNION ALL ".join(selects))

    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_district_lists AS
        WITH cand AS (
          SELECT l.district_no, l.list_no, l.committee_id, count(*) FILTER (WHERE yes_(k."Mandat")) AS seats
          FROM pe_cand_lists l
          JOIN {candidates} k ON int_(k.{k_district}) = l.district_no AND int_(k.{k_list}) = l.list_no
          GROUP BY ALL
        ), votes AS (
          SELECT p.district_no, v.committee_id, sum(v.votes) AS votes FROM list_votes v JOIN prot p USING (pkey) GROUP BY ALL
        ), district_valid AS (SELECT district_no, sum(valid_votes) AS valid_votes FROM prot GROUP BY ALL)
        SELECT {year} AS year, c.district_no, c.committee_id, c.list_no, CAST(v.votes AS BIGINT) AS votes,
               CAST(round(100.0 * v.votes / d.valid_votes, 2) AS DECIMAL(6, 2)) AS pct_of_district,
               sum(c.seats) OVER (PARTITION BY c.committee_id) > 0 AS in_seat_allocation,
               CAST(c.seats AS INT) AS seats
        FROM cand c LEFT JOIN votes v USING (district_no, committee_id) LEFT JOIN district_valid d USING (district_no)
        """
    )
    surname = column(con, candidates, "Nazwisko", "Nawisko")  # 2009 header typo
    position = column(con, candidates, "Nr")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{k_district}), int_(k.{k_list}), int_(k.{position})) AS INT) AS candidate_id,
               CAST(int_(k.{k_district}) AS INT) AS district_no, l.committee_id,
               CAST(int_(k.{k_list}) AS INT) AS list_no, CAST(int_(k.{position}) AS INT) AS list_position,
               trim(k."Imiona") || ' ' || upper(trim(k.{surname})) AS full_name, upper(trim(k.{surname})) AS last_name,
               trim(k."Imiona") AS first_names, gender_(k."Płeć") AS gender, k."Zawód" AS occupation,
               k.{column(con, candidates, "Miejsce zamieszkania", "Miejce zam.")} AS residence,  -- 2004 header typo
               CAST(NULL AS VARCHAR) AS residence_teryt,
               NULLIF(trim(k.{column(con, candidates, "Partia", "Członek")}), '') AS party_membership,
               int_(k."Głosy") AS votes,
               {pct_expr(con, candidates, column(con, candidates, "% głosów na listę", "%"))} AS pct_of_list,
               yes_(k."Mandat") AS elected
        FROM {candidates} k
        JOIN pe_district_lists l ON l.district_no = int_(k.{k_district}) AND l.list_no = int_(k.{k_list})
        """
    )
    seat_city = column(con, candidates, "Siedziba OKW", required=False)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE pe_districts AS
        SELECT {year} AS year, p.district_no,
               {f"(SELECT any_value(k.{seat_city}) FROM {candidates} k WHERE int_(k.{k_district}) = p.district_no)" if seat_city else "CAST(NULL AS VARCHAR)"}
                 AS seat_city,
               CAST((SELECT coalesce(sum(seats), 0) FROM pe_district_lists l WHERE l.district_no = p.district_no) AS INT) AS seats,
               CAST(sum(p.eligible_voters) AS INT) AS eligible_voters,
               CAST(NULL AS VARCHAR) AS description
        FROM prot p GROUP BY p.district_no
        """
    )
    con.execute("CREATE OR REPLACE TABLE cand_votes (pkey VARCHAR, candidate_id INT, votes INT)")
    con.execute("CREATE OR REPLACE TABLE pe_unmapped AS SELECT unnest(?::VARCHAR[]) AS header", [unmapped])
    con.execute("CREATE OR REPLACE TABLE pe_cand_columns (candidate_id INT, part INT)")
    con.execute("CREATE OR REPLACE TABLE pe_result_parts (part INT, district_no INT)")


def _candidate_votes_2014(
    con: duckdb.DuckDBPyConnection, sources: YearSources, unmapped: list[str]
) -> tuple[list[tuple[int, int]], list[tuple[int, int | None]]]:
    """2014: unpivot the candidate files of the 51 regional commissions into ``cand_votes``.

    A file covers part of one district (its precincts' district in ``prot``); candidate columns are
    matched by list block and name (``parse_list_blocks``). Returns the mapped (candidate, file) pairs
    and the (file, district) parts for the column check.
    """
    by_name = {
        (d, l, python_name_key(n)): c
        for c, d, l, n in con.execute("SELECT candidate_id, district_no, list_no, full_name FROM pe_candidates").fetchall()
    }
    mapped: list[tuple[int, int]] = []
    parts: list[tuple[int, int | None]] = []
    selects = []
    for part, path in enumerate(sources.files(PRECINCT_CANDIDATES_2014), start=1):
        source = f"read_parquet({sql_literal(path)}, hive_partitioning = false)"
        teryt = column(con, source, *GEO_ALIASES["teryt"])
        number = column(con, source, *GEO_ALIASES["precinct_no"])
        districts = con.execute(
            f"SELECT DISTINCT p.district_no FROM {source} s JOIN prot p ON p.pkey = pkey(s.{teryt}, s.{number})"
        ).fetchall()
        if len(districts) != 1:
            raise BuildError(f"{path}: precincts of {len(districts)} districts, expected one")
        district = districts[0][0]
        mapping = {}
        for header, ref in parse_list_blocks(columns_of(con, source)).items():
            cid = by_name.get((district, ref.list_no, ref.name_key))
            if cid:
                mapping[header] = cid
            elif not _has_votes(con, source, header):
                log.warning("district %s: column %r of %s has no register entry and no votes (struck-off candidate)",
                            district, header, Path(path).name)
            else:
                unmapped.append(f"{Path(path).name}: {header}")
        unpivot_votes(con, source, mapping, f"cand_votes_{part}", "candidate_id")
        mapped.extend((cid, part) for cid in mapping.values())
        parts.append((part, district))
        selects.append(f"SELECT * FROM cand_votes_{part}")
    if not selects:
        raise BuildError(f"year {sources.year}: no EP candidate files")
    con.execute("CREATE OR REPLACE TABLE cand_votes AS " + " UNION ALL ".join(selects))
    return mapped, parts


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


def _check(con: duckdb.DuckDBPyConnection, year: int, candidate_precincts: bool = True) -> None:
    """Fail the build unless precinct data reproduce the official EP results.

    Years published per gmina (``candidate_precincts`` false) have no candidate votes per unit; their
    list votes are checked against the candidates' votes and the powiat table instead.
    """
    unpublished = "(SELECT DISTINCT district_no FROM pe_precinct_protocols WHERE valid_votes IS NULL)"
    checks = {
        "EP districts != 13": "SELECT abs(count(*) - 13) FROM pe_districts",
        **({f"elected MEPs != {EXPECTED_SEATS[year]}": f"""
            SELECT abs(count(*) FILTER (WHERE elected) - {EXPECTED_SEATS[year]}) FROM pe_candidates"""} if year in EXPECTED_SEATS else {}),
        "district lists without an official vote total": "SELECT count(*) FROM pe_district_lists WHERE votes IS NULL",
        "elected MEPs != seats in district lists": """
            SELECT abs((SELECT count(*) FILTER (WHERE elected) FROM pe_candidates) - (SELECT sum(seats) FROM pe_district_lists))""",
        "candidates without a district list": "SELECT count(*) FROM pe_candidates WHERE committee_id IS NULL",
        "duplicate precincts": "SELECT count(*) - count(DISTINCT pkey) FROM prot",
        "vote rows of unknown precincts": "SELECT count(*) FROM (SELECT pkey FROM list_votes UNION ALL SELECT pkey FROM cand_votes) WHERE pkey NOT IN (SELECT pkey FROM prot)",
        "vote columns that match no list or candidate": "SELECT count(*) FROM pe_unmapped",
        # 2014 has several result files per district (one per regional commission).
        "candidates not read from exactly one column of every result file of their district": """
            SELECT count(*) FROM pe_candidates k,
                 LATERAL (SELECT count(*) AS n FROM pe_result_parts p WHERE coalesce(p.district_no, k.district_no) = k.district_no) f,
                 LATERAL (SELECT count(*) AS n, count(DISTINCT part) AS parts FROM pe_cand_columns m WHERE m.candidate_id = k.candidate_id) c
            WHERE c.n <> f.n OR c.parts <> f.n""",
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
    }
    if not candidate_precincts:
        struck_off = ", ".join(f"({d}, {n})" for d, n in sorted(STRUCK_OFF.get(year, ()))) or "(NULL, NULL)"
        for label in ("candidates not read from exactly one column of every result file of their district",
                      "candidates whose precinct votes differ from the official total"):
            del checks[label]
        checks.update({
            "gmina list votes for a committee without a list in the district": "SELECT count(*) FROM pe_list_votes_outside_lists",
            # Every EP vote is a vote for a candidate, so a list's votes are its candidates' votes.
            "district lists whose candidates' votes differ from the gmina votes, other than STRUCK_OFF lists": f"""
                SELECT count(*) FROM pe_district_lists d
                JOIN (SELECT district_no, committee_id, sum(votes) AS votes FROM pe_candidates GROUP BY ALL) k
                  USING (district_no, committee_id)
                WHERE k.votes <> d.votes AND (d.district_no, d.list_no) NOT IN ({struck_off})""",
            "STRUCK_OFF lists without a skipped position or with no more votes than their candidates": f"""
                SELECT count(*) FROM pe_district_lists d
                JOIN (SELECT district_no, list_no, sum(votes) AS votes, count(*) AS n, max(list_position) AS last
                      FROM pe_candidates GROUP BY ALL) k USING (district_no, list_no)
                WHERE (d.district_no, d.list_no) IN ({struck_off}) AND (k.n = k.last OR k.votes >= d.votes)""",
            "district lists whose gmina votes differ from the powiat table": """
                SELECT count(*) FROM pe_district_lists d
                FULL JOIN (SELECT district_no, committee_id, sum(votes) AS votes FROM pe_powiat_list_votes
                           GROUP BY ALL HAVING sum(votes) IS NOT NULL) p USING (district_no, committee_id)
                WHERE d.votes IS DISTINCT FROM p.votes""",
        })
    run_checks(con, year, checks)
