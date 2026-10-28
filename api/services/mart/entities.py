"""Entity resolution for questions, run before SQL generation (runtime, psycopg only).

Questions name places, people and parties in inflected Polish ("w Krakowie", "Kaczyńskiego",
"na PiS"), while the database stores nominative labels ("m. Kraków", "KACZYŃSKI", aliases {PiS}).
``resolve_entities`` finds the matching rows of ``mart.entity_lexicon`` so the SQL generator gets
exact keys (TERYT, person_id, committee name) instead of guessing ILIKE patterns.

Matching is deliberately simple and explainable:
- every question word is folded (lower case, no diacritics) and reduced to candidate stems by
  stripping one common case ending ("krakowie" -> "krakow", "kaczynskiego" -> "kaczynsk");
- a lexicon name matches when each of its words starts with one of those stems and the stem covers
  most of the word (so "now" matches "nowy" but not "nowak" or "nowogard");
- committee aliases ("pis", "ko", "td") must match a question word exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

import psycopg

from api.services.config import MART_DATABASE_URL

_FOLD = str.maketrans("ąćęłńóśźż", "acelnoszz")

# Longest first: the first ending that leaves a stem of at least 3 letters is stripped.
_ENDINGS = (
    "owie", "iego", "ach", "ami", "ego", "emu", "owi", "iem", "ich", "ym", "im", "em", "iu", "ie", "ow",
    "om", "a", "u", "y", "i", "e",
)

# Frequent question words that would otherwise prefix-match place or person names.
_STOPWORDS = frozenset(
    """
    ile ilu kto gdzie jaki jaka jakie ktory ktora ktore ktorych ktorej najwiecej najmniej najwyzsza najnizsza
    wybory wyborach wyborow sejm sejmu senat senatu gmina gminie gminy powiat powiecie powiatu okreg okregu
    wojewodztwo wojewodztwie glosow glosy glos procent poparcie frekwencja frekwencji kandydat kandydaci
    kandydatow partia partii komitet komitetu lista listy roku rok wynik wyniki mandat mandaty mandatow
    posel poslow posla senator senatora miasto miescie wies wsi oraz czy jak dla przez jest byl byla bylo
    """.split()
)

MAX_PER_KIND = 5


@dataclass(frozen=True)
class EntityMatch:
    """A lexicon entry matched in the question."""

    kind: str
    name: str
    ref: str | None
    detail: str | None
    years: list[int]


def fold(text: str) -> str:
    """Lower-case and strip Polish diacritics (Python twin of ``mart.fold_name`` without prefixes)."""
    return text.lower().translate(_FOLD)


def question_terms(question: str) -> tuple[set[str], set[str]]:
    """Return (stems for name matching, exact folded words for alias matching)."""
    words = [w for w in re.findall(r"[a-z0-9']+", fold(question).replace("-", " "))]
    exact = set(words)
    stems: set[str] = set()
    for word in words:
        if len(word) < 3 or word in _STOPWORDS or word.isdigit():
            continue
        stems.add(word)
        for ending in _ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                stems.add(word[: -len(ending)])
                break
    return stems, exact


def _word_matches(word: str, stems: set[str]) -> bool:
    """A name word matches when a stem is its prefix and leaves only a short ending uncovered.

    Stems of 4+ letters may leave up to 3 letters ("warszaw" -> "warszawa"); 3-letter stems at most
    one ("now" -> "nowy", but not "nowak"). Long words also match near-identical stems (ratio >= 0.88).
    """
    for stem in stems:
        if word.startswith(stem):
            rest = len(word) - len(stem)
            if rest <= 1 or (len(stem) >= 4 and rest <= 3):
                return True
        # Stem alternations that suffix stripping cannot undo ("bialymstok" -> "bialystok").
        # The shared 4-letter prefix keeps "krakowie" away from "krapkowice".
        elif len(word) >= 6 and word[:4] == stem[:4] and SequenceMatcher(None, word, stem).ratio() >= 0.88:
            return True
    return False


def score_entry(kind: str, folded: str, stems: set[str], exact: set[str]) -> bool:
    """Whether a lexicon entry (already folded) is mentioned by the question terms."""
    words = [w for w in re.split(r"[\s\-]+", folded) if w]
    if not words:
        return False
    if kind == "committee":
        return all(w in exact for w in words)
    return all(_word_matches(w, stems) for w in words)


def resolve_entities(question: str, conn: psycopg.Connection | None = None) -> list[EntityMatch]:
    """Lexicon entries mentioned in ``question``, at most MAX_PER_KIND per kind (most recent first)."""
    stems, exact = question_terms(question)
    if not stems and not exact:
        return []
    prefixes = sorted({s[:3] for s in stems} | {w for w in exact if len(w) <= 4})
    own = conn is None
    conn = conn or psycopg.connect(MART_DATABASE_URL)
    try:
        rows = conn.execute(
            """
            SELECT kind, name, folded, ref, detail, years
            FROM mart.entity_lexicon
            WHERE EXISTS (
              SELECT 1 FROM unnest(%(prefixes)s::text[]) AS p
              WHERE (' ' || replace(folded, '-', ' ')) LIKE '%% ' || p || '%%'
            )
            """,
            {"prefixes": prefixes},
        ).fetchall()
    finally:
        if own:
            conn.close()

    by_kind: dict[str, list[EntityMatch]] = {}
    for kind, name, folded, ref, detail, years in rows:
        if score_entry(kind, folded, stems, exact):
            by_kind.setdefault(kind, []).append(EntityMatch(kind, name, ref, detail, list(years)))
    matches: list[EntityMatch] = []
    for kind in sorted(by_kind):
        entries = sorted(by_kind[kind], key=lambda m: (-max(m.years), m.name))
        matches.extend(entries[:MAX_PER_KIND])
    return matches


def format_entity_hints(matches: list[EntityMatch]) -> str:
    """Human/LLM readable list of matches with the key to use in SQL."""
    key_hint = {
        "gmina": "mart.precincts.teryt_gmina",
        "powiat": "left(mart.precincts.teryt_gmina, 4)",
        "wojewodztwo": "mart.precincts.wojewodztwo",
        "sejm_district": "sejm_district_no",
        "person": "person_id",
        "committee": "sejm_committees.committee_name",
    }
    lines = []
    for m in matches:
        ref = f"{key_hint.get(m.kind, 'ref')} = '{m.ref}'" if m.ref is not None else ""
        detail = f" — {m.detail}" if m.detail else ""
        lines.append(f"- {m.kind}: {m.name} ({ref}; years {m.years}){detail}")
    return "\n".join(lines)
