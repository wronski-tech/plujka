#!/usr/bin/env python3
"""Evaluate LLM-written SQL against the evaluation set (``tests/eval/questions.jsonl``).

Each item is a question with either a reference ``sql`` (its rows are the expected result) or
``"expect": "clarify"`` (the question must end in a clarification); a follow-up item also has
``previous`` (``{"question", "sql"}`` of the earlier turn, sent as the conversation's previous turn). A generated answer passes when
every expected row is found in its own generated row: all numbers of the expected row appear in it
(to 0.05, so a percentage rounded to one decimal still matches); rows without numbers compare their
texts instead. Extra columns and extra rows do not fail an item.

  --check   runs every reference query through the SQL guard and reports entity resolution
            (no OpenAI key needed; CI runs it against the built mart)
  (default) asks the model for every question and writes a report with accuracy, clarifications,
            repairs, tokens, cost and time per election family
  --baseline REPORT  compares with an earlier report (same questions only): lists questions that
            passed there and fail now, and exits with an error when there are more than
            --max-regressions of them; the report records the model and the prompt version, so a
            regression after changing either is attributed to that change

Examples:
  python3 scripts/run_eval.py --check
  python3 scripts/run_eval.py --family sejm --limit 20 --out eval_report.json
  python3 scripts/run_eval.py --out eval_report.json --baseline previous/eval_report.json --max-regressions 5

Needs ``MART_DATABASE_URL`` (the read-only role) pointing at a built mart; the evaluation itself also
needs ``OPENAI_API_KEY``. Cost uses ``EVAL_USD_PER_1M_INPUT`` / ``EVAL_USD_PER_1M_OUTPUT`` when set.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services import dynamic_sql  # noqa: E402
from api.services.mart.entities import fold  # noqa: E402
from api.services.mart.safe_sql import run_readonly, validate_mart_sql  # noqa: E402

EVAL_PATH = ROOT / "tests" / "eval" / "questions.jsonl"
TOLERANCE = 0.0501  # half of 0.1, plus float error


def load_items(path: Path = EVAL_PATH) -> list[dict]:
    """Evaluation items in file order."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _split(row: dict) -> tuple[list[float], list[str]]:
    """Numbers and folded texts of one result row (booleans and NULLs are ignored)."""
    numbers: list[float] = []
    texts: list[str] = []
    for value in row.values():
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float, Decimal)):
            numbers.append(float(value))
        elif isinstance(value, (dt.date, dt.datetime)):
            texts.append(value.isoformat()[:10])
        else:
            texts.append(fold(str(value)))
    return numbers, texts


def _row_index(expected: dict, actual: list[tuple[list[float], list[str]]]) -> int | None:
    """Index of the first generated row that contains ``expected``, or None."""
    numbers, texts = _split(expected)
    for index, (got_numbers, got_texts) in enumerate(actual):
        if numbers:
            if all(any(abs(n - g) <= TOLERANCE for g in got_numbers) for n in numbers):
                return index
        elif all(any(t in g or g in t for g in got_texts) for t in texts):
            return index
    return None


def rows_match(expected: list[dict], actual: list[dict]) -> bool:
    """True when every expected row is found in its own generated row (see the module docstring)."""
    if not expected:
        return False
    remaining = [_split(row) for row in actual]
    for row in expected:
        index = _row_index(row, remaining)
        if index is None:
            return False
        del remaining[index]
    return True


def check(items: list[dict]) -> int:
    """Run every reference query; return the number of failures.

    Entity resolution is reported but does not fail: a question that wrongly ends in a clarification
    is a defect of the matcher, which the evaluation itself measures.
    """
    failed = warned = 0
    for item in items:
        problems = []
        clarification = dynamic_sql.resolve_question_entities(item["question"], None, item.get("previous"))[1]
        if bool(clarification) != (item.get("expect") == "clarify"):
            warned += 1
            got = f"clarification about {clarification['kind']}" if clarification else "no clarification"
            print(f"[warn] {item['id']} {item['question']}\n       {got}")
        if "sql" in item:
            try:
                rows = run_readonly(validate_mart_sql(item["sql"]))
                if not rows:
                    problems.append("reference query returned no rows")
            except Exception as exc:
                problems.append(f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        failed += bool(problems)
        if problems:
            print(f"[FAIL] {item['id']} {item['question']}\n       {'; '.join(problems)}")
    print(f"{len(items) - failed}/{len(items)} evaluation items OK; {warned} with unexpected entity resolution")
    return failed


def evaluate(item: dict) -> dict:
    """Ask the model for one item and grade the result."""
    started = time.monotonic()
    rows, sql, error, meta = dynamic_sql.try_dynamic_query(item["question"], None, item.get("previous"))
    status = meta.get("status", "skipped")
    if item.get("expect") == "clarify":
        passed = status == "clarify"
    else:
        passed = status in ("first_pass", "repaired", "cached") and rows_match(run_readonly(item["sql"]), rows)
    tokens = meta.get("tokens") or {}
    return {
        "id": item["id"],
        "family": item["family"],
        "question": item["question"],
        "status": status,
        "passed": passed,
        "prompt_tokens": tokens.get("prompt_tokens", 0),
        "completion_tokens": tokens.get("completion_tokens", 0),
        "ms": int((time.monotonic() - started) * 1000),
        "sql": sql,
        "error": error,
    }


def summarize(results: list[dict]) -> str:
    """Markdown table of the metrics per family, with a total row."""
    usd_in = float(os.getenv("EVAL_USD_PER_1M_INPUT", "0"))
    usd_out = float(os.getenv("EVAL_USD_PER_1M_OUTPUT", "0"))
    groups: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        groups[result["family"]].append(result)
    lines = [
        "| family | items | accuracy | clarify | repaired | failed/rejected | tokens | cost USD | avg s |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for family, group in [*sorted(groups.items()), ("total", results)]:
        statuses = Counter(r["status"] for r in group)
        prompt = sum(r["prompt_tokens"] for r in group)
        completion = sum(r["completion_tokens"] for r in group)
        cost = (prompt * usd_in + completion * usd_out) / 1_000_000
        lines.append(
            f"| {family} | {len(group)} | {100 * sum(r['passed'] for r in group) / len(group):.1f}% "
            f"| {statuses['clarify']} | {statuses['repaired']} | {statuses['failed'] + statuses['rejected']} "
            f"| {prompt + completion} | {cost:.3f} | {sum(r['ms'] for r in group) / len(group) / 1000:.1f} |"
        )
    return "\n".join(lines)


def prompt_version() -> str:
    """Short hash of the fixed SQL rules sent with every question (changes when the prompt does)."""
    return hashlib.sha256(dynamic_sql.SQL_RULES.encode()).hexdigest()[:12]


def compare(baseline: dict, results: list[dict]) -> tuple[str, list[dict]]:
    """Markdown comparison with ``baseline`` (a report written by --out) and the regressed items."""
    before = {r["id"]: r for r in baseline["results"]}
    common = [r for r in results if r["id"] in before]
    regressed = [r for r in common if before[r["id"]]["passed"] and not r["passed"]]
    fixed = [r for r in common if not before[r["id"]]["passed"] and r["passed"]]
    old = sum(before[r["id"]]["passed"] for r in common)
    new = sum(r["passed"] for r in common)
    lines = [
        f"Baseline: model {baseline.get('model', '?')}, prompt {baseline.get('prompt_version', '?')}; "
        f"now: model {dynamic_sql.OPENAI_CHAT_MODEL}, prompt {prompt_version()}.",
        f"Same {len(common)} questions: {old} -> {new} passed; {len(regressed)} regressed, {len(fixed)} fixed.",
    ]
    lines += [f"- regressed {r['id']} ({r['status']}): {r['question']}" for r in regressed]
    return "\n".join(lines), regressed


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Check reference queries and entities only (no model)")
    parser.add_argument("--family", help="Only items of this family")
    parser.add_argument("--limit", type=int, help="At most this many items")
    parser.add_argument("--out", type=Path, help="Write the per-item report as JSON")
    parser.add_argument("--baseline", type=Path, help="Earlier report to compare with (regression check)")
    parser.add_argument("--max-regressions", type=int, default=0, help="Regressed questions tolerated (default 0)")
    args = parser.parse_args()

    items = [i for i in load_items() if not args.family or i["family"] == args.family][: args.limit]
    if args.check:
        if check(items):
            raise SystemExit("evaluation set has failing items")
        return
    dynamic_sql.SQL_CACHE.ttl = 0  # grade the model, not SQL cached from an earlier run in this process
    results = []
    for item in items:
        result = evaluate(item)
        results.append(result)
        print(f"[{'ok' if result['passed'] else 'FAIL'}] {result['id']} {result['status']}: {item['question']}", flush=True)
    summary = summarize(results)
    regressed: list[dict] = []
    if args.baseline:
        comparison, regressed = compare(json.loads(args.baseline.read_text(encoding="utf-8")), results)
        summary += "\n\n" + comparison
    print(summary)
    if args.out:
        report = {"model": dynamic_sql.OPENAI_CHAT_MODEL, "prompt_version": prompt_version(), "summary": summary, "results": results}
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if len(regressed) > args.max_regressions:
        raise SystemExit(f"{len(regressed)} regressed question(s), more than --max-regressions {args.max_regressions}")


if __name__ == "__main__":
    main()
