#!/usr/bin/env python3
"""Stage KBW mirror CSV data into Parquet using DuckDB (container-friendly).

Scope (MVP):
- Reads plain CSV files under data/kbw_mirror/dane
- Reads CSV members from ZIP archives
- Writes one parquet per source CSV (stable path derived from source location)

This gives a fast intermediate format for local ETL iterations before Postgres load.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import tempfile
import zipfile
from pathlib import Path

import duckdb


def _year_from_path(path: Path) -> int | None:
    """Year from path."""
    m = re.search(r"(19\d{2}|20\d{2})", str(path))
    return int(m.group(1)) if m else None


def _safe_slug(path_str: str) -> str:
    """Safe slug."""
    return hashlib.sha1(path_str.encode("utf-8")).hexdigest()[:16]


def _sql_path_literal(path: Path) -> str:
    """Single-quoted SQL string for a filesystem path (paths are trusted; apostrophes escaped)."""
    return "'" + str(path).replace("'", "''") + "'"


def _copy_csv_to_parquet(con: duckdb.DuckDBPyConnection, csv_path: Path, parquet_path: Path) -> int:
    """Copy csv to parquet."""
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    # COPY ... TO does not accept prepared parameters in DuckDB; use escaped literals.
    csv_lit = _sql_path_literal(csv_path)
    out_lit = _sql_path_literal(parquet_path)
    sql = f"""
        COPY (
          SELECT * FROM read_csv_auto({csv_lit}, sample_size=-1, all_varchar=true, ignore_errors=true)
        ) TO {out_lit} (FORMAT PARQUET, COMPRESSION ZSTD);
    """
    con.execute(sql)
    rows = con.execute(f"SELECT COUNT(*) FROM read_parquet({out_lit})").fetchone()[0]
    return int(rows or 0)


def stage_to_parquet(root: Path, out_dir: Path, years: set[int] | None = None) -> dict[str, int]:
    """Stage to parquet."""
    con = duckdb.connect(database=":memory:")
    stats = {
        "files_seen": 0,
        "csv_staged": 0,
        "zip_csv_members_staged": 0,
        "rows_staged": 0,
        "skipped_year": 0,
        "errors": 0,
    }

    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".csv", ".zip"))
    for path in paths:
        year = _year_from_path(path)
        if years and year not in years:
            stats["skipped_year"] += 1
            continue

        stats["files_seen"] += 1
        rel = path.relative_to(root).as_posix()
        try:
            if path.suffix.lower() == ".csv":
                dst = out_dir / f"year={year or 'unknown'}" / (Path(rel).with_suffix("").as_posix() + ".parquet")
                rows = _copy_csv_to_parquet(con, path, dst)
                stats["csv_staged"] += 1
                stats["rows_staged"] += rows
                print(f"[stage] CSV rows={rows} {rel}")
                continue

            # ZIP: stage only CSV members.
            with zipfile.ZipFile(path) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".csv") and not m.endswith("/")]
                for member in members:
                    with tempfile.TemporaryDirectory(prefix="kbw_zip_csv_") as td:
                        extracted = Path(td) / Path(member).name
                        extracted.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, extracted.open("wb") as dstf:
                            dstf.write(src.read())
                        member_slug = _safe_slug(f"{rel}::{member}")
                        dst = (
                            out_dir
                            / f"year={year or 'unknown'}"
                            / Path(rel).with_suffix("").as_posix()
                            / f"{Path(member).stem}__{member_slug}.parquet"
                        )
                        rows = _copy_csv_to_parquet(con, extracted, dst)
                        stats["zip_csv_members_staged"] += 1
                        stats["rows_staged"] += rows
                        print(f"[stage] ZIP/CSV rows={rows} {rel}::{member}")
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            print(f"[stage] ERR {type(exc).__name__}: {exc} :: {rel}")

    return stats


def _parse_years(raw: str | None) -> set[int] | None:
    """Parse years."""
    if not raw:
        return None
    out: set[int] = set()
    for token in raw.split(","):
        t = token.strip()
        if t:
            out.add(int(t))
    return out or None


def main() -> None:
    """Main."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/kbw_mirror/dane"))
    parser.add_argument("--out", type=Path, default=Path("data/kbw_stage_parquet"))
    parser.add_argument("--years", type=str, default=None, help="Comma-separated years, e.g. 1997,2023")
    args = parser.parse_args()

    years = _parse_years(args.years)
    stats = stage_to_parquet(root=args.root, out_dir=args.out, years=years)
    print(stats)


if __name__ == "__main__":
    main()
