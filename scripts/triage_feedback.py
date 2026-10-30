#!/usr/bin/env python3
"""Group thumbs-down answers with the LLM and propose corrected SQL examples for review.

Reads the thumbs-down lines of the feedback log (``FEEDBACK_JSONL_PATH``), asks the chat model to
group the questions by the likely problem (wrong table, wrong entity, missing data, ...), writes the
groups to ``feedback_groups.md`` and, for each group with an LLM-written query, asks the model for a
corrected query (the usual dynamic SQL prompt, with the rejected query and the problem as the error
to fix). A proposal is kept only when it passes the SQL guard and returns rows; proposals go to
``example_proposals.jsonl`` next to the log and reach the prompt only after
``scripts/add_sql_example.py --review`` accepts them. Questions already proposed are skipped.

  python3 scripts/triage_feedback.py

Needs ``OPENAI_API_KEY`` and ``MART_DATABASE_URL`` (the read-only role) pointing at a built mart.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openai import OpenAI  # noqa: E402

from api.services import dynamic_sql  # noqa: E402
from api.services.config import FEEDBACK_JSONL_PATH, OPENAI_API_KEY, OPENAI_CHAT_MODEL  # noqa: E402
from api.services.mart import examples, safe_sql  # noqa: E402

GROUPING_PROMPT = """You triage answers that users of a Polish election Q&A tool marked as wrong.
Each item has an index, the question, the answer route (intent), the SQL that ran (if any) and the
first result rows. Group the items by the most likely shared problem, for example: wrong relation
or column, wrong entity (person or place), wrong election or year, misread question, data the
database does not have. Return JSON:
{"groups": [{"problem": "<one English sentence>", "items": [<indexes>]}]}
Every index appears in exactly one group."""


def load_thumbs_down(path: Path) -> list[dict]:
    """Thumbs-down records with their answer snapshot, latest per question."""
    latest: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line) if line.strip() else {}
            if record.get("rating") == "thumbs_down" and record.get("ask_response"):
                latest[examples.example_id(record["question"])] = record
    return list(latest.values())


def llm_groups(items: list[dict]) -> list[dict]:
    """Ask the chat model to group ``items``; return ``[{"problem", "items"}]``."""
    if not OPENAI_API_KEY:
        raise SystemExit("OPENAI_API_KEY is missing.")
    listing = [
        {"index": i, "question": r["question"], "intent": r["ask_response"].get("intent"),
         "sql": r["ask_response"].get("sql"), "rows": (r["ask_response"].get("result") or [])[:3]}
        for i, r in enumerate(items)
    ]
    response = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": GROUPING_PROMPT},
                  {"role": "user", "content": json.dumps(listing, ensure_ascii=False, default=str)}],
    )
    return json.loads(response.choices[0].message.content or "{}").get("groups") or []


def propose(record: dict, problem: str) -> str | None:
    """A corrected, guarded query with rows for one thumbs-down record, or None."""
    question, rejected = record["question"], record["ask_response"].get("sql")
    if not rejected:
        return None
    matches, _ = dynamic_sql.resolve_question_entities(question)
    context, _, _ = dynamic_sql.build_context(question, matches)
    error = f"The user marked the result of this query as wrong. Likely problem: {problem}"
    try:
        sql = safe_sql.validate_mart_sql(dynamic_sql._llm_sql(question, context, previous_sql=rejected, previous_error=error)[0])
        # The guard normalizes both, so an unchanged query compares equal.
        if sql == safe_sql.validate_mart_sql(rejected) or not safe_sql.run_readonly(sql):
            return None
    except Exception as exc:  # one bad proposal must not stop the triage
        print(f"no proposal for {question!r}: {type(exc).__name__}: {exc}")
        return None
    return sql


def triage(feedback_path: Path, group=llm_groups) -> int:
    """Write the groups report and new proposals; return the number of proposals written."""
    items = load_thumbs_down(feedback_path)
    proposals_path = feedback_path.with_name("example_proposals.jsonl")
    done = set()
    if proposals_path.exists():
        done = {examples.example_id(json.loads(line)["question"])
                for line in proposals_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    if not items:
        print("no thumbs-down answers")
        return 0
    report = [f"# Thumbs-down groups ({len(items)} questions)", ""]
    written = 0
    for g in group(items):
        members = [items[i] for i in g.get("items", []) if isinstance(i, int) and 0 <= i < len(items)]
        report += [f"## {g.get('problem', '?')} ({len(members)})", ""]
        for record in members:
            sql = None
            if examples.example_id(record["question"]) not in done:
                sql = propose(record, g.get("problem", ""))
            report.append(f"- {record['question']}" + (" — corrected query proposed" if sql else ""))
            if sql:
                with proposals_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"question": record["question"], "sql": sql, "note": g.get("problem", "")},
                                            ensure_ascii=False) + "\n")
                written += 1
        report.append("")
    feedback_path.with_name("feedback_groups.md").write_text("\n".join(report), encoding="utf-8")
    print(f"{len(items)} thumbs-down question(s), {written} proposal(s); review with scripts/add_sql_example.py --review")
    return written


if __name__ == "__main__":
    triage(Path(FEEDBACK_JSONL_PATH))
