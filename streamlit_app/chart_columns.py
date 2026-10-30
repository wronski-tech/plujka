"""Which result columns the UI charts: measures versus codes, and the label and axes of a scatter plot."""

from __future__ import annotations

# Numeric columns that identify or order rows rather than measure something.
CODE_COLUMNS = ("year", "round", "prev_year", "list_position", "list_group", "rank", "place")
# Text columns naming a territorial unit, finest unit first (the hover label of a scatter point).
TERRITORIAL_LABELS = ("unit_name", "gmina", "powiat", "wojewodztwo", "area")


def numeric_columns(rows: list[dict]) -> list[str]:
    """Columns with numbers that are values, not codes (years, rounds, list numbers, ids, TERYT)."""
    first = rows[0]
    return [
        name for name, value in first.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and name not in CODE_COLUMNS and not name.endswith(("_no", "_id"))
        and not name.startswith("teryt")
    ]


def scatter_columns(rows: list[dict]) -> tuple[str, str, str] | None:
    """(label, x, y) when each row is one territorial unit with exactly two measures, else None.

    A unit is territorial when the label is a unit name column (gmina, powiat, ...) or the rows carry a
    TERYT code. Rows repeating a unit (e.g. one row per year) are not plotted.
    """
    if len(rows) < 3:
        return None
    measures = numeric_columns(rows)
    if len(measures) != 2:
        return None
    texts = [n for n, v in rows[0].items() if isinstance(v, str)]
    labels = [n for n in TERRITORIAL_LABELS if n in texts]
    if not labels and any(n.startswith("teryt") for n in rows[0]):
        # e.g. v_gus_units: teryt + name
        labels = [n for n in texts if not n.startswith("teryt")]
    if not labels:
        return None
    unit_columns = [n for n in rows[0] if n == labels[0] or n in TERRITORIAL_LABELS or n.startswith("teryt")]
    units = [tuple(row.get(n) for n in unit_columns) for row in rows]
    if len(set(units)) != len(units):
        return None
    return labels[0], measures[0], measures[1]


def line_series(rows: list[dict], max_series: int = 12) -> tuple[str | None] | None:
    """(series column or None,) when rows form a time series: two or more years, at most one varying text
    column (e.g. party) with up to ``max_series`` values, and one row per year and series; else None."""
    if not rows or "year" not in rows[0] or not numeric_columns(rows):
        return None
    if len({row.get("year") for row in rows}) < 2:
        return None
    texts = [n for n, v in rows[0].items() if isinstance(v, str) and not n.startswith("teryt")]
    varying = [n for n in texts if len({row.get(n) for row in rows}) > 1]
    if len(varying) > 1:
        return None
    series = varying[0] if varying else None
    keys = [(row.get("year"), row.get(series) if series else None) for row in rows]
    if len(set(keys)) != len(keys) or (series and len({k[1] for k in keys}) > max_series):
        return None
    return (series,)
