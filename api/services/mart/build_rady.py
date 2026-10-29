"""Build the council tables of one local-election year in DuckDB (loader side): rady gmin (including
city councils), rady powiatów and rady dzielnic m.st. Warszawy. Part of the ``samorzad`` family;
called from ``build_samorzad.build_samorzad`` after the sejmik tables (committee aliases are taken
from sejmik committees of the same name).

Sources (KBW mirror):
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

import duckdb

from api.services.mart.build_common import BuildError, YearSources, columns_of, run_checks, sql_literal
from api.services.mart.schema import RADA_TABLES

log = logging.getLogger("mart")

TABLES = RADA_TABLES

SOURCES = {
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
# Largest majoritarian district: 2010 gminas up to 20,000 inhabitants had districts of 1-5 seats;
# single-member districts since 2014.
MAJORITARIAN_MAX_SEATS = {2010: 5}

# Districts where nobody registered, so the seat stayed empty (PKW 2018: two districts in gminas up
# to 20,000 inhabitants; by-elections were held later).
EMPTY_DISTRICTS = {2018: [("gmina", "160913", 6), ("gmina", "321602", 5)]}

# Districts whose candidates' votes fall short of the district's valid votes because votes were cast
# next to a candidate struck off after the ballots were printed (they count for the list only). The
# 2014 and 2018 registers omit such candidates, so the list cannot be identified and the list votes
# of these districts stay NULL. Verified against the per-precinct results: 2018 all levels (one list
# above its candidates per district, two in 142801/1 and three in 146501/1, none elsewhere); 2014
# cities with powiat rights and Warsaw districts (the same lists, none elsewhere). 2014 powiat
# councils have no per-district list results; their districts are named from the register alone.
# Format: council_level -> "teryt/district" items.
STRUCK_OFF_DISTRICTS = {
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
# Candidates whose surname in the source is not a name (2014: '1'), as (council_level, teryt, district).
SURNAME_ERRORS = {2014: [("gmina", "100408", 11), ("gmina", "140705", 19), ("gmina", "142512", 14), ("gmina", "182103", 7)]}


def available(sources: YearSources) -> bool:
    """Whether the year has staged council candidates."""
    spec = SOURCES.get(sources.year)
    return bool(spec) and sources.has(spec.get("candidates") or spec["cand_powiat"])


def build_rady(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """Build and check the council tables of ``sources.year`` (needs ``sejmik_committees``)."""
    year = sources.year
    {2010: _sources_2010, 2014: _sources_2014, 2018: _sources_2018, 2024: _sources_2024}[year](con, sources)
    _build_tables(con, year)
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


def _sources_2010(con: duckdb.DuckDBPyConnection, sources: YearSources) -> None:
    """2010: candidates only (the register is derived from them, as in 2014), plus for the checks the
    valid ballots of majoritarian districts (``rada_ballots``) and the official gmina statistics
    (``rada_stats``)."""
    spec = SOURCES[2010]
    raw = sources.load("rada_cand_raw", spec["candidates"])
    stats = sources.load("rada_stats_raw", spec["gminas"])
    # Gmina statistics: one column group per council type under a two-row header; staging keeps the
    # group names as headers, the second row (first data row) labels the columns.
    columns = columns_of(con, stats)
    groups = {4: "Rada gminy", 13: "Rada powiatu", 31: "Rada dzielnicy"}
    labels = con.execute(f"""SELECT column7, column16, column34 FROM {stats} WHERE "Gmina" = 'TERYT'""").fetchall()
    if [columns[i] for i in groups] != list(groups.values()) or labels != [("gł. ważne",) * 3]:
        raise BuildError(f"year 2010: unexpected council statistics headers {columns} / {labels}")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE rada_stats AS
        SELECT clean_teryt("Gmina") AS teryt, column1 AS gmina, int_(column7) AS gmina_valid, int_(column16) AS powiat_valid,
               int_(column34) AS dzielnica_valid
        FROM {stats} WHERE "Gmina" <> 'TERYT'
        """
    )
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
    # one list with a struck-off candidate when it is the only one in the district.
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
                    WHEN d.candidate_votes = d.valid_votes THEN l.candidate_votes
                    WHEN {year in STRUCK_OFF_IN_REGISTER} AND NOT l.struck THEN l.candidate_votes
                    WHEN {year in STRUCK_OFF_IN_REGISTER} AND d.struck_lists = 1 THEN l.candidate_votes + d.valid_votes - d.candidate_votes
               END AS votes,
               CASE WHEN d.contested THEN l.candidate_votes END AS candidate_votes
        FROM lists l JOIN dist d USING (council_level, teryt, district_no)
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
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({_named(year, EMPTY_DISTRICTS)})""",
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
              AND (r.council_level, r.teryt, r.district_no) NOT IN ({struck_off})""",
        "rada: named struck-off districts that reconcile": f"""
            SELECT count(*) FROM rada_districts r JOIN rada_dist_cand d USING (council_level, teryt, district_no)
            WHERE (r.council_level, r.teryt, r.district_no) IN ({struck_off}) AND d.candidate_votes = r.valid_votes""",
        "rada: candidates without votes in contested districts (registers that omit struck-off candidates)": f"""
            SELECT count(*) FROM rada_candidates k JOIN rada_districts r USING (council_level, teryt, district_no)
            WHERE r.contested AND k.votes IS NULL AND NOT {year in STRUCK_OFF_IN_REGISTER}""",
        "rada: list votes below candidate votes": """
            SELECT count(*) FROM rada_district_lists WHERE votes < candidate_votes""",
        "rada: districts with known list votes not adding up to valid votes": """
            SELECT count(*) FROM rada_districts r
            JOIN (SELECT council_level, teryt, district_no, sum(votes) AS votes, count(*) = count(votes) AS known
                  FROM rada_district_lists GROUP BY ALL) l USING (council_level, teryt, district_no)
            WHERE r.contested AND l.known AND l.votes <> r.valid_votes""",
        "rada: committees with several list numbers in one district": """
            SELECT count(*) FROM (SELECT council_level, teryt, district_no, committee_id FROM rada_candidates
                                  GROUP BY ALL HAVING count(DISTINCT list_no) > 1)""",
        "rada: majoritarian winners with fewer votes than a loser": """
            SELECT count(*) FROM rada_candidates w JOIN rada_districts r USING (council_level, teryt, district_no)
            WHERE r.voting_system = 'majoritarian' AND r.contested AND w.elected
              AND EXISTS (SELECT 1 FROM rada_candidates o WHERE o.council_level = w.council_level AND o.teryt = w.teryt
                          AND o.district_no = w.district_no AND NOT o.elected AND o.votes > w.votes)""",
    }
    if year == 2014:
        for (level, city), (councillors, districts) in OFFICIAL_2014.items():
            unit = f"council_level = '{level}' AND city_with_powiat_rights = {city}"
            checks[f"rada: 2014 {level}{' (cities with powiat rights)' if city else ''}: councillors != PKW {councillors}"] = (
                f"SELECT abs((SELECT sum(seats) FROM rada_districts WHERE {unit}) - {councillors})"
            )
            checks[f"rada: 2014 {level}{' (cities with powiat rights)' if city else ''}: districts != PKW {districts}"] = (
                f"SELECT abs((SELECT count(*) FROM rada_districts WHERE {unit}) - {districts})"
            )
    if year == 2010:
        for (level, city, system), (councillors, districts, councils) in OFFICIAL_2010.items():
            unit = f"council_level = '{level}' AND city_with_powiat_rights = {city} AND voting_system = '{system}'"
            name = f"rada: 2010 {level} {system}{' (cities with powiat rights)' if city else ''}"
            checks[f"{name}: councillors != PKW {councillors}"] = (
                f"SELECT abs((SELECT sum(seats) FROM rada_districts WHERE {unit}) - {councillors})"
            )
            checks[f"{name}: districts != PKW {districts}"] = f"SELECT abs((SELECT count(*) FROM rada_districts WHERE {unit}) - {districts})"
            checks[f"{name}: councils != PKW {councils}"] = (
                f"SELECT abs((SELECT count(DISTINCT teryt) FROM rada_districts WHERE {unit}) - {councils})"
            )
        checks["rada: 2010 majoritarian districts whose valid ballots do not follow from the candidates' percentages"] = """
            SELECT count(*) FROM rada_districts r LEFT JOIN rada_ballots b USING (teryt, district_no)
            WHERE r.voting_system = 'majoritarian' AND r.contested
              AND (b.lo IS DISTINCT FROM b.hi OR r.valid_votes > b.lo * r.seats OR (r.seats = 1 AND r.valid_votes <> b.lo))"""
        # Official valid votes per council: gmina councils (Rada m.st. Warszawy as the sum of its
        # districts' rows), powiat councils as the sum of their gminas, Warsaw district councils.
        checks["rada: 2010 councils whose valid votes (ballots in majoritarian districts) != official gmina statistics"] = """
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
    run_checks(con, year, checks)
    unknown = con.execute("SELECT count(*) FROM rada_district_lists WHERE votes IS NULL AND candidate_votes IS NOT NULL").fetchone()[0]
    log.info("year %s councils: %s lists with unknown list votes (struck-off candidates)", year, unknown)
    if not con.execute("SELECT count(*) FROM rada_districts").fetchone()[0]:
        raise BuildError(f"year {year}: no council districts")
