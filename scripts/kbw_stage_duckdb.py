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

from api.services.kbw_import import read_xlsx_table  # noqa: E402

try:
    import xlrd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional at runtime
    xlrd = None

TABLE_SUFFIXES = (".csv", ".xls", ".xlsx")
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


def count_csv_records(text: str, delimiter: str) -> int:
    """Number of non-empty data records (header excluded), honouring quoted newlines."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    next(reader, None)
    return sum(1 for record in reader if any(cell.strip() for cell in record))


def csv_to_parquet(con: duckdb.DuckDBPyConnection, data: bytes, parquet_path: Path) -> int:
    """Stage one CSV (given as bytes) into Parquet; return the staged row count.

    Raises StagingError when DuckDB cannot parse the file strictly or the row count differs from
    the number of CSV records.
    """
    text, _encoding = decode_text(data)
    if not text.strip():
        raise StagingError("empty file")
    delimiter = detect_delimiter(text.splitlines()[0])
    expected = count_csv_records(text, delimiter)

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kbw_stage_") as tmp:
        # DuckDB reads UTF-8 only, so always hand it a normalized UTF-8 copy.
        utf8_path = Path(tmp) / "source.csv"
        utf8_path.write_text(text, encoding="utf-8")
        try:
            con.execute(
                f"""
                COPY (
                  SELECT * FROM read_csv({_sql_literal(str(utf8_path))},
                    delim = {_sql_literal(delimiter)}, quote = '"', escape = '"',
                    header = true, all_varchar = true, sample_size = -1)
                ) TO {_sql_literal(str(parquet_path))} (FORMAT PARQUET, COMPRESSION ZSTD)
                """
            )
        except duckdb.Error as exc:
            raise StagingError(f"CSV parse failed (delimiter {delimiter!r}): {exc}") from exc

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


def read_excel_table(path: Path) -> list[list[str]]:
    """First worksheet of an XLS or XLSX file as rows of strings."""
    if path.suffix.lower() == ".xlsx":
        return read_xlsx_table(path)
    if xlrd is None:
        raise StagingError("xlrd is required to stage .xls files")
    try:
        book = xlrd.open_workbook(path.as_posix(), on_demand=True)
    except xlrd.XLRDError as exc:
        raise StagingError(f"not a readable XLS file: {exc}") from exc
    if book.nsheets == 0:
        return []
    sheet = book.sheet_by_index(0)
    return [[_excel_cell(v) for v in sheet.row_values(i)] for i in range(sheet.nrows)]


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


def excel_to_parquet(path: Path, parquet_path: Path) -> int:
    """Stage the first worksheet of an Excel file (header in row 1) into Parquet; return row count."""
    table = read_excel_table(path)
    if not table:
        raise StagingError("empty worksheet")
    headers = unique_headers(table[0])
    rows = [row for row in table[1:] if any(cell.strip() for cell in row)]
    columns = {
        name: pa.array([(row[i] if i < len(row) else "") or None for row in rows], type=pa.string())
        for i, name in enumerate(headers)
    }
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns), parquet_path, compression="zstd")
    return len(rows)


def stage_table(con: duckdb.DuckDBPyConnection, name: str, data: bytes, parquet_path: Path) -> int:
    """Stage one source table (CSV or Excel, given by file name and bytes) into Parquet."""
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        return csv_to_parquet(con, data, parquet_path)
    with tempfile.TemporaryDirectory(prefix="kbw_stage_") as tmp:
        local = Path(tmp) / f"source{suffix}"
        local.write_bytes(data)
        return excel_to_parquet(local, parquet_path)


def stage_to_parquet(root: Path, out_dir: Path, years: set[int] | None = None) -> dict[str, int]:
    """Stage every table under ``root`` into ``out_dir/year=<YYYY>/...``; return counters."""
    con = duckdb.connect(database=":memory:")
    stats = {"files_seen": 0, "tables_staged": 0, "rows_staged": 0, "skipped_year": 0, "errors": 0}

    paths = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (*TABLE_SUFFIXES, ".zip")
    )
    for path in paths:
        year = year_from_path(path)
        if years and year not in years:
            stats["skipped_year"] += 1
            continue
        stats["files_seen"] += 1
        rel = path.relative_to(root).as_posix()
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
                    f"{rel}::{member}",
                    member,
                    (lambda m=member, a=archive: a.read(m)),
                    year_dir / Path(rel).with_suffix("").as_posix() / f"{Path(member).stem}__{_slug(f'{rel}::{member}')}.parquet",
                )
                for member in archive.namelist()
                if Path(member).suffix.lower() in TABLE_SUFFIXES and not member.endswith("/")
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
    args = parser.parse_args()

    stats = stage_to_parquet(root=args.root, out_dir=args.out, years=_parse_years(args.years))
    print(f"[stage] summary {stats}", flush=True)


if __name__ == "__main__":
    main()
