#!/usr/bin/env python3
"""Build the relational election mart (PostgreSQL schema ``mart``) from staged KBW/PKW Parquet.

Elections are grouped in families that are built together from one year's files:

- ``parliament``: Sejm and Senate (2015, 2019, 2023)
- ``prezydent``: presidential elections, both rounds (2015, 2020, 2025)
- ``pe``: European Parliament (2019, 2024)
- ``referendum``: nationwide referendums (2015, 2023)
- ``samorzad``: local elections, voivodeship assemblies and mayors (2018, 2024)

Every family-year is built in its own in-memory DuckDB database, checked against official totals
and loaded into PostgreSQL in one transaction (its tables and its ``mart.elections`` rows). A failed
consistency check skips that family-year before anything is written. Afterwards persons,
materialized views and the entity lexicon are rebuilt from everything loaded, and the read-only role
used for LLM-generated SQL is refreshed.

Examples:
  python3 scripts/build_mart.py                                   # everything staged
  python3 scripts/build_mart.py --years 2023                      # every family of 2023
  python3 scripts/build_mart.py --elections prezydent,pe          # some families, all years
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import duckdb
import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.config import DATABASE_URL, MART_STATEMENT_TIMEOUT_MS  # noqa: E402
from api.services.mart import (  # noqa: E402
    build_pe,
    build_prezydent,
    build_referendum,
    build_samorzad,
    build_senat,
    derived,
    schema,
)
from api.services.mart.build_common import (  # noqa: E402
    BuildError,
    Election,
    YearSources,
    load_tables,
    open_duckdb,
    record_elections,
)
from api.services.mart.build_sejm import available_layout, build_sejm  # noqa: E402

log = logging.getLogger("mart")

PARLIAMENT_DATES = {2015: "2015-10-25", 2019: "2019-10-13", 2023: "2023-10-15"}


def build_parliament(con: duckdb.DuckDBPyConnection, sources: YearSources) -> list[Election]:
    """Sejm and, when staged, Senate of one year (they share the precinct table)."""
    year = sources.year
    build_sejm(con, sources)
    elections = [Election("sejm", year, 1, PARLIAMENT_DATES[year], f"Sejm election {year}")]
    if build_senat.available(sources):
        build_senat.build_senat(con, sources)
        elections.append(Election("senat", year, 1, PARLIAMENT_DATES[year], f"Senate election {year}"))
    else:
        log.info("year %s: no staged Senate files, Senate tables stay empty", year)
    return elections


@dataclass(frozen=True)
class Family:
    """Elections built from the same files: which years exist, which tables they fill, how to build."""

    years: tuple[int, ...]
    tables: tuple[str, ...]
    kinds: tuple[str, ...]  # values of mart.elections.election owned by the family
    available: Callable[[YearSources], bool]
    build: Callable[[duckdb.DuckDBPyConnection, YearSources], list[Election]]


FAMILIES = {
    "parliament": Family(
        (2015, 2019, 2023), schema.PARLIAMENT_TABLES, ("sejm", "senat"),
        lambda s: available_layout(s) is not None, build_parliament,
    ),
    "prezydent": Family(
        (2015, 2020, 2025), build_prezydent.TABLES, build_prezydent.FAMILY,
        build_prezydent.available, build_prezydent.build_prezydent,
    ),
    "pe": Family((2019, 2024), build_pe.TABLES, build_pe.FAMILY, build_pe.available, build_pe.build_pe),
    "referendum": Family(
        (2015, 2023), build_referendum.TABLES, build_referendum.FAMILY,
        build_referendum.available, build_referendum.build_referendum,
    ),
    "samorzad": Family(
        (2018, 2024), build_samorzad.TABLES, build_samorzad.FAMILY,
        build_samorzad.available, build_samorzad.build_samorzad,
    ),
}


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


def build_one(roots: list[Path], name: str, family: Family, year: int) -> bool:
    """Build, check and load one family-year; return False when its files are not staged."""
    started = time.monotonic()
    con = open_duckdb()
    try:
        sources = YearSources(con, roots, year)
        if not family.available(sources):
            log.info("%s %s: not staged, skipped", name, year)
            return False
        elections = family.build(con, sources)
        with psycopg.connect(DATABASE_URL) as pg:
            schema.ensure_schema(pg)
            load_tables(pg, con, year, family.tables)
            record_elections(pg, family.kinds, year, elections)
        log.info("%s %s built in %.1fs", name, year, time.monotonic() - started)
        return True
    finally:
        con.close()


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
    parser.add_argument("--years", type=str, default=None, help="Comma-separated years (default: all)")
    parser.add_argument(
        "--elections", type=str, default=None, help=f"Comma-separated families of {sorted(FAMILIES)} (default: all)"
    )
    parser.add_argument("--wait-db-seconds", type=int, default=0, help="Wait for PostgreSQL before building")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[mart] %(levelname)s %(message)s")

    roots = [root for root in (args.pkw_parquet_root, args.parquet_root) if root.is_dir()]
    log.info("staging roots in priority order: %s", [str(r) for r in roots])
    names = [n.strip() for n in args.elections.split(",") if n.strip()] if args.elections else list(FAMILIES)
    unknown = sorted(set(names) - set(FAMILIES))
    if unknown:
        parser.error(f"unknown election families {unknown}; known: {sorted(FAMILIES)}")
    years = {int(y) for y in args.years.split(",") if y.strip()} if args.years else None
    jobs = [(n, y) for n in names for y in FAMILIES[n].years if years is None or y in years]
    if not jobs:
        parser.error("no supported family-year matches --years/--elections")
    if args.wait_db_seconds:
        wait_for_database(args.wait_db_seconds)

    built, failed = [], []
    for name, year in jobs:
        try:
            if build_one(roots, name, FAMILIES[name], year):
                built.append(f"{name} {year}")
        except BuildError as exc:
            log.error("%s %s: %s", name, year, exc)
            failed.append(f"{name} {year}")
    if not built and not failed:
        raise SystemExit(f"[mart] nothing to build: no staged results under {roots}")

    with psycopg.connect(DATABASE_URL) as pg:
        schema.ensure_schema(pg)
        derived.rebuild(pg)
        schema.ensure_reader_role(pg, statement_timeout_ms=MART_STATEMENT_TIMEOUT_MS)
    if failed:
        raise SystemExit(f"[mart] failed consistency checks and were not loaded: {failed}")
    log.info("done: %s", built)


if __name__ == "__main__":
    main()
