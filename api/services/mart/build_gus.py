"""Build the GUS context statistics of one year (population, area, unemployment, salaries) in DuckDB.

Not an election: the ``gus`` family loads official statistics of Statistics Poland (GUS) per territorial
unit and year so that election results can be related to them (e.g. support for a party against the
unemployment rate of the powiat). It writes no ``mart.elections`` rows.

Indicators (``INDICATORS``): population (total, urban, rural), area, density, registered unemployment,
salaries, age structure (shares of pre-working, working and post-working age), gmina budget income per
resident, and census education (NSP 2021 by gmina, NSP 2011 by powiat: residents aged 13+ and with
higher education; NSP 2002 is not published in BDL by territorial unit).

Source: ``data/gus/bdl_values.parquet`` and ``data/gus/bdl_variables.csv``, fetched from the GUS Local
Data Bank API (Bank Danych Lokalnych, https://bdl.stat.gov.pl/api/v1/) by ``scripts/fetch_gus_bdl.py``
and committed. One row per BDL variable, year and territorial unit; ``teryt`` is derived from the BDL
unit id by ``bdl_unit_teryt``.

Units: Poland (BDL level 0), voivodeships (2), powiats incl. cities with powiat rights (5) and gminas
(6). Of the gmina-level units the mart keeps gminas (TERYT type digit 1 urban, 2 rural, 3
urban-rural) and Warsaw's districts (8); the town and rural parts of urban-rural gminas (4, 5) are not
loaded (``GMINA_TYPES``; their totals are the urban/rural population of the gmina). Warsaw is
both the city 146501 (type 1, also the powiat 1465) and its 18 districts 1465xx, as in the election
tables. Units with gmina digits 00 at gmina level are city totals, not gminas, and are skipped: "M.st.
Warszawa do 2001" (the city total of Warsaw's gminas until 2001) and the "Miejska strefa usług publicznych"
units of a few large cities (1997-1999). Warsaw's pseudo-units in ``NOT_GMINAS`` (e.g. "GMINY-DZIELNICY
WARSZAWY NIE USTALONO") are not loaded either, but count when gminas are added up.

Checks: one value per (year, level, unit, indicator); every TERYT appears in the loaded election
tables (``mart`` geography) or in ``NOT_IN_ELECTIONS``; additive indicators of gminas add up to their
powiat, powiats to their voivodeship and voivodeships to Poland, Warsaw's districts to the city;
urban + rural population = population; age shares sum to 100; census higher education <= residents 13+.
GUS publishes no unemployment figures and no budget income for Warsaw's districts and no district areas
for 2006-2007 (``NOT_PUBLISHED_FOR_DISTRICTS``; BDL returns zeros), so the mart has none.
"""

from __future__ import annotations

import csv
import logging
import re
from functools import lru_cache
from pathlib import Path

import duckdb
import psycopg

from api.services.config import DATABASE_URL
from api.services.mart.build_common import VOIVODESHIPS, BuildError, Election, YearSources, run_checks, sql_literal

log = logging.getLogger("mart")

TABLES = ("gus_units", "gus_indicators")
FAMILY: tuple[str, ...] = ()  # statistics, not an election: no mart.elections rows
YEARS = tuple(range(1995, 2025))
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "gus"
VALUES_FILE = DATA_DIR / "bdl_values.parquet"
VARIABLES_FILE = DATA_DIR / "bdl_variables.csv"

# Indicator name -> (BDL variable id, unit stored in mart, additive: children sum to their parent).
INDICATORS = {
    "population": (60641, "persons", True),
    "population_urban": (60617, "persons", True),
    "population_rural": (60633, "persons", True),
    "area_ha": (1, "ha", True),
    "population_density": (60559, "persons per km2", False),
    "unemployed_registered": (10514, "persons", True),
    "unemployed_share_working_age": (79214, "percent", False),
    "unemployment_rate": (60270, "percent", False),
    "avg_gross_salary": (64428, "PLN", False),
    "age_preworking_pct": (60567, "percent", False),
    "age_working_pct": (60566, "percent", False),
    "age_postworking_pct": (60565, "percent", False),
    "gmina_income_per_capita": (76973, "PLN", False),
    # Census education (NSP): residents aged 13+ and those with higher education. BDL publishes NSP 2021
    # down to gminas and NSP 2011 down to powiats; NSP 2002 is not in BDL by territorial unit.
    "nsp2021_population_13plus": (1671256, "persons", True),
    "nsp2021_higher_education": (1671257, "persons", True),
    "nsp2011_population_13plus": (396081, "persons", True),
    "nsp2011_higher_education": (396078, "persons", True),
}
# Additive indicators whose parts may differ from the total by at most this many persons: NSP 2011
# education comes from the census sample survey, and GUS rounds each unit's estimate separately
# (powiats differ from their voivodeship by up to 3 persons in 13 voivodeships).
SAMPLE_ROUNDING = {"nsp2011_higher_education": 3}
# BDL level -> mart level.
LEVELS = {0: "country", 2: "wojewodztwo", 5: "powiat", 6: "gmina"}
# TERYT type digit (last digit of a 7-digit TERYT) of the gmina-level units loaded -> gmina_type.
GMINA_TYPES = {"1": "miejska", "2": "wiejska", "3": "miejsko-wiejska", "8": "dzielnica Warszawy"}
# BDL attributes (GET /api/v1/attributes) of a published value; 0 is a zero ('-' in GUS tables), except
# where GUS publishes nothing for a unit (NOT_PUBLISHED_FOR_DISTRICTS, NOT_GMINAS).
VALUE_ATTRIBUTES = frozenset({0, 1, 3, 7, 9, 11, 13, 17, 20, 21, 94, 97, 98})
# BDL attributes meaning "no value": 4/14/91 'x' (no information or confidential), 15 '-' (unit or level
# changed), 50 'n' (not yet available). scripts/fetch_gus_bdl.py drops such records and counts them.
MISSING_ATTRIBUTES = frozenset({4, 14, 15, 50, 91})
# Gmina-level GUS units of Warsaw that are not gminas or districts, with no residents of their own in
# the population figures. Not loaded, but counted when gminas are added up to their powiat or city.
NOT_GMINAS = {
    "143198": "Związek gmin dzielnic Warszawy do 2001 (the union of Warsaw's gminas until 2001)",
    "143199": "GMINY-DZIELNICY WARSZAWY NIE USTALONO do 2001 (Warsaw, no known gmina)",
    "146599": "GMINY-DZIELNICY WARSZAWY NIE USTALONO (Warsaw, no known district; 2010-)",
}
# Indicators GUS does not publish for Warsaw's districts -> years (None: every year). BDL returns them as
# 0 with attribute 0, which would read as "no unemployed" or "no area". Unemployment: the labour office
# covers the whole city; area: missing in 2006-2007 only; gmina budget income: the city's budget. Dropped for districts; the build fails if any
# of them is non-zero, i.e. if GUS starts publishing them.
NOT_PUBLISHED_FOR_DISTRICTS: dict[str, tuple[int, ...] | None] = {
    "unemployed_registered": None,
    "unemployed_share_working_age": None,
    "area_ha": (2006, 2007),
    "gmina_income_per_capita": None,  # districts have no budgets of their own: the city's budget covers them
}
# TERYT codes in the GUS data that only some loaded elections use, with the reason; accepted even when
# those elections are not loaded. Any other code missing from the election tables fails the build.
NOT_IN_ELECTIONS = {
    "1431": "powiat warszawski (Warsaw) until 2001; only the 1998 local elections use it (2000/2001: districts 1465xx)",
    **{
        teryt: f"Warszawa-{name}, a gmina of Warsaw until 2001; used by the 1998 local elections, while the 2000/2001 "
        "elections use Warsaw's districts 1465xx"
        for teryt, name in {
            "143101": "Bemowo", "143102": "Białołęka", "143103": "Bielany", "143104": "Centrum",
            "143112": "Rembertów", "143113": "Targówek", "143114": "Ursus", "143115": "Ursynów",
            "143116": "Wawer", "143117": "Wilanów", "143118": "Włochy",
        }.items()
    },
}

_UNIT_ID = re.compile(r"\d{12}")


def bdl_unit_teryt(unit_id: str, level: int) -> str:
    """TERYT code of a BDL territorial unit id at BDL ``level`` ('' for Poland).

    BDL ids have 12 digits: macroregion (2), voivodeship (2), region (1), subregion (2), powiat (2),
    gmina (2) and the TERYT gmina type digit (1); e.g. 011212001011 (Bochnia, urban gmina) -> 1201011,
    071412865000 (powiat m.st. Warszawa) -> 1465. Gminas get the 7-digit TERYT including the type digit
    (1 urban, 2 rural, 3 urban-rural, 4/5 town/rural part of an urban-rural gmina, 8 Warsaw district,
    9 city delegation); powiats 4 digits, voivodeships 2. Raises ValueError for ids that do not have
    the shape of their level.
    """
    if not _UNIT_ID.fullmatch(unit_id):
        raise ValueError(f"BDL unit id {unit_id!r} is not 12 digits")
    voivodeship, powiat, gmina, kind = unit_id[2:4], unit_id[7:9], unit_id[9:11], unit_id[11]
    if level == 0 and unit_id == "0" * 12:
        return ""
    if level == 2 and voivodeship != "00" and unit_id[4:] == "0" * 8:
        return voivodeship
    if level == 5 and powiat != "00" and unit_id[9:] == "000":
        return voivodeship + powiat
    if level == 6 and powiat != "00" and kind != "0":
        return voivodeship + powiat + gmina + kind
    raise ValueError(f"BDL unit id {unit_id!r} does not match level {level}")


def available(sources: YearSources) -> bool:
    """Whether the committed GUS file exists (it covers every year of ``YEARS``)."""
    return VALUES_FILE.is_file()


@lru_cache(maxsize=1)
def _mart_geography() -> frozenset[str]:
    """TERYT codes of gminas (6 digits) and powiats (4 digits) in any loaded election (read once per run).

    NULL codes (abroad, ships, and pre-1999 rows, which carry the old 49-voivodeship codes in
    ``teryt_gmina_pre1999`` instead) are ignored.
    """
    gmina_sources = [
        "SELECT teryt_gmina FROM mart.precincts",
        "SELECT teryt_gmina FROM mart.prezydent_precinct_protocols",
        "SELECT teryt_gmina FROM mart.pe_precinct_protocols",
        "SELECT teryt_gmina FROM mart.referendum_precinct_protocols",
        "SELECT teryt_gmina FROM mart.sejmik_gmina_protocols",
        "SELECT teryt_gmina FROM mart.wbp_gmina_protocols",
        "SELECT teryt FROM mart.rada_districts WHERE council_level IN ('gmina', 'dzielnica')",
    ]
    with psycopg.connect(DATABASE_URL) as pg:
        gminas = {row[0] for row in pg.execute(" UNION ".join(gmina_sources)).fetchall() if row[0]}
        powiats = {row[0] for row in pg.execute("SELECT DISTINCT teryt_powiat FROM mart.rada_districts").fetchall() if row[0]}
    if not gminas:
        raise BuildError("no election is loaded: build the election families before gus (TERYT check)")
    return frozenset(gminas | powiats | {g[:4] for g in gminas})


def build_gus(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check ``gus_units`` and ``gus_indicators`` of ``sources.year``; no elections."""
    year = sources.year
    variables = {int(row["bdl_variable_id"]): row for row in csv.DictReader(VARIABLES_FILE.open(encoding="utf-8"))}
    missing = sorted(i for i, (v, _, _) in INDICATORS.items() if v not in variables)
    if missing:
        raise BuildError(f"{VARIABLES_FILE.name} has no BDL variable for {missing}")
    con.execute("CREATE OR REPLACE TABLE gus_indicator_spec (indicator VARCHAR, bdl_variable_id INT, unit VARCHAR, additive BOOLEAN)")
    con.executemany("INSERT INTO gus_indicator_spec VALUES (?, ?, ?, ?)", [[k, *v] for k, v in INDICATORS.items()])
    con.execute("CREATE OR REPLACE TABLE gus_sample_rounding (indicator VARCHAR, persons INT)")
    con.executemany("INSERT INTO gus_sample_rounding VALUES (?, ?)", [list(r) for r in SAMPLE_ROUNDING.items()])
    con.execute(
        f"""CREATE OR REPLACE TABLE gus_src AS SELECT * FROM read_parquet({sql_literal(str(VALUES_FILE))})
            WHERE year = {year}"""
    )
    bad_attrs = con.execute(
        f"SELECT DISTINCT attr_id FROM gus_src WHERE attr_id NOT IN ({', '.join(map(str, sorted(VALUE_ATTRIBUTES)))})"
    ).fetchall()
    if bad_attrs:
        raise BuildError(f"year {year}: unexpected BDL attributes {sorted(r[0] for r in bad_attrs)}")

    level_case = " ".join(f"WHEN {k} THEN '{v}'" for k, v in LEVELS.items())
    type_case = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in GMINA_TYPES.items())
    woj_case = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in VOIVODESHIPS.items())
    # Gmina-level units kept: gminas and Warsaw districts (GMINA_TYPES); not the parts of urban-rural
    # gminas (4, 5), not city totals (gmina digits 00).
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gus_unit_values AS
        SELECT s.year, CASE s.unit_level {level_case} END AS level,
               CASE WHEN s.unit_level = 6 THEN left(s.teryt, 6) ELSE nullif(s.teryt, '') END AS teryt,
               CASE WHEN s.unit_level = 6 THEN right(s.teryt, 1) END AS type_digit,
               s.bdl_unit_id, s.unit_name, s.bdl_variable_id, s.value, i.indicator, i.unit, i.additive
        FROM gus_src s JOIN gus_indicator_spec i USING (bdl_variable_id)
        WHERE s.unit_level IN ({', '.join(map(str, LEVELS))})
          AND (s.unit_level <> 6 OR (right(s.teryt, 1) IN ({', '.join(sql_literal(k) for k in GMINA_TYPES)})
                                    AND substr(s.teryt, 5, 2) <> '00'))
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gus_unit_values AS
        SELECT *, CASE level WHEN 'country' THEN 'country'
                             WHEN 'wojewodztwo' THEN CASE teryt {woj_case} END
                             ELSE teryt END AS unit_key
        FROM gus_unit_values
        """
    )
    unpublished = [i for i, years in NOT_PUBLISHED_FOR_DISTRICTS.items() if years is None or year in years]
    if unpublished:
        district_only = f"level = 'gmina' AND type_digit = '8' AND indicator IN ({', '.join(map(sql_literal, unpublished))})"
        published = con.execute(f"SELECT count(*) FROM gus_unit_values WHERE {district_only} AND value <> 0").fetchone()[0]
        if published:
            raise BuildError(f"year {year}: {published} non-zero values of {unpublished} for Warsaw districts")
        con.execute(f"DELETE FROM gus_unit_values WHERE {district_only}")
    not_gminas = ", ".join(sql_literal(t) for t in NOT_GMINAS)
    con.execute(
        f"""CREATE OR REPLACE TABLE gus_not_gminas AS
            SELECT year, level, teryt AS unit_key, teryt, indicator, CAST(value AS DECIMAL(18, 2)) AS value
            FROM gus_unit_values WHERE level = 'gmina' AND teryt IN ({not_gminas})"""
    )
    con.execute(f"DELETE FROM gus_unit_values WHERE level = 'gmina' AND teryt IN ({not_gminas})")
    con.execute(
        """
        CREATE OR REPLACE TABLE gus_indicators AS
        SELECT year, level, unit_key, teryt, indicator, CAST(value AS DECIMAL(18, 2)) AS value, unit, bdl_variable_id
        FROM gus_unit_values
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE gus_units AS
        SELECT year, level, unit_key, teryt, min(unit_name) AS name,
               CASE WHEN level = 'country' THEN NULL ELSE CASE left(teryt, 2) {woj_case} END END AS wojewodztwo,
               CASE WHEN level IN ('powiat', 'gmina') THEN left(teryt, 4) END AS teryt_powiat,
               CASE min(type_digit) {type_case} END AS gmina_type, min(bdl_unit_id) AS bdl_unit_id,
               count(DISTINCT bdl_unit_id) AS bdl_units, count(DISTINCT unit_name) AS names
        FROM gus_unit_values GROUP BY ALL
        """
    )
    con.execute(
        "CREATE OR REPLACE TABLE gus_geography (teryt VARCHAR)"
    )
    known = _mart_geography() | set(NOT_IN_ELECTIONS)
    con.executemany("INSERT INTO gus_geography VALUES (?)", [[t] for t in sorted(known)])
    _check(con, year)
    con.execute("ALTER TABLE gus_units DROP COLUMN bdl_units")
    con.execute("ALTER TABLE gus_units DROP COLUMN names")
    rows = con.execute("SELECT count(*) FROM gus_indicators").fetchone()[0]
    log.info("year %s: %s GUS indicator values", year, rows)
    return []


def _check(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Consistency checks of one year (see the module docstring)."""
    con.execute(
        """CREATE OR REPLACE VIEW gus_check_values AS
           SELECT level, unit_key, teryt, indicator, value FROM gus_indicators
           UNION ALL SELECT level, unit_key, teryt, indicator, value FROM gus_not_gminas"""
    )
    child_sum = """
        SELECT count(*) FROM (
            SELECT p.indicator, p.unit_key, p.value, coalesce(r.persons, 0) AS tolerance, sum(c.value) AS children
            FROM gus_indicators p
            JOIN gus_check_values c ON c.indicator = p.indicator AND c.level = '{child}' AND ({join})
            LEFT JOIN gus_sample_rounding r ON r.indicator = p.indicator
            WHERE p.level = '{parent}' AND p.indicator IN (SELECT indicator FROM gus_indicator_spec WHERE additive)
            GROUP BY ALL HAVING abs(sum(c.value) - p.value) > coalesce(r.persons, 0))"""
    checks = {
        "duplicate (year, level, unit, indicator)": """
            SELECT count(*) FROM (SELECT 1 FROM gus_indicators GROUP BY level, unit_key, indicator HAVING count(*) > 1)""",
        "units with several BDL ids or names in one year": "SELECT count(*) FROM gus_units WHERE bdl_units > 1 OR names > 1",
        "voivodeship TERYT not one of the 16 voivodeships": """
            SELECT count(*) FROM gus_units WHERE level = 'wojewodztwo' AND unit_key IS NULL""",
        "gmina or powiat TERYT absent from the election tables and NOT_IN_ELECTIONS": """
            SELECT count(*) FROM gus_units WHERE level IN ('powiat', 'gmina')
              AND teryt NOT IN (SELECT teryt FROM gus_geography)""",
        "gminas (without Warsaw districts) do not add up to their powiat": child_sum.format(
            child="gmina", parent="powiat", join="left(c.teryt, 4) = p.teryt AND c.teryt NOT LIKE '1465__' OR c.teryt = '146501' AND p.teryt = '1465'"
        ),
        "Warsaw districts do not add up to the city (146501)": child_sum.format(
            child="gmina", parent="gmina", join="c.teryt LIKE '1465__' AND c.teryt <> '146501' AND p.teryt = '146501'"
        ),
        "powiats do not add up to their voivodeship": child_sum.format(
            child="powiat", parent="wojewodztwo", join="left(c.teryt, 2) = p.teryt"
        ),
        "voivodeships do not add up to Poland": child_sum.format(child="wojewodztwo", parent="country", join="true"),
        "urban + rural population differs from population": """
            SELECT count(*) FROM gus_indicators t
            JOIN gus_indicators u ON u.indicator = 'population_urban' AND u.level = t.level AND u.unit_key = t.unit_key
            JOIN gus_indicators r ON r.indicator = 'population_rural' AND r.level = t.level AND r.unit_key = t.unit_key
            WHERE t.indicator = 'population' AND u.value + r.value <> t.value""",
        "urban population in a rural gmina or rural population in an urban gmina": """
            SELECT count(*) FROM gus_indicators i JOIN gus_units u USING (level, unit_key)
            WHERE (u.gmina_type = 'wiejska' AND i.indicator = 'population_urban' AND i.value <> 0)
               OR (u.gmina_type IN ('miejska', 'dzielnica Warszawy') AND i.indicator = 'population_rural' AND i.value <> 0)""",
        "percentages outside 0-100 or non-positive population, area or salary": """
            SELECT count(*) FROM gus_indicators
            WHERE (unit = 'percent' AND value NOT BETWEEN 0 AND 100)
               OR (indicator IN ('population', 'area_ha', 'avg_gross_salary') AND value <= 0)""",
        "age shares (pre-working, working, post-working) do not sum to 100 (+-0.2)": """
            SELECT count(*) FROM (
                SELECT level, unit_key FROM gus_indicators WHERE indicator IN ('age_preworking_pct', 'age_working_pct', 'age_postworking_pct')
                GROUP BY ALL HAVING count(*) <> 3 OR abs(sum(value) - 100) > 0.2)""",
        "census residents with higher education exceed residents aged 13+": """
            SELECT count(*) FROM gus_indicators h JOIN gus_indicators t USING (level, unit_key)
            WHERE (h.indicator, t.indicator) IN (('nsp2021_higher_education', 'nsp2021_population_13plus'),
                                                 ('nsp2011_higher_education', 'nsp2011_population_13plus'))
              AND h.value > t.value""",
        "gmina-level unit without population": """
            SELECT count(*) FROM gus_units u WHERE level = 'gmina'
              AND NOT EXISTS (SELECT 1 FROM gus_indicators i WHERE i.level = 'gmina' AND i.unit_key = u.unit_key
                              AND i.indicator = 'population')""",
    }
    run_checks(con, year, checks)
