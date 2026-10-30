#!/usr/bin/env python3
"""Build the relational election mart (PostgreSQL schema ``mart``) from staged KBW/PKW Parquet.

Elections are grouped in families that are built together from one year's files:

- ``parliament``: Sejm and Senate (1991, 1993, 1997, 2001, 2005, 2007, 2011, 2015, 2019, 2023; Senate 1991: candidates only)
- ``prezydent``: presidential elections, both rounds (1990, 1995; 2000: one round; 2005, 2010, 2015, 2020, 2025)
- ``pe``: European Parliament (2004, 2009, 2014, 2019, 2024)
- ``referendum``: nationwide referendums (1996, 1997, 2003, 2015, 2023)
- ``samorzad``: local elections: voivodeship assemblies, mayors and councils (1998 without mayors, 2002,
  2006, 2010, 2014, 2018, 2024)
- ``gus``: not an election; GUS statistics per territorial unit (population, unemployment, salaries; 1995-2024)

Every family-year is built in its own in-memory DuckDB database, checked against official totals
and loaded into PostgreSQL in one transaction (its tables and its ``mart.elections`` rows). A failed
consistency check skips that family-year before anything is written. Afterwards persons,
materialized views and the entity lexicon are rebuilt from everything loaded, and the read-only role
used for LLM-generated SQL is refreshed.

Examples:
  python3 scripts/build_mart.py                                   # everything staged
  python3 scripts/build_mart.py --years 2023                      # every family of 2023
  python3 scripts/build_mart.py --elections prezydent,pe          # some families, all years
  python3 scripts/build_mart.py --derived-only                    # after restoring a dump of the base tables
"""

from __future__ import annotations

import argparse
import logging
from concurrent.futures import ProcessPoolExecutor
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
    build_gus,
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
    record_checks,
    record_elections,
)
from api.services.mart.build_sejm import available_layout, build_sejm  # noqa: E402

log = logging.getLogger("mart")

PARLIAMENT_DATES = {
    1991: "1991-10-27", 1993: "1993-09-19", 1997: "1997-09-21",
    2001: "2001-09-23", 2005: "2005-09-25", 2007: "2007-10-21", 2011: "2011-10-09", 2015: "2015-10-25",
    2019: "2019-10-13", 2023: "2023-10-15",
}


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
        (1991, 1993, 1997, 2001, 2005, 2007, 2011, 2015, 2019, 2023), schema.PARLIAMENT_TABLES, ("sejm", "senat"),
        lambda s: available_layout(s) is not None, build_parliament,
    ),
    "prezydent": Family(
        (1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025), build_prezydent.TABLES, build_prezydent.FAMILY,
        build_prezydent.available, build_prezydent.build_prezydent,
    ),
    "pe": Family(
        (2004, 2009, 2014, 2019, 2024), build_pe.TABLES, build_pe.FAMILY, build_pe.available, build_pe.build_pe,
    ),
    "referendum": Family(
        (1996, 1997, 2003, 2015, 2023), build_referendum.TABLES, build_referendum.FAMILY,
        build_referendum.available, build_referendum.build_referendum,
    ),
    "samorzad": Family(
        (1994, 1998, 2002, 2006, 2010, 2014, 2018, 2024), build_samorzad.TABLES, build_samorzad.FAMILY,
        build_samorzad.available, build_samorzad.build_samorzad,
    ),
    # Not an election: GUS statistics per unit and year (data/gus); must come after the election families,
    # because its TERYT check reads the loaded election tables.
    "gus": Family(build_gus.YEARS, build_gus.TABLES, build_gus.FAMILY, build_gus.available, build_gus.build_gus),
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


def build_one(roots: list[Path], name: str, family: Family, year: int, ensure: bool = True) -> bool:
    """Build, check and load one family-year; return False when its files are not staged.

    ``ensure=False`` skips ``schema.ensure_schema`` (the caller ran it): its ALTER TABLE statements
    would lock every table for the whole load and serialize parallel builds.
    """
    started = time.monotonic()
    con = open_duckdb()
    try:
        sources = YearSources(con, roots, year)
        if not family.available(sources):
            log.info("%s %s: not staged, skipped", name, year)
            return False
        elections = family.build(con, sources)
        with psycopg.connect(DATABASE_URL) as pg:
            if ensure:
                schema.ensure_schema(pg)
            load_tables(pg, con, year, family.tables)
            record_elections(pg, family.kinds, year, elections)
            record_checks(pg, con, name, family.kinds, year)
        log.info("%s %s built in %.1fs", name, year, time.monotonic() - started)
        return True
    finally:
        con.close()


def build_family(roots: list[Path], name: str, years: list[int], ensure: bool = True) -> tuple[list[str], list[str]]:
    """Build the given years of one family in order; return (built, failed) labels."""
    built, failed = [], []
    for year in years:
        try:
            if build_one(roots, name, FAMILIES[name], year, ensure):
                built.append(f"{name} {year}")
        except BuildError as exc:
            log.error("%s %s: %s", name, year, exc)
            failed.append(f"{name} {year}")
    return built, failed


def _init_worker() -> None:
    logging.basicConfig(level=logging.INFO, format="[mart] %(levelname)s %(message)s", force=True)


def rebuild_derived() -> None:
    """Persons, views, entity lexicon and seat check from everything loaded; then the read-only role."""
    with psycopg.connect(DATABASE_URL) as pg:
        schema.ensure_schema(pg)
        derived.rebuild(pg)
        schema.ensure_reader_role(pg, statement_timeout_ms=MART_STATEMENT_TIMEOUT_MS)


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
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Families built in parallel processes (years of a family stay in order; gus runs last). "
             "Set MART_DUCKDB_MEMORY_LIMIT so that jobs x limit fits in memory.",
    )
    parser.add_argument(
        "--skip-derived", action="store_true",
        help="Load family-years only; leave persons, views and the seat check to a later full run",
    )
    parser.add_argument(
        "--derived-only", action="store_true",
        help="Only rebuild persons, views, the seat check and the reader role from the loaded base tables "
             "(e.g. after pg_restore of a CI mart dump)",
    )
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
    if args.derived_only:
        rebuild_derived()
        log.info("done: derived objects rebuilt")
        return

    by_family = {n: [y for m, y in jobs if m == n] for n in dict.fromkeys(n for n, _ in jobs)}
    built, failed = [], []
    if args.jobs > 1:
        with psycopg.connect(DATABASE_URL) as pg:
            schema.ensure_schema(pg)
        # gus checks its TERYT codes against the election tables, so it runs after them.
        parallel = [n for n in by_family if n != "gus"]
        with ProcessPoolExecutor(max_workers=args.jobs, initializer=_init_worker) as pool:
            results = list(pool.map(build_family, [roots] * len(parallel), parallel, [by_family[n] for n in parallel],
                                    [False] * len(parallel)))
        if "gus" in by_family:
            results.append(build_family(roots, "gus", by_family["gus"], ensure=False))
    else:
        results = [build_family(roots, name, years) for name, years in by_family.items()]
    for family_built, family_failed in results:
        built += family_built
        failed += family_failed
    if not built and not failed:
        raise SystemExit(f"[mart] nothing to build: no staged results under {roots}")

    if not args.skip_derived:
        rebuild_derived()
    if failed:
        raise SystemExit(f"[mart] failed consistency checks and were not loaded: {failed}")
    log.info("done: %s", built)


if __name__ == "__main__":
    main()
