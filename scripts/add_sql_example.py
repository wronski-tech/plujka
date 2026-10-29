#!/usr/bin/env python3
"""Add or check curated question -> SQL examples (``api/services/mart/sql_examples.jsonl``).

The examples are few-shot prompts for LLM-written SQL, so every one must pass the SQL guard and
return rows from the built mart. ``--check`` runs all curated examples (use after rebuilding the
mart or changing views); otherwise the given pair is validated, executed and appended.

Examples:
  python3 scripts/add_sql_example.py --check
  python3 scripts/add_sql_example.py --question "Frekwencja w wyborach do Sejmu 2023" \
      --sql "SELECT turnout_pct FROM mart.v_sejm_turnout WHERE year = 2023 AND level = 'country'"

Needs ``MART_DATABASE_URL`` (the read-only role) pointing at a built mart.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.mart import examples  # noqa: E402
from api.services.mart.safe_sql import run_readonly, validate_mart_sql  # noqa: E402


def check_example(question: str, sql: str) -> tuple[bool, str]:
    """Validate and execute one example; return (ok, message)."""
    try:
        rows = run_readonly(validate_mart_sql(sql))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if not rows:
        return False, "no rows"
    return True, f"{len(rows)} rows, first: {rows[0]}"


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Validate and run every curated example")
    parser.add_argument("--question", help="Question in Polish")
    parser.add_argument("--sql", help="SQL over the mart schema")
    args = parser.parse_args()

    if args.check:
        failed = 0
        for example in examples.load_seed():
            ok, message = check_example(example["question"], example["sql"])
            failed += not ok
            print(f"[{'ok' if ok else 'FAIL'}] {example['question']}\n       {message[:300]}")
        if failed:
            raise SystemExit(f"{failed} curated example(s) failed")
        return

    if not (args.question and args.sql):
        parser.error("--question and --sql are required unless --check is given")
    ok, message = check_example(args.question, args.sql)
    print(message)
    if not ok:
        raise SystemExit("example rejected")
    known = {examples.example_id(e["question"]) for e in examples.load_seed()}
    if examples.example_id(args.question) in known:
        raise SystemExit("an example with this question already exists")
    with examples.SEED_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"question": args.question, "sql": " ".join(args.sql.split())}, ensure_ascii=False) + "\n")
    print(f"added to {examples.SEED_PATH}")


if __name__ == "__main__":
    main()
