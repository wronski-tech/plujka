from __future__ import annotations

from typing import Any

import psycopg

from api.services.config import DATABASE_URL


def get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_database() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    embedding vector(1536)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS elections (
                    id SERIAL PRIMARY KEY,
                    year INT NOT NULL,
                    type TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id SERIAL PRIMARY KEY,
                    candidate_id INT NOT NULL REFERENCES candidates(id),
                    election_id INT NOT NULL REFERENCES elections(id),
                    votes INT NOT NULL,
                    district TEXT NOT NULL,
                    list_position INT NOT NULL DEFAULT 1
                );
                """
            )


def run_sql(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc.name for desc in cur.description]
            rows = cur.fetchall()
    return [dict(zip(columns, row)) for row in rows]
