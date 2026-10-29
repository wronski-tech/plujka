"""Build the presidential election tables of one year (both rounds) in DuckDB (loader side).

Sources per round (2000 had one round only):
- 2020, 2025: one per-precinct file with geography, protocol and one column per candidate
  (``wyniki_gl_na_kand_po_obwodach`` / ``protokoly_po_obwodach``) plus the candidate register
  ``kandydaci`` with official totals.
- 2010: per-precinct files ``pzt2010-wyn-obw`` (domestic, abroad and ships; candidates as 'SURNAME
  Given' headers; no invalid-votes column) and per-gmina totals ``pzt2010-wyn-gmn``, otherwise like 2015.
- 2005: per-precinct and per-gmina workbooks (KBW file names ``1456148660_37030`` / ``_37033`` for
  round 1, ``1456148721_37553`` / ``_37554`` for round 2), candidates from the headers ('Surname Given'
  in round 1, 'Given Surname - liczba' next to a percent column in round 2). Warsaw is one gmina
  (146501); see ``_fix_2005_warsaw``.
- 2000: per-precinct file ``prez2000obw`` ('Given SURNAME' headers, no candidate register) and per-gmina
  totals ``gm-kraj2000``, otherwise like 2010. Warsaw consists of the pre-2002 gminas
  (``WARSAW_GMINAS_2001``); Warszawa-Centrum (143104) is published without its districts.
- 2015: per-precinct files only (``prezydent_2015_tura1.csv``, ``wyniki_tura2.xls``) and per-gmina
  totals (``prez2015_N_tura_gm.xls``); there is no candidate register, so candidates come from the
  column headers. The round 2 file publishes TERYT as 0: its precincts get the TERYT of the round 1
  precinct with the same voivodeship, gmina name and number.

Checks: unique precincts, every candidate read from exactly one column, precinct votes equal the
official candidate totals (2015: equal the per-gmina file), and valid votes equal the sum of
candidate votes.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata

import duckdb

from api.services.mart.build_common import (
    BuildError,
    Election,
    YearSources,
    column,
    columns_of,
    python_name_key,
    quote_ident,
    run_checks,
    sql_list,
    sql_literal,
)
from api.services.mart.build_precincts import (
    PROTOCOL_ALIASES,
    create_voivodeships_1975,
    create_warsaw_districts,
    create_warsaw_gminas_2001,
    precinct_select,
    unpivot_votes,
    vote_columns,
)

log = logging.getLogger("mart")

TABLES = ("prezydent_candidates", "prezydent_precinct_protocols", "prezydent_precinct_candidate_votes")
FAMILY = ("prezydent",)

SOURCES = {
    1990: {
        r: {"results": "**/prezydent/prez90kraj/*.parquet", "abroad": "**/prezydent/zagr-i-statki90.parquet",
            "gmina_totals": "**/prezydent/prez1990gm.parquet"}
        for r in (1, 2)
    },
    1995: {
        r: {"results": f"**/prezydent/Prez1995/1995-prez-obw-tura{r}__*.parquet",
            "gmina_totals": f"**/prezydent/Prez1995/prez1995gm{r}tura__*.parquet"}
        for r in (1, 2)
    },
    2000: {1: {"results": "**/prezydent/prez2000obw.parquet", "gmina_totals": "**/prezydent/gm-kraj2000.parquet"}},
    2005: {
        1: {"results": "**/prezydent/1/1456148660_37030__sheet02_*.parquet", "gmina_totals": "**/prezydent/1/1456148660_37033.parquet",
            "surname_first": True},
        2: {"results": "**/prezydent/2/1456148721_37553.parquet", "gmina_totals": "**/prezydent/2/1456148721_37554.parquet"},
    },
    2010: {
        r: {"results": f"**/prezydent/{r}/pzt2010-wyn-obw/*.parquet", "gmina_totals": f"**/prezydent/{r}/pzt2010-wyn-gmn.parquet"}
        for r in (1, 2)
    },
    2015: {
        1: {"results": "**/prezydent/1/prezydent_2015_tura1/prezydent_2015_tura1__*.parquet",
            "gmina_totals": "**/prezydent/1/prez2015_1_tura_gm.parquet"},
        2: {"results": "**/prezydent/2/wyniki_tura2/*.parquet",
            "gmina_totals": "**/prezydent/2/prez2015_2_tura_gm.parquet"},
    },
    2020: {
        r: {"results": f"**/prezydent/{r}/wyniki_gl_na_kand_po_obwodach_csv/*.parquet",
            "candidates": f"**/prezydent/{r}/kandydaci_csv/*.parquet"}
        for r in (1, 2)
    },
    2025: {
        1: {"results": "**/prezydent/1/protokoly_po_obwodach_csv*/*.parquet",
            "candidates": "**/prezydent/1/kandydaci_csv*/*.parquet"},
        2: {"results": "**/prezydent/2/protokoly_po_obwodach_w_drugiej_turze_csv*/*.parquet",
            "candidates": "**/prezydent/2/kandydaci_w_drugiej_turze_csv*/*.parquet"},
    },
}

DATES = {
    (1990, 1): "1990-11-25", (1990, 2): "1990-12-09",
    (1995, 1): "1995-11-05", (1995, 2): "1995-11-19",
    (2000, 1): "2000-10-08",
    (2005, 1): "2005-10-09", (2005, 2): "2005-10-23",
    (2010, 1): "2010-06-20", (2010, 2): "2010-07-04",
    (2015, 1): "2015-05-10", (2015, 2): "2015-05-24",
    (2020, 1): "2020-06-28", (2020, 2): "2020-07-12",
    (2025, 1): "2025-05-18", (2025, 2): "2025-06-01",
}


def available(sources: YearSources) -> bool:
    """Whether both rounds of the year have staged per-precinct results."""
    rounds = SOURCES.get(sources.year)
    return bool(rounds) and all(sources.has(r["results"]) for r in rounds.values())


def _candidates_from_register(con: duckdb.DuckDBPyConnection, year: int, rnd: int, src: str) -> None:
    """``cand_<rnd>`` from the official register (2020: Nazwisko + Imiona; 2025: 'Nazwisko i imiona')."""
    if column(con, src, "Nazwisko i imiona", required=False):
        name = '"Nazwisko i imiona"'
    else:
        name = """("Imiona" || ' ' || "Nazwisko")"""
    elected = column(con, src, "Wybrany", "Czy uzyskał mandat")
    pct = column(con, src, "Procent wszystkich głosów", "Procent głosów")
    residence_teryt = column(con, src, "TERYT m. z.", required=False)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cand_{rnd} AS
        SELECT {year} AS year, {rnd} AS round,
               CAST(row_number() OVER (ORDER BY surname({name}), {name}) AS INT) AS candidate_id,
               display_name({name}) AS full_name,
               surname({name}) AS last_name, given_names({name}) AS first_names,
               gender_("Płeć") AS gender, "Zawód" AS occupation, "Miejsce zamieszkania" AS residence,
               {f"clean_teryt({residence_teryt})" if residence_teryt else "CAST(NULL AS VARCHAR)"} AS residence_teryt,
               "Przynależność do partii" AS party_membership, "Nazwa komitetu" AS committee_name,
               int_("Liczba głosów") AS votes, CAST(round(num_({pct}), 2) AS DECIMAL(6, 2)) AS pct,
               yes_({elected}) AS elected, name_key({name}) AS nkey
        FROM {src}
        """
    )


def _candidates_from_headers(
    con: duckdb.DuckDBPyConnection, year: int, rnd: int, headers: list[str], surname_first: bool = False
) -> None:
    """``cand_<rnd>`` from 'Given Names Surname' (2015, 2005 round 2), 'SURNAME Given Names' (2010) or,
    with ``surname_first``, 'Surname Given Names' (2005 round 1) headers.

    Votes are filled from precinct sums. DuckDB list slices are inclusive, so ``parts[:-2]`` is every
    word but the last (the 2015 surname) and ``parts[2:]`` every word but the first; 2010 surnames are
    the upper-case words.
    """
    if surname_first:
        last, first = "upper(parts[1])", "array_to_string(parts[2:], ' ')"
    else:
        last, first = "upper(parts[-1])", "array_to_string(parts[:-2], ' ')"
    con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_names (header VARCHAR)")
    con.executemany(f"INSERT INTO cand_{rnd}_names VALUES (?)", [[h] for h in headers])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cand_{rnd} AS
        SELECT {year} AS year, {rnd} AS round,
               CAST(row_number() OVER (ORDER BY last_name, header) AS INT) AS candidate_id,
               first_names || ' ' || last_name AS full_name, last_name, first_names,
               NULL AS gender, NULL AS occupation, NULL AS residence, CAST(NULL AS VARCHAR) AS residence_teryt,
               NULL AS party_membership,
               NULL AS committee_name, CAST(NULL AS BIGINT) AS votes, CAST(NULL AS DECIMAL(6, 2)) AS pct,
               false AS elected, name_key(header) AS nkey
        FROM (
          SELECT header,
                 CASE WHEN surname(header) <> '' THEN surname(header) ELSE {last} END AS last_name,
                 CASE WHEN surname(header) <> '' THEN given_names(header) ELSE {first} END AS first_names
          FROM (SELECT header, str_split(trim(header), ' ') AS parts FROM cand_{rnd}_names)
        )
        """
    )


def _fix_2015_round2_teryt(con: duckdb.DuckDBPyConnection, src: str) -> str:
    """Round 2 of 2015 publishes TERYT 0; take it from the matching round 1 precinct.

    Precincts are matched on (voivodeship, gmina name, number). A few voivodeships have two gminas
    with the same name (e.g. two "gm. Czarna" in podkarpackie); both files list precincts in the same
    order, so ties are paired by their position within the key, and the pairing is checked by the
    number of eligible voters.
    """
    key = """concat_ws('|', lower("Województwo"), "Gmina", CAST(int_("Numer obwodu") AS VARCHAR))"""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE r2_fixed AS
        WITH r1 AS (
          SELECT {key} AS k, row_number() OVER (PARTITION BY {key} ORDER BY rn) AS ord, "TERYT gminy" AS teryt,
                 int_("Liczba wyborców uprawnionych do głosowania") AS eligible
          FROM (SELECT *, row_number() OVER () AS rn FROM src_prez_results_1)
        ), r2 AS (
          SELECT *, {key} AS k, row_number() OVER (PARTITION BY {key} ORDER BY rn) AS ord
          FROM (SELECT *, row_number() OVER () AS rn FROM {src})
        )
        SELECT r2.* EXCLUDE (k, ord, rn) REPLACE (r1.teryt AS "TERYT gminy"),
               abs(r1.eligible - int_(r2."Liczba wyborców uprawnionych do głosowania")) * 1.0 / nullif(r1.eligible, 0) AS eligible_change
        FROM r2 LEFT JOIN r1 USING (k, ord)
        """
    )
    unmatched, tie_drift = con.execute(
        """
        SELECT count(*) FILTER (WHERE "Gmina" IS NOT NULL AND eligible_change IS NULL AND int_("Liczba wyborców uprawnionych do głosowania") > 0),
               max(eligible_change)
        FROM r2_fixed
        """
    ).fetchone()
    log.info("2015 round 2: %s precincts without a round 1 match; max eligible-voter change %.3f", unmatched, tie_drift or 0)
    con.execute("ALTER TABLE r2_fixed DROP COLUMN eligible_change")
    return "r2_fixed"


def _fix_2005_warsaw(con: duckdb.DuckDBPyConnection, src: str, rnd: int) -> str:
    """2005: Warsaw is one gmina (146501) in the precinct files; give its precincts their district.

    Round 1 names the district by a letter in '7-ma cyfra TERYT' (``WARSAW_DISTRICTS_2005``). Round 2
    has no letter: a Warsaw precinct takes the district of the round 1 precinct with the same number
    (numbers are unique within Warsaw; unmatched precincts keep 146501 and fail a check).
    """
    gmina = column(con, src, *("Nazwa gminy", "Gmina"))
    is_warsaw = """lpad(trim(s."TERYT"), 6, '0') = '146501'"""
    if rnd == 1:
        create_warsaw_districts(con)
        con.execute(
            f"""CREATE OR REPLACE TABLE warsaw_precincts_2005 AS
                SELECT int_(s."Numer obwodu") AS nr, w.teryt, w.gmina
                FROM {src} s JOIN warsaw_districts_2005 w ON {is_warsaw} AND w.letter = trim(s."7-ma cyfra TERYT")"""
        )
    con.execute(
        f"""CREATE OR REPLACE TABLE prez_warsaw_{rnd} AS
            SELECT s.* REPLACE (coalesce(w.teryt, s."TERYT") AS "TERYT", coalesce(w.gmina, s.{gmina}) AS {gmina})
            FROM {src} s LEFT JOIN warsaw_precincts_2005 w ON {is_warsaw} AND w.nr = int_(s."Numer obwodu")"""
    )
    return f"prez_warsaw_{rnd}"


class CentrumMatchError(BuildError):
    """A 2000 Warszawa-Centrum precinct cannot be placed in a district through the 2001 precinct list."""


# 2001 Sejm precinct file: Warszawa-Centrum precincts with the district letter ('Teren', WARSAW_GMINAS_2001).
CENTRUM_2001 = "**/sejm/sejm2001-lis-obw.parquet"
# 2000 Warszawa-Centrum precincts whose seat differs from the 2001 precinct with the same number in more than
# spelling, reviewed by hand: number -> (letter of the 2001 district, why it is the same district).
CENTRUM_2000_REVIEWED = {
    332: ("G", "Klub WSM, ul. Promyka 5 (2000) and Przedszkole nr 212, ul. Rybińskiego 2 (2001) are both on "
               "the Żoliborz housing estate of WSM; neighbours 331 and 333 match 2001 Żoliborz precincts by "
               "address, and Rybińskiego 2 is a Żoliborz precinct (letter R) in 2005 too"),
}


def _address_key(address: str) -> str:
    """Address compared across years: lower case, no diacritics, 'AI.'/'PI.' read as 'al.'/'pl.', no punctuation."""
    text = unicodedata.normalize("NFKD", address.lower().replace("ł", "l"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).replace('"', "")
    text = re.sub(r"\b([ap])[il]\.+\s*", r"\1l. ", text)
    text = re.sub(r"\b(\d+) ([a-z])\b", r"\1\2", re.sub(r"[.,;:\-]", " ", text))  # '1 a' -> '1a'
    return re.sub(r"\s+", " ", text).strip()


def _street_numbers(address: str) -> list[tuple[str, str, str]]:
    """(text before the street, street, house number without letter) of an address, e.g.
    [('szkola podstawowa nr 49', 'szanajcy', '17')]."""
    key = _address_key(address)
    pattern = r"\b(?:ul|al|pl|os)\s+((?:[a-z]+\s?)+?)\s*(\d+)[a-z]?\b"
    return [(key[: m.start()].strip(), m[1].strip(), m[2]) for m in re.finditer(pattern, key)]


def _same_building(address_2000: str, address_2001: str) -> bool:
    """Whether two seats are on a street spelled alike (ratio >= 0.8: 'Sonat'/'Sonaty', 'Ghandiego'/'Gandhiego')
    and either share the house number up to a letter ('14a'/'14': renamed schools) or name the same
    institution ('Szkoła Podstawowa nr 50, ul. Jagiellońska 3/5' / '... 7')."""
    return any(
        difflib.SequenceMatcher(None, street_a, street_b).ratio() >= 0.8
        and (number_a == number_b or (seat_a and seat_a == seat_b))
        for seat_a, street_a, number_a in _street_numbers(address_2000)
        for seat_b, street_b, number_b in _street_numbers(address_2001)
    )


def _centrum_2000_districts(con: duckdb.DuckDBPyConnection, sources: YearSources, src: str) -> None:
    """Create ``centrum_2000(nr, letter)``: the 2001 district letter of every 2000 Warszawa-Centrum precinct.

    The 2000 file gives Warszawa-Centrum (143104) without districts; the 2001 Sejm file splits it by a
    letter and numbers its precincts the same way. A 2000 precinct takes the letter of the 2001 precinct
    with the same number when their seats are the same building (same address after normalization, or
    the same street and house number), or when the pair is reviewed in CENTRUM_2000_REVIEWED. Raises
    CentrumMatchError when a precinct has no 2001 counterpart, a number is not unique, or a seat differs
    without a review (and when a reviewed entry is no longer needed).
    """
    files_2001 = YearSources(con, [d.parent for d in sources.year_dirs], 2001).files(CENTRUM_2001)
    if not files_2001:
        raise CentrumMatchError(f"2001 precinct file {CENTRUM_2001} is not staged; it is needed to split Warszawa-Centrum")
    rows_2000 = con.execute(
        f"""SELECT int_("Nr obw."), "Adres" FROM {src} WHERE lpad(trim("Kod gminy"), 6, '0') = '143104'"""
    ).fetchall()
    rows_2001 = con.execute(
        f"""SELECT int_("Nr obw."), "Adres", trim("Teren") FROM read_parquet({sql_list(files_2001)})
            WHERE lpad(trim("Kod gminy"), 6, '0') = '143104'"""
    ).fetchall()
    by_number: dict[int, list[tuple[str, str]]] = {}
    for number, address, letter in rows_2001:
        by_number.setdefault(number, []).append((address, letter))
    numbers_2000 = [number for number, _ in rows_2000]
    errors = [f"2000 precinct number {n} occurs more than once" for n in sorted({n for n in numbers_2000 if numbers_2000.count(n) > 1})]
    matched, counts, used_reviews = {}, {"same address": 0, "same building": 0, "reviewed": 0}, set()
    for number, address in sorted(rows_2000):
        candidates = by_number.get(number, [])
        if len(candidates) != 1:
            errors.append(f"precinct {number} has {len(candidates)} 2001 precincts with that number")
            continue
        address_2001, letter = candidates[0]
        if _address_key(address) == _address_key(address_2001):
            counts["same address"] += 1
        elif _same_building(address, address_2001):
            counts["same building"] += 1
            log.info("2000 Warszawa-Centrum precinct %s (%s): '%s' ~ 2001 '%s'", number, letter, address, address_2001)
        elif CENTRUM_2000_REVIEWED.get(number, ("", ""))[0] == letter:
            counts["reviewed"] += 1
            used_reviews.add(number)
            log.info("2000 Warszawa-Centrum precinct %s (%s): '%s' vs 2001 '%s' reviewed: %s",
                     number, letter, address, address_2001, CENTRUM_2000_REVIEWED[number][1])
        else:
            errors.append(f"precinct {number}: '{address}' (2000) vs '{address_2001}' (2001, letter {letter})")
            continue
        matched[number] = letter
    errors += [f"reviewed precinct {n} no longer needs a review" for n in sorted(set(CENTRUM_2000_REVIEWED) - used_reviews)]
    if errors:
        raise CentrumMatchError("Warszawa-Centrum 2000 cannot be split by the 2001 precincts: " + "; ".join(errors))
    log.info("2000 Warszawa-Centrum: %s precincts placed in districts through 2001 (%s)", len(matched), counts)
    con.execute("CREATE OR REPLACE TABLE centrum_2000 (nr BIGINT, letter VARCHAR)")
    con.executemany("INSERT INTO centrum_2000 VALUES (?, ?)", list(matched.items()))


def _fix_2000_warsaw(con: duckdb.DuckDBPyConnection, sources: YearSources, src: str) -> str:
    """2000: precincts of the pre-2002 Warsaw gminas (1431xx) get the TERYT of the post-2002 district.

    Warszawa-Centrum (143104) covered seven districts and the file does not say which: its precincts take
    the district of the 2001 precinct with the same number and seat (``_centrum_2000_districts``).
    """
    create_warsaw_gminas_2001(con)
    _centrum_2000_districts(con, sources, src)
    con.execute(
        f"""CREATE OR REPLACE TABLE prez_warsaw_2000 AS
            SELECT s.* REPLACE (coalesce(w.teryt, s."Kod gminy") AS "Kod gminy", coalesce(w.gmina, s."Gmina") AS "Gmina")
            FROM {src} s
            LEFT JOIN centrum_2000 c ON lpad(trim(s."Kod gminy"), 6, '0') = '143104' AND c.nr = int_(s."Nr obw.")
            LEFT JOIN warsaw_gminas_2001 w ON w.old_teryt = lpad(trim(s."Kod gminy"), 6, '0')
              AND (w.letter IS NULL OR w.letter = c.letter)"""
    )
    return "prez_warsaw_2000"


def _header_name(header: str) -> str:
    """Candidate name of a vote column: 2005 round 2 headers end with ' - liczba' (unit row)."""
    return re.sub(r" - liczba$", "", header)


# Pre-1999 rounds publish no candidate register: candidates (SURNAME, given names) and the national figures come
# from PKW's announcements (1990: Dz.U. 1990 nr 83 poz. 483 and nr 85 poz. 499; 1995: Dz.U. 1995 nr 126 poz. 604
# and nr 131 poz. 636), and the precinct sums must reproduce them. valid_ballots = voters who took part
# ("w głosowaniu wzięło udział", ballots cast); ballots issued are not published.
OFFICIAL_PRE1999 = {
    (1990, 1): {
        "precincts": 22665, "eligible_voters": 27545625, "valid_ballots": 16702000, "invalid_votes": 259526,
        "valid_votes": 16442474,
        "candidates": (("BARTOSZCZE", "Roman", 1176175), ("CIMOSZEWICZ", "Włodzimierz", 1514025),
                       ("MAZOWIECKI", "Tadeusz", 2973264), ("MOCZULSKI", "Leszek Robert", 411516),
                       ("TYMIŃSKI", "Stanisław", 3797605), ("WAŁĘSA", "Lech", 6569889)),
    },
    (1990, 2): {
        "precincts": 22236, "eligible_voters": 27436078, "valid_ballots": 14650037, "invalid_votes": 344243,
        "valid_votes": 14305794, "candidates": (("TYMIŃSKI", "Stanisław", 3683098), ("WAŁĘSA", "Lech", 10622696)),
    },
    (1995, 1): {
        "precincts": 22750, "eligible_voters": 28136332, "valid_ballots": 18203218, "invalid_votes": 330868,
        "valid_votes": 17872350,
        "candidates": (("BUBEL", "Leszek", 6825), ("GRONKIEWICZ-WALTZ", "Hanna", 492628),
                       ("KORWIN-MIKKE", "Janusz Ryszard", 428969), ("KOŹLUK", "Tadeusz", 27259),
                       ("KUROŃ", "Jacek Jan", 1646946), ("KWAŚNIEWSKI", "Aleksander", 6275670),
                       ("LEPPER", "Andrzej", 235797), ("OLSZEWSKI", "Jan", 1225453), ("PAWLAK", "Waldemar", 770419),
                       ("PIETRZAK", "Jan", 201033), ("PIOTROWICZ", "Kazimierz Wojciech", 12591),
                       ("WAŁĘSA", "Lech", 5917328), ("ZIELIŃSKI", "Tadeusz Stanisław", 631432)),
    },
    (1995, 2): {
        "precincts": 22472, "eligible_voters": 28062409, "valid_ballots": 19146496, "invalid_votes": 383881,
        "valid_votes": 18762615, "candidates": (("KWAŚNIEWSKI", "Aleksander", 9704439), ("WAŁĘSA", "Lech", 9058176)),
    },
}
# 1995 round 2 headers are initials.
HEADER_SURNAMES_1995 = {"AK": "KWAŚNIEWSKI", "LW": "WAŁĘSA"}
# 1990: ship precincts listed in ``zagr-i-statki90`` with zeros only (no eligible voters, no ballots), which PKW's
# 22,665 precincts do not count: (shipowner, number). They are dropped after a check that they are empty.
EMPTY_SHIP_PRECINCTS_1990 = {("PLO", 31): "PLK. DABEK", ("PLO", 37): "M.KALINOWSKI"}
# 1990: hospital precincts closed before round 2 (KBW: "2 fewer precincts in round 2 because 2 hospitals closed");
# the file gives them zeros in round 2: (7-digit code, number). Dropped from round 2 after a check that they are empty.
CLOSED_ROUND2_1990 = {("0100902", 654), ("3500001", 356)}


def _header_surname(header: str) -> str:
    """Surname of a pre-1999 vote column: 'Roman Bartoszcze', 'Lech Wałęsa_1' (round 2 of 1990), 'Bartoszcze'
    (1990 abroad), 'Hanna Gronkiewicz- -Waltz' (1995, hyphen split over two lines), 'AK' (1995 round 2)."""
    name = re.sub(r"_\d+$", "", header.replace("\n", " ")).replace("- -", "-").strip()
    return HEADER_SURNAMES_1995.get(name, name.split(" ")[-1].upper())


def _between(columns: list[str], start: str, end: str | None) -> list[str]:
    """Columns strictly between ``start`` and ``end`` (to the end, without 'year', when ``end`` is None)."""
    stop = columns.index(end) if end else len(columns)
    return [c for c in columns[columns.index(start) + 1 : stop] if c != "year"]


def _pre1999_parts(con: duckdb.DuckDBPyConnection, sources: YearSources, year: int, rnd: int, spec: dict) -> list[tuple[str, list[str]]]:
    """(SELECT with the standard columns, vote columns) per pre-1999 source of one round.

    Standard columns: pkey, code (5-digit pre-1999 gmina code), ptype (Z abroad, S ship, else domestic), nr,
    gmina, address, eligible_voters, valid_ballots, invalid_votes, valid_votes. 1990: the precinct file's
    'TERYT' is the 7-digit code of 1990, 'Gnina' / 'Nazwa' the gmina of the 1993 division that KBW assigned
    the precinct to (the code system of the 1991-1997 files); abroad (TERYT 100901, the Warsaw commission)
    and ships voted in round 1 only and are in ``zagr-i-statki90``. 1995: abroad is gmina 01999 (type Z),
    ships are type S in their port gmina.
    """
    src = sources.load(f"prez_results_{rnd}", spec["results"])
    columns = columns_of(con, src)
    parts = []
    if year == 1990:
        s = "" if rnd == 1 else "_1"
        votes = _between(columns, "Niew.", "Upr_1") if rnd == 1 else _between(columns, "Niew._1", "column25")
        closed = " OR ".join(f"""(trim("TERYT") = '{t}' AND int_("Nr obw.") = {n})""" for t, n in sorted(CLOSED_ROUND2_1990))
        if rnd == 2:
            numbers = ", ".join(f"int_({quote_ident(c)})" for c in ["Upr_1", "Odd_1", "Waz_1", "Niew._1", *votes])
            run_checks(con, year, {
                "CLOSED_ROUND2_1990 missing or not empty in round 2": f"""
                    SELECT {len(CLOSED_ROUND2_1990)} - count(*) FILTER (WHERE greatest({numbers}) = 0) FROM {src} WHERE {closed}""",
            })
        parts.append((
            f"""SELECT trim("TERYT") || '/' || CAST(int_("Nr obw.") AS VARCHAR) AS pkey, trim("Gnina") AS code, 'P' AS ptype,
                       int_("Nr obw.") AS nr, "Nazwa" AS gmina, "Adres" AS address, int_("Upr{s}") AS eligible_voters,
                       int_("Odd{s}") AS valid_ballots, int_("Niew.{s}") AS invalid_votes, int_("Waz{s}") AS valid_votes,
                       {", ".join(quote_ident(c) for c in votes)}
                FROM {src} WHERE int_("Upr{s}") IS NOT NULL AND ({rnd} = 1 OR NOT coalesce({closed}, false))""",
            votes,
        ))
        if rnd == 1:
            abroad = sources.load("prez_abroad_1", spec["abroad"])
            votes = _between(columns_of(con, abroad), "Nieważne", None)
            empty = " OR ".join(f"""("ARMATOR" = {sql_literal(o)} AND int_("NR") = {n})""" for o, n in EMPTY_SHIP_PRECINCTS_1990)
            numbers = ", ".join(f"int_({quote_ident(c)})" for c in ["Upr.", "Odd.", "Ważne", "Nieważne", *votes])
            run_checks(con, year, {
                "EMPTY_SHIP_PRECINCTS_1990 missing, renamed or not empty": f"""
                    SELECT {len(EMPTY_SHIP_PRECINCTS_1990)} - count(*) FILTER (
                        WHERE greatest({numbers}) = 0 AND trim("ADRES") IN ({", ".join(sql_literal(a) for a in EMPTY_SHIP_PRECINCTS_1990.values())}))
                    FROM {abroad} WHERE {empty}""",
            })
            parts.append((
                f"""SELECT CASE WHEN trim("TERYT") = '100901' THEN 'zagranica/' || CAST(int_("NR") AS VARCHAR)
                            ELSE 'statek/' || coalesce("ARMATOR", "SIEDZIBA") || '/' || CAST(int_("NR") AS VARCHAR) END AS pkey,
                           CAST(NULL AS VARCHAR) AS code, CASE WHEN trim("TERYT") = '100901' THEN 'Z' ELSE 'S' END AS ptype,
                           int_("NR") AS nr, CASE WHEN trim("TERYT") = '100901' THEN 'zagranica' ELSE 'statki' END AS gmina,
                           "ADRES" AS address, int_("Upr.") AS eligible_voters, int_("Odd.") AS valid_ballots,
                           int_("Nieważne") AS invalid_votes, int_("Ważne") AS valid_votes, {", ".join(quote_ident(c) for c in votes)}
                    FROM {abroad} WHERE NOT coalesce({empty}, false)""",
                votes,
            ))
    else:
        votes = _between(columns, "Ważne", None)
        parts.append((
            f"""SELECT CASE trim("Typ") WHEN 'Z' THEN 'zagranica/' || CAST(int_("Nr obw.") AS VARCHAR)
                            WHEN 'S' THEN 'statek/' || trim("Kod gm.") || '/' || CAST(int_("Nr obw.") AS VARCHAR)
                            ELSE trim("Kod gm.") || '/' || CAST(int_("Nr obw.") AS VARCHAR) END AS pkey,
                       trim("Kod gm.") AS code, trim("Typ") AS ptype, int_("Nr obw.") AS nr,
                       CASE trim("Typ") WHEN 'Z' THEN 'zagranica' WHEN 'S' THEN 'statki' ELSE "Nazwa gm." END AS gmina,
                       "Adres" AS address, int_("Upr.") AS eligible_voters, int_("Frekwencja") AS valid_ballots,
                       int_("Niew.") AS invalid_votes, int_("Ważne") AS valid_votes, {", ".join(quote_ident(c) for c in votes)}
                FROM {src}""",
            votes,
        ))
    return parts


def _gmina_totals_pre1999(con: duckdb.DuckDBPyConnection, sources: YearSources, year: int, rnd: int, spec: dict, ids: dict[str, int]) -> None:
    """``gmina_totals_<rnd>(candidate_id, votes)``: national sums of the per-gmina file (1990: one workbook for
    both rounds, names in the first row under 'I tura' / 'II tura'; abroad and ships are rows of their own)."""
    src = sources.load(f"prez_gmina_{rnd}", spec["gmina_totals"])
    columns = columns_of(con, src)
    if year == 1990:
        block = _between(columns, "I tura", "II tura") if rnd == 1 else _between(columns, "II tura", None)
        names = dict(zip(columns, con.execute(f"""SELECT * FROM {src} WHERE trim("I tura") = 'upr.'""").fetchone()))
        headers = {c: _header_surname(names[c]) for c in block if names[c] and _header_surname(names[c]) in ids}
        rows = f"""{src} WHERE int_("I tura") IS NOT NULL"""
    else:
        headers = {c: _header_surname(c) for c in _between(columns, "Ważne", None)}
        rows = src
    con.execute(
        f"CREATE OR REPLACE TABLE gmina_totals_{rnd} AS "
        + " UNION ALL ".join(f"SELECT {ids[s]} AS candidate_id, sum(int_({quote_ident(c)})) AS votes FROM {rows}" for c, s in headers.items())
    )


def _round_pre1999(con: duckdb.DuckDBPyConnection, sources: YearSources, year: int, rnd: int, spec: dict) -> None:
    """Build ``cand_<rnd>``, ``prot_<rnd>``, ``votes_<rnd>`` and the check tables of a pre-1999 round.

    Geography: teryt_gmina and powiat stay NULL (no official key maps pre-1999 gmina codes to TERYT), the
    5-digit code goes to teryt_gmina_pre1999 and the 1975-1998 voivodeship (by the code's first two digits) to
    wojewodztwo; both are NULL abroad and on ships.
    """
    official = OFFICIAL_PRE1999[(year, rnd)]
    con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_official (last_name VARCHAR, first_names VARCHAR, votes BIGINT)")
    con.executemany(f"INSERT INTO cand_{rnd}_official VALUES (?, ?, ?)", [list(c) for c in official["candidates"]])
    con.execute(
        f"""
        CREATE OR REPLACE TABLE cand_{rnd} AS
        SELECT {year} AS year, {rnd} AS round, CAST(row_number() OVER (ORDER BY last_name) AS INT) AS candidate_id,
               first_names || ' ' || last_name AS full_name, last_name, first_names,
               NULL AS gender, NULL AS occupation, NULL AS residence, CAST(NULL AS VARCHAR) AS residence_teryt,
               NULL AS party_membership, NULL AS committee_name, votes,
               CAST(round(100.0 * votes / {official["valid_votes"]}, 2) AS DECIMAL(6, 2)) AS pct,
               CASE WHEN {rnd} = 2 THEN votes = max(votes) OVER () ELSE 100.0 * votes / {official["valid_votes"]} > 50 END AS elected,
               name_key(first_names || ' ' || last_name) AS nkey
        FROM cand_{rnd}_official
        """
    )
    ids = dict(con.execute(f"SELECT last_name, candidate_id FROM cand_{rnd}").fetchall())
    parts = _pre1999_parts(con, sources, year, rnd, spec)
    con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_part_columns (part INT, header VARCHAR, candidate_id INT)")
    unmapped, selects, unpivots = [], [], []
    for part, (select, votes) in enumerate(parts):
        mapping = {h: ids[_header_surname(h)] for h in votes if _header_surname(h) in ids}
        unmapped += [h for h in votes if h not in mapping]
        con.executemany(f"INSERT INTO cand_{rnd}_part_columns VALUES (?, ?, ?)", [[part, h, i] for h, i in mapping.items()])
        con.execute(f"CREATE OR REPLACE TABLE pre_{rnd}_{part} AS {select}")
        selects.append(f"SELECT pkey, code, ptype, nr, gmina, address, eligible_voters, valid_ballots, invalid_votes, valid_votes FROM pre_{rnd}_{part}")
        cols = ", ".join(quote_ident(h) for h in mapping)
        unpivots.append(
            f"""SELECT u.pkey, m.candidate_id, CAST(coalesce(int_(u.votes), 0) AS INT) AS votes
                FROM (UNPIVOT (SELECT pkey, {cols} FROM pre_{rnd}_{part}) ON {cols} INTO NAME header VALUE votes) u
                JOIN cand_{rnd}_part_columns m ON m.part = {part} AND m.header = u.header"""
        )
    con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_unmapped AS SELECT unnest(?::VARCHAR[]) AS header", [unmapped])
    # Every part must read each candidate from one column; _check_round sees the first part's columns.
    con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_columns AS SELECT candidate_id FROM cand_{rnd}_part_columns WHERE part = 0")
    create_voivodeships_1975(con)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE prot_{rnd} AS
        SELECT p.pkey, CAST(NULL AS VARCHAR) AS teryt_gmina, CAST(p.nr AS INT) AS precinct_no, p.gmina,
               CAST(NULL AS VARCHAR) AS powiat, p.address, CASE WHEN p.ptype NOT IN ('Z', 'S') THEN v.name END AS wojewodztwo,
               CAST(p.eligible_voters AS INT) AS eligible_voters, CAST(NULL AS INT) AS ballots_issued,
               CAST(p.valid_ballots AS INT) AS valid_ballots, CAST(p.invalid_votes AS INT) AS invalid_votes,
               CAST(p.valid_votes AS INT) AS valid_votes, CASE WHEN p.ptype NOT IN ('Z', 'S') THEN p.code END AS teryt_gmina_pre1999
        FROM ({" UNION ALL ".join(selects)}) p LEFT JOIN voivodeships_1975 v ON v.code = left(p.code, 2)
        """
    )
    con.execute(f"CREATE OR REPLACE TABLE votes_{rnd} AS " + " UNION ALL ".join(unpivots))
    _gmina_totals_pre1999(con, sources, year, rnd, spec, ids)
    checks = {
        f"round {rnd}: candidates without a column in some source file": f"""
            SELECT count(*) FROM cand_{rnd} c, range({len(parts)}) r(part)
            WHERE (SELECT count(*) FROM cand_{rnd}_part_columns m WHERE m.candidate_id = c.candidate_id AND m.part = r.part) <> 1""",
        f"round {rnd}: domestic precincts without a 1975-1998 voivodeship": f"""
            SELECT count(*) FROM prot_{rnd} WHERE teryt_gmina_pre1999 IS NOT NULL AND wojewodztwo IS NULL""",
        f"round {rnd}: precincts other than in PKW's announcement ({official['precincts']})": f"""
            SELECT abs(count(*) - {official['precincts']}) FROM prot_{rnd}""",
    }
    for measure in ("eligible_voters", "valid_ballots", "invalid_votes", "valid_votes"):
        checks[f"round {rnd}: {measure} differ from PKW's announcement ({official[measure]})"] = f"""
            SELECT abs(sum({measure}) - {official[measure]}) FROM prot_{rnd}"""
    run_checks(con, year, checks)


def build_prezydent(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check both rounds of ``sources.year``; return the loaded elections."""
    year = sources.year
    elections = []
    for rnd, spec in SOURCES[year].items():
        if year < 1999:
            _round_pre1999(con, sources, year, rnd, spec)
            _check_round(con, year, rnd)
            elections.append(Election("prezydent", year, rnd, DATES[(year, rnd)], f"Presidential election {year}, round {rnd}"))
            continue
        src = sources.load(f"prez_results_{rnd}", spec["results"])
        if year == 2015 and rnd == 2:
            src = _fix_2015_round2_teryt(con, src)
        if year == 2005:
            src = _fix_2005_warsaw(con, src, rnd)
        if year == 2000:
            src = _fix_2000_warsaw(con, sources, src)
        headers = vote_columns(con, src)
        if "candidates" in spec:
            _candidates_from_register(con, year, rnd, sources.load(f"prez_candidates_{rnd}", spec["candidates"]))
        else:
            _candidates_from_headers(con, year, rnd, [_header_name(h) for h in headers], spec.get("surname_first", False))
        keys = dict(con.execute(f"SELECT nkey, candidate_id FROM cand_{rnd}").fetchall())
        mapping = {h: keys[python_name_key(_header_name(h))] for h in headers if python_name_key(_header_name(h)) in keys}
        con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_unmapped AS SELECT unnest(?::VARCHAR[]) AS header",
                    [[h for h in headers if h not in mapping]])
        con.execute(f"CREATE OR REPLACE TABLE cand_{rnd}_columns AS SELECT unnest(?::INT[]) AS candidate_id", [list(mapping.values())])
        if column(con, src, *PROTOCOL_ALIASES["invalid_votes"], required=False):
            con.execute(f"CREATE OR REPLACE TABLE prot_{rnd} AS SELECT {precinct_select(con, src)} FROM {src}")
        else:  # 2010 publishes votes cast and valid votes only; invalid votes are the difference.
            protocol = tuple(name for name in PROTOCOL_ALIASES if name != "invalid_votes")
            con.execute(
                f"""CREATE OR REPLACE TABLE prot_{rnd} AS
                    SELECT *, valid_ballots - valid_votes AS invalid_votes FROM (SELECT {precinct_select(con, src, protocol)} FROM {src})"""
            )
        unpivot_votes(con, src, mapping, f"votes_{rnd}", "candidate_id")
        if "gmina_totals" in spec:
            _fill_2015_totals(con, rnd, sources.load(f"prez_gmina_{rnd}", spec["gmina_totals"]))
        _check_round(con, year, rnd)
        elections.append(Election("prezydent", year, rnd, DATES[(year, rnd)], f"Presidential election {year}, round {rnd}"))
    _assemble(con, year)
    return elections


def _fill_2015_totals(con: duckdb.DuckDBPyConnection, rnd: int, gmina_src: str) -> None:
    """2010/2015: candidate totals from precinct sums; keep the per-gmina file's totals for the check."""
    headers = vote_columns(con, gmina_src)
    keys = dict(con.execute(f"SELECT nkey, candidate_id FROM cand_{rnd}").fetchall())
    selects = [
        f"SELECT {keys[python_name_key(_header_name(h))]} AS candidate_id, sum(int_(\"{h}\")) AS votes FROM {gmina_src}"
        for h in headers if python_name_key(_header_name(h)) in keys
    ]
    con.execute(f"CREATE OR REPLACE TABLE gmina_totals_{rnd} AS " + " UNION ALL ".join(selects))
    con.execute(
        f"""
        UPDATE cand_{rnd} c SET votes = v.votes, pct = CAST(round(100.0 * v.votes / t.total, 2) AS DECIMAL(6, 2))
        FROM (SELECT candidate_id, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v,
             (SELECT sum(votes) AS total FROM votes_{rnd}) t
        WHERE v.candidate_id = c.candidate_id
        """
    )
    # The 2015 round 1 precinct file has no powiat column; take it from the per-gmina file.
    con.execute(
        f"""
        UPDATE prot_{rnd} p SET powiat = g."Powiat"
        FROM {gmina_src} g WHERE lpad(trim(g.{column(con, gmina_src, "TERYT", "Kod gm.", "Kod gminy")}), 6, '0') = p.teryt_gmina AND p.powiat IS NULL
        """
    )
    # Elected: over 50% of valid votes (round 1) or the most votes in the run-off (round 2).
    con.execute(
        f"""
        UPDATE cand_{rnd} SET elected = CASE WHEN {rnd} = 2 THEN votes = (SELECT max(votes) FROM cand_{rnd}) ELSE pct > 50 END
        """
    )


def _check_round(con: duckdb.DuckDBPyConnection, year: int, rnd: int) -> None:
    """Consistency checks of one round."""
    checks = {
        f"round {rnd}: duplicate precincts": f"SELECT count(*) - count(DISTINCT pkey) FROM prot_{rnd}",
        # Warsaw is split into its districts in every year (2005: see _fix_2005_warsaw).
        f"round {rnd}: Warsaw precincts without a district (146501)": f"SELECT count(*) FROM prot_{rnd} WHERE teryt_gmina = '146501'",
        # 2000: every pre-2002 Warsaw gmina, Warszawa-Centrum included, is mapped to districts (_fix_2000_warsaw).
        f"round {rnd}: Warsaw precincts with a pre-2002 code (1431xx)": f"""
            SELECT count(*) FROM prot_{rnd} WHERE teryt_gmina LIKE '1431%'""",
        f"round {rnd}: vote columns that match no candidate": f"SELECT count(*) FROM cand_{rnd}_unmapped",
        f"round {rnd}: candidates not read from exactly one column": f"""
            SELECT count(*) FROM cand_{rnd} c
            WHERE (SELECT count(*) FROM cand_{rnd}_columns m WHERE m.candidate_id = c.candidate_id) <> 1""",
        # Known gap: a few military precincts abroad are published as "-" but counted in the official
        # totals (2020 round 1: 4 precincts). Precinct sums may then fall short, never exceed.
        f"round {rnd}: candidates with more precinct votes than the official total": f"""
            SELECT count(*) FROM cand_{rnd} c
            LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v USING (candidate_id)
            WHERE coalesce(v.votes, 0) > c.votes""",
        f"round {rnd}: candidates short of the official total although every precinct is published": f"""
            SELECT count(*) FROM cand_{rnd} c
            LEFT JOIN (SELECT candidate_id, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v USING (candidate_id)
            WHERE coalesce(v.votes, 0) < c.votes
              AND NOT EXISTS (SELECT 1 FROM prot_{rnd} WHERE valid_votes IS NULL)""",
        f"round {rnd}: precincts whose valid votes differ from the candidate votes": f"""
            SELECT count(*) FROM prot_{rnd} p
            LEFT JOIN (SELECT pkey, sum(votes) AS votes FROM votes_{rnd} GROUP BY ALL) v USING (pkey)
            WHERE p.valid_votes IS NOT NULL AND p.valid_votes <> coalesce(v.votes, 0)""",
        f"round {rnd}: elected candidates (round 2 must have exactly one)": f"""
            SELECT CASE WHEN {rnd} = 2 THEN abs(count(*) FILTER (WHERE elected) - 1) ELSE 0 END FROM cand_{rnd}""",
    }
    if con.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_name = 'gmina_totals_{rnd}'").fetchone()[0]:
        checks[f"round {rnd}: candidates whose precinct votes differ from the per-gmina file"] = f"""
            SELECT count(*) FROM cand_{rnd} c JOIN gmina_totals_{rnd} g USING (candidate_id) WHERE g.votes <> c.votes"""
    run_checks(con, year, checks)
    unpublished, gap = con.execute(
        f"""
        SELECT (SELECT count(*) FROM prot_{rnd} WHERE valid_votes IS NULL),
               (SELECT sum(votes) FROM cand_{rnd}) - (SELECT sum(votes) FROM votes_{rnd})
        """
    ).fetchone()
    if unpublished:
        log.info("year %s round %s: %s unpublished precincts, %s votes only in official totals", year, rnd, unpublished, gap)


def _assemble(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Final DuckDB tables named like the mart tables (rounds stacked; precinct ids per round)."""
    rounds = sorted(SOURCES[year])
    con.execute(
        "CREATE OR REPLACE TABLE prezydent_candidates AS "
        + " UNION ALL ".join(
            f"""SELECT year, round, candidate_id, full_name, last_name, first_names, gender, occupation, residence,
                       residence_teryt, party_membership, committee_name, votes, pct, elected,
                       CASE WHEN round = 1 THEN {"nkey IN (SELECT nkey FROM cand_2)" if 2 in rounds else "false"} END AS advanced_to_round2
                FROM cand_{r}"""
            for r in rounds
        )
    )
    for r in rounds:
        con.execute(
            f"CREATE OR REPLACE TABLE prot_{r}_ids AS SELECT *, CAST(row_number() OVER (ORDER BY pkey) AS INT) AS precinct_id FROM prot_{r}"
        )
    con.execute(
        "CREATE OR REPLACE TABLE prezydent_precinct_protocols AS "
        + " UNION ALL ".join(
            f"""SELECT {year} AS year, {r} AS round, precinct_id, teryt_gmina, gmina, powiat, wojewodztwo, precinct_no,
                       address, eligible_voters, ballots_issued, valid_ballots, invalid_votes, valid_votes,
                       {"teryt_gmina_pre1999" if year < 1999 else "CAST(NULL AS VARCHAR) AS teryt_gmina_pre1999"}
                FROM prot_{r}_ids"""
            for r in rounds
        )
    )
    con.execute(
        "CREATE OR REPLACE TABLE prezydent_precinct_candidate_votes AS "
        + " UNION ALL ".join(
            f"SELECT {year} AS year, {r} AS round, p.precinct_id, v.candidate_id, v.votes FROM votes_{r} v JOIN prot_{r}_ids p USING (pkey)"
            for r in rounds
        )
    )
