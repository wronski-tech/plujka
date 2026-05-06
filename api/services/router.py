from __future__ import annotations

import re

from api.services import db
from api.services.dynamic_sql import try_dynamic_query
from api.services.embeddings import embed_text
from api.services.llm import (
    _elected_sejm_candidate_pattern,
    extract_intent_and_entity,
    person_name_fragment_from_question,
)
from api.services.locations import districts_for_question
from api.services.sql_templates import SQL_TEMPLATES

INTENT_TEXT = {
    "total_votes_by_candidate": "pokaż sumę głosów dla wszystkich list",
    "votes_for_candidate": "ile głosów ma konkretna lista lub komitet",
    "votes_for_candidate_all_years": "głosy listy we wszystkich latach wyborów w bazie",
    "trend_by_district_for_candidate": "trend głosów po okręgach dla listy",
    "elected_candidates_sejm": "kandydaci którzy weszli do sejmu",
    "sejm_votes_by_powiat": "wyniki głosów na listy według powiatów",
    "sejm_votes_by_gmina": "wyniki głosów na listy według gmin",
    "sejm_votes_by_wojewodztwo": "wyniki głosów na listy według województw",
    "sejm_candidate_personal_votes": (
        "preferencyjne lub imienne głosy na kandydata według bazy zwycięzców mandatu"
    ),
    "candidate_sejm_participation": (
        "w których latach lub kadencjach wyborów startował kandydat do Sejmu według list z gmin"
    ),
}

_KBW_FALLBACK_STOPWORDS_RE = (
    r"(głos(y|ow)?\b|kart|frekw|uprawn|wydan|ważn|wazn|nieważ|niewaz|"
    r"obw(ó|o)d|okr(ę|e)g|numer|nr\b|powiat|gmina|wojew|teryt|siedziba|"
    r"komisj|wiek|mieszka|l\.\s*obwod)"
)


def route_question(question: str) -> dict:
    """Route question."""
    question_embedding = embed_text(question)
    llm_intent, llm_entity = extract_intent_and_entity(question)
    year = _extract_year(question)
    years = _extract_years(question)
    is_comparison = _is_year_comparison_question(question, years)
    default_year = year if year is not None else db.get_latest_election_year("sejm")

    intent = llm_intent if llm_intent in SQL_TEMPLATES else _fallback_semantic_intent(question_embedding)
    if intent == "candidate_sejm_participation":
        sql = SQL_TEMPLATES[intent]
        pattern = _sql_ilike_pattern(llm_entity) or _sql_ilike_pattern(
            person_name_fragment_from_question(question)
        )
        if not pattern:
            return {
                "question": question,
                "intent": intent,
                "entity": llm_entity,
                "year": None,
                "years": years,
                "sql": sql,
                "params": {"name_pattern": None},
                "result": [],
            }
        params = {"name_pattern": pattern}
        result = db.run_sql(sql, params)
        return {
            "question": question,
            "intent": intent,
            "entity": llm_entity,
            "year": None,
            "years": years,
            "sql": sql,
            "params": params,
            "result": result,
        }

    if intent == "sejm_candidate_personal_votes":
        sql = SQL_TEMPLATES[intent]
        personal_year = year if year is not None else db.get_latest_elected_candidates_year()
        pattern = _sql_ilike_pattern(llm_entity) or _sql_ilike_pattern(
            _elected_sejm_candidate_pattern(question)
        )
        if not pattern:
            return {
                "question": question,
                "intent": intent,
                "entity": llm_entity,
                "year": personal_year,
                "years": years,
                "sql": sql,
                "params": {"year": personal_year, "candidate_pattern": None},
                "result": [],
            }
        params = {"year": personal_year, "candidate_pattern": pattern}
        result = db.run_sql(sql, params)
        if not result and year is None and personal_year is not None:
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

    if intent in ("sejm_votes_by_powiat", "sejm_votes_by_gmina", "sejm_votes_by_wojewodztwo"):
        sql = SQL_TEMPLATES[intent]
        committee_pattern = f"%{llm_entity}%" if llm_entity else None
        place_key = {
            "sejm_votes_by_powiat": "powiat_pattern",
            "sejm_votes_by_gmina": "gmina_pattern",
            "sejm_votes_by_wojewodztwo": "wojewodztwo_pattern",
        }[intent]
        params = {
            "year": default_year,
            "committee_pattern": committee_pattern,
            place_key: _place_pattern_from_question(question, intent),
            "limit": 500,
        }
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

    if intent == "elected_candidates_sejm":
        sql = SQL_TEMPLATES["elected_candidates_sejm"]
        elected_default_year = year if year is not None else db.get_latest_elected_candidates_year()
        location_districts = districts_for_question(question)
        district_csv = ",".join(location_districts) if location_districts else None
        params = (
            {
                "year": elected_default_year,
                "candidate_pattern": _sql_ilike_pattern(llm_entity),
                "district_csv": district_csv,
            }
            if llm_entity
            else {
                "year": elected_default_year,
                "candidate_pattern": None,
                "district_csv": district_csv,
            }
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

    if intent in ("total_votes_by_candidate", "votes_for_candidate", "votes_for_candidate_all_years"):
        kbw_sql, kbw_params = _kbw_sejm_ranking_sql(
            year=None if intent == "votes_for_candidate_all_years" else default_year,
            limit=10 if intent == "total_votes_by_candidate" else (1 if intent == "votes_for_candidate" else 500),
            candidate_pattern=(
                _sql_ilike_pattern(llm_entity)
                if intent in ("votes_for_candidate", "votes_for_candidate_all_years")
                else None
            ),
            geo_pattern_folded=_kbw_geo_pattern_from_question(question),
            all_years=intent == "votes_for_candidate_all_years",
        )
        kbw_result = db.run_sql(kbw_sql, kbw_params)
        return {
            "question": question,
            "intent": intent,
            "entity": llm_entity,
            "year": None if intent == "votes_for_candidate_all_years" else default_year,
            "years": years,
            "sql": kbw_sql,
            "params": kbw_params,
            "result": kbw_result,
        }

    sql, params = _resolve_sql(intent, llm_entity, default_year, years, is_comparison)
    result = db.run_sql(sql, params)
    if _should_try_kbw_legacy_fallback(intent, result, default_year):
        kbw_sql, kbw_params = _kbw_legacy_sejm_sql(
            year=default_year,
            limit=int(params.get("limit", 10)),
            candidate_pattern=params.get("candidate_pattern"),
            geo_pattern_folded=_kbw_geo_pattern_from_question(question),
        )
        kbw_rows = db.run_sql(kbw_sql, kbw_params)
        if kbw_rows:
            return {
                "question": question,
                "intent": intent,
                "entity": llm_entity,
                "year": default_year,
                "years": years,
                "sql": kbw_sql,
                "params": kbw_params,
                "result": kbw_rows,
            }

    # Safe dynamic SQL fallback for broader KBW/legacy datasets or when template path returns empty.
    if _should_try_dynamic_sql(question, intent, result):
        dyn_rows, dyn_sql, dyn_err, dyn_meta = try_dynamic_query(question)
        if dyn_rows:
            return {
                "question": question,
                "intent": "dynamic_sql",
                "entity": llm_entity,
                "year": None,
                "years": years,
                "sql": dyn_sql,
                "params": {"_dynamic_sql_meta": dyn_meta},
                "result": dyn_rows,
            }
        # Surface dynamic generation telemetry while preserving deterministic fallback result.
        telemetry = {"_dynamic_sql_meta": dyn_meta}
        if dyn_err:
            telemetry["_dynamic_sql_error"] = dyn_err
        params = {**params, **telemetry}

    response_year = None if intent == "votes_for_candidate_all_years" else default_year
    return {
        "question": question,
        "intent": intent,
        "entity": llm_entity,
        "year": response_year,
        "years": years,
        "sql": sql,
        "params": params,
        "result": result,
    }


def _sql_ilike_pattern(entity: str | None) -> str | None:
    """Sql ilike pattern."""
    if entity is None:
        return None
    text = entity.strip()
    if not text:
        return None
    if text.startswith("%") and text.endswith("%"):
        return text
    return f"%{text}%"


def _fallback_semantic_intent(question_embedding: list[float]) -> str:
    """Fallback semantic intent."""
    scores: dict[str, float] = {}
    for intent_name, text in INTENT_TEXT.items():
        intent_embedding = embed_text(text)
        score = _cosine_similarity(question_embedding, intent_embedding)
        scores[intent_name] = score
    return max(scores, key=scores.get)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity."""
    length = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(length))


def _resolve_sql(
    intent: str,
    entity: str | None,
    year: int | None,
    years: list[int],
    is_comparison: bool,
) -> tuple[str, dict]:
    """Resolve sql."""
    if intent == "votes_for_candidate_all_years" and entity:
        return SQL_TEMPLATES["votes_for_candidate_all_years"], {
            "candidate_pattern": f"%{entity}%",
        }
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


def _should_try_kbw_legacy_fallback(intent: str, result: list[dict], year: int | None) -> bool:
    """Should try kbw legacy fallback."""
    if result:
        return False
    if year is None or year >= 2019:
        return False
    return intent in ("total_votes_by_candidate", "votes_for_candidate")


def _kbw_sejm_ranking_sql(
    *,
    year: int | None,
    limit: int,
    candidate_pattern: str | None,
    geo_pattern_folded: str | None,
    all_years: bool,
) -> tuple[str, dict]:
    """Kbw sejm ranking sql."""
    select_year = "er.year,"
    year_filter = ""
    order_clause = "ORDER BY er.year ASC, votes DESC" if all_years else "ORDER BY votes DESC"
    if not all_years:
        year_filter = "AND (%(year)s::int IS NULL OR er.year = %(year)s::int)"

    prefer_csv_sources = year is not None and year >= 2019 and not all_years

    sql = f"""
        SELECT
            {select_year}
            trim(f.subject->>'column') AS candidate,
            SUM(f.value)::bigint AS votes
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.family IN ('sejm', 'sejmsenat')
          {year_filter}
          AND f.is_percentage = FALSE
          AND COALESCE(f.subject->>'kind', '') = 'series'
          AND (df.rel_path ILIKE '%%sejm%%obw%%' OR df.rel_path ILIKE '%%po_obwodach%%')
          AND df.rel_path NOT ILIKE '%%proc%%'
          AND (%(prefer_csv_sources)s::bool = FALSE OR df.rel_path ILIKE '%%csv%%')
          AND COALESCE(f.subject->>'column', '') ~ '[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]'
          AND COALESCE(f.subject->>'column', '') !~* %(_stopwords)s
          AND lower(COALESCE(f.subject->>'column', '')) NOT LIKE 'nr obw%%'
          AND lower(COALESCE(f.subject->>'column', '')) NOT LIKE 'nr okr%%'
          AND lower(COALESCE(f.subject->>'column', '')) NOT LIKE 'nr woj%%'
          AND (%(candidate_pattern)s::text IS NULL OR f.subject->>'column' ILIKE %(candidate_pattern)s::text)
          AND (
            %(geo_pattern_folded)s::text IS NULL
            OR translate(lower(f.geography::text), 'ąćęłńóśźż', 'acelnoszz') ILIKE %(geo_pattern_folded)s::text
          )
        GROUP BY er.year, trim(f.subject->>'column')
        {order_clause}
        LIMIT %(limit)s::int
    """
    params = {
        "year": year,
        "limit": limit,
        "candidate_pattern": candidate_pattern,
        "geo_pattern_folded": geo_pattern_folded,
        "prefer_csv_sources": prefer_csv_sources,
        "_stopwords": _KBW_FALLBACK_STOPWORDS_RE,
    }
    return sql, params


def _kbw_legacy_sejm_sql(
    year: int,
    limit: int,
    candidate_pattern: str | None,
    geo_pattern_folded: str | None,
) -> tuple[str, dict]:
    """Kbw legacy sejm sql."""
    sql = f"""
        SELECT
            er.year,
            trim(f.subject->>'column') AS candidate,
            SUM(f.value)::bigint AS votes
        FROM kbw_facts f
        JOIN kbw_election_runs er ON er.id = f.election_run_id
        JOIN kbw_dane_files df ON df.id = f.source_file_id
        WHERE er.family IN ('sejm', 'sejmsenat')
          AND er.year = %(year)s::int
          AND f.is_percentage = FALSE
          AND COALESCE(f.subject->>'kind', '') = 'series'
          AND (df.rel_path ILIKE '%%sejm%%obw%%' OR df.rel_path ILIKE '%%po_obwodach%%')
          AND df.rel_path NOT ILIKE '%%proc%%'
          AND COALESCE(f.subject->>'column', '') ~ '[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]'
          AND COALESCE(f.subject->>'column', '') !~* %(_stopwords)s
          AND lower(COALESCE(f.subject->>'column', '')) NOT LIKE 'nr obw%%'
          AND lower(COALESCE(f.subject->>'column', '')) NOT LIKE 'nr okr%%'
          AND lower(COALESCE(f.subject->>'column', '')) NOT LIKE 'nr woj%%'
          AND (%(candidate_pattern)s::text IS NULL OR f.subject->>'column' ILIKE %(candidate_pattern)s::text)
          AND (
            %(geo_pattern_folded)s::text IS NULL
            OR translate(lower(f.geography::text), 'ąćęłńóśźż', 'acelnoszz') ILIKE %(geo_pattern_folded)s::text
          )
        GROUP BY er.year, trim(f.subject->>'column')
        ORDER BY votes DESC
        LIMIT %(limit)s::int
    """
    params = {
        "year": year,
        "limit": limit,
        "candidate_pattern": candidate_pattern,
        "geo_pattern_folded": geo_pattern_folded,
        "_stopwords": _KBW_FALLBACK_STOPWORDS_RE,
    }
    return sql, params


def _kbw_geo_pattern_from_question(question: str) -> str | None:
    """Kbw geo pattern from question."""
    q = question.strip()
    m = re.search(
        r"\bw\s+([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż][A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż\- ]{1,40})",
        q,
        re.IGNORECASE,
    )
    if not m:
        return None
    place = m.group(1).strip()
    place = re.split(r"\s+(?:w|z|na|roku|r\.)\b|\b(?:19|20)\d{2}\b", place, maxsplit=1)[0].strip()
    if len(place) < 3:
        return None
    return f"%{_fold_polish_text(place)}%"


def _fold_polish_text(text: str) -> str:
    """Fold polish text."""
    lowered = text.lower()
    table = str.maketrans(
        {
            "ą": "a",
            "ć": "c",
            "ę": "e",
            "ł": "l",
            "ń": "n",
            "ó": "o",
            "ś": "s",
            "ź": "z",
            "ż": "z",
        }
    )
    return lowered.translate(table)


def _extract_year(question: str) -> int | None:
    """Extract year."""
    match = re.search(r"\b((?:19|20)\d{2})\b", question)
    if not match:
        return None
    value = int(match.group(1))
    return value if 1900 <= value <= 2099 else None


def _extract_years(question: str) -> list[int]:
    """Extract years."""
    years = []
    for token in re.findall(r"\b((?:19|20)\d{2})\b", question):
        value = int(token)
        if 1900 <= value <= 2099 and value not in years:
            years.append(value)
    return years


def _place_pattern_from_question(question: str, intent: str) -> str | None:
    """Rough extraction of powiat/gmina/województwo name from Polish questions for ILIKE."""
    lowered = question.lower()
    patterns: list[str] = []
    if intent == "sejm_votes_by_powiat":
        patterns = [
            r"w\s+powiecie\s+([^\n,.;?!]{2,60})",
            r"powiatu\s+([^\n,.;?!]{2,60})",
            r"powiat(?:zie)?\s+([^\n,.;?!]{2,60})",
        ]
    elif intent == "sejm_votes_by_gmina":
        patterns = [
            r"w\s+gmin(?:ie|y)\s+([^\n,.;?!]{2,60})",
            r"gmin(?:y|a|ie)\s+([^\n,.;?!]{2,60})",
        ]
    else:
        patterns = [
            r"w\s+wojew(?:ó|o)dztw(?:ie|ach)\s+([^\n,.;?!]{2,60})",
            r"wojew(?:ó|o)dztwa\s+([^\n,.;?!]{2,60})",
            r"wojew(?:ó|o)dztw(?:ie|ach)?\s+([^\n,.;?!]{2,60})",
        ]
    for raw_pat in patterns:
        match = re.search(raw_pat, lowered)
        if match:
            chunk = match.group(1).strip()
            chunk = re.split(r"\s+(?:dla|w\s+roku|20\d{2})", chunk)[0].strip()
            if len(chunk) >= 2:
                return f"%{chunk}%"
    return None


def _is_year_comparison_question(question: str, years: list[int]) -> bool:
    """Is year comparison question."""
    lowered = question.lower()
    return len(years) >= 2 and (" vs " in lowered or "versus" in lowered or "porówn" in lowered)


def _should_try_dynamic_sql(question: str, intent: str, result: list[dict]) -> bool:
    """Should try dynamic sql."""
    lowered = question.lower()
    broad_tokens = (
        "prezydent",
        "referendum",
        "rw",
        "rejestr wyborcow",
        "rejestr wyborców",
        "samorzad",
        "samorząd",
        "parlament eu",
        "senat",
        "kbw",
    )
    asks_broad_domain = any(t in lowered for t in broad_tokens)
    # If template path has no rows or the question targets broader families, try dynamic query.
    if asks_broad_domain:
        return True
    if not result and intent in (
        "total_votes_by_candidate",
        "votes_for_candidate",
        "trend_by_district_for_candidate",
        "votes_for_candidate_all_years",
    ):
        return True
    return False
