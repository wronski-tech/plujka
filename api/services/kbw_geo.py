"""Shared KBW geography expressions for SQL (okręg / district from JSON `geography`).

Used by `kbw_v_sejm_district_list_agg`, analytics queries, and person backfill so filters stay aligned.
"""

from __future__ import annotations


def district_expr_sql(alias: str = "f") -> str:
    """SQL fragment: normalized Sejm district string from `kbw_facts.geography` JSON.

    `alias` is the table alias for `kbw_facts` (default `f`).
    """
    a = alias
    return f"""COALESCE(
          NULLIF(trim({a}.geography->>'Numer okręgu'), ''),
          NULLIF(trim({a}.geography->>'Numer okregu'), ''),
          NULLIF(trim({a}.geography->>'Nr okręgu'), ''),
          NULLIF(trim({a}.geography->>'Nr okregu'), ''),
          NULLIF(trim({a}.geography->>'Okręg'), ''),
          NULLIF(trim({a}.geography->>'Okreg'), ''),
          NULLIF(trim({a}.geography->>'Numer okręgu'), '')
        )"""
