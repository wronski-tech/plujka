"""Definition questions answered from ``docs/DEFINITIONS.md``, never from the model's own knowledge.

A question that asks what a term means ("Co to jest frekwencja?", "What is the D'Hondt method?") and
names no year gets the curated text of every section whose ``match`` pattern it contains. Anything else,
including a definition question about a term the file does not cover, goes on to SQL.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from api.services.answer import is_english
from api.services.mart.entities import fold

DEFINITIONS_PATH = Path(__file__).resolve().parents[2] / "docs" / "DEFINITIONS.md"
DOC = "docs/DEFINITIONS.md"
MAX_DEFINITIONS = 3

# Asking for a meaning or a rule, in Polish or English (folded text).
_DEFINITION_QUESTION_RE = re.compile(
    r"\bco to (jest|sa|znaczy|oznacza)\b|\bczym (jest|sa|sie rozni\w*|rozni\w* sie)\b|\bco (oznacza|znaczy)\b|"
    r"\bjak (sie )?(liczy|oblicza|wylicza|dziala|wyznacza)\b|\bjak (jest |sa )?(liczon|obliczan|wyznaczan)\w*|"
    r"\bna czym polega|\bdefinicj|\bwyjasnij|\bkiedy\b.*\b(wazn|wiazac)|"
    # "What is ... in Warsaw" asks for data, not a definition.
    r"\bwhat (is|are|does)\b(?!.*\bin\b)|\bhow (is|are|does)\b.*\b(calculated|computed|counted|work)|\bdefin|\bexplain\b|"
    r"\bwhat makes\b|\bwhen is\b.*\b(valid|binding)\b"
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@lru_cache(maxsize=1)
def load_definitions(path: Path = DEFINITIONS_PATH) -> list[dict[str, Any]]:
    """The ``##`` sections of the file: ``{"title", "anchor", "pattern", "pl", "en"}``."""
    out = []
    for section in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.MULTILINE)[1:]:
        title, _, body = section.partition("\n")
        match = re.search(r"<!-- match: (.+?) -->", body)
        if not match:
            raise ValueError(f"{path.name}: section {title!r} has no match pattern")
        text = body[match.end():]
        pl, _, en = text.partition("**EN:**")
        out.append({
            "title": title.strip(),
            "anchor": re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-"),
            "pattern": re.compile(match.group(1)),
            "pl": " ".join(pl.split()),
            "en": " ".join(en.split()),
        })
    return out


def find_definitions(question: str) -> list[dict[str, Any]]:
    """Sections that answer ``question`` (empty unless it asks for a definition and names no year)."""
    folded = fold(question)
    if _YEAR_RE.search(question) or not _DEFINITION_QUESTION_RE.search(folded):
        return []
    return [d for d in load_definitions() if d["pattern"].search(folded)][:MAX_DEFINITIONS]


def definition_answer(question: str, found: list[dict[str, Any]]) -> dict[str, Any]:
    """Answer payload (same shape as ``answer.compose_answer``) with the curated texts and their sources."""
    language = "en" if is_english(question) else "pl"
    # Section titles are Polish; English answers show the text alone.
    text = "\n\n".join(d["en"] if language == "en" and d["en"] else f"**{d['title']}.** {d['pl']}" for d in found)
    return {
        "text": text,
        "source": "docs",
        "verified": True,
        "caveats": [],
        "sources": [f"{DOC}#{d['anchor']}" for d in found],
    }
