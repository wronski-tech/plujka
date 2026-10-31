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
    "owie", "iego", "ach", "ami", "ego", "emu", "owi", "iem", "ich", "iej", "ym", "im", "em", "ej", "iu",
    "ie", "ow", "om", "a", "u", "y", "i", "e",
)
# A declined adjective inside a one-word town name ("Białegostoku", "Białymstoku", "Krasnymstawie").
_INNER_ADJECTIVE = re.compile(r"(?<=[a-z]{3})(?:ego|ym)(?=[a-z]{3})")

# Frequent question words that would otherwise prefix-match place or person names.
_STOPWORDS = frozenset(
    """
    ile ilu kto gdzie jaki jaka jakie ktory ktora ktore ktorych ktorej najwiecej najmniej najwyzsza najnizsza
    wybory wyborach wyborow sejm sejmu senat senatu gmina gminie gminy powiat powiecie powiatu okreg okregu
    wojewodztwo wojewodztwie glosow glosy glos procent poparcie frekwencja frekwencji kandydat kandydaci
    kandydatow partia partii komitet komitetu lista listy roku rok wynik wyniki mandat mandaty mandatow
    posel poslow posla senator senatora miasto miescie wies wsi oraz czy jak dla przez jest byl byla bylo
    tura ture turze tury turach mieli mial miala mialo
    the who what which how many much did does was were get got and for from with won win votes vote
    election elections turnout party parties seats seat result results district districts county counties
    municipality municipalities voivodeship voivodeships province president presidential parliament
    parliamentary european local mayor round first second percent share most highest lowest city town
    candidate candidates committee year compare between support
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


def _word_stems(word: str) -> set[str]:
    """Stems of one folded question word: the word (also with an inner adjective or a "-rze" locative
    in the nominative) and the word without one case ending."""
    if len(word) < 3 or word in _STOPWORDS or word.isdigit():
        return set()
    stems = set()
    # Locative "-rze" of "-ra" nouns ("Górze" -> "Góra").
    for form in {word, _INNER_ADJECTIVE.sub("y", word, count=1), re.sub(r"rze$", "ra", word)}:
        stems.add(form)
        for ending in _ENDINGS:
            if form.endswith(ending) and len(form) - len(ending) >= 3:
                stems.add(form[: -len(ending)])
                break
    return stems


def question_terms(question: str) -> tuple[set[str], set[str]]:
    """Return (stems for name matching, exact folded words for alias matching)."""
    # English possessives: "Warsaw's" is "Warsaw".
    words = [w for w in re.findall(r"[a-z0-9']+", re.sub(r"'s\b", "", fold(question)).replace("-", " "))]
    return {s for word in words for s in _word_stems(word)}, set(words)


def _match_rest(word: str, stems: set[str]) -> int | None:
    """Letters of ``word`` a stem leaves uncovered when it matches (None: no match).

    Stems of 4+ letters may leave up to 3 letters ("warszaw" -> "warszawa"); 3-letter stems at most
    one ("now" -> "nowy", but not "nowak"). Long words also match near-identical stems (ratio >= 0.88).
    """
    best = None
    for stem in stems:
        if word.startswith(stem):
            rest = len(word) - len(stem)
            if rest <= 1 or (len(stem) >= 4 and rest <= 3):
                best = rest if best is None else min(best, rest)
        # Stem alternations that suffix stripping cannot undo ("bialymstok" -> "bialystok").
        # The shared 4-letter prefix keeps "krakowie" away from "krapkowice".
        elif len(word) >= 6 and word[:4] == stem[:4] and SequenceMatcher(None, word, stem).ratio() >= 0.88:
            best = 3 if best is None else best
        # One typo (a wrong letter or two swapped letters) in a long stem ("platfromy" -> "platforma").
        elif len(stem) >= 6 and word[:2] == stem[:2] and len(word) - len(stem) <= 3 and _one_edit(word[: len(stem)], stem):
            best = 3 if best is None else best
    return best


def _one_edit(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` (same length) differ by one substituted letter or two swapped neighbours."""
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    return len(a) == len(b) and (len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                                   and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]))


def _word_matches(word: str, stems: set[str]) -> bool:
    """A name word matches when a stem is its prefix and leaves only a short ending uncovered."""
    return _match_rest(word, stems) is not None


def surname_mentioned(name: str, question: str, alias_words: set[str] = frozenset()) -> bool:
    """Whether ``question`` names the person ``name`` ('Given Names SURNAME'), not a look-alike word.

    Each surname word must be matched by a question word other than the one naming a given name
    ("Rafał Trzaskowski" is not Rafał RAFALSKI). When the question has none of the person's given
    names, that word must also be capitalized ("w gminach" is not GMIŃSKI, "turę" is not TUR) and
    closer to the surname than to any committee alias word ("Lewicy" is Lewica, not LEWICKI); on a
    tie it must be written like a name ("Kukiz" is the person too, "PiS" only the party, not PIŚ).
    """
    parts = name.split()
    given = [fold(w) for w in parts if not (w.isupper() and len(w) > 1)]
    surname = [fold(p) for w in parts if w.isupper() and len(w) > 1 for p in w.split("-") if p]
    words = [(raw, _word_stems(fold(raw))) for raw in re.findall(r"[^\W_]+", question)]
    named = any(_word_matches(g, stems) for g in given for _, stems in words)

    def names(part: str, raw: str, stems: set[str]) -> bool:
        rest = _match_rest(part, stems)
        if rest is None or any(_word_matches(g, stems) for g in given):
            return False
        if named:
            return True
        alias_rest = min((r for a in alias_words if (r := _match_rest(a, stems)) is not None), default=None)
        # A tie with an alias keeps the person only when written like a name ("Kukiz", not "PiS").
        closer = alias_rest is None or rest < alias_rest or (rest == alias_rest and raw[1:].islower())
        return raw[:1].isupper() and closer

    return bool(surname) and all(any(names(part, raw, stems) for raw, stems in words) for part in surname)


def score_entry(kind: str, folded: str, stems: set[str], exact: set[str]) -> bool:
    """Whether a lexicon entry (already folded) is mentioned by the question terms."""
    words = [w for w in re.split(r"[\s\-]+", folded) if w]
    if not words:
        return False
    if kind == "committee":
        # Abbreviations must be written exactly ("PO" is not "pod"); longer words may be inflected
        # ("Konfederacji", "Platformy").
        return all(w in exact or (len(w) >= 5 and _word_matches(w, stems)) for w in words)
    return all(_word_matches(w, stems) for w in words)


def resolve_entities(question: str, conn: psycopg.Connection | None = None) -> list[EntityMatch]:
    """Lexicon entries mentioned in ``question``, at most MAX_PER_KIND per kind.

    Places are ordered by name length in words, then recency; persons as described in the sort key below.
    """
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

    alias_words = {w for kind, _, folded, *_ in rows if kind == "committee" for w in re.split(r"[\s\-]+", folded) if w}
    by_kind: dict[str, list[EntityMatch]] = {}
    for kind, name, folded, ref, detail, years in rows:
        if score_entry(kind, folded, stems, exact) and (kind != "person" or surname_mentioned(name, question, alias_words)):
            by_kind.setdefault(kind, []).append(EntityMatch(kind, name, ref, detail, list(years)))

    def uncovered(m: EntityMatch) -> int:
        """Letters the question leaves uncovered in the name (exact alias words leave none)."""
        words = [w for w in re.split(r"[\s\-]+", _PLACE_PREFIX.sub("", fold(m.name))) if w]
        return sum(0 if w in exact else (_match_rest(w, stems) or 0) for w in words)

    # A place or committee named twice (an English or colloquial name and its own: "Warsaw" also
    # matches "Warszawie") keeps the closer name, so duplicates do not fill the cut below.
    for kind, found in by_kind.items():
        if kind != "person":
            best: dict[tuple, EntityMatch] = {}
            for m in found:
                key = (m.ref, tuple(m.years))
                if key not in best or uncovered(m) < uncovered(best[key]):
                    best[key] = m
            by_kind[kind] = list(best.values())
    matches: list[EntityMatch] = []
    years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", question)}
    for kind in sorted(by_kind):
        if kind == "person":
            # Before the cut: people whose given name is in the question, then those who ran in a
            # year it names, then those elected more often (a bare "Kaczyński" most likely means the
            # long-serving MP), then recency.
            def key(m: EntityMatch) -> tuple:
                named = any(_word_matches(g, stems) for g in _given_names(m))
                return (not named, not years & set(m.years), -(m.detail or "").count("elected"), -max(m.years), m.name)
        elif kind == "committee":
            # Committees of the years the question names, then the latest ("Third Way and Polish People's
            # Party" keeps the 2023 coalition, not five older PSL committees with a longer name).
            def key(m: EntityMatch) -> tuple:
                return (bool(years) and not years & set(m.years), -max(m.years), m.name)
        else:
            # Longer names first: every word of a match is in the question ("Jelenia Góra" before "Góra").
            def key(m: EntityMatch) -> tuple:
                return (-len(re.split(r"[\s\-]+", _PLACE_PREFIX.sub("", fold(m.name)))), -max(m.years), m.name)
        matches.extend(sorted(by_kind[kind], key=key)[:MAX_PER_KIND])
    return matches


def format_entity_hints(matches: list[EntityMatch]) -> str:
    """Human/LLM readable list of matches with the key to use in SQL."""
    key_hint = {
        "gmina": "teryt_gmina",
        "powiat": "teryt_powiat (or left(teryt_gmina, 4))",
        "wojewodztwo": "wojewodztwo",
        "sejm_district": "sejm_district_no",
        "person": "person_id",
        "committee": "committee_name (alias in aliases)",
    }
    lines = []
    for m in matches:
        ref = f"{key_hint.get(m.kind, 'ref')} = '{m.ref}'" if m.ref is not None else ""
        detail = f" — {m.detail}" if m.detail else ""
        lines.append(f"- {m.kind}: {m.name} ({ref}; years {m.years}){detail}")
    return "\n".join(lines)


# --- Narrowing and ambiguity ------------------------------------------------------------------

PLACE_KINDS = ("gmina", "powiat", "wojewodztwo", "sejm_district")
# Same prefix rule as mart.fold_name: 'gm. X', 'm. X' and 'm. st. X' are all 'x'.
_PLACE_PREFIX = re.compile(r"^(m\.\s?st\.\s|m\.\s|gm\.\s)")


def _given_names(match: EntityMatch) -> list[str]:
    """Folded given names of a person match (``name`` is 'Given Names SURNAME')."""
    return [fold(w) for w in match.name.split() if not (w.isupper() and len(w) > 1)]


# Voivodeship and powiat names are adjectives written in lower case ("województwo mazowieckie",
# "powiat bocheński"), unlike gmina names and surnames.
ADJECTIVE_PLACE_KINDS = ("wojewodztwo", "powiat")


def _capitalized_inside(question: str, surname: str) -> bool:
    """Whether a capitalized word after the first one names ``surname`` ("Wynik Mazowieckiego")."""
    words = re.findall(r"[^\W_]+", question)[1:]
    return any(raw[:1].isupper() and _word_matches(surname, _word_stems(fold(raw))) for raw in words)


# Words naming an election -> the chamber label in person details ("prezydent 2005 (round 1)").
_CHAMBER_WORDS = (
    (r"prezydenck", "prezydent"), (r"\bsejm(?!ik)", "sejm"), (r"\bsenat", "senat"), (r"europ", "pe"),
    (r"\bsejmik", "sejmik"), (r"\b(wojt|burmistrz|prezydent(em|a)? miasta)", "wbp"),
)


def narrow_matches(question: str, matches: list[EntityMatch]) -> list[EntityMatch]:
    """Drop matches the question rules out.

    - Persons whose given name is not in the question, when another person with the same surname has
      one there ("Jarosław Kaczyński" keeps only Jarosław).
    - Persons whose surname is really a matched place name ("w Krakowie" also matches surname KRAK),
      unless the place is a voivodeship or powiat (lower-case adjectives) and the person is named
      with a given name or a capitalized word inside the sentence ("Tadeusz Mazowiecki", "wynik
      Mazowieckiego"): then the place is dropped instead.
    - Places named like the given name of a person in the question ("Jarosław Kaczyński" also
      matches the gminas Jarosław).
    - Places matched only by a word that is a matched committee alias ("PiS" is not the gmina Pisz).
    - Voivodeships of 1975-1998 when the same question also matched a current gmina or powiat and
      names neither a year before 1999 nor "województwo" ("w Krakowie" is the city, not the
      voivodeship krakowskie).
    - Places whose question words another match names more closely or together with more words
      ("Szczecin" is not Szczecinek, "Bielsko-Biała" not Biała, "Przemysław Czarnek" neither
      Przemyśl nor Czarne).
    - Persons without a candidacy in the years the question names, when another matched person has
      one ("Jarosław Kaczyński w 2023" is the one who ran in 2023), and then without a candidacy in
      the election it names ("prezydenckich 2005").
    - Rural gminas named like a town gmina when the question says "miasto" ("w mieście Elbląg").
    """
    stems, _ = question_terms(question)
    question_words = [_word_stems(fold(raw)) for raw in re.findall(r"[^\W_]+", question)]

    def rests(m: EntityMatch) -> dict[int, int]:
        """Question word index -> fewest letters any name word of ``m`` leaves uncovered there."""
        out: dict[int, int] = {}
        for word in re.split(r"[\s\-]+", _PLACE_PREFIX.sub("", fold(m.name))):
            for i, word_stems in enumerate(question_words):
                if word and (rest := _match_rest(word, word_stems)) is not None:
                    out[i] = min(rest, out.get(i, rest))
        return out

    covered = {id(m): rests(m) for m in matches if m.kind in PLACE_KINDS or m.kind == "person"}

    def dominated(place: EntityMatch) -> bool:
        mine = covered[id(place)]
        name = _PLACE_PREFIX.sub("", fold(place.name))
        for other in matches:
            theirs = covered.get(id(other))
            # Places compete with places of their kind; a person only when named with more words
            # (the surname alone may be the place: KRAK in "w Krakowie"). Persons compete with other
            # people ("Kosiniak-Kamysz" is not KAMYSZEK, a typo-close KOSIENIAK or KACZYŃSKA for "Kaczyński").
            # A person also yields to a place its word names more closely (KRASOWSKI is not "Krakowa").
            rivals = (*PLACE_KINDS, "person") if place.kind == "person" else (place.kind, "person")
            if theirs is None or other is place or other.kind not in rivals or (
                other.kind == place.kind and (_PLACE_PREFIX.sub("", fold(other.name)) == name or other.ref == place.ref)
            ):
                continue
            if mine and all(i in theirs and theirs[i] <= r for i, r in mine.items()) and (
                len(theirs) > len(mine) or ((other.kind != "person" or place.kind == "person") and any(theirs[i] < r for i, r in mine.items()))
            ):
                return True
        return False

    towns = {_PLACE_PREFIX.sub("", fold(m.name)): m.ref for m in matches if m.kind == "gmina" and m.name.startswith("m. ")}
    town_named = bool(towns) and re.search(r"\bmiast|\bmiesc", fold(question)) is not None
    shadowed = {id(m) for m in matches if (m.kind in PLACE_KINDS or m.kind == "person") and dominated(m)}
    years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", question)}
    in_years = [m for m in matches if m.kind == "person" and years & set(m.years)]
    # The election the question names narrows further ("Marek Borowski ... prezydenckich 2005").
    chambers = [c for pattern, c in _CHAMBER_WORDS if re.search(pattern, fold(question))]
    in_chamber = [m for m in in_years or [m for m in matches if m.kind == "person"]
                  if any(f"{c} {y}" in (m.detail or "") for c in chambers for y in (years or {""}))]
    if in_chamber:
        in_years = in_chamber
    aliases = {fold(m.name) for m in matches if m.kind == "committee"}
    other_stems = {s for raw in re.findall(r"[^\W_]+", question) if fold(raw) not in aliases
                   for s in _word_stems(fold(raw))}
    old_years = any(int(y) < 1999 for y in re.findall(r"\b(?:19|20)\d{2}\b", question))
    current_place = any(m.kind in ("gmina", "powiat") and max(m.years, default=0) >= 1999 for m in matches)
    persons = [m for m in matches if m.kind == "person"]
    named = [m for m in persons if any(_word_matches(g, stems) for g in _given_names(m))]
    given = {g for m in named for g in _given_names(m)}

    def clashing_places(person: EntityMatch) -> list[EntityMatch]:
        surname = fold(person.name.split()[-1])
        if len(surname) < 4:
            return []
        return [p for p in matches if p.kind in PLACE_KINDS and id(p) not in shadowed
                and any(w.startswith(surname[:4]) for w in re.split(r"[\s\-]+", _PLACE_PREFIX.sub("", fold(p.name))))]

    person_wins, beaten = set(), set()
    for m in persons:
        clash = clashing_places(m)
        if clash and all(p.kind in ADJECTIVE_PLACE_KINDS for p in clash) and (
            m in named or _capitalized_inside(question, fold(m.name.split()[-1]))
        ):
            person_wins.add(id(m))
            beaten.update(id(p) for p in clash)
    out = []
    for m in matches:
        if id(m) in beaten or id(m) in shadowed:
            continue
        if m.kind == "person" and in_years and m not in in_years:
            continue
        if town_named and m.kind == "gmina" and towns.get(_PLACE_PREFIX.sub("", fold(m.name)), m.ref) != m.ref:
            continue
        if m.kind in PLACE_KINDS and _PLACE_PREFIX.sub("", fold(m.name)) in given:
            continue
        if m.kind in PLACE_KINDS and aliases and not all(
            _word_matches(w, other_stems) for w in re.split(r"[\s\-]+", _PLACE_PREFIX.sub("", fold(m.name))) if w
        ):
            continue
        if m.kind == "wojewodztwo" and max(m.years, default=0) < 1999 and current_place and not old_years \
                and "wojewodztw" not in fold(question):
            continue
        if m.kind == "person":
            if named and m not in named:
                continue
            if id(m) not in person_wins and clashing_places(m):
                continue
        out.append(m)
    return out


def find_ambiguity(matches: list[EntityMatch]) -> dict | None:
    """A clarification request when the question names several different people or same-name gminas.

    Returns ``{"prompt", "kind", "options": [{"label", "kind", "ref"}]}`` or None. Places of different
    kinds that share a name (gmina Kraków, powiat Kraków, Sejm district Kraków) are not ambiguous:
    the SQL generator picks the level from the question.
    """
    persons = list({m.ref: m for m in matches if m.kind == "person"}.values())  # a colloquial name and its person
    if len(persons) > 1:
        return {
            "prompt": "Pytanie pasuje do kilku osób. O którą chodzi?",
            "kind": "person",
            "options": [{"label": f"{m.name} — {m.detail}", "kind": "person", "ref": m.ref} for m in persons],
        }
    gminas_by_name: dict[str, list[EntityMatch]] = {}
    for m in matches:
        if m.kind == "gmina":
            gminas_by_name.setdefault(_PLACE_PREFIX.sub("", fold(m.name)), []).append(m)
    for same_name in gminas_by_name.values():
        if len({m.ref for m in same_name}) > 1:
            return {
                "prompt": "Jest kilka gmin o tej nazwie. O którą chodzi?",
                "kind": "gmina",
                "options": [{"label": f"{m.name} ({m.detail})", "kind": "gmina", "ref": m.ref} for m in same_name],
            }
    return None


def lookup_refs(refs: list[dict], conn: psycopg.Connection | None = None) -> list[EntityMatch]:
    """Lexicon entries for explicit ``{"kind", "ref"}`` choices (e.g. answers to a clarification)."""
    if not refs:
        return []
    own = conn is None
    conn = conn or psycopg.connect(MART_DATABASE_URL)
    try:
        rows = conn.execute(
            """
            SELECT kind, name, ref, detail, years FROM mart.entity_lexicon
            WHERE (kind, ref) IN (SELECT * FROM unnest(%(kinds)s::text[], %(refs)s::text[]))
            """,
            {"kinds": [r["kind"] for r in refs], "refs": [str(r["ref"]) for r in refs]},
        ).fetchall()
    finally:
        if own:
            conn.close()
    return [EntityMatch(kind, name, ref, detail, list(years)) for kind, name, ref, detail, years in rows]
