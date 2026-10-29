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
Requires the ``precincts`` table built by ``build_sejm`` in the same DuckDB connection.
"""

from __future__ import annotations

import logging
import re

import duckdb

from api.services.mart.build_common import (
    PATRONYMIC,
    YearSources,
    build_wide_votes,
    candidate_index,
    column,
    columns_of,
    district_files,
    map_candidate_columns,
    parse_name_dash_label,
    parse_plain_names,
    pct_expr,
    python_name_key,
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
    ),
}
TERYT_ALIASES = ("TERYT Gminy", "Kod TERYT", "TERYT gminy", "Kod terytorialny gminy")
NUMBER_ALIASES = ("Nr komisji", "Numer", "Numer obwodu", "Nr obwodu głosowania")


def available(sources: YearSources) -> bool:
    """Whether the year has staged Senate candidates and per-precinct results."""
    csv = sources.has("**/kandydaci_senat_csv/*.parquet") and sources.has(CANDIDATE_RESULTS)
    layout_2011 = sources.has(CANDIDATES_2011) and sources.has(RESULTS_2011)
    return csv or layout_2011 or (sources.has(EXCEL_CANDIDATES) and sources.has(EXCEL_RESULTS))


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


def build_senat(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check all Senate tables of ``sources.year``."""
    year = sources.year
    if sources.has(EXCEL_CANDIDATES) and not sources.has(CANDIDATE_RESULTS):
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
        protocol = ",\n".join(f"int_({column(con, source, *aliases)}) AS {name}" for name, aliases in PROTOCOL_ALIASES.items())
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
        # 2019 headers are bare names, 2023 headers are 'NAME - KW SHORT'.
        parse=lambda cols, district: parse_plain_names(cols, keys_by_district.get(district, set())) or parse_name_dash_label(cols),
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


def _check(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Fail the build unless Senate precinct votes reproduce the official candidate totals."""
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
            "Senate districts != 100": "SELECT abs(count(*) - 100) FROM senat_districts",
            "elected senators != 100": "SELECT abs(count(*) FILTER (WHERE elected) - 100) FROM senat_candidates",
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
