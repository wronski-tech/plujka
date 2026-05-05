from __future__ import annotations

import json
import re

from openai import OpenAI

from api.services.config import OPENAI_API_KEY, OPENAI_CHAT_MODEL

COMMITTEE_ALIASES = {
    "ko": "KOALICJA OBYWATELSKA",
    "koalicja obywatelska": "KOALICJA OBYWATELSKA",
    "pis": "PRAWO I SPRAWIEDLIWOŚĆ",
    "prawo i sprawiedliwość": "PRAWO I SPRAWIEDLIWOŚĆ",
    "konfederacja": "KONFEDERACJA",
    "lewica": "NOWA LEWICA",
    "trzecia droga": "TRZECIA DROGA",
}

# Osoba publiczna → fragment nazwy komitetu (jak w `candidates.name` / CSV list). To nie są głosy imienne.
_POLITICIAN_COMMITTEE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdonald\s+tusk\b", re.IGNORECASE), "KOALICJA OBYWATELSKA"),
    (re.compile(r"\btusk\b", re.IGNORECASE), "KOALICJA OBYWATELSKA"),
    (re.compile(r"\bmateusz\s+morawiecki\b", re.IGNORECASE), "PRAWO I SPRAWIEDLIWOŚĆ"),
    (re.compile(r"\bmorawiecki\b", re.IGNORECASE), "PRAWO I SPRAWIEDLIWOŚĆ"),
    (re.compile(r"\bjarosław\s+kaczy(?:ń|n)ski\b", re.IGNORECASE), "PRAWO I SPRAWIEDLIWOŚĆ"),
    (re.compile(r"\bkaczy(?:ń|n)ski\b", re.IGNORECASE), "PRAWO I SPRAWIEDLIWOŚĆ"),
]


def extract_intent_and_entity(question: str) -> tuple[str, str | None]:
    lowered = question.lower()

    if any(
        token in lowered
        for token in [
            "wesz",
            "mandat",
            "weszli do sejmu",
            "wybrani do sejmu",
            "posł",
            "posl",
            "poseł",
            "poslowie",
            "posłowie",
        ]
    ):
        alias = _extract_alias_from_question(question)
        return "elected_candidates_sejm", alias

    committee = _committee_from_politician_name(question)
    if committee and _question_asks_vote_totals(question):
        if _question_asks_multiple_election_years(question):
            return "votes_for_candidate_all_years", committee
        return "votes_for_candidate", committee

    if not OPENAI_API_KEY:
        if "trend" in lowered or "okręg" in lowered or "district" in lowered:
            entity = _extract_after_keyword(question, ["dla", "for"])
            entity = _normalize_entity(entity, question)
            return "trend_by_district_for_candidate", entity
        if "ile" in lowered or "votes" in lowered or "głos" in lowered:
            entity = _extract_after_keyword(question, ["dla", "for", "na"])
            entity = _normalize_entity(entity, question)
            if entity:
                return "votes_for_candidate", entity
            alias = _extract_alias_from_question(question)
            if alias:
                return "votes_for_candidate", alias
        return "total_votes_by_candidate", None

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = (
        "You are an intent extractor for a deterministic SQL system. "
        "Return JSON only with keys: intent, entity. "
        "Allowed intents: total_votes_by_candidate, votes_for_candidate, votes_for_candidate_all_years, "
        "trend_by_district_for_candidate, elected_candidates_sejm."
    )
    response = client.chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ],
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    intent = payload.get("intent", "total_votes_by_candidate")
    entity = _normalize_entity(payload.get("entity"), question)
    if intent == "votes_for_candidate" and not entity:
        entity = _extract_alias_from_question(question)
    return intent, entity


def _extract_after_keyword(text: str, keywords: list[str]) -> str | None:
    lowered = text.lower()
    for keyword in keywords:
        pattern = rf"{keyword}\s+(.+)$"
        match = re.search(pattern, lowered)
        if match:
            return text[match.start(1) :].strip(" ?.")
    return None


def _committee_from_politician_name(question: str) -> str | None:
    for pattern, committee in _POLITICIAN_COMMITTEE_PATTERNS:
        if pattern.search(question):
            return committee
    return None


def _question_asks_vote_totals(question: str) -> bool:
    lowered = question.lower()
    if re.search(r"\bile\b", lowered):
        return True
    return any(
        tok in lowered
        for tok in (
            "wynik",
            "głos",
            "glos",
            "poparc",
            "procent",
            "wybor",
            "elekcj",
            "mandat",
            "dostał",
            "dostal",
            "zdobył",
            "zdobyl",
        )
    )


def _question_asks_multiple_election_years(question: str) -> bool:
    lowered = question.lower()
    if len(re.findall(r"\b20\d{2}\b", lowered)) >= 2:
        return True
    return any(
        p in lowered
        for p in (
            "kolejn",
            "wszystkich wybor",
            "wszystkie wybory",
            "wszystkie lata",
            "każde wybory",
            "kazde wybory",
            "różnych lat",
            "roznych lat",
            "na przestrzeni",
            "z lat ",
            "z roku na rok",
        )
    )


def _extract_alias_from_question(question: str) -> str | None:
    lowered = question.lower()
    for alias, canonical in COMMITTEE_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return canonical
    return None


def _normalize_entity(entity: str | None, question: str) -> str | None:
    if entity:
        lowered = entity.lower()
        for alias, canonical in COMMITTEE_ALIASES.items():
            if alias in lowered:
                return canonical
        return entity.strip()
    return _extract_alias_from_question(question)
