"""Questions outside the data (polls, forecasts, future results), refused before any SQL or model call.

The mart holds official results of past elections and GUS statistics. A poll, a forecast or "who will
win" has no rows to come from, and letting the model answer would put facts from outside the data into
the answer, so such questions get a fixed refusal.
"""

from __future__ import annotations

import re
from typing import Any

from api.services.answer import is_english
from api.services.mart.entities import fold

# Folded text (lower case, no Polish letters). Future forms only: "wygra" is not "wygrał", and
# conditionals of the seat simulator ("miałby", "gdyby") are not refused.
_OUT_OF_SCOPE_RE = re.compile(
    # Polls and exit polls.
    r"sondaz|\bankiet|\bcbos\b|\bibris\b|\bipsos\b|\bexit.?poll|\blate.?poll|\bpolls?\b|\bpolling (data|average)|"
    # Forecasts.
    r"prognoz|przewidyw|przewidz|predykc|forecast|predict|projection|"
    # Future results.
    r"\bwygra(ja)?\b|\bzdobedzie|\bzdobeda\b|\bdostanie\b|\bdostana\b|\bwejdzie do|\bwejda do|"
    r"\bbedzie (mial|miala|mialo|rzadzic|prezydentem|premierem)|\bbeda (mieli|rzadzic)|"
    r"\bnastepn\w* wybor|\bprzyszl\w* (wybor|rok|kadencj)|\bnadchodzac\w* wybor|"
    r"^\W*will\b|\bwho will\b|\bwill (win|get|be elected|enter|form)\b|\bnext election|\bupcoming election"
)

REFUSAL_PL = (
    "Na to pytanie nie odpowiem: w bazie są tylko oficjalne wyniki minionych wyborów (PKW/KBW) i statystyki "
    "GUS, a sondaże, prognozy ani przyszłe wyniki w niej nie występują. Mogę pokazać wyniki wcześniejszych "
    "wyborów, np. „Jaki był wynik PiS w wyborach do Sejmu 2023?”."
)
REFUSAL_EN = (
    "I can't answer that: the database holds only official results of past elections (PKW/KBW) and GUS "
    "statistics, not polls, forecasts or future results. I can show results of past elections, e.g. "
    "\"What was PiS's result in the 2023 Sejm election?\"."
)


def is_out_of_scope(question: str) -> bool:
    """Whether the question asks about polls, forecasts or future results."""
    return _OUT_OF_SCOPE_RE.search(fold(question)) is not None


def refusal_answer(question: str) -> dict[str, Any]:
    """Answer payload (same shape as ``answer.compose_answer``) with the fixed refusal."""
    text = REFUSAL_EN if is_english(question) else REFUSAL_PL
    return {"text": text, "source": "refusal", "verified": True, "caveats": []}
