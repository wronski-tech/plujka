#!/usr/bin/env python3
"""Load KBW mirror tabular files into kbw_election_runs + kbw_facts (see api/services/kbw_import.py)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services import db, kbw_import  # noqa: E402


def main() -> None:
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
    args = parser.parse_args()

    db.init_database()
    if args.clear:
        print("[kbw-import] clearing existing kbw_facts/kbw_election_runs...", flush=True)
        kbw_import.clear_kbw_imported_facts()
        print("[kbw-import] clear done", flush=True)

    stats = kbw_import.import_all_kbw_csv_facts(
        root=args.root,
        limit=args.limit,
        progress_every=args.progress_every,
    )
    if args.errors_out:
        args.errors_out.parent.mkdir(parents=True, exist_ok=True)
        args.errors_out.write_text(json.dumps(stats.get("error_details", []), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
