#!/usr/bin/env python3
"""Upsert ``kbw_candidates`` from ``kbw_person_election_fact`` (relational Phase 2 slice).

Requires person facts — run ``scripts/backfill_kbw_person_facts.py`` or import with
``--backfill-person-facts`` first.
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=None, help="Limit to one election year")
    args = parser.parse_args()
    db.init_database()
    n = db.sync_kbw_candidates_from_person_facts(year=args.year)
    print(f"sync_kbw_candidates_from_person_facts: rowcount={n}")


if __name__ == "__main__":
    main()
