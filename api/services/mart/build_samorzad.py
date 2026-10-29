"""Build the local-election tables of one year in DuckDB (loader side): voivodeship assemblies
(sejmiki) and mayors (wójt / burmistrz / prezydent miasta, "wbp").

Councils of gminas and powiats are not loaded: they have tens of thousands of districts and are
rarely asked about.

Sources (KBW mirror):
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
    pct_expr,
    python_name_key,
    quote_ident,
    run_checks,
    sql_list,
    sql_literal,
)
from api.services.mart.build_precincts import GEO_ALIASES, PROTOCOL_ALIASES, vote_columns
from api.services.mart.schema import SEJMIK_TABLES, WBP_TABLES

log = logging.getLogger("mart")

TABLES = SEJMIK_TABLES + WBP_TABLES
FAMILY = ("sejmik", "wbp")

DATES = {2018: ("2018-10-21", "2018-11-04"), 2024: ("2024-04-07", "2024-04-21")}
SOURCES = {
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
    """Build and check sejmik and mayor tables of ``sources.year``; return the loaded elections."""
    year = sources.year
    # 2018 registers have separate name columns, some rows in capitals: 'WITOLD' -> 'Witold'.
    con.execute("CREATE OR REPLACE MACRO cap_(x) AS CASE WHEN upper(x) = x THEN upper(left(x, 1)) || lower(substr(x, 2)) ELSE x END")
    _build_sejmik(con, sources)
    _build_wbp(con, sources)
    _check(con, year)
    first, second = DATES[year]
    return [
        Election("sejmik", year, 1, first, f"Voivodeship assembly (sejmik) elections {year}"),
        Election("wbp", year, 1, first, f"Mayor (wójt/burmistrz/prezydent miasta) elections {year}, round 1"),
        Election("wbp", year, 2, second, f"Mayor (wójt/burmistrz/prezydent miasta) elections {year}, round 2"),
    ]


# --- Voivodeship assemblies -----------------------------------------------------------------


def _build_sejmik(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Sejmik committees, candidates, district lists, gmina protocols and gmina list votes."""
    year = sources.year
    spec = SOURCES[year]
    create_committee_aliases(con)
    raw = sources.load("sejmik_cand", spec["sejmik_candidates"])
    if year == 2018:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw} WHERE \"Szczebel\" = 'województwo'")
        f = {
            "woj": '"Jednostka"', "district": '"Nr okr."', "list_no": '"Nr listy"', "position": '"Poz."',
            "name": """concat_ws(' ', cap_("Imię"), cap_("Drugie imię"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zam."', "elected": '"Mandat"',
        }
    else:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw}")
        f = {
            "woj": '"Województwo"', "district": '"Nr okręgu"', "list_no": '"Nr listy"', "position": '"Pozycja na liście"',
            "name": '"Nazwisko i imiona"', "committee": '"Nazwa komitetu"',
            "residence": '"Miejsce zamieszkania"', "elected": '"Czy uzyskał mandat"',
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
               gender_(k."Płeć") AS gender, k.{f['residence']} AS residence, int_(k."Liczba głosów") AS votes,
               yes_(k.{f['elected']}) AS elected
        FROM sejmik_cand k JOIN sejmik_committees c ON c.committee_name = strip_signature(k.{f['committee']})
        """
    )
    seats = sources.load("sejmik_seats", spec["sejmik_seats"])
    seat_filter = "WHERE \"Szczebel\" = 'Sejmik województwa'" if year == 2018 else ""
    con.execute(f"CREATE OR REPLACE TABLE sejmik_seats AS SELECT sum(int_(\"Liczba mandatów\")) AS seats FROM {seats} {seat_filter}")

    gminas = sources.load("sejmik_gminas", spec["sejmik_gminas"])
    parts = [(gminas, f"clean_teryt({column(con, gminas, *GEO_ALIASES['teryt'])})", '"Gmina"')]
    if year == 2024:
        # Cities with powiat rights (powiat code 61-99) are only in the powiat file; their gmina
        # TERYT is the powiat code with '01'. Warsaw (1465xx) is already listed by district.
        powiats = sources.load("sejmik_powiats", spec["sejmik_powiats"])
        con.execute(
            f"""CREATE OR REPLACE TABLE sejmik_cities AS SELECT * FROM {powiats}
                WHERE substr(lpad("TERYT Powiatu", 6, '0'), 3, 2) >= '61' AND left(lpad("TERYT Powiatu", 6, '0'), 4) <> '1465'"""
        )
        parts.append(("sejmik_cities", "left(lpad(\"TERYT Powiatu\", 6, '0'), 4) || '01'", "'m. ' || \"Powiat\""))
    committee_by_header = _sejmik_header_map(con, sources, gminas)
    selects, list_selects = [], []
    con.execute("CREATE OR REPLACE TABLE sejmik_unmapped (source VARCHAR, header VARCHAR, votes BIGINT)")
    for source, teryt, gmina in parts:
        protocol = ", ".join(f"CAST(int_({column(con, source, *PROTOCOL_ALIASES[p])}) AS INT) AS {p}" for p in PROTOCOL)
        selects.append(f"SELECT {teryt} AS teryt_gmina, {gmina} AS gmina, \"Powiat\" AS powiat, {protocol} FROM {source}")
        headers = [h for h in vote_columns(con, source) if h in committee_by_header]
        cols = ", ".join(quote_ident(h) for h in headers)
        list_selects.append(
            f"SELECT teryt_gmina, header, votes FROM (UNPIVOT (SELECT {teryt} AS teryt_gmina, {cols} FROM {source}) "
            f"ON {cols} INTO NAME header VALUE votes)"
        )
        unmapped = [h for h in vote_columns(con, source) if h not in committee_by_header]
        for h in unmapped:
            con.execute(
                f"INSERT INTO sejmik_unmapped SELECT ?, ?, coalesce(sum(int_({quote_ident(h)})), 0) FROM {source}", [source, h]
            )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_gmina_protocols AS
        SELECT {year} AS year, teryt_gmina, gmina, powiat, voivodeship(teryt_gmina) AS wojewodztwo, {', '.join(PROTOCOL)}
        FROM ({' UNION ALL '.join(selects)})
        """
    )
    con.execute("CREATE OR REPLACE TEMP TABLE sejmik_header_map (header VARCHAR, committee_id INT)")
    con.executemany("INSERT INTO sejmik_header_map VALUES (?, ?)", list(committee_by_header.items()))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_gmina_list_votes AS
        SELECT {year} AS year, u.teryt_gmina, m.committee_id, CAST(sum(int_(u.votes)) AS INT) AS votes
        FROM ({' UNION ALL '.join(list_selects)}) u JOIN sejmik_header_map m USING (header)
        WHERE int_(u.votes) IS NOT NULL
        GROUP BY ALL
        """
    )
    # Gmina -> district: sejmik districts consist of whole powiats, so every gmina (and every
    # Warsaw district) lies in exactly one. 2018 lists it in the gmina file, 2024 only per precinct.
    if year == 2018:
        con.execute(f"""CREATE OR REPLACE TABLE sejmik_gmina_district AS
            SELECT DISTINCT clean_teryt("TERYT gminy") AS teryt_gmina, CAST(int_("Nr okr.") AS INT) AS district_no FROM {gminas}""")
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
            SELECT voivodeship(v.teryt_gmina) AS wojewodztwo, d.district_no, v.committee_id, sum(v.votes) AS votes
            FROM sejmik_gmina_list_votes v JOIN sejmik_gmina_district d USING (teryt_gmina) GROUP BY ALL),
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
    headers = vote_columns(con, gminas)
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


# --- Mayors -----------------------------------------------------------------------------------


def _build_wbp(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Mayor candidates and gmina protocols of both rounds."""
    year = sources.year
    spec = SOURCES[year]
    if year == 2018:
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
        "sejmik: duplicate gminas": """
            SELECT count(*) - count(DISTINCT teryt_gmina) FROM sejmik_gmina_protocols""",
        "sejmik: gminas whose list votes != valid votes": """
            SELECT count(*) FROM sejmik_gmina_protocols p
            WHERE p.valid_votes <> (SELECT coalesce(sum(v.votes), 0) FROM sejmik_gmina_list_votes v WHERE v.teryt_gmina = p.teryt_gmina)""",
        "sejmik: gminas without a district": """
            SELECT count(*) FROM sejmik_gmina_protocols p
            WHERE (SELECT count(*) FROM sejmik_gmina_district d WHERE d.teryt_gmina = p.teryt_gmina) <> 1""",
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
