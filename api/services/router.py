from __future__ import annotations

import re

from api.services import db
from api.services.embeddings import embed_text
from api.services.llm import extract_intent_and_entity
from api.services.sql_templates import SQL_TEMPLATES

INTENT_TEXT = {
    "total_votes_by_candidate": "pokaż sumę głosów dla wszystkich list",
    "votes_for_candidate": "ile głosów ma konkretna lista lub komitet",
    "trend_by_district_for_candidate": "trend głosów po okręgach dla listy",
}


def route_question(question: str) -> dict:
    question_embedding = embed_text(question)
    llm_intent, llm_entity = extract_intent_and_entity(question)
    year = _extract_year(question)

    intent = llm_intent if llm_intent in SQL_TEMPLATES else _fallback_semantic_intent(question_embedding)
    sql, params = _resolve_sql(intent, llm_entity, year)
    result = db.run_sql(sql, params)
    return {
        "question": question,
        "intent": intent,
        "entity": llm_entity,
        "year": year,
        "sql": sql,
        "params": params,
        "result": result,
    }


def _fallback_semantic_intent(question_embedding: list[float]) -> str:
    scores: dict[str, float] = {}
    for intent_name, text in INTENT_TEXT.items():
        intent_embedding = embed_text(text)
        score = _cosine_similarity(question_embedding, intent_embedding)
        scores[intent_name] = score
    return max(scores, key=scores.get)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    length = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(length))


def _resolve_sql(intent: str, entity: str | None, year: int | None) -> tuple[str, dict]:
    if intent == "votes_for_candidate" and entity:
        return SQL_TEMPLATES[intent], {"candidate_pattern": f"%{entity}%", "limit": 1, "year": year}
    if intent == "trend_by_district_for_candidate" and entity:
        return SQL_TEMPLATES[intent], {"candidate_pattern": f"%{entity}%", "limit": 10, "year": year}
    return SQL_TEMPLATES["total_votes_by_candidate"], {"limit": 10, "year": year}


def _extract_year(question: str) -> int | None:
    match = re.search(r"\b(20\d{2})\b", question)
    if not match:
        return None
    value = int(match.group(1))
    return value if 2000 <= value <= 2099 else None
