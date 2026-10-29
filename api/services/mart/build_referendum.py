"""Build the nationwide referendum tables of one year in DuckDB (loader side).

Sources:
- 1996, 1997: ``ref96obw`` / ``ref97obw`` (precincts) and ``ref96gm`` / ``ref97gm`` (gmina totals, used for the
  check), workbooks with merged headers read by position (``_LAYOUT_PRE1999``); pre-1999 geography and the
  checks against PKW's announcements are in ``_build_pre1999``. 1996 had two referendums on the same day:
  question 1 (uwłaszczenie) on one ballot, questions 2-5 (state property) on another.
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

from api.services.mart.build_common import BuildError, Election, YearSources, columns_of, quote_ident, run_checks, sql_literal
from api.services.mart.build_precincts import create_voivodeships_1975, create_warsaw_districts, precinct_select

log = logging.getLogger("mart")

TABLES = ("referendum_questions", "referendum_precinct_protocols", "referendum_precinct_answers")
FAMILY = ("referendum",)

QUESTIONS = {
    1996: [
        # Referendum o powszechnym uwłaszczeniu obywateli (ordered by the President, Dz.U. 1995 nr 138 poz. 685).
        ("powszechne uwłaszczenie obywateli", "Czy jesteś za przeprowadzeniem powszechnego uwłaszczenia obywateli?"),
        # Referendum o niektórych kierunkach wykorzystania majątku państwowego (Sejm, Dz.U. 1995 nr 154 poz. 795),
        # questions 1-4 on one ballot.
        ("majątek państwowy: zobowiązania wobec emerytów i budżetówki",
         "Czy jesteś za tym, aby zobowiązania wobec emerytów i rencistów oraz pracowników sfery budżetowej, wynikające z orzeczeń Trybunału Konstytucyjnego, były zaspokojone z prywatyzowanego majątku państwowego?"),
        ("majątek państwowy: powszechne fundusze emerytalne",
         "Czy jesteś za tym, aby część prywatyzowanego majątku państwowego zasiliła powszechne fundusze emerytalne?"),
        ("majątek państwowy: Narodowe Fundusze Inwestycyjne",
         "Czy jesteś za tym, aby zwiększyć wartość świadectw udziałowych Narodowych Funduszy Inwestycyjnych przez objęcie tym programem dalszych przedsiębiorstw?"),
        ("majątek państwowy: bony prywatyzacyjne", "Czy jesteś za uwzględnieniem w programie uwłaszczeniowym bonów prywatyzacyjnych?"),
    ],
    1997: [
        ("Konstytucja RP",
         "Czy jesteś za przyjęciem Konstytucji Rzeczypospolitej Polskiej uchwalonej przez Zgromadzenie Narodowe w dniu 2 kwietnia 1997 r.?"),
    ],
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
DATES = {1996: "1996-02-18", 1997: "1997-05-25", 2003: "2003-06-08", 2015: "2015-09-06", 2023: "2023-10-15"}
SOURCES = {
    1996: {"precincts": "**/referendum/ref96obw.parquet", "totals": "**/referendum/ref96gm.parquet"},
    1997: {"precincts": "**/referendum/Ref1997/ref97obw__*.parquet", "totals": "**/referendum/Ref1997/ref97gm__*.parquet"},
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


# Pre-1999 precinct and gmina workbooks: merged two- or three-row headers above the data, read by position.
# question -> (ballots issued, valid ballots, invalid votes, valid votes, yes, no) columns, each with a
# pattern its header text must match (checked). 1996: question 1 is the uwłaszczenie referendum (own ballot),
# questions 2-5 the four questions of the state-property referendum (one ballot).
_U, _M = "Referendum o powszechnym uwłaszczeniu obywateli", "Referendum o niektórych kierunkach wykorzystania majątku państwowego"
_LAYOUT_PRE1999 = {
    1996: {
        1: (_U, "column10", "column11", "column14", "column15", "column16"),
        2: (_M, "column18", "column19", "column22", "column23", "column24"),
        3: (_M, "column18", "column25", "column28", "column29", "column30"),
        4: (_M, "column18", "column31", "column34", "column35", "column36"),
        5: (_M, "column18", "column37", "column40", "column41", "column42"),
    },
    1997: {1: ("Wydane karty do głosowania", "Frekw.", "Głosy nieważne", "Głosy ważne", "column15", "column16")},
}
# The gmina workbooks have three columns fewer before the answers ('Nr obw.', 'Adres', 'Kod obw.' and 'Typ obw.'
# are replaced by 'Liczba obwodów' / 'L. obw.').
_GMINA_SHIFT = {1996: 3, 1997: 3}
_HEADER_PATTERNS = ("Wydane karty", "Frekw", "nieważne", "Głosy ważne", '"TAK"', '"NIE"')
# Precinct answers that contradict the precinct's own totals, as published: (year, gmina code, number, column)
# -> (published value, corrected value). The correction makes yes + no equal valid votes and reproduces PKW's
# national result.
CORRECTIONS_PRE1999 = {
    # Żuromin precinct 8: 'NIE' published as 13101 with 29 valid votes and 18 'TAK'; PKW's national 'NIE'
    # (5,570,493) is the file's sum minus 13,090, i.e. 11 votes here.
    (1997, "13309", 8, "column16"): ("13101", "11"),
}
# Gmina workbook rows that differ from their precincts, as published: (year, gmina code, question, answer) ->
# published - precinct sum. Żuromin 1997: the gmina row lacks the 11 'NIE' votes of precinct 8.
GMINA_FILE_GAPS = {(1997, "13309", 1, "no_votes"): -11}
# PKW's announcements (1996: Dz.U. 1996 nr 22 poz. 101; 1997: corrected result of 8 July 1997, Dz.U. 1997 nr 75
# poz. 476, without the three precincts the Supreme Court annulled, which the KBW files omit too).
OFFICIAL_PRE1999 = {
    1996: {
        "precincts": 22459, "eligible_voters": 28009715,
        "valid_ballots": {1: 9076004, 2: 9085145},
        "answers": {1: (8923326, 152678, 8580129, 343197), 2: (8876924, 208221, 8439458, 437466),
                    3: (8868294, 216851, 8512931, 355363), 4: (8574126, 511019, 1985567, 6588559),
                    5: (8785258, 299887, 8022353, 762905)},
    },
    1997: {
        "precincts": 22792, "eligible_voters": 28319650, "valid_ballots": {1: 12137136},
        "answers": {1: (11967134, 170002, 6396641, 5570493)},
    },
}


def _build_pre1999(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """1996 and 1997: precinct workbooks with positional columns and the pre-1999 geography.

    Like the presidential elections of 1990/1995, teryt_gmina and powiat stay NULL, the 5-digit gmina code goes
    to teryt_gmina_pre1999 and the 1975-1998 voivodeship to wojewodztwo (NULL abroad, type Z, and on ships,
    type S). 1996 had two referendums on separate ballots, so its ballot numbers are per question
    (referendum_precinct_answers.ballots_issued / valid_ballots) and NULL in the protocol table. The 1997
    workbook repeats 288 precinct rows of other gminas to the right of the data (columns 17-31); a check
    requires each to duplicate a precinct row, and they are ignored.
    """
    year = sources.year
    layout = _LAYOUT_PRE1999[year]
    raw = sources.load("ref_precincts", SOURCES[year]["precincts"])
    totals = sources.load("ref_totals", SOURCES[year]["totals"])
    gmina_name = "Nazwa gm." if year == 1996 else "Nazwa gminy"
    for (y, code, number, col), (published, corrected) in CORRECTIONS_PRE1999.items():
        if y == year:
            con.execute(
                f"""UPDATE {raw} SET {quote_ident(col)} = {sql_literal(corrected)}
                    WHERE trim("Kod gm.") = {sql_literal(code)} AND int_("Nr obw.") = {number} AND trim({quote_ident(col)}) = {sql_literal(published)}"""
            )
    header_rows = f"""(SELECT * FROM {raw} WHERE int_("Nr obw.") IS NULL)"""
    checks = {}
    for q, cols in layout.items():
        for col, pattern in zip(cols, _HEADER_PATTERNS):
            checks[f"question {q}: header of {col} does not match {pattern!r}"] = f"""
                SELECT CASE WHEN {sql_literal(col)} || ' ' || coalesce(string_agg({quote_ident(col)}, ' '), '')
                                 LIKE {sql_literal('%' + pattern + '%')} THEN 0 ELSE 1 END FROM {header_rows}"""
    create_voivodeships_1975(con)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE ref_src AS
        SELECT r.*, trim(r."Kod gm.") AS code, trim(r."Typ obw.") AS ptype,
               CASE trim(r."Typ obw.") WHEN 'Z' THEN 'zagranica/' WHEN 'S' THEN 'statek/' || trim(r."Kod gm.") || '/'
                    ELSE trim(r."Kod gm.") || '/' END || CAST(int_(r."Nr obw.") AS VARCHAR) AS pkey
        FROM {raw} r WHERE int_(r."Nr obw.") IS NOT NULL
        """
    )
    q1 = layout[1]
    con.execute(
        f"""
        CREATE OR REPLACE TABLE ref_ids AS
        SELECT pkey, code, CAST(row_number() OVER (ORDER BY pkey) AS INT) AS precinct_id,
               CASE WHEN ptype NOT IN ('Z', 'S') THEN code END AS teryt_gmina_pre1999,
               CASE ptype WHEN 'Z' THEN 'zagranica' WHEN 'S' THEN 'statki' ELSE {quote_ident(gmina_name)} END AS gmina,
               CASE WHEN ptype NOT IN ('Z', 'S') THEN v.name END AS wojewodztwo,
               CAST(int_("Nr obw.") AS INT) AS precinct_no, CAST(int_("Upr.") AS INT) AS eligible_voters,
               {"CAST(NULL AS INT)" if year == 1996 else f"CAST(int_({quote_ident(q1[0])}) AS INT)"} AS ballots_issued,
               {"CAST(NULL AS INT)" if year == 1996 else f"CAST(int_({quote_ident(q1[1])}) AS INT)"} AS valid_ballots
        FROM ref_src LEFT JOIN (SELECT code AS woj_code, name FROM voivodeships_1975) v ON v.woj_code = left(ref_src.code, 2)
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE referendum_precinct_protocols AS
        SELECT {year} AS year, precinct_id, CAST(NULL AS VARCHAR) AS teryt_gmina, gmina, CAST(NULL AS VARCHAR) AS powiat,
               wojewodztwo, precinct_no, eligible_voters, ballots_issued, valid_ballots, teryt_gmina_pre1999
        FROM ref_ids
        """
    )
    con.execute(
        "CREATE OR REPLACE TABLE referendum_precinct_answers AS "
        + " UNION ALL ".join(
            f"""SELECT {year} AS year, precinct_id, {q} AS question_no,
                       CAST(int_({quote_ident(valid)}) AS INT) AS valid_votes, CAST(int_({quote_ident(invalid)}) AS INT) AS invalid_votes,
                       CAST(int_({quote_ident(yes)}) AS INT) AS yes_votes, CAST(int_({quote_ident(no)}) AS INT) AS no_votes,
                       {f"CAST(int_({quote_ident(issued)}) AS INT)" if year == 1996 else "CAST(NULL AS INT)"} AS ballots_issued,
                       {f"CAST(int_({quote_ident(ballots)}) AS INT)" if year == 1996 else "CAST(NULL AS INT)"} AS valid_ballots
                FROM ref_src JOIN ref_ids USING (pkey)"""
            for q, (issued, ballots, invalid, valid, yes, no) in sorted(layout.items())
        )
    )
    con.execute("CREATE OR REPLACE TABLE referendum_questions (year INT, question_no INT, topic VARCHAR, question_text VARCHAR)")
    con.executemany(
        "INSERT INTO referendum_questions VALUES (?, ?, ?, ?)",
        [[year, i, topic, text] for i, (topic, text) in enumerate(QUESTIONS[year], start=1)],
    )
    official = OFFICIAL_PRE1999[year]
    corrected = [
        f"""(code = {sql_literal(c)} AND int_("Nr obw.") = {n} AND trim({quote_ident(col)}) = {sql_literal(v[1])})"""
        for (y, c, n, col), v in CORRECTIONS_PRE1999.items() if y == year
    ]
    checks |= {
        "duplicate precincts": "SELECT count(*) - count(DISTINCT pkey) FROM ref_ids",
        "corrections that matched no published value": f"""
            SELECT {len(corrected)} - count(*) FROM ref_src WHERE {" OR ".join(corrected) or "false"}""",
        "domestic precincts without a 1975-1998 voivodeship": """
            SELECT count(*) FROM ref_ids WHERE teryt_gmina_pre1999 IS NOT NULL AND wojewodztwo IS NULL""",
        "yes + no != valid votes": "SELECT count(*) FROM referendum_precinct_answers WHERE yes_votes + no_votes <> valid_votes",
        f"precincts other than in PKW's announcement ({official['precincts']})": f"SELECT abs(count(*) - {official['precincts']}) FROM ref_ids",
        "eligible voters differ from PKW's announcement": f"SELECT abs(sum(eligible_voters) - {official['eligible_voters']}) FROM ref_ids",
    }
    for q, ballots in official["valid_ballots"].items():
        source = "referendum_precinct_answers" if year == 1996 else "referendum_precinct_protocols"
        where = f"WHERE question_no = {q}" if year == 1996 else ""
        checks[f"question {q}: valid ballots differ from PKW's announcement"] = f"SELECT abs(sum(valid_ballots) - {ballots}) FROM {source} {where}"
    for q, figures in official["answers"].items():
        for answer, value in zip(ANSWERS, figures):
            checks[f"question {q}: {answer} differ from PKW's announcement"] = f"""
                SELECT abs(sum({answer}) - {value}) FROM referendum_precinct_answers WHERE question_no = {q}"""
    if year == 1997:
        checks["1997: rows right of the data that duplicate no precinct row"] = f"""
            SELECT count(*) FROM {raw} x WHERE x.column17 IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM {raw} r WHERE trim(r."Kod obw.") = trim(x.column22) AND int_(r."Nr obw.") = int_(x.column20)
                  AND int_(r."Upr.") = int_(x.column24) AND int_(r."Frekw.") = int_(x.column25)
                  AND int_(r."Głosy ważne") = int_(x.column29) AND int_(r.column15) = int_(x.column30)
                  AND int_(r.column16) = int_(x.column31))"""
    _check_gminas_pre1999(con, year, totals, layout, checks)
    run_checks(con, year, checks)
    return [Election("referendum", year, 1, DATES[year], f"Nationwide referendum{'s' if year == 1996 else ''} {year}")]


def _check_gminas_pre1999(con: duckdb.DuckDBPyConnection, year: int, totals: str, layout: dict, checks: dict[str, str]) -> None:
    """Add checks: precinct answers per gmina code equal the gmina workbook (columns shifted by
    ``_GMINA_SHIFT``), except the named GMINA_FILE_GAPS."""
    columns = columns_of(con, totals)
    precinct_columns = columns_of(con, "ref_src")
    shift = _GMINA_SHIFT[year]
    gaps = {(c, q, a): d for (y, c, q, a), d in GMINA_FILE_GAPS.items() if y == year}
    for q, cols in layout.items():
        for answer, col in zip(ANSWERS, (cols[3], cols[2], cols[4], cols[5])):
            gmina_col = columns[precinct_columns.index(col) - shift]
            exceptions = " ".join(
                f"WHEN g.code = {sql_literal(c)} THEN {d}" for (c, qq, a), d in gaps.items() if qq == q and a == answer
            )
            checks[f"question {q}: {answer} per gmina differ from the gmina workbook ({gmina_col})"] = f"""
                SELECT count(*) FROM (SELECT trim("Kod gm.") AS code, sum(int_({quote_ident(gmina_col)})) AS votes
                                      FROM {totals} WHERE int_("Upr.") IS NOT NULL GROUP BY 1) g
                FULL JOIN (SELECT i.code, sum(a.{answer}) AS votes FROM referendum_precinct_answers a JOIN ref_ids i USING (precinct_id)
                           WHERE a.question_no = {q} GROUP BY 1) p USING (code)
                WHERE coalesce(g.votes, 0) - coalesce(p.votes, 0) <> CASE {exceptions or "WHEN false THEN 0"} ELSE 0 END"""


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
    if year < 1999:
        return _build_pre1999(con, sources)
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
               eligible_voters, ballots_issued, valid_ballots, CAST(NULL AS VARCHAR) AS teryt_gmina_pre1999
        FROM ref_ids
        """
    )
    con.execute(
        "CREATE OR REPLACE TABLE referendum_precinct_answers AS "
        + " UNION ALL ".join(
            f"SELECT {year} AS year, precinct_id, {q} AS question_no, "
            + ", ".join(f"CAST(int_({quote_ident(cols[a])}) AS INT) AS {a}" for a in ANSWERS)
            + ", CAST(NULL AS INT) AS ballots_issued, CAST(NULL AS INT) AS valid_ballots"
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
