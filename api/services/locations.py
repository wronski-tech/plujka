"""Map Polish place names to Sejm electoral district numbers (okręg).

These are national Sejm district IDs as used in PKW CSV `Nr okręgu` / `Numer okręgu`.
One district often covers a city plus surrounding powiaty — filtering is therefore
"okręg containing Wrocław", not gmina-level city-only results.
"""

from __future__ import annotations

# Lowercase ASCII or Polish keys; match as substrings in normalized question.
CITY_TO_SEJM_DISTRICTS: dict[str, list[str]] = {
    "wrocław": ["3"],
    "wroclaw": ["3"],
}


def districts_for_question(question: str) -> list[str] | None:
    lowered = question.lower()
    for key, districts in CITY_TO_SEJM_DISTRICTS.items():
        if key in lowered:
            return districts
    return None
