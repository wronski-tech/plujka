#!/usr/bin/env python3
"""Add or check curated question -> SQL examples (``api/services/mart/sql_examples.jsonl``).

The examples are few-shot prompts for LLM-written SQL, so every one must pass the SQL guard and
return rows from the built mart. ``--check`` runs all curated examples (use after rebuilding the
mart or changing views); ``--review`` goes through the thumbs-up answers queued in the feedback log
(``FEEDBACK_JSONL_PATH``): each is run, shown and added only when you accept it; decisions are kept
in ``example_reviews.jsonl`` next to the log so a candidate is asked once. Otherwise the given pair is
validated, executed and appended.

Examples:
  python3 scripts/add_sql_example.py --check
  python3 scripts/add_sql_example.py --review
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

from api.services.config import FEEDBACK_JSONL_PATH  # noqa: E402
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


def append_example(question: str, sql: str) -> None:
    """Append a checked pair to the curated file (refusing a question that is already there)."""
    if examples.example_id(question) in {examples.example_id(e["question"]) for e in examples.load_seed()}:
        raise SystemExit("an example with this question already exists")
    with examples.SEED_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"question": question, "sql": " ".join(sql.split())}, ensure_ascii=False) + "\n")
    examples.load_seed.cache_clear()
    print(f"added to {examples.SEED_PATH}")


def review_candidates(feedback_path: Path, ask=input) -> int:
    """Ask about every undecided thumbs-up candidate; return the number added."""
    reviews_path = feedback_path.with_name("example_reviews.jsonl")
    decided = set()
    if reviews_path.exists():
        decided = {json.loads(line)["id"] for line in reviews_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    curated = {examples.example_id(e["question"]) for e in examples.load_seed()}
    candidates: dict[str, dict] = {}
    if feedback_path.exists():
        for line in feedback_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line) if line.strip() else {}
            if record.get("rating") == "thumbs_up" and record.get("sql"):
                key = examples.example_id(record["question"])
                if key not in decided and key not in curated:
                    candidates[key] = record  # the latest SQL for a repeated question
    added = 0
    for key, record in candidates.items():
        ok, message = check_example(record["question"], record["sql"])
        print(f"\nQ: {record['question']}\n{record['sql']}\n-> {message[:300]}")
        if not ok:
            answer = "n"
            print("rejected automatically")
        else:
            answer = ask("Add as a curated example? [y]es / [n]o / [s]kip for now: ").strip().lower()[:1]
        if answer == "y":
            append_example(record["question"], record["sql"])
            added += 1
        if answer in ("y", "n"):
            with reviews_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"id": key, "question": record["question"], "accepted": answer == "y"},
                                        ensure_ascii=False) + "\n")
    print(f"{len(candidates)} candidate(s), {added} added")
    return added


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Validate and run every curated example")
    parser.add_argument("--review", action="store_true", help="Review queued thumbs-up candidates")
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

    if args.review:
        review_candidates(Path(FEEDBACK_JSONL_PATH))
        return

    if not (args.question and args.sql):
        parser.error("--question and --sql are required unless --check or --review is given")
    ok, message = check_example(args.question, args.sql)
    print(message)
    if not ok:
        raise SystemExit("example rejected")
    append_example(args.question, args.sql)


if __name__ == "__main__":
    main()
