"""Build the local-election tables of one year in DuckDB (loader side): voivodeship assemblies
(sejmiki), mayors (wójt / burmistrz / prezydent miasta, "wbp") and, through ``build_rady``, the
councils of gminas, powiats and Warsaw districts.

Sources (KBW mirror):
- 2010 sejmik: ``2010-kand-rady`` (candidates of every council; ``Szczebel = 'sejmik'``) and
  ``2010-sejmiki-po-gminach`` (full committee names; Łódź is split between two districts and has one
  row per district). There is no district register; the 561 seats are checked against the PKW total.
- 2010 wbp: ``2010-WBP`` (both rounds side by side, like 2014, see ``_WBP_2010``), the per-round gmina
  statistics ``2010-frekw-wbp-gm`` (also side by side) and, for votes against a single candidate, the
  per-precinct files ``wbp2010obw1tura`` / ``wbp2010obw2tura`` (column ``"Przeciw"``).
- 2014 sejmik: ``2014-kand-rady`` (candidates of every council; ``Szczebel = 'województwo'``) and the
  gmina results ``1461143032_a1c2…`` (full committee names, mixed case). There is no district
  register; the 555 seats are checked against the PKW total (``SEJMIK_SEATS``).
- 2014 wbp: ``1579520736_samo2014_kandydaci_na_WBP`` (both rounds side by side; the staged headers of
  the per-round columns are generic, see ``_WBP_2014``) and the per-round gmina statistics in
  ``samo2014-statystyki-według-gmin``.
- 2018 sejmik: ``2018-kand-rady`` (candidates of every council; ``Szczebel = 'województwo'``),
  ``okregi`` (seats), ``2018-sejmiki-po-gminach`` (gmina results; committee columns carry the SHORT
  committee name, mapped through the ``komitety`` register).
- 2024 sejmik: ``kandydaci_sejmiki_wojewodztw``, ``okregi_sejmiki_wojewodztw``,
  ``2024-sejmiki-po-gminach`` (full committee names; cities with powiat rights are missing and are
  taken from ``2024-sejmiki-po-powiatach``, where such a city is its own powiat).
- 2018 wbp: ``2018-kand-wbp`` (both rounds, column ``Tura``) and the per-round gmina protocols
  ``2018-WBP-I-tura-komitety-gminy`` / ``II-tura``.
- 2024 wbp: ``kandydaci_wbp`` / ``kandydaci_wbp2`` and the per-precinct protocols (round 1: one
  file per voivodeship, each with its own candidate columns), aggregated to gminas. Votes against
  a single candidate are only in the protocols (``Głosy przeciw wyborowi kandydata - NAME ...``).
  A candidate who withdrew or was struck off after the ballots were printed keeps a row with NULL
  votes (in a run-off the other candidate then faced a for/against vote).
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
    normalize_header,
    pct_expr,
    python_name_key,
    quote_ident,
    run_checks,
    sql_list,
    sql_literal,
)
from api.services.mart import build_rady
from api.services.mart.build_precincts import GEO_ALIASES, NOT_VOTE_COLUMNS, PROTOCOL_ALIASES
from api.services.mart.schema import RADA_TABLES, SEJMIK_TABLES, WBP_TABLES

log = logging.getLogger("mart")

TABLES = SEJMIK_TABLES + WBP_TABLES + RADA_TABLES
FAMILY = ("sejmik", "wbp", "rada")

# 2010: ten run-offs took place on 19 December instead of 5 December (see LATE_RUNOFFS).
DATES = {
    2010: ("2010-11-21", "2010-12-05"),
    2014: ("2014-11-16", "2014-11-30"),
    2018: ("2018-10-21", "2018-11-04"),
    2024: ("2024-04-07", "2024-04-21"),
}
SOURCES = {
    2010: {
        "sejmik_candidates": "**/samorzad/samo2010/2010-kand-rady.parquet",
        "sejmik_gminas": "**/samorzad/sejmik/2010-sejmiki-po-gminach.parquet",
        "wbp_candidates": "**/samorzad/samo2010/2010-WBP.parquet",
        "wbp_protocols": "**/samorzad/2010-frekw-wbp-gm.parquet",
        "wbp_precincts_1": "**/samorzad/samo2010/wbp/wbp2010obw1tura/*.parquet",
        "wbp_precincts_2": "**/samorzad/samo2010/wbp/wbp2010obw2tura.parquet",
    },
    2014: {
        "sejmik_candidates": "**/samorzad/2014-kand-rady.parquet",
        "sejmik_gminas": "**/samorzad/1461143032_a1c242476ecfd74febac7beef6a12fcb.parquet",
        "wbp_candidates": "**/samorzad/1579520736_samo2014_kandydaci_na_WBP.parquet",
        "wbp_protocols_1": "**/samorzad/samo2014-statystyki-według-gmin/samo2014 WBP I tura w gminach__*.parquet",
        "wbp_protocols_2": "**/samorzad/samo2014-statystyki-według-gmin/samo2014 WBP II tura w gminach__*.parquet",
    },
    2018: {
        "sejmik_candidates": "**/samorzad/2018-kand-rady.parquet",
        "sejmik_seats": "**/samorzad/okregi/*.parquet",
        "sejmik_gminas": "**/samorzad/2018-sejmiki/2018-sejmiki-po-gminach__*.parquet",
        "committees": "**/samorzad/komitety/*.parquet",
        "wbp_candidates": "**/samorzad/2018-wybbp/2018-kand-wbp__*.parquet",
        "wbp_protocols_1": "**/samorzad/2018-wybbp/2018-WBP-I-tura-komitety-gminy__*.parquet",
        "wbp_protocols_2": "**/samorzad/2018-wybbp/2018-WBP-II-tura-komitety-gminy__*.parquet",
    },
    2024: {
        "sejmik_candidates": "**/samorzad/kandydaci_sejmiki_wojewodztw_csv/*.parquet",
        "sejmik_seats": "**/samorzad/okregi_sejmiki_wojewodztw_csv/*.parquet",
        "sejmik_gminas": "**/samorzad/wyniki_glosowania_na_listy_kandydatow_do_sejmikow_wojewodztw/2024-sejmiki-po-gminach__*.parquet",
        "sejmik_precincts": "**/samorzad/protokoly_po_obwodach_sejmik_wojewodztwa_csv/*.parquet",
        "sejmik_powiats": "**/samorzad/wyniki_glosowania_na_listy_kandydatow_do_sejmikow_wojewodztw/2024-sejmiki-po-powiatach__*.parquet",
        "wbp_candidates_1": "**/samorzad/kandydaci_wbp_csv/*.parquet",
        "wbp_candidates_2": "**/samorzad/kandydaci_wbp2_csv/*.parquet",
        "wbp_protocols_1": "**/samorzad/protokoly_po_obwodach_wbp_csv/*.parquet",
        "wbp_protocols_2": "**/samorzad/protokoly_po_obwodach_wbp2_csv/*.parquet",
    },
}
PROTOCOL = ("eligible_voters", "ballots_issued", "valid_ballots", "invalid_votes", "valid_votes")
# 2014 gmina statistics use short headers, 2010 sejmik gmina results long ones ('Liczba głosów
# oddanych' = valid ballots, as in the 2010 protocols); they come after the shared aliases, so later
# years are unaffected.
LOCAL_PROTOCOL_ALIASES = {
    name: PROTOCOL_ALIASES[name] + extra
    for name, extra in {
        "eligible_voters": ("Liczba wyborców",),
        "ballots_issued": ("Wydane karty", "Liczba wydanych kart do głosowania"),
        "valid_ballots": ("Karty ważne", "Liczba głosów oddanych"),
        "invalid_votes": ("Głosy nieważne",),
        "valid_votes": ("Głosy ważne",),
    }.items()
}
# 2010 and 2014 have no sejmik district register: PKW totals of seats ("Wybory samorządowe 2014 -
# statystyki", pkw.gov.pl: 555 radnych województw; wybory2010.pkw.gov.pl/geo/pl/000000.html: 561
# radnych in 87 districts).
SEJMIK_SEATS = {2010: 561, 2014: 555}
# Gminas split between two sejmik districts (one gmina-file row per district): Łódź 2010 (districts
# 1 and 2, PKW "Zbiorcze informacje o okręgach" of łódzkie).
SEJMIK_SPLIT_GMINAS = {2010: ["106101"]}
# 2010 run-offs held on 19 December instead of 5 December ('data gł.' in the candidate file): a
# candidate who qualified withdrew (see _wbp_2010).
LATE_RUNOFFS = {2010: ["020603", "040902", "061403", "120604", "200404", "200802", "201209", "220903", "281501", "321408"]}
OFFICES = {"W": "wójt", "B": "burmistrz", "P": "prezydent miasta"}
_AGAINST = re.compile(r"^Głosy przeciw wyborowi kandydata - (.+?) zarejestrowanego przez ")


def available(sources: YearSources) -> bool:
    """Whether the year has staged sejmik candidates and mayor candidates."""
    spec = SOURCES.get(sources.year)
    if not spec:
        return False
    wbp = spec.get("wbp_candidates") or spec["wbp_candidates_1"]
    return sources.has(spec["sejmik_candidates"]) and sources.has(wbp)


def build_samorzad(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check sejmik, mayor and council tables of ``sources.year``; return the loaded elections."""
    year = sources.year
    # 2018 registers have separate name columns, some rows in capitals: 'WITOLD' -> 'Witold'.
    con.execute("CREATE OR REPLACE MACRO cap_(x) AS CASE WHEN upper(x) = x THEN upper(left(x, 1)) || lower(substr(x, 2)) ELSE x END")
    # 2014 registers have all given names in one column, some in capitals: 'JAN MARIA' -> 'Jan Maria'.
    con.execute(
        "CREATE OR REPLACE MACRO capw_(x) AS CASE WHEN upper(x) = x THEN array_to_string("
        "list_transform(str_split(x, ' '), w -> upper(left(w, 1)) || lower(substr(w, 2))), ' ') ELSE x END"
    )
    _build_sejmik(con, sources)
    _build_wbp(con, sources)
    _check(con, year)
    first, second = DATES[year]
    elections = [
        Election("sejmik", year, 1, first, f"Voivodeship assembly (sejmik) elections {year}"),
        Election("wbp", year, 1, first, f"Mayor (wójt/burmistrz/prezydent miasta) elections {year}, round 1"),
        Election("wbp", year, 2, second, f"Mayor (wójt/burmistrz/prezydent miasta) elections {year}, round 2"),
    ]
    if build_rady.available(sources):
        build_rady.build_rady(con, sources)
        elections.append(Election("rada", year, 1, first, f"Gmina, powiat and Warsaw district council elections {year}"))
    else:
        log.info("year %s: no staged council files, council tables stay empty", year)
    return elections


# --- Voivodeship assemblies -----------------------------------------------------------------


def _build_sejmik(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Sejmik committees, candidates, district lists, gmina protocols and gmina list votes."""
    year = sources.year
    spec = SOURCES[year]
    create_committee_aliases(con)
    raw = sources.load("sejmik_cand", spec["sejmik_candidates"])
    if year == 2010:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw} WHERE \"Szczebel\" = 'sejmik'")
        f = {
            "woj": '"Jednostka"', "district": '"Okręg"', "list_no": '"Nr listy"', "position": '"Pozycja"',
            "name": """concat_ws(' ', capw_("Imiona"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zamieszkania"', "elected": '"Wybrany"', "votes": '"L. głosów"', "gender": '"Plec"',
        }
    elif year == 2014:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw} WHERE \"Szczebel\" = 'województwo'")
        f = {
            "woj": '"Jednostka"', "district": '"Nr okr."', "list_no": '"Nr listy"', "position": '"Poz."',
            "name": """concat_ws(' ', capw_("Imiona"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zam."', "elected": '"Mandat"', "votes": '"Głosy"',
        }
    elif year == 2018:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw} WHERE \"Szczebel\" = 'województwo'")
        f = {
            "woj": '"Jednostka"', "district": '"Nr okr."', "list_no": '"Nr listy"', "position": '"Poz."',
            "name": """concat_ws(' ', cap_("Imię"), cap_("Drugie imię"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zam."', "elected": '"Mandat"', "votes": '"Liczba głosów"',
        }
    else:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw}")
        f = {
            "woj": '"Województwo"', "district": '"Nr okręgu"', "list_no": '"Nr listy"', "position": '"Pozycja na liście"',
            "name": '"Nazwisko i imiona"', "committee": '"Nazwa komitetu"',
            "residence": '"Miejsce zamieszkania"', "elected": '"Czy uzyskał mandat"', "votes": '"Liczba głosów"',
        }
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_committees AS
        WITH names AS (SELECT DISTINCT strip_signature({f['committee']}) AS committee_name FROM sejmik_cand)
        SELECT {year} AS year, CAST(row_number() OVER (ORDER BY committee_name) AS INT) AS committee_id, committee_name,
               coalesce((SELECT r.aliases_pg FROM committee_alias_rules r WHERE committee_name LIKE '%' || r.needle || '%'
                         ORDER BY r.priority LIMIT 1), '{{}}') AS aliases
        FROM names
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY lower(k.{f['woj']}), int_(k.{f['district']}), int_(k.{f['list_no']}),
                                       int_(k.{f['position']})) AS INT) AS candidate_id,
               lower(k.{f['woj']}) AS wojewodztwo, CAST(int_(k.{f['district']}) AS INT) AS district_no, c.committee_id,
               CAST(int_(k.{f['list_no']}) AS INT) AS list_no, CAST(int_(k.{f['position']}) AS INT) AS list_position,
               display_name({f['name']}) AS full_name, surname({f['name']}) AS last_name, given_names({f['name']}) AS first_names,
               gender_(k.{f.get('gender', '"Płeć"')}) AS gender, CAST(int_(k."Wiek") AS INT) AS age, k.{f['residence']} AS residence,
               int_(k.{f['votes']}) AS votes,
               yes_(k.{f['elected']}) AS elected
        FROM sejmik_cand k JOIN sejmik_committees c ON c.committee_name = strip_signature(k.{f['committee']})
        """
    )
    if year in SEJMIK_SEATS:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_seats AS SELECT {SEJMIK_SEATS[year]} AS seats")
    else:
        seats = sources.load("sejmik_seats", spec["sejmik_seats"])
        seat_filter = "WHERE \"Szczebel\" = 'Sejmik województwa'" if year == 2018 else ""
        con.execute(f"CREATE OR REPLACE TABLE sejmik_seats AS SELECT sum(int_(\"Liczba mandatów\")) AS seats FROM {seats} {seat_filter}")

    gminas = sources.load("sejmik_gminas", spec["sejmik_gminas"])
    # 2010/2014 name gminas 'Bolesławiec, m.' (Warsaw districts without suffix); later years 'm. Bolesławiec'.
    gmina_name = (
        f"""regexp_replace({column(con, gminas, "Gmina/dzielnica", "Gmina (dzielnica)")}, '^(.*), (m\\. st\\.|m\\.|gm\\.)$', '\\2 \\1')"""
        if year in (2010, 2014) else '"Gmina"'
    )
    # 2010-2018 gmina files name the district of every row; a gmina split between districts (Łódź
    # 2010) has one row per district. 2024 has the district only per precinct.
    district = f"CAST(int_({column(con, gminas, 'Nr okr.', 'Numer okręgu')}) AS INT)" if year < 2024 else "CAST(NULL AS INT)"
    parts = [(gminas, f"clean_teryt({column(con, gminas, *GEO_ALIASES['teryt'])})", gmina_name, district)]
    if year == 2024:
        # Cities with powiat rights (powiat code 61-99) are only in the powiat file; their gmina
        # TERYT is the powiat code with '01'. Warsaw (1465xx) is already listed by district.
        powiats = sources.load("sejmik_powiats", spec["sejmik_powiats"])
        con.execute(
            f"""CREATE OR REPLACE TABLE sejmik_cities AS SELECT * FROM {powiats}
                WHERE substr(lpad("TERYT Powiatu", 6, '0'), 3, 2) >= '61' AND left(lpad("TERYT Powiatu", 6, '0'), 4) <> '1465'"""
        )
        parts.append(("sejmik_cities", "left(lpad(\"TERYT Powiatu\", 6, '0'), 4) || '01'", "'m. ' || \"Powiat\"", district))
    committee_by_header = _sejmik_header_map(con, sources, gminas)
    selects, list_selects = [], []
    con.execute("CREATE OR REPLACE TABLE sejmik_unmapped (source VARCHAR, header VARCHAR, votes BIGINT)")
    for source, teryt, gmina, district_no in parts:
        protocol = ", ".join(f"CAST(int_({column(con, source, *LOCAL_PROTOCOL_ALIASES[p])}) AS INT) AS {p}" for p in PROTOCOL)
        selects.append(
            f"SELECT {teryt} AS teryt_gmina, {district_no} AS district_no, {gmina} AS gmina, \"Powiat\" AS powiat, {protocol} FROM {source}"
        )
        headers = [h for h in _vote_columns(con, source) if h in committee_by_header]
        cols = ", ".join(quote_ident(h) for h in headers)
        list_selects.append(
            f"SELECT teryt_gmina, district_no, header, votes FROM (UNPIVOT (SELECT {teryt} AS teryt_gmina, "
            f"{district_no} AS district_no, {cols} FROM {source}) ON {cols} INTO NAME header VALUE votes)"
        )
        unmapped = [h for h in _vote_columns(con, source) if h not in committee_by_header]
        for h in unmapped:
            con.execute(
                f"INSERT INTO sejmik_unmapped SELECT ?, ?, coalesce(sum(int_({quote_ident(h)})), 0) FROM {source}", [source, h]
            )
    # One row per gmina: the rows of a gmina split between districts are added up.
    con.execute(f"CREATE OR REPLACE TEMP TABLE sejmik_prot_rows AS {' UNION ALL '.join(selects)}")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_gmina_protocols AS
        SELECT {year} AS year, teryt_gmina, any_value(gmina) AS gmina, any_value(powiat) AS powiat,
               voivodeship(teryt_gmina) AS wojewodztwo, {', '.join(f'CAST(sum({p}) AS INT) AS {p}' for p in PROTOCOL)}
        FROM sejmik_prot_rows
        GROUP BY teryt_gmina
        """
    )
    con.execute("CREATE OR REPLACE TEMP TABLE sejmik_header_map (header VARCHAR, committee_id INT)")
    con.executemany("INSERT INTO sejmik_header_map VALUES (?, ?)", list(committee_by_header.items()))
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE sejmik_row_votes AS
        SELECT u.teryt_gmina, u.district_no, m.committee_id, int_(u.votes) AS votes
        FROM ({' UNION ALL '.join(list_selects)}) u JOIN sejmik_header_map m USING (header)
        WHERE int_(u.votes) IS NOT NULL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_gmina_list_votes AS
        SELECT {year} AS year, teryt_gmina, committee_id, CAST(sum(votes) AS INT) AS votes
        FROM sejmik_row_votes GROUP BY ALL
        """
    )
    # Gmina -> district: sejmik districts consist of whole powiats (and Łódź in 2010 of parts of the
    # city), so every gmina and Warsaw district lies in one district except the named split gminas
    # (see the check). 2010-2018 list it in the gmina file, 2024 only per precinct.
    if year < 2024:
        con.execute(f"""CREATE OR REPLACE TABLE sejmik_gmina_district AS
            SELECT DISTINCT clean_teryt({column(con, gminas, *GEO_ALIASES['teryt'])}) AS teryt_gmina, {district} AS district_no
            FROM {gminas}""")
    else:
        files = sources.files(spec["sejmik_precincts"])
        con.execute(f"""CREATE OR REPLACE TABLE sejmik_gmina_district AS
            SELECT DISTINCT clean_teryt("Teryt Gminy") AS teryt_gmina, CAST(int_("Nr okręgu") AS INT) AS district_no
            FROM read_parquet({sql_list(files)}, union_by_name = true, hive_partitioning = false)""")
    # List votes come from the results (they include votes for struck-off candidates, which count
    # for the list only); seats and list numbers from the candidates.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_district_lists AS
        WITH lists AS (
            SELECT voivodeship(v.teryt_gmina) AS wojewodztwo, coalesce(v.district_no, d.district_no) AS district_no,
                   v.committee_id, sum(v.votes) AS votes
            FROM sejmik_row_votes v
            LEFT JOIN sejmik_gmina_district d ON d.teryt_gmina = v.teryt_gmina AND v.district_no IS NULL
            GROUP BY ALL),
        cands AS (
            SELECT wojewodztwo, district_no, committee_id, min(list_no) AS list_no, sum(coalesce(votes, 0)) AS candidate_votes,
                   CAST(count(*) FILTER (WHERE elected) AS INT) AS seats
            FROM sejmik_candidates GROUP BY ALL)
        SELECT {year} AS year, wojewodztwo, district_no, committee_id, c.list_no, coalesce(l.votes, 0) AS votes,
               coalesce(c.seats, 0) AS seats, c.candidate_votes
        FROM lists l FULL JOIN cands c USING (wojewodztwo, district_no, committee_id)
        """
    )


def _sejmik_header_map(con: duckdb.DuckDBPyConnection, sources: YearSources, gminas: str) -> dict[str, int]:
    """Gmina-file committee column -> committee_id.

    2024 headers are full committee names. 2018 headers are short names (``'KW PRAWO I
    SPRAWIEDLIWOŚĆ'``), translated through the committee register (``Skrót nazwy`` -> ``Nazwa``);
    a short name that maps to several sejmik committees is left unmapped and fails the check.
    """
    by_name = dict(con.execute("SELECT committee_name, committee_id FROM sejmik_committees").fetchall())
    short_to_ids: dict[str, set[int]] = {}
    if sources.year == 2018:
        register = sources.load("committees", SOURCES[2018]["committees"])
        for short, full in con.execute(
            f'SELECT strip_signature("Skrót nazwy"), strip_signature("Nazwa") FROM {register}'
        ).fetchall():
            if full in by_name:
                short_to_ids.setdefault(short, set()).add(by_name[full])
    mapping = {}
    headers = _vote_columns(con, gminas)
    for header in headers:
        # Two committees with the same name (2024: "... RUCH OBRONY POLSKI" in two voivodeships)
        # give a duplicate header that staging suffixes with "_1"; both mean the same name.
        base = re.sub(r"_\d+$", "", header)
        key = con.execute("SELECT strip_signature(?)", [base if base in headers else header]).fetchone()[0]
        if key in by_name:
            mapping[header] = by_name[key]
        elif len(short_to_ids.get(key, ())) == 1:
            mapping[header] = next(iter(short_to_ids[key]))
    return mapping


def _vote_columns(con: duckdb.DuckDBPyConnection, source: str) -> list[str]:
    """Headers after the valid-votes column (committees), without totals; ``vote_columns`` with 2014 headers."""
    columns = columns_of(con, source)
    valid = column(con, source, *LOCAL_PROTOCOL_ALIASES["valid_votes"]).strip('"').replace('""', '"')
    return [c for c in columns[columns.index(valid) + 1 :] if normalize_header(c) not in NOT_VOTE_COLUMNS]


# --- Mayors -----------------------------------------------------------------------------------


def _build_wbp(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Mayor candidates and gmina protocols of both rounds."""
    year = sources.year
    spec = SOURCES[year]
    if year == 2010:
        _wbp_2010(con, sources)
    elif year == 2014:
        _wbp_2014(con, sources)
    elif year == 2018:
        raw = sources.load("wbp_cand", spec["wbp_candidates"])
        con.execute(
            f"""
            CREATE OR REPLACE TABLE wbp_cand AS
            SELECT CAST(int_("Tura") AS INT) AS round, clean_teryt("TERYT") AS teryt_gmina,
                   concat_ws(' ', cap_("Imię"), cap_("Drugie imię"), upper("Nazwisko")) AS name, "Płeć" AS gender, int_("Wiek") AS age,
                   "Miejsce zamieszkania" AS residence, "Komitet" AS committee, "Członek partii" AS party,
                   int_({quote_ident('Głosy "za"')}) AS votes, int_({quote_ident('Głosy "przeciw"')}) AS votes_against,
                   {pct_expr(con, raw, '"%"')} AS pct, coalesce("Wybrany" = 'Tak', false) AS elected,
                   int_("Gł. ważne") AS valid_votes, "Urząd" AS office_code
            FROM {raw}
            """
        )
        protocols = []
        for round_no in (1, 2):
            src = sources.load(f"wbp_prot{round_no}", spec[f"wbp_protocols_{round_no}"])
            protocol = ", ".join(f"CAST(int_({column(con, src, *PROTOCOL_ALIASES[p])}) AS INT) AS {p}" for p in PROTOCOL)
            protocols.append(
                f"""SELECT {round_no} AS round, clean_teryt("TERYT") AS teryt_gmina,
                       regexp_replace("Gmina", '^(.*), (m\\. st\\.|m\\.|gm\\.)$', '\\2 \\1') AS gmina, "Powiat" AS powiat,
                       {_office_case('"Urząd"')} AS office, {protocol} FROM {src}"""
            )
        con.execute(f"CREATE OR REPLACE TABLE wbp_prot AS {' UNION ALL '.join(protocols)}")
    else:
        cands = []
        for round_no in (1, 2):
            src = sources.load(f"wbp_cand{round_no}", spec[f"wbp_candidates_{round_no}"])
            cands.append(
                f"""SELECT {round_no} AS round, clean_teryt("TERYT") AS teryt_gmina, "Nazwisko i imiona" AS name,
                       "Płeć" AS gender, int_("Wiek") AS age, "Miejsce zamieszkania" AS residence,
                       "Nazwa komitetu" AS committee,
                       NULLIF(regexp_replace("Przynależność do partii", '^członek partii politycznej: ', ''),
                              'nie należy do partii politycznej') AS party,
                       int_("Liczba głosów") AS votes, CAST(NULL AS BIGINT) AS votes_against,
                       {pct_expr(con, src, '"Procent głosów"')} AS pct, yes_("Czy uzyskał mandat") AS elected,
                       int_("Liczba głosów ważnych na wszystkich kandydatów") AS valid_votes,
                       upper(left("Urząd", 1)) AS office_code, "Obszar" AS area
                FROM {src}"""
            )
        con.execute(f"CREATE OR REPLACE TABLE wbp_cand AS {' UNION ALL '.join(cands)}")
        _wbp_protocols_2024(con, sources)

    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_gmina_protocols AS
        SELECT {year} AS year, round, teryt_gmina, gmina, powiat, voivodeship(teryt_gmina) AS wojewodztwo, office,
               {', '.join(PROTOCOL)}
        FROM wbp_prot
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_candidates AS
        SELECT {year} AS year, round,
               CAST(row_number() OVER (ORDER BY round, teryt_gmina, name_key(name)) AS INT) AS candidate_id,
               teryt_gmina, display_name(name) AS full_name, surname(name) AS last_name, given_names(name) AS first_names,
               gender_(gender) AS gender, CAST(age AS INT) AS age, residence,
               strip_signature(committee) AS committee_name, NULLIF(trim(party), '') AS party_membership,
               CAST(votes AS INT) AS votes, CAST(votes_against AS INT) AS votes_against, pct, elected
        FROM wbp_cand
        """
    )


# 2010 candidate file and gmina statistics: both rounds side by side under a two-row header, like
# 2014; the second header row (first data row) labels the generic staged columns and is checked.
_WBP_2010 = {
    1: {"elected": '"I tura"', "votes": "column21", "pct": "column22"},
    2: {"elected": '"II tura"', "votes": "column24", "pct": "column25"},
}
_WBP_2010_LABELS = ("wybrany", "l. głosów", "% głosów", "wybrany", "l. głosów", "% głosów", "data gł.")
_WBP_2010_PROTOCOL = {
    # round: (eligible_voters, ballots_issued, valid_ballots (votes cast), invalid_votes, valid_votes)
    1: ('"I tura"', "column6", "column7", "column9", "column8"),
    2: ('"II tura"', "column14", "column15", "column17", "column16"),
}
_WBP_2010_PROTOCOL_LABELS = ("nazwa", "powiat", "upr.", "k. wyd.", "gł. odd.", "gł. niew.", "gł. ważne")
# Excel serial dates of round 2 in the candidate file: 5 and 19 December 2010.
_RUNOFF_DATES_2010 = {"40517": "2010-12-05", "40531": "2010-12-19"}


def _wbp_2010(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2010 mayor candidates (wbp_cand) and gmina protocols (wbp_prot) of both rounds.

    Round 1 lists every candidate ('I tura': T elected, 2 qualified for the run-off, N); round 2 the
    candidates with round-2 votes. In the ten LATE_RUNOFFS gminas a qualified candidate withdrew and
    the run-off was postponed to 19 December: in nine another candidate took the place, in Ostróda
    the remaining candidate faced a for/against vote. The withdrawn candidate was not on the run-off
    ballot and has no round-2 row. Votes against a single candidate are only in the per-precinct files.
    """
    spec = SOURCES[2010]
    raw = sources.load("wbp_cand", spec["wbp_candidates"])
    labels = con.execute(
        f"""SELECT "I tura", column21, column22, "II tura", column24, column25, column26 FROM {raw} WHERE "TERYT" IS NULL"""
    ).fetchall()
    if labels != [_WBP_2010_LABELS]:
        raise BuildError(f"year 2010: unexpected mayor candidate sub-headers {labels}")
    cands = []
    for round_no, c in _WBP_2010.items():
        runoff = f"AND {c['votes']} IS NOT NULL" if round_no == 2 else ""
        cands.append(
            f"""SELECT {round_no} AS round, clean_teryt("TERYT") AS teryt_gmina,
                   concat_ws(' ', capw_("Imiona"), upper("Nazwisko")) AS name, "Płec" AS gender, int_("Wiek") AS age,
                   "Miejsce zamieszkania" AS residence, "Komitet" AS committee, NULLIF(trim("Partia"), '') AS party,
                   int_({c['votes']}) AS votes, CAST(NULL AS BIGINT) AS votes_against,
                   {pct_expr(con, raw, c['pct'])} AS pct, coalesce({c['elected']} = 'T', false) AS elected,
                   "Urząd" AS office_code
            FROM {raw} WHERE "TERYT" IS NOT NULL {runoff}"""
        )
    con.execute(f"CREATE OR REPLACE TABLE wbp_cand AS {' UNION ALL '.join(cands)}")

    src = sources.load("wbp_prot", spec["wbp_protocols"])
    for round_no, cols in _WBP_2010_PROTOCOL.items():
        found = con.execute(f"""SELECT column1, column2, {', '.join(cols)} FROM {src} WHERE "Gmina" = 'TERYT'""").fetchall()
        if [tuple(re.sub(r"\s+", " ", v or "") for v in row) for row in found] != [_WBP_2010_PROTOCOL_LABELS]:
            raise BuildError(f"year 2010: unexpected mayor protocol sub-headers {found}")
    protocols = []
    for round_no, cols in _WBP_2010_PROTOCOL.items():
        protocol = ", ".join(f"CAST(int_({c}) AS INT) AS {p}" for c, p in zip(cols, PROTOCOL))
        protocols.append(
            f"""SELECT {round_no} AS round, clean_teryt("Gmina") AS teryt_gmina, column1 AS gmina, column2 AS powiat,
                   {_office_case('"Urząd"')} AS office, {protocol}
            FROM {src} WHERE "Gmina" <> 'TERYT' AND {cols[0]} IS NOT NULL"""
        )
    con.execute(f"CREATE OR REPLACE TABLE wbp_prot AS {' UNION ALL '.join(protocols)}")

    # Votes against a single candidate, summed from the per-precinct files.
    against = []
    for round_no in (1, 2):
        prec = sources.load(f"wbp_prec{round_no}", spec[f"wbp_precincts_{round_no}"])
        against.append(
            f"""SELECT {round_no} AS round, clean_teryt("TERYT") AS teryt_gmina, sum(int_({column(con, prec, '"Przeciw"')})) AS votes
            FROM {prec} WHERE int_({column(con, prec, '"Przeciw"')}) IS NOT NULL GROUP BY ALL"""
        )
    con.execute(f"CREATE OR REPLACE TABLE wbp_against AS {' UNION ALL '.join(against)}")
    con.execute(
        """
        UPDATE wbp_cand c SET votes_against = a.votes
        FROM wbp_against a
        WHERE a.round = c.round AND a.teryt_gmina = c.teryt_gmina AND c.votes IS NOT NULL
              AND (SELECT count(o.votes) FROM wbp_cand o WHERE o.round = c.round AND o.teryt_gmina = c.teryt_gmina) = 1
        """
    )
    dates = " ".join(f"WHEN {sql_literal(k)} THEN {sql_literal(v)}" for k, v in _RUNOFF_DATES_2010.items())
    late = ", ".join(map(sql_literal, LATE_RUNOFFS[2010]))
    run_checks(con, 2010, {
        "wbp: votes against in gminas with several candidates": """
            SELECT count(*) FROM wbp_against a
            WHERE (SELECT count(c.votes) FROM wbp_cand c WHERE c.round = a.round AND c.teryt_gmina = a.teryt_gmina) <> 1""",
        "wbp: run-offs on an unknown date, or on 19 December != the named gminas": f"""
            SELECT count(*) FROM {raw} WHERE "TERYT" IS NOT NULL AND {_WBP_2010[2]['votes']} IS NOT NULL
              AND (CASE column26 {dates} END = '2010-12-19')
                  IS DISTINCT FROM (clean_teryt("TERYT") IN ({late}))""",
        "wbp: qualified candidates without round-2 votes outside the named gminas": f"""
            SELECT count(*) FROM {raw} WHERE "TERYT" IS NOT NULL AND "I tura" = '2' AND {_WBP_2010[2]['votes']} IS NULL
              AND clean_teryt("TERYT") NOT IN ({late})""",
    })


# 2014 candidate file: both rounds side by side under a two-row header. Staging keeps the first row
# as headers, so the per-round columns are 'I tura' / 'II tura' (valid votes in the gmina) followed by
# generic names; the second header row (first data row) labels them and is checked here.
_WBP_2014 = {
    1: {"valid": '"I tura"', "votes": "column18", "against": "column19", "pct": "column20", "elected": "column21"},
    2: {"valid": '"II tura"', "votes": "column23", "against": "column24", "pct": "column25", "elected": "column26"},
}
_WBP_2014_LABELS = ("Gł. wazne", "Gł. na kand.", "Gł. przeciw", "% głosów", "Mandat")


def _wbp_2014(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2014 mayor candidates (wbp_cand) and gmina protocols (wbp_prot) of both rounds."""
    spec = SOURCES[2014]
    raw = sources.load("wbp_cand", spec["wbp_candidates"])
    for cols in _WBP_2014.values():
        labels = con.execute(
            f"SELECT {', '.join(cols[k] for k in ('valid', 'votes', 'against', 'pct', 'elected'))} FROM {raw} WHERE \"TERYT\" IS NULL"
        ).fetchall()
        if [tuple(re.sub(r"\s+", " ", v or "") for v in row) for row in labels] != [_WBP_2014_LABELS]:
            raise BuildError(f"year 2014: unexpected mayor candidate sub-headers {labels}")
    cands = []
    for round_no, c in _WBP_2014.items():
        cands.append(
            f"""SELECT {round_no} AS round, clean_teryt("TERYT") AS teryt_gmina,
                   concat_ws(' ', capw_("Imiona"), upper("Nazwisko")) AS name, "Płeć" AS gender, int_("Wiek") AS age,
                   "Miejscowość" AS residence, "Komitet" AS committee, NULLIF(trim("Członek"), '') AS party,
                   int_({c['votes']}) AS votes, int_({c['against']}) AS votes_against,
                   {pct_expr(con, raw, c['pct'])} AS pct, coalesce({c['elected']} = 'T', false) AS elected,
                   int_({c['valid']}) AS valid_votes, "Urząd" AS office_code
            FROM {raw} WHERE "TERYT" IS NOT NULL AND {c['valid']} IS NOT NULL"""
        )
    con.execute(f"CREATE OR REPLACE TABLE wbp_cand AS {' UNION ALL '.join(cands)}")
    protocols = []
    for round_no in (1, 2):
        src = sources.load(f"wbp_prot{round_no}", spec[f"wbp_protocols_{round_no}"])
        protocol = ", ".join(f"CAST(int_({column(con, src, *LOCAL_PROTOCOL_ALIASES[p])}) AS INT) AS {p}" for p in PROTOCOL)
        protocols.append(
            f"""SELECT {round_no} AS round, clean_teryt("TERYT") AS teryt_gmina,
                   CASE WHEN clean_teryt("TERYT") = '146501' THEN 'm. st. Warszawa'
                        ELSE regexp_replace("Gmina", '^(.*), (m\\.|gm\\.)$', '\\2 \\1') END AS gmina,
                   "Powiat" AS powiat, {protocol} FROM {src} WHERE "TERYT" IS NOT NULL"""
        )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_prot AS
        SELECT p.round, p.teryt_gmina, p.gmina, p.powiat,
               (SELECT {_office_case('any_value(c.office_code)')} FROM wbp_cand c
                WHERE c.round = p.round AND c.teryt_gmina = p.teryt_gmina) AS office,
               {', '.join(PROTOCOL)}
        FROM ({' UNION ALL '.join(protocols)}) p
        """
    )


def _office_case(code: str) -> str:
    """SQL CASE turning the office code (W/B/P) into its name."""
    return "CASE " + code + " " + " ".join(f"WHEN {sql_literal(k)} THEN {sql_literal(v)}" for k, v in OFFICES.items()) + " END"


def _wbp_protocols_2024(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Aggregate the per-precinct mayor protocols to gminas and fill ``votes_against``.

    Round 1 has one file per voivodeship with different candidate columns, so files are read one
    at a time instead of as one wide union.
    """
    spec = SOURCES[2024]
    con.execute(
        "CREATE OR REPLACE TABLE wbp_prot_raw (round INT, teryt_gmina VARCHAR, gmina VARCHAR, powiat VARCHAR, "
        + ", ".join(f"{p} BIGINT" for p in PROTOCOL)
        + ")"
    )
    con.execute("CREATE OR REPLACE TABLE wbp_against (teryt_gmina VARCHAR, name_key VARCHAR, votes BIGINT)")
    for round_no in (1, 2):
        for path in sources.files(spec[f"wbp_protocols_{round_no}"]):
            src = f"read_parquet({sql_literal(path)}, hive_partitioning = false)"
            # Precincts of Warsaw carry the TERYT of their district; the mayor is elected city-wide.
            raw_teryt = f"clean_teryt({column(con, src, *GEO_ALIASES['teryt'])})"
            teryt = f"CASE WHEN left({raw_teryt}, 4) = '1465' THEN '146501' ELSE {raw_teryt} END"
            protocol = ", ".join(f"int_({column(con, src, *PROTOCOL_ALIASES[p])})" for p in PROTOCOL)
            con.execute(f'INSERT INTO wbp_prot_raw SELECT {round_no}, {teryt}, "Gmina", "Powiat", {protocol} FROM {src}')
            against = {h: python_name_key(m[1]) for h in columns_of(con, src) if (m := _AGAINST.match(h))}
            for header, key in against.items():
                con.execute(
                    f"""INSERT INTO wbp_against SELECT {teryt}, ?, sum(int_({quote_ident(header)})) FROM {src}
                        WHERE int_({quote_ident(header)}) IS NOT NULL GROUP BY 1""",
                    [key],
                )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_prot AS
        SELECT p.round, p.teryt_gmina,
               CASE WHEN p.teryt_gmina = '146501' THEN 'm. st. Warszawa' ELSE any_value(p.gmina) END AS gmina,
               CASE WHEN p.teryt_gmina = '146501' THEN 'Warszawa' ELSE any_value(p.powiat) END AS powiat,
               (SELECT {_office_case('any_value(c.office_code)')} FROM wbp_cand c
                WHERE c.round = p.round AND c.teryt_gmina = p.teryt_gmina) AS office,
               {', '.join(f'CAST(sum({x}) AS INT) AS {x}' for x in PROTOCOL)}
        FROM wbp_prot_raw p GROUP BY p.round, p.teryt_gmina
        """
    )
    con.execute(
        """
        UPDATE wbp_cand c SET votes_against = a.votes
        FROM wbp_against a
        WHERE a.teryt_gmina = c.teryt_gmina AND a.name_key = name_key(c.name)
              AND (SELECT count(o.votes) FROM wbp_cand o WHERE o.round = c.round AND o.teryt_gmina = c.teryt_gmina) = 1
        """
    )


# --- Checks -----------------------------------------------------------------------------------


# A list's district total exceeds the sum of its candidates when a candidate was struck off after
# the ballots were printed: votes marked next to that name count for the list only. 2024 keeps
# struck-off candidates in the register with empty votes (detected in the check); the 2018 register
# drops them, so those lists are named here. Verified against the 2018 per-precinct candidate files
# (the struck-off candidate's column is empty). Documented in docs/MART.md under "Known gaps".
STRUCK_OFF = {
    # 2014: lists whose total in the gmina results exceeds the sum of their candidates' votes. Seven
    # have a missing list position (a struck-off candidate dropped from the register). The other six
    # (mazowieckie 4: four lists 1-10 votes above; śląskie 2: PO 1 vote, Oburzeni 85) have no missing
    # position; the per-precinct candidate file of mazowieckie confirms the register's candidate votes,
    # so the published files differ by these few votes. Named so that no other list may differ.
    2014: [
        ("dolnośląskie", 1, "KOMITET WYBORCZY WYBORCÓW BEZPARTYJNI SAMORZĄDOWCY"),
        ("dolnośląskie", 3, "KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ"),
        ("mazowieckie", 4, "KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ"),
        ("mazowieckie", 4, "KOALICYJNY KOMITET WYBORCZY SLD LEWICA RAZEM"),
        ("mazowieckie", 4, "KOMITET WYBORCZY NOWA PRAWICA — JANUSZA KORWIN-MIKKE"),
        ("mazowieckie", 4, "KOMITET WYBORCZY PLATFORMA OBYWATELSKA RP"),
        ("mazowieckie", 4, "KOMITET WYBORCZY MAZOWIECKA WSPÓLNOTA SAMORZĄDOWA"),
        ("pomorskie", 1, "KOMITET WYBORCZY TWÓJ RUCH"),
        ("pomorskie", 4, "KOMITET WYBORCZY TWÓJ RUCH"),
        ("warmińsko-mazurskie", 3, "KOMITET WYBORCZY DEMOKRACJA BEZPOŚREDNIA"),
        ("łódzkie", 3, "KOMITET WYBORCZY DEMOKRACJA BEZPOŚREDNIA"),
        ("śląskie", 2, "KOMITET WYBORCZY OBURZENI"),
        ("śląskie", 2, "KOMITET WYBORCZY PLATFORMA OBYWATELSKA RP"),
    ],
    2018: [
        ("mazowieckie", 7, "KOMITET WYBORCZY WYBORCÓW WOLNOŚĆ W SAMORZĄDZIE"),
        ("świętokrzyskie", 3, "KOMITET WYBORCZY PARTII RAZEM"),
        ("śląskie", 1, "KOMITET WYBORCZY PARTIA ZIELONI"),
    ],
}


def _known_struck_off(year: int) -> str:
    """SQL row-value list of STRUCK_OFF for ``year`` (a never-matching row when empty)."""
    rows = STRUCK_OFF.get(year) or [("", -1, "")]
    return ", ".join(f"({sql_literal(w)}, {d}, {sql_literal(c)})" for w, d, c in rows)


def _check(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Local results must reconcile between candidates, lists and gmina protocols."""
    run_checks(con, year, {
        "sejmik: elected councillors != seats in the district register": """
            SELECT abs((SELECT count(*) FROM sejmik_candidates WHERE elected) - (SELECT seats FROM sejmik_seats))""",
        "sejmik: committee columns with votes not mapped to a committee": """
            SELECT count(*) FROM sejmik_unmapped WHERE votes > 0""",
        "sejmik: duplicate gmina rows (same gmina and district)": """
            SELECT count(*) - count(DISTINCT (teryt_gmina, district_no)) FROM sejmik_prot_rows""",
        "sejmik: gminas whose list votes != valid votes": """
            SELECT count(*) FROM sejmik_gmina_protocols p
            WHERE p.valid_votes <> (SELECT coalesce(sum(v.votes), 0) FROM sejmik_gmina_list_votes v WHERE v.teryt_gmina = p.teryt_gmina)""",
        "sejmik: gminas without a district or in several (except named split gminas)": f"""
            SELECT count(*) FROM sejmik_gmina_protocols p
            WHERE (SELECT count(*) FROM sejmik_gmina_district d WHERE d.teryt_gmina = p.teryt_gmina)
                  <> CASE WHEN p.teryt_gmina IN ({', '.join(map(sql_literal, SEJMIK_SPLIT_GMINAS.get(year, [''])))}) THEN 2 ELSE 1 END""",
        "sejmik: district lists without candidates": """
            SELECT count(*) FROM sejmik_district_lists WHERE candidate_votes IS NULL AND votes > 0""",
        "sejmik: candidate votes exceed list votes": """
            SELECT count(*) FROM sejmik_district_lists WHERE candidate_votes > votes""",
        "sejmik: list votes above candidate votes in lists without a struck-off candidate": f"""
            SELECT count(*) FROM sejmik_district_lists l JOIN sejmik_committees c USING (committee_id)
            WHERE l.candidate_votes < l.votes
              AND NOT EXISTS (SELECT 1 FROM sejmik_candidates k WHERE k.votes IS NULL AND k.wojewodztwo = l.wojewodztwo
                              AND k.district_no = l.district_no AND k.committee_id = l.committee_id)
              AND (l.wojewodztwo, l.district_no, c.committee_name) NOT IN ({_known_struck_off(year)})""",
        "wbp: candidates in gminas without a protocol": """
            SELECT count(*) FROM wbp_candidates c
            WHERE NOT EXISTS (SELECT 1 FROM wbp_gmina_protocols p WHERE p.round = c.round AND p.teryt_gmina = c.teryt_gmina)""",
        "wbp: duplicate gmina protocols": """
            SELECT count(*) - count(DISTINCT (round, teryt_gmina)) FROM wbp_gmina_protocols""",
        "wbp: candidate votes != valid votes (several candidates)": """
            SELECT count(*) FROM wbp_gmina_protocols p
            JOIN (SELECT round, teryt_gmina, sum(votes) AS votes, count(votes) AS n FROM wbp_candidates GROUP BY ALL) c
              USING (round, teryt_gmina)
            WHERE c.n > 1 AND c.votes <> p.valid_votes""",
        "wbp: votes for + against != valid votes (single candidate)": """
            SELECT count(*) FROM wbp_gmina_protocols p
            JOIN (SELECT round, teryt_gmina, sum(votes) AS votes, sum(votes_against) AS against, count(votes) AS n
                  FROM wbp_candidates GROUP BY ALL) c USING (round, teryt_gmina)
            WHERE c.n = 1 AND coalesce(c.votes + c.against, -1) <> p.valid_votes""",
        "wbp: gminas with more than one elected mayor": """
            SELECT count(*) FROM (SELECT teryt_gmina FROM wbp_candidates WHERE elected GROUP BY 1 HAVING count(*) > 1)""",
        "wbp: round 2 in a gmina without round 1": """
            SELECT count(*) FROM wbp_gmina_protocols p WHERE round = 2
              AND NOT EXISTS (SELECT 1 FROM wbp_gmina_protocols q WHERE q.round = 1 AND q.teryt_gmina = p.teryt_gmina)""",
    })
    if con.execute("SELECT count(*) FROM wbp_gmina_protocols WHERE office IS NULL").fetchone()[0]:
        raise BuildError(f"year {year}: mayor protocols without an office")
