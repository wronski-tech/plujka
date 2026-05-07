#!/usr/bin/env python3
"""Populate ``kbw_candidate_geo_votes`` from ``kbw_facts`` (candidate-level Sejm mirror files).

Join ``kbw_facts`` on ``kbw_fact_id`` for gmina / obwód / TERYT in ``geography``.
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
    n = db.backfill_kbw_candidate_geo_votes_from_facts(year=args.year)
    print(f"backfill_kbw_candidate_geo_votes_from_facts: rowcount={n}")


if __name__ == "__main__":
    main()
