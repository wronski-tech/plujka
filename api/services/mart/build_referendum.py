"""Build the nationwide referendum tables of one year in DuckDB (loader side).

Sources:
- 2003: ``prot-obw.xls`` (precincts) and ``prot-pow.xls`` (powiat totals, used for the check). Both have
  a three-row header and a row of column numbers above the data and an explanation of the codes below
  it; data rows are those with a 6-digit TERYT, columns are read by position (``_LAYOUT_2003``).
  Warsaw is one gmina (146501) with the district letter of ``WARSAW_DISTRICTS_2005``. The Supreme Court
  annulled the vote in two precincts (``ANNULLED_PRECINCTS``); they are dropped after the check against
  the powiat totals, so the national figures are PKW's corrected result of 21 July 2003.
- 2015: ``ref2015obw.xls`` (precincts; each question has four columns under a merged header
  ``Pytanie N``: invalid, valid, yes, no; the first data row repeats those sub-headers) and
  ``ref2015woj.xls`` (voivodeship totals, used for the check).
- 2023: ``wyniki_referendum_po_obwodach`` (columns ``Sprawa N - Liczba głosów ważnych|nieważnych|tak|nie``)
  and ``wyniki_referendum_po_gminach`` (gmina totals, used for the check).

Question texts are not part of the data files; they are the official questions of the referendum
orders and are stored in ``referendum_questions``.
"""

from __future__ import annotations

import logging
import re

import duckdb

from api.services.mart.build_common import BuildError, Election, YearSources, columns_of, quote_ident, run_checks
from api.services.mart.build_precincts import create_warsaw_districts, precinct_select

log = logging.getLogger("mart")

TABLES = ("referendum_questions", "referendum_precinct_protocols", "referendum_precinct_answers")
FAMILY = ("referendum",)

QUESTIONS = {
    2003: [
        ("przystąpienie Polski do Unii Europejskiej",
         "Czy wyraża Pani/Pan zgodę na przystąpienie Rzeczypospolitej Polskiej do Unii Europejskiej?"),
    ],
    2015: [
        ("jednomandatowe okręgi wyborcze (JOW)",
         "Czy jest Pani/Pan za wprowadzeniem jednomandatowych okręgów wyborczych w wyborach do Sejmu Rzeczypospolitej Polskiej?"),
        ("finansowanie partii politycznych z budżetu państwa",
         "Czy jest Pani/Pan za utrzymaniem dotychczasowego sposobu finansowania partii politycznych z budżetu państwa?"),
        ("rozstrzyganie wątpliwości podatkowych na korzyść podatnika",
         "Czy jest Pani/Pan za wprowadzeniem zasady ogólnej rozstrzygania wątpliwości co do wykładni przepisów prawa podatkowego na korzyść podatnika?"),
    ],
    2023: [
        ("wyprzedaż majątku państwowego",
         "Czy popierasz wyprzedaż majątku państwowego podmiotom zagranicznym, prowadzącą do utraty kontroli Polek i Polaków nad strategicznymi sektorami gospodarki?"),
        ("wiek emerytalny",
         "Czy popierasz podniesienie wieku emerytalnego, w tym przywrócenie podwyższonego do 67 lat wieku emerytalnego dla kobiet i mężczyzn?"),
        ("bariera na granicy z Białorusią",
         "Czy popierasz likwidację bariery na granicy Rzeczypospolitej Polskiej z Republiką Białoruską?"),
        ("relokacja nielegalnych imigrantów",
         "Czy popierasz przyjęcie tysięcy nielegalnych imigrantów z Bliskiego Wschodu i Afryki, zgodnie z przymusowym mechanizmem relokacji narzucanym przez biurokrację europejską?"),
    ],
}
# 2003: voting took place on 7 and 8 June; the referendum was ordered for 8 June.
DATES = {2003: "2003-06-08", 2015: "2015-09-06", 2023: "2023-10-15"}
SOURCES = {
    2003: {"precincts": "**/referendum/prot-obw.parquet", "totals": "**/referendum/prot-pow.parquet"},
    2015: {"precincts": "**/referendum/ref2015obw/*.parquet", "totals": "**/referendum/ref2015woj.parquet"},
    2023: {"precincts": "**/referendum/wyniki_referendum_po_obwodach_csv/*.parquet",
           "totals": "**/referendum/wyniki_referendum_po_gminach_csv/*.parquet"},
}
ANSWERS = ("valid_votes", "invalid_votes", "yes_votes", "no_votes")
# Precincts whose vote the Supreme Court annulled (resolution III SW 144/03 of 16 July 2003): (TERYT,
# number) -> (eligible voters, ballots issued, valid ballots, yes, no) as listed in PKW's announcement
# of 21 July 2003 on the corrected result.
ANNULLED_PRECINCTS = {
    2003: {("280501", 14): (2366, 1359, 1359, 1109, 247), ("026401", 15): (1139, 771, 745, 631, 110)},
}
# 2003 staged headers (merged cells of a three-row header) -> names the 2015/2023 code understands.
# The precinct file has 'Gmina' = TERYT, 'column1' = gmina name, ..., the powiat file 'Powiat ...' = TERYT.
_LAYOUT_2003 = {
    "precincts": {
        "Gmina": "TERYT", "column1": "Gmina", "column2": "Powiat", "Obwód głosowania": "Nr obw.", "column4": "Adres",
        "Miasto/ wieś/ dzielnica": "Teren", "Liczba uprawnio- nych do głoso- wania": "Liczba wyborców uprawnionych do głosowania",
        "Liczba wydanych kart": "Liczba wyborców, którym wydano karty do głosowania", "Karty ważne": "Liczba kart ważnych",
        "column10": "Sprawa 1 - Liczba głosów nieważnych", "column11": "Sprawa 1 - Liczba głosów ważnych",
        "column12": "Sprawa 1 - Liczba głosów tak", "column13": "Sprawa 1 - Liczba głosów nie",
    },
    "totals": {
        "Powiat (miasto na prawach powiatu)": "TERYT", "column6": "Sprawa 1 - Liczba głosów nieważnych",
        "column7": "Sprawa 1 - Liczba głosów ważnych", "column8": "Sprawa 1 - Liczba głosów tak",
        "column9": "Sprawa 1 - Liczba głosów nie",
    },
}
_SPRAWA = {"ważnych": "valid_votes", "nieważnych": "invalid_votes", "tak": "yes_votes", "nie": "no_votes"}


def available(sources: YearSources) -> bool:
    """Whether the year has a staged per-precinct referendum file."""
    spec = SOURCES.get(sources.year)
    return bool(spec) and sources.has(spec["precincts"])


def answer_columns(columns: list[str]) -> dict[int, dict[str, str]]:
    """Map question number -> {answer field: column} for both layouts.

    2023: ``Sprawa 2 - Liczba głosów tak``. 2015: ``Pytanie 2`` (invalid) followed by three unnamed
    columns (valid, yes, no).
    """
    out: dict[int, dict[str, str]] = {}
    for i, col in enumerate(columns):
        if m := re.match(r"^Sprawa (\d+) - Liczba głosów (ważnych|nieważnych|tak|nie)$", col):
            out.setdefault(int(m[1]), {})[_SPRAWA[m[2]]] = col
        elif m := re.match(r"^Pytanie (\d+)$", col):
            if i + 3 >= len(columns):
                raise BuildError(f"question block {col!r} is truncated")
            out[int(m[1])] = {"invalid_votes": col, "valid_votes": columns[i + 1], "yes_votes": columns[i + 2], "no_votes": columns[i + 3]}
    return out


def _rename_2003(con: duckdb.DuckDBPyConnection, raw: str, kind: str) -> str:
    """2003: data rows of a staged file with ``_LAYOUT_2003`` names; Warsaw precincts get their district.

    Rows without a 6-digit TERYT are the header, the column-number row and the code explanations;
    ``ref_2003_<kind>_other_rows`` keeps those that carry any number (a check allows only the
    column-number row).
    """
    layout = _LAYOUT_2003[kind]
    teryt = quote_ident(next(k for k, v in layout.items() if v == "TERYT"))
    numbers = [quote_ident(k) for k, v in layout.items() if v.startswith(("Liczba", "Sprawa"))]
    con.execute(
        f"""CREATE OR REPLACE TABLE ref_2003_{kind}_other_rows AS SELECT * FROM {raw}
            WHERE NOT regexp_full_match(coalesce(trim({teryt}), ''), '\\d{{6}}')
              AND coalesce({", ".join(f"int_({n})" for n in numbers)}) IS NOT NULL"""
    )
    exprs = {name: f"r.{quote_ident(header)}" for header, name in layout.items()}
    exprs["TERYT"] = f"trim(r.{teryt})"
    join = ""
    if kind == "precincts":
        create_warsaw_districts(con)
        exprs["TERYT"] = f"coalesce(w.teryt, trim(r.{teryt}))"
        exprs["Gmina"] = f"coalesce(w.gmina, {exprs['Gmina']})"
        join = f"""LEFT JOIN warsaw_districts_2005 w ON trim(r.{teryt}) = '146501' AND w.letter = trim(r."Miasto/ wieś/ dzielnica")"""
    select = ", ".join(f"{expr} AS {quote_ident(name)}" for name, expr in exprs.items())
    con.execute(
        f"""CREATE OR REPLACE TABLE ref_2003_{kind} AS SELECT {select} FROM {raw} r {join}
            WHERE regexp_full_match(coalesce(trim(r.{teryt}), ''), '\\d{{6}}')"""
    )
    return f"ref_2003_{kind}"


def build_referendum(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check the referendum of ``sources.year``; return the loaded election."""
    year = sources.year
    raw = sources.load("ref_precincts", SOURCES[year]["precincts"])
    totals = sources.load("ref_totals", SOURCES[year]["totals"])
    if year == 2003:
        raw, totals = _rename_2003(con, raw, "precincts"), _rename_2003(con, totals, "totals")
    number = "\"Nr komisji\"" if year == 2023 else "\"Nr obw.\""
    # 2015: the first data row repeats the sub-headers ("gł. niew.", "ważne", ...).
    con.execute(f"CREATE OR REPLACE TABLE ref_src AS SELECT *, row_number() OVER () AS rn FROM {raw} WHERE int_({number}) IS NOT NULL")
    questions = answer_columns(columns_of(con, "ref_src"))
    if sorted(questions) != list(range(1, len(QUESTIONS[year]) + 1)):
        raise BuildError(f"year {year}: found questions {sorted(questions)}, expected {len(QUESTIONS[year])}")

    con.execute(
        f"""CREATE OR REPLACE TABLE ref_prot AS
        SELECT rn, {precinct_select(con, 'ref_src', ('eligible_voters', 'ballots_issued', 'valid_ballots'))} FROM ref_src"""
    )
    con.execute("CREATE OR REPLACE TABLE ref_ids AS SELECT *, CAST(row_number() OVER (ORDER BY pkey) AS INT) AS precinct_id FROM ref_prot")
    con.execute(
        f"""
        CREATE OR REPLACE TABLE referendum_precinct_protocols AS
        SELECT {year} AS year, precinct_id, teryt_gmina, gmina, powiat, wojewodztwo, precinct_no,
               eligible_voters, ballots_issued, valid_ballots
        FROM ref_ids
        """
    )
    con.execute(
        "CREATE OR REPLACE TABLE referendum_precinct_answers AS "
        + " UNION ALL ".join(
            f"SELECT {year} AS year, precinct_id, {q} AS question_no, "
            + ", ".join(f"CAST(int_({quote_ident(cols[a])}) AS INT) AS {a}" for a in ANSWERS)
            + " FROM ref_src JOIN ref_ids USING (rn)"
            for q, cols in sorted(questions.items())
        )
    )
    con.execute("CREATE OR REPLACE TABLE referendum_questions (year INT, question_no INT, topic VARCHAR, question_text VARCHAR)")
    con.executemany(
        "INSERT INTO referendum_questions VALUES (?, ?, ?, ?)",
        [[year, i, topic, text] for i, (topic, text) in enumerate(QUESTIONS[year], start=1)],
    )
    _check(con, year, totals, answer_columns(columns_of(con, totals)))
    _drop_annulled(con, year)
    return [Election("referendum", year, 1, DATES[year], f"Nationwide referendum {year}")]


def _drop_annulled(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Remove the ANNULLED_PRECINCTS of ``year`` after checking their figures against the announcement."""
    annulled = ANNULLED_PRECINCTS.get(year, {})
    if not annulled:
        return
    con.execute("CREATE OR REPLACE TABLE ref_annulled (pkey VARCHAR, eligible INT, issued INT, valid_ballots INT, yes INT, no INT)")
    con.executemany(
        "INSERT INTO ref_annulled VALUES (pkey(?, ?), ?, ?, ?, ?, ?)", [[t, str(n), *figures] for (t, n), figures in annulled.items()]
    )
    run_checks(con, year, {
        "annulled precincts missing or with other figures than in PKW's corrected result": """
            SELECT count(*) FROM ref_annulled x
            WHERE NOT EXISTS (
                SELECT 1 FROM ref_ids i JOIN referendum_precinct_answers a USING (precinct_id)
                WHERE i.pkey = x.pkey AND i.eligible_voters = x.eligible AND i.ballots_issued = x.issued
                  AND i.valid_ballots = x.valid_ballots AND a.yes_votes = x.yes AND a.no_votes = x.no)""",
    })
    ids = "(SELECT precinct_id FROM ref_ids WHERE pkey IN (SELECT pkey FROM ref_annulled))"
    con.execute(f"DELETE FROM referendum_precinct_answers WHERE precinct_id IN {ids}")
    con.execute(f"DELETE FROM referendum_precinct_protocols WHERE precinct_id IN {ids}")
    log.info("year %s: dropped %s precincts annulled by the Supreme Court", year, len(annulled))


def _check(con: duckdb.DuckDBPyConnection, year: int, totals: str, total_columns: dict[int, dict[str, str]]) -> None:
    """Precinct answers must add up to the published territorial totals, question by question.

    2015 compares all precincts with the voivodeship file. 2023 compares domestic precincts only: the
    gmina file lists abroad results by country and omits ships and some abroad precincts (244 valid
    votes on question 1), while the precinct file matches the official national result.
    """
    domestic_only = year == 2023
    precinct_filter = "AND p.teryt_gmina IS NOT NULL" if domestic_only else ""
    total_filter = 'WHERE "TERYT Gminy" IS NOT NULL' if domestic_only else ""
    checks = {"duplicate precincts": "SELECT count(*) - count(DISTINCT pkey) FROM ref_prot"}
    if year == 2003:
        # Above the data: three header rows and the column numbers (1, 2, 3, ...); below: code explanations.
        checks["2003: rows without a TERYT that carry numbers, other than the column-number row"] = """
            SELECT (SELECT count(*) FROM ref_2003_precincts_other_rows WHERE NOT (trim("Gmina") = '1' AND trim("column1") = '2'))
                 + (SELECT count(*) FROM ref_2003_totals_other_rows
                    WHERE NOT (trim("Powiat (miasto na prawach powiatu)") = '1' AND trim("column1") = '2'))"""
        checks["2003: Warsaw precincts (146501) without a district letter"] = """
            SELECT count(*) FROM ref_prot WHERE teryt_gmina = '146501'"""
    for q, cols in sorted(total_columns.items()):
        for answer in ANSWERS:
            checks[f"question {q}: {answer} differ from the territorial totals"] = f"""
                SELECT abs((SELECT coalesce(sum(a.{answer}), 0) FROM referendum_precinct_answers a
                            JOIN referendum_precinct_protocols p USING (precinct_id) WHERE a.question_no = {q} {precinct_filter})
                         - (SELECT coalesce(sum(int_({quote_ident(cols[answer])})), 0) FROM {totals} {total_filter}))"""
    checks["yes + no != valid votes"] = """
        SELECT count(*) FROM referendum_precinct_answers WHERE yes_votes + no_votes <> valid_votes"""
    run_checks(con, year, checks)
