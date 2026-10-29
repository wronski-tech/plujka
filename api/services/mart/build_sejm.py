"""Build the Sejm tables of one election year in DuckDB (loader side).

Three source layouts are supported:
- CSV layout (2019, 2023): ``wyniki_gl_na_listy_po_obwodach_sejm`` (precincts, protocol, list votes),
  ``wyniki_gl_na_kand*_po_obwodach_sejm`` (one wide file per district), ``kandydaci_sejm``,
  ``wykaz_list_sejm``, ``okregi_sejm`` and ``obwody_glosowania``.
- 2015 Excel layout: ``wyniki_okr_sejm/NN.xlsx`` (per district: precincts, protocol, list totals and
  candidate votes), ``2015-kand-sejm.xls``, ``obwody2015parl.xlsx`` and ``2015-gl-lis-pow`` (powiat names).
- 2011 layout: one precinct file with list votes, one candidate file per district and registers
  (see ``_build_2011_layout``).
- 2001/2005/2007 layout: one precinct file with list votes, the candidate register with official votes,
  the district table and, in 2007 only, one candidate file per district and list (see
  ``_build_2005_layout``). 2001 and 2005 have no per-precinct candidate results.
- 1991/1993/1997 layout: one precinct file with list votes, the candidate register with official votes
  and seat type (district or national list), the district table and, in 1991, the list table with
  list blocs. 69 seats came from national lists (district 0, see ``_build_1990s_layout``).

All produce the same DuckDB tables, named like the PostgreSQL mart tables (``precincts`` keeps an
extra ``pkey`` column used to join the per-precinct files).
"""

from __future__ import annotations

import logging
import re

import duckdb

from api.services.mart.build_common import (
    PATRONYMIC,
    VOIVODESHIPS,
    BuildError,
    YearSources,
    candidate_index,
    build_wide_votes,
    column,
    columns_of,
    create_committee_aliases,
    district_files,
    map_candidate_columns,
    parse_blocks_2015,
    parse_list_blocks,
    parse_list_position,
    parse_name_dash_label,
    parse_numbered_names,
    pct_expr,
    python_name_key,
    quote_ident,
    run_checks,
    sql_list,
    sql_literal,
)
from api.services.mart.build_precincts import (
    COMBINED_PRECINCTS,
    PRECINCT_TYPES_2001,
    PRECINCT_TYPES_2005,
    create_voivodeships_1975,
    create_warsaw_districts,
    create_warsaw_gminas_2001,
    rows_without_number,
)

log = logging.getLogger("mart")

CSV_LIST_RESULTS = "**/wyniki_gl_na_listy_po_obwodach_sejm_csv/*.parquet"
CSV_CANDIDATE_RESULTS = "**/wyniki_gl_na_kand*_po_obwodach_sejm_csv/*.parquet"
EXCEL_DISTRICT_RESULTS = "**/sejm/wyniki_okr_sejm/*.parquet"
LIST_RESULTS_2011 = "**/sejmsenat/*-sejm-gl-lis-obw.parquet"
CANDIDATE_RESULTS_2011 = "**/sejmsenat/*-sejm-kand-obw/*.parquet"
LIST_RESULTS_2005 = "**/sejm/sejm*-lis-obw/*.parquet"
LIST_RESULTS_2001 = "**/sejm/sejm*-lis-obw.parquet"
LIST_RESULTS_2007 = "**/sejm/sejm*-obw-listy.parquet"
CANDIDATE_RESULTS_2007 = "**/sejm/sejm*-kand-obw/*.parquet"
# Senate district of each precinct: 2005 per gmina, 2007 from the Senate per-precinct files.
SENATE_GMINAS_2005 = "**/*-parl-frekw-gm.parquet"
SENATE_RESULTS_2007 = "**/senat/parl*-sen-gl_kand_obw/*.parquet"
SENATE_RESULTS_2001 = "**/senat/parl*OBWSEN/*.parquet"

PROTOCOL_ALIASES = {
    "eligible_voters": (
        "Liczba wyborców uprawnionych do głosowania", "Sejm - Liczba wyborców uprawnionych do głosowania",
        "Liczba uprawnionych do głosowania",
    ),
    "ballots_issued": (
        "Liczba wyborców, którym wydano karty do głosowania w lokalu wyborczym oraz w głosowaniu korespondencyjnym (łącznie)",
        "Liczba wyborców, którym wydano karty do głosowania",
        "Sejm - Liczba wyborców, którym wydano karty do głosowania",
        "Liczba kart wydanych",
    ),
    "valid_ballots": ("Liczba kart ważnych", "Sejm - Liczba kart ważnych", "Karty ważne"),
    "invalid_votes": ("Liczba głosów nieważnych", "Sejm - Liczba głosów nieważnych", "Głosy nieważne"),
    "valid_votes": (
        "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
        "Sejm - Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
        "Głosy ważne",
        "Ważne",  # 2007
    ),
}
# 2005/2007 headers. 2007 has no invalid-votes column: 'Odd.' (votes cast) = valid ballots, and invalid
# votes are cast minus valid.
PROTOCOL_ALIASES["eligible_voters"] += ("Uprawnieni do głosowania", "Upr.")
PROTOCOL_ALIASES["ballots_issued"] += ("Karty wydane (frekwencja)", "Frekw.")
PROTOCOL_ALIASES["valid_ballots"] += ("Karty ważne (głosy oddane)", "Odd.")
# 2001 headers ('Karty ważne' is already an alias of valid ballots).
PROTOCOL_ALIASES["ballots_issued"] += ("Wydane karty (frekwencja)",)
PROTOCOL_ALIASES["invalid_votes"] += ("Głosy nieważne z kart ważnych",)
PROTOCOL_ALIASES["valid_votes"] += ("Głosy ważne z kart ważnych",)

# TERYT 'rodzaj gminy' codes used by the 2011 precinct file ('Teren obw.'), worded like the 2015 register.
AREA_TYPES_2011 = {
    "1": "gmina miejska", "2": "gmina wiejska", "4": "miasto w gminie miejsko-wiejskiej",
    "5": "obszar wiejski w gminie miejsko-wiejskiej", "Z": "zagranica", "S": "statek",
    "8": "dzielnica w m.st. Warszawa",  # 2007; 2005 gives the district letter instead (WARSAW_DISTRICTS_2005)
}


# 1991-1997 sources per year: precinct results with list votes, candidates (with official votes, seat type
# and national-list position), district results and, in 1991, the district lists with their blocs.
SOURCES_1990S = {
    1991: {"results": "**/sejm/1991-listy-obwody.parquet", "candidates": "**/sejm/kandsejm1991kom.parquet",
           "districts": "**/sejm/okreg1991sejm.parquet", "lists": "**/sejm/sejm1991/listyokr1991__*.parquet"},
    1993: {"results": "**/sejm/1993-listy-obw/*.parquet", "candidates": "**/sejm/kandsejm1993kom.parquet",
           "districts": "**/sejm/okregi1993.parquet"},
    1997: {"results": "**/sejm/sejm1997obw.parquet", "candidates": "**/sejm/kandsejm1997pop.parquet",
           "districts": "**/sejm/sejm1997okregi.parquet", "national_lists": "**/sejm/listy-kandydaci/OGPOL-*.parquet"},
}
# 1997 registered national lists (listy-kandydaci.zip, OGPOL-NN = list NN: position, surname, given names, district of
# the candidate's district list), checked against the candidate register's national-list positions. Rows that differ,
# by (list number, position in OGPOL), as described in the archive's informacje_dodatkowe.txt and found by the check:
NATIONAL_LIST_EXCEPTIONS_1997 = {
    # Struck off after registration, not in the candidate register (positions of later candidates kept).
    (6, 15): "ŻOCHOWSKI Ryszard Jacek, struck off (informacje_dodatkowe.txt)",
    (10, 24): "MORAWSKI Zbigniew Marian, struck off (informacje_dodatkowe.txt)",
    # Not in the candidate register either. informacje_dodatkowe.txt names URBAŃCZYK Andrzej Tadeusz (position 66) as
    # the second struck-off SLD candidate, but he is in the register at position 66 and won a district seat (21).
    (6, 115): "POKRZEPA Władysław Kazimierz, not in the candidate register",
    # OGPOL-01 prints position 35 twice (SAWICKI, MIKOSZ) and no 36; the register has MIKOSZ at 36.
    (1, 35): "MIKOSZ Jerzy Maria, register position 36",
    # Surname spelled differently in the register.
    (5, 32): "SMERCZYŃSKA Maria Władysława, register SMERECZYŃSKA",
    (5, 98): "ZWIERZYNIECKI Krzysztof, register ZWIERZYŃSKI",
    (7, 87): "OPALSKI Jacek, register OPALSKI-WIECZOREK",
}
# Register-side rows of NATIONAL_LIST_EXCEPTIONS_1997 (list number, register position) with no OGPOL row.
NATIONAL_LIST_REGISTER_ONLY_1997 = {(1, 36)}
# National-list positions held by two candidates, in the register and in OGPOL alike (PSL list 7: SIWOŃ and GWIŻDŻ at
# 28, no 29), as (year, list number, position).
SHARED_NATIONAL_LIST_POSITIONS = {(1997, 7, 28)}
# Minimum candidate age (passive electoral right) in 1991-1997: 21 for the Sejm (art. 8 of the 1991 law, art. 14 of the
# 1993 law) and, through art. 1 of the Senate law of 10 May 1991, for the Senate (30 only from the 1997 Constitution,
# in force since 17 October 1997, after that year's election).
MIN_CANDIDATE_AGE_1990S = 21
# Sejm districts per year where not 41: 1991 37, 1993/1997 52, each plus district 0 (national list).
SEJM_DISTRICTS = {1991: 38, 1993: 53, 1997: 53}
# Seats elected from national lists (lista ogólnopolska) 1991-1997; stored as district 0.
NATIONAL_LIST_SEATS = 69
# National figures and national-list seats per list number from PKW's announcements: M.P. 1991 nr 41
# poz. 288, M.P. 1993 nr 50 poz. 470, M.P. 1997 nr 64 poz. 620 ("głosy oddane" = valid + invalid votes).
# Known gaps between the KBW files and the announcements, named in the checks:
# - 1991: the announcement counts 22,341 precinct commissions; every KBW file (precincts, gminas,
#   districts) has 22,340 precincts, and all their votes add up to the announced figures.
# - 1993: KBW's precinct and district files give 9,000 fewer eligible voters in district 21 (Kraków:
#   900,774, announced 909,774) and 12,807 fewer in district 32 (Piła: 337,497, announced 350,304); the
#   announced district turnouts (52.41%, 56.74%) confirm the announced figures. Votes are equal.
OFFICIAL_1990S = {
    1991: {"precincts": 22341, "precincts_without_results": 1, "eligible_voters": 27517280, "votes_cast": 11887949,
           "invalid_votes": 669347, "valid_votes": 11218602,
           "national_list_seats": {2: 7, 12: 7, 13: 5, 17: 7, 30: 4, 32: 6, 54: 11, 60: 10, 63: 3, 20: 1, 41: 8}},
    1993: {"precincts": 22482, "eligible_voters": 27677302, "eligible_voters_missing": {21: 9000, 32: 12807},
           "votes_cast": 14415586, "invalid_votes": 619359, "valid_votes": 13796227,
           "national_list_seats": {5: 26, 6: 20, 9: 14, 11: 9}},
    1997: {"precincts": 23074, "eligible_voters": 28409054, "votes_cast": 13616378, "invalid_votes": 528147,
           "valid_votes": 13088231, "national_list_seats": {4: 11, 5: 29, 6: 23, 7: 6}},
}
# Coalition committees (8% national threshold, art. 3 ust. 2 of the 1993 electoral law; 1991 had no
# threshold): SLD, named as a coalition in the 1993 and 1997 announcements, and the Catholic Electoral
# Committee "Ojczyzna" (1993 list 2), which won 6.37% and no seat, which only a coalition could.
COALITION_LISTS_1990S = {1993: {2, 5}, 1997: {6}}
# National-minority lists exempt from the threshold, as (list number, district or None for all districts),
# from the announcements: 1991 the minority national lists (art. 100 ust. 4 of the 1991 law), 1993 and
# 1997 the district lists exempt from the 5% threshold (art. 5 of the 1993 law). The Olsztyn German minority
# list of 1993 (list 25, district 29) did not claim the exemption and is not one.
MINORITY_LISTS_1990S = {
    1991: {(20, None), (48, None), (55, None)},
    1993: {(16, None), (25, 4), (25, 9), (25, 16), (25, 30)},
    1997: {(14, None), (15, 4), (15, 9), (15, 10), (15, 16), (15, 29), (15, 30)},
}
# 1991 national lists joined in blocs (art. 77 ust. 3 of the 1991 law), from the announcement; the bloc is
# stored as list_group (its first list number) on the national-list rows. District blocs are in the data.
NATIONAL_BLOCS_1991 = ((20, 48), (41, 49, 58, 62))
# 1991: national lists counted only the district lists linked to them (art. 100 ust. 2 of the 1991 law), and
# the linkage is not in the data files. Districts where a committee won a seat with a list not linked to its
# national list, from the announcement's table (columns 3 and 4): Christian Democracy (list 7) won seats in
# 5 districts but only 4 linked ones, so its national list did not qualify. National vote shares use all
# district lists (the linked sums are 0-4% lower, which changes neither qualification nor seats).
UNLINKED_SEAT_DISTRICTS_1991 = {7: 1, 12: 2, 13: 1, 17: 1, 63: 1}
# 1991 precincts abroad: Warszawa-Śródmieście (01009) precincts numbered from 1001; the file has no
# precinct type.
ABROAD_1991 = ("01009", 1001)


def available_layout(sources: YearSources) -> str | None:
    """'csv', 'excel', '2011', '2005', '1990s' or None when the year has no staged Sejm precinct results."""
    if sources.year in SOURCES_1990S:
        return "1990s" if sources.has(SOURCES_1990S[sources.year]["results"]) else None
    if sources.has(CSV_LIST_RESULTS):
        return "csv"
    if sources.has(EXCEL_DISTRICT_RESULTS):
        return "excel"
    if sources.has(LIST_RESULTS_2011):
        return "2011"
    if sources.has(LIST_RESULTS_2005) or sources.has(LIST_RESULTS_2007) or sources.has(LIST_RESULTS_2001):
        return "2005"
    return None


def build_sejm(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check all Sejm tables of ``sources.year``."""
    layout = available_layout(sources)
    if layout is None:
        raise BuildError(f"year {sources.year}: no staged Sejm precinct results")
    create_committee_aliases(con, sources.year)
    # Rows of candidate files without a precinct number (2011: combined military precincts, see
    # ``rows_without_number``) and their votes per candidate; empty for other layouts.
    con.execute("CREATE OR REPLACE TABLE sejm_rows_without_number (district_no INT, address VARCHAR)")
    if layout == "csv":
        _build_csv_layout(con, sources)
    elif layout == "excel":
        _build_excel_layout(con, sources)
    elif layout == "2011":
        _build_2011_layout(con, sources)
    elif layout == "1990s":
        _build_1990s_layout(con, sources)
    else:
        _build_2005_layout(con, sources)
    # Columns that only the 1991-1997 layout fills (pre-1999 gmina code, list blocs, national lists, age).
    con.execute("ALTER TABLE precincts ADD COLUMN IF NOT EXISTS teryt_gmina_pre1999 VARCHAR")
    con.execute("ALTER TABLE sejm_district_lists ADD COLUMN IF NOT EXISTS list_group INT")
    con.execute("ALTER TABLE sejm_candidates ADD COLUMN IF NOT EXISTS national_list_position INT")
    con.execute("ALTER TABLE sejm_candidates ADD COLUMN IF NOT EXISTS national_list_seat BOOLEAN DEFAULT false")
    con.execute("ALTER TABLE sejm_candidates ADD COLUMN IF NOT EXISTS age INT")  # published 1991-1997 only
    con.execute(
        """
        CREATE OR REPLACE TABLE sejm_combined_candidate_votes AS
        SELECT m.key AS candidate_id, sum(coalesce(int_(r.votes), 0)) AS votes
        FROM sejm_precinct_candidate_votes_raw r
        JOIN sejm_precinct_candidate_votes_columns m ON m.district_no = r.district_no AND m.header = r.header
        WHERE r.pkey IS NULL AND r.district_no IN (SELECT district_no FROM sejm_rows_without_number)
        GROUP BY ALL
        """
    )
    # 2001, 2005 and 1991-1997 publish no per-precinct candidate results.
    _check(con, sources.year, candidate_precincts=not (
        sources.has(LIST_RESULTS_2005) or sources.has(LIST_RESULTS_2001) or layout == "1990s"))


def _protocol_select(con: duckdb.DuckDBPyConnection, table: str) -> str:
    """SELECT list of the five protocol columns, resolved against ``table``'s headers.

    Without an invalid-votes column (2007) invalid votes are valid ballots (votes cast) minus valid votes.
    """
    cols = {
        name: f"int_({col})" if (col := column(con, table, *aliases, required=name != "invalid_votes")) else None
        for name, aliases in PROTOCOL_ALIASES.items()
    }
    if cols["invalid_votes"] is None:
        cols["invalid_votes"] = f"{cols['valid_ballots']} - {cols['valid_votes']}"
    return ",\n".join(f"{expr} AS {name}" for name, expr in cols.items())


def _committee_table(con: duckdb.DuckDBPyConnection, year: int, names_sql: str) -> None:
    """Create ``sejm_committees`` from ``names_sql`` yielding (committee_name, min_list)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_committees AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY min_list, committee_name) AS INT) AS committee_id,
               committee_name,
               coalesce((SELECT arg_min(r.aliases_pg, r.priority) FROM committee_alias_rules r
                         WHERE contains(upper(committee_name), r.needle)), '{{}}') AS aliases,
               upper(committee_name) LIKE 'KOALICYJNY KOMITET%' AS is_coalition,
               contains(upper(committee_name), 'MNIEJSZOŚĆ') AS is_minority
        FROM ({names_sql})
        """
    )


def _districts_wojewodztwo(con: duckdb.DuckDBPyConnection) -> str:
    """SQL subquery: most common voivodeship of each Sejm district's precincts."""
    return "(SELECT sejm_district_no, mode(wojewodztwo) AS wojewodztwo FROM precincts GROUP BY 1)"


def _build_csv_layout(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2019/2023 CSV files."""
    year = sources.year
    results = sources.load("sejm_results", CSV_LIST_RESULTS)
    register = sources.load("precinct_register", "**/sejmsenat/obwody_glosowania_csv/*.parquet")
    candidates_src = sources.load("sejm_candidates", "**/kandydaci_sejm_csv/*.parquet")
    lists_src = sources.load("sejm_lists", "**/wykaz_list_sejm_csv/*.parquet")
    districts_src = sources.load("sejm_districts", "**/okregi_sejm_csv/*.parquet")

    r_teryt = column(con, results, "TERYT Gminy", "Kod TERYT")
    r_number = column(con, results, "Nr komisji", "Numer")
    o_teryt = column(con, register, "TERYT gminy")
    o_number = column(con, register, "Numer")
    r_type = f"r.{col}" if (col := column(con, results, "Typ obwodu", required=False)) else "NULL"
    r_area = f"r.{col}" if (col := column(con, results, "Typ obszaru", required=False)) else "NULL"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE precincts AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY clean_teryt(r.{r_teryt}) NULLS LAST, r."Gmina", int_(r.{r_number})) AS INT)
                 AS precinct_id,
               pkey(r.{r_teryt}, r.{r_number}) AS pkey,
               clean_teryt(r.{r_teryt}) AS teryt_gmina,
               r."Gmina" AS gmina,
               -- 2019 files assign abroad precincts to powiat 'zagranica' / mazowieckie; the mart uses NULL.
               CASE WHEN clean_teryt(r.{r_teryt}) IS NOT NULL THEN r."Powiat" END AS powiat,
               CASE WHEN clean_teryt(r.{r_teryt}) IS NOT NULL THEN r."Województwo" END AS wojewodztwo,
               CAST(int_(r.{column(con, results, "Nr okręgu", "Okręg")}) AS INT) AS sejm_district_no,
               CAST(int_(o.{column(con, register, "Okręg Senat", "Numer okręgu do Senatu")}) AS INT) AS senat_district_no,
               CAST(int_(r.{r_number}) AS INT) AS precinct_no,
               r."Siedziba" AS address,
               coalesce({r_type}, o."Typ obwodu") AS precinct_type,
               coalesce({r_area}, o."Typ obszaru") AS area_type,
               CAST(int_(o."Mieszkańcy") AS INT) AS residents,
               o.{o_number} IS NOT NULL AS in_register
        FROM {results} r
        LEFT JOIN {register} o ON pkey(o.{o_teryt}, o.{o_number}) = pkey(r.{r_teryt}, r.{r_number})
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, {_protocol_select(con, results)}
        FROM {results} r JOIN precincts p ON p.pkey = pkey(r.{r_teryt}, r.{r_number})
        """
    )

    l_name = column(con, lists_src, "Nazwa Komitetu", "Nazwa")
    l_list = column(con, lists_src, "Nr listy", "Numer listy")
    _committee_table(
        con, year, f"SELECT strip_signature({l_name}) AS committee_name, min(int_({l_list})) AS min_list FROM {lists_src} GROUP BY 1"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_district_lists AS
        SELECT {year} AS year,
               CAST(int_(w."Numer okręgu") AS INT) AS district_no,
               c.committee_id,
               CAST(int_(w.{l_list}) AS INT) AS list_no,
               CAST(int_(w."Liczba kandydatów") AS INT) AS candidates_count,
               int_(w."Liczba głosów") AS votes,
               {pct_expr(con, lists_src, column(con, lists_src, "Procent głosów oddanych w okręgu", "Procent głosów w okręgu"))} AS pct_of_district,
               yes_(w."Udział w podziale mandatów") AS in_seat_allocation,
               CAST(int_(w."Liczba mandatów") AS INT) AS seats
        FROM {lists_src} w JOIN sejm_committees c ON c.committee_name = strip_signature(w.{l_name})
        """
    )

    if column(con, candidates_src, "Nazwisko i imiona", required=False):
        full_name = '"Nazwisko i imiona"'
    else:
        full_name = """("Imiona" || ' ' || "Nazwisko")"""
    k_list = column(con, candidates_src, "Nr listy", "Nr listy (po nadaniu)")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr okręgu"), int_(k.{k_list}), int_(k."Pozycja na liście")) AS INT)
                 AS candidate_id,
               CAST(int_(k."Nr okręgu") AS INT) AS district_no,
               c.committee_id,
               CAST(int_(k.{k_list}) AS INT) AS list_no,
               CAST(int_(k."Pozycja na liście") AS INT) AS list_position,
               display_name({full_name}) AS full_name,
               surname({full_name}) AS last_name,
               given_names({full_name}) AS first_names,
               gender_(k."Płeć") AS gender,
               k."Zawód" AS occupation,
               k."Miejsce zamieszkania" AS residence,
               {f"clean_teryt(k.{teryt})" if (teryt := column(con, candidates_src, "TERYT m. z.", required=False)) else "CAST(NULL AS VARCHAR)"}
                 AS residence_teryt,
               k."Przynależność do partii" AS party_membership,
               NULLIF(trim(k."Poparcie"), '') AS party_support,
               int_(k."Liczba głosów") AS votes,
               {pct_expr(con, candidates_src, '"Procent głosów oddanych na listę"')} AS pct_of_list,
               {pct_expr(con, candidates_src, '"Procent głosów oddanych w okręgu"')} AS pct_of_district,
               yes_(k."Czy przyznano mandat") AS elected
        FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k."Nazwa komitetu")
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_districts AS
        SELECT {year} AS year,
               CAST(int_(d."Numer okręgu") AS INT) AS district_no,
               d."Siedziba OKW" AS seat_city,
               w.wojewodztwo,
               CAST(int_(d."Liczba mandatów") AS INT) AS seats,
               CAST(int_(d."Wyborcy") AS INT) AS eligible_voters,
               regexp_replace(d."Opis granic", '\\s+', ' ', 'g') AS description
        FROM {districts_src} d
        LEFT JOIN {_districts_wojewodztwo(con)} w ON w.sejm_district_no = int_(d."Numer okręgu")
        """
    )

    # List votes: one wide file; committee columns carry the committee name (2019: plus " - ZPOW-..." signature).
    committees = {name: cid for cid, name in con.execute("SELECT committee_id, committee_name FROM sejm_committees").fetchall()}
    stripped = {
        col: con.execute("SELECT strip_signature(?)", [col]).fetchone()[0] for col in columns_of(con, results)
    }
    list_mapping = {col: committees[name] for col, name in stripped.items() if name in committees}
    results_path = sources.files(CSV_LIST_RESULTS)[0]
    build_wide_votes(
        con, year=year, target="sejm_precinct_list_votes", key_name="committee_id",
        files=[(0, results_path, list_mapping)],
        teryt_aliases=("TERYT Gminy", "Kod TERYT"), number_aliases=("Nr komisji", "Numer"),
    )

    index = candidate_index(con, "sejm_candidates", "committee_id")
    files = map_candidate_columns(
        con,
        files_by_district=district_files(sources.files(CSV_CANDIDATE_RESULTS), r"(?:okreg|okr)_(\d+)_"),
        index=index,
        parse=lambda cols, _district: parse_list_position(cols) or parse_name_dash_label(cols),
        unmapped_table="sejm_candidate_columns_unmapped",
    )
    build_wide_votes(
        con, year=year, target="sejm_precinct_candidate_votes", key_name="candidate_id", files=files,
        teryt_aliases=("TERYT Gminy", "Kod TERYT"), number_aliases=("Nr komisji", "Numer"),
    )


def _build_excel_layout(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2015 Excel files (staged to Parquet)."""
    year = sources.year
    by_district = district_files(sources.files(EXCEL_DISTRICT_RESULTS), r"^(\d+)\.parquet$")
    candidates_src = sources.load("sejm_candidates", "**/sejm/*kand-sejm*.parquet")
    register = sources.load("precinct_register", "**/obwody*parl*.parquet")
    powiats = sources.load("powiats", "**/sejm/*gl-lis-pow/*.parquet")
    con.execute(
        "CREATE OR REPLACE TABLE voivodeships AS SELECT * FROM (VALUES "
        + ", ".join(f"({sql_literal(code)}, {sql_literal(name)})" for code, name in VOIVODESHIPS.items())
        + ") t(code, name)"
    )

    # Each precinct appears in exactly one district file, together with its Sejm protocol.
    parts = []
    for district, path in by_district.items():
        source = f"read_parquet({sql_literal(path)})"
        parts.append(
            f"""SELECT {district} AS sejm_district_no, "TERYT gminy" AS teryt, "Numer obwodu" AS nr, "Gmina" AS gmina,
                       "Nazwa komisji" AS commission, {_protocol_select(con, source)} FROM {source}"""
        )
    con.execute("CREATE OR REPLACE TABLE src_sejm_results AS " + " UNION ALL ".join(parts))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE precincts AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY clean_teryt(r.teryt) NULLS LAST, r.gmina, int_(r.nr)) AS INT) AS precinct_id,
               pkey(r.teryt, r.nr) AS pkey,
               clean_teryt(r.teryt) AS teryt_gmina,
               r.gmina,
               pw.powiat,
               v.name AS wojewodztwo,
               r.sejm_district_no,
               CAST(int_(o."Nr okr. Senat") AS INT) AS senat_district_no,
               CAST(int_(r.nr) AS INT) AS precinct_no,
               coalesce(o."Pełna siedziba", r.commission) AS address,
               o."Typ" AS precinct_type,
               o."Teren" AS area_type,
               CAST(int_(o."Mieszk.") AS INT) AS residents,
               o."Nr obw." IS NOT NULL AS in_register
        FROM src_sejm_results r
        LEFT JOIN {register} o ON pkey(o."TERYT", o."Nr obw.") = pkey(r.teryt, r.nr)
        LEFT JOIN (SELECT lpad("TERYT", 4, '0') AS teryt4, any_value("Powiat") AS powiat FROM {powiats} GROUP BY 1) pw
          ON pw.teryt4 = left(clean_teryt(r.teryt), 4)
        LEFT JOIN voivodeships v ON v.code = left(clean_teryt(r.teryt), 2)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, r.eligible_voters, r.ballots_issued, r.valid_ballots, r.invalid_votes, r.valid_votes
        FROM src_sejm_results r JOIN precincts p ON p.pkey = pkey(r.teryt, r.nr)
        """
    )

    _committee_table(
        con, year, f'SELECT strip_signature("Komitet") AS committee_name, min(int_("Nr listy")) AS min_list FROM {candidates_src} GROUP BY 1'
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr okr."), int_(k."Nr listy"), int_(k."Poz.")) AS INT) AS candidate_id,
               CAST(int_(k."Nr okr.") AS INT) AS district_no,
               c.committee_id,
               CAST(int_(k."Nr listy") AS INT) AS list_no,
               CAST(int_(k."Poz.") AS INT) AS list_position,
               trim(k."Imiona") || ' ' || trim(k."Nazwisko") AS full_name,
               trim(k."Nazwisko") AS last_name,
               trim(k."Imiona") AS first_names,
               gender_(k."Płeć") AS gender,
               k."Zawód" AS occupation,
               k."Miejsce zam." AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt,
               NULLIF(trim(k."Przynależność do partii politycznej"), '') AS party_membership,
               CAST(NULL AS VARCHAR) AS party_support,
               int_(k."Głosy") AS votes,
               {pct_expr(con, candidates_src, '"% gł. na listę"')} AS pct_of_list,
               {pct_expr(con, candidates_src, '"% gł. w okr."')} AS pct_of_district,
               yes_(k."Mandat") AS elected
        FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k."Komitet")
        """
    )
    # 2015 has no list summary with seats. Per-precinct list votes are the sum of the candidate columns
    # of the list's block; a committee took part in seat allocation iff it won any seat.
    index = candidate_index(con, "sejm_candidates", "committee_id")
    list_committee = {
        (district, list_no): committee_id
        for district, list_no, committee_id in con.execute(
            "SELECT DISTINCT district_no, list_no, committee_id FROM sejm_candidates"
        ).fetchall()
    }
    list_files = []
    for district, path in by_district.items():
        refs, _totals = parse_blocks_2015(columns_of(con, f"read_parquet({sql_literal(path)})"), index.list_names[district])
        list_files.append((district, path, {col: list_committee[(district, ref.list_no)] for col, ref in refs.items()}))
    build_wide_votes(
        con, year=year, target="sejm_precinct_list_votes", key_name="committee_id", files=list_files,
        teryt_aliases=("TERYT gminy",), number_aliases=("Numer obwodu",),
    )
    # Official list totals per district (they also count struck-off candidates, whose per-precinct
    # columns are empty in the district files). Headers are '<list no> - <committee name>'.
    totals_src = sources.load("sejm_district_totals", "**/sejm/*gl-lis-okr/*.parquet")
    committees = {name: cid for cid, name in con.execute("SELECT committee_id, committee_name FROM sejm_committees").fetchall()}
    total_cols = {}
    for col in columns_of(con, totals_src):
        if re.match(r"^\d+ - ", col):
            name = con.execute("SELECT strip_signature(?)", [re.sub(r"^\d+ - ", "", col)]).fetchone()[0]
            if name in committees:
                total_cols[col] = committees[name]
    con.execute("CREATE OR REPLACE TABLE official_list_votes (district_no INT, committee_id INT, votes BIGINT)")
    for col, committee_id in total_cols.items():
        con.execute(
            f"""INSERT INTO official_list_votes
                SELECT int_("Nr okr."), {committee_id}, int_({quote_ident(col)}) FROM {totals_src}
                WHERE int_({quote_ident(col)}) IS NOT NULL"""
        )
    _district_lists_from_candidates(con, year)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_districts AS
        SELECT {year} AS year, l.district_no, any_value(t."Siedziba OKW") AS seat_city, w.wojewodztwo,
               CAST(sum(l.seats) AS INT) AS seats, CAST(NULL AS INT) AS eligible_voters, CAST(NULL AS VARCHAR) AS description
        FROM sejm_district_lists l
        LEFT JOIN {_districts_wojewodztwo(con)} w ON w.sejm_district_no = l.district_no
        LEFT JOIN {totals_src} t ON int_(t."Nr okr.") = l.district_no
        GROUP BY l.district_no, w.wojewodztwo
        """
    )

    files = map_candidate_columns(
        con,
        files_by_district=by_district,
        index=index,
        parse=lambda cols, district: parse_blocks_2015(cols, index.list_names[district])[0],
        unmapped_table="sejm_candidate_columns_unmapped",
    )
    build_wide_votes(
        con, year=year, target="sejm_precinct_candidate_votes", key_name="candidate_id", files=files,
        teryt_aliases=("TERYT gminy",), number_aliases=("Numer obwodu",),
    )


def _build_2011_layout(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2011 files.

    ``2011-sejm-gl-lis-obw`` (precincts, protocol, list votes as ``'<list no> - <committee>'``),
    ``kandydaci_sejm`` (candidates with official votes), ``parl2011-sejm-kand-obw/pNN`` (candidate
    votes per district, see ``parse_list_blocks``), the precinct register ``NN0000-obwody`` (Senate
    district, precinct type), ``2011-sejm-pow-listy`` (powiat names), ``parl2011okręgi`` (seats,
    voters, boundaries) and ``2011-gl-lis-gm`` (official list votes per gmina, summed per district).
    """
    year = sources.year
    results = sources.load("sejm_results", LIST_RESULTS_2011)
    register = sources.load("precinct_register", "**/sejmsenat/*-obwody/*.parquet")
    candidates_src = sources.load("sejm_candidates", "**/sejmsenat/kandydaci_sejm/*.parquet")
    powiats = sources.load("powiats", "**/sejmsenat/*-sejm-pow-listy.parquet")
    gminas = sources.load("sejm_gmina_totals", "**/sejmsenat/*-gl-lis-gm.parquet")
    districts_src = sources.load("districts", "**/sejmsenat/parl*okręgi.parquet")
    area_cases = " ".join(f"WHEN {sql_literal(code)} THEN {sql_literal(name)}" for code, name in AREA_TYPES_2011.items())

    con.execute(
        f"""
        CREATE OR REPLACE TABLE precincts AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY clean_teryt(r."TERYT") NULLS LAST, r."Gmina", int_(r."Nr obw.")) AS INT) AS precinct_id,
               pkey(r."TERYT", r."Nr obw.") AS pkey,
               clean_teryt(r."TERYT") AS teryt_gmina,
               r."Gmina" AS gmina,
               pw.powiat,
               voivodeship(clean_teryt(r."TERYT")) AS wojewodztwo,
               CAST(int_(r."Okręg") AS INT) AS sejm_district_no,
               CAST(int_(o."Okręg do Senatu nr") AS INT) AS senat_district_no,
               CAST(int_(r."Nr obw.") AS INT) AS precinct_no,
               r."Adres" AS address,
               o."Typ obwodu" AS precinct_type,
               CASE r."Teren obw." {area_cases} END AS area_type,
               CAST(NULL AS INT) AS residents,
               o."Nr obwodu" IS NOT NULL AS in_register
        FROM {results} r
        LEFT JOIN {register} o ON pkey(o."Kod gm.", o."Nr obwodu") = pkey(r."TERYT", r."Nr obw.")
        LEFT JOIN (SELECT left(lpad("TERYT", 6, '0'), 4) AS teryt4, any_value("Powiat") AS powiat FROM {powiats} GROUP BY 1) pw
          ON pw.teryt4 = left(clean_teryt(r."TERYT"), 4)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, {_protocol_select(con, results)}
        FROM {results} r JOIN precincts p ON p.pkey = pkey(r."TERYT", r."Nr obw.")
        """
    )

    _committee_table(
        con, year,
        f'SELECT strip_signature("Nazwa komitetu") AS committee_name, min(int_("Nr listy")) AS min_list FROM {candidates_src} GROUP BY 1',
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr okręgu"), int_(k."Nr listy"), int_(k."Nr kandydata na liście")) AS INT)
                 AS candidate_id,
               CAST(int_(k."Nr okręgu") AS INT) AS district_no,
               c.committee_id,
               CAST(int_(k."Nr listy") AS INT) AS list_no,
               CAST(int_(k."Nr kandydata na liście") AS INT) AS list_position,
               trim(k."Imiona") || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name,
               trim(k."Imiona") AS first_names,
               gender_(k."Płeć") AS gender,
               k."Zawód" AS occupation,
               k."Miejsce zamieszkania" AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt,
               NULLIF(trim(k."Partia polityczna"), '') AS party_membership,
               CAST(NULL AS VARCHAR) AS party_support,
               int_(k."Liczba głosów") AS votes,
               {pct_expr(con, candidates_src, '"% głosów na listę"')} AS pct_of_list,
               {pct_expr(con, candidates_src, '"% głosów w okręgu"')} AS pct_of_district,
               yes_(k."Wybrany") AS elected
        FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k."Nazwa komitetu")
        """
    )

    # List numbers are national: '<list no> - <committee>' columns of the precinct and gmina files.
    list_committee = dict(con.execute("SELECT DISTINCT list_no, committee_id FROM sejm_candidates").fetchall())
    if len(list_committee) != con.execute("SELECT count(DISTINCT committee_id) FROM sejm_candidates").fetchone()[0]:
        raise BuildError(f"year {year}: a list number is shared by several committees")
    list_columns = {
        col: list_committee[int(m.group(1))]
        for col in columns_of(con, results)
        if (m := re.match(r"^(\d+) - ", col)) and int(m.group(1)) in list_committee
    }
    build_wide_votes(
        con, year=year, target="sejm_precinct_list_votes", key_name="committee_id",
        files=[(0, sources.files(LIST_RESULTS_2011)[0], list_columns)],
        teryt_aliases=("TERYT",), number_aliases=("Nr obw.",),
    )
    con.execute("CREATE OR REPLACE TABLE official_list_votes (district_no INT, committee_id INT, votes BIGINT)")
    for col, committee_id in list_columns.items():
        con.execute(
            f"""INSERT INTO official_list_votes
                SELECT int_("Okręg"), {committee_id}, sum(int_({quote_ident(col)})) FROM {gminas}
                GROUP BY 1 HAVING count(int_({quote_ident(col)})) > 0"""
        )
    _district_lists_from_candidates(con, year)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_districts AS
        SELECT {year} AS year,
               CAST(int_(d."Nr okręgu") AS INT) AS district_no,
               (SELECT any_value(k."Siedziba OKW") FROM {candidates_src} k WHERE int_(k."Nr okręgu") = int_(d."Nr okręgu")) AS seat_city,
               w.wojewodztwo,
               CAST(int_(d."Liczba mandatów") AS INT) AS seats,
               CAST(int_(d."Liczba wyborców") AS INT) AS eligible_voters,
               regexp_replace(d."Granice okręgu wyborczego", '\\s+', ' ', 'g') AS description
        FROM {districts_src} d
        LEFT JOIN {_districts_wojewodztwo(con)} w ON w.sejm_district_no = int_(d."Nr okręgu")
        WHERE d."Izba" = 'Sejm'
        """
    )

    by_district = district_files(sources.files(CANDIDATE_RESULTS_2011), r"^p(\d+)__")
    rows_without_number(con, by_district, "sejm_rows_without_number")
    files = map_candidate_columns(
        con,
        files_by_district=by_district,
        index=candidate_index(con, "sejm_candidates", "committee_id"),
        parse=lambda cols, _district: parse_list_blocks(cols),
        unmapped_table="sejm_candidate_columns_unmapped",
    )
    # Namesakes in one district are told apart in the register by a patronymic ('Andrzej Stefan syn
    # Józefa' on list 3 and 'Andrzej Stefan syn Stefana' on list 5 in Tarnów); result headers omit it.
    plain_names = {
        (district, list_no, python_name_key(re.sub(PATRONYMIC, "", name))): candidate_id
        for candidate_id, district, list_no, name in con.execute(
            "SELECT candidate_id, district_no, list_no, full_name FROM sejm_candidates"
        ).fetchall()
        if re.search(PATRONYMIC, name)
    }
    for district, path, mapping in files:
        for header, ref in parse_list_blocks(columns_of(con, f"read_parquet({sql_literal(path)})")).items():
            candidate_id = plain_names.get((district, ref.list_no, ref.name_key))
            if header not in mapping and candidate_id is not None:
                mapping[header] = candidate_id
                con.execute("DELETE FROM sejm_candidate_columns_unmapped WHERE district_no = ? AND header = ?", [district, header])
    build_wide_votes(
        con, year=year, target="sejm_precinct_candidate_votes", key_name="candidate_id", files=files,
        teryt_aliases=("Kod terytorialny gminy",), number_aliases=("Nr obwodu głosowania",),
    )


def _build_2005_layout(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2001, 2005 and 2007 KBW workbooks.

    ``sejm2001-lis-obw`` / ``sejm2005-lis-obw`` / ``sejm2007-obw-listy`` (precincts, protocol, list votes
    as ``'<list no> - <name>'``), ``kandsejm<year>`` (candidates with official votes), ``okręgi2001sejm``
    / ``okregi2005sejm`` / ``okręgi2007sejm`` (seat city, seats, boundaries; 2001 continues a boundary
    description on the rows below). Official list votes per district come from the register's
    ``'Gł. na listę'`` (2001, 2005) or the gmina table ``sejm2007-gm-listy`` (2007). Only 2007 has
    per-precinct candidate votes: ``sejm2007-kand-obw/kandsejmobw-2007-DD-LL`` (district DD, list LL).
    List numbers are national, except that in 2005 number 19 belongs to a different committee in each
    district that has one, so list columns are mapped per (district, list number). 2001 uses the
    pre-2002 Warsaw gminas (``WARSAW_GMINAS_2001``); the precinct key keeps their codes, which the
    2001 Senate files use too.
    """
    year = sources.year
    layout_2001 = sources.has(LIST_RESULTS_2001)
    results = sources.load(
        "sejm_results", next(p for p in (LIST_RESULTS_2005, LIST_RESULTS_2001, LIST_RESULTS_2007) if sources.has(p))
    )
    candidates_src = sources.load("sejm_candidates", "**/sejm/kandsejm*.parquet")
    districts_src = sources.load("districts", "**/sejm/okr*sejm.parquet")
    create_warsaw_districts(con)
    create_warsaw_gminas_2001(con)
    r_district = column(con, results, "Nr okręgu", "Okręg", "Nr okr.")
    r_teryt = column(con, results, "TERYT", "Kod gm.", "Kod gminy")
    r_number = column(con, results, "Numer obwodu", "Nr obw.")
    r_area = column(con, results, "7-ma cyfra TERYT", "Teren")
    r_type = column(con, results, "Typ obwodu", "Typ")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE src_sejm_precincts AS
        SELECT r.*, lpad(trim(r.{r_teryt}), 6, '0') AS teryt_raw,
               coalesce(w.teryt, w1.teryt, lpad(trim(r.{r_teryt}), 6, '0')) AS teryt_fixed,
               -- Precinct key: 2001 Warsaw precincts keep their pre-2002 code (numbers are unique per old gmina).
               coalesce(w.teryt, lpad(trim(r.{r_teryt}), 6, '0')) AS teryt_key,
               coalesce(w.gmina, w1.gmina, r."Gmina") AS gmina_name, w.letter IS NOT NULL AS warsaw_letter,
               w1.letter IS NOT NULL AS centrum_letter_2001
        FROM {results} r
        LEFT JOIN warsaw_districts_2005 w ON lpad(trim(r.{r_teryt}), 6, '0') = '146501' AND w.letter = trim(r.{r_area})
        LEFT JOIN warsaw_gminas_2001 w1 ON {year} < 2002 AND w1.old_teryt = lpad(trim(r.{r_teryt}), 6, '0')
          AND (w1.letter IS NULL OR w1.letter = trim(r.{r_area}))
        """
    )
    # Senate district of each precinct: per gmina in 2005, from the Senate per-precinct files in 2001/2007.
    con.execute("CREATE OR REPLACE TABLE senate_district_of (key VARCHAR, senat_district_no INT)")
    senate_key = "NULL"
    if sources.has(SENATE_GMINAS_2005):
        gminas = sources.load("senate_gminas", SENATE_GMINAS_2005)
        con.execute(f"""INSERT INTO senate_district_of SELECT lpad(trim("TERYT"), 6, '0'), int_("Nr okr. - Senat") FROM {gminas}""")
        senate_key = "r.teryt_raw"
    elif sources.has(SENATE_RESULTS_2007) or sources.has(SENATE_RESULTS_2001):
        pattern, regex = (SENATE_RESULTS_2001, r"^SENOBW(\d+)__") if layout_2001 else (SENATE_RESULTS_2007, r"obw-(\d+)__")
        for district, path in district_files(sources.files(pattern), regex).items():
            source = f"read_parquet({sql_literal(path)}, hive_partitioning = false)"
            con.execute(
                f"INSERT INTO senate_district_of SELECT DISTINCT pkey({column(con, source, 'TERYT', 'Kod gminy')}, "
                f"{column(con, source, 'Nr obwodu', 'Nr obw.')}), {district} FROM {source}"
            )
        senate_key = f"pkey(r.teryt_key, r.{r_number})"
    area_cases = " ".join(f"WHEN {sql_literal(code)} THEN {sql_literal(name)}" for code, name in AREA_TYPES_2011.items())
    precinct_types = PRECINCT_TYPES_2001 if layout_2001 else PRECINCT_TYPES_2005
    type_cases = " ".join(f"WHEN {sql_literal(code)} THEN {sql_literal(name)}" for code, name in precinct_types.items())
    con.execute(
        f"""
        CREATE OR REPLACE TABLE precincts AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY clean_teryt(r.teryt_fixed) NULLS LAST, r.gmina_name, int_(r.{r_number})) AS INT) AS precinct_id,
               pkey(r.teryt_key, r.{r_number}) AS pkey,
               clean_teryt(r.teryt_fixed) AS teryt_gmina,
               r.gmina_name AS gmina,
               CASE WHEN clean_teryt(r.teryt_fixed) IS NOT NULL THEN r."Powiat" END AS powiat,
               voivodeship(clean_teryt(r.teryt_fixed)) AS wojewodztwo,
               CAST(int_(r.{r_district}) AS INT) AS sejm_district_no,
               s.senat_district_no,
               CAST(int_(r.{r_number}) AS INT) AS precinct_no,
               r.{column(con, results, "Adres obwodu", "Adres")} AS address,
               CASE trim(r.{r_type}) {type_cases} END AS precinct_type,
               -- 2001: Warszawa-Centrum, whose districts the letter names, was an urban gmina.
               CASE WHEN r.warsaw_letter THEN {sql_literal(AREA_TYPES_2011["8"])}
                    WHEN r.centrum_letter_2001 THEN {sql_literal(AREA_TYPES_2011["1"])}
                    ELSE CASE trim(r.{r_area}) {area_cases} END END AS area_type,
               CAST(NULL AS INT) AS residents,
               true AS in_register
        FROM src_sejm_precincts r
        LEFT JOIN senate_district_of s ON s.key = {senate_key}
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, {_protocol_select(con, "src_sejm_precincts")}
        FROM src_sejm_precincts r JOIN precincts p ON p.pkey = pkey(r.teryt_key, r.{r_number})
        """
    )

    k_district = column(con, candidates_src, "Nr okr.", "Okręg")
    k_list = column(con, candidates_src, "Nr listy", "Lista")
    k_position = column(con, candidates_src, "Pozycja", "Nr kand.")
    # 2001 publishes the first and the second given name in two columns.
    if column(con, candidates_src, "Imiona", required=False):
        first_names = 'trim(k."Imiona")'
    else:
        first_names = """concat_ws(' ', NULLIF(trim(k."1. imię"), ''), NULLIF(trim(k."2. imię"), ''))"""
    k_party = column(con, candidates_src, "Partia", required=False)
    k_support = column(con, candidates_src, "Poparcie", required=False)
    # 2001 and 2005 publish the list and district totals next to each candidate, 2007 the percentages.
    k_list_total = column(con, candidates_src, "Gł. na listę", required=False)
    if k_list_total:
        pct_of_list = f"""CAST(round(100.0 * int_(k."Głosy") / NULLIF(int_(k.{k_list_total}), 0), 2) AS DECIMAL(6, 2))"""
        district_total = column(con, candidates_src, "Gł. w okręgu")
        pct_of_district = f"""CAST(round(100.0 * int_(k."Głosy") / NULLIF(int_(k.{district_total}), 0), 2) AS DECIMAL(6, 2))"""
    else:
        pct_of_list = pct_expr(con, candidates_src, '"% z listy"')
        pct_of_district = pct_expr(con, candidates_src, '"% w okręgu"')
    _committee_table(
        con, year,
        f'SELECT strip_signature("Komitet wyborczy") AS committee_name, min(int_({k_list})) AS min_list FROM {candidates_src} GROUP BY 1',
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{k_district}), int_(k.{k_list}), int_(k.{k_position})) AS INT) AS candidate_id,
               CAST(int_(k.{k_district}) AS INT) AS district_no,
               c.committee_id,
               CAST(int_(k.{k_list}) AS INT) AS list_no,
               CAST(int_(k.{k_position}) AS INT) AS list_position,
               {first_names} || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name,
               {first_names} AS first_names,
               gender_(k."Płeć") AS gender,
               k."Zawód" AS occupation,
               k."Miejsce zamieszkania" AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt,
               {f"NULLIF(trim(k.{k_party}), '')" if k_party else "CAST(NULL AS VARCHAR)"} AS party_membership,
               {f"NULLIF(trim(k.{k_support}), '')" if k_support else "CAST(NULL AS VARCHAR)"} AS party_support,
               int_(k."Głosy") AS votes,
               {pct_of_list} AS pct_of_list,
               {pct_of_district} AS pct_of_district,
               yes_(k."Mandat") AS elected
        FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k."Komitet wyborczy")
        """
    )

    con.execute("CREATE OR REPLACE TABLE official_list_votes (district_no INT, committee_id INT, votes BIGINT)")
    if k_list_total:
        con.execute(
            f"""
            INSERT INTO official_list_votes
            SELECT int_(k.{k_district}), c.committee_id, any_value(int_(k.{k_list_total}))
            FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k."Komitet wyborczy")
            GROUP BY ALL
            """
        )
        list_totals_differ = f"""
            SELECT count(*) FROM (SELECT 1 FROM {candidates_src} GROUP BY int_({k_district}), int_({k_list})
                                  HAVING count(DISTINCT int_({k_list_total})) <> 1)"""
    else:
        gminas = sources.load("sejm_gmina_totals", "**/sejm/sejm*-gm-listy.parquet")
        list_committee = dict(con.execute("SELECT DISTINCT list_no, committee_id FROM sejm_candidates").fetchall())
        if len(list_committee) != con.execute("SELECT count(DISTINCT committee_id) FROM sejm_candidates").fetchone()[0]:
            raise BuildError(f"year {year}: a list number is shared by several committees")
        for col in columns_of(con, gminas):
            if (m := re.match(r"^(\d+) - ", col)) and int(m[1]) in list_committee:
                con.execute(
                    f"""INSERT INTO official_list_votes
                        SELECT int_("Okręg"), {list_committee[int(m[1])]}, sum(int_({quote_ident(col)})) FROM {gminas}
                        GROUP BY 1 HAVING count(int_({quote_ident(col)})) > 0"""
                )
        list_totals_differ = "SELECT 0"
    _district_lists_from_candidates(con, year)
    d_number = column(con, districts_src, "Numer okręgu", "Nr okręgu")
    # 2005/2007: 'Liczba' spans four columns (powiats, gminas, precincts, seats); seats is the unnamed last one.
    d_seats = column(con, districts_src, "Liczba mandatów", "column7")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_districts AS
        WITH rows AS (
          -- A row without a district number continues the description of the district above (2001).
          SELECT *, count(int_({d_number})) OVER (ORDER BY filename, file_row_number) AS district_row
          FROM read_parquet({sql_list(sources.files("**/sejm/okr*sejm.parquet"))}, filename = true, file_row_number = true)
        )
        SELECT {year} AS year,
               CAST(int_(d.{d_number}) AS INT) AS district_no,
               d."Siedziba Okręgowej Komisji Wyborczej" AS seat_city,
               w.wojewodztwo,
               CAST(int_(d.{d_seats}) AS INT) AS seats,
               CAST(NULL AS INT) AS eligible_voters,
               (SELECT regexp_replace(string_agg(c."Obszar okręgu wyborczego", ' ' ORDER BY c.file_row_number), '\\s+', ' ', 'g')
                FROM rows c WHERE c.district_row = d.district_row) AS description
        FROM rows d
        LEFT JOIN {_districts_wojewodztwo(con)} w ON w.sejm_district_no = int_(d.{d_number})
        WHERE int_(d.{d_number}) IS NOT NULL
        """
    )

    # List votes: '<list no> - <name>' columns, mapped by the precinct's district and the list number.
    list_columns = {col: int(m[1]) for col in columns_of(con, results) if (m := re.match(r"^(\d+) - ", col))}
    con.execute("CREATE OR REPLACE TABLE sejm_list_columns (header VARCHAR, list_no INT)")
    con.executemany("INSERT INTO sejm_list_columns VALUES (?, ?)", list(list_columns.items()))
    cols = ", ".join(quote_ident(c) for c in list_columns)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_list_votes_raw AS
        SELECT district_no, pkey, header, votes
        FROM (UNPIVOT (SELECT CAST(int_({r_district}) AS INT) AS district_no, pkey(teryt_key, {r_number}) AS pkey, {cols}
                       FROM src_sejm_precincts)
              ON {cols} INTO NAME header VALUE votes)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_list_votes AS
        SELECT {year} AS year, p.precinct_id, l.committee_id, CAST(sum(coalesce(int_(r.votes), 0)) AS INT) AS votes
        FROM sejm_precinct_list_votes_raw r
        JOIN sejm_list_columns m USING (header)
        JOIN sejm_district_lists l ON l.district_no = r.district_no AND l.list_no = m.list_no
        JOIN precincts p USING (pkey)
        GROUP BY ALL
        """
    )
    run_checks(
        con,
        year,
        {
            "candidates of one district list with different list totals": list_totals_differ,
            "Warsaw precincts (146501) without a district letter": "SELECT count(*) FROM precincts WHERE teryt_gmina = '146501'",
            "Warsaw precincts with a pre-2002 code (1431xx) not mapped to a district": """
                SELECT count(*) FROM precincts WHERE teryt_gmina LIKE '1431%'""",
            "list votes in a column whose list number did not run in the precinct's district": """
                SELECT count(*) FROM sejm_precinct_list_votes_raw r JOIN sejm_list_columns m USING (header)
                WHERE coalesce(int_(r.votes), 0) <> 0 AND NOT EXISTS (
                    SELECT 1 FROM sejm_district_lists l WHERE l.district_no = r.district_no AND l.list_no = m.list_no)""",
            "precincts with votes in two columns of one list number": """
                SELECT count(*) FROM (SELECT r.pkey, m.list_no FROM sejm_precinct_list_votes_raw r JOIN sejm_list_columns m USING (header)
                                      WHERE r.votes IS NOT NULL GROUP BY ALL HAVING count(*) > 1)""",
            # Votes for a candidate struck off after the ballots were printed count for the list only; the
            # register then skips the candidate's position (2005: 16 lists, 2007: 5).
            "district lists with more votes than their candidates but no skipped list position": """
                SELECT count(*) FROM sejm_district_lists d
                JOIN (SELECT district_no, committee_id, sum(votes) AS votes, count(*) AS n, max(list_position) AS last
                      FROM sejm_candidates GROUP BY ALL) k USING (district_no, committee_id)
                WHERE d.votes > k.votes AND k.n = k.last""",
        },
    )

    index = candidate_index(con, "sejm_candidates", "committee_id")
    con.execute("CREATE OR REPLACE TABLE sejm_candidate_columns_unmapped (district_no INT, header VARCHAR, list_no INT, position INT)")
    files = []
    for path in sources.files(CANDIDATE_RESULTS_2007):
        district, list_no = map(int, re.search(r"-(\d+)-(\d+)__[0-9a-f]+\.parquet$", path).groups())
        mapping = {}
        for header, ref in parse_numbered_names(columns_of(con, f"read_parquet({sql_literal(path)})"), list_no).items():
            candidate_id = index.resolve(district, ref)
            if candidate_id is None:
                con.execute("INSERT INTO sejm_candidate_columns_unmapped VALUES (?, ?, ?, ?)", [district, header, list_no, ref.position])
            else:
                mapping[header] = candidate_id
        # One file per district and list: the file key keeps headers of different lists apart.
        files.append((district * 100 + list_no, path, mapping))
    build_wide_votes(
        con, year=year, target="sejm_precinct_candidate_votes", key_name="candidate_id", files=files,
        teryt_aliases=("TERYT",), number_aliases=("Nr obwodu",),
    )


def _build_1990s_layout(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """1991, 1993 and 1997 KBW workbooks (SOURCES_1990S).

    Precincts use the pre-1999 geography (as president 1990/1995 and the 1996/1997 referendums):
    teryt_gmina and powiat stay NULL, the 5-digit gmina code goes to teryt_gmina_pre1999 and the
    1975-1998 voivodeship to wojewodztwo (NULL abroad and on ships). The Senate district of a precinct is
    its voivodeship ('Nr woj.'). 1993 publishes per precinct only eligible voters, votes cast and valid
    votes: ballots_issued is NULL and invalid votes are cast minus valid.

    Candidates carry the seat type ('Mandat': O district list, K national list) and their position on
    their committee's national list ('Nr na liście oglnp.'); every national-list candidate also ran on a
    district list. 1997 positions are checked against the registered national lists (OGPOL files,
    NATIONAL_LIST_EXCEPTIONS_1997). The registers publish each candidate's age ('Wiek'). The 69
    national-list seats are district 0: one sejm_district_lists row per committee with a national list,
    votes 0 (the national list had no votes of its own; seats went by the committee's district votes
    nationwide), seats from the K mandates. Thresholds follow the electoral
    laws: none in 1991 districts; 1993/1997 districts 5% nationally (coalitions 8%, exempt minority lists,
    COALITION_LISTS_1990S, MINORITY_LISTS_1990S); national lists 7% (1993/1997) or, in 1991, 5% or seats in
    at least 5 districts or a minority list, with blocs (NATIONAL_BLOCS_1991) counted together. 1991
    district blocs ('Blok') are list_group; list numbers 66-70 (1991), 25-26 (1993) and 15 (1997) belong to
    a different local committee in each district, so lists are matched per (district, list number).
    """
    year = sources.year
    spec = SOURCES_1990S[year]
    results = sources.load("sejm_results", spec["results"])
    candidates_src = sources.load("sejm_candidates", spec["candidates"])
    districts_src = sources.load("districts", spec["districts"])
    create_voivodeships_1975(con)

    # Precinct rows with unified column names; list-vote columns keep their headers.
    if year == 1991:
        code, number = '"Kod gminy (cyfry 1-5)"', '"Numer obwodu"'
        ptype = f"CASE WHEN trim({code}) = {sql_literal(ABROAD_1991[0])} AND int_({number}) >= {ABROAD_1991[1]} THEN 'Z' END"
        area, issued, invalid = "CAST(NULL AS VARCHAR)", 'int_("Karty wydane")', 'int_("Głosy nieważne")'
        list_header = r"^(\d+)$"
    elif year == 1993:
        code, number, ptype, area = '"Kod gm."', '"Nr obw."', 'trim("Typ")', 'trim("Teren")'
        issued, invalid = "NULL", 'int_("Odd.") - int_("Ważne")'
        list_header = r"^Lista nr (\d+)$"
    else:
        code, number, ptype, area = '"TERYT gminy"', '"Nr obw."', 'trim("Typ obw.")', "CAST(NULL AS VARCHAR)"
        issued, invalid = 'int_("Karty wydane")', 'int_("Głosy nieważne")'
        list_header = r"^(\d+) - "
    con.execute(
        f"""
        CREATE OR REPLACE TABLE src_sejm_precincts AS
        SELECT r.*, CAST(int_({column(con, results, "Numer okręgu", "Nr okr.")}) AS INT) AS district_no_,
               CAST(int_({column(con, results, "Numer województwa", "Nr woj.")}) AS INT) AS voivodeship_no_,
               trim({code}) AS code_, CAST(int_({number}) AS INT) AS precinct_no_, pkey({code}, {number}) AS pkey,
               {column(con, results, "Nazwa gminy", "Nazwa gm.", "Gmina")} AS gmina_,
               {column(con, results, "Adres komisji obwodowej", "Adres")} AS address_,
               {ptype} AS ptype_, {area} AS area_,
               int_({column(con, results, "Liczba uprawnionych", "Upr.", "Uprawnieni do głosowania")}) AS eligible_voters,
               {issued} AS ballots_issued,
               int_({column(con, results, "Głosy oddane", "Odd.", "Głosy oddane (frekwencja)")}) AS valid_ballots,
               {invalid} AS invalid_votes,
               int_({column(con, results, "Głosy ważne", "Ważne")}) AS valid_votes
        FROM {results} r
        """
    )
    type_cases = " ".join(f"WHEN {sql_literal(c)} THEN {sql_literal(n)}" for c, n in PRECINCT_TYPES_2001.items())
    con.execute(
        f"""
        CREATE OR REPLACE TABLE precincts AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY r.code_, r.precinct_no_) AS INT) AS precinct_id,
               r.pkey,
               CAST(NULL AS VARCHAR) AS teryt_gmina,
               CASE r.ptype_ WHEN 'Z' THEN 'zagranica' WHEN 'S' THEN 'statki' ELSE r.gmina_ END AS gmina,
               CAST(NULL AS VARCHAR) AS powiat,
               CASE WHEN coalesce(r.ptype_, '') NOT IN ('Z', 'S') THEN v.name END AS wojewodztwo,
               r.district_no_ AS sejm_district_no,
               r.voivodeship_no_ AS senat_district_no,
               r.precinct_no_ AS precinct_no,
               r.address_ AS address,
               CASE r.ptype_ {type_cases} END AS precinct_type,
               CASE r.area_ WHEN 'M' THEN 'miasto' WHEN 'W' THEN 'wieś' END AS area_type,
               CAST(NULL AS INT) AS residents,
               true AS in_register,
               CASE WHEN coalesce(r.ptype_, '') NOT IN ('Z', 'S') THEN r.code_ END AS teryt_gmina_pre1999
        FROM src_sejm_precincts r LEFT JOIN voivodeships_1975 v ON v.code = left(r.code_, 2)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, CAST(r.eligible_voters AS INT) AS eligible_voters,
               CAST(r.ballots_issued AS INT) AS ballots_issued, CAST(r.valid_ballots AS INT) AS valid_ballots,
               CAST(r.invalid_votes AS INT) AS invalid_votes, CAST(r.valid_votes AS INT) AS valid_votes
        FROM src_sejm_precincts r JOIN precincts p USING (pkey)
        """
    )

    k_district = column(con, candidates_src, "Nr okr.", "Okręg")
    k_list = column(con, candidates_src, "Nr listy")
    k_committee = column(con, candidates_src, "Komitet wyborczy")
    k_position = column(con, candidates_src, "Nr kand.", "Nr na liście", "Lp")
    k_mandate = column(con, candidates_src, "Mandat")
    # 1991 'Umiona' (sic); 1997 publishes the first and the second given name in two columns.
    if given := column(con, candidates_src, "Imiona", "Umiona", required=False):
        first_names = f"trim(k.{given})"
    else:
        first_names = """concat_ws(' ', NULLIF(trim(k."1. imię"), ''), NULLIF(trim(k."2. Imię"), ''))"""
    k_votes = column(con, candidates_src, "L. głosów", "L. gł.", "Głosy")
    k_list_total = column(con, candidates_src, "Gł. na listę", "Głosy na listę")
    k_district_total = column(con, candidates_src, "Gł. ważne w okręgu", "Gł. w okr.", "Głosy w okręgu")
    k_support = column(
        con, candidates_src, "Nazwa partii/organizacji popierającej kandydata", "Nazwa partii.organizacji popierającej kandydata",
        "Poparcie",
    )
    _committee_table(
        con, year, f"SELECT strip_signature({k_committee}) AS committee_name, min(int_({k_list})) AS min_list FROM {candidates_src} GROUP BY 1"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{k_district}), int_(k.{k_list}), int_(k.{k_position})) AS INT) AS candidate_id,
               CAST(int_(k.{k_district}) AS INT) AS district_no,
               c.committee_id,
               CAST(int_(k.{k_list}) AS INT) AS list_no,
               CAST(int_(k.{k_position}) AS INT) AS list_position,
               {first_names} || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name,
               {first_names} AS first_names,
               gender_(k."Płeć") AS gender,
               NULLIF(trim(k."Zawód"), '') AS occupation,
               NULLIF(trim(k.{column(con, candidates_src, "Miejsce zamieszkania", "Miejscowość")}), '') AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt,
               CAST(NULL AS VARCHAR) AS party_membership,
               NULLIF(trim(k.{k_support}), '') AS party_support,
               int_(k.{k_votes}) AS votes,
               CAST(round(100.0 * int_(k.{k_votes}) / NULLIF(int_(k.{k_list_total}), 0), 2) AS DECIMAL(6, 2)) AS pct_of_list,
               CAST(round(100.0 * int_(k.{k_votes}) / NULLIF(int_(k.{k_district_total}), 0), 2) AS DECIMAL(6, 2)) AS pct_of_district,
               coalesce(trim(k.{k_mandate}), '') IN ('O', 'K') AS elected,
               CAST(int_(k."Nr na liście oglnp.") AS INT) AS national_list_position,
               coalesce(trim(k.{k_mandate}), '') = 'K' AS national_list_seat,
               CAST(int_(k."Wiek") AS INT) AS age
        FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k.{k_committee})
        """
    )
    # Coalitions and exempt minority lists from the announcements (a committee with a local list number
    # ran in one district).
    coalitions = ", ".join(str(n) for n in COALITION_LISTS_1990S.get(year, ())) or "NULL"
    minority = " OR ".join(
        f"(k.list_no = {n}" + (f" AND k.district_no = {d})" if d else ")") for n, d in sorted(MINORITY_LISTS_1990S[year], key=str)
    )
    con.execute(
        f"""
        UPDATE sejm_committees c SET
          is_coalition = c.committee_id IN (SELECT committee_id FROM sejm_candidates WHERE list_no IN ({coalitions})),
          is_minority = c.committee_id IN (SELECT committee_id FROM sejm_candidates k WHERE {minority})
        """
    )

    # Official list totals per district: the candidates' 'Gł. na listę'; 1991 also the list table.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE official_list_votes AS
        SELECT CAST(int_(k.{k_district}) AS INT) AS district_no, c.committee_id, any_value(int_(k.{k_list_total})) AS votes,
               count(DISTINCT int_(k.{k_list_total})) AS totals
        FROM {candidates_src} k JOIN sejm_committees c ON c.committee_name = strip_signature(k.{k_committee})
        GROUP BY ALL
        """
    )
    total = "(SELECT sum(votes) FROM official_list_votes)"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE committee_national_votes AS
        SELECT committee_id, sum(votes) AS votes, 100.0 * sum(votes) / {total} AS pct
        FROM official_list_votes GROUP BY 1
        """
    )
    if year == 1991:
        lists_src = sources.load("sejm_lists", spec["lists"])
        list_group = "CAST(int_(l.\"Blok\") AS INT)"
        lists_join = f"""LEFT JOIN {lists_src} l ON int_(l."Nr okr.") = c.district_no AND int_(l."Nr listy") = c.list_no"""
        in_allocation = "true"  # 1991: no threshold in districts
    else:
        list_group, lists_join = "CAST(NULL AS INT)", ""
        in_allocation = "m.is_minority OR n.pct >= CASE WHEN m.is_coalition THEN 8 ELSE 5 END"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_district_lists AS
        WITH cand AS (
          SELECT district_no, committee_id, any_value(list_no) AS list_no, count(*) AS candidates_count,
                 count(*) FILTER (WHERE elected AND NOT national_list_seat) AS seats
          FROM sejm_candidates GROUP BY ALL
        )
        SELECT {year} AS year, c.district_no, c.committee_id, c.list_no, CAST(c.candidates_count AS INT) AS candidates_count,
               CAST(o.votes AS BIGINT) AS votes,
               CAST(round(100.0 * o.votes / sum(o.votes) OVER (PARTITION BY c.district_no), 2) AS DECIMAL(6, 2)) AS pct_of_district,
               {in_allocation} AS in_seat_allocation,
               CAST(c.seats AS INT) AS seats,
               {list_group} AS list_group
        FROM cand c
        JOIN official_list_votes o USING (district_no, committee_id)
        JOIN sejm_committees m USING (committee_id)
        JOIN committee_national_votes n USING (committee_id)
        {lists_join}
        """
    )
    # National lists (district 0): committees with national-list candidates; 1991 blocs as list_group.
    bloc_of = {n: bloc[0] for bloc in (NATIONAL_BLOCS_1991 if year == 1991 else ()) for n in bloc}
    bloc_cases = " ".join(f"WHEN {n} THEN {b}" for n, b in bloc_of.items())
    con.execute(
        f"""
        CREATE OR REPLACE TABLE national_lists AS
        SELECT k.committee_id, any_value(k.list_no) AS list_no, count(*) FILTER (WHERE k.national_list_position IS NOT NULL) AS candidates_count,
               count(*) FILTER (WHERE k.national_list_seat) AS seats,
               {f"CASE any_value(k.list_no) {bloc_cases} END" if bloc_cases else "CAST(NULL AS INT)"} AS list_group
        FROM sejm_candidates k GROUP BY 1 HAVING count(k.national_list_position) > 0
        """
    )
    if year == 1991:
        # 5% nationally, or district seats in at least 5 (linked) districts, or a minority list (art. 100 ust. 2
        # and 4 of the 1991 law); a bloc qualifies together.
        unlinked = " ".join(f"WHEN {n} THEN {d}" for n, d in UNLINKED_SEAT_DISTRICTS_1991.items())
        con.execute(
            f"""
            CREATE OR REPLACE TABLE national_list_seat_districts AS
            SELECT k.committee_id, count(DISTINCT k.district_no) - coalesce(CASE any_value(k.list_no) {unlinked} END, 0) AS districts
            FROM sejm_candidates k WHERE k.elected AND NOT k.national_list_seat GROUP BY 1
            """
        )
        con.execute(
            """
            CREATE OR REPLACE TABLE national_list_qualified AS
            SELECT l.committee_id,
                   bool_or(m.is_minority) OVER w OR sum(n.pct) OVER w >= 5 OR sum(coalesce(d.districts, 0)) OVER w >= 5 AS qualified
            FROM national_lists l JOIN sejm_committees m USING (committee_id) JOIN committee_national_votes n USING (committee_id)
            LEFT JOIN national_list_seat_districts d USING (committee_id)
            WINDOW w AS (PARTITION BY coalesce(l.list_group, -l.committee_id))
            """
        )
    else:  # 7% of the valid votes nationally (art. 4 of the 1993 law)
        con.execute(
            """CREATE OR REPLACE TABLE national_list_qualified AS
               SELECT committee_id, n.pct >= 7 AS qualified FROM national_lists JOIN committee_national_votes n USING (committee_id)"""
        )
    con.execute(
        f"""
        INSERT INTO sejm_district_lists
        SELECT {year}, 0, l.committee_id, l.list_no, CAST(l.candidates_count AS INT), 0, NULL, q.qualified, CAST(l.seats AS INT), l.list_group
        FROM national_lists l JOIN national_list_qualified q USING (committee_id)
        """
    )

    d_number = column(con, districts_src, "Nr", "Nr okr", "column0")
    d_seats = column(con, districts_src, "L. mand.", "Liczba")
    d_eligible = column(con, districts_src, "L. upr.", "Uprawnieni")
    d_description = column(con, districts_src, "Granice (województwo)", "Obszar", "column2")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_district_totals AS
        SELECT CAST(int_(d.{d_number}) AS INT) AS district_no,
               int_(d.{column(con, districts_src, "L. obw.", "column15")}) AS precincts,
               int_(d.{d_eligible}) AS eligible_voters,
               int_(d.{column(con, districts_src, "Gł. odd.", "Głosy oddane")}) AS valid_ballots,
               int_(d.{column(con, districts_src, "Gł. niew.", "Gł niew.", "Głosy nieważne")}) AS invalid_votes,
               int_(d.{column(con, districts_src, "Gł. ważne", "Głosy ważne")}) AS valid_votes
        FROM {districts_src} d WHERE int_(d.{d_number}) IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_districts AS
        SELECT {year} AS year, CAST(int_(d.{d_number}) AS INT) AS district_no,
               trim(d.{column(con, districts_src, "Siedziba OKW", "column1")}) AS seat_city, w.wojewodztwo,
               CAST(int_(d.{d_seats}) AS INT) AS seats, CAST(int_(d.{d_eligible}) AS INT) AS eligible_voters,
               regexp_replace(trim(d.{d_description}), '\\s+', ' ', 'g') AS description
        FROM {districts_src} d
        LEFT JOIN {_districts_wojewodztwo(con)} w ON w.sejm_district_no = int_(d.{d_number})
        WHERE int_(d.{d_number}) IS NOT NULL
        UNION ALL
        SELECT {year}, 0, NULL, NULL, {NATIONAL_LIST_SEATS}, NULL, NULL
        """
    )

    # List votes: list-number columns, mapped by the precinct's district and the list number.
    list_columns = {col: int(m[1]) for col in columns_of(con, results) if (m := re.match(list_header, col))}
    con.execute("CREATE OR REPLACE TABLE sejm_list_columns (header VARCHAR, list_no INT)")
    con.executemany("INSERT INTO sejm_list_columns VALUES (?, ?)", list(list_columns.items()))
    cols = ", ".join(quote_ident(c) for c in list_columns)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_list_votes_raw AS
        SELECT district_no, pkey, header, votes
        FROM (UNPIVOT (SELECT district_no_ AS district_no, pkey, {cols} FROM src_sejm_precincts)
              ON {cols} INTO NAME header VALUE votes)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_precinct_list_votes AS
        SELECT {year} AS year, p.precinct_id, l.committee_id, CAST(sum(coalesce(int_(r.votes), 0)) AS INT) AS votes
        FROM sejm_precinct_list_votes_raw r
        JOIN sejm_list_columns m USING (header)
        JOIN sejm_district_lists l ON l.district_no = r.district_no AND l.list_no = m.list_no
        JOIN precincts p USING (pkey)
        GROUP BY ALL
        """
    )
    # No per-precinct candidate results: empty candidate-vote tables for the shared checks.
    con.execute("CREATE OR REPLACE TABLE sejm_candidate_columns_unmapped (district_no INT, header VARCHAR, list_no INT, position INT)")
    build_wide_votes(
        con, year=year, target="sejm_precinct_candidate_votes", key_name="candidate_id", files=[],
        teryt_aliases=(), number_aliases=(),
    )

    official = OFFICIAL_1990S[year]
    without_results = official.get("precincts_without_results", 0)
    eligible_missing = official.get("eligible_voters_missing", {})
    checks = {
        f"precincts other than in PKW's announcement ({official['precincts']}, of them {without_results} without results)": f"""
            SELECT abs(count(*) + {without_results} - {official['precincts']}) FROM precincts""",
        f"eligible voters differ from PKW's announcement (except {sum(eligible_missing.values())} missing in districts {sorted(eligible_missing)})": f"""
            SELECT abs(sum(eligible_voters) + {sum(eligible_missing.values())} - {official['eligible_voters']}) FROM sejm_precinct_protocols""",
        "votes cast differ from PKW's announcement": f"SELECT abs(sum(valid_ballots) - {official['votes_cast']}) FROM sejm_precinct_protocols",
        "invalid votes differ from PKW's announcement": f"SELECT abs(sum(invalid_votes) - {official['invalid_votes']}) FROM sejm_precinct_protocols",
        "valid votes differ from PKW's announcement": f"SELECT abs(sum(valid_votes) - {official['valid_votes']}) FROM sejm_precinct_protocols",
        "precincts with votes cast != valid + invalid": """
            SELECT count(*) FROM sejm_precinct_protocols WHERE valid_ballots <> valid_votes + invalid_votes""",
        "districts whose precinct protocols differ from the district results": """
            SELECT count(*) FROM sejm_district_totals d
            LEFT JOIN (SELECT p.sejm_district_no AS district_no, count(*) AS precincts, sum(r.eligible_voters) AS eligible_voters,
                              sum(r.valid_ballots) AS valid_ballots, sum(r.invalid_votes) AS invalid_votes, sum(r.valid_votes) AS valid_votes
                       FROM sejm_precinct_protocols r JOIN precincts p USING (precinct_id) GROUP BY 1) s USING (district_no)
            WHERE (s.precincts, s.eligible_voters, s.valid_ballots, s.invalid_votes, s.valid_votes)
                  IS DISTINCT FROM (d.precincts, d.eligible_voters, d.valid_ballots, d.invalid_votes, d.valid_votes)""",
        "domestic precincts without a 1975-1998 voivodeship": """
            SELECT count(*) FROM precincts WHERE teryt_gmina_pre1999 IS NOT NULL AND wojewodztwo IS NULL""",
        "precincts abroad outside Warsaw's Sejm district and voivodeship": """
            SELECT count(*) FROM precincts WHERE precinct_type = 'zagranica' AND (sejm_district_no <> 1 OR senat_district_no <> 1)""",
        "candidates of one district list with different list totals": "SELECT count(*) FROM official_list_votes WHERE totals <> 1",
        "candidates whose district valid votes differ from the district results": f"""
            SELECT count(*) FROM {candidates_src} k JOIN sejm_district_totals d ON d.district_no = int_(k.{k_district})
            WHERE int_(k.{k_district_total}) <> d.valid_votes""",
        "candidates with an unknown seat type": f"""
            SELECT count(*) FROM {candidates_src} WHERE coalesce(trim({k_mandate}), '') NOT IN ('', 'O', 'K')""",
        "national-list seats of candidates not on their national list": """
            SELECT count(*) FROM sejm_candidates WHERE national_list_seat AND national_list_position IS NULL""",
        "list numbers shared by committees in one district": """
            SELECT count(*) FROM (SELECT district_no, list_no FROM sejm_district_lists GROUP BY ALL HAVING count(*) > 1)""",
        "list votes in a column whose list number did not run in the precinct's district": """
            SELECT count(*) FROM sejm_precinct_list_votes_raw r JOIN sejm_list_columns m USING (header)
            WHERE coalesce(int_(r.votes), 0) <> 0 AND NOT EXISTS (
                SELECT 1 FROM sejm_district_lists l WHERE l.district_no = r.district_no AND l.list_no = m.list_no)""",
        "precincts with votes in two columns of one list number": """
            SELECT count(*) FROM (SELECT r.pkey, m.list_no FROM sejm_precinct_list_votes_raw r JOIN sejm_list_columns m USING (header)
                                  WHERE NULLIF(trim(r.votes), '') IS NOT NULL GROUP BY ALL HAVING count(*) > 1)""",
        "precincts whose list votes differ from their valid votes": """
            SELECT count(*) FROM sejm_precinct_protocols r
            LEFT JOIN (SELECT precinct_id, sum(votes) AS votes FROM sejm_precinct_list_votes GROUP BY 1) v USING (precinct_id)
            WHERE coalesce(v.votes, 0) <> r.valid_votes""",
        "district lists whose candidates' votes differ from the list total": """
            SELECT count(*) FROM sejm_district_lists d
            JOIN (SELECT district_no, committee_id, sum(votes) AS votes FROM sejm_candidates GROUP BY ALL) k USING (district_no, committee_id)
            WHERE k.votes <> d.votes""",
        "lists with seats outside the seat allocation (thresholds)": "SELECT count(*) FROM sejm_district_lists WHERE seats > 0 AND NOT in_seat_allocation",
        "district seats in districts != 391": "SELECT abs(sum(seats) - 391) FROM sejm_districts WHERE district_no > 0",
        "national-list seats per list differ from PKW's announcement": f"""
            SELECT count(*) FROM (SELECT list_no, seats FROM sejm_district_lists WHERE district_no = 0 AND seats > 0) l
            FULL JOIN (VALUES {", ".join(f"({n}, {s})" for n, s in official["national_list_seats"].items())}) o(list_no, seats) USING (list_no)
            WHERE l.seats IS DISTINCT FROM o.seats""",
        # National-list seats went down the list, skipping candidates who won a district seat.
        "national-list seats not held by the first candidates in list order without a district seat": """
            SELECT count(*) FROM (
              SELECT k.national_list_seat, l.seats,
                     row_number() OVER (PARTITION BY k.committee_id ORDER BY k.national_list_position, k.candidate_id) AS rank
              FROM sejm_candidates k JOIN national_lists l USING (committee_id)
              WHERE k.national_list_position IS NOT NULL AND NOT (k.elected AND NOT k.national_list_seat))
            WHERE national_list_seat <> (rank <= seats)""",
        "national-list positions held by two candidates other than SHARED_NATIONAL_LIST_POSITIONS, or vice versa": f"""
            SELECT count(*) FROM (SELECT list_no || '/' || national_list_position AS k FROM sejm_candidates
                                  WHERE national_list_position IS NOT NULL GROUP BY ALL HAVING count(*) > 1) d
            FULL JOIN (SELECT unnest([{", ".join(f"'{n}/{p}'" for y, n, p in SHARED_NATIONAL_LIST_POSITIONS if y == year)}]::VARCHAR[]) AS k) s
              ON s.k = d.k
            WHERE d.k IS NULL OR s.k IS NULL""",
        "candidates without a published age": "SELECT count(*) FROM sejm_candidates WHERE age IS NULL",
        f"candidates younger than {MIN_CANDIDATE_AGE_1990S} or older than 100": f"""
            SELECT count(*) FROM sejm_candidates WHERE age < {MIN_CANDIDATE_AGE_1990S} OR age > 100""",
    }
    if "national_lists" in spec:
        checks |= _national_list_register_checks(con, sources.files(spec["national_lists"]))
    if year == 1991:
        checks |= {
            "district lists whose votes, seats or candidates differ from the list table": f"""
                SELECT count(*) FROM sejm_district_lists d
                FULL JOIN (SELECT int_("Nr okr.") AS district_no, int_("Nr listy") AS list_no, int_("L. glosow") AS votes,
                                  int_("L. mand.") AS seats, int_("L. kand.") AS candidates_count FROM {lists_src}) l
                  ON l.district_no = d.district_no AND l.list_no = d.list_no
                WHERE coalesce(d.district_no, 1) > 0
                  AND (d.votes, d.seats, d.candidates_count) IS DISTINCT FROM (l.votes, l.seats, l.candidates_count)""",
        }
    run_checks(con, year, checks)


def _national_list_register_checks(con: duckdb.DuckDBPyConnection, files: list[str]) -> dict[str, str]:
    """Checks of the candidates' national-list positions against the registered national lists (1997 OGPOL files).

    Every OGPOL row must be a register candidate with the same list, position, district, surname (letters only) and
    first given name, and every register position must have an OGPOL row of the same list and district, except the
    rows in NATIONAL_LIST_EXCEPTIONS_1997 / NATIONAL_LIST_REGISTER_ONLY_1997. Column names and their order differ
    between the files, so they are read by name.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TABLE national_list_register AS
        SELECT CAST(regexp_extract(filename, 'OGPOL-(\\d+)__', 1) AS INT) AS list_no,
               CAST(int_(coalesce("Lp", "Lp.")) AS INT) AS position,
               CAST(int_("Okręg") AS INT) AS district_no,
               regexp_replace(upper(trim("Nazwisko")), '[^\\p{{L}}]', '', 'g') AS surname_key,
               split_part(trim(coalesce("Imiona", "Imię")), ' ', 1) AS first_name
        FROM read_parquet({sql_list(files)}, union_by_name = true, filename = true, hive_partitioning = false)
        """
    )
    con.execute("CREATE OR REPLACE TABLE national_list_exceptions (list_no INT, position INT)")
    con.executemany("INSERT INTO national_list_exceptions VALUES (?, ?)", sorted(NATIONAL_LIST_EXCEPTIONS_1997))
    register_only = " OR ".join(
        f"(k.list_no = {n} AND k.national_list_position = {p})" for n, p in sorted(NATIONAL_LIST_REGISTER_ONLY_1997)
    ) or "false"
    exact = """EXISTS (SELECT 1 FROM sejm_candidates k
                       WHERE k.list_no = r.list_no AND k.national_list_position = r.position AND k.district_no = r.district_no
                         AND regexp_replace(k.last_name, '[^\\p{L}]', '', 'g') = r.surname_key
                         AND split_part(k.first_names, ' ', 1) = r.first_name)"""
    return {
        "national-list register rows without list, position, district, surname or given name": """
            SELECT count(*) FROM national_list_register
            WHERE list_no IS NULL OR position IS NULL OR district_no IS NULL OR surname_key = '' OR coalesce(first_name, '') = ''""",
        "national-list register rows unlike the register candidate, other than NATIONAL_LIST_EXCEPTIONS_1997": f"""
            SELECT count(*) FROM national_list_register r
            WHERE NOT {exact} AND NOT EXISTS (SELECT 1 FROM national_list_exceptions e
                                              WHERE e.list_no = r.list_no AND e.position = r.position)""",
        "NATIONAL_LIST_EXCEPTIONS_1997 entries missing from the national-list register or equal to the register": f"""
            SELECT count(*) FROM national_list_exceptions e
            WHERE NOT EXISTS (SELECT 1 FROM national_list_register r
                              WHERE r.list_no = e.list_no AND r.position = e.position AND NOT {exact})""",
        "candidates' national-list positions missing from the national-list register, other than NATIONAL_LIST_REGISTER_ONLY_1997": f"""
            SELECT count(*) FROM sejm_candidates k
            WHERE k.national_list_position IS NOT NULL AND NOT ({register_only}) AND NOT EXISTS (
                SELECT 1 FROM national_list_register r
                WHERE r.list_no = k.list_no AND r.position = k.national_list_position AND r.district_no = k.district_no)""",
        "NATIONAL_LIST_REGISTER_ONLY_1997 entries that are not register candidates missing from the national-list register": f"""
            SELECT {len(NATIONAL_LIST_REGISTER_ONLY_1997)} - count(*) FROM sejm_candidates k
            WHERE ({register_only}) AND NOT EXISTS (
                SELECT 1 FROM national_list_register r
                WHERE r.list_no = k.list_no AND r.position = k.national_list_position AND r.district_no = k.district_no)""",
    }


def _district_lists_from_candidates(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """``sejm_district_lists`` from the candidates and ``official_list_votes`` (layouts without a list
    register): seats are elected candidates, and a committee took part in seat allocation iff it won
    any seat."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_district_lists AS
        WITH cand AS (
          SELECT district_no, committee_id, any_value(list_no) AS list_no, count(*) AS candidates_count,
                 count(*) FILTER (WHERE elected) AS seats
          FROM sejm_candidates GROUP BY ALL
        )
        SELECT {year} AS year, c.district_no, c.committee_id, c.list_no, CAST(c.candidates_count AS INT) AS candidates_count,
               CAST(o.votes AS BIGINT) AS votes,
               CAST(round(100.0 * o.votes / sum(o.votes) OVER (PARTITION BY c.district_no), 2) AS DECIMAL(6, 2)) AS pct_of_district,
               sum(c.seats) OVER (PARTITION BY c.committee_id) > 0 AS in_seat_allocation,
               CAST(c.seats AS INT) AS seats
        FROM cand c LEFT JOIN official_list_votes o USING (district_no, committee_id)
        """
    )


# An unmapped result column whose list is known but whose slot (2019: list + position) or name (2015:
# within the list) is absent from the candidate register belongs to a candidate missing from the register.
_NOT_IN_REGISTER = """u.list_no IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM sejm_candidates k
    WHERE k.district_no = u.district_no AND k.list_no = u.list_no
      AND (k.list_position = u.position OR (u.position IS NULL AND name_key(k.full_name) = name_key(u.header))))"""


def _check(con: duckdb.DuckDBPyConnection, year: int, candidate_precincts: bool = True) -> None:
    """Fail the build unless precinct facts reproduce the official totals.

    Without per-precinct candidate results (2005) the candidate checks are skipped; candidate totals
    are then only checked against their list totals.
    """
    for district, gmina, number in con.execute(
        """SELECT p.sejm_district_no, p.gmina, p.precinct_no FROM sejm_precinct_protocols r
           JOIN precincts p USING (precinct_id) WHERE r.valid_votes IS NULL"""
    ).fetchall():
        log.warning(
            "year %s: PKW published no results for precinct %s/%s in district %s; that district's totals are not reconciled",
            year, gmina, number, district,
        )
    # Result columns for a (list, position) slot that the candidate register does not contain at all
    # (e.g. a candidate who died before election day in 2019) cannot be mapped; report, don't fail.
    for district, header in con.execute(
        f"SELECT district_no, header FROM sejm_candidate_columns_unmapped u WHERE {_NOT_IN_REGISTER}"
    ).fetchall():
        log.warning("year %s: district %s column %r has no candidate in the register; its votes are skipped", year, district, header)
    for district, address in con.execute("SELECT district_no, address FROM sejm_rows_without_number").fetchall():
        log.warning(
            "year %s: district %s candidate file has a combined row %r without precinct number; its votes count "
            "towards the candidate totals but belong to no precinct", year, district, address.strip(),
        )
    unpublished_precincts = "(SELECT precinct_id FROM sejm_precinct_protocols WHERE valid_votes IS NULL)"
    unpublished = "(SELECT p.sejm_district_no FROM sejm_precinct_protocols r JOIN precincts p USING (precinct_id) WHERE r.valid_votes IS NULL)"
    districts = SEJM_DISTRICTS.get(year, 41)
    checks = {
        f"Sejm districts != {districts}": f"SELECT abs(count(*) - {districts}) FROM sejm_districts",
        "district lists without an official vote total": "SELECT count(*) FROM sejm_district_lists WHERE votes IS NULL",
        "Sejm seats in districts != 460": "SELECT abs(sum(seats) - 460) FROM sejm_districts",
        "elected Sejm candidates != 460": "SELECT abs(count(*) FILTER (WHERE elected) - 460) FROM sejm_candidates",
        # 1991-1997: national-list seats (district 0) went to candidates of district lists.
        "districts whose elected candidates differ from their seats": """
            SELECT count(*) FROM sejm_districts d
            WHERE d.seats <> (SELECT count(*) FROM sejm_candidates k
                              WHERE k.elected AND CASE WHEN d.district_no = 0 THEN k.national_list_seat
                                                       ELSE k.district_no = d.district_no AND NOT k.national_list_seat END)""",
        "precincts missing from the precinct register": f"""
            SELECT count(*) FROM precincts WHERE NOT in_register AND precinct_id NOT IN {unpublished_precincts}""",
        "duplicate precinct keys": "SELECT count(*) - count(DISTINCT pkey) FROM precincts",
        "list-result rows of unknown precincts": "SELECT count(*) FROM sejm_precinct_list_votes_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
        "candidate-result rows of unknown precincts": "SELECT count(*) FROM sejm_precinct_candidate_votes_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
        "candidate columns that match no candidate": f"""
            SELECT count(*) FROM sejm_candidate_columns_unmapped u WHERE NOT ({_NOT_IN_REGISTER})""",
        "candidate-file rows without a precinct number, other than combined military precincts": f"""
            SELECT count(*) FROM sejm_rows_without_number WHERE address NOT LIKE '{COMBINED_PRECINCTS}'""",
        "candidates not read from exactly one column": """
            SELECT count(*) FROM sejm_candidates k
            WHERE (SELECT count(*) FROM sejm_precinct_candidate_votes_columns m WHERE m.key = k.candidate_id) <> 1""",
        "district lists whose precinct votes differ from the official total": f"""
            SELECT count(*) FROM sejm_district_lists d
            LEFT JOIN (SELECT p.sejm_district_no, v.committee_id, sum(v.votes) AS votes
                       FROM sejm_precinct_list_votes v JOIN precincts p USING (precinct_id) GROUP BY ALL) s
              ON s.sejm_district_no = d.district_no AND s.committee_id = d.committee_id
            WHERE coalesce(s.votes, 0) <> d.votes AND d.district_no NOT IN {unpublished}
              AND NOT EXISTS (SELECT 1 FROM sejm_candidate_columns_unmapped u
                              WHERE u.district_no = d.district_no AND u.list_no = d.list_no)""",
        # A list can have more votes than its registered candidates (votes for struck-off candidates
        # count for the list), never fewer.
        "lists with fewer votes than their candidates": """
            SELECT count(*) FROM sejm_district_lists d
            JOIN (SELECT district_no, committee_id, sum(votes) AS votes FROM sejm_candidates GROUP BY ALL) k
              USING (district_no, committee_id)
            WHERE k.votes > d.votes""",
        # Votes of a combined row (2011) belong to no precinct but are part of the official total.
        "candidates whose precinct votes (plus combined rows) differ from the official total": f"""
            SELECT count(*) FROM sejm_candidates k
            LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM sejm_precinct_candidate_votes GROUP BY ALL) s
              USING (candidate_id)
            LEFT JOIN sejm_combined_candidate_votes c USING (candidate_id)
            WHERE coalesce(s.votes, 0) + coalesce(c.votes, 0) <> k.votes AND k.district_no NOT IN {unpublished}""",
    }
    if not candidate_precincts:
        for label in ("candidates not read from exactly one column",
                      "candidates whose precinct votes (plus combined rows) differ from the official total"):
            del checks[label]
    run_checks(con, year, checks)
