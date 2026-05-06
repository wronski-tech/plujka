#!/usr/bin/env python3
"""Load KBW mirror tabular files into kbw_election_runs + kbw_facts (see api/services/kbw_import.py)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services import db, kbw_import  # noqa: E402


def _parse_int_set(raw: str) -> set[int]:
    """Parse int set."""
    out: set[int] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        out.add(int(value))
    return out


def _parse_ext_set(raw: str) -> set[str]:
    """Parse ext set."""
    out: set[str] = set()
    for part in raw.split(","):
        value = part.strip().lower().lstrip(".")
        if value:
            out.add(value)
    return out


def _init_db_with_retry(wait_seconds: int) -> None:
    """Init db with retry."""
    if wait_seconds <= 0:
        db.init_database()
        return
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            db.init_database()
            if attempt > 1:
                print(f"[kbw-import] database ready after {attempt} attempts", flush=True)
            return
        except Exception as exc:  # noqa: BLE001
            elapsed = int(time.monotonic() - start)
            if elapsed >= wait_seconds:
                raise
            print(
                f"[kbw-import] db not ready ({type(exc).__name__}: {exc}); "
                f"retry in 5s ({elapsed}/{wait_seconds}s)",
                flush=True,
            )
            time.sleep(5)


def main() -> None:
    """Main."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("data/kbw_mirror/dane"),
        help="Mirror root (catalog paths must match kbw_dane_files.rel_path)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Import at most N files (debug)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete kbw_facts and kbw_election_runs before import",
    )
    parser.add_argument(
        "--errors-out",
        type=Path,
        default=None,
        help="Optional path to save detailed import errors as JSON",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress every N seen files (0 disables progress logs)",
    )
    parser.add_argument(
        "--years",
        type=str,
        default=None,
        help="Comma-separated years to import only, e.g. 1997,2023",
    )
    parser.add_argument(
        "--exts",
        type=str,
        default=None,
        help="Comma-separated extensions filter, e.g. csv,xls,xlsx,zip",
    )
    parser.add_argument(
        "--wait-db-seconds",
        type=int,
        default=180,
        help="Wait up to N seconds for DB readiness before failing (0 disables retry).",
    )
    args = parser.parse_args()

    years = _parse_int_set(args.years) if args.years else None
    exts = _parse_ext_set(args.exts) if args.exts else None

    _init_db_with_retry(args.wait_db_seconds)
    if args.clear:
        print("[kbw-import] clearing existing kbw_facts/kbw_election_runs...", flush=True)
        kbw_import.clear_kbw_imported_facts()
        print("[kbw-import] clear done", flush=True)

    stats = kbw_import.import_all_kbw_csv_facts(
        root=args.root,
        limit=args.limit,
        progress_every=args.progress_every,
        years=years,
        exts=exts,
    )
    if args.errors_out:
        args.errors_out.parent.mkdir(parents=True, exist_ok=True)
        args.errors_out.write_text(json.dumps(stats.get("error_details", []), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
