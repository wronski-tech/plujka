from __future__ import annotations

import json
import re

from openai import OpenAI

from api.services import db
from api.services.config import OPENAI_API_KEY, OPENAI_CHAT_MODEL

ALLOWED_TABLES = {
    "kbw_facts",
    "kbw_election_runs",
    "kbw_dane_files",
    "kbw_v_sejm_district_list_agg",
    "kbw_person_election_fact",
    "kbw_candidates",
    "kbw_candidate_geo_votes",
    "elections",
    "results",
    "candidates",
    "elected_candidates",
    "sejm_aggregate_results",
    "sejm_candidate_ballots",
    "senate_results",
}

BLOCKED_SQL_PATTERNS = (
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\bdrop\b",
    r"\balter\b",
    r"\btruncate\b",
    r"\bcreate\b",
    r"\bgrant\b",
    r"\brevoke\b",
    r";",
    r"--",
    r"/\*",
)


def _looks_like_select(sql: str) -> bool:
    """Looks like select."""
    s = sql.strip().lower()
    return s.startswith("select") or s.startswith("with")


def _tables_referenced(sql: str) -> set[str]:
    """Tables referenced."""
    tables: set[str] = set()
    for pat in (r"\bfrom\s+([a-zA-Z_][\w\.]*)", r"\bjoin\s+([a-zA-Z_][\w\.]*)"):
        for match in re.finditer(pat, sql, flags=re.IGNORECASE):
            ref = match.group(1).split(".")[-1].strip('"')
            tables.add(ref)
    return tables


def validate_safe_sql(sql: str) -> tuple[bool, str | None]:
    """Validate safe sql."""
    if not sql or not _looks_like_select(sql):
        return False, "Only SELECT/CTE queries are allowed."
    lowered = sql.lower()
    for pat in BLOCKED_SQL_PATTERNS:
        if re.search(pat, lowered, flags=re.IGNORECASE):
            return False, f"Blocked token/pattern detected: {pat}"
    refs = _tables_referenced(sql)
    if not refs:
        return False, "No table reference detected."
    unknown = refs - ALLOWED_TABLES
    if unknown:
        return False, f"Unknown/forbidden tables: {', '.join(sorted(unknown))}"
    if "limit" not in lowered:
        return False, "Query must include LIMIT."
    return True, None


def _schema_hint() -> str:
    """Schema hint."""
    return """
You can query only these tables:
- kbw_election_runs(id, family, year, round, slice, variant, dataset_hint)
- kbw_facts(id, election_run_id, geography jsonb, subject jsonb, metric, value, is_percentage, source_file_id)
- kbw_v_sejm_district_list_agg(election_run_id, year, district, list_label, votes, has_csv_source) — aggregated Sejm list votes by district
- kbw_person_election_fact(id, person_key, display_name, year, election_family, party_list_label, votes, elected, district, kbw_source_file_id) — cross-election persons; fill with scripts/backfill_kbw_person_facts.py after KBW import
- kbw_candidates(id, election_run_id, person_key, district, list_label, display_name, list_position, votes) — relational slice; fill with db.sync_kbw_candidates_from_person_facts after person backfill
- kbw_candidate_geo_votes(kbw_fact_id PK→kbw_facts.id, election_run_id, person_key, display_name, votes) — per-row candidate votes; JOIN kbw_facts ON kbw_facts.id = kbw_candidate_geo_votes.kbw_fact_id for geography jsonb
- kbw_dane_files(id, rel_path, file_name, file_ext, file_kind, year, dataset_key, header)
- elections(id, year, type)
- results(id, candidate_id, election_id, votes, district, list_position)
- candidates(id, name)
- elected_candidates(year, district, committee_name, candidate_name, candidate_votes, list_position)
- sejm_aggregate_results(election_id, geography_level, sejm_district, teryt, gmina, powiat, wojewodztwo, committee_name, metric_value, is_percentage)
- sejm_candidate_ballots(year, district, committee_name, candidate_name, list_position, total_votes)
- senate_results(election_id, senate_district, symbol_kontrolny, teryt, numer_obwodu, gmina, powiat, wojewodztwo, candidate_name, votes)

For kbw_facts:
- geography and subject are JSONB; use ->> to extract keys, e.g. geography->>'Gmina', subject->>'label'
- election run context is in kbw_election_runs joined by kbw_facts.election_run_id

Rules:
- Produce a single PostgreSQL SELECT (or WITH...SELECT) query.
- Never use DDL/DML.
- Include LIMIT <= 500.
- Prefer explicit ORDER BY for deterministic results.
"""


def _data_context_hint() -> str:
    """Small runtime context to reduce wrong key/table guesses."""
    try:
        families = db.run_sql(
            """
            SELECT family, COUNT(*)::int AS runs
            FROM kbw_election_runs
            GROUP BY family
            ORDER BY runs DESC
            LIMIT 12
            """,
            {},
        )
        metrics = db.run_sql(
            """
            SELECT metric, COUNT(*)::int AS cnt
            FROM kbw_facts
            GROUP BY metric
            ORDER BY cnt DESC
            LIMIT 20
            """,
            {},
        )
        geo_keys = db.run_sql(
            """
            SELECT key, COUNT(*)::int AS cnt
            FROM (
                SELECT jsonb_object_keys(geography) AS key
                FROM kbw_facts
                LIMIT 200000
            ) t
            GROUP BY key
            ORDER BY cnt DESC
            LIMIT 20
            """,
            {},
        )
        subject_keys = db.run_sql(
            """
            SELECT key, COUNT(*)::int AS cnt
            FROM (
                SELECT jsonb_object_keys(subject) AS key
                FROM kbw_facts
                LIMIT 200000
            ) t
            GROUP BY key
            ORDER BY cnt DESC
            LIMIT 20
            """,
            {},
        )
    except Exception:  # noqa: BLE001
        return ""

    return (
        "Runtime data samples:\n"
        f"- families in kbw_election_runs: {json.dumps(families, ensure_ascii=False)}\n"
        f"- common kbw_facts.metric values: {json.dumps(metrics, ensure_ascii=False)}\n"
        f"- common geography JSON keys: {json.dumps(geo_keys, ensure_ascii=False)}\n"
        f"- common subject JSON keys: {json.dumps(subject_keys, ensure_ascii=False)}\n"
    )


def _llm_generate_sql(question: str, *, previous_sql: str | None = None, previous_error: str | None = None) -> tuple[str | None, str | None]:
    """Llm generate sql."""
    if not OPENAI_API_KEY:
        return None, "OPENAI_API_KEY is missing."
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = (
        "You generate safe PostgreSQL SELECT queries for a Polish election analytics app. "
        "Return JSON with exactly one key: sql."
    )
    repair_note = ""
    if previous_sql or previous_error:
        repair_note = (
            "\nPrevious attempt failed. Fix the query using the error details.\n"
            f"Previous SQL: {previous_sql or ''}\n"
            f"PostgreSQL error: {previous_error or ''}\n"
        )
    response = client.chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": prompt + "\n" + _schema_hint() + "\n" + _data_context_hint() + repair_note},
            {"role": "user", "content": question},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None, "Model response was not valid JSON."
    sql = (payload.get("sql") or "").strip()
    ok, err = validate_safe_sql(sql)
    if not ok:
        return None, err
    return sql, None


def generate_dynamic_sql(question: str) -> tuple[str | None, str | None]:
    """Generate dynamic sql."""
    return _llm_generate_sql(question)


def try_dynamic_query(question: str) -> tuple[list[dict], str | None, str | None, dict]:
    """Try dynamic query."""
    sql, err = generate_dynamic_sql(question)
    if not sql:
        return [], None, err, {"status": "skipped", "phase": "generation", "error": err}
    try:
        rows = db.run_sql(sql, {})
    except Exception as exc:  # noqa: BLE001
        # One repair attempt with SQL + exact DB error.
        repaired_sql, repair_err = _llm_generate_sql(
            question,
            previous_sql=sql,
            previous_error=str(exc),
        )
        if not repaired_sql:
            return [], sql, repair_err or str(exc), {
                "status": "failed",
                "phase": "repair_generation",
                "first_sql_error": str(exc),
                "error": repair_err or str(exc),
            }
        try:
            repaired_rows = db.run_sql(repaired_sql, {})
        except Exception as exc2:  # noqa: BLE001
            return [], repaired_sql, str(exc2), {
                "status": "failed",
                "phase": "repair_execution",
                "first_sql_error": str(exc),
                "error": str(exc2),
            }
        return repaired_rows, repaired_sql, None, {
            "status": "repaired",
            "phase": "repair_execution",
            "first_sql_error": str(exc),
        }
    return rows, sql, None, {"status": "first_pass", "phase": "execution"}
