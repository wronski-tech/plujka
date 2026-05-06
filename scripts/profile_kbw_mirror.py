#!/usr/bin/env python3
"""Scan data/kbw_mirror/dane and upsert kbw_dane_files (inventory + CSV headers)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from api.services import db, kbw_catalog  # noqa: E402
except ModuleNotFoundError as exc:
    if getattr(exc, "name", None) == "psycopg":
        print(
            "psycopg is not installed. From the repo root, run:\n"
            "  python3 -m pip install -r api/requirements.txt\n"
            "or use a venv: python3 -m venv .venv && source .venv/bin/activate && pip install -r api/requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)
    raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=kbw_catalog.DEFAULT_KBW_ROOT,
        help="Mirror root (default: data/kbw_mirror/dane)",
    )
    args = parser.parse_args()

    db.init_database()
    stats = kbw_catalog.profile_kbw_dane_files(args.root)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
