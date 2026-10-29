"""Build the council tables of one local-election year in DuckDB (loader side): rady gmin (including
city councils), rady powiatów and rady dzielnic m.st. Warszawy. Part of the ``samorzad`` family;
called from ``build_samorzad.build_samorzad`` after the sejmik tables (committee aliases are taken
from sejmik committees of the same name).

Sources (KBW mirror):
- 1998: ``1998-kand-rady`` (candidates of every council with votes, the list's district total ``Gł. na
  listę``, the district's valid votes or, in majoritarian districts, valid ballots ``Gł. ważne``, and
  ``Mand.`` = W (elected by votes), P / L (by precedence / lot in a tie), B (without voting)). No
  district register, as in 2002-2010; gminas up to 20,000 inhabitants elected their councils in
  majoritarian districts of 1-5 seats. A vote marked next to a struck-off candidate counted for the
  list, and the published list totals include such votes. Warsaw had its 1994-2002 structure: eleven
  gminas of powiat warszawski (1431xx, kept: a gmina council is not a district council), Rada m.st.
  Warszawy (KBW code 149900, loaded as 146501) and the councils of the seven districts of gmina
  Warszawa-Centrum (143105-143111, loaded as their post-2002 districts, WARSAW_COUNCILS_1998). Codes
  are those of the 1999 TERYT register.
- 2002: ``2002-kand-rady`` (candidates of every council with votes, ``% gł. odd. na listę``, ``% gł. w
  okręgu`` and ``Mandat`` = G (elected by votes), L (by lot), B (without voting)),
  ``2002-frekwencja-i-nieważne-gminy`` (official valid votes per gmina / Warsaw district for every
  council type) and ``samo2002pow-listy`` (powiat council list totals per district, for a check). No
  district register, as in 2010. Gminas up to 20,000 inhabitants elected their councils in districts of
  1-5 seats (like 2010). A vote marked next to a struck-off candidate counted for the list in 2002, and
  the files omit struck-off candidates, so in the STRUCK_OFF_DISTRICTS a list's total exceeds its
  candidates' votes. List totals are not published outside powiat councils: they are recovered from the
  candidates' ``% gł. odd. na listę`` (votes / list total, 2 decimals; see ``_list_totals_2002``), which
  reproduces all 7,797 published powiat list totals. Warsaw's city council and its 18 district councils
  are the post-2002 structure (no Warszawa-Centrum gmina).
- 2006: ``2006-kand-rady-new`` (candidates of every council with votes, the list's district total
  ``Gł. na listę``, the district's valid votes or, in majoritarian districts, valid ballots ``Ważne``,
  and ``Mandat`` = T / B (elected without voting)) and ``2006-frekwencja-i-nieważne-gminy`` (as in
  2010). No district register; majoritarian districts of 1-5 seats as in 2010. Lists could form blocs
  (grupy list) in proportional elections, sharing the seat allocation; the files do not record blocs
  (PKW published them only on its results pages), and seats are the elected candidates, so the seat
  checks are unaffected.
- 2010: ``2010-kand-rady`` (candidates of every council with votes, ``% gł. w okręgu`` and
  ``Wybrany`` = T / B (elected without voting)) and ``2010-frekwencja-i-niewazne-gminy`` (valid votes
  per gmina / Warsaw district for every council type, used for the checks). There is no district
  register (seats are the elected candidates, national totals are checked against ``OFFICIAL_2010``).
  Gminas up to 20,000 inhabitants elected their councils in majoritarian districts of 1-5 seats where
  every voter had as many votes as seats; ``valid_votes`` of such a district is the votes cast for all
  its candidates, and the number of valid ballots (recovered from the candidates' percentages) is only
  used to check the district against the gmina totals. A vote marked only next to a struck-off
  candidate was invalid in 2010, so list totals are exactly the candidates' votes.
- 2014: ``2014-kand-rady`` (candidates of every council with votes, the district's valid votes in
  ``Ważne`` and ``Mandat`` = T / B (elected without voting)). There is no district register, so a
  district's seats are its elected candidates (PKW: every 2014 seat was filled); national totals are
  checked against the official counts in ``OFFICIAL_2014``.
- 2018: ``2018-kand-rady`` (candidates; the district's valid votes follow from ``% w okr.``) and
  ``okregi`` (district register with seats).
- 2024: ``kandydaci_rady_*`` (one file per council type, with the district's valid votes) and
  ``okregi_rady_*`` (district registers).

List totals are not published per district outside the per-precinct protocols, so they are derived
from the candidates: a list's votes equal its candidates' votes, except that a vote marked next to a
candidate struck off after the ballots were printed counts for the list only. Where the candidates'
votes of a district add up to its valid votes, every list total is exact. Otherwise (struck-off
votes), 2024 registers keep the struck-off candidate with NULL votes, so when exactly one list of the
district has one, the remainder belongs to it; other cases leave the list votes NULL. This was
verified against the per-precinct protocols: 2024 all derived list totals equal the protocols'
list totals; 2018 the 13,249 council lists differ from the protocols only in the districts named in
``STRUCK_OFF_DISTRICTS`` (list totals above candidate votes, never below).
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from api.services.mart.build_common import BuildError, YearSources, columns_of, run_checks, sql_literal
from api.services.mart.build_precincts import WARSAW_DISTRICTS_2005
from api.services.mart.schema import RADA_TABLES

log = logging.getLogger("mart")

TABLES = RADA_TABLES

SOURCES = {
    1998: {"candidates": "**/samorzad/1998-kand-rady.parquet"},
    2002: {
        "candidates": "**/samorzad/2002-kand-rady.parquet",
        "gminas": "**/samorzad/2002-frekwencja-i-niewa*ne-gminy.parquet",
        "powiat_lists": "**/samorzad/rada_powiatu/samo2002pow-listy.parquet",
    },
    2006: {
        "candidates": "**/samorzad/2006-kand-rady-new.parquet",
        "gminas": "**/samorzad/2006-frekwencja-i-niewa*ne-gminy.parquet",
    },
    2010: {
        "candidates": "**/samorzad/samo2010/2010-kand-rady.parquet",
        "gminas": "**/samorzad/samo2010/2010-frekwencja-i-niewazne-gminy.parquet",
    },
    2014: {"candidates": "**/samorzad/2014-kand-rady.parquet"},
    2018: {"candidates": "**/samorzad/2018-kand-rady.parquet", "districts": "**/samorzad/okregi/*.parquet"},
    2024: {
        "cand_gmina_small": "**/samorzad/kandydaci_rady_gmin_do_20k_xlsx/*.parquet",
        "cand_gmina_large": "**/samorzad/kandydaci_rady_gmin_powyzej_20k_csv/*.parquet",
        "cand_powiat": "**/samorzad/kandydaci_rady_powiatow_csv/*.parquet",
        "cand_dzielnica": "**/samorzad/kandydaci_rady_dzielnic_csv/*.parquet",
        "dist_gmina": "**/samorzad/okregi_rady_gmin_csv/*.parquet",
        "dist_powiat": "**/samorzad/okregi_rady_powiatow_csv/*.parquet",
        "dist_dzielnica": "**/samorzad/okregi_rady_dzielnic_csv/*.parquet",
    },
}

# 2014 has no district register: elected councillors and districts per council level are compared
# with the PKW figures ("Wybory samorządowe 2014 - statystyki", pkw.gov.pl, and the PKW report
# "Informacja o przygotowaniu i przeprowadzeniu wyborów ... 16 listopada 2014 r.": 37,842
# single-member gmina districts, 297 districts in cities with powiat rights, 1,332 powiat and 75
# Warsaw district districts; full compositions of all councils were elected).
OFFICIAL_2014 = {
    # (council_level, city_with_powiat_rights): (councillors, districts)
    ("gmina", False): (37842, 37842),
    ("gmina", True): (1694, 297),
    ("powiat", False): (6276, 1332),
    ("dzielnica", False): (423, 75),
}

# 2010 has no district register either: councillors, districts and councils per council type are
# compared with the PKW figures (wybory2010.pkw.gov.pl/geo/pl/000000.html, "Zbiorcze informacje o
# wyborach - Kraj": gminas over 20,000 inhabitants 327 councils / 7,269 councillors / 1,220 districts
# including the 65 cities with powiat rights).
OFFICIAL_2010 = {
    # (council_level, city_with_powiat_rights, voting_system): (councillors, districts, councils)
    ("gmina", False, "majoritarian"): (32280, 20379, 2152),
    ("gmina", False, "proportional"): (5568, 921, 262),
    ("gmina", True, "proportional"): (1701, 299, 65),
    ("powiat", False, "proportional"): (6290, 1345, 314),
    ("dzielnica", False, "proportional"): (409, 70, 18),
}
# 2006 and 2002 have no district register either. PKW announcements on the results of the council
# elections (2006: 15 November 2006, wybory2006.pkw.gov.pl obwieszczenie "wyniki"; 2002: 8 November
# 2002, Dz. U. Nr 187, poz. 1568), seats / districts / councils; cities with powiat rights include
# Rada m.st. Warszawy (2006: 64 cities 1,647 / 289 + Warsaw 60 / 10; 2002: 65 cities 1,685 / 297 +
# Warsaw 60 / 10, Wałbrzych still a city with powiat rights).
OFFICIAL_2006 = {
    ("gmina", False, "majoritarian"): (32265, 20556, 2151),
    ("gmina", False, "proportional"): (5566, 927, 262),
    ("gmina", True, "proportional"): (1707, 299, 65),
    ("powiat", False, "proportional"): (6284, 1346, 314),
    ("dzielnica", False, "proportional"): (407, 69, 18),
}
OFFICIAL_2002 = {
    ("gmina", False, "majoritarian"): (32205, 20692, 2147),
    ("gmina", False, "proportional"): (5629, 938, 265),
    ("gmina", True, "proportional"): (1745, 307, 66),
    ("powiat", False, "proportional"): (6294, 1346, 314),
    ("dzielnica", False, "proportional"): (399, 72, 18),
}
# 1998: PKW announcement of 23 October 1998 (Dz. U. Nr 131, poz. 861): seats to fill / districts /
# councils. Gminas up to 20,000 inhabitants 41,474 / 26,159 / 2,156 (two seats stayed empty, see
# UNFILLED_DISTRICTS and EMPTY_DISTRICTS); over 20,000 7,984 / 1,227 / 268; cities with powiat rights
# 2,921 / 453 / 65 plus Rada m.st. Warszawy 68 / 7 (loaded as 146501, so counted with them, as in
# 2002); powiats 10,290 / 2,058 / 308; Warszawa-Centrum districts 175 / 25 / 7.
OFFICIAL_1998 = {
    ("gmina", False, "majoritarian"): (41474, 26159, 2156),
    ("gmina", False, "proportional"): (7984, 1227, 268),
    ("gmina", True, "proportional"): (2989, 460, 66),
    ("powiat", False, "proportional"): (10290, 2058, 308),
    ("dzielnica", False, "proportional"): (175, 25, 7),
}
# 1998 valid votes per council type in the same announcement (gminas up to 20,000 inhabitants: valid
# ballots); no gmina statistics are published for councils.
OFFICIAL_VALID_1998 = {
    ("gmina", False, "majoritarian"): 5699732,
    ("gmina", False, "proportional"): 3321909,
    ("gmina", True, "proportional"): 3365282 + 514106,
    ("powiat", False, "proportional"): 8780524,
    ("dzielnica", False, "proportional"): 306191,
}
OFFICIAL = {1998: OFFICIAL_1998, 2002: OFFICIAL_2002, 2006: OFFICIAL_2006, 2010: OFFICIAL_2010}
# Largest majoritarian district: 1998-2010 gminas up to 20,000 inhabitants had districts of 1-5 seats;
# single-member districts since 2014.
MAJORITARIAN_MAX_SEATS = {1998: 5, 2002: 5, 2006: 5, 2010: 5}
# Districts where candidates ran but the seat stayed empty: 2006 Markuszów (061407) district 4, where
# no valid vote was cast (PKW: 1 seat not filled; 'wakaty.txt' in the KBW bundle wbp2006.zip); 1998
# Margonin (300104) district 14, where none of the 216 voters voted (KBW per-precinct council file of
# wielkopolskie: 0 ballots; PKW: 2 seats not filled, the other one EMPTY_DISTRICTS). Without a district
# register the seat is added to the elected count.
UNFILLED_DISTRICTS = {1998: [("gmina", "300104", 14)], 2006: [("gmina", "061407", 4)]}
# 1998 Warsaw councils -> loaded code: Rada m.st. Warszawy (KBW pseudo code 149900, which clean_teryt
# reads as abroad) is the city council 146501 (its area lacked Wesoła, a gmina of powiat miński until
# 2002); the district councils of gmina Warszawa-Centrum take the code of the post-2002 district of the
# same name and area (as the 2001 Centrum letters in build_precincts.WARSAW_GMINAS_2001).
WARSAW_COUNCILS_1998 = {
    "149900": "146501", "143105": "146505", "143106": "146506", "143107": "146507", "143108": "146508",
    "143109": "146510", "143110": "146518", "143111": "146519",
}
# 2002 list totals that the candidates' percentages leave open (votes for a struck-off candidate),
# as (council_level, teryt, district_no, list_no): total. Rada m.st. Warszawy district 7, list 18
# (PiS): 19,029-19,030 by the percentages; 19,030 per PKW (district 7 valid votes 64,492 and PiS
# 144,647 in the city, wybory2002.pkw.gov.pl/grada/gw1/w14/m1465/index.html), which also matches the
# official gmina statistics (Targówek, Rembertów, Wawer and Wesoła).
RESOLVED_LIST_TOTALS = {2002: {("gmina", "146501", 7, 18): 19030}}

# Districts where nobody registered, so the seat stayed empty (PKW 2018: two districts in gminas up
# to 20,000 inhabitants; by-elections were held later). 1998: Śniadowo (200707) district 12, missing from
# the candidate file (PKW: no list registered in 1 district); 1 seat, because PKW counts 2 empty seats
# (the other is Margonin, UNFILLED_DISTRICTS) and both councils then have 18 seats, like 866 other 1998
# councils of gminas up to 20,000 inhabitants (no such council has 17).
EMPTY_DISTRICTS = {1998: [("gmina", "200707", 12)], 2018: [("gmina", "160913", 6), ("gmina", "321602", 5)]}
# 1998 districts whose published valid votes exceed the sum of all published list totals, so votes of a
# list that is not in the candidate file are missing: Sochaczew (142801) district 5, 1,609 valid votes,
# 1,532 for its five lists. List votes are exact; the district's valid votes are kept as published.
UNLISTED_VOTES_DISTRICTS = {1998: [("gmina", "142801", 5)]}

# Districts whose candidates' votes fall short of the district's valid votes because votes were cast
# next to a candidate struck off after the ballots were printed (they count for the list only). The
# 2014 and 2018 registers omit such candidates, so the list cannot be identified and the list votes
# of these districts stay NULL. Verified against the per-precinct results: 2018 all levels (one list
# above its candidates per district, two in 142801/1 and three in 146501/1, none elsewhere); 2014
# cities with powiat rights and Warsaw districts (the same lists, none elsewhere). 2014 powiat
# councils have no per-district list results; their districts are named from the register alone.
# 2002: the files omit struck-off candidates too, but the recovered list totals (rada_list_totals) keep
# the list votes; the powiat districts are confirmed by the published list totals (samo2002pow-listy).
# Format: council_level -> "teryt/district" items.
STRUCK_OFF_DISTRICTS = {
    # 1998: one list per district above its candidates; the published list totals keep the list votes.
    1998: {
        "gmina": "026401/3 046101/7 046201/7 046301/8 066201/2 080302/1 086201/8 100101/2 100704/3 106201/4 141804/3 "
        "143103/3 143104/3 143104/12 180301/4 180301/5 180805/2 240101/4 240801/4 241101/3 241304/2 241602/1 "
        "246201/7 246501/7 246901/7 247901/6 301901/2 302604/2 303005/6 306301/4",
        "powiat": "0223/6 0403/6 0419/5 0602/7 0808/5 1012/7 1017/3 1808/5 1811/6 1814/3 1820/1 2409/5 3021/12 3204/2 3213/1",
        "dzielnica": "146505/1",
    },
    2002: {
        "gmina": "022203/2 022503/1 022503/3 026101/3 026201/2 040401/2 086101/5 100607/1 106101/3 106101/4 120402/2 "
        "121103/2 121304/3 121609/3 126101/1 126101/2 126101/3 126101/4 140605/1 141804/2 141804/3 142801/1 146201/1 "
        "146501/1 146501/2 146501/7 146501/8 160101/3 160101/4 166101/3 186201/1 240101/1 246101/4 246101/5 246801/4 "
        "246901/1 260503/3 266101/3 281508/1 301101/1 302006/3 306401/3 306401/5 321001/3 326201/2",
        "powiat": "0223/4 0601/3 0611/2 0812/2 1013/4 1208/2 1208/3 1217/1 1417/2 1438/1 1601/2 1813/2 2214/2 2215/2 2413/2 "
        "3018/1 3209/2",
        "dzielnica": "146505/5 146507/4 146509/2 146511/1",
    },
    2014: {
        "gmina": "066201/2 066401/1 126301/3 146501/3 146501/8 226301/2 246201/5 246601/4 306301/4",
        "powiat": "0219/5 0614/2 0806/3 1003/1 1203/3 1208/2 1212/4 1408/2 1414/4 1435/3 1603/1 1805/2 2204/3 2205/2 "
        "2210/2 2401/1 2818/2 3214/3",
        "dzielnica": "146505/2 146510/5",
    },
    2018: {
        "gmina": "026101/2 066301/6 106101/1 120616/2 142801/1 146501/1 146501/8 186301/4 226101/1 226101/5 226401/4 "
        "246501/3 246801/3 247801/3",
        "powiat": "0209/5 0216/1 0614/4 0809/1 1404/1 1416/4 1434/4 1435/3 2011/3 2415/2 3003/1 3210/3 3211/3",
        "dzielnica": "146505/1",
    },
}
# Years whose registers keep candidates struck off after the ballots were printed (with NULL votes).
STRUCK_OFF_IN_REGISTER = {2024}
# Candidates whose surname in the source is not a name (1998, 2002, 2014: '1'), as (council_level, teryt, district).
SURNAME_ERRORS = {
    1998: [("gmina", "120604", 6), ("gmina", "140705", 3), ("powiat", "1425", 10), ("powiat", "1434", 4), ("gmina", "146301", 8),
           ("gmina", "221209", 3), ("gmina", "260202", 1)],
    2002: [("gmina", "120604", 5), ("gmina", "140705", 2), ("powiat", "1425", 6), ("gmina", "142512", 7), ("gmina", "142512", 10)],
    2014: [("gmina", "100408", 11), ("gmina", "140705", 19), ("gmina", "142512", 14), ("gmina", "182103", 7)]}
# 1998 majoritarian districts where committees of the same name ('KOMITET WYBORCZY', a village
# committee) registered separate lists: the committees are matched by name, so their lists form one
# committee row in rada_district_lists (list_no is the lowest number, votes and seats are added up).
SHARED_NAME_DISTRICTS = {
    1998: [("gmina", "120603", 13), ("gmina", "220608", 14), ("gmina", "221309", 5), ("gmina", "300505", 6), ("gmina", "320901", 12),
           ("gmina", "320902", 1), ("gmina", "320902", 3), ("gmina", "320902", 4), ("gmina", "320902", 6), ("gmina", "321302", 8),
           ("gmina", "321302", 10)],
}
# 1998 numbering (art. 109 and 141 of the act of 16 July 1998): the lists of a committee registered in
# more than one district of a council share one number, and lists registered in a single district are
# numbered after those numbers. The candidate file spells many committees differently from district to
# district, so in proportional councils every number up to the highest one that a committee uses in two
# districts is taken as one committee, named as in most of its districts (_uniform_committees_1998).
# Named exceptions, as (council_level, teryt, list_no): numbers that different committees use in
# different districts although they are not above that highest number, kept as separate committees:
# Kutno (100201) list 11 ('NIEZRZESZENI Z INICJATYWĄ' in districts 2-3, 'KANDYDACI NIEZALEŻNI' in 4)
# and Koluszki (100607) list 8 (three local committees in districts 1-3).
SEPARATE_LIST_NUMBERS = {1998: [("gmina", "100201", 11), ("gmina", "100607", 8)]}
# Numbers above that highest number that one committee spelled differently in every district, taken
# as one committee too: Powiat Dzierżoniowski (0202) list 8 ('ZARZĄD STOWARZYSZENIA PONADPARTYJNEGO
# FORUM' in district 1, 'ZARZĄD STOWARZYSZENIE PONADPARTYJNE FORUM' in 2), whose official seats (1 and 2)
# need its powiat-wide 6.7%; either spelling alone is below the 5% threshold.
UNIFORM_LIST_NUMBERS = {1998: [("powiat", "0202", 8)]}


# 2006 list blocs (grupy list): committees could join their lists in a council or voivodeship assembly
# into a bloc that took part in the seat allocation as one list (see the list_group comments in
# schema.py). The KBW files do not record blocs; PKW published them only on its results pages, from
# which scripts/scrape_list_groups_2006.py made this file (one row per list in a bloc, with the page URL).
LIST_GROUPS = {2006: Path(__file__).resolve().parents[3] / "data" / "pkw_html" / "list_groups_2006.csv"}
# 2006 councils elected proportionally whose PKW page lists no bloc (59 of 659; every voivodeship
# assembly had blocs), as council_level/unit items. Checked: no row in the bloc file, while every other
# proportional council has rows; their official seats also follow plain D'Hondt.
NO_LIST_GROUPS = {
    2006: (
        "dzielnica/146509 dzielnica/146512 dzielnica/146516 dzielnica/146519 gmina/020101 gmina/021001 "
        "gmina/021902 gmina/022405 gmina/041003 gmina/041005 gmina/061701 gmina/081002 gmina/121016 "
        "gmina/121109 gmina/121801 gmina/121904 gmina/141201 gmina/142101 gmina/143801 gmina/160103 "
        "gmina/180304 gmina/180805 gmina/181004 gmina/181904 gmina/200301 gmina/221303 gmina/240205 "
        "gmina/240310 gmina/240802 gmina/240901 gmina/241005 gmina/246301 gmina/247301 gmina/247601 "
        "gmina/247901 gmina/280407 gmina/281001 gmina/300207 gmina/302104 gmina/302604 gmina/302903 "
        "gmina/321401 powiat/0220 powiat/0226 powiat/0410 powiat/1215 powiat/1421 powiat/1609 powiat/2012 "
        "powiat/2203 powiat/2402 powiat/2809 powiat/2811 powiat/2815 powiat/3002 powiat/3004 powiat/3021 "
        "powiat/3203 powiat/3206"
    ),
}

# Bloc members whose list PKW invalidated before the ballots were printed ("Lista unieważniona przed
# wydrukiem kart" on the district pages), so they have no candidates in the KBW files, as
# (council_level, unit, list_no). The bloc keeps its number (Powiat Skierniewicki: bloc 6 = lists 20, 21).
INVALIDATED_GROUP_LISTS = {2006: [("gmina", "186301", 15), ("powiat", "1015", 6)]}


def add_list_groups(con: duckdb.DuckDBPyConnection, year: int, election: str) -> None:
    """Add ``list_group`` to the district lists of ``election`` ('sejmik' or 'rada'): the lowest list
    number of the list's bloc, from LIST_GROUPS (NULL in other years), and check the bloc file."""
    table = f"{election}_district_lists"
    con.execute(f"ALTER TABLE {table} ADD COLUMN list_group INT")
    path = LIST_GROUPS.get(year)
    if path is None:
        return
    con.execute(
        f"""
        CREATE OR REPLACE TABLE list_groups AS
        SELECT coalesce(council_level, '') AS council_level, unit, group_no, list_no, committee_name, source_url
        FROM read_csv({sql_literal(str(path))}, header = true, delim = ',', quote = '"', escape = '"',
                      columns = {{'year': 'INT', 'election': 'VARCHAR', 'council_level': 'VARCHAR', 'unit': 'VARCHAR',
                                 'group_no': 'INT', 'list_no': 'INT', 'committee_name': 'VARCHAR', 'source_url': 'VARCHAR'}})
        WHERE year = {year} AND election = {sql_literal(election)}
        """
    )
    if election == "sejmik":
        match = "t.wojewodztwo = g.unit"
        units = "SELECT DISTINCT '' AS council_level, wojewodztwo AS unit FROM sejmik_district_lists"
    else:
        match = "t.council_level = g.council_level AND t.teryt = g.unit"
        units = "SELECT DISTINCT council_level, teryt AS unit FROM rada_districts WHERE voting_system = 'proportional'"
    con.execute(f"UPDATE {table} t SET list_group = g.group_no FROM list_groups g WHERE {match} AND t.list_no = g.list_no")
    named = NO_LIST_GROUPS.get(year, "").split() if election == "rada" else []
    no_groups = ", ".join(f"({sql_literal(item.split('/')[0])}, {sql_literal(item.split('/')[1])})" for item in named) or "('', '-')"
    invalidated = _named(year, INVALIDATED_GROUP_LISTS) if election == "rada" else _named(year, {})
    matches = f"(SELECT count(DISTINCT committee_id) FROM {table} t WHERE {match} AND t.list_no = g.list_no)"
    run_checks(con, year, {
        f"{election}: bloc rows that do not match exactly one committee's list in their unit (except named invalidated lists)": f"""
            SELECT count(*) FROM list_groups g
            WHERE {matches} <> 1 AND (g.council_level, g.unit, g.list_no) NOT IN ({invalidated})""",
        f"{election}: named invalidated bloc lists that are loaded or not in the bloc file": f"""
            SELECT count(*) FROM (VALUES {invalidated}) v(council_level, unit, list_no)
            WHERE v.unit <> '' AND (SELECT count(*) FROM list_groups g WHERE g.council_level = v.council_level AND g.unit = v.unit
                                    AND g.list_no = v.list_no AND {matches} = 0) <> 1""",
        f"{election}: bloc rows whose committee name differs from the loaded committee": f"""
            SELECT count(DISTINCT (g.council_level, g.unit, g.list_no)) FROM list_groups g
            JOIN {table} t ON {match} AND t.list_no = g.list_no JOIN {election}_committees c USING (committee_id)
            WHERE strip_signature(g.committee_name) <> c.committee_name""",
        f"{election}: blocs with fewer than two lists or not numbered by their lowest list": """
            SELECT count(*) FROM (SELECT council_level, unit, group_no FROM list_groups GROUP BY ALL
                                  HAVING count(*) < 2 OR min(list_no) <> group_no)""",
        f"{election}: lists in more than one bloc": """
            SELECT count(*) FROM (SELECT council_level, unit, list_no FROM list_groups GROUP BY ALL HAVING count(*) > 1)""",
        f"{election}: proportional units without blocs that are not named, or named units with blocs": f"""
            SELECT count(*) FROM ({units}) u
            WHERE EXISTS (SELECT 1 FROM list_groups g WHERE g.council_level = u.council_level AND g.unit = u.unit)
                  = ((u.council_level, u.unit) IN ({no_groups}))""",
        f"{election}: named units without blocs that are not proportional units": f"""
            SELECT count(*) FROM (VALUES {no_groups}) n(council_level, unit)
            WHERE n.unit <> '-' AND NOT EXISTS (SELECT 1 FROM ({units}) u WHERE u.council_level = n.council_level AND u.unit = n.unit)""",
        f"{election}: bloc rows of units that are not proportional units": f"""
            SELECT count(*) FROM list_groups g
            WHERE NOT EXISTS (SELECT 1 FROM ({units}) u WHERE u.council_level = g.council_level AND u.unit = g.unit)""",
    })
    log.info("year %s %s: %s lists in %s blocs", year, election,
             *con.execute("SELECT count(*), count(DISTINCT (council_level, unit, group_no)) FROM list_groups").fetchone())


def available(sources: YearSources) -> bool:
    """Whether the year has staged council candidates."""
    spec = SOURCES.get(sources.year)
    return bool(spec) and sources.has(spec.get("candidates") or spec["cand_powiat"])


def build_rady(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check the council tables of ``sources.year`` (needs ``sejmik_committees``)."""
    year = sources.year
    {1998: _sources_1998, 2002: _sources_2002, 2006: _sources_2006, 2010: _sources_2010, 2014: _sources_2014,
     2018: _sources_2018, 2024: _sources_2024}[year](con, sources)
    _build_tables(con, year)
    add_list_groups(con, year, "rada")
    _check(con, year)


# --- Per-year sources -> rada_cand (one row per candidate) and rada_reg (district register) ----------

# Columns of rada_cand: council_level, teryt, district_no, unit_name, council_name, voting_system,
# list_no, list_position, last_name, first_names, committee, gender, age, residence, votes, elected,
# district_valid. 2014/2018 publish surname and given names separately (taken as they are, so a
# surname without letters in the source stays visible, see SURNAME_ERRORS); 2024 combines them.


def _tidy(expr: str) -> str:
    """SQL: ``expr`` trimmed with single spaces."""
    return f"regexp_replace(trim({expr}), '\\s+', ' ', 'g')"


_UNIT = "CASE WHEN {t} = '146501' THEN 'm. st. Warszawa' ELSE regexp_replace({j}, '^(.*), (m\\. st\\.|m\\.|gm\\.)$', '\\2 \\1') END"


# Official gmina statistics (2002, 2006, 2010): one column group per council type under a two-row
# header; staging keeps the group names as headers, the second row (first data row) labels the columns.
# Year -> (column index -> group name, valid-votes columns of rada gminy, rada powiatu, rada dzielnicy).
_STATS = {
    2002: ({2: "Rada gminy", 11: "Rada powiatu", 29: "Rada dzielnicy"}, ("column5", "column14", "column32")),
    2006: ({4: "Rada gminy", 13: "Rada powiatu", 31: "Rada dzielnicy"}, ("column7", "column16", "column34")),
    2010: ({4: "Rada gminy", 13: "Rada powiatu", 31: "Rada dzielnicy"}, ("column7", "column16", "column34")),
}


def _rada_stats(con: duckdb.DuckDBPyConnection, sources: YearSources, year: int) -> None:
    """``rada_stats(teryt, gmina, gmina_valid, powiat_valid, dzielnica_valid)`` from the official gmina statistics."""
    stats = sources.load("rada_stats_raw", SOURCES[year]["gminas"])
    columns = columns_of(con, stats)
    groups, valid = _STATS[year]
    labels = con.execute(f"""SELECT {', '.join(valid)} FROM {stats} WHERE "Gmina" = 'TERYT'""").fetchall()
    if [columns[i] for i in groups] != list(groups.values()) or labels != [("gł. ważne",) * 3]:
        raise BuildError(f"year {year}: unexpected council statistics headers {columns} / {labels}")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_stats AS
        SELECT clean_teryt("Gmina") AS teryt, column1 AS gmina, int_({valid[0]}) AS gmina_valid, int_({valid[1]}) AS powiat_valid,
               int_({valid[2]}) AS dzielnica_valid
        FROM {stats} WHERE "Gmina" <> 'TERYT'
        """
    )


_DERIVED_REGISTER = """
    CREATE OR REPLACE TABLE rada_reg AS
    SELECT council_level, teryt, district_no, any_value(council_name) AS council_name, any_value(unit_name) AS unit_name,
           any_value(voting_system) AS voting_system, CAST(count(*) FILTER (WHERE elected) AS INT) AS seats,
           bool_or(votes IS NOT NULL) AS contested
    FROM rada_cand GROUP BY ALL
"""


def _add_unfilled_seats(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """One seat for each named UNFILLED_DISTRICTS district (no register: seats are the elected candidates)."""
    for level, teryt, district in UNFILLED_DISTRICTS.get(year, []):
        con.execute(
            "UPDATE rada_reg SET seats = seats + 1 WHERE council_level = ? AND teryt = ? AND district_no = ?", [level, teryt, district]
        )


def _sources_1998(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """1998: candidates only (the register is derived from them, as in 2006), plus the published list
    totals of proportional lists (``rada_list_totals``) and, for the checks, the valid ballots of
    majoritarian districts (``rada_ballots``, published as 'Gł. ważne')."""
    raw = sources.load("rada_cand_raw", SOURCES[1998]["candidates"])
    districts = dict(WARSAW_DISTRICTS_2005.values())
    warsaw = ", ".join(
        f"({sql_literal(old)}, {sql_literal(new)}, {sql_literal(districts.get(new, 'm. st. Warszawa'))})"
        for old, new in WARSAW_COUNCILS_1998.items()
    )
    level = "CASE k.\"Szczebel\" WHEN 'powiat' THEN 'powiat' WHEN 'dzielnica w gminie Warszawa-Centrum' THEN 'dzielnica' ELSE 'gmina' END"
    teryt = "CASE WHEN k.\"Szczebel\" = 'powiat' THEN left(clean_teryt(k.\"TERYT\"), 4) ELSE coalesce(w.teryt, clean_teryt(k.\"TERYT\")) END"
    votes = "CASE WHEN k.\"Mand.\" = 'B' THEN NULL ELSE int_(k.\"Gł. kand.\") END"
    # 'Gł. ważne' is the district's valid votes in proportional districts and its valid ballots in
    # majoritarian ones, where district_valid is the votes cast for all candidates (as in 2006).
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_cand AS
        SELECT {level} AS council_level, {teryt} AS teryt, CAST(int_(k."Nr okr.") AS INT) AS district_no,
               coalesce(w.unit_name, k."Jednostka") AS unit_name, CAST(NULL AS VARCHAR) AS council_name,
               CASE WHEN k."Szczebel" = 'gmina do 20 tys. mieszkańców' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_(k."Nr listy") AS INT) AS list_no, CAST(int_(k."Poz.") AS INT) AS list_position,
               {_tidy('upper(k."Nazwisko")')} AS last_name, {_tidy('capw_(k."Imiona")')} AS first_names,
               k."Komitet" AS committee, k."Płeć" AS gender, int_(k."Wiek") AS age, k."Miejsce zam." AS residence,
               {votes} AS votes, k."Mand." IN ('W', 'P', 'L', 'B') AS elected, int_(k."Gł. na listę") AS list_total,
               int_(k."Gł. ważne") AS published_valid,
               CASE WHEN k."Szczebel" = 'gmina do 20 tys. mieszkańców'
                    THEN sum({votes}) OVER (PARTITION BY k."TERYT", k."Nr okr.") ELSE int_(k."Gł. ważne") END AS district_valid
        FROM {raw} k LEFT JOIN (VALUES {warsaw}) w(old, teryt, unit_name) ON w.old = k."TERYT"
        WHERE k."Szczebel" <> 'województwo'
        """
    )
    _uniform_committees_1998(con)
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_list_totals AS
        SELECT council_level, teryt, district_no, list_no, min(list_total) AS votes
        FROM rada_cand WHERE voting_system = 'proportional' AND votes IS NOT NULL GROUP BY ALL
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_ballots AS
        SELECT council_level, teryt, district_no, min(published_valid) AS lo, max(published_valid) AS hi
        FROM rada_cand WHERE voting_system = 'majoritarian' AND votes IS NOT NULL GROUP BY ALL
        """
    )
    con.execute(_DERIVED_REGISTER)
    _add_unfilled_seats(con, 1998)
    # Districts nobody ran in are not in the candidate file (see EMPTY_DISTRICTS).
    for level, teryt, district in EMPTY_DISTRICTS[1998]:
        con.execute("INSERT INTO rada_reg VALUES (?, ?, ?, NULL, NULL, 'majoritarian', 1, false)", [level, teryt, district])


def _uniform_committees_1998(con: duckdb.DuckDBPyConnection) -> None:
    """Give the lists of one uniform number of a proportional council one committee name (see
    SEPARATE_LIST_NUMBERS and UNIFORM_LIST_NUMBERS); ``rada_uniform`` keeps the numbers and names for
    the checks."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_uniform AS
        WITH lists AS (
            SELECT DISTINCT council_level, teryt, district_no, list_no, strip_signature(committee) AS name
            FROM rada_cand WHERE voting_system = 'proportional'
        ), highest AS (
            SELECT council_level, teryt, max(list_no) AS list_no
            FROM (SELECT council_level, teryt, list_no FROM lists GROUP BY council_level, teryt, list_no, name HAVING count(*) > 1)
            GROUP BY ALL
        ), names AS (
            SELECT l.council_level, l.teryt, l.list_no, l.name, count(*) AS districts
            FROM lists l LEFT JOIN highest h ON h.council_level = l.council_level AND h.teryt = l.teryt
            WHERE l.list_no <= h.list_no OR (l.council_level, l.teryt, l.list_no) IN ({_named(1998, UNIFORM_LIST_NUMBERS)})
            GROUP BY ALL
        )
        SELECT council_level, teryt, list_no, first(name ORDER BY districts DESC, name) AS name, count(*) AS names,
               (council_level, teryt, list_no) IN ({_named(1998, SEPARATE_LIST_NUMBERS)}) AS separate
        FROM names GROUP BY ALL
        """
    )
    con.execute(
        """
        UPDATE rada_cand k SET committee = u.name FROM rada_uniform u
        WHERE NOT u.separate AND u.names > 1 AND k.voting_system = 'proportional' AND k.council_level = u.council_level
          AND k.teryt = u.teryt AND k.list_no = u.list_no
        """
    )


def _sources_2002(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2002: candidates only (the register is derived from them, as in 2010), the recovered list totals
    of proportional districts (``rada_list_totals``), and for the checks the valid ballots of
    majoritarian districts (``rada_ballots``) and the official gmina statistics (``rada_stats``).
    Gmina councils are named by ``gmina_names`` (built by build_samorzad for 2002)."""
    spec = SOURCES[2002]
    raw = sources.load("rada_cand_raw", spec["candidates"])
    _rada_stats(con, sources, 2002)
    level = "CASE k.\"Szczebel\" WHEN 'powiat' THEN 'powiat' WHEN 'dzielnica Warszawy' THEN 'dzielnica' ELSE 'gmina' END"
    teryt = "CASE WHEN k.\"Szczebel\" = 'powiat' THEN left(clean_teryt(k.\"TERYT\"), 4) ELSE clean_teryt(k.\"TERYT\") END"
    votes = "CASE WHEN k.\"Mandat\" = 'B' THEN NULL ELSE int_(k.\"Głosy\") END"
    first_names = _tidy("concat_ws(' ', cap_(k.\"1. imię\"), cap_(k.\"2. imię\"))")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_cand AS
        SELECT {level} AS council_level, {teryt} AS teryt, CAST(int_(k."Okręg") AS INT) AS district_no,
               CASE WHEN k."Szczebel" IN ('powiat', 'dzielnica Warszawy') THEN k."Jednostka" ELSE n.gmina END AS unit_name,
               CAST(NULL AS VARCHAR) AS council_name,
               CASE WHEN k."Szczebel" = 'gmina do 20 tys. mieszkańców' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_(k."Nr listy") AS INT) AS list_no, CAST(int_(k."Nr pozycji") AS INT) AS list_position,
               {_tidy('upper(k."Nazwisko")')} AS last_name, {first_names} AS first_names,
               k."Komitet" AS committee, k."Płeć" AS gender, int_(k."Wiek") AS age, k."Miejsce zamieszkania" AS residence,
               {votes} AS votes, k."Mandat" IN ('G', 'L', 'B') AS elected,
               num_(k."% gł. w okręgu") AS pct, num_(k."% gł. odd. na listę") AS pct_list,
               CAST(NULL AS BIGINT) AS district_valid
        FROM {raw} k LEFT JOIN gmina_names n ON n.teryt_gmina = clean_teryt(k."TERYT")
        WHERE k."Szczebel" <> 'województwo'
        """
    )
    _list_totals_2002(con)
    lists = sources.load("rada_powiat_lists_raw", spec["powiat_lists"])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_powiat_lists AS
        SELECT left(clean_teryt("TERYT"), 4) AS teryt, CAST(int_("Okręg") AS INT) AS district_no, CAST(int_("Nr listy") AS INT) AS list_no,
               int_("Głosy") AS votes
        FROM {lists}
        """
    )
    # district_valid: proportional districts the sum of the list totals; majoritarian districts the
    # votes cast for all candidates (a voter had up to `seats` votes), as in 2010.
    con.execute(
        """
        UPDATE rada_cand k SET district_valid = d.valid
        FROM (SELECT council_level, teryt, district_no, sum(votes) AS valid FROM rada_list_totals GROUP BY ALL
              UNION ALL
              SELECT council_level, teryt, district_no, sum(votes) FROM rada_cand WHERE voting_system = 'majoritarian' GROUP BY ALL) d
        WHERE d.council_level = k.council_level AND d.teryt = k.teryt AND d.district_no = k.district_no
        """
    )
    _ballots_from_pct(con, "majoritarian")
    _ballots_from_pct(con, "proportional", "rada_pct_valid")
    con.execute(_DERIVED_REGISTER)


def _list_totals_2002(con: duckdb.DuckDBPyConnection) -> None:
    """``rada_list_totals``: totals of the 2002 proportional lists.

    A candidate's '% gł. odd. na listę' (2 decimals) is votes / list total, so each candidate with votes
    bounds the list total to an interval; ``lo``/``hi`` is their intersection. The total is the
    candidates' votes unless the interval lies above them (votes for a struck-off candidate, who is
    not in the files), then ``lo``, which must equal ``hi`` except in the named RESOLVED_LIST_TOTALS.
    """
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_list_totals AS
        WITH b AS (
            SELECT council_level, teryt, district_no, list_no, sum(votes) AS candidate_votes,
                   CAST(ceil(max(votes * 100 / (pct_list + 0.005 + 1e-9)) FILTER (WHERE votes > 0 AND pct_list > 0)) AS BIGINT) AS lo,
                   CAST(floor(min(votes * 100 / (pct_list - 0.005 - 1e-9)) FILTER (WHERE votes > 0 AND pct_list > 0)) AS BIGINT) AS hi
            FROM rada_cand WHERE voting_system = 'proportional' AND votes IS NOT NULL GROUP BY ALL)
        SELECT *, CASE WHEN candidate_votes < lo THEN lo ELSE candidate_votes END AS votes FROM b
        """
    )
    for (level, teryt, district, list_no), votes in RESOLVED_LIST_TOTALS[2002].items():
        con.execute(
            "UPDATE rada_list_totals SET votes = ? WHERE council_level = ? AND teryt = ? AND district_no = ? AND list_no = ?",
            [votes, level, teryt, district, list_no],
        )


def _ballots_from_pct(con: duckdb.DuckDBPyConnection, system: str, table: str = "rada_ballots") -> None:
    """``table(council_level, teryt, district_no, lo, hi)``: each candidate's percentage of the district
    (2 decimals) is votes / valid ballots (majoritarian) or votes (proportional), so each gives an
    interval; the intersection must hold exactly one integer."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT council_level, teryt, district_no, CAST(ceil(max(votes * 100 / (pct + 0.005 + 1e-9))) AS BIGINT) AS lo,
               CAST(floor(min(votes * 100 / (pct - 0.005 - 1e-9))) AS BIGINT) AS hi
        FROM rada_cand WHERE voting_system = '{system}' AND votes > 0 AND pct > 0
        GROUP BY ALL
        """
    )


def _sources_2006(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2006: candidates only (the register is derived from them, as in 2010), plus for the checks the
    valid ballots of majoritarian districts (``rada_ballots``, published as 'Ważne') and the official
    gmina statistics (``rada_stats``)."""
    raw = sources.load("rada_cand_raw", SOURCES[2006]["candidates"])
    _rada_stats(con, sources, 2006)
    level = "CASE \"Szczebel\" WHEN 'PO' THEN 'powiat' WHEN 'DZ' THEN 'dzielnica' ELSE 'gmina' END"
    teryt = "CASE WHEN \"Szczebel\" = 'PO' THEN left(clean_teryt(\"TERYT\"), 4) ELSE clean_teryt(\"TERYT\") END"
    votes = "CASE WHEN \"Mandat\" = 'B' THEN NULL ELSE int_(\"Gł. na kand.\") END"
    # 'Ważne' is the district's valid votes in proportional districts and its valid ballots in
    # majoritarian ones, where district_valid is the votes cast for all candidates (as in 2010).
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_cand AS
        SELECT {level} AS council_level, {teryt} AS teryt, CAST(int_("Okręg") AS INT) AS district_no,
               "Jednostka" AS unit_name, CAST(NULL AS VARCHAR) AS council_name,
               CASE WHEN "Szczebel" = 'GW' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_("Lista") AS INT) AS list_no, CAST(int_("Pozycja") AS INT) AS list_position,
               {_tidy('upper("Nazwisko")')} AS last_name, {_tidy('capw_("Imiona")')} AS first_names,
               "Komitet" AS committee, "Płeć" AS gender, int_("Wiek") AS age, "Miejsce zamieszkania" AS residence,
               {votes} AS votes, "Mandat" IN ('T', 'B') AS elected, int_("Gł. na listę") AS list_total, int_("Ważne") AS published_valid,
               CASE WHEN "Szczebel" = 'GW' THEN sum({votes}) OVER (PARTITION BY {teryt}, "Okręg") ELSE int_("Ważne") END AS district_valid
        FROM {raw} WHERE "Szczebel" <> 'WO'
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_ballots AS
        SELECT council_level, teryt, district_no, min(published_valid) AS lo, max(published_valid) AS hi
        FROM rada_cand WHERE voting_system = 'majoritarian' AND votes IS NOT NULL GROUP BY ALL
        """
    )
    con.execute(_DERIVED_REGISTER)
    _add_unfilled_seats(con, 2006)


def _sources_2010(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2010: candidates only (the register is derived from them, as in 2014), plus for the checks the
    valid ballots of majoritarian districts (``rada_ballots``) and the official gmina statistics
    (``rada_stats``)."""
    spec = SOURCES[2010]
    raw = sources.load("rada_cand_raw", spec["candidates"])
    _rada_stats(con, sources, 2010)
    level = "CASE k.\"Szczebel\" WHEN 'powiat' THEN 'powiat' WHEN 'rada dzielnicy' THEN 'dzielnica' ELSE 'gmina' END"
    teryt = "CASE WHEN k.\"Szczebel\" = 'powiat' THEN left(clean_teryt(k.\"TERYT\"), 4) ELSE clean_teryt(k.\"TERYT\") END"
    # Gmina councils are named by the statistics file ('m. Bolesławiec', 'gm. Bolesławiec'); the
    # candidate file has the bare name. Warsaw's city council is not a row there (only its districts).
    unit = (
        "CASE WHEN k.\"Szczebel\" IN ('powiat', 'rada dzielnicy') THEN k.\"Jednostka\" "
        "WHEN clean_teryt(k.\"TERYT\") = '146501' THEN 'm. st. Warszawa' ELSE s.gmina END"
    )
    # district_valid: votes cast for all candidates of the district (in majoritarian districts a voter
    # had up to `seats` votes); the checks compare them with the gmina statistics.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_cand AS
        SELECT {level} AS council_level, {teryt} AS teryt, CAST(int_(k."Okręg") AS INT) AS district_no,
               {unit} AS unit_name, CAST(NULL AS VARCHAR) AS council_name,
               CASE WHEN k."Szczebel" = 'gmina do 20 tyś.' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_(k."Nr listy") AS INT) AS list_no, CAST(int_(k."Pozycja") AS INT) AS list_position,
               {_tidy('upper(k."Nazwisko")')} AS last_name, {_tidy('capw_(k."Imiona")')} AS first_names,
               k."Komitet" AS committee, k."Plec" AS gender, int_(k."Wiek") AS age, k."Miejsce zamieszkania" AS residence,
               int_(k."L. głosów") AS votes, coalesce(k."Wybrany" IN ('T', 'B'), false) AS elected,
               num_(k."% gł. w okręgu") AS pct,
               sum(int_(k."L. głosów")) OVER (PARTITION BY {level}, {teryt}, k."Okręg") AS district_valid
        FROM {raw} k LEFT JOIN rada_stats s ON s.teryt = clean_teryt(k."TERYT") AND k."Szczebel" NOT IN ('powiat', 'rada dzielnicy')
        WHERE k."Szczebel" <> 'sejmik'
        """
    )
    # Valid ballots of a majoritarian district: every candidate's percentage (2 decimals) is votes /
    # ballots, so each gives an interval; the intersection must hold exactly one integer.
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_ballots AS
        SELECT teryt, district_no, CAST(ceil(max(votes * 100 / (pct + 0.005 + 1e-9))) AS BIGINT) AS lo,
               CAST(floor(min(votes * 100 / (pct - 0.005 - 1e-9))) AS BIGINT) AS hi
        FROM rada_cand WHERE voting_system = 'majoritarian' AND votes > 0 AND pct > 0
        GROUP BY ALL
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_reg AS
        SELECT council_level, teryt, district_no, any_value(council_name) AS council_name, any_value(unit_name) AS unit_name,
               any_value(voting_system) AS voting_system, CAST(count(*) FILTER (WHERE elected) AS INT) AS seats,
               bool_or(votes IS NOT NULL) AS contested
        FROM rada_cand GROUP BY ALL
        """
    )


def _sources_2014(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2014: candidates only; the register is derived from them."""
    raw = sources.load("rada_cand_raw", SOURCES[2014]["candidates"])
    level = "CASE \"Szczebel\" WHEN 'powiat' THEN 'powiat' WHEN 'dzielnica Warszawy' THEN 'dzielnica' ELSE 'gmina' END"
    teryt = "CASE WHEN \"Szczebel\" = 'powiat' THEN left(clean_teryt(\"TERYT\"), 4) ELSE clean_teryt(\"TERYT\") END"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_cand AS
        SELECT {level} AS council_level, {teryt} AS teryt, CAST(int_("Nr okr.") AS INT) AS district_no,
               {_UNIT.format(t='clean_teryt("TERYT")', j='"Jednostka"')} AS unit_name, CAST(NULL AS VARCHAR) AS council_name,
               CASE WHEN "Szczebel" = 'gmina niebędąca m.p.p.' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_("Nr listy") AS INT) AS list_no, CAST(int_("Poz.") AS INT) AS list_position,
               {_tidy('upper("Nazwisko")')} AS last_name, {_tidy('capw_("Imiona")')} AS first_names,
               "Komitet" AS committee, "Płeć" AS gender,
               int_("Wiek") AS age, "Miejsce zam." AS residence, int_("Głosy") AS votes,
               coalesce("Mandat" IN ('T', 'B'), false) AS elected, int_("Ważne") AS district_valid
        FROM {raw} WHERE "TERYT" IS NOT NULL AND "Szczebel" <> 'województwo'
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_reg AS
        SELECT council_level, teryt, district_no, any_value(council_name) AS council_name, any_value(unit_name) AS unit_name,
               any_value(voting_system) AS voting_system, CAST(count(*) FILTER (WHERE elected) AS INT) AS seats,
               bool_or(votes IS NOT NULL) AS contested
        FROM rada_cand GROUP BY ALL
        """
    )


def _sources_2018(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2018: one candidate register for every council level and the district register ``okregi``."""
    raw = sources.load("rada_cand_raw", SOURCES[2018]["candidates"])
    level = "CASE \"Szczebel\" WHEN 'powiat' THEN 'powiat' WHEN 'dzielnica' THEN 'dzielnica' ELSE 'gmina' END"
    teryt = "CASE WHEN \"Szczebel\" = 'powiat' THEN left(clean_teryt(\"TERYT\"), 4) ELSE clean_teryt(\"TERYT\") END"
    # The district's valid votes are not a column; every candidate row carries votes / valid votes
    # in percent with full double precision, so the valid votes are recovered exactly.
    valid = "CAST(round(int_(\"Liczba głosów\") * 100 / NULLIF(num_(\"% w okr.\"), 0)) AS BIGINT)"
    given = _tidy("""concat_ws(' ', cap_("Imię"), cap_("Drugie imię"))""")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_cand AS
        SELECT {level} AS council_level, {teryt} AS teryt, CAST(int_("Nr okr.") AS INT) AS district_no,
               {_UNIT.format(t='clean_teryt("TERYT")', j='"Jednostka"')} AS unit_name, CAST(NULL AS VARCHAR) AS council_name,
               CASE WHEN "Szczebel" = 'gmina do 20 tys.' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_("Nr listy") AS INT) AS list_no, CAST(int_("Poz.") AS INT) AS list_position,
               {_tidy('upper("Nazwisko")')} AS last_name, {given} AS first_names,
               "Komitet" AS committee,
               "Płeć" AS gender, int_("Wiek") AS age, "Miejsce zam." AS residence, int_("Liczba głosów") AS votes,
               "Mandat" IN ('T', 'B') AS elected, {valid} AS district_valid
        FROM {raw} WHERE "Szczebel" <> 'województwo'
        """
    )
    reg = sources.load("rada_reg_raw", SOURCES[2018]["districts"])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_reg AS
        SELECT CASE "Szczebel" WHEN 'Rada powiatu' THEN 'powiat' WHEN 'Rada dzielnicy' THEN 'dzielnica' ELSE 'gmina' END AS council_level,
               CASE WHEN "Szczebel" = 'Rada powiatu' THEN left(clean_teryt("TERYT"), 4) ELSE clean_teryt("TERYT") END AS teryt,
               CAST(int_("Numer okręgu") AS INT) AS district_no, "Rada" AS council_name, CAST(NULL AS VARCHAR) AS unit_name,
               CASE WHEN "Szczebel" = 'Rada gminy do 20 tys. mieszkańców' THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_("Liczba mandatów") AS INT) AS seats, CAST(NULL AS BOOLEAN) AS contested
        FROM {reg} WHERE "Szczebel" <> 'Sejmik województwa'
        """
    )


def _sources_2024(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2024: one candidate file and one district register per council type."""
    spec = SOURCES[2024]
    # (council_level, candidates, districts, voting system, unit column, TERYT column, has lists)
    kinds = [
        ("gmina", "cand_gmina_small", None, "majoritarian", '"Gmina"', '"TERYT Gminy"', False),
        ("gmina", "cand_gmina_large", None, "proportional", '"Gmina"', '"TERYT Gminy"', True),
        ("powiat", "cand_powiat", "dist_powiat", "proportional", '"Powiat"', '"TERYT Powiatu"', True),
        ("dzielnica", "cand_dzielnica", "dist_dzielnica", "proportional", '"Dzielnica"', '"TERYT Dzielnicy"', True),
    ]
    selects = []
    for level, cand, _dist, system, unit, teryt_col, has_lists in kinds:
        raw = sources.load(f"rada_{cand}", spec[cand])
        teryt = f"left(clean_teryt({teryt_col}), 4)" if level == "powiat" else f"clean_teryt({teryt_col})"
        lists = ('CAST(int_("Nr listy") AS INT)', 'CAST(int_("Pozycja na liście") AS INT)') if has_lists else ("NULL", "NULL")
        selects.append(
            f"""SELECT '{level}' AS council_level, {teryt} AS teryt, CAST(int_("Nr okręgu") AS INT) AS district_no,
                   {unit} AS unit_name, "Rada" AS council_name, '{system}' AS voting_system,
                   CAST({lists[0]} AS INT) AS list_no, CAST({lists[1]} AS INT) AS list_position,
                   surname("Nazwisko i imiona") AS last_name, given_names("Nazwisko i imiona") AS first_names,
                   "Nazwa komitetu" AS committee, "Płeć" AS gender, int_("Wiek") AS age,
                   "Miejsce zamieszkania" AS residence, int_("Liczba głosów") AS votes, yes_("Czy uzyskał mandat") AS elected,
                   int_("Liczba głosów ważnych oddanych w okręgu") AS district_valid
            FROM {raw}"""
        )
    con.execute(f"CREATE OR REPLACE TABLE rada_cand AS {' UNION ALL '.join(selects)}")
    gmina = sources.load("rada_dist_gmina", spec["dist_gmina"])
    powiat = sources.load("rada_dist_powiat", spec["dist_powiat"])
    dzielnica = sources.load("rada_dist_dzielnica", spec["dist_dzielnica"])
    # Gmina councils elected in single-member districts are those in the "up to 20,000" candidate file.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_reg AS
        SELECT 'gmina' AS council_level, clean_teryt("TERYT Gminy") AS teryt, CAST(int_("Numer okręgu") AS INT) AS district_no,
               "Wybierany organ" AS council_name, "Gmina" AS unit_name,
               CASE WHEN clean_teryt("TERYT Gminy") IN (SELECT teryt FROM rada_cand WHERE voting_system = 'majoritarian')
                    THEN 'majoritarian' ELSE 'proportional' END AS voting_system,
               CAST(int_("Liczba mandatów") AS INT) AS seats, "Sposób wyborów w okręgu" = 'Przeprowadza się głosowanie' AS contested
        FROM {gmina}
        UNION ALL
        SELECT 'powiat', left(clean_teryt("TERYT Powiatu"), 4), CAST(int_("Numer okręgu") AS INT), "Wybierany organ", "Powiat",
               'proportional', CAST(int_("Liczba mandatów") AS INT), NULL
        FROM {powiat}
        UNION ALL
        SELECT 'dzielnica', clean_teryt("TERYT Dzielnicy"), CAST(int_("Numer okręgu") AS INT), "Wybierany organ", "Dzielnica",
               'proportional', CAST(int_("Liczba mandatów") AS INT), "Sposób wyborów w okręgu" = 'Przeprowadza się głosowanie'
        FROM {dzielnica}
        """
    )


# --- Mart tables ----------------------------------------------------------------------------------


def _build_tables(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """rada_committees, rada_candidates, rada_districts and rada_district_lists from rada_cand/rada_reg."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_committees AS
        WITH names AS (SELECT DISTINCT strip_signature(committee) AS committee_name FROM rada_cand)
        SELECT {year} AS year, CAST(row_number() OVER (ORDER BY committee_name) AS INT) AS committee_id, committee_name,
               coalesce((SELECT s.aliases FROM sejmik_committees s WHERE s.committee_name = n.committee_name), '{{}}') AS aliases
        FROM names n
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_candidates AS
        SELECT {year} AS year,
               CAST(row_number() OVER (ORDER BY k.council_level, k.teryt, k.district_no, k.list_no, k.list_position,
                                       k.last_name, k.first_names) AS INT) AS candidate_id,
               k.council_level, k.teryt, k.district_no, c.committee_id, k.list_no, k.list_position,
               concat_ws(' ', NULLIF(k.first_names, ''), NULLIF(k.last_name, '')) AS full_name, k.last_name, k.first_names,
               gender_(k.gender) AS gender, CAST(k.age AS INT) AS age, k.residence, CAST(k.votes AS INT) AS votes, k.elected
        FROM rada_cand k JOIN rada_committees c ON c.committee_name = strip_signature(k.committee)
        """
    )
    # District facts from the candidates: count, whether a vote took place, the district's valid
    # votes (min and max must agree, see the checks) and the candidates' vote sum.
    con.execute(
        """
        CREATE OR REPLACE TABLE rada_dist_cand AS
        SELECT council_level, teryt, district_no, any_value(unit_name) AS unit_name, any_value(council_name) AS council_name,
               any_value(voting_system) AS voting_system, CAST(count(*) AS INT) AS candidates,
               CAST(count(*) FILTER (WHERE elected) AS INT) AS elected, bool_or(votes IS NOT NULL) AS voted,
               min(district_valid) AS valid_min, max(district_valid) AS valid_max, sum(votes) AS candidate_votes
        FROM rada_cand GROUP BY ALL
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_districts AS
        SELECT {year} AS year, r.council_level, r.teryt, r.district_no, coalesce(r.council_name, d.council_name) AS council_name,
               coalesce(d.unit_name, r.unit_name, max(d.unit_name) OVER (PARTITION BY r.council_level, r.teryt)) AS unit_name,
               CASE WHEN r.council_level = 'dzielnica' THEN '1465' ELSE left(r.teryt, 4) END AS teryt_powiat,
               voivodeship(r.teryt) AS wojewodztwo,
               r.council_level = 'gmina' AND substr(r.teryt, 3, 2) >= '61' AS city_with_powiat_rights,
               r.voting_system, r.seats, coalesce(d.candidates, 0) AS candidates,
               coalesce(r.contested, coalesce(d.voted, false)) AS contested,
               CAST(CASE WHEN coalesce(r.contested, coalesce(d.voted, false)) THEN d.valid_max END AS INT) AS valid_votes
        FROM rada_reg r LEFT JOIN rada_dist_cand d USING (council_level, teryt, district_no)
        """
    )
    # List votes: exact where the district's candidate votes reconcile with its valid votes; in 2024
    # (struck-off candidates kept with NULL votes) also lists without a struck-off candidate, and the
    # one list with a struck-off candidate when it is the only one in the district; in 2002 the
    # recovered and in 1998 the published list totals (rada_list_totals).
    recovered = (
        ("LEFT JOIN rada_list_totals lt USING (council_level, teryt, district_no, list_no)",
         "WHEN lt.votes IS NOT NULL THEN lt.votes")
        if year in (1998, 2002) else ("", "")
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_district_lists AS
        WITH lists AS (
            SELECT k.council_level, k.teryt, k.district_no, k.committee_id, min(k.list_no) AS list_no,
                   CAST(count(*) AS INT) AS candidates, CAST(count(*) FILTER (WHERE k.elected) AS INT) AS seats,
                   sum(k.votes) AS candidate_votes, count(*) > count(k.votes) AS struck
            FROM rada_candidates k GROUP BY ALL
        ), dist AS (
            SELECT l.council_level, l.teryt, l.district_no, d.contested, d.valid_votes, sum(l.candidate_votes) AS candidate_votes,
                   count(*) FILTER (WHERE l.struck) AS struck_lists
            FROM lists l JOIN rada_districts d USING (council_level, teryt, district_no) GROUP BY ALL
        )
        SELECT {year} AS year, l.council_level, l.teryt, l.district_no, l.committee_id, l.list_no, l.candidates, l.seats,
               CASE WHEN NOT d.contested THEN NULL
                    {recovered[1]}
                    WHEN d.candidate_votes = d.valid_votes THEN l.candidate_votes
                    WHEN {year in STRUCK_OFF_IN_REGISTER} AND NOT l.struck THEN l.candidate_votes
                    WHEN {year in STRUCK_OFF_IN_REGISTER} AND d.struck_lists = 1 THEN l.candidate_votes + d.valid_votes - d.candidate_votes
               END AS votes,
               CASE WHEN d.contested THEN l.candidate_votes END AS candidate_votes
        FROM lists l JOIN dist d USING (council_level, teryt, district_no) {recovered[0]}
        """
    )


# --- Checks ---------------------------------------------------------------------------------------


def _named(year: int, names: dict[int, list[tuple[str, str, int]]]) -> str:
    """SQL row-value list of (council_level, teryt, district_no) for ``year`` (never matching when empty)."""
    rows = names.get(year) or [("", "", -1)]
    return ", ".join(f"({sql_literal(level)}, {sql_literal(teryt)}, {district})" for level, teryt, district in rows)


def _struck_off(year: int) -> dict[int, list[tuple[str, str, int]]]:
    """STRUCK_OFF_DISTRICTS of ``year`` as (council_level, teryt, district_no) tuples."""
    items = []
    for level, text in STRUCK_OFF_DISTRICTS.get(year, {}).items():
        for item in text.split():
            teryt, district = item.split("/")
            items.append((level, teryt, int(district)))
    return {year: items}


def _check(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Councils must reconcile between the district register, candidates and valid votes."""
    struck_off = _named(year, _struck_off(year))
    unlisted = _named(year, UNLISTED_VOTES_DISTRICTS)
    max_seats = MAJORITARIAN_MAX_SEATS.get(year, 1)
    checks = {
        "rada: duplicate districts in the register": """
            SELECT count(*) - count(DISTINCT (council_level, teryt, district_no)) FROM rada_reg""",
        "rada: candidates in districts missing from the register": """
            SELECT count(*) FROM rada_dist_cand d
            WHERE NOT EXISTS (SELECT 1 FROM rada_districts r
                              WHERE r.council_level = d.council_level AND r.teryt = d.teryt AND r.district_no = d.district_no)""",
        "rada: candidates whose surname has no letter (except named source errors)": f"""
            SELECT count(*) FROM rada_candidates
            WHERE NOT regexp_matches(last_name, '\\p{{L}}') AND (council_level, teryt, district_no) NOT IN ({_named(year, SURNAME_ERRORS)})""",
        "rada: candidates without a committee": """
            SELECT (SELECT count(*) FROM rada_cand) - (SELECT count(*) FROM rada_candidates)""",
        "rada: districts whose elected councillors != seats (except named empty districts)": f"""
            SELECT count(*) FROM rada_districts r
            LEFT JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE coalesce(d.elected, 0) <> r.seats
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({_named(year, EMPTY_DISTRICTS)})
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({_named(year, UNFILLED_DISTRICTS)})""",
        "rada: named unfilled districts with an elected candidate or other than one seat": f"""
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE (r.council_level, r.teryt, r.district_no) IN ({_named(year, UNFILLED_DISTRICTS)}) AND (d.elected > 0 OR r.seats <> 1)""",
        "rada: named empty districts that have candidates": f"""
            SELECT count(*) FROM rada_districts WHERE candidates > 0
              AND (council_level, teryt, district_no) IN ({_named(year, EMPTY_DISTRICTS)})""",
        f"rada: majoritarian districts with seats outside 1-{max_seats} or proportional with fewer than 3": f"""
            SELECT count(*) FROM rada_districts
            WHERE (voting_system = 'majoritarian' AND seats NOT BETWEEN 1 AND {max_seats})
               OR (voting_system = 'proportional' AND seats < 3)""",
        "rada: voting system differs between register and candidates": """
            SELECT count(*) FROM rada_dist_cand d JOIN rada_districts r USING (council_level, teryt, district_no)
            WHERE d.voting_system <> r.voting_system""",
        "rada: uncontested districts with votes or more candidates than seats": """
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE NOT r.contested AND (d.voted OR d.candidates > r.seats OR d.elected <> d.candidates)""",
        "rada: contested districts without valid votes or with disagreeing valid votes": """
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE r.contested AND (d.valid_min IS NULL OR d.valid_min <> d.valid_max OR NOT d.voted)""",
        "rada: candidates' votes above the district's valid votes": """
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE r.contested AND d.candidate_votes > r.valid_votes""",
        "rada: candidates' votes below valid votes without a struck-off candidate (2024) or a named district": f"""
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE r.contested AND d.candidate_votes < r.valid_votes
              AND NOT ({year in STRUCK_OFF_IN_REGISTER} AND r.voting_system = 'proportional'
                       AND EXISTS (SELECT 1 FROM rada_candidates k WHERE k.votes IS NULL AND k.council_level = r.council_level
                                   AND k.teryt = r.teryt AND k.district_no = r.district_no))
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({struck_off})
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({unlisted})""",
        "rada: named struck-off districts that reconcile": f"""
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE (r.council_level, r.teryt, r.district_no) IN ({struck_off}) AND d.candidate_votes = r.valid_votes""",
        "rada: candidates without votes in contested districts (registers that omit struck-off candidates)": f"""
            SELECT count(*) FROM rada_candidates k JOIN rada_districts r USING (council_level, teryt, district_no)
            WHERE r.contested AND k.votes IS NULL AND NOT {year in STRUCK_OFF_IN_REGISTER}""",
        "rada: list votes below candidate votes": """
            SELECT count(*) FROM rada_district_lists WHERE votes < candidate_votes""",
        "rada: districts with known list votes not adding up to valid votes (except named districts with unlisted votes)": f"""
            SELECT count(*) FROM rada_districts r
            JOIN (SELECT council_level, teryt, district_no, sum(votes) AS votes, count(*) = count(votes) AS known
                  FROM rada_district_lists GROUP BY ALL) l USING (council_level, teryt, district_no)
            WHERE r.contested AND l.known AND l.votes <> r.valid_votes
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({unlisted})""",
        "rada: named districts with unlisted votes whose list votes are unknown or not below valid votes": f"""
            SELECT count(*) FROM (VALUES {unlisted}) n(council_level, teryt, district_no)
            LEFT JOIN rada_districts r USING (council_level, teryt, district_no)
            LEFT JOIN (SELECT council_level, teryt, district_no, sum(votes) AS votes, count(*) = count(votes) AS known
                       FROM rada_district_lists GROUP BY ALL) l USING (council_level, teryt, district_no)
            WHERE n.teryt <> '' AND (r.teryt IS NULL OR NOT l.known OR l.votes >= r.valid_votes)""",
        "rada: committees with several list numbers in one district (except named majoritarian districts)": f"""
            SELECT count(*) FROM (SELECT council_level, teryt, district_no, committee_id FROM rada_candidates
                                  GROUP BY ALL HAVING count(DISTINCT list_no) > 1)
            WHERE (council_level, teryt, district_no) NOT IN ({_named(year, SHARED_NAME_DISTRICTS)})""",
        "rada: named districts with committees of one name that are not majoritarian or have no such committee": f"""
            SELECT count(*) FROM (VALUES {_named(year, SHARED_NAME_DISTRICTS)}) n(council_level, teryt, district_no)
            WHERE n.teryt <> '' AND NOT EXISTS (
                SELECT 1 FROM rada_candidates k JOIN rada_districts r USING (council_level, teryt, district_no)
                WHERE (k.council_level, k.teryt, k.district_no) = (n.council_level, n.teryt, n.district_no)
                  AND r.voting_system = 'majoritarian' GROUP BY k.committee_id HAVING count(DISTINCT k.list_no) > 1)""",
        "rada: majoritarian winners with fewer votes than a loser": """
            SELECT count(*) FROM rada_candidates w JOIN rada_districts r USING (council_level, teryt, district_no)
            WHERE r.voting_system = 'majoritarian' AND r.contested AND w.elected
              AND EXISTS (SELECT 1 FROM rada_candidates o WHERE o.council_level = w.council_level AND o.teryt = w.teryt
                          AND o.district_no = w.district_no AND NOT o.elected AND o.votes > w.votes)""",
    }
    if year == 1998:
        checks["rada: named separate list numbers that are not uniform numbers of several committees"] = f"""
            SELECT count(*) FROM (VALUES {_named(year, SEPARATE_LIST_NUMBERS)}) n(council_level, teryt, list_no)
            LEFT JOIN rada_uniform u USING (council_level, teryt, list_no) WHERE coalesce(u.names, 0) < 2"""
        checks["rada: named uniform list numbers that are not spelled differently in several districts"] = f"""
            SELECT count(*) FROM (VALUES {_named(year, UNIFORM_LIST_NUMBERS)}) n(council_level, teryt, list_no)
            LEFT JOIN rada_uniform u USING (council_level, teryt, list_no) WHERE coalesce(u.names, 0) < 2"""
    if year == 2014:
        for (level, city), (councillors, districts) in OFFICIAL_2014.items():
            unit = f"council_level = '{level}' AND city_with_powiat_rights = {city}"
            checks[f"rada: 2014 {level}{' (cities with powiat rights)' if city else ''}: councillors != PKW {councillors}"] = (
                f"SELECT abs((SELECT sum(seats) FROM rada_districts WHERE {unit}) - {councillors})"
            )
            checks[f"rada: 2014 {level}{' (cities with powiat rights)' if city else ''}: districts != PKW {districts}"] = (
                f"SELECT abs((SELECT count(*) FROM rada_districts WHERE {unit}) - {districts})"
            )
    if year in OFFICIAL:
        for (level, city, system), (councillors, districts, councils) in OFFICIAL[year].items():
            unit = f"council_level = '{level}' AND city_with_powiat_rights = {city} AND voting_system = '{system}'"
            name = f"rada: {year} {level} {system}{' (cities with powiat rights)' if city else ''}"
            checks[f"{name}: councillors != PKW {councillors}"] = (
                f"SELECT abs((SELECT sum(seats) FROM rada_districts WHERE {unit}) - {councillors})"
            )
            checks[f"{name}: districts != PKW {districts}"] = f"SELECT abs((SELECT count(*) FROM rada_districts WHERE {unit}) - {districts})"
            checks[f"{name}: councils != PKW {councils}"] = (
                f"SELECT abs((SELECT count(DISTINCT teryt) FROM rada_districts WHERE {unit}) - {councils})"
            )
        ballots = "published 'Ważne'" if year in (1998, 2006) else "the candidates' percentages"
        checks[f"rada: {year} majoritarian districts whose valid ballots do not follow from {ballots}"] = """
            SELECT count(*) FROM rada_districts r LEFT JOIN rada_ballots b USING (teryt, district_no)
            WHERE r.voting_system = 'majoritarian' AND r.contested
              AND (b.lo IS DISTINCT FROM b.hi OR r.valid_votes > b.lo * r.seats OR (r.seats = 1 AND r.valid_votes <> b.lo))"""
    for (level, city, system), valid in (OFFICIAL_VALID_1998.items() if year == 1998 else []):
        unit = f"r.council_level = '{level}' AND r.city_with_powiat_rights = {city} AND r.voting_system = '{system}'"
        checks[f"rada: 1998 {level} {system}{' (cities with powiat rights)' if city else ''}: valid votes (ballots) != PKW {valid}"] = f"""
            SELECT abs(sum(CASE WHEN NOT r.contested THEN 0 WHEN r.voting_system = 'majoritarian' THEN b.lo ELSE r.valid_votes END) - {valid})
            FROM rada_districts r LEFT JOIN rada_ballots b ON r.voting_system = 'majoritarian' AND b.teryt = r.teryt
                 AND b.district_no = r.district_no
            WHERE {unit}"""
    if year in _STATS:
        # Official valid votes per council: gmina councils (Rada m.st. Warszawy as the sum of its
        # districts' rows), powiat councils as the sum of their gminas, Warsaw district councils.
        checks[f"rada: {year} councils whose valid votes (ballots in majoritarian districts) != official gmina statistics"] = """
            WITH built AS (
                SELECT r.council_level, r.teryt,
                       sum(CASE WHEN NOT r.contested THEN 0 WHEN r.voting_system = 'majoritarian' THEN b.lo ELSE r.valid_votes END) AS v
                FROM rada_districts r LEFT JOIN rada_ballots b ON r.voting_system = 'majoritarian' AND b.teryt = r.teryt
                     AND b.district_no = r.district_no
                GROUP BY ALL),
            official AS (
                SELECT 'gmina' AS council_level, CASE WHEN left(teryt, 4) = '1465' THEN '146501' ELSE teryt END AS teryt,
                       sum(gmina_valid) AS v FROM rada_stats GROUP BY ALL
                UNION ALL
                SELECT 'powiat', left(teryt, 4), sum(powiat_valid) FROM rada_stats WHERE substr(teryt, 3, 2) < '61' GROUP BY ALL
                UNION ALL
                SELECT 'dzielnica', teryt, dzielnica_valid FROM rada_stats WHERE left(teryt, 4) = '1465')
            SELECT count(*) FROM built b FULL JOIN official o USING (council_level, teryt)
            WHERE coalesce(b.v, 0) <> coalesce(o.v, 0)"""
    if year in (1998, 2006):
        checks[f"rada: {year} proportional lists whose votes != published list total ('Gł. na listę')"] = """
            SELECT count(*) FROM rada_district_lists l
            JOIN (SELECT DISTINCT council_level, teryt, district_no, list_no, list_total FROM rada_cand
                  WHERE voting_system = 'proportional') k USING (council_level, teryt, district_no, list_no)
            JOIN rada_districts r USING (council_level, teryt, district_no)
            WHERE r.contested AND l.votes IS DISTINCT FROM k.list_total"""
    if year == 2002:
        resolved = ", ".join(
            f"({sql_literal(level)}, {sql_literal(teryt)}, {district}, {list_no}, {votes})"
            for (level, teryt, district, list_no), votes in RESOLVED_LIST_TOTALS[2002].items()
        )
        checks["rada: 2002 proportional districts whose valid votes are outside the candidates' percentages"] = """
            SELECT count(*) FROM rada_districts r LEFT JOIN rada_pct_valid p USING (council_level, teryt, district_no)
            WHERE r.voting_system = 'proportional' AND r.contested AND coalesce(r.valid_votes NOT BETWEEN p.lo AND p.hi, true)"""
        checks["rada: 2002 list totals not determined by the candidates' percentages (except named)"] = f"""
            SELECT count(*) FROM rada_list_totals
            WHERE candidate_votes < lo AND lo <> hi AND (council_level, teryt, district_no, list_no) NOT IN
                  ({", ".join(f"({sql_literal(k[0])}, {sql_literal(k[1])}, {k[2]}, {k[3]})" for k in RESOLVED_LIST_TOTALS[2002])})"""
        checks["rada: 2002 named list totals outside the percentages' interval or not open"] = f"""
            SELECT count(*) FROM (VALUES {resolved}) v(l, t, d, n, total)
            LEFT JOIN rada_list_totals x ON x.council_level = v.l AND x.teryt = v.t AND x.district_no = v.d AND x.list_no = v.n
            WHERE x.lo IS NULL OR x.lo = x.hi OR v.total NOT BETWEEN x.lo AND x.hi OR x.votes <> v.total"""
        checks["rada: 2002 powiat list votes != published list totals (samo2002pow-listy)"] = """
            SELECT count(*) FROM rada_district_lists l FULL JOIN rada_powiat_lists p
                 ON l.council_level = 'powiat' AND p.teryt = l.teryt AND p.district_no = l.district_no AND p.list_no = l.list_no
            WHERE (l.council_level = 'powiat' OR l.council_level IS NULL) AND l.votes IS DISTINCT FROM p.votes"""
    run_checks(con, year, checks)
    unknown = con.execute("SELECT count(*) FROM rada_district_lists WHERE votes IS NULL AND candidate_votes IS NOT NULL").fetchone()[0]
    log.info("year %s councils: %s lists with unknown list votes (struck-off candidates)", year, unknown)
    if not con.execute("SELECT count(*) FROM rada_districts").fetchone()[0]:
        raise BuildError(f"year {year}: no council districts")
