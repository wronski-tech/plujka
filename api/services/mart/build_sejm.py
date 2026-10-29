"""Build the Sejm tables of one election year in DuckDB (loader side).

Three source layouts are supported:
- CSV layout (2019, 2023): ``wyniki_gl_na_listy_po_obwodach_sejm`` (precincts, protocol, list votes),
  ``wyniki_gl_na_kand*_po_obwodach_sejm`` (one wide file per district), ``kandydaci_sejm``,
  ``wykaz_list_sejm``, ``okregi_sejm`` and ``obwody_glosowania``.
- 2015 Excel layout: ``wyniki_okr_sejm/NN.xlsx`` (per district: precincts, protocol, list totals and
  candidate votes), ``2015-kand-sejm.xls``, ``obwody2015parl.xlsx`` and ``2015-gl-lis-pow`` (powiat names).
- 2011 layout: one precinct file with list votes, one candidate file per district and registers
  (see ``_build_2011_layout``).
- 2005/2007 layout: one precinct file with list votes, the candidate register with official votes,
  the district table and, in 2007 only, one candidate file per district and list (see
  ``_build_2005_layout``). 2005 has no per-precinct candidate results.

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
    sql_literal,
)
from api.services.mart.build_precincts import (
    COMBINED_PRECINCTS,
    PRECINCT_TYPES_2005,
    create_warsaw_districts,
    rows_without_number,
)

log = logging.getLogger("mart")

CSV_LIST_RESULTS = "**/wyniki_gl_na_listy_po_obwodach_sejm_csv/*.parquet"
CSV_CANDIDATE_RESULTS = "**/wyniki_gl_na_kand*_po_obwodach_sejm_csv/*.parquet"
EXCEL_DISTRICT_RESULTS = "**/sejm/wyniki_okr_sejm/*.parquet"
LIST_RESULTS_2011 = "**/sejmsenat/*-sejm-gl-lis-obw.parquet"
CANDIDATE_RESULTS_2011 = "**/sejmsenat/*-sejm-kand-obw/*.parquet"
LIST_RESULTS_2005 = "**/sejm/sejm*-lis-obw/*.parquet"
LIST_RESULTS_2007 = "**/sejm/sejm*-obw-listy.parquet"
CANDIDATE_RESULTS_2007 = "**/sejm/sejm*-kand-obw/*.parquet"
# Senate district of each precinct: 2005 per gmina, 2007 from the Senate per-precinct files.
SENATE_GMINAS_2005 = "**/*-parl-frekw-gm.parquet"
SENATE_RESULTS_2007 = "**/senat/parl*-sen-gl_kand_obw/*.parquet"

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

# TERYT 'rodzaj gminy' codes used by the 2011 precinct file ('Teren obw.'), worded like the 2015 register.
AREA_TYPES_2011 = {
    "1": "gmina miejska", "2": "gmina wiejska", "4": "miasto w gminie miejsko-wiejskiej",
    "5": "obszar wiejski w gminie miejsko-wiejskiej", "Z": "zagranica", "S": "statek",
    "8": "dzielnica w m.st. Warszawa",  # 2007; 2005 gives the district letter instead (WARSAW_DISTRICTS_2005)
}


def available_layout(sources: YearSources) -> str | None:
    """'csv', 'excel', '2011', '2005' or None when the year has no staged Sejm precinct results."""
    if sources.has(CSV_LIST_RESULTS):
        return "csv"
    if sources.has(EXCEL_DISTRICT_RESULTS):
        return "excel"
    if sources.has(LIST_RESULTS_2011):
        return "2011"
    if sources.has(LIST_RESULTS_2005) or sources.has(LIST_RESULTS_2007):
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
    else:
        _build_2005_layout(con, sources)
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
    # 2005 publishes no per-precinct candidate results.
    _check(con, sources.year, candidate_precincts=not sources.has(LIST_RESULTS_2005))


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
    """2005 and 2007 KBW workbooks.

    ``sejm2005-lis-obw`` / ``sejm2007-obw-listy`` (precincts, protocol, list votes as ``'<list no> -
    <name>'``), ``kandsejm<year>`` (candidates with official votes), ``okregi2005sejm`` /
    ``okręgi2007sejm`` (seat city, seats, boundaries). Official list votes per district come from the
    register's ``'Gł. na listę'`` (2005) or the gmina table ``sejm2007-gm-listy`` (2007). Only 2007 has
    per-precinct candidate votes: ``sejm2007-kand-obw/kandsejmobw-2007-DD-LL`` (district DD, list LL).
    List numbers are national, except that in 2005 number 19 belongs to a different committee in each
    district that has one, so list columns are mapped per (district, list number).
    """
    year = sources.year
    results = sources.load("sejm_results", LIST_RESULTS_2005 if sources.has(LIST_RESULTS_2005) else LIST_RESULTS_2007)
    candidates_src = sources.load("sejm_candidates", "**/sejm/kandsejm*.parquet")
    districts_src = sources.load("districts", "**/sejm/okr*sejm.parquet")
    create_warsaw_districts(con)
    r_district = column(con, results, "Nr okręgu", "Okręg")
    r_teryt = column(con, results, "TERYT", "Kod gm.")
    r_number = column(con, results, "Numer obwodu", "Nr obw.")
    r_area = column(con, results, "7-ma cyfra TERYT", "Teren")
    r_type = column(con, results, "Typ obwodu", "Typ")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE src_sejm_precincts AS
        SELECT r.*, lpad(trim(r.{r_teryt}), 6, '0') AS teryt_raw, coalesce(w.teryt, lpad(trim(r.{r_teryt}), 6, '0')) AS teryt_fixed,
               coalesce(w.gmina, r."Gmina") AS gmina_name, w.letter IS NOT NULL AS warsaw_letter
        FROM {results} r
        LEFT JOIN warsaw_districts_2005 w ON lpad(trim(r.{r_teryt}), 6, '0') = '146501' AND w.letter = trim(r.{r_area})
        """
    )
    # Senate district of each precinct: per gmina in 2005, from the Senate per-precinct files in 2007.
    con.execute("CREATE OR REPLACE TABLE senate_district_of (key VARCHAR, senat_district_no INT)")
    senate_key = "NULL"
    if sources.has(SENATE_GMINAS_2005):
        gminas = sources.load("senate_gminas", SENATE_GMINAS_2005)
        con.execute(f"""INSERT INTO senate_district_of SELECT lpad(trim("TERYT"), 6, '0'), int_("Nr okr. - Senat") FROM {gminas}""")
        senate_key = "r.teryt_raw"
    elif sources.has(SENATE_RESULTS_2007):
        for district, path in district_files(sources.files(SENATE_RESULTS_2007), r"obw-(\d+)__").items():
            source = f"read_parquet({sql_literal(path)}, hive_partitioning = false)"
            con.execute(
                f"INSERT INTO senate_district_of SELECT DISTINCT pkey({column(con, source, 'TERYT')}, {column(con, source, 'Nr obwodu')}), "
                f"{district} FROM {source}"
            )
        senate_key = f"pkey(r.teryt_fixed, r.{r_number})"
    area_cases = " ".join(f"WHEN {sql_literal(code)} THEN {sql_literal(name)}" for code, name in AREA_TYPES_2011.items())
    type_cases = " ".join(f"WHEN {sql_literal(code)} THEN {sql_literal(name)}" for code, name in PRECINCT_TYPES_2005.items())
    con.execute(
        f"""
        CREATE OR REPLACE TABLE precincts AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY clean_teryt(r.teryt_fixed) NULLS LAST, r.gmina_name, int_(r.{r_number})) AS INT) AS precinct_id,
               pkey(r.teryt_fixed, r.{r_number}) AS pkey,
               clean_teryt(r.teryt_fixed) AS teryt_gmina,
               r.gmina_name AS gmina,
               CASE WHEN clean_teryt(r.teryt_fixed) IS NOT NULL THEN r."Powiat" END AS powiat,
               voivodeship(clean_teryt(r.teryt_fixed)) AS wojewodztwo,
               CAST(int_(r.{r_district}) AS INT) AS sejm_district_no,
               s.senat_district_no,
               CAST(int_(r.{r_number}) AS INT) AS precinct_no,
               r.{column(con, results, "Adres obwodu", "Adres")} AS address,
               CASE trim(r.{r_type}) {type_cases} END AS precinct_type,
               CASE WHEN r.warsaw_letter THEN {sql_literal(AREA_TYPES_2011["8"])} ELSE CASE trim(r.{r_area}) {area_cases} END END
                 AS area_type,
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
        FROM src_sejm_precincts r JOIN precincts p ON p.pkey = pkey(r.teryt_fixed, r.{r_number})
        """
    )

    k_district = column(con, candidates_src, "Nr okr.", "Okręg")
    k_position = column(con, candidates_src, "Pozycja", "Nr kand.")
    k_party = column(con, candidates_src, "Partia", required=False)
    k_support = column(con, candidates_src, "Poparcie", required=False)
    # 2005 publishes the list and district totals next to each candidate, 2007 the percentages.
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
        f'SELECT strip_signature("Komitet wyborczy") AS committee_name, min(int_("Nr listy")) AS min_list FROM {candidates_src} GROUP BY 1',
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY int_(k.{k_district}), int_(k."Nr listy"), int_(k.{k_position})) AS INT) AS candidate_id,
               CAST(int_(k.{k_district}) AS INT) AS district_no,
               c.committee_id,
               CAST(int_(k."Nr listy") AS INT) AS list_no,
               CAST(int_(k.{k_position}) AS INT) AS list_position,
               trim(k."Imiona") || ' ' || upper(trim(k."Nazwisko")) AS full_name,
               upper(trim(k."Nazwisko")) AS last_name,
               trim(k."Imiona") AS first_names,
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
            SELECT count(*) FROM (SELECT 1 FROM {candidates_src} GROUP BY int_({k_district}), int_("Nr listy")
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
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejm_districts AS
        SELECT {year} AS year,
               CAST(int_(d."Numer okręgu") AS INT) AS district_no,
               d."Siedziba Okręgowej Komisji Wyborczej" AS seat_city,
               w.wojewodztwo,
               -- 'Liczba' spans four columns (powiats, gminas, precincts, seats); seats is the unnamed last one.
               CAST(int_(d."column7") AS INT) AS seats,
               CAST(NULL AS INT) AS eligible_voters,
               regexp_replace(d."Obszar okręgu wyborczego", '\\s+', ' ', 'g') AS description
        FROM {districts_src} d
        LEFT JOIN {_districts_wojewodztwo(con)} w ON w.sejm_district_no = int_(d."Numer okręgu")
        WHERE int_(d."Numer okręgu") IS NOT NULL
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
        FROM (UNPIVOT (SELECT CAST(int_({r_district}) AS INT) AS district_no, pkey(teryt_fixed, {r_number}) AS pkey, {cols}
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
    checks = {
        "Sejm districts != 41": "SELECT abs(count(*) - 41) FROM sejm_districts",
        "district lists without an official vote total": "SELECT count(*) FROM sejm_district_lists WHERE votes IS NULL",
        "Sejm seats in districts != 460": "SELECT abs(sum(seats) - 460) FROM sejm_districts",
        "elected Sejm candidates != 460": "SELECT abs(count(*) FILTER (WHERE elected) - 460) FROM sejm_candidates",
        "districts whose elected candidates differ from their seats": """
            SELECT count(*) FROM sejm_districts d
            WHERE d.seats <> (SELECT count(*) FILTER (WHERE elected) FROM sejm_candidates k WHERE k.district_no = d.district_no)""",
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
