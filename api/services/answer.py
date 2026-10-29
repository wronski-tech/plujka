"""Natural-language answer for SQL rows, with every number checked against the rows.

The LLM may phrase the result in Polish, but it may not introduce facts. ``compose_answer`` therefore
extracts every number from the generated text and accepts the text only when each number appears in
the result rows (allowing rounding to 0-2 decimals and Polish number formatting) or in the question.
Otherwise it returns a deterministic summary built directly from the rows.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any

from openai import OpenAI

from api.services.config import ANSWER_SUMMARY_ENABLED, OPENAI_API_KEY, OPENAI_CHAT_MODEL

log = logging.getLogger(__name__)

MAX_ROWS_IN_PROMPT = 30

# A number in Polish or English formatting: "7 640 854", "7,640,854", "35,38", "35.4", "2023".
_NUMBER_RE = re.compile(r"(?<![\w.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:,\d+)?(?![\d])|(?<![\w])\d+(?:[.,]\d+)*")

ANSWER_PROMPT = """You describe the result of a SQL query over official Polish election data.
Write 1-3 short sentences in Polish that answer the question.
Rules:
- Use only facts and numbers that appear in the rows. Do not compute new numbers (no differences,
  sums or ratios that are not in the rows) and do not add knowledge from outside the rows.
- Copy numbers exactly; you may round percentages to one decimal. Write thousands with spaces
  (7 640 854) and decimals with a comma (35,38%).
- If the rows do not answer the question, say so briefly.
Return JSON: {"answer": "..."}"""


def _number_variants(token: str) -> set[Decimal]:
    """All plausible values of a number token (``1,234`` may be 1234 or 1.234)."""
    compact = re.sub(r"[ \u00a0\u202f]", "", token)
    variants: set[Decimal] = set()
    candidates = {compact.replace(",", "."), compact.replace(",", ""), compact.replace(".", "").replace(",", "."),
                  compact.replace(".", "")}
    for candidate in candidates:
        try:
            variants.add(Decimal(candidate))
        except ArithmeticError:
            continue
    return variants


def numbers_in_text(text: str) -> list[str]:
    """Number tokens in ``text`` (thousand separators and decimal commas kept together)."""
    return [m.group(0).rstrip(".,") for m in _NUMBER_RE.finditer(text) if m.group(0).rstrip(".,")]


def allowed_numbers(rows: list[dict[str, Any]], question: str) -> set[Decimal]:
    """Numbers an answer may contain: row values (and their roundings), numbers in the question, row ranks."""
    allowed: set[Decimal] = {Decimal(n) for n in range(1, len(rows) + 1)}
    for row in rows:
        for value in row.values():
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float, Decimal)):
                number = Decimal(str(value))
                allowed.add(number)
                allowed.update(round(number, digits) for digits in (0, 1, 2))
            else:
                for token in numbers_in_text(str(value)):
                    allowed.update(_number_variants(token))
    for token in numbers_in_text(question):
        allowed.update(_number_variants(token))
    return allowed


def unsupported_numbers(text: str, rows: list[dict[str, Any]], question: str) -> list[str]:
    """Number tokens of ``text`` that are not backed by the rows or the question."""
    allowed = allowed_numbers(rows, question)
    return [token for token in numbers_in_text(text) if not (_number_variants(token) & allowed)]


# Columns holding codes rather than quantities: printed without thousand separators.
_CODE_COLUMNS = ("year", "round", "prev_year")
_CODE_SUFFIXES = ("_no", "_id")


def format_number(value: Any, column: str = "") -> str:
    """Polish formatting: thousands separated by spaces, decimal comma; years and ids stay plain."""
    if isinstance(value, bool):
        return "tak" if value else "nie"
    if isinstance(value, int) and (column in _CODE_COLUMNS or column.endswith(_CODE_SUFFIXES)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, (float, Decimal)):
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    return str(value)


def fallback_answer(rows: list[dict[str, Any]]) -> str:
    """Deterministic summary taken directly from the rows."""
    if not rows:
        return "Brak wyników w danych dla tego pytania."
    first = "; ".join(f"{column}: {format_number(value, column)}" for column, value in rows[0].items() if value is not None)
    if len(rows) == 1:
        return f"Wynik: {first}."
    return f"Znaleziono {format_number(len(rows))} wierszy. Pierwszy: {first}."


def _values(rows: list[dict[str, Any]], column: str) -> set[Any]:
    """Distinct values of ``column`` in the rows."""
    return {row.get(column) for row in rows if column in row}


def _about(rows: list[dict[str, Any]], sql: str, election: str, year: int) -> bool:
    """Whether the rows (``election``/``year`` columns) or the SQL text concern one election-year."""
    sql_l = sql.lower()
    elections = _values(rows, "election") or {e for e in ("prezydent", "pe", "sejm", "senat") if f"'{e}'" in sql_l or f"v_{e}_" in sql_l}
    years = _values(rows, "year") or {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", sql)}
    return election in elections and year in years


# Caveats of the data that a reader must know to interpret the rows. Each rule looks only at the rows
# and the SQL, never at outside facts, so the text contains no numbers of its own. Details of each
# gap are in docs/MART.md ("Known gaps").
CAVEATS: tuple[tuple[str, Any], ...] = (
    (
        "Porównanie dotyczy różnych komitetów tej samej rodziny partyjnej (np. PO → KO, PSL → Trzecia Droga); "
        "zmiana poparcia nie jest porównaniem tego samego komitetu.",
        lambda rows, sql: False in _values(rows, "comparable"),
    ),
    (
        "W części okręgów rad PKW nie opublikowała kandydatów skreślonych po druku kart, więc głosy na listy "
        "są tam sumą głosów pozostałych kandydatów (dane przybliżone).",
        lambda rows, sql: False in _values(rows, "votes_exact"),
    ),
    (
        "Frekwencja w wyborach prezydenckich 2010 jest liczona z głosów oddanych (pliki nie podają liczby kart "
        "ważnych), więc może się nieznacznie różnić od frekwencji podanej przez PKW, liczonej z kart wydanych.",
        lambda rows, sql: _about(rows, sql, "prezydent", 2010) and ("turnout_pct" in sql.lower() or bool(_values(rows, "turnout_pct"))),
    ),
    (
        "Frekwencja w wyborach do Sejmu 2005 i 2007 oraz prezydenckich 2005 jest liczona z kart ważnych, "
        "więc jest nieznacznie niższa niż podana przez PKW, liczona z kart wydanych.",
        lambda rows, sql: (_about(rows, sql, "sejm", 2005) or _about(rows, sql, "sejm", 2007)
                           or _about(rows, sql, "prezydent", 2005))
        and ("turnout_pct" in sql.lower() or bool(_values(rows, "turnout_pct"))),
    ),
    (
        "Frekwencja w wyborach do PE 2014 nie obejmuje pakietów wyborczych (głosowanie korespondencyjne), "
        "więc może być nieznacznie niższa niż podana przez PKW.",
        lambda rows, sql: _about(rows, sql, "pe", 2014) and ("turnout_pct" in sql.lower() or bool(_values(rows, "turnout_pct"))),
    ),
    (
        "Wyniki kandydatów na prezydenta są sumą wyników obwodów; kilka obwodów za granicą nie opublikowało "
        "wyników, więc sumy krajowe mogą być nieco niższe od oficjalnych (oficjalne są w prezydent_candidates).",
        lambda rows, sql: "v_prezydent_results" in sql.lower() and "country" in _values(rows, "level"),
    ),
    (
        "PKW nie publikuje identyfikatorów osób: kandydatury łączone są po imieniu, nazwisku, płci "
        "(i roku urodzenia w wyborach samorządowych), więc imiennicy mogą być połączeni lub rozdzieleni błędnie.",
        lambda rows, sql: "v_person_history" in sql.lower() or "persons" in sql.lower(),
    ),
)


def caveats(rows: list[dict[str, Any]], sql: str) -> list[str]:
    """Polish caveats that apply to these rows and this SQL (empty when there are no rows)."""
    if not rows:
        return []
    return [text for text, applies in CAVEATS if applies(rows, sql or "")]


def _llm_answer(question: str, rows: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    """Ask the chat model for a short answer; return (text, token usage)."""
    payload = json.dumps(rows[:MAX_ROWS_IN_PROMPT], ensure_ascii=False, default=str)
    note = f"\n(Showing {MAX_ROWS_IN_PROMPT} of {len(rows)} rows.)" if len(rows) > MAX_ROWS_IN_PROMPT else ""
    response = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": ANSWER_PROMPT},
            {"role": "user", "content": f"Question: {question}\nRows: {payload}{note}"},
        ],
    )
    usage = response.usage
    tokens = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else {}
    text = (json.loads(response.choices[0].message.content or "{}").get("answer") or "").strip()
    return text, tokens


def compose_answer(question: str, rows: list[dict[str, Any]], sql: str = "") -> dict[str, Any]:
    """Answer text for the rows: LLM text when every number checks out, otherwise the fallback summary.

    Returns ``{"text", "source": "llm" | "fallback", "verified": bool, "caveats": [...], ...}``;
    ``rejected_numbers`` lists the numbers that made an LLM text fail the check; ``caveats`` are fixed
    Polish notes about data gaps that apply to these rows (see ``CAVEATS``).
    """
    result: dict[str, Any] = {"text": fallback_answer(rows), "source": "fallback", "verified": True, "caveats": caveats(rows, sql)}
    if not rows or not ANSWER_SUMMARY_ENABLED or not OPENAI_API_KEY:
        return result
    try:
        text, tokens = _llm_answer(question, rows)
    except Exception as exc:  # the answer text is optional; rows are still returned
        log.warning("answer generation failed: %s", exc)
        return {**result, "error": str(exc)}
    result["tokens"] = tokens
    if not text:
        return result
    rejected = unsupported_numbers(text, rows, question)
    if rejected:
        log.info("answer rejected: numbers %s are not in the SQL result", rejected)
        return {**result, "rejected_numbers": rejected}
    return {**result, "text": text, "source": "llm"}
