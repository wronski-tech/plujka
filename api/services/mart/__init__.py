"""Relational election mart (PostgreSQL schema ``mart``).

Build side (loader, needs DuckDB): ``build_common``, ``build_sejm``, ``build_senat``, ``derived``.
Runtime side (API, psycopg only): ``schema`` (relation list, reader role), ``safe_sql``, ``entities``.

See ``docs/MART.md`` for the data model and ``AGENTS.md`` for contributor rules.
"""
