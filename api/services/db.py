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
                    type TEXT NOT NULL,
                    UNIQUE (year, type)
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS elected_candidates (
                    id SERIAL PRIMARY KEY,
                    year INT NOT NULL,
                    district TEXT NOT NULL,
                    committee_name TEXT NOT NULL,
                    candidate_name TEXT NOT NULL,
                    candidate_votes INT NOT NULL,
                    list_position INT,
                    UNIQUE (year, district, committee_name, candidate_name)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS source_files (
                    id SERIAL PRIMARY KEY,
                    election_key TEXT NOT NULL,
                    year INT,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    column_count INT NOT NULL,
                    header JSONB NOT NULL,
                    profiled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS source_columns (
                    id SERIAL PRIMARY KEY,
                    source_file_id INT NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
                    column_index INT NOT NULL,
                    column_name TEXT NOT NULL,
                    is_committee BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE (source_file_id, column_index)
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


def get_latest_election_year(election_type: str = "sejm") -> int | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(year) FROM elections WHERE type = %s", (election_type,))
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def get_latest_elected_candidates_year() -> int | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(year) FROM elected_candidates")
            row = cur.fetchone()
    return row[0] if row and row[0] is not None else None
