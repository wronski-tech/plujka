#!/usr/bin/env python3
"""Build the relational election mart (PostgreSQL schema ``mart``) from staged KBW Parquet.

For every requested year: Sejm tables (2015 Excel layout or 2019/2023 CSV layout) and, when the
files exist, Senate tables are built in DuckDB, checked against official totals and loaded into
PostgreSQL (one transaction per year). Afterwards persons, materialized views and the entity lexicon
are rebuilt from all loaded years, and the read-only role used for LLM-generated SQL is refreshed.

A failed consistency check aborts the year before anything is written to PostgreSQL.

Examples:
  python3 scripts/build_mart.py                       # every supported year found in Parquet
  python3 scripts/build_mart.py --years 2023 --parquet-root /app/data/kbw_stage_parquet \
      --pkw-parquet-root /app/data/pkw_stage_parquet
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.config import DATABASE_URL, MART_STATEMENT_TIMEOUT_MS  # noqa: E402
from api.services.mart import build_senat, derived, schema  # noqa: E402
from api.services.mart.build_common import BuildError, YearSources, load_year, open_duckdb  # noqa: E402
from api.services.mart.build_sejm import available_layout, build_sejm  # noqa: E402

SUPPORTED_YEARS = (2015, 2019, 2023)
log = logging.getLogger("mart")


def wait_for_database(seconds: int) -> None:
    """Retry connecting until PostgreSQL accepts connections (useful right after `compose up`)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            psycopg.connect(DATABASE_URL).close()
            return
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise
            log.info("database not ready (%s); retrying in 5s", exc)
            time.sleep(5)


def build_year(parquet_roots: list[Path], year: int) -> None:
    """Build, check and load one year."""
    started = time.monotonic()
    con = open_duckdb()
    sources = YearSources(con, parquet_roots, year)
    build_sejm(con, sources)
    if build_senat.available(sources):
        build_senat.build_senat(con, sources)
    else:
        log.info("year %s: no staged Senate files, Senate tables stay empty", year)
    with psycopg.connect(DATABASE_URL) as pg:
        schema.ensure_schema(pg)
        load_year(pg, con, year)
    log.info("year %s built in %.1fs", year, time.monotonic() - started)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--parquet-root", type=Path, default=Path("data/kbw_stage_parquet"), help="Staged KBW mirror")
    parser.add_argument(
        "--pkw-parquet-root",
        type=Path,
        default=Path("data/pkw_stage_parquet"),
        help="Staged PKW 'dane w arkuszach' bundles; preferred per dataset (KBW's 2023 Senate precinct file is empty)",
    )
    parser.add_argument("--years", type=str, default=None, help=f"Comma-separated subset of {SUPPORTED_YEARS}")
    parser.add_argument("--wait-db-seconds", type=int, default=0, help="Wait for PostgreSQL before building")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[mart] %(levelname)s %(message)s")

    roots = [root for root in (args.pkw_parquet_root, args.parquet_root) if root.is_dir()]
    log.info("staging roots in priority order: %s", [str(r) for r in roots])
    if args.wait_db_seconds:
        wait_for_database(args.wait_db_seconds)
    if args.years:
        years = [int(y) for y in args.years.split(",") if y.strip()]
        unsupported = sorted(set(years) - set(SUPPORTED_YEARS))
        if unsupported:
            parser.error(f"unsupported years {unsupported}; supported: {SUPPORTED_YEARS}")
    else:
        years = [y for y in SUPPORTED_YEARS if available_layout(YearSources(open_duckdb(), roots, y))]
        log.info("years with staged Sejm results: %s", years)
    if not years:
        raise SystemExit(f"[mart] nothing to build: no staged Sejm results under {roots}")

    failed = []
    for year in years:
        try:
            build_year(roots, year)
        except BuildError as exc:
            log.error("%s", exc)
            failed.append(year)

    with psycopg.connect(DATABASE_URL) as pg:
        schema.ensure_schema(pg)
        derived.rebuild(pg)
        schema.ensure_reader_role(pg, statement_timeout_ms=MART_STATEMENT_TIMEOUT_MS)
    if failed:
        raise SystemExit(f"[mart] years failed consistency checks and were not loaded: {failed}")
    log.info("done: %s", years)


if __name__ == "__main__":
    main()
