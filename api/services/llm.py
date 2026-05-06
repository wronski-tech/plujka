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


def _elected_sejm_candidate_pattern(question: str) -> str | None:
    """Match known politicians to a substring for ILIKE on elected_candidates.candidate_name."""
    if re.search(r"\bdonald\s+tusk\b", question, re.IGNORECASE) or re.search(
        r"\btusk\b", question, re.IGNORECASE
    ):
        return "Tusk"
    if re.search(r"\bmateusz\s+morawiecki\b", question, re.IGNORECASE) or re.search(
        r"\bmorawiecki\b", question, re.IGNORECASE
    ):
        return "Morawiecki"
    if re.search(r"\bjarosław\s+kaczy", question, re.IGNORECASE) or re.search(
        r"\bkaczy(?:ń|n)ski\b", question, re.IGNORECASE
    ):
        return "Kaczyński"
    return None


_PARTICIPATION_STOPWORDS = frozenset(
    {
        "wszystkie",
        "wszystkich",
        "jakie",
        "które",
        "ktore",
        "których",
        "ktorych",
        "latach",
        "lat",
        "wyborach",
        "wybory",
        "kadencje",
        "kadencji",
        "kadencyjnych",
        "sejm",
        "sejmu",
        "pytanie",
        "podaj",
        "wskaż",
        "wskaz",
        "wskaże",
        "startował",
        "startowal",
        "startowała",
        "startowala",
        "kandydował",
        "kandydowal",
        "kandydowała",
        "kandydowala",
        "osoby",
        "osoba",
        "jakich",
        "which",
        "what",
        "elections",
    }
)


def _question_asks_candidate_participation(question: str) -> bool:
    """Pytania o lata/kadencje wyborów, w których ktoś był kandydatem do Sejmu."""
    lowered = question.lower()
    return any(
        p in lowered
        for p in (
            "kadencj",
            "startował",
            "startowal",
            "startowała",
            "startowala",
            "kandydował",
            "kandydowal",
            "kandydowała",
            "kandydowala",
            "w których latach",
            "których wyborach",
            "ktorych wyborach",
            "które wybory",
            "ktore wybory",
            "w jakich latach",
            "jakie lata",
            "jakie roczniki",
            "which years",
            "election years",
        )
    )


def person_name_fragment_from_question(question: str) -> str | None:
    """Bare substring for ILIKE on candidate_name (router wraps with %)."""
    m = re.search(
        r"(?:startował|startowal|startowała|startowala|kandydował|kandydowal|kandydowała|kandydowala)\s+"
        r"([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż][^\s,.;:?]*(?:\s+[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż][^\s,.;:?]*)?)",
        question,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    tokens = re.findall(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{4,}", question)
    for t in sorted(tokens, key=len, reverse=True):
        if t.lower() not in _PARTICIPATION_STOPWORDS and len(t) >= 5:
            return t
    for t in sorted(tokens, key=len, reverse=True):
        if t.lower() not in _PARTICIPATION_STOPWORDS and len(t) >= 4:
            return t
    return None


def _question_asks_personal_candidate_votes(question: str) -> bool:
    """Prefer routing to imienne / preferencyjne głosy kandydata (elected_candidates), nie sumy na listę."""
    lowered = question.lower()
    return any(
        phrase in lowered
        for phrase in (
            "preferencyjn",
            "głosów imiennych",
            "glosow imiennych",
            "głosy imienne",
            "glosy imienne",
            "osobistych głos",
            "osobiste głos",
            "osobiście",
            "osobiscie",
            "na nazwisko",
            "jako kandydat",
            "jako kandydaci",
            "na kandydata",
            "oddanych na",
            "oddane na",
            "na tuska",
            "na morawieckiego",
            "na kaczyńskiego",
            "na kaczynskiego",
            "ile zebrał",
            "ile zebral",
            "zdobył osobiście",
            "zdobyl osobiscie",
            "poparcie osobiste",
        )
    )


def extract_intent_and_entity(question: str) -> tuple[str, str | None]:
    """Extract intent and entity."""
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
        person = _elected_sejm_candidate_pattern(question)
        if person:
            return "elected_candidates_sejm", person
        alias = _extract_alias_from_question(question)
        return "elected_candidates_sejm", alias

    if _question_asks_candidate_participation(question):
        frag = person_name_fragment_from_question(question)
        if frag:
            return "candidate_sejm_participation", frag

    committee = _committee_from_politician_name(question)

    if _question_asks_vote_totals(question):
        person = _elected_sejm_candidate_pattern(question)
        if person and _question_asks_personal_candidate_votes(question):
            return "sejm_candidate_personal_votes", person

    if _question_asks_vote_totals(question):
        if any(t in lowered for t in ("gmina", "gminie", "gminy")):
            ent = committee or _extract_alias_from_question(question) or _normalize_entity(None, question)
            return "sejm_votes_by_gmina", ent
        if any(
            t in lowered
            for t in (
                "województwo",
                "województwie",
                "województwach",
                "wojewodztwo",
                "wojewodztwie",
                "wojewodztwach",
            )
        ):
            ent = committee or _extract_alias_from_question(question) or _normalize_entity(None, question)
            return "sejm_votes_by_wojewodztwo", ent
        if "powiat" in lowered:
            ent = committee or _extract_alias_from_question(question) or _normalize_entity(None, question)
            return "sejm_votes_by_powiat", ent

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
        "trend_by_district_for_candidate, elected_candidates_sejm, candidate_sejm_participation, "
        "sejm_candidate_personal_votes, sejm_votes_by_powiat, sejm_votes_by_gmina, sejm_votes_by_wojewodztwo."
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
    """Extract after keyword."""
    lowered = text.lower()
    for keyword in keywords:
        pattern = rf"{keyword}\s+(.+)$"
        match = re.search(pattern, lowered)
        if match:
            return text[match.start(1) :].strip(" ?.")
    return None


def _committee_from_politician_name(question: str) -> str | None:
    """Committee from politician name."""
    for pattern, committee in _POLITICIAN_COMMITTEE_PATTERNS:
        if pattern.search(question):
            return committee
    return None


def _question_asks_vote_totals(question: str) -> bool:
    """Question asks vote totals."""
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
    """Question asks multiple election years."""
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
    """Extract alias from question."""
    lowered = question.lower()
    for alias, canonical in COMMITTEE_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return canonical
    return None


def _normalize_entity(entity: str | None, question: str) -> str | None:
    """Normalize entity."""
    if entity:
        lowered = entity.lower()
        for alias, canonical in COMMITTEE_ALIASES.items():
            if alias in lowered:
                return canonical
        return entity.strip()
    return _extract_alias_from_question(question)
