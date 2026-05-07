#!/usr/bin/env python3
"""Populate ``kbw_person_election_fact`` from ``kbw_facts`` (candidate-level Sejm files).

Run after ``import_kbw_facts.py`` when you want cross-election person analytics without waiting
for the full relational KBW model. See ``docs/ANALYTICS_ARCHITECTURE.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services import db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill kbw_person_election_fact from kbw_facts.")
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Limit to a single election year (default: all years in facts)",
    )
    args = parser.parse_args()
    db.init_database()
    n = db.backfill_kbw_person_election_facts(year=args.year)
    print(f"backfill_kbw_person_election_facts: rowcount={n}")


if __name__ == "__main__":
    main()
