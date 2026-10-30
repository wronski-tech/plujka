"""Build the local-election tables of one year in DuckDB (loader side): voivodeship assemblies
(sejmiki), mayors (wójt / burmistrz / prezydent miasta, "wbp") and, through ``build_rady``, the
councils of gminas, powiats and Warsaw districts.

Sources (KBW mirror):
- 1994: gmina councils only (no mayors, powiats or voivodeship assemblies), see ``build_rady``.
- 1998 sejmik (no mayors: wójts were elected by the councils until 2002): ``1998-kand-rady``
  (candidates of every council; ``Szczebel = 'województwo'``, ``Mand.`` W = elected by votes, P = by
  precedence and L = by lot in a tie, B = without voting, N = not elected; the list's district total
  ``Gł. na listę`` and the district's valid votes ``Gł. ważne``) and the per-precinct list results
  ``sejmik_listy_98`` (columns are list numbers, which name a committee per voivodeship and district),
  summed to gminas and districts and checked against the gmina file ``sejmik-gminy-98``. The
  per-district workbooks ``1998woj`` (see ``build_rady.load_district_workbooks_1998``) repeat every
  precinct with its ballots issued ('Wydane karty', loaded as ballots_issued) and give the
  candidates' votes per precinct (checked against the precinct and candidate files, not loaded);
  ``1998mazowieckie`` summarises mazowieckie per district (seats, lists, candidates, protocol, list and
  candidate votes; checked). PKW computed the 1998 turnout from valid ballots ('oddało karty
  ważne'). There is no district register; the 855 seats are checked against the PKW total. Codes are
  the 1999 TERYT register; Warsaw gminas (powiat 1431) are mapped to their post-2002 districts as in
  2000/2001, except Warszawa-Centrum (143104, seven districts), as for the 2000 presidential election.
- 2002 sejmik: ``2002-kand-rady`` (candidates of every council; ``Szczebel = 'województwo'``, ``Mandat``
  G = elected by votes, L = by lot, B = without voting) and the per-precinct committee results
  ``samo2002-sej-kom-obw``, summed to gminas (Warsaw is one gmina there, 146501, split between three
  districts; Łódź between two). Invalid votes are votes cast minus valid votes (not published per
  precinct); the gmina sums are checked against ``2002-frekwencja-i-nieważne-gminy``. There is no
  district register; the 561 seats are checked against the PKW total. Gmina and powiat names come
  from ``samo2002gminy`` and the gmina type of the mayor file (see ``_gmina_names_2002``).
- 2002 wbp: ``wojt2002`` (both rounds, column ``Tura``) and the per-round gmina statistics in
  ``2002-frekwencja-i-nieważne-gminy`` (Warsaw as the sum of its districts). Votes against a single
  candidate are not published: the ballot offered only "za" / "przeciw", so they are valid votes
  minus votes for (see ``_wbp_2002``).
- 2006 sejmik: ``2006-kand-rady-new`` (candidates of every council with the list's district total;
  ``Szczebel = 'WO'``), ``2006-sejmiki-okregi`` (seats) and ``2006-sejmiki-gm`` (gmina results, full
  committee names; Łódź is split between two districts).
- 2006 wbp: ``wojt2006-zbiorówka`` (both rounds, column ``Tura``), the per-round gmina statistics
  ``2006-frekw-wbp-gm`` (side by side, like 2010), votes against a single candidate from the
  per-precinct file ``samo2006-wbp-obw`` and the withdrawals before the run-off ``2006-trzecia-tura``
  (see ``_wbp_2006``).
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
from api.services.mart.build_precincts import GEO_ALIASES, NOT_VOTE_COLUMNS, PROTOCOL_ALIASES, create_warsaw_gminas_2001
from api.services.mart.schema import RADA_TABLES, SEJMIK_TABLES, WBP_TABLES

log = logging.getLogger("mart")

TABLES = SEJMIK_TABLES + WBP_TABLES + RADA_TABLES
FAMILY = ("sejmik", "wbp", "rada")

# 2006 and 2010: some run-offs took place two weeks later (see LATE_RUNOFFS). 1998 had no mayor elections;
# 1994 only gmina councils (build_rady; no powiats or voivodeship assemblies before 1999).
DATES = {
    1994: ("1994-06-19", None),
    1998: ("1998-10-11", None),
    2002: ("2002-10-27", "2002-11-10"),
    2006: ("2006-11-12", "2006-11-26"),
    2010: ("2010-11-21", "2010-12-05"),
    2014: ("2014-11-16", "2014-11-30"),
    2018: ("2018-10-21", "2018-11-04"),
    2024: ("2024-04-07", "2024-04-21"),
}
SOURCES = {
    1998: {
        "sejmik_candidates": "**/samorzad/1998-kand-rady.parquet",
        "sejmik_precincts": "**/samorzad/sejmik/1998/sejmik_listy_98__*.parquet",
        "sejmik_gminas": "**/samorzad/sejmik/1998/sejmik-gminy-98__*.parquet",
        "sejmik_workbooks": "**/samorzad/sejmik/1998woj/*__sheet*.parquet",
        "sejmik_mazowieckie": "**/samorzad/sejmik/1998mazowieckie.parquet",
    },
    2002: {
        "sejmik_candidates": "**/samorzad/2002-kand-rady.parquet",
        "sejmik_precincts": "**/samorzad/sejmik/samo2002-sej-kom-obw.parquet",
        "gminas": "**/samorzad/samo2002gminy.parquet",
        "stats": "**/samorzad/2002-frekwencja-i-niewa*ne-gminy.parquet",
        "wbp_candidates": "**/samorzad/wbp/wbp2002/wojt2002__*.parquet",
    },
    2006: {
        "sejmik_candidates": "**/samorzad/2006-kand-rady-new.parquet",
        "sejmik_seats": "**/samorzad/sejmik/2006/2006-sejmiki-okregi__*.parquet",
        "sejmik_gminas": "**/samorzad/sejmik/2006/2006-sejmiki-gm__*.parquet",
        "wbp_candidates": "**/samorzad/wbp/wbp2006/wojt2006-zbi*__*.parquet",
        "wbp_protocols": "**/samorzad/2006-frekw-wbp-gm.parquet",
        "wbp_precincts": "**/samorzad/wbp/samo2006-wbp-obw.parquet",
        "wbp_withdrawn": "**/samorzad/wbp/wbp2006/2006-trzecia-tura__*.parquet",
    },
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
# oddanych' = valid ballots, as in the 2010 protocols), 2002/2006 abbreviations ('Odd.' = votes cast =
# valid ballots, 'Frekw.' = ballots issued); they come after the shared aliases, so later years are
# unaffected.
LOCAL_PROTOCOL_ALIASES = {
    name: PROTOCOL_ALIASES[name] + extra
    for name, extra in {
        "eligible_voters": ("Liczba wyborców", "Upr."),
        "ballots_issued": ("Wydane karty", "Liczba wydanych kart do głosowania", "Frekw."),
        "valid_ballots": ("Karty ważne", "Liczba głosów oddanych", "Odd."),
        "invalid_votes": ("Głosy nieważne", "Niew."),
        "valid_votes": ("Głosy ważne", "Ważne"),
    }.items()
}
# 2002, 2010 and 2014 have no sejmik district register: PKW totals of seats ("Wybory samorządowe 2014 -
# statystyki", pkw.gov.pl: 555 radnych województw; wybory2010.pkw.gov.pl/geo/pl/000000.html: 561
# radnych in 87 districts; PKW announcement of 8 November 2002, Dz. U. Nr 187, poz. 1568: 561 radnych
# in 88 districts; PKW announcement of 23 October 1998, Dz. U. Nr 131, poz. 861: 855 radnych in 127
# districts).
SEJMIK_SEATS = {1998: 855, 2002: 561, 2010: 561, 2014: 555}
# Gminas split between sejmik districts (one gmina-file row per district) -> number of districts:
# Łódź 2002, 2006 and 2010 (districts 1 and 2, PKW "Zbiorcze informacje o okręgach" of łódzkie);
# Warsaw 2002, published as one gmina in three districts (the 2002 precinct file has no district
# TERYT). 1998: the district of every precinct is published; Wrocław, Łódź, Kraków and Gdańsk lie in
# two districts each, gmina Warszawa-Centrum in four.
SEJMIK_SPLIT_GMINAS = {
    1998: {"026401": 2, "106101": 2, "126101": 2, "143104": 4, "226101": 2},
    2002: {"106101": 2, "146501": 3}, 2006: {"106101": 2}, 2010: {"106101": 2},
}
# 1998 sejmik committees spelled differently in some districts of one voivodeship (strip_signature
# form): variant -> name used in the other districts. Each pair has the same list number in the same
# voivodeship, and the KBW list register (wykaz-list) names one committee for that number there.
SEJMIK_NAME_VARIANTS = {
    1998: {
        "PRZYMIERZE SPOŁECZNE PSL-UP-KPEIR": "KRAJOWY KOMITET WYBORCZY PRZYMIERZE SPOŁECZNE: PSL-UP-KPEIR",  # mazowieckie 13
        "PRZYMIERZE SPOŁECZNE: PSL-UP-KPEIR": "KRAJOWY KOMITET WYBORCZY PRZYMIERZE SPOŁECZNE: PSL-UP-KPEIR",  # świętokrzyskie 6
        "RADA GŁÓWNA KONSERWATYWNO - LIBERALNEJ PARTII UNIA POLITYKI REALNEJ":
            "RADA GŁÓWNA KONSERWATYWNO-LIBERALNEJ PARTII UNIA POLITYKI REALNEJ",  # łódzkie 5
        "RADA GŁÓWNA KONSERWATYWNO-LIBERALNA PARTII UNII POLITYKI REALNEJ":
            "RADA GŁÓWNA KONSERWATYWNO-LIBERALNEJ PARTII UNIA POLITYKI REALNEJ",  # łódzkie 2, 4
        '"SAMORZĄD OBYWATELSKI"': 'KOMITET WYBORCZY "SAMORZĄD OBYWATELSKI"',  # mazowieckie 4 (other: 6)
        "KOMITET WYBORCZY POROZUMIENIA WARSZAWSKIEJ RADY GOSPODARCZEJ":
            "POROZUMIENIE WARSZAWSKIEJ RADY GOSPODARCZEJ",  # mazowieckie 2
        "ZARZĄD GŁÓWNY CHRZEŚCIJAŃSKIEJ DEMOKRACJI III RZECZYPOSPOLITEJ POLSKIEJ":
            "ZARZĄD GŁÓWNY CHRZEŚCIJAŃSKIEJ DEMOKRACJI III RZECZYPOSPOLITEJ",  # pomorskie 3, 8
    },
}
# Sejmik districts whose eligible voters in the district register differ from the sum of their gmina
# rows (2006: dolnośląskie 1 register 502,072 vs gminas 502,060; lubelskie 1 276,406 vs 276,409; all
# other protocol figures agree). The gmina rows are loaded, so national eligible voters are 30,241,176
# against PKW's 30,241,185 (the district sum).
SEJMIK_ELIGIBLE_GAPS = {2006: [("dolnośląskie", 1), ("lubelskie", 1)]}
# 1998 sejmik totals in PKW's announcement (Dz. U. Nr 131, poz. 861) -> (official figure, KBW minus PKW).
# Every KBW file gives the larger figures: the precinct file, the gmina file, the per-district workbooks
# (1998woj, precinct by precinct) and the mazowieckie district summary (district by district); the
# 23,756 precincts are PKW's count. The difference is not located in any published file.
SEJMIK_PKW_GAP_1998 = {
    "eligible_voters": (28544777, 1608), "valid_ballots": (12945043, 1402), "valid_votes": (11721825, 1382), "invalid_votes": (1223218, 20),
}
# Run-offs held two weeks later because a candidate who qualified withdrew: 2006 on 10 December
# instead of 26 November (PKW announcements of 27 November and 11 December 2006: 16 gminas, listed in
# '2006-trzecia-tura', see _wbp_2006), 2010 on 19 December instead of 5 December ('data gł.' in the
# candidate file, see _wbp_2010).
LATE_RUNOFFS = {
    2006: ["040804", "041804", "060304", "061812", "061902", "100402", "101508", "120702", "142001", "142011",
           "181706", "221308", "241703", "246201", "280705", "320701"],
    2010: ["020603", "040902", "061403", "120604", "200404", "200802", "201209", "220903", "281501", "321408"],
}
# 2002 run-offs (all on 10 November) where a qualified candidate withdrew and the candidate with the
# next most round-1 votes took the place ('rezyg.txt' in the KBW bundle wbp2002.zip: 17 gminas).
REPLACED_RUNOFF_CANDIDATES = {
    2002: ["040606", "062004", "101605", "121502", "140502", "141608", "160802", "180307", "181408", "200802",
           "200903", "220205", "220305", "220705", "280804", "281408", "321406"],
}
# Years with council elections only (1994: gmina councils).
COUNCILS_ONLY = {1994}
OFFICES = {"W": "wójt", "B": "burmistrz", "P": "prezydent miasta"}
_AGAINST = re.compile(r"^Głosy przeciw wyborowi kandydata - (.+?) zarejestrowanego przez ")


def _has_mayors(year: int) -> bool:
    """Whether mayors were elected directly in ``year`` (since 2002)."""
    return DATES[year][1] is not None


def available(sources: YearSources) -> bool:
    """Whether the year has staged sejmik candidates and, in years with mayor elections, mayor candidates."""
    if sources.year in COUNCILS_ONLY:
        return build_rady.available(sources)
    spec = SOURCES.get(sources.year)
    if not spec:
        return False
    if not _has_mayors(sources.year):
        return sources.has(spec["sejmik_candidates"])
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
    if year in COUNCILS_ONLY:
        build_rady.build_rady(con, sources)
        return [Election("rada", year, 1, DATES[year][0], f"Gmina council elections {year}")]
    if year == 2002:
        _gmina_names_2002(con, sources)
        _stats_2002(con, sources)
    _build_sejmik(con, sources)
    build_rady.add_list_groups(con, year, "sejmik")
    if _has_mayors(year):
        _build_wbp(con, sources)
    _check(con, year)
    first, second = DATES[year]
    elections = [Election("sejmik", year, 1, first, f"Voivodeship assembly (sejmik) elections {year}")]
    if _has_mayors(year):
        elections += [
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
    create_committee_aliases(con, sources.year)
    raw = sources.load("sejmik_cand", spec["sejmik_candidates"])
    if year == 1998:
        # Mand.: W elected by votes, P by precedence and L by lot in a tie, B without voting, N not elected.
        variants = " ".join(f"WHEN {sql_literal(v)} THEN {sql_literal(c)}" for v, c in SEJMIK_NAME_VARIANTS[1998].items())
        con.execute(
            f"""CREATE OR REPLACE TABLE sejmik_cand AS
                SELECT * REPLACE (CASE strip_signature("Komitet") {variants} ELSE "Komitet" END AS "Komitet"),
                       CASE WHEN "Mand." IN ('W', 'P', 'L', 'B') THEN 'T' ELSE 'N' END AS elected_
                FROM {raw} WHERE "Szczebel" = 'województwo'"""
        )
        f = {
            "woj": '"Jednostka"', "district": '"Nr okr."', "list_no": '"Nr listy"', "position": '"Poz."',
            "name": """concat_ws(' ', capw_("Imiona"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zam."', "elected": "elected_", "votes": '"Gł. kand."',
        }
    elif year == 2002:
        # Mandat: G elected by votes, L by lot (tie), B without voting, N not elected.
        con.execute(
            f"""CREATE OR REPLACE TABLE sejmik_cand AS SELECT *, CASE WHEN "Mandat" IN ('G', 'L', 'B') THEN 'T' ELSE 'N' END AS elected_
                FROM {raw} WHERE "Szczebel" = 'województwo'"""
        )
        f = {
            "woj": '"Jednostka"', "district": '"Okręg"', "list_no": '"Nr listy"', "position": '"Nr pozycji"',
            "name": """concat_ws(' ', cap_("1. imię"), cap_("2. imię"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zamieszkania"', "elected": "elected_", "votes": '"Głosy"',
        }
    elif year == 2006:
        con.execute(f"CREATE OR REPLACE TABLE sejmik_cand AS SELECT * FROM {raw} WHERE \"Szczebel\" = 'WO'")
        f = {
            "woj": '"Jednostka"', "district": '"Okręg"', "list_no": '"Lista"', "position": '"Pozycja"',
            "name": """concat_ws(' ', capw_("Imiona"), upper("Nazwisko"))""", "committee": '"Komitet"',
            "residence": '"Miejsce zamieszkania"', "elected": '"Mandat"', "votes": '"Gł. na kand."',
        }
    elif year == 2010:
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
        mandates = column(con, seats, "Liczba mandatów", "L. mand.")
        con.execute(f"CREATE OR REPLACE TABLE sejmik_seats AS SELECT sum(int_({mandates})) AS seats FROM {seats} {seat_filter}")

    if year == 1998:
        gminas = _sejmik_gminas_1998(con, sources)
    elif year == 2002:
        gminas = _sejmik_gminas_2002(con, sources)
    else:
        gminas = sources.load("sejmik_gminas", spec["sejmik_gminas"])
    # 2010/2014 name gminas 'Bolesławiec, m.' (Warsaw districts without suffix); later years 'm. Bolesławiec'.
    gmina_name = (
        f"""regexp_replace({column(con, gminas, "Gmina/dzielnica", "Gmina (dzielnica)")}, '^(.*), (m\\. st\\.|m\\.|gm\\.)$', '\\2 \\1')"""
        if year in (2010, 2014) else '"Gmina"'
    )
    # 2010-2018 gmina files name the district of every row; a gmina split between districts (Łódź
    # 2010) has one row per district. 2024 has the district only per precinct.
    district = f"CAST(int_({column(con, gminas, 'Nr okr.', 'Numer okręgu', 'Okręg')}) AS INT)" if year < 2024 else "CAST(NULL AS INT)"
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


def _gmina_names_2002(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """``gmina_names(teryt_gmina, gmina, powiat)`` of 2002 in the style of later years.

    2002 files name gminas bare, a rural gmina named like its town with a suffix ('Bolesławiec gm.');
    the gmina type of the mayor file (1 urban, 2 rural, 3 urban-rural) gives the prefix of later years
    ('m. Bolesławiec', 'gm. Bolesławiec', 'gm. Nowogrodziec').
    """
    spec = SOURCES[2002]
    gminas = sources.load("gminas", spec["gminas"])
    wbp = sources.load("wbp_cand", spec["wbp_candidates"])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gmina_names AS
        WITH types AS (SELECT clean_teryt("TERYT") AS teryt_gmina, any_value("Typ") AS typ, count(DISTINCT "Typ") AS n
                       FROM {wbp} GROUP BY 1)
        SELECT clean_teryt(g."TERYT gm.") AS teryt_gmina,
               CASE WHEN clean_teryt(g."TERYT gm.") = '146501' THEN 'm. st. Warszawa'
                    WHEN t.typ = '1' THEN 'm. ' || g."Gmina"
                    WHEN t.typ IN ('2', '3') THEN 'gm. ' || regexp_replace(g."Gmina", ' gm\\.$', '') END AS gmina,
               g."Powiat" AS powiat, t.n AS types
        FROM {gminas} g LEFT JOIN types t ON t.teryt_gmina = clean_teryt(g."TERYT gm.")
        """
    )
    run_checks(con, 2002, {
        "gminas without exactly one gmina type or name": """
            SELECT count(*) + (SELECT count(*) - count(DISTINCT teryt_gmina) FROM gmina_names)
            FROM gmina_names WHERE types IS DISTINCT FROM 1 OR gmina IS NULL""",
    })


# 2002 gmina statistics: one column group per election under a two-row header, like 2010. Staging keeps
# the group name as the header of its first column and generic names for the others; the second row
# (first data row, "Gmina" = 'TERYT') labels them and is checked. Columns in PROTOCOL order: eligible,
# ballots issued, votes cast (valid ballots), invalid votes, valid votes. Warsaw has rows per district.
_STATS_2002 = {
    "sejmik": ('"Sejmik województwa"', "column21", "column22", "column24", "column23"),
    "wbp1": ('"Wójt (burmistrz, prezydent miasta) I tura"', "column39", "column40", "column42", "column41"),
    "wbp2": ('"Wójt (burmistrz, prezydent miasta) II tura"', "column47", "column48", "column50", "column49"),
}
_STATS_2002_LABELS = ("upr.", "k. wyd.", "gł. odd.", "gł. niew.", "gł. ważne")


def _stats_2002(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """``stats_2002``: official 2002 gmina statistics of the sejmik and mayor elections (``<group>_<protocol>``)."""
    stats = sources.load("stats", SOURCES[2002]["stats"])
    for group, cols in _STATS_2002.items():
        found = con.execute(f"""SELECT {', '.join(cols)} FROM {stats} WHERE "Gmina" = 'TERYT'""").fetchall()
        if found != [_STATS_2002_LABELS]:
            raise BuildError(f"year 2002: unexpected {group} statistics sub-headers {found}")
    selects = ", ".join(
        f"CAST(int_({c}) AS INT) AS {group}_{p}" for group, cols in _STATS_2002.items() for c, p in zip(cols, PROTOCOL)
    )
    con.execute(f"""CREATE OR REPLACE TABLE stats_2002 AS SELECT clean_teryt("Gmina") AS teryt, {selects}
                    FROM {stats} WHERE "Gmina" <> 'TERYT'""")


def _sejmik_gminas_2002(con: duckdb.DuckDBPyConnection, sources: YearSources) -> str:
    """2002 sejmik results per gmina and district (``sejmik_gminas_2002``), summed from the precinct file.

    The result has the headers of a gmina file: protocol columns, then one column per committee.
    Invalid votes are not published per precinct: votes cast (valid ballots) minus valid votes.
    """
    prec = sources.load("sejmik_prec", SOURCES[2002]["sejmik_precincts"])
    sums = ", ".join(f"CAST(sum(int_(p.{quote_ident(c)})) AS VARCHAR) AS {quote_ident(c)}" for c in _vote_columns(con, prec))
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_gminas_2002 AS
        SELECT clean_teryt(p."Kod gminy") AS "TERYT gminy", p."Nr okr.", any_value(n.gmina) AS "Gmina", any_value(n.powiat) AS "Powiat",
               CAST(sum(int_(p."L. upr.")) AS VARCHAR) AS "Upr.", CAST(sum(int_(p."Frekw.")) AS VARCHAR) AS "Frekw.",
               CAST(sum(int_(p."Odd.")) AS VARCHAR) AS "Odd.", CAST(sum(int_(p."Odd.")) - sum(int_(p."Ważne")) AS VARCHAR) AS "Niew.",
               CAST(sum(int_(p."Ważne")) AS VARCHAR) AS "Ważne", {sums}
        FROM {prec} p LEFT JOIN gmina_names n ON n.teryt_gmina = clean_teryt(p."Kod gminy")
        GROUP BY clean_teryt(p."Kod gminy"), p."Nr okr."
        """
    )
    # Every committee has a column in every precinct, with 0 where it had no list: such zeros are no
    # result (left as 0 they would become empty lists). Non-zero votes without a list are kept and
    # fail the checks.
    for c in _vote_columns(con, prec):
        con.execute(
            f"""UPDATE sejmik_gminas_2002 g SET {quote_ident(c)} = NULL
                WHERE int_(g.{quote_ident(c)}) = 0 AND NOT EXISTS (
                    SELECT 1 FROM sejmik_candidates k JOIN sejmik_committees m USING (committee_id)
                    WHERE k.wojewodztwo = voivodeship(g."TERYT gminy") AND k.district_no = int_(g."Nr okr.")
                      AND m.committee_name = strip_signature(?))""",
            [c],
        )
    return "sejmik_gminas_2002"


def _sejmik_gminas_1998(con: duckdb.DuckDBPyConnection, sources: YearSources) -> str:
    """1998 sejmik results per gmina and district (``sejmik_gminas_1998``), summed from the precinct file.

    The result has the headers of a gmina file: protocol columns, then one column per committee. The
    precinct file has one column per list number; a number names a committee per voivodeship and
    district (``sejmik_list_names_1998``, from the candidates). Ballots issued come from the precinct
    sheets of the per-district workbooks (``sejmik_wb_1998_precincts``, same precincts, see the
    checks); "Frekwencja" is the votes cast (valid ballots). Warsaw gminas get the code of their
    post-2002 district (WARSAW_GMINAS_2001), except Warszawa-Centrum (143104).
    """
    prec = sources.load("sejmik_prec", SOURCES[1998]["sejmik_precincts"])
    sources.load("sejmik_gminas_file", SOURCES[1998]["sejmik_gminas"])  # for the checks
    sources.load("sejmik_mazowieckie", SOURCES[1998]["sejmik_mazowieckie"])  # for the checks
    build_rady.load_district_workbooks_1998(con, sources, SOURCES[1998]["sejmik_workbooks"], "sejmik_wb_1998")
    create_warsaw_gminas_2001(con)
    con.execute(
        f"""CREATE OR REPLACE TABLE sejmik_prec_1998 AS
            SELECT p.*, coalesce(w.teryt, clean_teryt(p."Kod gminy")) AS teryt, coalesce(w.gmina, p."Gmina") AS gmina,
                   CAST(int_(p."Nr okr.") AS INT) AS district_no, int_(b."Wydane karty") AS ballots_issued
            FROM {prec} p LEFT JOIN warsaw_gminas_2001 w ON w.letter IS NULL AND w.old_teryt = clean_teryt(p."Kod gminy")
            LEFT JOIN sejmik_wb_1998_precincts b ON b."Kod gminy" = p."Kod gminy" AND int_(b."Nr obw.") = int_(p."Nr obw.")"""
    )
    cols = ", ".join(quote_ident(c) for c in columns_of(con, prec) if c.isdigit())
    con.execute(
        f"""CREATE OR REPLACE TABLE sejmik_votes_1998 AS
            SELECT teryt, district_no, CAST(list_no AS INT) AS list_no, sum(int_(votes)) AS votes
            FROM (UNPIVOT (SELECT teryt, district_no, {cols} FROM sejmik_prec_1998) ON {cols} INTO NAME list_no VALUE votes)
            WHERE int_(votes) IS NOT NULL
            GROUP BY ALL"""
    )
    con.execute(
        """CREATE OR REPLACE TABLE sejmik_list_names_1998 AS
           SELECT DISTINCT k.wojewodztwo, k.district_no, k.list_no, c.committee_name
           FROM sejmik_candidates k JOIN sejmik_committees c USING (committee_id)"""
    )
    names = [r[0] for r in con.execute("SELECT DISTINCT committee_name FROM sejmik_list_names_1998 ORDER BY 1").fetchall()]
    sums = ", ".join(
        f"CAST(sum(v.votes) FILTER (WHERE n.committee_name = {sql_literal(name)}) AS VARCHAR) AS {quote_ident(name)}" for name in names
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE sejmik_gminas_1998 AS
        WITH prot AS (
            SELECT teryt, district_no, any_value(gmina) AS gmina, any_value("Powiat") AS powiat, sum(int_("Upr.")) AS eligible,
                   sum(ballots_issued) AS issued, sum(int_("Frekwencja")) AS ballots, sum(int_("Nieważne")) AS invalid,
                   sum(int_("Ważne")) AS valid
            FROM sejmik_prec_1998 GROUP BY ALL)
        SELECT p.teryt AS "TERYT gminy", CAST(p.district_no AS VARCHAR) AS "Nr okr.", p.gmina AS "Gmina", p.powiat AS "Powiat",
               CAST(p.eligible AS VARCHAR) AS "Upr.", CAST(p.issued AS VARCHAR) AS "Wydane karty", CAST(p.ballots AS VARCHAR) AS "Odd.",
               CAST(p.invalid AS VARCHAR) AS "Niew.", CAST(p.valid AS VARCHAR) AS "Ważne", {sums}
        FROM prot p
        LEFT JOIN sejmik_votes_1998 v USING (teryt, district_no)
        LEFT JOIN sejmik_list_names_1998 n ON n.wojewodztwo = voivodeship(v.teryt) AND n.district_no = v.district_no AND n.list_no = v.list_no
        GROUP BY p.teryt, p.district_no, p.gmina, p.powiat, p.eligible, p.issued, p.ballots, p.invalid, p.valid
        """
    )
    return "sejmik_gminas_1998"


# --- Mayors -----------------------------------------------------------------------------------


def _build_wbp(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Mayor candidates and gmina protocols of both rounds."""
    year = sources.year
    spec = SOURCES[year]
    if year == 2002:
        _wbp_2002(con, sources)
    elif year == 2006:
        _wbp_2006(con, sources)
    elif year == 2010:
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


def _wbp_2002(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2002 mayor candidates (wbp_cand) and gmina protocols (wbp_prot) of both rounds.

    The candidate file has both rounds (``Tura``); 'Wybrany' is T for the elected candidate only. The
    protocols are the official gmina statistics (``stats_2002``; Warsaw, one mayor, as the sum of its
    district rows). The files publish no percentages and no votes against a single candidate: ``pct``
    is votes / valid votes, and as such a ballot offered only "za" / "przeciw", ``votes_against`` is
    valid votes minus votes for. In the REPLACED_RUNOFF_CANDIDATES gminas a qualified candidate withdrew
    before the run-off and the candidate with the next most votes took the place. Candidates are matched
    across rounds by name and committee (Aleksandrów, 101001, had two candidates named Henryk MAŁECKI).
    """
    spec = SOURCES[2002]
    raw = sources.load("wbp_cand", spec["wbp_candidates"])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_cand AS
        SELECT CAST(int_("Tura") AS INT) AS round, clean_teryt("TERYT") AS teryt_gmina,
               concat_ws(' ', cap_("1. Imie"), cap_("2. Imie"), upper("Nazwisko")) AS name, "Plec" AS gender, int_("Wiek") AS age,
               "Miejsce zamieszkania" AS residence, "Komitet wyborczy" AS committee, CAST(NULL AS VARCHAR) AS party,
               int_("Gł. na kand.") AS votes, CAST(NULL AS BIGINT) AS votes_against,
               CAST(round(100.0 * int_("Gł. na kand.") / NULLIF(int_("Ważne"), 0), 2) AS DECIMAL(6, 2)) AS pct,
               coalesce("Wybrany" = 'T', false) AS elected, int_("Ważne") AS valid_votes, int_("Upr.") AS eligible_voters,
               "Urząd" AS office_code
        FROM {raw}
        """
    )
    protocols = []
    for round_no in (1, 2):
        group = f"wbp{round_no}"
        protocol = ", ".join(f"CAST(sum({group}_{p}) AS INT) AS {p}" for p in PROTOCOL)
        protocols.append(
            f"""SELECT {round_no} AS round, CASE WHEN left(teryt, 4) = '1465' THEN '146501' ELSE teryt END AS teryt_gmina, {protocol}
                FROM stats_2002 WHERE {group}_eligible_voters IS NOT NULL GROUP BY 2"""
        )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_prot AS
        SELECT p.round, p.teryt_gmina, n.gmina, n.powiat,
               (SELECT {_office_case('any_value(c.office_code)')} FROM wbp_cand c
                WHERE c.round = p.round AND c.teryt_gmina = p.teryt_gmina) AS office,
               {', '.join(PROTOCOL)}
        FROM ({' UNION ALL '.join(protocols)}) p LEFT JOIN gmina_names n USING (teryt_gmina)
        """
    )
    con.execute(
        """
        UPDATE wbp_cand c SET votes_against = p.valid_votes - c.votes
        FROM wbp_prot p
        WHERE p.round = c.round AND p.teryt_gmina = c.teryt_gmina
              AND (SELECT count(*) FROM wbp_cand o WHERE o.round = c.round AND o.teryt_gmina = c.teryt_gmina) = 1
        """
    )
    replaced = ", ".join(map(sql_literal, REPLACED_RUNOFF_CANDIDATES[2002]))
    run_checks(con, 2002, {
        "wbp: candidate-file valid votes or eligible voters != gmina statistics": """
            SELECT count(*) FROM wbp_cand c JOIN wbp_prot p USING (round, teryt_gmina)
            WHERE c.valid_votes <> p.valid_votes OR c.eligible_voters <> p.eligible_voters""",
        "wbp: single candidates with negative votes against, or elected != more than half of valid votes": """
            SELECT count(*) FROM wbp_cand WHERE votes_against IS NOT NULL
              AND (votes_against < 0 OR elected <> (2 * votes > votes + votes_against))""",
        "wbp: run-off candidates outside the two with most round-1 votes (except named gminas), or named gminas without": f"""
            WITH ranked AS (SELECT teryt_gmina, name_key(name) || '/' || committee AS k,
                                   rank() OVER (PARTITION BY teryt_gmina ORDER BY votes DESC) AS r
                            FROM wbp_cand WHERE round = 1),
            outsiders AS (SELECT DISTINCT c.teryt_gmina FROM wbp_cand c
                          JOIN ranked r ON r.teryt_gmina = c.teryt_gmina AND r.k = name_key(c.name) || '/' || c.committee
                          WHERE c.round = 2 AND r.r > 2)
            SELECT (SELECT count(*) FROM outsiders WHERE teryt_gmina NOT IN ({replaced}))
                 + (SELECT count(*) FROM (VALUES {', '.join(f'({t})' for t in replaced.split(', '))}) v(t)
                    WHERE t NOT IN (SELECT teryt_gmina FROM outsiders))
                 + (SELECT count(*) FROM wbp_cand c WHERE round = 2
                    AND NOT EXISTS (SELECT 1 FROM ranked r WHERE r.teryt_gmina = c.teryt_gmina AND r.k = name_key(c.name) || '/' || c.committee))""",
    })


# 2006 gmina statistics: both rounds side by side under a two-row header, like 2010 (other columns).
_WBP_2006_PROTOCOL = {
    # round: (eligible_voters, ballots_issued, valid_ballots (votes cast), invalid_votes, valid_votes)
    1: ('"I tura"', "column6", "column8", "column11", "column9"),
    2: ('"II tura"', "column16", "column18", "column21", "column19"),
}


def _wbp_2006(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2006 mayor candidates (wbp_cand) and gmina protocols (wbp_prot) of both rounds.

    The candidate file has both rounds (``Tura``); in round 1 'Mandat' is T (elected), 2 (qualified for
    the run-off) or N. It publishes no percentages: ``pct`` is votes / valid votes. In the 16
    LATE_RUNOFFS gminas a qualified candidate withdrew (in Brześć Kujawski, Mszana Dolna and Dorohusk
    a second one too) and the run-off took place on 10 December: in fifteen the candidate with the next
    most votes took the place, in Dorohusk the remaining candidate faced a for/against vote. Withdrawn
    candidates have no round-2 row. Votes against a single candidate are only in the per-precinct file.
    Gręboszów had no candidate (no election); 'Urząd' of Rajgród's candidate is '42' in the candidate
    file, so the office comes from the gmina statistics.
    """
    spec = SOURCES[2006]
    raw = sources.load("wbp_cand", spec["wbp_candidates"])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE wbp_cand AS
        SELECT CAST(int_("Tura") AS INT) AS round, clean_teryt("TERYT") AS teryt_gmina,
               concat_ws(' ', capw_("Imiona"), upper("Nazwisko")) AS name, "Płeć" AS gender, int_("Wiek") AS age,
               "Miejsce zam." AS residence, "Komitet" AS committee, NULLIF(trim("Partia"), '') AS party,
               int_("Kandydat") AS votes, CAST(NULL AS BIGINT) AS votes_against,
               CAST(round(100.0 * int_("Kandydat") / NULLIF(int_("Ważne"), 0), 2) AS DECIMAL(6, 2)) AS pct,
               coalesce("Mandat" = 'T', false) AS elected, int_("Ważne") AS valid_votes, int_("Upr.") AS eligible_voters
        FROM {raw}
        """
    )
    src = sources.load("wbp_prot", spec["wbp_protocols"])
    for round_no, cols in _WBP_2006_PROTOCOL.items():
        found = con.execute(f"""SELECT column1, column2, {', '.join(cols)} FROM {src} WHERE "Gmina" = 'TERYT'""").fetchall()
        if [tuple(re.sub(r"\s+", " ", v or "") for v in row) for row in found] != [_WBP_2010_PROTOCOL_LABELS]:
            raise BuildError(f"year 2006: unexpected mayor protocol sub-headers {found}")
    protocols = []
    for round_no, cols in _WBP_2006_PROTOCOL.items():
        protocol = ", ".join(f"CAST(int_({c}) AS INT) AS {p}" for c, p in zip(cols, PROTOCOL))
        protocols.append(
            f"""SELECT {round_no} AS round, clean_teryt("Gmina") AS teryt_gmina, column1 AS gmina, column2 AS powiat,
                   {_office_case('"Urząd"')} AS office, {protocol}
            FROM {src} WHERE "Gmina" <> 'TERYT' AND {cols[0]} IS NOT NULL"""
        )
    con.execute(f"CREATE OR REPLACE TABLE wbp_prot AS {' UNION ALL '.join(protocols)}")

    prec = sources.load("wbp_prec", spec["wbp_precincts"])
    con.execute(
        f"""CREATE OR REPLACE TABLE wbp_against AS
            SELECT CAST(int_("Tura") AS INT) AS round, clean_teryt("TERYT") AS teryt_gmina, sum(int_("Gł. przeciw")) AS votes
            FROM {prec} WHERE int_("Gł. przeciw") IS NOT NULL GROUP BY ALL"""
    )
    con.execute(
        """
        UPDATE wbp_cand c SET votes_against = a.votes
        FROM wbp_against a
        WHERE a.round = c.round AND a.teryt_gmina = c.teryt_gmina
              AND (SELECT count(*) FROM wbp_cand o WHERE o.round = c.round AND o.teryt_gmina = c.teryt_gmina) = 1
        """
    )
    late = ", ".join(map(sql_literal, LATE_RUNOFFS[2006]))
    withdrawn = sources.load("wbp_withdrawn", spec["wbp_withdrawn"])
    # Round-1 candidates who qualified (or replaced a qualified candidate) but have no round-2 row.
    con.execute(
        f"""CREATE OR REPLACE TABLE wbp_dropped AS
            SELECT clean_teryt(r."TERYT") AS teryt_gmina, upper(trim(r."Nazwisko")) AS surname, r."Mandat" AS mark FROM {raw} r
            WHERE r."Tura" = '1' AND clean_teryt(r."TERYT") IN (SELECT teryt_gmina FROM wbp_cand WHERE round = 2)
              AND NOT EXISTS (SELECT 1 FROM {raw} o WHERE o."Tura" = '2' AND o."TERYT" = r."TERYT" AND o."Nazwisko" = r."Nazwisko"
                              AND o."Imiona" = r."Imiona")"""
    )
    run_checks(con, 2006, {
        "wbp: votes against in gminas with several candidates": """
            SELECT count(*) FROM wbp_against a
            WHERE (SELECT count(*) FROM wbp_cand c WHERE c.round = a.round AND c.teryt_gmina = a.teryt_gmina) <> 1""",
        "wbp: candidate-file valid votes or eligible voters != gmina statistics": """
            SELECT count(*) FROM wbp_cand c JOIN wbp_prot p USING (round, teryt_gmina)
            WHERE c.valid_votes <> p.valid_votes OR c.eligible_voters <> p.eligible_voters""",
        "wbp: qualified candidates without a run-off row, or run-off candidates who did not qualify, outside the named gminas": f"""
            SELECT (SELECT count(*) FROM wbp_dropped WHERE mark = '2' AND teryt_gmina NOT IN ({late}))
                 + (SELECT count(*) FROM {raw} o WHERE o."Tura" = '2' AND clean_teryt(o."TERYT") NOT IN ({late})
                    AND NOT EXISTS (SELECT 1 FROM {raw} r WHERE r."Tura" = '1' AND r."Mandat" = '2' AND r."TERYT" = o."TERYT"
                                    AND r."Nazwisko" = o."Nazwisko" AND r."Imiona" = o."Imiona"))""",
        "wbp: named late run-offs without a withdrawn candidate listed in 2006-trzecia-tura, or listed ones elsewhere": f"""
            WITH listed AS (SELECT upper(trim("Skreślony kandydat")) AS surname FROM {withdrawn} WHERE "Kod Gminy" IS NOT NULL)
            SELECT (SELECT count(*) FROM (VALUES {', '.join(f'({t})' for t in late.split(', '))}) v(t)
                    WHERE NOT EXISTS (SELECT 1 FROM wbp_dropped d JOIN listed l USING (surname) WHERE d.teryt_gmina = v.t))
                 + (SELECT count(*) FROM listed l
                    WHERE NOT EXISTS (SELECT 1 FROM wbp_dropped d WHERE d.surname = l.surname AND d.teryt_gmina IN ({late})))""",
    })


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
    # 1998: lists whose published district total ('Gł. na listę', equal to the precinct results)
    # exceeds the sum of their candidates' votes. The two UW lists have a missing list position (a
    # struck-off candidate dropped from the register); the SLD list (*, 21 votes) has none.
    1998: [
        ("małopolskie", 2, "KRAJOWY KOMITET WYBORCZY SOJUSZU LEWICY DEMOKRATYCZNEJ"),  # *
        ("śląskie", 11, "ZARZĄD UNII WOLNOŚCI"),
        ("warmińsko-mazurskie", 2, "ZARZĄD UNII WOLNOŚCI"),
    ],
    # 2002: lists whose total in the per-precinct committee results exceeds the sum of their candidates'
    # votes. The per-precinct candidate files (2002sejmiki-kand-obw) give the same list totals and
    # candidate votes for all 1,122 lists, so the gap is in the published data. Eighteen have a missing
    # list position (a struck-off candidate dropped from the files); the eight marked * have none.
    2002: [
        ("dolnośląskie", 4, "KRAJOWY KWW UNIA SAMORZĄDOWA"),
        ("lubuskie", 2, "KRAJOWY KWW UNIA SAMORZĄDOWA"),
        ("mazowieckie", 3, "KW LIGA POLSKICH RODZIN"),
        ("mazowieckie", 5, "KW POLSKIEGO STRONNICTWA LUDOWEGO"),  # *
        ("mazowieckie", 5, "KWW KONFEDERACJA RUCH OBRONY BEZROBOTNYCH RP"),  # *
        ("mazowieckie", 6, "KW ALTERNATYWA PARTIA PRACY"),
        ("mazowieckie", 7, "KW SAMOOBRONA RZECZYPOSPOLITEJ POLSKIEJ"),
        ("mazowieckie", 7, "KWW RAZEM POLSCE"),
        ("mazowieckie", 7, "KW KRAJOWEJ PARTII EMERYTÓW I RENCISTÓW"),  # *
        ("małopolskie", 2, "KW KOMITETU OBRONY POLSKIEJ ZIEMI PLACÓWKA"),  # *
        ("małopolskie", 5, "KW KRAJOWEJ PARTII EMERYTÓW I RENCISTÓW"),
        ("opolskie", 3, "KW MNIEJSZOŚĆ NIEMIECKA"),
        ("podkarpackie", 5, "KW POLSKIEGO STRONNICTWA LUDOWEGO"),  # *
        ("podlaskie", 1, "KWW WSPÓLNA INICJATYWA - POLSKA PODLASIE"),
        ("pomorskie", 5, "KW LIGA POLSKICH RODZIN"),  # *
        ("zachodniopomorskie", 2, "KRAJOWY KWW UNIA SAMORZĄDOWA"),
        ("zachodniopomorskie", 4, "KRAJOWY KWW UNIA SAMORZĄDOWA"),
        ("zachodniopomorskie", 5, "KRAJOWY KWW UNIA SAMORZĄDOWA"),  # *
        ("łódzkie", 6, "KOALICYJNY KW PLATFORMA OBYWATELSKA - PRAWO I SPRAWIEDLIWOŚĆ"),
        ("śląskie", 3, "KW LIGA POLSKICH RODZIN"),
        ("śląskie", 3, "KW KRAJOWEJ PARTII EMERYTÓW I RENCISTÓW"),
        ("śląskie", 3, "KWW KONFEDERACJA RUCH OBRONY BEZROBOTNYCH RP"),  # *
        ("śląskie", 5, "KW RUCHU AUTONOMII ŚLĄSKA"),
        ("śląskie", 6, "KW ALTERNATYWA PARTIA PRACY"),
        ("świętokrzyskie", 1, "KW KONSERWATYWNO-LIBERALNA PARTIA UNIA POLITYKI REALNEJ"),
        ("świętokrzyskie", 5, "KWW RAZEM POLSCE"),
    ],
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
    checks = {
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
                  <> CASE p.teryt_gmina {' '.join(f'WHEN {sql_literal(t)} THEN {n}' for t, n in SEJMIK_SPLIT_GMINAS.get(year, {'': 1}).items())}
                     ELSE 1 END""",
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
    }
    if not _has_mayors(year):
        checks = {name: sql for name, sql in checks.items() if not name.startswith("wbp:")}
    if year == 1998:
        lists = [c for c in columns_of(con, "src_sejmik_prec") if c.isdigit()]
        gminas = "src_sejmik_gminas_file"
        # The gmina file has 0 where the precinct file leaves a list number empty.
        compared = ["Upr.", "Frekwencja", "Nieważne", "Ważne", *lists]
        sums = ", ".join(f"coalesce(sum(int_({quote_ident(c)})), 0) AS {quote_ident(c)}" for c in compared)
        checks["sejmik: 1998 list numbers naming several committees in one district"] = """
            SELECT count(*) - count(DISTINCT (wojewodztwo, district_no, list_no)) FROM sejmik_list_names_1998"""
        checks["sejmik: 1998 precinct list votes without a committee of that number in the district"] = """
            SELECT count(*) FROM sejmik_votes_1998 v
            WHERE v.votes > 0 AND NOT EXISTS (SELECT 1 FROM sejmik_list_names_1998 n WHERE n.wojewodztwo = voivodeship(v.teryt)
                                              AND n.district_no = v.district_no AND n.list_no = v.list_no)"""
        checks["sejmik: 1998 precincts whose votes cast != invalid + valid votes"] = """
            SELECT count(*) FROM src_sejmik_prec WHERE int_("Frekwencja") <> int_("Nieważne") + int_("Ważne")"""
        checks["sejmik: 1998 gmina sums of the precinct file != gmina file (sejmik-gminy-98)"] = f"""
            SELECT count(*) FROM (SELECT clean_teryt("Kod gminy") AS t, {sums} FROM src_sejmik_prec GROUP BY 1) p
            FULL JOIN (SELECT clean_teryt("Kod gminy") AS t, {sums} FROM {gminas} WHERE "Kod gminy" IS NOT NULL GROUP BY 1) g USING (t)
            WHERE {' OR '.join(f'p.{quote_ident(c)} IS DISTINCT FROM g.{quote_ident(c)}' for c in compared)}"""
        checks["sejmik: 1998 district list votes != list totals of the candidate file"] = """
            SELECT count(*) FROM sejmik_district_lists l JOIN sejmik_committees c USING (committee_id)
            FULL JOIN (SELECT lower("Jednostka") AS wojewodztwo, CAST(int_("Nr okr.") AS INT) AS district_no,
                              strip_signature("Komitet") AS committee_name, min(int_("Gł. na listę")) AS lo, max(int_("Gł. na listę")) AS hi
                       FROM sejmik_cand GROUP BY ALL) k USING (wojewodztwo, district_no, committee_name)
            WHERE l.votes IS DISTINCT FROM k.lo OR k.lo <> k.hi"""
        checks["sejmik: 1998 district valid votes != 'Gł. ważne' of the candidate file"] = """
            SELECT count(*) FROM (SELECT wojewodztwo, district_no, sum(votes) AS votes FROM sejmik_district_lists GROUP BY ALL) l
            FULL JOIN (SELECT lower("Jednostka") AS wojewodztwo, CAST(int_("Nr okr.") AS INT) AS district_no,
                              min(int_("Gł. ważne")) AS lo, max(int_("Gł. ważne")) AS hi FROM sejmik_cand GROUP BY ALL) k
              USING (wojewodztwo, district_no)
            WHERE l.votes IS DISTINCT FROM k.lo OR k.lo <> k.hi"""
        checks.update(_checks_workbooks_1998(lists))
    if year == 2002:
        # Precinct sums against the official gmina statistics (Warsaw districts added up to 146501).
        checks["sejmik: 2002 gmina sums of the precinct file != official gmina statistics"] = f"""
            SELECT count(*) FROM sejmik_gmina_protocols p
            FULL JOIN (SELECT CASE WHEN left(teryt, 4) = '1465' THEN '146501' ELSE teryt END AS teryt_gmina,
                              {', '.join(f'sum(sejmik_{x}) AS {x}' for x in PROTOCOL)}
                       FROM stats_2002 GROUP BY 1) s USING (teryt_gmina)
            WHERE {' OR '.join(f'p.{x} IS DISTINCT FROM s.{x}' for x in PROTOCOL)}"""
    if year == 2006:
        # The candidate file repeats each list's district total and the register gives seats per district.
        checks["sejmik: 2006 district list votes != list totals of the candidate file"] = """
            SELECT count(*) FROM sejmik_district_lists l JOIN sejmik_committees c USING (committee_id)
            FULL JOIN (SELECT lower("Jednostka") AS wojewodztwo, CAST(int_("Okręg") AS INT) AS district_no,
                              strip_signature("Komitet") AS committee_name, min(int_("Gł. na listę")) AS lo, max(int_("Gł. na listę")) AS hi
                       FROM sejmik_cand GROUP BY ALL) k USING (wojewodztwo, district_no, committee_name)
            WHERE l.votes IS DISTINCT FROM k.lo OR k.lo <> k.hi"""
        checks["sejmik: 2006 districts whose elected councillors != seats in the district register"] = """
            SELECT count(*) FROM src_sejmik_seats r
            FULL JOIN (SELECT wojewodztwo, district_no, count(*) FILTER (WHERE elected) AS elected FROM sejmik_candidates GROUP BY ALL) k
              ON k.wojewodztwo = lower(r."Wojewóztwo") AND k.district_no = int_(r."Nr okr.")
            WHERE k.elected IS DISTINCT FROM int_(r."L. mand.")"""
        gaps = ", ".join(f"({sql_literal(w)}, {d})" for w, d in SEJMIK_ELIGIBLE_GAPS[2006])
        checks["sejmik: 2006 district sums of the gmina file != district register (eligible voters: except named districts)"] = f"""
            WITH g AS (SELECT voivodeship(teryt_gmina) AS wojewodztwo, district_no, {', '.join(f'sum({x}) AS {x}' for x in PROTOCOL)}
                       FROM sejmik_prot_rows GROUP BY ALL),
            r AS (SELECT lower("Wojewóztwo") AS wojewodztwo, CAST(int_("Nr okr.") AS INT) AS district_no, int_("L. upr.") AS eligible_voters,
                         int_("Frekw.") AS ballots_issued, int_("Gł. odd.") AS valid_ballots, int_("Gł. niew.") AS invalid_votes,
                         int_("Gł. ważne") AS valid_votes FROM src_sejmik_seats)
            SELECT count(*) + (SELECT count(*) FROM g JOIN r USING (wojewodztwo, district_no)
                               WHERE g.eligible_voters = r.eligible_voters AND (wojewodztwo, district_no) IN ({gaps}))
            FROM g FULL JOIN r USING (wojewodztwo, district_no)
            WHERE {' OR '.join(f'g.{x} IS DISTINCT FROM r.{x}' for x in PROTOCOL if x != 'eligible_voters')}
               OR (g.eligible_voters IS DISTINCT FROM r.eligible_voters AND (wojewodztwo, district_no) NOT IN ({gaps}))"""
    run_checks(con, year, checks)
    if _has_mayors(year) and con.execute("SELECT count(*) FROM wbp_gmina_protocols WHERE office IS NULL").fetchone()[0]:
        raise BuildError(f"year {year}: mayor protocols without an office")


def _checks_workbooks_1998(lists: list[str]) -> dict[str, str]:
    """1998 sejmik checks against the per-district workbooks (``sejmik_wb_1998_*``, 1998woj), the
    mazowieckie district summary (``src_sejmik_mazowieckie``) and PKW's national totals; ``lists`` are
    the list-number columns of the precinct file."""
    cols = ", ".join(quote_ident(c) for c in lists)
    # Lists whose candidates' votes in a precinct differ from the list's precinct total.
    short = """(SELECT voivodeship(l.council) AS wojewodztwo, l.district_no, l.list_no
                FROM sejmik_wb_1998_lists l
                LEFT JOIN (SELECT council, district_no, list_no, gmina, precinct_no, sum(votes) AS votes FROM sejmik_wb_1998_candidates
                           GROUP BY ALL) c USING (council, district_no, list_no, gmina, precinct_no)
                WHERE coalesce(c.votes, 0) <> l.votes GROUP BY ALL)"""
    short_named = f"""(SELECT s.wojewodztwo, s.district_no, coalesce(n.committee_name, '') AS committee_name FROM {short} s
                       LEFT JOIN sejmik_list_names_1998 n USING (wojewodztwo, district_no, list_no))"""
    # Mazowieckie summary: district rows (and the voivodeship row, "Nr" = '-'), list rows, candidate rows.
    maz = """FROM src_sejmik_mazowieckie WHERE "Nazwa jednostki administracyjnej" = 'województwo mazowieckie'"""
    gap = " + ".join(f"abs(sum({c}) - {official} - {diff})" for c, (official, diff) in SEJMIK_PKW_GAP_1998.items())
    gap_label = ", ".join(f"{c} {diff:+}" for c, (_, diff) in SEJMIK_PKW_GAP_1998.items())
    return {
        "sejmik: 1998 workbook precincts (1998woj) != precinct file (duplicates, district, eligible, votes cast, valid; ballots issued)": """
            SELECT (SELECT count(*) - count(DISTINCT ("Kod gminy", int_("Nr obw."))) FROM sejmik_wb_1998_precincts) + count(*)
            FROM sejmik_wb_1998_precincts w FULL JOIN src_sejmik_prec p ON p."Kod gminy" = w."Kod gminy" AND int_(p."Nr obw.") = int_(w."Nr obw.")
            WHERE w."Kod gminy" IS NULL OR p."Kod gminy" IS NULL OR w."Kod rady" <> p."Kod rady" OR int_(w."Nr okr.") <> int_(p."Nr okr.")
               OR int_(w."Upr.") <> int_(p."Upr.") OR int_(w."Gł. odd. (frekw.)") <> int_(p."Frekwencja")
               OR int_(w."Gł. ważne") <> int_(p."Ważne") OR int_(w."Wydane karty") IS NULL""",
        "sejmik: 1998 workbook list totals per precinct != precinct file": f"""
            SELECT count(*) FROM sejmik_wb_1998_lists w
            FULL JOIN (SELECT gmina, precinct_no, CAST(list_no AS INT) AS list_no, int_(votes) AS votes
                       FROM (UNPIVOT (SELECT "Kod gminy" AS gmina, int_("Nr obw.") AS precinct_no, {cols} FROM src_sejmik_prec)
                             ON {cols} INTO NAME list_no VALUE votes)) p USING (gmina, precinct_no, list_no)
            WHERE w.votes IS DISTINCT FROM p.votes""",
        "sejmik: 1998 workbook candidates != register (votes, mark, surname)": """
            SELECT count(*) FROM (SELECT voivodeship(council) AS wojewodztwo, district_no, list_no, position AS list_position,
                                         min(name) AS name_lo, max(name) AS name_hi, min(mark) AS mark_lo, max(mark) AS mark_hi,
                                         sum(votes) AS votes
                                  FROM sejmik_wb_1998_candidates GROUP BY ALL) w
            FULL JOIN sejmik_candidates k USING (wojewodztwo, district_no, list_no, list_position)
            WHERE w.votes IS DISTINCT FROM k.votes OR w.name_lo <> w.name_hi OR w.mark_lo <> w.mark_hi
               OR (w.mark_lo = 'W') IS DISTINCT FROM k.elected OR surname(w.name_lo) IS DISTINCT FROM k.last_name""",
        "sejmik: 1998 lists whose candidates' precinct votes != the list's precinct total (except named struck-off lists)": f"""
            SELECT count(*) FROM {short_named} WHERE (wojewodztwo, district_no, committee_name) NOT IN ({_known_struck_off(1998)})""",
        "sejmik: 1998 named struck-off lists whose candidates match the list in every precinct of the workbooks": f"""
            SELECT count(*) FROM (VALUES {_known_struck_off(1998)}) n(wojewodztwo, district_no, committee_name)
            ANTI JOIN {short_named} s USING (wojewodztwo, district_no, committee_name)""",
        "sejmik: 1998 workbook district valid votes (sum of precincts) != 'Gł. ważne' of the candidate file": """
            SELECT count(*) FROM (SELECT voivodeship("Kod rady") AS wojewodztwo, CAST(int_("Nr okr.") AS INT) AS district_no,
                                         sum(int_("Gł. ważne")) AS valid FROM sejmik_wb_1998_precincts GROUP BY ALL) w
            FULL JOIN (SELECT lower("Jednostka") AS wojewodztwo, CAST(int_("Nr okr.") AS INT) AS district_no,
                              min(int_("Gł. ważne")) AS lo, max(int_("Gł. ważne")) AS hi FROM sejmik_cand GROUP BY ALL) k
              USING (wojewodztwo, district_no)
            WHERE w.valid IS DISTINCT FROM k.lo OR k.lo <> k.hi""",
        f"sejmik: 1998 KBW totals minus PKW announcement != named gap ({gap_label})": f"""
            SELECT {gap} FROM sejmik_gmina_protocols""",
        "sejmik: 1998 mazowieckie summary (1998mazowieckie) districts and voivodeship != build (seats, lists, candidates, protocol)": f"""
            WITH m AS (SELECT CAST(int_("Nr") AS INT) AS district_no, int_("Liczba") AS seats, int_(column3) AS lists,
                              int_(column4) AS candidates, int_("Liczba_1") AS eligible, int_("Oddane karty ważne") AS cast_,
                              int_("Oddane głosy - nieważne") AS invalid, int_(column10) AS no_x, int_("ważne") AS valid
                       {maz} AND "Liczba" IS NOT NULL AND ("Nr" = '-' OR int_("Nr") IS NOT NULL)),
            k AS (SELECT district_no, count(*) FILTER (WHERE elected) AS seats, count(DISTINCT list_no) AS lists, count(*) AS candidates
                  FROM sejmik_candidates WHERE wojewodztwo = 'mazowieckie' GROUP BY ALL),
            b AS (SELECT p.district_no, k.seats, k.lists, k.candidates, p.eligible, p.cast_, p.invalid, p.no_x, p.valid
                  FROM (SELECT district_no, sum(int_("Upr.")) AS eligible, sum(int_("Frekwencja")) AS cast_, sum(int_("Nieważne")) AS invalid,
                               sum(int_("0 'x'")) AS no_x, sum(int_("Ważne")) AS valid
                        FROM sejmik_prec_1998 WHERE voivodeship(teryt) = 'mazowieckie' GROUP BY ALL) p FULL JOIN k USING (district_no)),
            x AS (SELECT * FROM b UNION ALL
                  SELECT NULL, sum(seats), sum(lists), sum(candidates), sum(eligible), sum(cast_), sum(invalid), sum(no_x), sum(valid) FROM b)
            SELECT count(*) FROM m FULL JOIN x ON m.district_no IS NOT DISTINCT FROM x.district_no
            WHERE (m.seats, m.lists, m.candidates, m.eligible, m.cast_, m.invalid, m.no_x, m.valid)
                  IS DISTINCT FROM (x.seats, x.lists, x.candidates, x.eligible, x.cast_, x.invalid, x.no_x, x.valid)""",
        "sejmik: 1998 mazowieckie summary list votes != district lists": f"""
            SELECT count(*) FROM (SELECT CAST(int_("Nr") AS INT) AS district_no, CAST(int_("Nr_1") AS INT) AS list_no,
                                         int_("Głosy ważne") AS votes {maz} AND "Nr_1" IS NOT NULL AND "Nr_2" IS NULL) m
            FULL JOIN (SELECT district_no, list_no, votes FROM sejmik_district_lists WHERE wojewodztwo = 'mazowieckie') l
              USING (district_no, list_no)
            WHERE m.votes IS DISTINCT FROM l.votes""",
        "sejmik: 1998 mazowieckie summary candidates != register (votes, elected, surname)": f"""
            SELECT count(*) FROM (SELECT CAST(int_("Nr") AS INT) AS district_no, CAST(int_("Nr_1") AS INT) AS list_no,
                                         CAST(int_("Nr_2") AS INT) AS list_position, upper(trim("Nazwisko")) AS last_name,
                                         int_("Głosy ważne") AS votes, yes_("Wybrany") AS elected {maz} AND "Nr_2" IS NOT NULL) m
            FULL JOIN (SELECT * FROM sejmik_candidates WHERE wojewodztwo = 'mazowieckie') k USING (district_no, list_no, list_position)
            WHERE m.votes IS DISTINCT FROM k.votes OR m.elected IS DISTINCT FROM k.elected OR m.last_name IS DISTINCT FROM k.last_name""",
    }
