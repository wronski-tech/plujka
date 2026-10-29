"""Shared building blocks for loading one election year into the mart (loader side, needs DuckDB).

A year is built in an in-memory DuckDB database from staged Parquet (``scripts/kbw_stage_duckdb.py``):
source tables are loaded as ``src_*`` (all VARCHAR), transformed into tables named exactly like the
PostgreSQL mart tables, checked against official totals, and then copied to PostgreSQL by
``load_tables`` (one transaction per election family and year, see ``scripts/build_mart.py``).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import duckdb
import psycopg


log = logging.getLogger("mart")

# Voivodeship names by the first two TERYT digits (stable since 1999).
VOIVODESHIPS = {
    "02": "dolnośląskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie",
    "10": "łódzkie", "12": "małopolskie", "14": "mazowieckie", "16": "opolskie",
    "18": "podkarpackie", "20": "podlaskie", "22": "pomorskie", "24": "śląskie",
    "26": "świętokrzyskie", "28": "warmińsko-mazurskie", "30": "wielkopolskie", "32": "zachodniopomorskie",
}

# Colloquial committee names, matched as upper-case substrings of the official name. Order matters:
# the first matching key wins (e.g. "TRZECIA DROGA ... POLSKIE STRONNICTWO LUDOWE" must hit TRZECIA DROGA).
COMMITTEE_ALIASES: list[tuple[str, list[str]]] = [
    ("PRAWO I SPRAWIEDLIWOŚĆ", ["PiS", "Zjednoczona Prawica"]),
    # Small 2004/2005 committees that the broad needles below would mislabel as KO and Konfederacja.
    ("OGÓLNOPOLSKA KOALICJA OBYWATELSKA", ["OKO", "Ogólnopolska Koalicja Obywatelska"]),
    ("KONFEDERACJA RUCH OBRONY BEZROBOTNYCH", ["KROB"]),
    # Local elections 2002/2006 committees that "KONFEDERACJA" below would mislabel.
    ("KONFEDERACJA POLSKI NIEPODLEGŁEJ", ["KPN", "Konfederacja Polski Niepodległej"]),
    ("KONFEDERACJI POLSKI NIEPODLEGŁEJ", ["KPN", "Konfederacja Polski Niepodległej"]),
    ("KONFEDERACJA - GODNOŚĆ I PRACA", ["Godność i Praca"]),
    ("KOALICJA OBYWATELSKA", ["KO", "PO", "Platforma Obywatelska"]),
    ("PLATFORMA OBYWATELSKA", ["PO", "Platforma Obywatelska"]),
    # European Parliament 2019: PO-led coalition with PSL, SLD, .N and Zieloni.
    ("KOALICJA EUROPEJSKA", ["KE", "Koalicja Europejska", "PO"]),
    ("TRZECIA DROGA", ["TD", "Trzecia Droga", "Polska 2050", "PSL"]),
    ("NOWA LEWICA", ["Lewica", "NL"]),
    ("SOJUSZ LEWICY DEMOKRATYCZNEJ", ["Lewica", "SLD"]),
    ("SLD LEWICA RAZEM", ["Lewica", "SLD"]),
    ("ZJEDNOCZONA LEWICA", ["ZL", "Zjednoczona Lewica"]),
    ("LEWICA RAZEM", ["Lewica Razem", "Razem"]),
    ("WIOSNA", ["Wiosna"]),
    ("KONFEDERACJA", ["Konfederacja"]),
    ("POLSKIE STRONNICTWO LUDOWE", ["PSL", "Koalicja Polska"]),
    ("WYBORCZY PSL", ["PSL"]),
    ("KUKIZ", ["Kukiz'15", "Kukiz"]),
    ("NOWOCZESNA", ["Nowoczesna"]),
    # Sejm 2011 and European Parliament 2014 ("POLSKA RAZEM" must precede "RAZEM", "NOWA PRAWICA - JANUSZA
    # KORWIN-MIKKE" must precede "KORWIN").
    ("NOWA PRAWICA", ["KNP", "Nowa Prawica", "Kongres Nowej Prawicy", "KORWiN"]),
    ("RUCH PALIKOTA", ["Ruch Palikota"]),
    ("POLSKA JEST NAJWAŻNIEJSZA", ["PJN", "Polska Jest Najważniejsza"]),
    ("SOLIDARNA POLSKA", ["Solidarna Polska", "SP"]),
    ("POLSKA RAZEM", ["Polska Razem"]),
    ("EUROPA PLUS", ["Europa Plus Twój Ruch", "Twój Ruch"]),
    ("KORWIN", ["KORWiN"]),
    # Only the party: local committees such as "ZAWSZE RAZEM" or "ŚLONZOKI RAZEM" are not Razem.
    ("PARTIA RAZEM", ["Razem"]),
    ("PARTII RAZEM", ["Razem"]),
    ("JOW BEZPARTYJNI", ["JOW"]),
    # Only the Bezpartyjni Samorządowcy movement, not every committee calling itself non-partisan.
    ("BEZPARTYJNI SAMORZĄDOWCY", ["Bezpartyjni", "BS"]),
    ("KOALICJA BEZPARTYJNI I SAMORZĄDOWCY", ["Bezpartyjni", "BS"]),
    ("RUCH NARODOWY", ["RN", "Ruch Narodowy"]),
    ("TWÓJ RUCH", ["TR", "Twój Ruch"]),
    ("RUCH AUTONOMII ŚLĄSKA", ["RAŚ", "Ruch Autonomii Śląska"]),
    ("RUCHU AUTONOMII ŚLĄSKA", ["RAŚ", "Ruch Autonomii Śląska"]),
    ("PARTIA ZIELONI", ["Zieloni"]),
    ("WOLNOŚĆ W SAMORZĄDZIE", ["Wolność w Samorządzie"]),
    ("WOLNI I SOLIDARNI", ["WiS", "Wolni i Solidarni"]),
    ("POLSKA JEST JEDNA", ["PJJ"]),
    ("MNIEJSZOŚĆ NIEMIECKA", ["MN", "Mniejszość Niemiecka"]),
    # Local elections 2010: the German minority ran as the TSKN society.
    ("NIEMCÓW NA ŚLĄSKU OPOLSKIM", ["MN", "Mniejszość Niemiecka"]),
    ("SKUTECZNI PIOTRA", ["Skuteczni"]),
    ("AKCJA ZAWIEDZIONYCH EMERYTÓW", ["AZER"]),
    # 2004-2009 Sejm and EP committees (and sejmik/council committees of these parties). Genitive forms
    # are the 2004-2007 committee names ("Komitet Wyborczy Polskiego Stronnictwa Ludowego").
    # "LEWICA I DEMOKRACI" must precede the generic "WYBORCZY LEWICA" below.
    ("LEWICA I DEMOKRACI", ["LiD", "Lewica i Demokraci", "SLD"]),
    ("POLSKIEGO STRONNICTWA LUDOWEGO", ["PSL"]),
    ("SAMOOBRONA RZECZPOSPOLITEJ", ["Samoobrona"]),
    # 2001 committees: Samoobrona spelled "RZECZYPOSPOLITEJ", and the AWS successor coalition.
    ("SAMOOBRONA RZECZYPOSPOLITEJ", ["Samoobrona"]),
    ("AKCJA WYBORCZA SOLIDARNOŚĆ", ["AWSP", "AWS", "Akcja Wyborcza Solidarność"]),
    ("LIGA POLSKICH RODZIN", ["LPR", "Liga Polskich Rodzin"]),
    ("SOCJALDEMOKRACJI POLSKIEJ", ["SDPL", "Socjaldemokracja Polska"]),
    ("PARTII DEMOKRATYCZNEJ", ["PD", "Partia Demokratyczna"]),
    ("UNII WOLNOŚCI", ["UW", "Unia Wolności"]),
    ("POLITYKI REALNEJ", ["UPR", "Unia Polityki Realnej"]),
    # Generic last: 2024 committees named just "... LEWICA" (after the specific Lewica rules above).
    ("WYBORCZY LEWICA", ["Lewica"]),
]

MACROS = r"""
CREATE MACRO int_(x) AS coalesce(
    TRY_CAST(NULLIF(trim(x), '') AS BIGINT),
    TRY_CAST(TRY_CAST(replace(NULLIF(trim(x), ''), ',', '.') AS DOUBLE) AS BIGINT));
CREATE MACRO num_(x) AS TRY_CAST(replace(NULLIF(trim(x), ''), ',', '.') AS DOUBLE);
-- 6-digit gmina TERYT; NULL abroad/on ships (no code, or the pseudo code 1499xx used by 2015/2019).
CREATE MACRO clean_teryt(t) AS CASE
    WHEN NULLIF(trim(t), '') IS NULL OR lpad(trim(t), 6, '0') LIKE '1499%' THEN NULL
    ELSE lpad(trim(t), 6, '0') END;
-- Precinct identity shared by every per-precinct file of a year. Abroad/ship precincts have no
-- usable TERYT, but their numbers are unique nationally.
CREATE MACRO pkey(teryt, nr) AS coalesce(clean_teryt(teryt), 'zagranica') || '/' || CAST(int_(nr) AS VARCHAR);
-- Order-insensitive name key: 'Paweł Piotr KLESZCZ' and 'KLESZCZ Paweł Piotr' are equal.
CREATE MACRO name_key(n) AS array_to_string(list_sort(str_split(upper(trim(regexp_replace(n, '\s+', ' ', 'g'))), ' ')), ' ');
CREATE MACRO is_surname_token(t) AS upper(t) = t AND lower(t) <> t;
CREATE MACRO surname(n) AS array_to_string(list_filter(str_split(trim(n), ' '), t -> is_surname_token(t)), ' ');
CREATE MACRO given_names(n) AS array_to_string(list_filter(str_split(trim(n), ' '), t -> NOT is_surname_token(t)), ' ');
-- 'KLESZCZ Paweł Piotr' -> 'Paweł Piotr KLESZCZ' (registers publish both orders).
CREATE MACRO display_name(n) AS concat_ws(' ',
    NULLIF(given_names(regexp_replace(trim(n), '\s+', ' ', 'g')), ''),
    NULLIF(surname(regexp_replace(trim(n), '\s+', ' ', 'g')), ''));
CREATE MACRO gender_(g) AS CASE upper(trim(g)) WHEN 'K' THEN 'Kobieta' WHEN 'M' THEN 'Mężczyzna' ELSE NULLIF(trim(g), '') END;
CREATE MACRO yes_(x) AS coalesce(upper(trim(x)) IN ('TAK', 'T'), false);
-- Canonical committee name: no " - ZPOW-..." registration signature (2019), single spaces, upper
-- case (2015 files use mixed case, later years upper case).
CREATE MACRO strip_signature(n) AS upper(trim(regexp_replace(regexp_replace(n, '\s+-\s+ZPOW-.*$', ''), '\s+', ' ', 'g')));
"""


class BuildError(Exception):
    """The source files of a year do not have the expected shape or fail a consistency check."""


def python_name_key(name: str) -> str:
    """Python twin of the ``name_key`` DuckDB macro."""
    return " ".join(sorted(re.sub(r"\s+", " ", name.strip()).upper().split(" ")))


def quote_ident(name: str) -> str:
    """Double-quoted DuckDB identifier."""
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    """Single-quoted DuckDB string literal."""
    return "'" + value.replace("'", "''") + "'"


def sql_list(paths: list[str]) -> str:
    """DuckDB list literal of file paths (DDL statements cannot take bound parameters)."""
    return "[" + ", ".join(sql_literal(p) for p in paths) + "]"


class YearSources:
    """Finds staged Parquet files of one year and loads them into DuckDB as ``src_*`` tables.

    Several staging roots may be given in priority order (e.g. the PKW bundle before the KBW
    mirror); each dataset is read from the first root that has it, never mixed across roots.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection, parquet_roots: list[Path], year: int) -> None:
        self.con = con
        self.year = year
        self.year_dirs = [root / f"year={year}" for root in parquet_roots]
        self.year_dir = self.year_dirs[-1]

    def files(self, pattern: str) -> list[str]:
        """Parquet files matching a glob relative to the year directory of the first root that has any."""
        for year_dir in self.year_dirs:
            found = sorted(str(p) for p in year_dir.glob(pattern))
            if found:
                return found
        return []

    def has(self, pattern: str) -> bool:
        """Whether any staged file matches the pattern."""
        return bool(self.files(pattern))

    def load(self, table: str, pattern: str) -> str:
        """Create ``src_<table>`` from all files matching ``pattern`` (same columns); return its name."""
        files = self.files(pattern)
        if not files:
            raise BuildError(f"year {self.year}: no staged Parquet matches {self.year_dir}/{pattern}")
        name = f"src_{table}"
        self.con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM read_parquet({sql_list(files)})")
        return name


def columns_of(con: duckdb.DuckDBPyConnection, relation: str) -> list[str]:
    """Column names of a DuckDB table or of a ``read_parquet(...)`` expression."""
    return [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()]


def normalize_header(name: str) -> str:
    """Header comparison key: no-break spaces and runs of whitespace become one space, lower case."""
    return re.sub(r"\s+", " ", name.replace("\u00a0", " ")).strip().lower()


def column(con: duckdb.DuckDBPyConnection, table: str, *aliases: str, required: bool = True) -> str | None:
    """Quoted name of the first alias present in ``table`` (headers differ between years).

    Raises BuildError listing the available columns when none matches and ``required`` is set.
    """
    available = columns_of(con, table)
    normalized = {normalize_header(c): c for c in available}
    for alias in aliases:
        if alias in available:
            return quote_ident(alias)
        if normalize_header(alias) in normalized:
            return quote_ident(normalized[normalize_header(alias)])
    if required:
        raise BuildError(f"{table}: none of {aliases} found; columns are {available}")
    return None


def pct_expr(con: duckdb.DuckDBPyConnection, table: str, col: str) -> str:
    """SQL expression for a percentage column as DECIMAL(6,2) in 0-100.

    2019 files publish fractions (0.2481), other years percents ('20,28'); the scale is detected
    from the column's maximum.
    """
    max_value = con.execute(f"SELECT max(num_({col})) FROM {table}").fetchone()[0] or 0
    factor = 100 if max_value <= 1.0 else 1
    return f"CAST(round(num_({col}) * {factor}, 2) AS DECIMAL(6, 2))"


def open_duckdb() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB connection with the shared parsing macros."""
    con = duckdb.connect(database=":memory:")
    # Optional cap (e.g. "3GB") for small machines and CI; DuckDB spills to its temp directory.
    if limit := os.getenv("MART_DUCKDB_MEMORY_LIMIT"):
        con.execute(f"SET memory_limit = {sql_literal(limit)}")
    con.execute(MACROS)
    cases = " ".join(f"WHEN {sql_literal(code)} THEN {sql_literal(name)}" for code, name in VOIVODESHIPS.items())
    # Voivodeship name from a gmina TERYT code (NULL abroad).
    con.execute(f"CREATE MACRO voivodeship(t) AS CASE left(t, 2) {cases} END")
    return con


# Rules for one election year, checked before COMMITTEE_ALIASES: committees whose name equals a later
# committee's name but which are a different formation.
YEAR_COMMITTEE_ALIASES: dict[int, list[tuple[str, list[str]]]] = {
    # Local elections 2010: not SLD (which ran as its own committee) and not the 2024 Lewica committee
    # of the same name.
    2010: [("KOALICYJNY KOMITET WYBORCZY LEWICA", ["KKW Lewica"])],
    # Local elections 2002: the joint PO-PiS sejmik committee (not PiS alone, which "PRAWO I
    # SPRAWIEDLIWOŚĆ" would give it).
    2002: [("PLATFORMA OBYWATELSKA - PRAWO I SPRAWIEDLIWOŚĆ", ["POPiS", "PO", "PiS"])],
    # Local elections 1998: AWS itself (the generic rule leads with AWSP, its 2001 successor), SLD and
    # the PSL-UP-KPEiR alliance named in the genitive or without a party name the rules below match,
    # and two nationwide sejmik committees without a rule.
    1998: [
        ("KOMITET WYBORCZY AKCJA WYBORCZA SOLIDARNOŚĆ", ["AWS", "Akcja Wyborcza Solidarność"]),
        ("KOMITET WYBORCZY SOJUSZU LEWICY DEMOKRATYCZNEJ", ["Lewica", "SLD"]),
        ("PRZYMIERZE SPOŁECZNE: PSL-UP-KPEIR", ["PS", "Przymierze Społeczne", "PSL"]),
        ("RUCH PATRIOTYCZNY OJCZYZNA", ["RPO", "Ruch Patriotyczny Ojczyzna"]),
        # No "RP" alias: in questions it means the country (w RP), not this committee.
        ('STOWARZYSZENIA "RODZINA POLSKA"', ["Rodzina Polska"]),
    ],
    # Sejm 1991: committees of the parties of the 1990s, and the employers' confederation, which the generic
    # "KONFEDERACJA" rule would label Konfederacja. No bare "Solidarność" alias: AWS shares the word, and
    # the "Solidarność 80" lists are other committees.
    1991: [
        ("POLSKIE STRONNICTWO LUDOWE SOJUSZ PROGRAMOWY", ["PSL", "PSL-SP"]),
        ("POROZUMIENIE OBYWATELSKIE CENTRUM", ["POC", "PC", "Porozumienie Centrum"]),
        ("WYBORCZA AKCJA KATOLICKA", ["WAK", "Wyborcza Akcja Katolicka"]),
        ("KONGRES LIBERALNO-DEMOKRATYCZNY", ["KLD", "Kongres Liberalno-Demokratyczny"]),
        ("UNIA DEMOKRATYCZNA", ["UD", "Unia Demokratyczna"]),
        ("POROZUMIENIE LUDOWE", ["PL", "Porozumienie Ludowe"]),
        ('"SOLIDARNOŚĆ" 80', []),
        ('NSZZ "SOLIDARNOŚĆ"', ["NSZZ Solidarność"]),
        ("POLSKA PARTIA PRZYJACIÓŁ PIWA", ["PPPP", "Polska Partia Przyjaciół Piwa"]),
        ("CHRZEŚCIJAŃSKA DEMOKRACJA", ["ChD", "Chrześcijańska Demokracja"]),
        ("PARTIA CHRZEŚCIJAŃSKICH DEMOKRATÓW", ["PChD", "Partia Chrześcijańskich Demokratów"]),
        ("SOLIDARNOŚĆ PRACY", ["Solidarność Pracy"]),
        ("STRONNICTWO DEMOKRATYCZNE", ["SD", "Stronnictwo Demokratyczne"]),
        ("KONFEDERACJA PRACODAWCÓW", []),
        ('"POJEDNANIE I PRZYSZŁOŚĆ"', ["MN", "Mniejszość Niemiecka"]),
    ],
    # Sejm 1993: PSL-PL is not PSL and PSL is not yet Koalicja Polska (the generic rule would say so);
    # German minority committees not named "Mniejszość Niemiecka".
    1993: [
        ("POLSKIE STRONNICTWO LUDOWE - POROZUMIENIE LUDOWE", ["PSL-PL", "PSL-Porozumienie Ludowe"]),
        ("POLSKIE STRONNICTWO LUDOWE", ["PSL"]),
        ("POLSKA PARTIA PRZYJACIÓŁ PIWA", ["PPPP", "Polska Partia Przyjaciół Piwa"]),
        ("POROZUMIENIE CENTRUM", ["PC", "Porozumienie Centrum"]),
        ('KATOLICKI KOMITET WYBORCZY "OJCZYZNA"', ["KKW Ojczyzna", "Ojczyzna"]),
        ("KONGRES LIBERALNO-DEMOKRATYCZNY", ["KLD", "Kongres Liberalno-Demokratyczny"]),
        ('KOMISJA KRAJOWA NSZZ "SOLIDARNOŚĆ"', ["NSZZ Solidarność"]),
        ("UNIA DEMOKRATYCZNA", ["UD", "Unia Demokratyczna"]),
        ("BEZPARTYJNY BLOK WSPIERANIA REFORM", ["BBWR", "Bezpartyjny Blok Wspierania Reform"]),
        ("UNIA PRACY", ["UP", "Unia Pracy"]),
        ("SAMOOBRONA - LEPPERA", ["Samoobrona"]),
        ("SPOŁECZNO-KULTURALNE NIEMCÓW", ["MN", "Mniejszość Niemiecka"]),
        ("NIEMIECKA WSPÓLNOTA POJEDNANIE I PRZYSZŁOŚĆ", ["MN", "Mniejszość Niemiecka"]),
    ],
    # Sejm 1997: committees named after their proposing party organ, in the genitive ("Zarząd Główny Ruchu
    # Odbudowy Polski"); AWS itself (the generic rule leads with AWSP, its 2001 successor).
    1997: [
        ("KOMITET WYBORCZY AKCJA WYBORCZA SOLIDARNOŚĆ", ["AWS", "Akcja Wyborcza Solidarność"]),
        ("KOMITET WYBORCZY SOJUSZU LEWICY DEMOKRATYCZNEJ", ["Lewica", "SLD"]),
        ("RUCHU ODBUDOWY POLSKI", ["ROP", "Ruch Odbudowy Polski"]),
        ("KOMITET WYKONAWCZY UNII PRACY", ["UP", "Unia Pracy"]),
        ("UNIA PRAWICY RZECZYPOSPOLITEJ", ["UPR", "Unia Prawicy Rzeczypospolitej"]),
        ("RADA KRAJOWA PRZYMIERZE SAMOOBRONA", ["Samoobrona"]),
        ("BLOK DLA POLSKI", ["Blok dla Polski"]),
        ("KULTURALNEGO NIEMCÓW", ["MN", "Mniejszość Niemiecka"]),
        ("MNIEJSZOŚCI NIEMIECKIEJ", ["MN", "Mniejszość Niemiecka"]),
        ('"POJEDNANIE I PRZYSZŁOŚĆ"', ["MN", "Mniejszość Niemiecka"]),
    ],
}


def create_committee_aliases(con: duckdb.DuckDBPyConnection, year: int) -> None:
    """Create ``committee_alias_rules(priority, needle, aliases_pg)`` for ``year``.

    Rules of YEAR_COMMITTEE_ALIASES for that year come first, then COMMITTEE_ALIASES.
    ``aliases_pg`` is already a PostgreSQL array literal so it can be COPYed into TEXT[].
    """
    rows = []
    for priority, (needle, aliases) in enumerate(YEAR_COMMITTEE_ALIASES.get(year, []) + COMMITTEE_ALIASES):
        pg_array = "{" + ",".join('"' + a.replace('"', '\\"') + '"' for a in aliases) + "}"
        rows.append(f"({priority}, {sql_literal(needle)}, {sql_literal(pg_array)})")
    con.execute(
        "CREATE OR REPLACE TABLE committee_alias_rules AS SELECT * FROM (VALUES "
        + ", ".join(rows)
        + ") t(priority, needle, aliases_pg)"
    )


# --- Mapping wide candidate columns to candidates -------------------------------------------


@dataclass(frozen=True)
class ColumnRef:
    """What a wide result column tells about its candidate (any field may be unknown)."""

    name_key: str | None = None
    list_no: int | None = None
    position: int | None = None
    label: str | None = None


@dataclass(frozen=True)
class Candidate:
    """Candidate fields needed to resolve result columns."""

    candidate_id: int
    district_no: int
    list_no: int | None
    position: int | None
    name_key: str
    group: int | None  # committee_id (Sejm) or None (Senate)


class CandidateIndex:
    """Resolves ``ColumnRef``s of one year to candidate ids.

    Resolution order: (district, list_no, position) when the header carries them; a name unique
    within (district, list_no) when only the list is known; otherwise a name unique within the district; otherwise name + committee learned from the short committee label
    in the header (labels are learned from candidates whose name is unique in their district).
    """

    def __init__(self, candidates: list[Candidate]) -> None:
        self.by_slot = {(c.district_no, c.list_no, c.position): c for c in candidates}
        self.by_name: dict[tuple[int, str], list[Candidate]] = defaultdict(list)
        self.by_list_name: dict[tuple[int, int | None, str], list[Candidate]] = defaultdict(list)
        for c in candidates:
            self.by_name[(c.district_no, c.name_key)].append(c)
            self.by_list_name[(c.district_no, c.list_no, c.name_key)].append(c)
        self.list_names: dict[int, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
        for c in candidates:
            if c.list_no is not None:
                self.list_names[c.district_no][c.list_no].add(c.name_key)
        self.label_groups: dict[str, int] = {}

    def learn_labels(self, refs: list[tuple[int, ColumnRef]]) -> None:
        """Learn which committee each short header label means (most common among unique names)."""
        votes: dict[str, Counter] = defaultdict(Counter)
        for district, ref in refs:
            matches = self.by_name.get((district, ref.name_key or ""), [])
            if ref.label and len(matches) == 1 and matches[0].group is not None:
                votes[ref.label][matches[0].group] += 1
        self.label_groups = {label: counter.most_common(1)[0][0] for label, counter in votes.items()}

    def resolve(self, district: int, ref: ColumnRef) -> int | None:
        """Candidate id for a column, or None when it cannot be resolved unambiguously."""
        if ref.list_no is not None and ref.position is not None:
            hit = self.by_slot.get((district, ref.list_no, ref.position))
            if hit and (ref.name_key is None or hit.name_key == ref.name_key):
                return hit.candidate_id
            return None
        if ref.list_no is not None:
            in_list = self.by_list_name.get((district, ref.list_no, ref.name_key or ""), [])
            return in_list[0].candidate_id if len(in_list) == 1 else None
        matches = self.by_name.get((district, ref.name_key or ""), [])
        if len(matches) == 1:
            return matches[0].candidate_id
        if len(matches) > 1 and ref.label in self.label_groups:
            group = self.label_groups[ref.label]
            narrowed = [c for c in matches if c.group == group]
            if len(narrowed) == 1:
                return narrowed[0].candidate_id
        return None


def candidate_index(con: duckdb.DuckDBPyConnection, table: str, group_col: str | None) -> CandidateIndex:
    """Build a CandidateIndex from a DuckDB candidates table (sejm_candidates / senat_candidates)."""
    group = group_col or "NULL"
    slot = "list_no, list_position" if group_col else "NULL, NULL"
    rows = con.execute(
        f"SELECT candidate_id, district_no, {slot}, name_key(full_name), {group} FROM {table}"
    ).fetchall()
    return CandidateIndex([Candidate(*row) for row in rows])


def parse_name_dash_label(columns: list[str]) -> dict[str, ColumnRef]:
    """2023 headers: ``'KLESZCZ Paweł Piotr - KW BEZPARTYJNI SAMORZĄDOWCY'`` (name, short committee)."""
    refs = {}
    for col in columns:
        if " - " in col:
            name, label = col.split(" - ", 1)
            refs[col] = ColumnRef(name_key=python_name_key(name), label=label.strip())
    return refs


def parse_list_position(columns: list[str]) -> dict[str, ColumnRef]:
    """2019 Sejm headers: ``'Lista nr 1 - KOMITET ...'`` then ``'Stanisław Adam ŻUK - nr na liście 1'``."""
    refs, current_list = {}, None
    for col in columns:
        list_header = re.match(r"^Lista nr (\d+) - ", col)
        if list_header:
            current_list = int(list_header.group(1))
            continue
        candidate = re.match(r"^(.*) - nr na liście (\d+)$", col)
        if candidate and current_list is not None:
            refs[col] = ColumnRef(
                name_key=python_name_key(candidate.group(1)), list_no=current_list, position=int(candidate.group(2))
            )
    return refs


# Patronymic that tells namesakes apart in 2011 registers ('Andrzej Stefan syn Józefa', 'Mieczysław s.
# Stanisława'); result headers carry only the bare name.
PATRONYMIC = r" (?:s\.|c\.|syn|córka) \S+"


def parse_list_blocks(columns: list[str]) -> dict[str, ColumnRef]:
    """2011 Sejm and 2014 EP candidate files: per list a block of
    ``'Lista nr 4 — KW ... - Liczba głosów ważnych oddanych na listę'`` (list total), its candidates
    (``'SURNAME Given Names'``) and ``'Razem'``. DuckDB suffixes repeated headers (``'Razem_1'``, a
    namesake on another list ``'SZTORC Andrzej Stefan_1'``); the suffix is not part of the name.
    """
    refs, current_list = {}, None
    for col in columns:
        if list_header := re.match(r"^Lista nr (\d+) — ", col):
            current_list = int(list_header.group(1))
        elif re.match(r"^Razem(_\d+)?$", col):
            current_list = None
        elif current_list is not None:
            refs[col] = ColumnRef(name_key=python_name_key(re.sub(r"_\d+$", "", col)), list_no=current_list)
    return refs


def parse_numbered_names(columns: list[str], list_no: int | None = None) -> dict[str, ColumnRef]:
    """2007 Sejm and Senate candidate files: ``'3. Kowalski Jan Maria (T)'`` (ballot position, name,
    elected T/N; 2001 Senate: W/N). With ``list_no`` (Sejm: from the file name) the position identifies the slot."""
    refs = {}
    for col in columns:
        if m := re.match(r"^(\d+)\. (.+) \([TNW]\)$", col):
            position = int(m[1]) if list_no is not None else None
            refs[col] = ColumnRef(name_key=python_name_key(m[2]), list_no=list_no, position=position)
    return refs


def parse_plain_names(columns: list[str], known_keys: set[str]) -> dict[str, ColumnRef]:
    """2019 Senate headers are bare names (``'Władysław KOZAKIEWICZ'``); keep those naming a candidate."""
    return {col: ColumnRef(name_key=key) for col in columns if (key := python_name_key(col)) in known_keys}


def parse_blocks_2015(
    columns: list[str], list_names: dict[int, set[str]]
) -> tuple[dict[str, ColumnRef], dict[str, int]]:
    """2015 Sejm district files: ``'<list label>'``, its candidates, ``'Razem <list label>'``.

    A block starts at a column whose ``'Razem ' + name`` column also exists (labels vary: ``'KW ...'``,
    ``'KKW ...'``, ``'Komitet Wyborczy PSL'``). Each block is assigned to the district list sharing
    most candidate names with it (``list_names``: list_no -> name keys); column order is not trusted
    because struck-off candidates leave gaps. Returns (candidate refs, list-total column -> list_no).
    """
    present = set(columns)
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for col in columns:
        if current is not None and col == f"Razem {current[0]}":
            blocks.append(current)
            current = None
        elif f"Razem {col}" in present:
            current = (col, [])
        elif current is not None:
            current[1].append(col)

    refs: dict[str, ColumnRef] = {}
    totals: dict[str, int] = {}
    for label, members in blocks:
        keys = {python_name_key(c) for c in members}
        list_no, overlap = max(((n, len(keys & names)) for n, names in list_names.items()), key=lambda t: t[1], default=(None, 0))
        if list_no is None or overlap == 0:
            continue
        totals[f"Razem {label}"] = list_no
        for col in members:
            refs[col] = ColumnRef(name_key=python_name_key(col), list_no=list_no)
    return refs, totals


def build_wide_votes(
    con: duckdb.DuckDBPyConnection,
    *,
    year: int,
    target: str,
    key_name: str,
    files: list[tuple[int, str, dict[str, int]]],
    teryt_aliases: tuple[str, ...],
    number_aliases: tuple[str, ...],
) -> None:
    """Unpivot wide per-precinct result files into ``target(year, precinct_id, <key_name>, votes)``.

    ``files`` holds ``(district_no, parquet_path, {header: key})``; only mapped headers are read and
    several headers may share a key (their votes are summed). The mapping is kept in ``<target>_columns`` for consistency checks. Precincts are matched through
    ``precincts.pkey``; rows of unknown precincts stay in ``<target>_raw`` and are caught by checks.
    """
    con.execute(f"CREATE OR REPLACE TABLE {target}_columns (district_no INT, header VARCHAR, key INT)")
    con.execute(f"CREATE OR REPLACE TABLE {target}_raw (district_no INT, pkey VARCHAR, header VARCHAR, votes VARCHAR)")
    for district, path, mapping in files:
        if not mapping:
            continue
        con.executemany(
            f"INSERT INTO {target}_columns VALUES (?, ?, ?)", [[district, h, k] for h, k in mapping.items()]
        )
        source = f"read_parquet({sql_literal(path)})"
        teryt = column(con, source, *teryt_aliases)
        number = column(con, source, *number_aliases)
        value_cols = ", ".join(quote_ident(h) for h in mapping)
        con.execute(
            f"""
            INSERT INTO {target}_raw
            SELECT {district}, pkey(t, n), header, votes
            FROM (UNPIVOT (SELECT {teryt} AS t, {number} AS n, {value_cols} FROM {source})
                  ON {value_cols} INTO NAME header VALUE votes)
            """
        )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE {target} AS
        SELECT {year} AS year, p.precinct_id, m.key AS {key_name}, CAST(sum(coalesce(int_(r.votes), 0)) AS INT) AS votes
        FROM {target}_raw r
        JOIN {target}_columns m ON m.district_no = r.district_no AND m.header = r.header
        JOIN precincts p ON p.pkey = r.pkey
        GROUP BY ALL
        """
    )


def map_candidate_columns(
    con: duckdb.DuckDBPyConnection,
    *,
    files_by_district: dict[int, str],
    index: CandidateIndex,
    parse,
    unmapped_table: str,
) -> list[tuple[int, str, dict[str, int]]]:
    """Resolve the candidate columns of per-district files with ``parse(columns, district_no)``.

    Returns ``build_wide_votes`` input. Columns that look like candidates but cannot be resolved go
    to ``unmapped_table(district_no, header, list_no, position)`` so a check can fail the build.
    """
    parsed = {
        district: parse(columns_of(con, f"read_parquet({sql_literal(path)})"), district)
        for district, path in files_by_district.items()
    }
    index.learn_labels([(d, ref) for d, refs in parsed.items() for ref in refs.values()])
    con.execute(f"CREATE OR REPLACE TABLE {unmapped_table} (district_no INT, header VARCHAR, list_no INT, position INT)")
    files = []
    for district, path in files_by_district.items():
        mapping = {}
        for header, ref in parsed[district].items():
            candidate_id = index.resolve(district, ref)
            if candidate_id is None:
                con.execute(
                    f"INSERT INTO {unmapped_table} VALUES (?, ?, ?, ?)", [district, header, ref.list_no, ref.position]
                )
            else:
                mapping[header] = candidate_id
        files.append((district, path, mapping))
    return files


def district_files(paths: list[str], pattern: str) -> dict[int, str]:
    """Map district number -> file using a regex with one group, e.g. ``r'okreg_(\\d+)_'``."""
    out = {}
    for path in paths:
        match = re.search(pattern, Path(path).name)
        if not match:
            raise BuildError(f"cannot read the district number from {path}")
        out[int(match.group(1))] = path
    return out


# --- Consistency checks and loading ----------------------------------------------------------


def run_checks(con: duckdb.DuckDBPyConnection, year: int, checks: dict[str, str]) -> None:
    """Run ``SELECT count(*)`` checks; every one must return 0 or the build fails."""
    failures = []
    for label, query in checks.items():
        bad = con.execute(query).fetchone()[0]
        log.info("year %s check %-70s %s", year, label, bad)
        if bad:
            failures.append(f"{label} ({bad})")
    if failures:
        raise BuildError(f"year {year}: consistency checks failed: {'; '.join(failures)}")


def load_tables(pg: psycopg.Connection, con: duckdb.DuckDBPyConnection, year: int, tables: tuple[str, ...]) -> None:
    """Replace one year of the given mart tables with the DuckDB tables of the same name.

    Runs in the caller's transaction. Tables the build did not produce (e.g. Senate tables when no
    Senate files are staged) are only cleared for that year. Other election families are untouched.
    """
    produced = {row[0] for row in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    with pg.cursor() as cur, tempfile.TemporaryDirectory(prefix="mart_") as tmp:
        for table in tables:
            cur.execute(f"DELETE FROM mart.{table} WHERE year = %s", (year,))
            if table not in produced:
                continue
            columns = [
                row[0]
                for row in cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'mart' AND table_name = %s AND column_name <> 'person_id'
                    ORDER BY ordinal_position
                    """,
                    (table,),
                ).fetchall()
            ]
            col_list = ", ".join(columns)
            csv_path = Path(tmp) / f"{table}.csv"
            con.execute(f"COPY (SELECT {col_list} FROM {table}) TO {sql_literal(str(csv_path))} (FORMAT CSV, HEADER false)")
            with cur.copy(f"COPY mart.{table} ({col_list}) FROM STDIN (FORMAT csv)") as copy, csv_path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    copy.write(chunk)
            log.info("year %s loaded mart.%s: %s rows", year, table, cur.rowcount)


@dataclass(frozen=True)
class Election:
    """One row of ``mart.elections``: what was loaded, for which round, and when it took place."""

    election: str
    year: int
    round: int
    election_date: str
    description: str


def record_elections(pg: psycopg.Connection, families: tuple[str, ...], year: int, elections: list[Election]) -> None:
    """Replace the ``mart.elections`` rows of the given election kinds and year (caller's transaction)."""
    pg.execute("DELETE FROM mart.elections WHERE year = %s AND election = ANY(%s)", (year, list(families)))
    for e in elections:
        pg.execute(
            "INSERT INTO mart.elections (election, year, round, election_date, description) VALUES (%s, %s, %s, %s, %s)",
            (e.election, e.year, e.round, e.election_date, e.description),
        )
