"""Validation and read-only execution of LLM-generated SQL against the ``mart`` schema.

Two independent layers protect the database:

1. ``validate_mart_sql`` parses the query with sqlglot (PostgreSQL dialect) and accepts only a single
   SELECT (optionally with CTEs / set operations) that reads mart relations and calls no dangerous
   functions; it also caps the row count with LIMIT.
2. ``run_readonly`` executes it as the ``MART_DATABASE_URL`` login role, which may only SELECT from
   mart, runs every transaction read-only and has a statement timeout (see ``schema.ensure_reader_role``).
"""

from __future__ import annotations

from typing import Any

import psycopg
import sqlglot
from sqlglot import exp

from api.services.config import MART_DATABASE_URL, MART_MAX_ROWS, MART_STATEMENT_TIMEOUT_MS
from api.services.mart.schema import QUERYABLE_RELATIONS, SCHEMA

# Statement-level nodes that must never appear anywhere in the tree.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Set, exp.Use, exp.Grant, exp.TruncateTable, exp.Copy, exp.Into, exp.Lock,
)

# Functions with side effects or that expose server internals. Matched case-insensitively against
# the function name; names starting with ``pg_`` are rejected as well.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "set_config", "current_setting", "dblink", "dblink_exec", "lo_import", "lo_export", "lo_get",
        "lo_put", "query_to_xml", "query_to_json", "table_to_xml", "cursor_to_xml", "database_to_xml",
        "schema_to_xml", "txid_current", "version", "inet_server_addr", "inet_client_addr",
    }
)


class UnsafeSqlError(ValueError):
    """The SQL is not a single read-only query over the mart schema."""


def _function_name(node: exp.Expression) -> str:
    """Lower-case name of a function call node (anonymous or known)."""
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return node.sql_name().lower() if isinstance(node, exp.Func) else ""


def validate_mart_sql(sql: str, max_rows: int = MART_MAX_ROWS) -> str:
    """Return a normalized, row-capped version of ``sql`` or raise UnsafeSqlError.

    Rules: exactly one statement; the root is SELECT / UNION / INTERSECT / EXCEPT (CTEs allowed);
    no DML/DDL or session commands anywhere; every table is a CTE name or a relation of the mart
    schema (qualified ``mart.x`` or bare ``x``); no system or side-effect functions; LIMIT <= max_rows.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise UnsafeSqlError(f"SQL does not parse: {exc}") from exc
    if len(statements) != 1:
        raise UnsafeSqlError(f"Expected exactly one statement, got {len(statements)}.")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSqlError(f"Only SELECT queries are allowed (got {type(tree).__name__}).")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise UnsafeSqlError(f"Forbidden statement part: {type(node).__name__}.")
        name = _function_name(node)
        if name and (name in _FORBIDDEN_FUNCTIONS or name.startswith("pg_")):
            raise UnsafeSqlError(f"Function {name}() is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not table.name:  # table-valued function such as generate_series(...)
            continue
        schema_name = (table.db or "").lower()
        relation = table.name.lower()
        if not schema_name and relation in cte_names:
            continue
        if schema_name not in ("", SCHEMA) or relation not in QUERYABLE_RELATIONS:
            raise UnsafeSqlError(f"Relation {table.sql(dialect='postgres')} is not part of the {SCHEMA} schema.")

    limit = tree.args.get("limit")
    current = None
    if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
        current = int(limit.expression.this)
    if current is None or current > max_rows:
        if isinstance(tree, exp.Select):
            tree = tree.limit(max_rows)
        else:
            tree = exp.select("*").from_(tree.subquery("q")).limit(max_rows)
    return tree.sql(dialect="postgres")


def run_readonly(sql: str, timeout_ms: int = MART_STATEMENT_TIMEOUT_MS) -> list[dict[str, Any]]:
    """Execute validated SQL as the read-only mart role and return rows as dicts."""
    with psycopg.connect(MART_DATABASE_URL, autocommit=False) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql)
            columns = [desc.name for desc in cur.description or []]
            rows = cur.fetchall()
        conn.rollback()
    return [dict(zip(columns, row)) for row in rows]
