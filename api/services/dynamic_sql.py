"""LLM-generated SQL over the relational ``mart`` schema (fallback path of ``router.route_question``).

Flow for one question:
1. ``entities.resolve_entities`` maps inflected names in the question to exact keys (TERYT, person_id,
   committee name) found in ``mart.entity_lexicon``.
2. The prompt carries the mart schema description (table/column ``COMMENT``s read from PostgreSQL),
   the loaded years with their committees, the entity matches and the SQL rules.
3. The model returns one SELECT; ``safe_sql.validate_mart_sql`` checks it with sqlglot and caps LIMIT.
4. ``safe_sql.run_readonly`` runs it as the read-only mart role. On a database error the model gets
   one repair attempt with the failing SQL and the error message.

Answers therefore always come from SQL over official data; the LLM only writes the query.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

import psycopg
from openai import OpenAI

from api.services.config import MART_DATABASE_URL, MART_MAX_ROWS, OPENAI_API_KEY, OPENAI_CHAT_MODEL
from api.services.mart import entities, safe_sql

log = logging.getLogger(__name__)

SQL_RULES = f"""Rules:
- Write exactly one PostgreSQL SELECT (WITH ... SELECT allowed). No other statements.
- Use only relations of schema mart listed below; qualify them as mart.<name>.
- Prefer the views: v_sejm_national_results (national results, thresholds, seats), v_sejm_list_results
  (list support per country/wojewodztwo/powiat/gmina/sejm_district), v_sejm_turnout (turnout per unit),
  v_person_history (all candidacies of a person).
- Always filter by year; when the question gives no year, use the latest loaded year.
- Use the matched entities' keys (teryt_gmina, person_id, committee name) instead of guessing names.
  Committees: filter with '<alias>' = ANY(aliases). Names in questions are inflected Polish forms.
- Percentages in the mart are already 0-100. Order results deterministically.
- Include LIMIT {MART_MAX_ROWS} or less.
Return JSON: {{"sql": "..."}}"""


@lru_cache(maxsize=1)
def schema_description() -> str:
    """Mart relations with their table/column comments, as plain text for the prompt (cached)."""
    with psycopg.connect(MART_DATABASE_URL) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, obj_description(c.oid, 'pg_class'), a.attname,
                   format_type(a.atttypid, a.atttypmod), col_description(c.oid, a.attnum)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE n.nspname = 'mart' AND c.relkind IN ('r', 'v', 'm') AND c.relname <> 'entity_lexicon'
            ORDER BY c.relname, a.attnum
            """
        ).fetchall()
    relations: dict[str, tuple[str, list[str]]] = {}
    for relation, table_doc, column, col_type, column_doc in rows:
        doc, columns = relations.setdefault(relation, (table_doc or "", []))
        columns.append(f"  {column} {col_type}" + (f" -- {column_doc}" if column_doc else ""))
    return "\n\n".join(f"mart.{name} -- {doc}\n" + "\n".join(cols) for name, (doc, cols) in relations.items())


def data_context() -> str:
    """Loaded years and the main committees of each year (small, fetched per question)."""
    with psycopg.connect(MART_DATABASE_URL) as conn:
        committees = conn.execute(
            """
            SELECT year, string_agg(coalesce(aliases[1], committee_name) || ' (seats ' || seats || ')', ', ' ORDER BY votes DESC)
            FROM mart.v_sejm_national_results WHERE pct >= 1 GROUP BY year ORDER BY year
            """
        ).fetchall()
    lines = [f"- {year}: {names}" for year, names in committees]
    return "Loaded Sejm elections and main committees:\n" + "\n".join(lines)


def _llm_sql(question: str, context: str, previous_sql: str | None = None, previous_error: str | None = None) -> str:
    """Ask the chat model for one SQL query; raise ValueError on an unusable answer."""
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is missing.")
    repair = ""
    if previous_sql:
        repair = f"\nThe previous query failed. Fix it.\nPrevious SQL: {previous_sql}\nError: {previous_error}\n"
    response = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": "You write SQL for a Polish election analytics database. Facts come only from the database.\n"
                f"{SQL_RULES}\n\n{context}{repair}",
            },
            {"role": "user", "content": question},
        ],
    )
    try:
        sql = (json.loads(response.choices[0].message.content or "{}").get("sql") or "").strip()
    except json.JSONDecodeError as exc:
        raise ValueError("Model response was not valid JSON.") from exc
    if not sql:
        raise ValueError("Model returned no SQL.")
    return sql


def build_context(question: str) -> tuple[str, list[dict[str, Any]]]:
    """Prompt context (schema, data, entity matches) and the matches as dicts for telemetry."""
    matches = entities.resolve_entities(question)
    hints = entities.format_entity_hints(matches) or "- none"
    context = f"Schema:\n{schema_description()}\n\n{data_context()}\n\nEntities matched in the question:\n{hints}"
    return context, [m.__dict__ for m in matches]


def try_dynamic_query(question: str) -> tuple[list[dict], str | None, str | None, dict]:
    """Generate, validate and run SQL for ``question``.

    Returns ``(rows, sql, error, meta)``; ``meta["status"]`` is one of skipped / rejected / failed /
    first_pass / repaired, and ``meta["entities"]`` lists the entity matches used in the prompt.
    """
    try:
        context, matched = build_context(question)
    except psycopg.Error as exc:
        log.warning("dynamic SQL skipped: mart not reachable (%s)", exc)
        return [], None, str(exc), {"status": "skipped", "phase": "context", "error": str(exc)}
    meta: dict[str, Any] = {"entities": matched}

    sql: str | None = None
    error: str | None = None
    for attempt in ("first_pass", "repaired"):
        try:
            raw = _llm_sql(question, context, previous_sql=sql, previous_error=error)
            sql = safe_sql.validate_mart_sql(raw)
        except safe_sql.UnsafeSqlError as exc:
            log.warning("dynamic SQL rejected: %s", exc)
            return [], None, str(exc), {**meta, "status": "rejected", "phase": attempt, "error": str(exc)}
        except ValueError as exc:
            return [], sql, str(exc), {**meta, "status": "skipped", "phase": attempt, "error": str(exc)}
        try:
            rows = safe_sql.run_readonly(sql)
            return rows, sql, None, {**meta, "status": attempt}
        except psycopg.Error as exc:
            error = str(exc).splitlines()[0]
            log.info("dynamic SQL failed on attempt %s: %s", attempt, error)
    return [], sql, error, {**meta, "status": "failed", "error": error}
