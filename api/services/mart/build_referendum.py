"""Build the nationwide referendum tables of one year in DuckDB (loader side).

Sources:
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
from api.services.mart.build_precincts import precinct_select

log = logging.getLogger("mart")

TABLES = ("referendum_questions", "referendum_precinct_protocols", "referendum_precinct_answers")
FAMILY = ("referendum",)

QUESTIONS = {
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
DATES = {2015: "2015-09-06", 2023: "2023-10-15"}
SOURCES = {
    2015: {"precincts": "**/referendum/ref2015obw/*.parquet", "totals": "**/referendum/ref2015woj.parquet"},
    2023: {"precincts": "**/referendum/wyniki_referendum_po_obwodach_csv/*.parquet",
           "totals": "**/referendum/wyniki_referendum_po_gminach_csv/*.parquet"},
}
ANSWERS = ("valid_votes", "invalid_votes", "yes_votes", "no_votes")
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


def build_referendum(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Build and check the referendum of ``sources.year``; return the loaded election."""
    year = sources.year
    raw = sources.load("ref_precincts", SOURCES[year]["precincts"])
    totals = sources.load("ref_totals", SOURCES[year]["totals"])
    number = "\"Nr obw.\"" if year == 2015 else "\"Nr komisji\""
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
    return [Election("referendum", year, 1, DATES[year], f"Nationwide referendum {year}")]


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
    for q, cols in sorted(total_columns.items()):
        for answer in ANSWERS:
            checks[f"question {q}: {answer} differ from the territorial totals"] = f"""
                SELECT abs((SELECT coalesce(sum(a.{answer}), 0) FROM referendum_precinct_answers a
                            JOIN referendum_precinct_protocols p USING (precinct_id) WHERE a.question_no = {q} {precinct_filter})
                         - (SELECT coalesce(sum(int_({quote_ident(cols[answer])})), 0) FROM {totals} {total_filter}))"""
    checks["yes + no != valid votes"] = """
        SELECT count(*) FROM referendum_precinct_answers WHERE yes_votes + no_votes <> valid_votes"""
    run_checks(con, year, checks)
