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
- 2005/2007: ``kandsen<year>`` (candidates with official votes), the district table
  (``senat2005okręgi`` / ``okr2007senat``: seat city, seats, boundaries) and, in 2007,
  ``parl2007-sen-gl_kand_obw/sen2007obw-NN`` (protocol and ``'N. Surname Given (T)'`` candidate
  columns). 40 districts elected 2-4 senators each. 2005 publishes candidate votes per powiat only
  (``2005-sen-gl_kan_pow/SNT_NN_g_p``): they are checked against the official totals, and the Senate
  precinct tables stay empty for 2005.
Requires the ``precincts`` table built by ``build_sejm`` in the same DuckDB connection.
"""

from __future__ import annotations

import logging
import re

import duckdb

from api.services.mart.build_common import (
    PATRONYMIC,
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
    sql_literal,
)
from api.services.mart.build_precincts import COMBINED_PRECINCTS, rows_without_number

log = logging.getLogger("mart")

CANDIDATE_RESULTS = "**/wyniki_gl_na_kand*_po_obwodach_senat_csv/*.parquet"
CANDIDATES_2011 = "**/sejmsenat/kandydaci_senat/*.parquet"
RESULTS_2011 = "**/sejmsenat/*-senat-głosy-w-obwodach/*.parquet"
PROTOCOLS = "**/protokoly_po_obwodach_senat_csv/*.parquet"
EXCEL_CANDIDATES = "**/senat/2015-kand-sen.parquet"
EXCEL_RESULTS = "**/senat/wyniki_okr_senat/*.parquet"
CANDIDATES_2005 = "**/senat/kandsen*.parquet"
DISTRICTS_2005 = "**/senat/*okr*.parquet"
RESULTS_2007 = "**/senat/parl*-sen-gl_kand_obw/*.parquet"
POWIAT_RESULTS_2005 = "**/senat/*-sen-gl_kan_pow/*.parquet"
# Senate districts per year where not 100 single-member districts (2005/2007: 40 districts, 2-4 seats).
DISTRICTS = {2005: 40, 2007: 40}

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
TERYT_ALIASES = ("TERYT Gminy", "Kod TERYT", "TERYT gminy", "Kod terytorialny gminy", "TERYT")
NUMBER_ALIASES = ("Nr komisji", "Numer", "Numer obwodu", "Nr obwodu głosowania", "Nr obwodu")


def available(sources: YearSources) -> bool:
    """Whether the year has staged Senate candidates and per-precinct results."""
    csv = sources.has("**/kandydaci_senat_csv/*.parquet") and sources.has(CANDIDATE_RESULTS)
    layout_2011 = sources.has(CANDIDATES_2011) and sources.has(RESULTS_2011)
    layout_2005 = sources.has(CANDIDATES_2005) and (sources.has(RESULTS_2007) or sources.has(POWIAT_RESULTS_2005))
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
               yes_(k."Mandat") AS elected
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
               yes_(k.{elected}) AS elected
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
    """2005/2007: candidates from ``kandsen<year>`` and districts from the district table.

    ``senat_district_seats`` keeps the seats per district for the checks (not a mart column).
    2005 gives each candidate's district valid votes ('Głos w okr.'), 2007 the percentage.
    """
    candidates_src = sources.load("senat_candidates", CANDIDATES_2005)
    districts_src = sources.load("senat_districts", DISTRICTS_2005)
    if district_votes := column(con, candidates_src, "Głos w okr.", required=False):
        pct = f'CAST(round(100.0 * int_(k."Głosy") / NULLIF(int_(k.{district_votes}), 0), 2) AS DECIMAL(6, 2))'
    else:
        pct = pct_expr(con, candidates_src, '"%"')
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k."Okręg"), int_(k."Nr poz.")) AS INT) AS candidate_id,
               CAST(int_(k."Okręg") AS INT) AS district_no,
               strip_signature(k."Komitet wyborczy") AS committee_name,
               trim(k."Imiona") || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name, trim(k."Imiona") AS first_names,
               gender_(k."Płeć") AS gender, k."Zawód" AS occupation, k."Miejsce zamieszkania" AS residence,
               CAST(NULL AS VARCHAR) AS residence_teryt, CAST(NULL AS VARCHAR) AS party_membership,
               CAST(NULL AS VARCHAR) AS party_support,
               int_(k."Głosy") AS votes, {pct} AS pct_of_district, yes_(k."Mandat") AS elected
        FROM {candidates_src} k
        """
    )
    number = column(con, districts_src, "Numer okręgu", "Numer okręgu -")
    # 'Liczba' spans four columns (powiats, gminas, precincts, seats); seats is the last one.
    seats = column(con, districts_src, "mandatów", "column7")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE senat_districts AS
        SELECT {year} AS year, CAST(int_(d.{number}) AS INT) AS district_no,
               d.{column(con, districts_src, "Siedziba Okręgowej Komisji Wyborczej", "Siedziba Okręgowej Komisji Wyborczej -")} AS seat_city,
               CAST(NULL AS INT) AS eligible_voters,
               regexp_replace(d.{column(con, districts_src, "Obszar okręgu wyborczego", "Obszar okręgu wyborczego -")}, '\\s+', ' ', 'g')
                 AS description
        FROM {districts_src} d WHERE int_(d.{number}) IS NOT NULL
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


def build_senat(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check all Senate tables of ``sources.year``."""
    year = sources.year
    if sources.has(CANDIDATES_2005):
        _build_2005_registers(con, year, sources)
        if not sources.has(RESULTS_2007):
            # 2005: no per-precinct results; empty precinct tables, candidate totals checked per powiat.
            _check_2005_powiats(con, year, sources)
            con.execute("CREATE OR REPLACE TABLE senat_precinct_protocols (year INT, precinct_id INT, eligible_voters INT, "
                        "ballots_issued INT, valid_ballots INT, invalid_votes INT, valid_votes INT)")
            con.execute("CREATE OR REPLACE TABLE senat_precinct_candidate_votes (year INT, precinct_id INT, candidate_id INT, votes INT)")
            _check(con, year, precinct_results=False)
            return
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
    if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'senat_district_seats'").fetchone()[0]:
        checks["Senate seats in districts != 100"] = "SELECT abs(sum(seats) - 100) FROM senat_district_seats"
        checks["Senate districts whose elected candidates differ from their seats"] = """
            SELECT count(*) FROM senat_district_seats d
            WHERE d.seats <> (SELECT count(*) FILTER (WHERE elected) FROM senat_candidates k WHERE k.district_no = d.district_no)"""
    return checks
