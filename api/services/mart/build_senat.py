"""Build the Senate tables of one election year in DuckDB (loader side).

Supported layouts:
- CSV (2019, 2023): ``kandydaci_senat``, ``okregi_senat`` and one wide
  ``wyniki_gl_na_kand*_po_obwodach_senat`` file per district. The protocol comes from
  ``protokoly_po_obwodach_senat`` (2023) or from the protocol columns of the district files (2019).
- Excel (2015): ``2015-kand-sen.xls`` and ``wyniki_okr_senat/NNN.xlsx`` (protocol columns prefixed
  ``Senat - `` followed by one column per candidate). There is no district register, so
  ``senat_districts`` only has district numbers.
- 2011: ``kandydaci_senat``, ``parl2011okręgi`` and ``2011-senat-głosy-w-obwodach/sNNN`` (protocol
  and bare-name candidate columns, like 2019).
- 2001/2005/2007: ``2001-kan-sen`` / ``kandsen<year>`` (candidates with official votes), the district
  table (``okręgi2001senat`` / ``senat2005okręgi`` / ``okr2007senat``: seat city, seats, boundaries) and,
  in 2001 and 2007, ``parl2001OBWSEN/SENOBWNN`` / ``parl2007-sen-gl_kand_obw/sen2007obw-NN`` (protocol
  and ``'N. Surname Given (T)'`` candidate columns, 2001: ``(W)``). 40 districts elected 2-4 senators
  each. 2005 publishes candidate votes per powiat only (``2005-sen-gl_kan_pow/SNT_NN_g_p``): they are
  checked against the official totals, and the Senate precinct tables stay empty for 2005.
Requires the ``precincts`` table built by ``build_sejm`` in the same DuckDB connection.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from api.services.mart.build_common import (
    PATRONYMIC,
    BuildError,
    ColumnRef,
    YearSources,
    build_wide_votes,
    candidate_index,
    column,
    columns_of,
    district_files,
    map_candidate_columns,
    parse_name_dash_label,
    parse_numbered_names,
    parse_plain_names,
    pct_expr,
    python_name_key,
    quote_ident,
    run_checks,
    sql_list,
    sql_literal,
)
from api.services.mart.build_precincts import COMBINED_PRECINCTS, rows_without_number
from api.services.mart.build_sejm import MIN_CANDIDATE_AGE_1990S

log = logging.getLogger("mart")

CANDIDATE_RESULTS = "**/wyniki_gl_na_kand*_po_obwodach_senat_csv/*.parquet"
CANDIDATES_2011 = "**/sejmsenat/kandydaci_senat/*.parquet"
RESULTS_2011 = "**/sejmsenat/*-senat-głosy-w-obwodach/*.parquet"
PROTOCOLS = "**/protokoly_po_obwodach_senat_csv/*.parquet"
EXCEL_CANDIDATES = "**/senat/2015-kand-sen.parquet"
EXCEL_RESULTS = "**/senat/wyniki_okr_senat/*.parquet"
CANDIDATES_2005 = "**/senat/kandsen*.parquet"
CANDIDATES_2001 = "**/senat/*-kan-sen.parquet"
DISTRICTS_2005 = "**/senat/*okr*.parquet"
RESULTS_2007 = "**/senat/parl*-sen-gl_kand_obw/*.parquet"
RESULTS_2001 = "**/senat/parl*OBWSEN/*.parquet"
POWIAT_RESULTS_2005 = "**/senat/*-sen-gl_kan_pow/*.parquet"
# Senate districts per year where not 100 single-member districts (2001-2007: 40 districts, 2-4 seats;
# 1991-1997: the 49 voivodeships, 2-3 seats).
DISTRICTS = {1991: 49, 1993: 49, 1997: 49, 2001: 40, 2005: 40, 2007: 40}

PROTOCOL_ALIASES = {
    "eligible_voters": ("Liczba wyborców uprawnionych do głosowania", "Senat - Liczba wyborców uprawnionych do głosowania"),
    "ballots_issued": (
        "Liczba wyborców, którym wydano karty do głosowania w lokalu wyborczym oraz w głosowaniu korespondencyjnym (łącznie)",
        "Liczba wyborców, którym wydano karty do głosowania",
        "Senat - Liczba wyborców, którym wydano karty do głosowania",
    ),
    "valid_ballots": ("Liczba kart ważnych", "Senat - Liczba kart ważnych"),
    "invalid_votes": ("Liczba głosów nieważnych", "Senat - Liczba głosów nieważnych"),
    "valid_votes": (
        "Liczba głosów ważnych oddanych łącznie na wszystkich kandydatów",
        "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
        "Senat - Liczba głosów ważnych oddanych łącznie na wszystkich kandydatów",
        "Liczba głosów ważnych",
        "Ważne",  # 2007: valid ballots; each voter had as many votes as the district had seats
    ),
}
# 2007 headers. There is no invalid-votes column: 'Odd' (votes cast) = valid ballots, and invalid votes
# are cast minus valid.
PROTOCOL_ALIASES["eligible_voters"] += ("Upr",)
PROTOCOL_ALIASES["ballots_issued"] += ("Frekw",)
PROTOCOL_ALIASES["valid_ballots"] += ("Odd",)
# 2001 headers: valid and invalid votes are counted in ballots ('Karty ważne z głosami (nie)ważnymi').
PROTOCOL_ALIASES["eligible_voters"] += ("Uprawnieni do głosowania",)
PROTOCOL_ALIASES["ballots_issued"] += ("Wydane karty (frekwencja)",)
PROTOCOL_ALIASES["valid_ballots"] += ("Karty ważne",)
PROTOCOL_ALIASES["invalid_votes"] += ("Karty ważne z głosami nieważnymi",)
PROTOCOL_ALIASES["valid_votes"] += ("Karty ważne z głosami ważnymi",)
# Rows of the 2001 per-precinct files whose address is split over two cells followed by an empty cell, so
# that the rest of the row sits two columns to the right: (district, TERYT, precinct number). Łęczna 6:
# with the row realigned, district 6 reproduces the official candidate totals.
SHIFTED_ROWS_2001 = {(6, "061003", 6)}
TERYT_ALIASES = ("TERYT Gminy", "Kod TERYT", "TERYT gminy", "Kod terytorialny gminy", "TERYT", "Kod gminy")
NUMBER_ALIASES = ("Nr komisji", "Numer", "Numer obwodu", "Nr obwodu głosowania", "Nr obwodu", "Nr obw.")

# 1991-1997: the 49 voivodeships (1975-1998) were the Senate districts, numbered as 'Nr woj.' in every
# file. Sources per year: candidates with official votes ('Mandat' T = elected), per-precinct files
# (1993, 1997; protocol and 'Surname Given' candidate columns), the gmina turnout file (1993) and the
# district table (1997). 1991 publishes candidates only.
SOURCES_1990S = {
    1991: {"candidates": "**/senat/KANSEN1991.parquet"},
    1993: {"candidates": "**/senat/KANSEN1993.parquet", "results": "**/senat/senat1993/sen1993obw*.parquet",
           "gminas": "**/senat/1993-frekw-gm.parquet"},
    1997: {"candidates": "**/senat/ksenat1997pop.parquet", "results": "**/senat/obwsenat97/OBWSEN*.parquet",
           "districts": "**/senat/senat1997okr.parquet"},
}
# Two senators per voivodeship and three in warszawskie (1) and katowickie (14): the
# Senate electoral law of 10 May 1991 (Dz.U. 1991 nr 58 poz. 246, art. 2 ust. 3).
SENATE_SEATS_1990S = {1: 3, 14: 3}
# National figures from PKW's announcements on the Senate results: M.P. 1991 nr 41 poz. 287, M.P. 1993
# nr 50 poz. 471, M.P. 1997 nr 64 poz. 621 ('głosy oddane' = valid + invalid ballots; 'ważne' counts
# ballots, each voter had as many votes as the voivodeship had seats). 1993 has the Sejm's eligible-voter
# gap (OFFICIAL_1990S in build_sejm: 21,807 missing in two Sejm districts in KBW's files).
OFFICIAL_SENAT_1990S = {
    1991: {"eligible_voters": 27517280, "votes_cast": 11887865, "invalid_votes": 413019, "valid_votes": 11474846},
    1993: {"eligible_voters": 27677302, "eligible_voters_missing": 21807, "votes_cast": 14408367, "invalid_votes": 422832,
           "valid_votes": 13985535},
    1997: {"eligible_voters": 28409054, "votes_cast": 13614101, "invalid_votes": 296149, "valid_votes": 13317952},
}
# 1993 per-precinct files: skierniewickie (38) has candidate columns but no protocol columns ('Odd.',
# 'Ważne'); its precincts get an empty Senate protocol and its protocol figures exist per gmina only
# (1993-frekw-gm). Zielonogórskie (49) is published twice: 'sen1993obw49.XLS' (precinct register with
# eligible voters only) and 'sen1993obw49.xlsx' (results); the register copy must match the results file.
SENATE_NO_PROTOCOL_1993 = {38}
SENATE_REGISTER_COPY_1993 = {49}


def available(sources: YearSources) -> bool:
    """Whether the year has staged Senate candidates and per-precinct results (1991: candidates only)."""
    if sources.year in SOURCES_1990S:
        return sources.has(SOURCES_1990S[sources.year]["candidates"])
    csv = sources.has("**/kandydaci_senat_csv/*.parquet") and sources.has(CANDIDATE_RESULTS)
    layout_2011 = sources.has(CANDIDATES_2011) and sources.has(RESULTS_2011)
    layout_2005 = (sources.has(CANDIDATES_2005) or sources.has(CANDIDATES_2001)) and (
        sources.has(RESULTS_2007) or sources.has(RESULTS_2001) or sources.has(POWIAT_RESULTS_2005)
    )
    return csv or layout_2011 or layout_2005 or (sources.has(EXCEL_CANDIDATES) and sources.has(EXCEL_RESULTS))


def _build_excel_registers(con: duckdb.DuckDBPyConnection, year: int, candidates_src: str) -> None:
    """2015: candidates from ``2015-kand-sen.xls``; districts only as numbers (no district register)."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr okr."), k."Nazwisko", k."Imiona") AS INT) AS candidate_id,
               CAST(int_(k."Nr okr.") AS INT) AS district_no,
               strip_signature(k."Komitet") AS committee_name,
               trim(k."Imiona") || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name, trim(k."Imiona") AS first_names,
               gender_(k."Płeć") AS gender, k."Zawód" AS occupation, k."Miejsce zam." AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt,
               k."Przynależność do partii politycznej" AS party_membership, NULL AS party_support,
               int_(k."Głosy") AS votes,
               {pct_expr(con, candidates_src, '"% w okr."')} AS pct_of_district,
               yes_(k."Mandat") AS elected, CAST(NULL AS INT) AS age
        FROM {candidates_src} k
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_districts AS
        SELECT DISTINCT {year} AS year, district_no, CAST(NULL AS VARCHAR) AS seat_city,
               CAST(NULL AS INT) AS eligible_voters, CAST(NULL AS VARCHAR) AS description
        FROM senat_candidates
        """
    )


def _build_csv_registers(con: duckdb.DuckDBPyConnection, year: int, sources: YearSources) -> None:
    """2019/2023: candidates and districts from ``kandydaci_senat`` and ``okregi_senat``.

    2011: ``kandydaci_senat`` with other headers (surnames in mixed case) and the Senate rows of the
    district table ``parl2011okręgi``.
    """
    layout_2011 = sources.has(CANDIDATES_2011)
    candidates_src = sources.load("senat_candidates", CANDIDATES_2011 if layout_2011 else "**/kandydaci_senat_csv/*.parquet")
    if column(con, candidates_src, "Nazwisko i imiona", required=False):
        full_name = '"Nazwisko i imiona"'
    else:
        full_name = """("Imiona" || ' ' || upper("Nazwisko"))"""
    position = column(con, candidates_src, "Pozycja na liście", "Numer_na_karcie")
    party = column(con, candidates_src, "Przynależność do partii", "Partia polityczna")
    support = column(con, candidates_src, "Poparcie", required=False)
    pct = column(con, candidates_src, "Procent głosów oddanych w okręgu", "% głosów w okręgu")
    elected = column(con, candidates_src, "Czy przyznano mandat", "Wybrany")
    residence_teryt = column(con, candidates_src, "TERYT m. z.", required=False)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr okręgu"), int_(k.{position}), {full_name}) AS INT) AS candidate_id,
               CAST(int_(k."Nr okręgu") AS INT) AS district_no,
               strip_signature(k."Nazwa komitetu") AS committee_name,
               display_name({full_name}) AS full_name,
               surname({full_name}) AS last_name,
               given_names({full_name}) AS first_names,
               gender_(k."Płeć") AS gender,
               k."Zawód" AS occupation,
               k."Miejsce zamieszkania" AS residence,
               {f"clean_teryt(k.{residence_teryt})" if residence_teryt else "CAST(NULL AS VARCHAR)"} AS residence_teryt,
               k.{party} AS party_membership,
               {f"NULLIF(trim(k.{support}), '')" if support else "CAST(NULL AS VARCHAR)"} AS party_support,
               int_(k."Liczba głosów") AS votes,
               {pct_expr(con, candidates_src, pct)} AS pct_of_district,
               yes_(k.{elected}) AS elected, CAST(NULL AS INT) AS age
        FROM {candidates_src} k
        """
    )
    if layout_2011:
        districts_src = sources.load("senat_districts", "**/sejmsenat/parl*okręgi.parquet")
        con.execute(
            f"""
            CREATE OR REPLACE TABLE senat_districts AS
            SELECT {year} AS year,
                   CAST(int_(d."Nr okręgu") AS INT) AS district_no,
                   (SELECT any_value(k."Siedziba OKW") FROM {candidates_src} k WHERE int_(k."Nr okręgu") = int_(d."Nr okręgu")) AS seat_city,
                   CAST(NULLIF(int_(d."Liczba wyborców"), 0) AS INT) AS eligible_voters,
                   regexp_replace(d."Granice okręgu wyborczego", '\\s+', ' ', 'g') AS description
            FROM {districts_src} d WHERE d."Izba" = 'Senat'
            """
        )
        return
    districts_src = sources.load("senat_districts", "**/okregi_senat_csv/*.parquet")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_districts AS
        SELECT {year} AS year,
               CAST(int_(d."Numer okręgu") AS INT) AS district_no,
               d."Siedziba OKW" AS seat_city,
               CAST(NULLIF(int_(d."Wyborcy"), 0) AS INT) AS eligible_voters,
               regexp_replace(d."Opis granic", '\\s+', ' ', 'g') AS description
        FROM {districts_src} d
        """
    )


def _build_2005_registers(con: duckdb.DuckDBPyConnection, year: int, sources: YearSources) -> None:
    """2001/2005/2007: candidates from ``2001-kan-sen`` / ``kandsen<year>`` and districts from the district table.

    ``senat_district_seats`` keeps the seats per district for the checks (not a mart column).
    2005 gives each candidate's district valid votes ('Głos w okr.'), 2001 and 2007 the percentage.
    2001 has the given names in two columns and the party affiliation ('Afiliacja': membership or
    'pop. ...' support, as published).
    """
    candidates_src = sources.load("senat_candidates", CANDIDATES_2005 if sources.has(CANDIDATES_2005) else CANDIDATES_2001)
    districts_src = sources.load("senat_districts", DISTRICTS_2005)
    if district_votes := column(con, candidates_src, "Głos w okr.", required=False):
        pct = f'CAST(round(100.0 * int_(k."Głosy") / NULLIF(int_(k.{district_votes}), 0), 2) AS DECIMAL(6, 2))'
    else:
        pct = pct_expr(con, candidates_src, '"%"')
    k_district = column(con, candidates_src, "Okręg", "Nr okr.")
    k_position = column(con, candidates_src, "Nr poz.", "Poz.")
    if column(con, candidates_src, "Imiona", required=False):
        first_names = 'trim(k."Imiona")'
    else:
        first_names = """concat_ws(' ', NULLIF(trim(k."1. Imię"), ''), NULLIF(trim(k."2. Imię"), ''))"""
    affiliation = column(con, candidates_src, "Afiliacja", required=False)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{k_district}), int_(k.{k_position})) AS INT) AS candidate_id,
               CAST(int_(k.{k_district}) AS INT) AS district_no,
               strip_signature(k."Komitet wyborczy") AS committee_name,
               {first_names} || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name, {first_names} AS first_names,
               gender_(k."Płeć") AS gender, k."Zawód" AS occupation,
               k.{column(con, candidates_src, "Miejsce zamieszkania", "Mieszka")} AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt,
               {f"NULLIF(trim(k.{affiliation}), '')" if affiliation else "CAST(NULL AS VARCHAR)"} AS party_membership,
               CAST(NULL AS VARCHAR) AS party_support,
               int_(k."Głosy") AS votes, {pct} AS pct_of_district, yes_(k."Mandat") AS elected,
               CAST(NULL AS INT) AS age
        FROM {candidates_src} k
        """
    )
    number = column(con, districts_src, "Numer okręgu", "Numer okręgu -", "Nr okręgu")
    # 2005/2007: 'Liczba' spans four columns (powiats, gminas, precincts, seats); seats is the last one.
    seats = column(con, districts_src, "mandatów", "column7", "Liczba mandatów")
    description = column(con, districts_src, "Obszar okręgu wyborczego", "Obszar okręgu wyborczego -")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_districts AS
        WITH rows AS (
          -- A row without a district number continues the description of the district above (2001).
          SELECT *, count(int_({number})) OVER (ORDER BY filename, file_row_number) AS district_row
          FROM read_parquet({sql_list(sources.files(DISTRICTS_2005))}, filename = true, file_row_number = true)
        )
        SELECT {year} AS year, CAST(int_(d.{number}) AS INT) AS district_no,
               d.{column(con, districts_src, "Siedziba Okręgowej Komisji Wyborczej", "Siedziba Okręgowej Komisji Wyborczej -")} AS seat_city,
               CAST(NULL AS INT) AS eligible_voters,
               (SELECT regexp_replace(string_agg(c.{description}, ' ' ORDER BY c.file_row_number), '\\s+', ' ', 'g')
                FROM rows c WHERE c.district_row = d.district_row) AS description
        FROM rows d WHERE int_(d.{number}) IS NOT NULL
        """
    )
    con.execute(
        f"""CREATE OR REPLACE TABLE senat_district_seats AS
            SELECT CAST(int_({number}) AS INT) AS district_no, CAST(int_({seats}) AS INT) AS seats
            FROM {districts_src} WHERE int_({number}) IS NOT NULL"""
    )


def _check_2005_powiats(con: duckdb.DuckDBPyConnection, year: int, sources: YearSources) -> None:
    """2005: candidate votes per powiat (``SNT_NN_g_p``) must add up to the official totals.

    The last row of each file ('Suma') is ignored: in 24 of the 40 files it holds zeros only.
    """
    index = candidate_index(con, "senat_candidates", None)
    con.execute("CREATE OR REPLACE TABLE senat_powiat_votes (district_no INT, header VARCHAR, candidate_id INT, votes BIGINT)")
    unmapped = []
    for district, path in district_files(sources.files(POWIAT_RESULTS_2005), r"^SNT_(\d+)_").items():
        source = f"read_parquet({sql_literal(path)}, hive_partitioning = false)"
        for header in columns_of(con, source)[columns_of(con, source).index("Razem na kandydatów") + 1:]:
            candidate_id = index.resolve(district, ColumnRef(name_key=python_name_key(header)))
            if candidate_id is None:
                unmapped.append(f"district {district}: {header}")
                continue
            con.execute(
                f"""INSERT INTO senat_powiat_votes
                    SELECT {district}, ?, {candidate_id}, sum(int_({quote_ident(header)})) FROM {source} WHERE "Powiat" <> 'Suma'""",
                [header],
            )
    con.execute("CREATE OR REPLACE TABLE senat_powiat_unmapped AS SELECT unnest(?::VARCHAR[]) AS header", [unmapped])
    run_checks(
        con,
        year,
        {
            "Senate powiat-file columns that match no candidate": "SELECT count(*) FROM senat_powiat_unmapped",
            "Senate candidates not read from exactly one powiat-file column": """
                SELECT count(*) FROM senat_candidates k
                WHERE (SELECT count(*) FROM senat_powiat_votes v WHERE v.candidate_id = k.candidate_id) <> 1""",
            "Senate candidates whose powiat votes differ from the official total": """
                SELECT count(*) FROM senat_candidates k JOIN senat_powiat_votes v USING (candidate_id) WHERE v.votes <> k.votes""",
        },
    )


def _realign_2001(
    con: duckdb.DuckDBPyConnection, by_district: dict[int, str], tmp_dir: str
) -> dict[int, str]:
    """2001: copies of the district files with the SHIFTED_ROWS_2001 rows moved back into place.

    Fills ``senat_misaligned_rows(district_no, teryt, precinct_no)`` with every other row that has a
    value in the unnamed columns right of the last candidate (a check fails on any). A listed row
    that is not found, or whose cell after the split address is not empty, fails the build.
    """
    out, found = {}, set()
    for district, path in by_district.items():
        table = pq.read_table(path, partitioning=None)
        names = table.column_names
        extra = [i for i, name in enumerate(names) if re.fullmatch(r"column\d+", name)]
        rows, changed = table.to_pylist(), False
        for row in rows:
            key = (district, (row["Kod gminy"] or "").strip(), int(row["Nr obw."]))
            values = [row[name] for name in names]
            if key in SHIFTED_ROWS_2001:
                a = names.index("Adres")
                if values[a + 2] not in (None, ""):
                    raise BuildError(f"year 2001: Senate district {district} row {key} is not shifted as expected")
                address = " ".join(v.strip() for v in values[a:a + 2] if v and v.strip())
                values = values[:a] + [address] + values[a + 3:] + [None, None]
                row.update(zip(names, values))
                found.add(key)
                changed = True
            elif any(values[i] not in (None, "") for i in extra):
                con.execute("INSERT INTO senat_misaligned_rows VALUES (?, ?, ?)", list(key))
        out[district] = path
        if changed:
            out[district] = str(Path(tmp_dir) / Path(path).name)
            pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), out[district])
    if missing := {k for k in SHIFTED_ROWS_2001 if k[0] in by_district} - found:
        raise BuildError(f"year 2001: shifted Senate rows {sorted(missing)} not found")
    return out


def build_senat(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check all Senate tables of ``sources.year``."""
    year = sources.year
    if year in SOURCES_1990S:
        _build_1990s(con, sources)
        return
    if sources.has(CANDIDATES_2005) or sources.has(CANDIDATES_2001):
        _build_2005_registers(con, year, sources)
        if sources.has(RESULTS_2001):
            by_district = district_files(sources.files(RESULTS_2001), r"^SENOBW(\d+)__")
        elif not sources.has(RESULTS_2007):
            # 2005: no per-precinct results; empty precinct tables, candidate totals checked per powiat.
            _check_2005_powiats(con, year, sources)
            con.execute("CREATE OR REPLACE TABLE senat_precinct_protocols (year INT, precinct_id INT, eligible_voters INT, "
                        "ballots_issued INT, valid_ballots INT, invalid_votes INT, valid_votes INT)")
            con.execute("CREATE OR REPLACE TABLE senat_precinct_candidate_votes (year INT, precinct_id INT, candidate_id INT, votes INT)")
            _check(con, year, precinct_results=False)
            return
        else:
            by_district = district_files(sources.files(RESULTS_2007), r"obw-(\d+)__")
    elif sources.has(EXCEL_CANDIDATES) and not sources.has(CANDIDATE_RESULTS):
        _build_excel_registers(con, year, sources.load("senat_candidates", EXCEL_CANDIDATES))
        by_district = district_files(sources.files(EXCEL_RESULTS), r"^(\d+)\.parquet$")
    elif sources.has(RESULTS_2011):
        _build_csv_registers(con, year, sources)
        by_district = district_files(sources.files(RESULTS_2011), r"^s(\d+)__")
    else:
        _build_csv_registers(con, year, sources)
        by_district = district_files(sources.files(CANDIDATE_RESULTS), r"(?:okreg|okr)_(\d+)_")
    con.execute("CREATE OR REPLACE TABLE senat_misaligned_rows (district_no INT, teryt VARCHAR, precinct_no INT)")
    # Corrected copies of source files live until the votes are read (2001 only).
    corrected = tempfile.TemporaryDirectory(prefix="mart_senat_")
    if sources.has(RESULTS_2001):
        by_district = _realign_2001(con, by_district, corrected.name)
    # Rows without a precinct number (2011: combined military precincts abroad); empty otherwise.
    if sources.has(RESULTS_2011):
        rows_without_number(con, by_district, "senat_rows_without_number")
    else:
        con.execute("CREATE OR REPLACE TABLE senat_rows_without_number (district_no INT, address VARCHAR)")

    if sources.has(PROTOCOLS):
        protocol_sources = [sources.load("senat_protocols", PROTOCOLS)]
    else:  # 2019: the protocol is repeated in front of the candidate columns of every district file.
        protocol_sources = [f"read_parquet({sql_literal(path)})" for path in by_district.values()]
    parts = []
    for source in protocol_sources:
        cols = {name: column(con, source, *aliases, required=name != "invalid_votes") for name, aliases in PROTOCOL_ALIASES.items()}
        protocol = ",\n".join(f"int_({col}) AS {name}" for name, col in cols.items() if col)
        if cols["invalid_votes"] is None:  # 2007: votes cast (valid ballots) minus valid votes
            protocol += f",\nint_({cols['valid_ballots']}) - int_({cols['valid_votes']}) AS invalid_votes"
        parts.append(
            f"SELECT pkey({column(con, source, *TERYT_ALIASES)}, {column(con, source, *NUMBER_ALIASES)}) AS pkey, {protocol} FROM {source}"
        )
    con.execute("CREATE OR REPLACE TABLE senat_protocols_raw AS " + " UNION ALL ".join(parts))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, r.eligible_voters, r.ballots_issued, r.valid_ballots, r.invalid_votes, r.valid_votes
        FROM senat_protocols_raw r JOIN precincts p USING (pkey)
        -- 2019 repeats unpublished (all-empty) military precincts in every district file: keep one row.
        QUALIFY row_number() OVER (PARTITION BY p.precinct_id ORDER BY r.valid_votes IS NULL) = 1
        """
    )

    index = candidate_index(con, "senat_candidates", None)
    keys_by_district: dict[int, set[str]] = {}
    for district, name in con.execute("SELECT district_no, full_name FROM senat_candidates").fetchall():
        keys_by_district.setdefault(district, set()).add(python_name_key(name))
    files = map_candidate_columns(
        con,
        files_by_district=by_district,
        index=index,
        # 2019 headers are bare names, 2007 '1. Surname Given (T)' (surnames may contain ' - '), 2023
        # 'NAME - KW SHORT'.
        parse=lambda cols, district: (
            parse_plain_names(cols, keys_by_district.get(district, set())) or parse_numbered_names(cols) or parse_name_dash_label(cols)
        ),
        unmapped_table="senat_candidate_columns_unmapped",
    )
    # Namesakes are told apart in the register by a patronymic ('Mieczysław s. Stanisława' and 'Mieczysław
    # s. Tomasza BAGIŃSKI', district 59 in 2011); the result file repeats the bare name in ballot order
    # ('BAGIŃSKI Mieczysław', 'BAGIŃSKI Mieczysław_1'), and candidate ids follow the ballot order.
    namesakes: dict[tuple[int, str], list[int]] = {}
    for candidate_id, district, name in con.execute(
        "SELECT candidate_id, district_no, full_name FROM senat_candidates ORDER BY candidate_id"
    ).fetchall():
        if re.search(PATRONYMIC, name):
            namesakes.setdefault((district, python_name_key(re.sub(PATRONYMIC, "", name))), []).append(candidate_id)
    for district, path, mapping in files:
        for header in columns_of(con, f"read_parquet({sql_literal(path)})"):
            bare, repeat = re.match(r"^(.*?)(?:_(\d+))?$", header).groups()
            ids = namesakes.get((district, python_name_key(bare)), [])
            if header not in mapping and int(repeat or 0) < len(ids):
                mapping[header] = ids[int(repeat or 0)]
    build_wide_votes(
        con, year=year, target="senat_precinct_candidate_votes", key_name="candidate_id", files=files,
        teryt_aliases=TERYT_ALIASES, number_aliases=NUMBER_ALIASES,
    )
    corrected.cleanup()
    con.execute(
        """
        CREATE OR REPLACE TABLE senat_combined_candidate_votes AS
        SELECT m.key AS candidate_id, sum(coalesce(int_(r.votes), 0)) AS votes
        FROM senat_precinct_candidate_votes_raw r
        JOIN senat_precinct_candidate_votes_columns m ON m.district_no = r.district_no AND m.header = r.header
        WHERE r.pkey IS NULL AND r.district_no IN (SELECT district_no FROM senat_rows_without_number)
        GROUP BY ALL
        """
    )
    _check(con, year)


def _build_1990s(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """1991, 1993 and 1997 (SOURCES_1990S): 49 voivodeship districts electing 2 or 3 senators each.

    1991 publishes only the candidates with their official votes: the Senate precinct tables stay empty.
    1993 and 1997 publish one file per voivodeship with the protocol ('Upr.', 'Odd.' = votes cast, 1997
    'Niew.', 'Ważne' = valid ballots) and one 'Surname Given' column per candidate; ballots issued are not
    published (NULL), 1993 invalid votes are cast minus valid. See SENATE_NO_PROTOCOL_1993 and
    SENATE_REGISTER_COPY_1993 for the two irregular 1993 files. Requires the 1990s ``precincts`` of
    ``build_sejm`` (5-digit gmina code and precinct number as ``pkey``).
    """
    year = sources.year
    spec = SOURCES_1990S[year]
    src = sources.load("senat_candidates", spec["candidates"])
    if year == 1997:
        first_names = """concat_ws(' ', NULLIF(trim(k."1. imię"), ''), NULLIF(trim(k."2. imię"), ''))"""
        position, committee, votes, residence = '"Poz."', '"Komitet"', '"Głosy"', '"Miejscowość"'
        pct = 'CAST(round(100.0 * int_(k."Głosy") / NULLIF(int_(k."Ważne w woj."), 0), 2) AS DECIMAL(6, 2))'
    else:
        first_names = 'trim(k."Imiona")'
        position, committee, votes, residence = '"Pozycja"', '"Komitet wyborczy"', '"Liczba głosów"', '"Miejsce zamieszkania"'
        pct = pct_expr(con, src, '"%"')
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Nr woj."), int_(k.{position})) AS INT) AS candidate_id,
               CAST(int_(k."Nr woj.") AS INT) AS district_no,
               strip_signature(k.{committee}) AS committee_name,
               {first_names} || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name, {first_names} AS first_names,
               gender_(k."Płeć") AS gender, NULLIF(trim(k."Zawód"), '') AS occupation,
               NULLIF(trim(k.{residence}), '') AS residence, CAST(NULL AS VARCHAR) AS residence_teryt,
               CAST(NULL AS VARCHAR) AS party_membership, NULLIF(trim(k."Poparcie"), '') AS party_support,
               int_(k.{votes}) AS votes, {pct} AS pct_of_district, yes_(k."Mandat") AS elected,
               CAST(int_(k."Wiek") AS INT) AS age
        FROM {src} k
        """
    )
    seat_cases = " ".join(f"WHEN {d} THEN {n}" for d, n in SENATE_SEATS_1990S.items())
    con.execute(
        f"""CREATE OR REPLACE TABLE senat_district_seats AS
            SELECT DISTINCT district_no, CASE district_no {seat_cases} ELSE 2 END AS seats FROM senat_candidates"""
    )
    if year == 1997:
        districts_src = sources.load("senat_district_table", spec["districts"])
        con.execute(
            f"""
            CREATE OR REPLACE TABLE senat_district_totals AS
            SELECT CAST(int_("Nr woj.") AS INT) AS district_no, trim("Miasto woj.") AS seat_city, trim("Nazwa woj.") AS name,
                   int_("L. mand.") AS seats, int_("L. obw.") AS precincts, int_("L. upr.") AS eligible_voters,
                   int_("Odd.") AS valid_ballots, int_("Niew.") AS invalid_votes, int_("Ważne") AS valid_votes
            FROM {districts_src} WHERE int_("Nr woj.") IS NOT NULL
            """
        )
        seat_city, eligible, districts_join = "t.seat_city", "CAST(t.eligible_voters AS INT)", "LEFT JOIN senat_district_totals t USING (district_no)"
    else:
        seat_city = "k.seat_city" if year == 1991 else "CAST(NULL AS VARCHAR)"
        eligible, districts_join = "CAST(NULL AS INT)", ""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_districts AS
        SELECT {year} AS year, district_no, {seat_city} AS seat_city, {eligible} AS eligible_voters,
               'województwo ' || k.name AS description
        FROM (SELECT CAST(int_("Nr woj.") AS INT) AS district_no, any_value(trim("Województwo")) AS name,
                     {'any_value(trim("Siedziba"))' if year == 1991 else "NULL"} AS seat_city
              FROM {src} GROUP BY 1) k
        {districts_join}
        """
    )
    official = OFFICIAL_SENAT_1990S[year]
    if "results" not in spec:
        con.execute("CREATE OR REPLACE TABLE senat_precinct_protocols (year INT, precinct_id INT, eligible_voters INT, "
                    "ballots_issued INT, valid_ballots INT, invalid_votes INT, valid_votes INT)")
        con.execute("CREATE OR REPLACE TABLE senat_precinct_candidate_votes (year INT, precinct_id INT, candidate_id INT, votes INT)")
        _check(con, year, precinct_results=False)
        return

    # Sort the per-voivodeship files: results with protocol, results without protocol, register copies.
    keys_by_district: dict[int, set[str]] = {}
    for district, name in con.execute("SELECT district_no, full_name FROM senat_candidates").fetchall():
        keys_by_district.setdefault(district, set()).add(python_name_key(name))
    register_columns = {"Nr okr.", "Nr woj.", "Kod gm.", "Kod gminy", "Nazwa gm.", "Gmina", "Nr obw.", "Kod obw.", "Adres",
                        "Adres komisji obwodowej", "Typ", "Upr.", "Odd.", "Niew.", "Ważne", "year"}
    results, register_copies, without_protocol = {}, {}, set()
    con.execute("CREATE OR REPLACE TABLE senat_unknown_columns (district_no INT, header VARCHAR)")
    for district, paths in _files_by_district(sources.files(spec["results"]), r"^(?:sen1993obw|OBWSEN)(\d+)__").items():
        for path in paths:
            cols = columns_of(con, f"read_parquet({sql_literal(path)})")
            candidates = parse_plain_names(cols, keys_by_district.get(district, set()))
            if not candidates:
                if year != 1993 or district not in SENATE_REGISTER_COPY_1993 or district in register_copies:
                    raise BuildError(f"year {year}: Senate file {path} has no candidate columns")
                register_copies[district] = path
                continue
            if district in results:
                raise BuildError(f"year {year}: two Senate result files for district {district}")
            results[district] = path
            if "Ważne" not in cols:
                without_protocol.add(district)
            for header in (c for c in cols if c not in register_columns and c not in candidates):
                con.execute("INSERT INTO senat_unknown_columns VALUES (?, ?)", [district, header])
    if without_protocol != (SENATE_NO_PROTOCOL_1993 if year == 1993 else set()):
        raise BuildError(f"year {year}: Senate files without protocol columns: {sorted(without_protocol)}")
    if set(register_copies) != (SENATE_REGISTER_COPY_1993 if year == 1993 else set()):
        raise BuildError(f"year {year}: Senate register copies: {sorted(register_copies)}")

    parts = []
    for district, path in results.items():
        source = f"read_parquet({sql_literal(path)})"
        code = column(con, source, "Kod gm.", "Kod gminy")
        if district in without_protocol:
            protocol = "NULL, NULL, NULL, NULL, NULL"
        else:
            invalid = 'int_("Niew.")' if column(con, source, "Niew.", required=False) else 'int_("Odd.") - int_("Ważne")'
            protocol = f'int_("Upr."), NULL, int_("Odd."), {invalid}, int_("Ważne")'
        parts.append(f"""SELECT {district} AS district_no, CAST(int_("Nr woj.") AS INT) AS file_district, pkey({code}, "Nr obw.") AS pkey,
                                trim({code}) AS code, int_("Upr.") AS register_eligible, {protocol} FROM {source}""")
    con.execute(
        "CREATE OR REPLACE TABLE senat_protocols_raw (district_no INT, file_district INT, pkey VARCHAR, code VARCHAR, "
        "register_eligible BIGINT, eligible_voters BIGINT, ballots_issued BIGINT, valid_ballots BIGINT, invalid_votes BIGINT, "
        "valid_votes BIGINT)"
    )
    con.execute("INSERT INTO senat_protocols_raw " + " UNION ALL ".join(parts))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_precinct_protocols AS
        SELECT {year} AS year, p.precinct_id, CAST(r.eligible_voters AS INT) AS eligible_voters,
               CAST(r.ballots_issued AS INT) AS ballots_issued, CAST(r.valid_ballots AS INT) AS valid_ballots,
               CAST(r.invalid_votes AS INT) AS invalid_votes, CAST(r.valid_votes AS INT) AS valid_votes
        FROM senat_protocols_raw r JOIN precincts p USING (pkey)
        """
    )
    files = map_candidate_columns(
        con, files_by_district=results, index=candidate_index(con, "senat_candidates", None),
        parse=lambda cols, district: parse_plain_names(cols, keys_by_district.get(district, set())),
        unmapped_table="senat_candidate_columns_unmapped",
    )
    build_wide_votes(
        con, year=year, target="senat_precinct_candidate_votes", key_name="candidate_id", files=files,
        teryt_aliases=("Kod gm.", "Kod gminy"), number_aliases=("Nr obw.",),
    )

    def total(name: str) -> str:
        return f"(SELECT coalesce(sum({name}), 0) FROM senat_precinct_protocols)"

    missing = official.get("eligible_voters_missing", 0)
    checks = {
        **_register_checks(con, year),
        "precincts with more than one Senate protocol": "SELECT count(*) - count(DISTINCT pkey) FROM senat_protocols_raw",
        "Senate protocol rows of unknown precincts": "SELECT count(*) FROM senat_protocols_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
        "precincts without a Senate protocol row": """
            SELECT count(*) FROM precincts WHERE precinct_id NOT IN (SELECT precinct_id FROM senat_precinct_protocols)""",
        "Senate rows whose voivodeship differs from their file or from the precinct's Senate district": """
            SELECT count(*) FROM senat_protocols_raw r JOIN precincts p USING (pkey)
            WHERE r.file_district <> r.district_no OR p.senat_district_no <> r.district_no""",
        "Senate result rows of unknown precincts": "SELECT count(*) FROM senat_precinct_candidate_votes_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
        "Senate file columns that are neither protocol nor a candidate of the district": "SELECT count(*) FROM senat_unknown_columns",
        "Senate candidate columns that match no candidate": "SELECT count(*) FROM senat_candidate_columns_unmapped",
        "Senate candidates not read from exactly one column": """
            SELECT count(*) FROM senat_candidates k
            WHERE (SELECT count(*) FROM senat_precinct_candidate_votes_columns m WHERE m.key = k.candidate_id) <> 1""",
        "Senate candidates whose precinct votes differ from the official total (all districts)": """
            SELECT count(*) FROM senat_candidates k
            LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM senat_precinct_candidate_votes GROUP BY ALL) s USING (candidate_id)
            WHERE coalesce(s.votes, 0) <> k.votes""",
        "Senate precincts with votes cast != valid + invalid": """
            SELECT count(*) FROM senat_precinct_protocols WHERE valid_ballots <> valid_votes + invalid_votes""",
        f"Senate precincts without a protocol outside districts {sorted(without_protocol)}": f"""
            SELECT count(*) FROM senat_protocols_raw
            WHERE valid_votes IS NULL AND district_no NOT IN ({", ".join(map(str, without_protocol)) or "NULL"})""",
    }
    if year == 1997:
        checks |= {
            "Senate eligible voters differ from PKW's announcement": f"SELECT abs({total('eligible_voters')} - {official['eligible_voters']})",
            "Senate votes cast differ from PKW's announcement": f"SELECT abs({total('valid_ballots')} - {official['votes_cast']})",
            "Senate invalid votes differ from PKW's announcement": f"SELECT abs({total('invalid_votes')} - {official['invalid_votes']})",
            "Senate valid ballots differ from PKW's announcement": f"SELECT abs({total('valid_votes')} - {official['valid_votes']})",
            "Senate districts whose precinct protocols differ from the district table": """
                SELECT count(*) FROM senat_district_totals d
                LEFT JOIN (SELECT p.senat_district_no AS district_no, count(*) AS precincts, sum(r.eligible_voters) AS eligible_voters,
                                  sum(r.valid_ballots) AS valid_ballots, sum(r.invalid_votes) AS invalid_votes, sum(r.valid_votes) AS valid_votes
                           FROM senat_precinct_protocols r JOIN precincts p USING (precinct_id) GROUP BY 1) s USING (district_no)
                WHERE (s.precincts, s.eligible_voters, s.valid_ballots, s.invalid_votes, s.valid_votes)
                      IS DISTINCT FROM (d.precincts, d.eligible_voters, d.valid_ballots, d.invalid_votes, d.valid_votes)""",
            "Senate districts whose seats differ from the district table": """
                SELECT count(*) FROM senat_district_seats s FULL JOIN senat_district_totals d USING (district_no)
                WHERE s.seats IS DISTINCT FROM d.seats""",
            "Senate candidates whose district valid ballots differ from the district table": f"""
                SELECT count(*) FROM {src} k JOIN senat_district_totals d ON d.district_no = int_(k."Nr woj.")
                WHERE int_(k."Ważne w woj.") <> d.valid_votes""",
        }
    else:
        # 1993: protocol figures of SENATE_NO_PROTOCOL_1993 come from the gmina file (Senate columns).
        gminas_src = sources.load("senat_gminas", spec["gminas"])
        con.execute(
            f"""
            CREATE OR REPLACE TABLE senat_gmina_totals AS
            SELECT left(trim("TERYT"), 5) AS code, CAST(int_("Nr woj..") AS INT) AS district_no,
                   int_("Liczba uprawnionych") AS eligible_voters, int_("Głosy oddane - Senat") AS valid_ballots,
                   int_("Głosy nieważne - Senat") AS invalid_votes, int_("Głosy ważne - Senat") AS valid_votes
            FROM {gminas_src} WHERE int_("Id") IS NOT NULL
            """
        )
        gap = f"(SELECT sum({{0}}) FROM senat_gmina_totals WHERE district_no IN ({', '.join(map(str, without_protocol))}))"
        checks |= {
            f"Senate eligible voters differ from PKW's announcement (except {missing} missing as in the Sejm files, "
            f"districts {sorted(without_protocol)} from the gmina file)": f"""
                SELECT abs({total('eligible_voters')} + {gap.format('eligible_voters')} + {missing} - {official['eligible_voters']})""",
            **{
                f"Senate {label} differ from PKW's announcement (districts {sorted(without_protocol)} from the gmina file)": f"""
                    SELECT abs({total(name)} + {gap.format(name)} - {official[key]})"""
                for label, name, key in (("votes cast", "valid_ballots", "votes_cast"), ("invalid votes", "invalid_votes", "invalid_votes"),
                                         ("valid ballots", "valid_votes", "valid_votes"))
            },
            "gminas whose Senate precinct protocols differ from the gmina file": """
                SELECT count(*) FROM senat_gmina_totals g
                FULL JOIN (SELECT code, any_value(district_no) AS district_no, sum(register_eligible) AS register_eligible,
                                  sum(eligible_voters) AS eligible_voters, sum(valid_ballots) AS valid_ballots,
                                  sum(invalid_votes) AS invalid_votes, sum(valid_votes) AS valid_votes
                           FROM senat_protocols_raw GROUP BY 1) s USING (code)
                WHERE g.eligible_voters IS DISTINCT FROM s.register_eligible
                   OR (s.valid_votes IS NOT NULL AND (g.eligible_voters, g.valid_ballots, g.invalid_votes, g.valid_votes)
                       IS DISTINCT FROM (s.eligible_voters, s.valid_ballots, s.invalid_votes, s.valid_votes))""",
        }
        for district, path in register_copies.items():
            source = f"read_parquet({sql_literal(path)})"
            checks[f"Senate district {district} register copy rows that differ from its results file"] = f"""
                SELECT count(*) FROM (SELECT pkey("Kod gm.", "Nr obw.") AS pkey, int_("Upr.") AS eligible FROM {source}) c
                FULL JOIN (SELECT pkey, register_eligible AS eligible FROM senat_protocols_raw WHERE district_no = {district}) r USING (pkey)
                WHERE c.eligible IS DISTINCT FROM r.eligible"""
    run_checks(con, year, checks)


def _files_by_district(paths: list[str], pattern: str) -> dict[int, list[str]]:
    """Like ``district_files``, but keeps every file of a district (1993: a district published twice)."""
    out: dict[int, list[str]] = {}
    for path in sorted(paths):
        match = re.search(pattern, Path(path).name)
        if not match:
            raise BuildError(f"cannot read the district number from {path}")
        out.setdefault(int(match.group(1)), []).append(path)
    return out


def _check(con: duckdb.DuckDBPyConnection, year: int, precinct_results: bool = True) -> None:
    """Fail the build unless Senate precinct votes reproduce the official candidate totals.

    Without per-precinct results (2005) only the registers are checked.
    """
    if not precinct_results:
        run_checks(con, year, _register_checks(con, year))
        return
    for district, address in con.execute("SELECT district_no, address FROM senat_rows_without_number").fetchall():
        log.warning(
            "year %s: Senate district %s file has a combined row %r without precinct number; its votes count towards "
            "the candidate totals, its precincts have no Senate protocol", year, district, address.strip(),
        )
    unpublished = "(SELECT p.senat_district_no FROM senat_precinct_protocols r JOIN precincts p USING (precinct_id) WHERE r.valid_votes IS NULL)"
    run_checks(
        con,
        year,
        {
            **_register_checks(con, year),
            "precincts with more than one published Senate protocol": """
                SELECT count(*) FROM (SELECT pkey FROM senat_protocols_raw WHERE valid_votes IS NOT NULL GROUP BY 1 HAVING count(*) > 1)""",
            "Senate protocol rows of unknown precincts": "SELECT count(*) FROM senat_protocols_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
            "Senate result rows of unknown precincts": "SELECT count(*) FROM senat_precinct_candidate_votes_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
            "Senate candidate columns that match no candidate": "SELECT count(*) FROM senat_candidate_columns_unmapped",
            "Senate rows with values right of the last candidate, other than SHIFTED_ROWS_2001": """
                SELECT count(*) FROM senat_misaligned_rows""",
            "Senate rows without a precinct number, other than combined military precincts": f"""
                SELECT count(*) FROM senat_rows_without_number WHERE address NOT LIKE '{COMBINED_PRECINCTS}'""",
            "Senate candidates not read from exactly one column": """
                SELECT count(*) FROM senat_candidates k
                WHERE (SELECT count(*) FROM senat_precinct_candidate_votes_columns m WHERE m.key = k.candidate_id) <> 1""",
            # Votes of a combined row (2011) belong to no precinct but are part of the official total.
            "Senate candidates whose precinct votes (plus combined rows) differ from the official total": f"""
                SELECT count(*) FROM senat_candidates k
                LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM senat_precinct_candidate_votes GROUP BY ALL) s
                  USING (candidate_id)
                LEFT JOIN senat_combined_candidate_votes c USING (candidate_id)
                WHERE coalesce(s.votes, 0) + coalesce(c.votes, 0) <> k.votes AND k.district_no NOT IN {unpublished}""",
        },
    )


def _register_checks(con: duckdb.DuckDBPyConnection, year: int) -> dict[str, str]:
    """District and seat checks; with a district seat table (2005/2007) seats are checked per district."""
    districts = DISTRICTS.get(year, 100)
    checks = {
        f"Senate districts != {districts}": f"SELECT abs(count(*) - {districts}) FROM senat_districts",
        "elected senators != 100": "SELECT abs(count(*) FILTER (WHERE elected) - 100) FROM senat_candidates",
    }
    if year in SOURCES_1990S:  # the only registers that publish the age
        checks["Senate candidates without a published age"] = "SELECT count(*) FROM senat_candidates WHERE age IS NULL"
        checks[f"Senate candidates younger than {MIN_CANDIDATE_AGE_1990S} or older than 100"] = f"""
            SELECT count(*) FROM senat_candidates WHERE age < {MIN_CANDIDATE_AGE_1990S} OR age > 100"""
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'senat_district_seats'").fetchone()[0]:
        checks["Senate seats in districts != 100"] = "SELECT abs(sum(seats) - 100) FROM senat_district_seats"
        checks["Senate districts whose elected candidates differ from their seats"] = """
            SELECT count(*) FROM senat_district_seats d
            WHERE d.seats <> (SELECT count(*) FILTER (WHERE elected) FROM senat_candidates k WHERE k.district_no = d.district_no)"""
    return checks
