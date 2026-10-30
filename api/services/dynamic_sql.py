"""LLM-generated SQL over the relational ``mart`` schema (fallback path of ``router.route_question``).

Flow for one question:
1. ``entities.resolve_entities`` maps inflected names in the question to exact keys (TERYT, person_id,
   committee name) found in ``mart.entity_lexicon``; ``narrow_matches`` drops matches the question
   rules out. If several people or same-name gminas remain, the caller gets a clarification request
   instead of a guess. Explicit choices (``entity_refs``) replace matches of their kind.
2. The prompt carries the mart schema description (table/column ``COMMENT``s read from PostgreSQL),
   the loaded elections, the entity matches, similar verified examples (``examples.py``) and rules.
3. The model returns one SELECT; ``safe_sql.validate_mart_sql`` checks it with sqlglot and caps LIMIT.
4. ``safe_sql.run_readonly`` runs it as the read-only mart role. On a database error the model gets
   one repair attempt with the failing SQL and the error message.

A validated SQL that ran successfully is cached per (question, entity choices, mart load) so a
repeated question skips the LLM. ``meta`` records status, timings and token usage for telemetry.

Answers therefore always come from SQL over official data; the LLM only writes the query.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from typing import Any

import psycopg
from openai import OpenAI

from api.services.config import (
    MART_DATABASE_URL,
    MART_MAX_ROWS,
    OPENAI_API_KEY,
    OPENAI_CHAT_MODEL,
    SQL_CACHE_MAX_ENTRIES,
    SQL_CACHE_TTL_SECONDS,
)
from api.services.mart import entities, examples, safe_sql

log = logging.getLogger(__name__)

SQL_RULES = f"""Rules:
- Write exactly one PostgreSQL SELECT (WITH ... SELECT allowed). No other statements.
- Use only relations of schema mart listed below; qualify them as mart.<name>.
- Prefer the v_* views: they already define percentages, thresholds, turnout and aggregation levels.
- Always filter by election year (and round for two-round elections); when the question gives no
  year, use the latest loaded election of that kind.
- Use the matched entities' keys (teryt_gmina, person_id, committee name) instead of guessing names.
  Committees: filter with '<alias>' = ANY(aliases). Names in questions are inflected Polish forms.
- Comparisons across elections: mart.v_turnout (turnout of every election), mart.v_party_results
  and mart.v_party_change (party families; return the comparable column so the answer can say
  when the committees differed). Join units by teryt_gmina / teryt_powiat / wojewodztwo.
- Seats under another method or threshold ("co gdyby"): mart.v_seat_methods (Sejm, sejmik), or
  mart.allocate_seats(array_agg(votes ORDER BY committee_id), <district seats>, 'dhondt' | 'sainte_lague' |
  'sainte_lague_modified') per district over the lists that pass the chosen threshold, then sum.
- Percentages in the mart are already 0-100. Order results deterministically.
- Include LIMIT {MART_MAX_ROWS} or less.
Return JSON: {{"sql": "..."}}"""


class _SqlCache:
    """Small thread-safe LRU of validated SQL with a time-to-live."""

    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._data: OrderedDict[tuple, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> str | None:
        """Cached SQL for ``key`` or None (expired entries are dropped)."""
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if hit[0] < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return hit[1]

    def put(self, key: tuple, sql: str) -> None:
        """Store ``sql`` (no-op when the cache is disabled)."""
        if self.ttl <= 0 or self.max_entries <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, sql)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def drop(self, key: tuple) -> None:
        """Forget ``key``."""
        with self._lock:
            self._data.pop(key, None)


SQL_CACHE = _SqlCache(SQL_CACHE_MAX_ENTRIES, SQL_CACHE_TTL_SECONDS)


@lru_cache(maxsize=2)
def schema_description(version: str = "", compact: bool = False) -> dict[str, str]:
    """Prompt text per mart relation: table/column comments read from PostgreSQL.

    With ``compact``, base tables (not v_* views, not SHARED_RELATIONS) list only their comment and
    column names; used when the question names no election kind and the whole schema is sent.
    Cached per mart load ``version`` (from ``data_context``), so a rebuilt mart is picked up
    without restarting the API.
    """
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
    if not compact:
        return {name: f"mart.{name} -- {doc}\n" + "\n".join(cols) for name, (doc, cols) in relations.items()}
    return {
        name: f"mart.{name} -- {doc}\n" + (
            "\n".join(cols) if name.startswith("v_") or name in SHARED_RELATIONS
            else "  columns: " + ", ".join(c.split()[0] for c in cols)
        )
        for name, (doc, cols) in relations.items()
    }


# Words that name an election kind (folded, no diacritics) -> relation name prefixes of that kind.
# A question naming at least one kind gets only those relations plus SHARED_RELATIONS in the
# prompt; a question naming none gets the whole schema.
FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    r"\bsejm(?!ik)|posel|posl|poslank|parlament(?!\w*\s+europ)": ("sejm_", "v_sejm_", "precincts", "v_candidate_"),
    r"senat|senac|parlament(?!\w*\s+europ)": ("senat_", "precincts", "v_candidate_"),
    r"prezyden": ("prezydent_", "v_prezydent_", "wbp_"),
    r"europ|\bpe\b": ("pe_", "v_pe_", "v_candidate_"),
    r"referend": ("referendum_", "v_referendum_"),
    r"sejmik|wojewodzk": ("sejmik_", "v_sejmik_"),
    r"samorzad": ("sejmik_", "v_sejmik_", "wbp_", "rada_", "v_rada_"),
    r"wojt|burmistrz|mayor": ("wbp_",),
    r"\brad(a|y|e|zie|ach|ami)\b|radn": ("rada_", "v_rada_"),
    r"hondt|sainte|lague|metod|podzial\w* mandat|gdyby": ("v_seat_", "sejm_district", "sejmik_district", "rada_district"),
    # GUS statistics, not an election. Not "mieszka" (candidates' residence), not "miejsk" alone ("rada miejska").
    r"\bgus\b|ludnosc|mieszkanc|bezrobo|wynagrodz|zarob|pensj|gestosc|zaludn|powierzchn|wiejsk|\bwsi\b"
    r"|gmin\w* miejsk|miejsk\w* gmin|\bw miastach\b": ("gus_", "v_gus_"),
    # The 1990s results use the old gmina codes; the key maps them to today's units.
    # Single polling stations: a plain view next to the election's own relations.
    r"obwod|lokal\w* wyborcz|komisj\w* obwodow": ("v_precinct_",),
    r"\b199[0-7]\b|sprzed 1999|dawn\w* (wojewodz|gmin)|star\w* wojewodz|49 wojewodz": ("gmina_codes_pre1999",),
}
# Groups that only add relations: GUS statistics and the pre-1999 gmina key are used with some election, so a question
# naming no election kind besides them ("Nawrocki w gminach wiejskich") still gets the whole schema.
ADD_ONLY_PREFIXES = ("gus_", "v_gus_", "gmina_codes_pre1999", "v_precinct_")
SHARED_RELATIONS = (
    "elections", "persons", "v_person_history", "v_turnout", "v_party_results", "v_party_change", "v_gmina_continuity",
    "party_families",
)


def relevant_relations(question: str, available: list[str]) -> list[str]:
    """Relations to describe in the prompt for ``question`` (all of them when no kind is named)."""
    folded = entities.fold(question)
    prefixes = [p for pattern, group in FAMILY_KEYWORDS.items() if re.search(pattern, folded) for p in group]
    if not set(prefixes) - set(ADD_ONLY_PREFIXES):
        return list(available)
    return [r for r in available if r in SHARED_RELATIONS or r.startswith(tuple(prefixes))]


def data_context() -> tuple[str, str]:
    """Loaded elections, GUS years and the main Sejm committees, plus a version string of the last mart load."""
    with psycopg.connect(MART_DATABASE_URL) as conn:
        elections = conn.execute(
            "SELECT election, year, round, description, loaded_at FROM mart.elections ORDER BY election, year, round"
        ).fetchall()
        committees = conn.execute(
            """
            SELECT year, string_agg(coalesce(aliases[1], committee_name) || ' (seats ' || seats || ')', ', ' ORDER BY votes DESC)
            FROM mart.v_sejm_national_results WHERE pct >= 1 GROUP BY year ORDER BY year
            """
        ).fetchall()
        # GUS statistics write no mart.elections row; their years (primary-key index, no scan) join the version.
        gus = None
        if conn.execute("SELECT to_regclass('mart.gus_indicators')").fetchone()[0]:
            gus = conn.execute("SELECT min(year), max(year) FROM mart.gus_indicators").fetchone()
    has_gus = bool(gus and gus[0])
    version = max((str(e[4]) for e in elections), default="") + (f"|gus {gus[0]}-{gus[1]}" if has_gus else "")
    lines = [f"- {e[0]} {e[1]}" + (f" round {e[2]}" if e[2] else "") + f": {e[3]}" for e in elections]
    sejm = [f"- {year}: {names}" for year, names in committees]
    text = "Loaded elections (election, year, round):\n" + "\n".join(lines)
    if has_gus:
        text += f"\n\nGUS statistics (not elections, mart.gus_* / mart.v_gus_units): years {gus[0]}-{gus[1]}"
    text += "\n\nMain Sejm committees per year:\n" + "\n".join(sejm)
    return text, version


def _llm_sql(
    question: str, context: str, previous_sql: str | None = None, previous_error: str | None = None
) -> tuple[str, dict[str, int]]:
    """Ask the chat model for one SQL query; return (sql, token usage). Raise ValueError when unusable."""
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
    usage = response.usage
    tokens = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else {}
    try:
        sql = (json.loads(response.choices[0].message.content or "{}").get("sql") or "").strip()
    except json.JSONDecodeError as exc:
        raise ValueError("Model response was not valid JSON.") from exc
    if not sql:
        raise ValueError("Model returned no SQL.")
    return sql, tokens


def resolve_question_entities(
    question: str, entity_refs: list[dict] | None = None, previous: dict | None = None
) -> tuple[list[entities.EntityMatch], dict | None]:
    """Entity matches for the prompt and an optional clarification request.

    Kinds chosen explicitly in ``entity_refs`` replace the automatic matches of that kind and are
    never reported as ambiguous. In a follow-up (``previous`` turn), kinds the new question does
    not mention are inherited from the previous question ("a w 2019?" keeps the person or gmina).
    """
    matches = entities.narrow_matches(question, entities.resolve_entities(question))
    clarification = None if entity_refs else entities.find_ambiguity(matches)
    if entity_refs:
        pinned = entities.lookup_refs(entity_refs)
        pinned_kinds = {m.kind for m in pinned}
        matches = [m for m in matches if m.kind not in pinned_kinds] + pinned
    if previous and not clarification:
        earlier = entities.narrow_matches(previous["question"], entities.resolve_entities(previous["question"]))
        if previous.get("entity_refs"):
            pinned = entities.lookup_refs(previous["entity_refs"])
            earlier = [m for m in earlier if m.kind not in {p.kind for p in pinned}] + pinned
        present = {m.kind for m in matches}
        matches += [m for m in earlier if m.kind not in present]
    return matches, clarification


def build_context(
    question: str, matches: list[entities.EntityMatch], previous: dict | None = None
) -> tuple[str, str, dict[str, Any]]:
    """Prompt context, mart version, and what went into the prompt (for telemetry)."""
    hints = entities.format_entity_hints(matches) or "- none"
    elections, version = data_context()
    described = schema_description(version)
    scope = f"{previous['question']} {question}" if previous else question
    relations = relevant_relations(scope, list(described))
    if len(relations) == len(described):  # no election kind named: whole schema, base tables compact
        described = schema_description(version, compact=True)
    similar = examples.similar_examples(question)
    context = (
        "Schema:\n" + "\n\n".join(described[r] for r in relations)
        + f"\n\n{elections}\n\nEntities matched in the question:\n{hints}"
        + (f"\n\n{examples.format_examples(similar)}" if similar else "")
    )
    if previous:
        context += (
            "\n\nThis question follows up the previous one in the conversation; keep its filters "
            f"(election, year, place, person, party) unless the new question changes them.\n"
            f"Previous question: {previous['question']}\nPrevious SQL: {previous.get('sql') or '-'}"
        )
    used = {
        "examples": [{"question": e["question"], "source": e.get("source")} for e in similar],
        "schema_relations": len(relations),
        "prompt_chars": len(context),
    }
    return context, version, used


def _ms(started: float) -> int:
    """Milliseconds since ``started`` (time.monotonic)."""
    return int((time.monotonic() - started) * 1000)


def try_dynamic_query(
    question: str, entity_refs: list[dict] | None = None, previous: dict | None = None
) -> tuple[list[dict], str | None, str | None, dict]:
    """Generate, validate and run SQL for ``question``.

    Returns ``(rows, sql, error, meta)``. ``meta["status"]`` is one of skipped / clarify / rejected /
    failed / cached / first_pass / repaired. ``meta`` also holds ``entities``, ``examples``,
    ``timings_ms``, ``tokens``, ``schema_relations`` and ``prompt_chars``; for ``clarify`` it holds
    ``clarification`` (see ``entities.find_ambiguity``). ``previous`` is the preceding turn of the
    conversation (``{"question", "sql", "entity_refs"}``) for follow-up questions.
    """
    timings: dict[str, int] = {}
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    started = time.monotonic()
    try:
        matches, clarification = resolve_question_entities(question, entity_refs, previous)
        meta: dict[str, Any] = {"entities": [m.__dict__ for m in matches], "timings_ms": timings, "tokens": tokens}
        if clarification:
            timings["entities"] = _ms(started)
            return [], None, None, {**meta, "status": "clarify", "clarification": clarification}
        context, version, used = build_context(question, matches, previous)
    except psycopg.Error as exc:
        log.warning("dynamic SQL skipped: mart not reachable (%s)", exc)
        return [], None, str(exc), {"status": "skipped", "phase": "context", "error": str(exc)}
    timings["context"] = _ms(started)
    meta.update(used)

    turn = json.dumps({k: previous.get(k) for k in ("question", "sql")}, sort_keys=True) if previous else ""
    cache_key = (" ".join(entities.fold(question).split()), json.dumps(entity_refs or [], sort_keys=True), turn, version)
    cached = SQL_CACHE.get(cache_key)
    if cached:
        try:
            phase = time.monotonic()
            rows = safe_sql.run_readonly(cached)
            timings["execute"] = _ms(phase)
            return rows, cached, None, {**meta, "status": "cached"}
        except psycopg.Error:
            SQL_CACHE.drop(cache_key)

    sql: str | None = None
    error: str | None = None
    for attempt in ("first_pass", "repaired"):
        try:
            phase = time.monotonic()
            raw, used = _llm_sql(question, context, previous_sql=sql, previous_error=error)
            timings[f"llm_{attempt}"] = _ms(phase)
            for name, value in used.items():
                tokens[name] = tokens.get(name, 0) + value
            sql = safe_sql.validate_mart_sql(raw)
        except safe_sql.UnsafeSqlError as exc:
            log.warning("dynamic SQL rejected: %s", exc)
            return [], None, str(exc), {**meta, "status": "rejected", "phase": attempt, "error": str(exc)}
        except ValueError as exc:
            return [], sql, str(exc), {**meta, "status": "skipped", "phase": attempt, "error": str(exc)}
        try:
            phase = time.monotonic()
            rows = safe_sql.run_readonly(sql)
            timings[f"execute_{attempt}"] = _ms(phase)
            SQL_CACHE.put(cache_key, sql)
            return rows, sql, None, {**meta, "status": attempt}
        except psycopg.Error as exc:
            error = str(exc).splitlines()[0]
            log.info("dynamic SQL failed on attempt %s: %s", attempt, error)
    return [], sql, error, {**meta, "status": "failed", "error": error}
