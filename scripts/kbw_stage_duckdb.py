#!/usr/bin/env python3
"""Stage KBW mirror tables into Parquet (one Parquet file per source table).

Inputs under ``--root`` (default ``data/kbw_mirror/dane``):
- ``*.csv``                     plain CSV files
- ``*.xls`` / ``*.xlsx``        first worksheet of Excel files (used by older elections, e.g. 2015)
- ``*.zip``                     CSV / XLS / XLSX members of ZIP archives

Every value is written as a string; typing happens later (``api/services/mart``).

Staging is strict on purpose. Earlier versions let DuckDB guess the delimiter and passed
``ignore_errors=true``, which silently dropped rows (``okregi_sejm`` kept 8 of 41 districts). Now:
- the delimiter is detected from the header line and passed explicitly,
- non-UTF-8 files are transcoded from CP1250 before DuckDB reads them,
- malformed rows fail the file instead of being skipped,
- the number of staged rows must equal the number of records in the source file.
A file that fails any of these checks is reported as an error and no Parquet is left behind.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.kbw_import import read_xlsx_sheets  # noqa: E402

try:
    import xlrd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional at runtime
    xlrd = None

TABLE_SUFFIXES = (".csv", ".xls", ".xlsx")
# Boundary descriptions ("Opis granic") can be very long; the csv module's default 128 KiB cap is too low.
csv.field_size_limit(sys.maxsize)
CANDIDATE_DELIMITERS = (";", ",", "\t")


class StagingError(Exception):
    """A source table could not be staged without losing or corrupting rows."""


def year_from_path(path: Path) -> int | None:
    """Return the first 4-digit year (1900-2099) found in the path, if any."""
    match = re.search(r"(19\d{2}|20\d{2})", str(path))
    return int(match.group(1)) if match else None


def _slug(text: str) -> str:
    """Short stable hash used to keep Parquet names of ZIP members unique."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _member_display_name(info: zipfile.ZipInfo) -> str:
    """ZIP member name with Polish letters restored.

    Members without the UTF-8 flag are decoded by ``zipfile`` as CP437, which turns names written
    as UTF-8 or CP852 into mojibake ("nieważne" -> "niewa╛ne"). Used for output file names only.
    """
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    for encoding in ("utf-8", "cp852"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _sql_literal(value: str) -> str:
    """Single-quoted SQL string literal (DuckDB COPY does not accept bound parameters)."""
    return "'" + value.replace("'", "''") + "'"


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode file bytes as UTF-8 (with or without BOM), falling back to CP1250."""
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1250"), "cp1250"


def detect_delimiter(header_line: str) -> str:
    """Pick the delimiter that splits the header line into the most fields (quotes respected)."""
    best, best_fields = ";", 0
    for delimiter in CANDIDATE_DELIMITERS:
        fields = next(csv.reader([header_line], delimiter=delimiter), [])
        if len(fields) > best_fields:
            best, best_fields = delimiter, len(fields)
    return best


def count_csv_records(text: str, delimiter: str, quoting: int = csv.QUOTE_MINIMAL) -> int:
    """Number of non-empty data records (header excluded), honouring quoted newlines."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quoting=quoting)
    next(reader, None)
    return sum(1 for record in reader if any(cell.strip() for cell in record))


def unwrap_excel_text(cell: str) -> str:
    """``'="020101"'`` -> ``'020101'``: Excel's text guard, used by 2010-2014 CSV exports to keep leading zeros."""
    match = re.match(r'^="(.*)"$', cell.strip(), re.S)
    return match.group(1) if match else cell


def fold_group_header(group: list[str], header: list[str]) -> list[str]:
    """Header cells (``="..."`` unwrapped), prefixed with the non-empty cell of the group row above them."""
    if group and len(group) != len(header):
        raise StagingError(f"group-header row has {len(group)} fields, header has {len(header)}")
    out = [unwrap_excel_text(cell) for cell in header]
    for index, label in enumerate(group):
        if label := unwrap_excel_text(label).strip():
            out[index] = f"{label} - {out[index]}"
    return out


def strip_preamble(text: str) -> str:
    """Drop title lines above the CSV header and fold a group-header row into the header.

    2011/2014 per-precinct CSVs start with the election title, the commission name and a blank line
    (lines without any delimiter). Candidate files then have a row naming each list above its first
    column (``'Lista nr 1 — KW ...'``, first cell empty); such a cell is kept as a prefix of the
    column name below it (``'Lista nr 1 — KW ... - Liczba głosów ważnych oddanych na listę'``).
    Header cells written as ``="..."`` are unwrapped. Text without title lines is returned unchanged.
    """
    lines = text.splitlines(keepends=True)
    start = 0
    while start < len(lines) and not any(d in lines[start] for d in CANDIDATE_DELIMITERS):
        start += 1
    if start == 0 or start >= len(lines):
        return text
    delimiter = detect_delimiter(lines[start])
    first = next(csv.reader([lines[start]], delimiter=delimiter))
    group: list[str] = []
    if not first[0].strip() and start + 1 < len(lines):
        group, start = first, start + 1
    header = fold_group_header(group, next(csv.reader([lines[start]], delimiter=delimiter)))
    out = io.StringIO()
    csv.writer(out, delimiter=delimiter, lineterminator="\n").writerow(header)
    return out.getvalue() + "".join(lines[start + 1 :])


def csv_to_parquet(con: duckdb.DuckDBPyConnection, data: bytes, parquet_path: Path) -> int:
    """Stage one CSV (given as bytes) into Parquet; return the staged row count.

    Raises StagingError when DuckDB cannot parse the file strictly or the row count differs from
    the number of CSV records.
    """
    text, _encoding = decode_text(data)
    if not text.strip():
        raise StagingError("empty file")
    text = strip_preamble(text)
    delimiter = detect_delimiter(text.splitlines()[0])
    expected = count_csv_records(text, delimiter)

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kbw_stage_") as tmp:
        # DuckDB reads UTF-8 only, so always hand it a normalized UTF-8 copy.
        utf8_path = Path(tmp) / "source.csv"
        utf8_path.write_text(text, encoding="utf-8")

        def copy(quote: str) -> None:
            # Values written as ="..." (Excel text guard) are unwrapped; everything else is kept as is.
            con.execute(
                f"""
                COPY (
                  SELECT regexp_replace(COLUMNS(*), '^="(.*)"$', '\\1', 's') FROM read_csv({_sql_literal(str(utf8_path))},
                    delim = {_sql_literal(delimiter)}, quote = {_sql_literal(quote)}, escape = {_sql_literal(quote)},
                    header = true, all_varchar = true, sample_size = -1)
                ) TO {_sql_literal(str(parquet_path))} (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )

        try:
            copy('"')
        except duckdb.Error as exc:
            lines = [line for line in text.splitlines() if line.strip()]
            if '"=""' in text:
                # 2011/2014 files wrap text as "=""...""" but leave quotes inside unescaped
                # ('"=""Dom Pomocy Społecznej "Kombatant", ..."""'). Re-encode such fields as standard
                # quoted fields (the field ends at the first '"""' before a delimiter or line end).
                quoting = csv.QUOTE_MINIMAL
                text = re.sub(
                    rf'"=""([^\n]*?)"""(?=[{re.escape(delimiter)}\r\n]|$)',
                    lambda m: '"' + m.group(1).replace('"', '""') + '"',
                    text,
                    flags=re.M,
                )
                utf8_path.write_text(text, encoding="utf-8")
                try:
                    copy('"')
                except duckdb.Error as exc2:
                    raise StagingError(f"CSV parse failed (delimiter {delimiter!r}, re-encoded \"=\"\"...\"\"\" fields): {exc2}") from exc2
            elif all(line.count(delimiter) == lines[0].count(delimiter) for line in lines):
                # 2010 presidential files never quote fields but contain doubled quotes inside them
                # ('Klub ""Sowa""'). Every line has as many fields as the header without quoting, so
                # no quote protects a delimiter or a newline: read them without quoting.
                quoting = csv.QUOTE_NONE
                try:
                    copy("")
                except duckdb.Error as exc2:
                    raise StagingError(f"CSV parse failed (delimiter {delimiter!r}, no quoting): {exc2}") from exc2
            else:
                raise StagingError(f"CSV parse failed (delimiter {delimiter!r}): {exc}") from exc
            expected = count_csv_records(text, delimiter, quoting=quoting)

    staged = con.execute(f"SELECT count(*) FROM read_parquet({_sql_literal(str(parquet_path))})").fetchone()[0]
    if staged != expected:
        parquet_path.unlink(missing_ok=True)
        raise StagingError(f"row count mismatch: source has {expected} records, Parquet has {staged}")
    return int(staged)


def _excel_cell(value: object) -> str:
    """String form of an Excel cell; integral floats lose the '.0' that xlrd adds (1.0 -> '1')."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def read_excel_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
    """Every worksheet of an XLS or XLSX file as (sheet title, rows of strings)."""
    if path.suffix.lower() == ".xlsx":
        return read_xlsx_sheets(path)
    if xlrd is None:
        raise StagingError("xlrd is required to stage .xls files")
    try:
        book = xlrd.open_workbook(path.as_posix(), on_demand=True)
    except xlrd.XLRDError as exc:
        raise StagingError(f"not a readable XLS file: {exc}") from exc
    return [
        (sheet.name, [[_excel_cell(v) for v in sheet.row_values(i)] for i in range(sheet.nrows)])
        for sheet in (book.sheet_by_index(n) for n in range(book.nsheets))
    ]


def unique_headers(raw_headers: list[str]) -> list[str]:
    """Normalize header cells (collapse whitespace/newlines) and suffix duplicates like DuckDB does.

    ``["Typ", "Nr okr.\\nSejm", "Typ"]`` -> ``["Typ", "Nr okr. Sejm", "Typ_1"]``; blanks become ``column<N>``.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for index, raw in enumerate(raw_headers):
        name = re.sub(r"\s+", " ", raw.replace("\ufeff", "")).strip() or f"column{index}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def is_unit_row(row: list[str]) -> bool:
    """Whether a row only names units (``'liczba'``, ``'%'``, ``'% (frekwencja)'``) under the header."""
    cells = [re.sub(r"\s+", " ", cell).strip() for cell in row if cell.strip()]
    return bool(cells) and all(re.fullmatch(r"liczba|%.*", cell) for cell in cells)


def fold_unit_row(header: list[str], units: list[str]) -> list[str]:
    """Header cells with the unit row below them folded in (2005 tables with numbers and percents).

    ``['Głosy ważne', '']`` over ``['liczba', '%']`` -> ``['Głosy ważne - liczba', 'Głosy ważne - %']``:
    the header cell spans its unit columns, so a unit under an empty header cell belongs to the
    nearest labelled column on its left. Columns without a unit keep their header.
    """
    if len(units) > len(header):
        raise StagingError(f"unit row has {len(units)} fields, header has {len(header)}")
    out, label = [], ""
    for index, cell in enumerate(header):
        unit = re.sub(r"\s+", " ", units[index]).strip() if index < len(units) else ""
        label = cell.strip() or label
        if unit and not label:
            raise StagingError(f"unit {unit!r} in column {index} has no header cell on its left")
        out.append(f"{label} - {unit}" if unit else cell)
    return out


def _write_sheet(table: list[list[str]], parquet_path: Path) -> int:
    """Write one worksheet (header in row 1) to Parquet; return the number of data rows.

    Title rows above the header (empty, or text in the first column only; 2011 per-precinct
    workbooks) are skipped, a group-header row is folded into the header as in ``strip_preamble``
    (also one naming a single list: first cell empty, one label further right), and a unit row
    below the header (2005: ``'liczba'`` / ``'%'``) is folded into it with ``fold_unit_row``.
    """
    is_title = [not any(cell.strip() for cell in row[1:]) for row in table[:5]]
    start = next((i for i, title in enumerate(is_title) if not title), 0)  # single-column sheets keep row 1
    header = table[start]
    if start > 0 and not header[0].strip() and start + 1 < len(table):
        group, header = table[start], table[start + 1]
        start += 1
        header = fold_group_header(group + [""] * (len(header) - len(group)), header)
    if start + 1 < len(table) and is_unit_row(table[start + 1]):
        header = fold_unit_row(header, table[start + 1])
        start += 1
    headers = unique_headers(header)
    rows = [row for row in table[start + 1 :] if any(cell.strip() for cell in row)]
    columns = {
        name: pa.array([(row[i] if i < len(row) else "") or None for row in rows], type=pa.string())
        for i, name in enumerate(headers)
    }
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), parquet_path, compression="zstd")
    return len(rows)


def excel_to_parquet(path: Path, parquet_path: Path) -> int:
    """Stage an Excel file into Parquet; return the total row count.

    A single non-empty worksheet goes to ``parquet_path``. Workbooks with several non-empty sheets
    (e.g. one sheet per district) get one file per sheet: ``<stem>__sheetNN_<title>.parquet``.
    """
    sheets = [(title, table) for title, table in read_excel_sheets(path) if table]
    if not sheets:
        raise StagingError("empty workbook")
    if len(sheets) == 1:
        return _write_sheet(sheets[0][1], parquet_path)
    total = 0
    for index, (title, table) in enumerate(sheets, start=1):
        target = parquet_path.with_name(f"{parquet_path.stem}__sheet{index:02d}_{_slug(title)[:8]}.parquet")
        total += _write_sheet(table, target)
    return total


def stage_table(con: duckdb.DuckDBPyConnection, name: str, data: bytes, parquet_path: Path) -> int:
    """Stage one source table (CSV or Excel, given by file name and bytes) into Parquet."""
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        return csv_to_parquet(con, data, parquet_path)
    with tempfile.TemporaryDirectory(prefix="kbw_stage_") as tmp:
        local = Path(tmp) / f"source{suffix}"
        local.write_bytes(data)
        return excel_to_parquet(local, parquet_path)


def stage_to_parquet(
    root: Path, out_dir: Path, years: set[int] | None = None, prefixes: list[str] | None = None
) -> dict[str, int]:
    """Stage every table under ``root`` into ``out_dir/year=<YYYY>/...``; return counters.

    ``prefixes`` limits staging to paths relative to ``root`` that start with one of them (e.g.
    ``2014/parlament_eu``); output paths stay relative to ``root``.
    """
    con = duckdb.connect(database=":memory:")
    stats = {"files_seen": 0, "tables_staged": 0, "rows_staged": 0, "skipped": 0, "errors": 0}

    paths = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (*TABLE_SUFFIXES, ".zip")
    )
    for path in paths:
        year = year_from_path(path)
        rel = path.relative_to(root).as_posix()
        if (years and year not in years) or (prefixes and not any(rel.startswith(p) for p in prefixes)):
            stats["skipped"] += 1
            continue
        stats["files_seen"] += 1
        year_dir = out_dir / f"year={year or 'unknown'}"

        if path.suffix.lower() != ".zip":
            members = [(rel, path.name, path.read_bytes, year_dir / f"{Path(rel).with_suffix('').as_posix()}.parquet")]
        else:
            try:
                archive = zipfile.ZipFile(path)
            except zipfile.BadZipFile as exc:
                stats["errors"] += 1
                print(f"[stage] ERROR {rel}: not a ZIP archive ({exc})", flush=True)
                continue
            members = [
                (
                    f"{rel}::{_member_display_name(info)}",
                    info.filename,
                    (lambda m=info.filename, a=archive: a.read(m)),
                    year_dir
                    / Path(rel).with_suffix("").as_posix()
                    / f"{Path(_member_display_name(info)).stem}__{_slug(f'{rel}::{info.filename}')}.parquet",
                )
                for info in archive.infolist()
                if Path(info.filename).suffix.lower() in TABLE_SUFFIXES
                and not info.filename.endswith("/")
                and not info.filename.startswith("__MACOSX/")  # macOS resource forks, not tables
            ]

        for label, name, read_bytes, parquet_path in members:
            try:
                rows = stage_table(con, name, read_bytes(), parquet_path)
            except (StagingError, OSError, zipfile.BadZipFile) as exc:
                stats["errors"] += 1
                print(f"[stage] ERROR {label}: {exc}", flush=True)
                continue
            stats["tables_staged"] += 1
            stats["rows_staged"] += rows
            print(f"[stage] rows={rows} {label}", flush=True)
    return stats


def _parse_years(raw: str | None) -> set[int] | None:
    """Parse a comma-separated year list such as ``2015,2019``."""
    if not raw:
        return None
    return {int(token) for token in raw.split(",") if token.strip()} or None


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("data/kbw_mirror/dane"))
    parser.add_argument("--out", type=Path, default=Path("data/kbw_stage_parquet"))
    parser.add_argument("--years", type=str, default=None, help="Comma-separated years, e.g. 2015,2023")
    parser.add_argument(
        "--prefix", action="append", default=None, help="Only paths under --root starting with this (repeatable)"
    )
    args = parser.parse_args()

    stats = stage_to_parquet(root=args.root, out_dir=args.out, years=_parse_years(args.years), prefixes=args.prefix)
    print(f"[stage] summary {stats}", flush=True)


if __name__ == "__main__":
    main()
