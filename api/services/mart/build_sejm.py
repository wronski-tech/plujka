"""Build the Sejm tables of one election year in DuckDB (loader side).

Two source layouts are supported:
- CSV layout (2019, 2023): ``wyniki_gl_na_listy_po_obwodach_sejm`` (precincts, protocol, list votes),
  ``wyniki_gl_na_kand*_po_obwodach_sejm`` (one wide file per district), ``kandydaci_sejm``,
  ``wykaz_list_sejm``, ``okregi_sejm`` and ``obwody_glosowania``.
- 2015 Excel layout: ``wyniki_okr_sejm/NN.xlsx`` (per district: precincts, protocol, list totals and
  candidate votes), ``2015-kand-sejm.xls``, ``obwody2015parl.xlsx`` and ``2015-gl-lis-pow`` (powiat names).

Both produce the same DuckDB tables, named like the PostgreSQL mart tables (``precincts`` keeps an
extra ``pkey`` column used to join the per-precinct files).
"""

from __future__ import annotations

import logging
import re

import duckdb

from api.services.mart.build_common import (
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
    parse_list_position,
    parse_name_dash_label,
    pct_expr,
    quote_ident,
    run_checks,
    sql_literal,
)

log = logging.getLogger("mart")

CSV_LIST_RESULTS = "**/wyniki_gl_na_listy_po_obwodach_sejm_csv/*.parquet"
CSV_CANDIDATE_RESULTS = "**/wyniki_gl_na_kand*_po_obwodach_sejm_csv/*.parquet"
EXCEL_DISTRICT_RESULTS = "**/sejm/wyniki_okr_sejm/*.parquet"

PROTOCOL_ALIASES = {
    "eligible_voters": ("Liczba wyborców uprawnionych do głosowania", "Sejm - Liczba wyborców uprawnionych do głosowania"),
    "ballots_issued": (
        "Liczba wyborców, którym wydano karty do głosowania w lokalu wyborczym oraz w głosowaniu korespondencyjnym (łącznie)",
        "Liczba wyborców, którym wydano karty do głosowania",
        "Sejm - Liczba wyborców, którym wydano karty do głosowania",
    ),
    "valid_ballots": ("Liczba kart ważnych", "Sejm - Liczba kart ważnych"),
    "invalid_votes": ("Liczba głosów nieważnych", "Sejm - Liczba głosów nieważnych"),
    "valid_votes": (
        "Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
        "Sejm - Liczba głosów ważnych oddanych łącznie na wszystkie listy kandydatów",
    ),
}


def available_layout(sources: YearSources) -> str | None:
    """'csv', 'excel' or None when the year has no staged Sejm precinct results."""
    if sources.has(CSV_LIST_RESULTS):
        return "csv"
    if sources.has(EXCEL_DISTRICT_RESULTS):
        return "excel"
    return None


def build_sejm(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check all Sejm tables of ``sources.year``."""
    layout = available_layout(sources)
    if layout is None:
        raise BuildError(f"year {sources.year}: no staged Sejm precinct results")
    create_committee_aliases(con)
    if layout == "csv":
        _build_csv_layout(con, sources)
    else:
        _build_excel_layout(con, sources)
    _check(con, sources.year)


def _protocol_select(con: duckdb.DuckDBPyConnection, table: str) -> str:
    """SELECT list of the five protocol columns, resolved against ``table``'s headers."""
    return ",\n".join(f"int_({column(con, table, *aliases)}) AS {name}" for name, aliases in PROTOCOL_ALIASES.items())


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
               trim(regexp_replace({full_name}, '\\s+', ' ', 'g')) AS full_name,
               surname({full_name}) AS last_name,
               given_names({full_name}) AS first_names,
               gender_(k."Płeć") AS gender,
               k."Zawód" AS occupation,
               k."Miejsce zamieszkania" AS residence,
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


# An unmapped result column whose list is known but whose slot (2019: list + position) or name (2015:
# within the list) is absent from the candidate register belongs to a candidate missing from the register.
_NOT_IN_REGISTER = """u.list_no IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM sejm_candidates k
    WHERE k.district_no = u.district_no AND k.list_no = u.list_no
      AND (k.list_position = u.position OR (u.position IS NULL AND name_key(k.full_name) = name_key(u.header))))"""


def _check(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Fail the build unless precinct facts reproduce the official totals."""
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
    unpublished_precincts = "(SELECT precinct_id FROM sejm_precinct_protocols WHERE valid_votes IS NULL)"
    unpublished = "(SELECT p.sejm_district_no FROM sejm_precinct_protocols r JOIN precincts p USING (precinct_id) WHERE r.valid_votes IS NULL)"
    run_checks(
        con,
        year,
        {
            "Sejm districts != 41": "SELECT abs(count(*) - 41) FROM sejm_districts",
            "district lists without an official vote total": "SELECT count(*) FROM sejm_district_lists WHERE votes IS NULL",
            "Sejm seats in districts != 460": "SELECT abs(sum(seats) - 460) FROM sejm_districts",
            "elected Sejm candidates != 460": "SELECT abs(count(*) FILTER (WHERE elected) - 460) FROM sejm_candidates",
            "precincts missing from the precinct register": f"""
                SELECT count(*) FROM precincts WHERE NOT in_register AND precinct_id NOT IN {unpublished_precincts}""",
            "duplicate precinct keys": "SELECT count(*) - count(DISTINCT pkey) FROM precincts",
            "list-result rows of unknown precincts": "SELECT count(*) FROM sejm_precinct_list_votes_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
            "candidate-result rows of unknown precincts": "SELECT count(*) FROM sejm_precinct_candidate_votes_raw WHERE pkey NOT IN (SELECT pkey FROM precincts)",
            "candidate columns that match no candidate": f"""
                SELECT count(*) FROM sejm_candidate_columns_unmapped u WHERE NOT ({_NOT_IN_REGISTER})""",
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
            "candidates whose precinct votes differ from the official total": f"""
                SELECT count(*) FROM sejm_candidates k
                LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM sejm_precinct_candidate_votes GROUP BY ALL) s
                  USING (candidate_id)
                WHERE coalesce(s.votes, 0) <> k.votes AND k.district_no NOT IN {unpublished}""",
        },
    )
