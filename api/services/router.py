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
    "elected_candidates_sejm": "kandydaci którzy weszli do sejmu",
}


def route_question(question: str) -> dict:
    question_embedding = embed_text(question)
    llm_intent, llm_entity = extract_intent_and_entity(question)
    year = _extract_year(question)
    years = _extract_years(question)
    is_comparison = _is_year_comparison_question(question, years)
    default_year = year if year is not None else db.get_latest_election_year("sejm")

    intent = llm_intent if llm_intent in SQL_TEMPLATES else _fallback_semantic_intent(question_embedding)
    if intent == "elected_candidates_sejm":
        sql = SQL_TEMPLATES["elected_candidates_sejm"]
        elected_default_year = year if year is not None else db.get_latest_elected_candidates_year()
        params = (
            {"year": elected_default_year, "candidate_pattern": f"%{llm_entity}%"}
            if llm_entity
            else {"year": elected_default_year, "candidate_pattern": None}
        )
        result = db.run_sql(sql, params)
        if not result and year is None and elected_default_year is not None:
            params["year"] = None
            result = db.run_sql(sql, params)
        return {
            "question": question,
            "intent": intent,
            "entity": llm_entity,
            "year": params["year"],
            "years": years,
            "sql": sql,
            "params": params,
            "result": result,
        }

    sql, params = _resolve_sql(intent, llm_entity, default_year, years, is_comparison)
    result = db.run_sql(sql, params)
    return {
        "question": question,
        "intent": intent,
        "entity": llm_entity,
        "year": default_year,
        "years": years,
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


def _resolve_sql(
    intent: str,
    entity: str | None,
    year: int | None,
    years: list[int],
    is_comparison: bool,
) -> tuple[str, dict]:
    if intent == "votes_for_candidate" and entity:
        if is_comparison and len(years) >= 2:
            y1, y2 = sorted(years)[:2]
            return SQL_TEMPLATES["votes_for_candidate_compare_years"], {
                "candidate_pattern": f"%{entity}%",
                "year_1": y1,
                "year_2": y2,
            }
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


def _extract_years(question: str) -> list[int]:
    years = []
    for token in re.findall(r"\b(20\d{2})\b", question):
        value = int(token)
        if 2000 <= value <= 2099 and value not in years:
            years.append(value)
    return years


def _is_year_comparison_question(question: str, years: list[int]) -> bool:
    lowered = question.lower()
    return len(years) >= 2 and (" vs " in lowered or "versus" in lowered or "porówn" in lowered)
