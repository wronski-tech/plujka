"""PostgreSQL checks of the read-only mart role (``PLUJKA_RUN_DB_TESTS=1``; needs CREATEROLE on DATABASE_URL)."""

from __future__ import annotations

import os
import unittest

_RUN = os.environ.get("PLUJKA_RUN_DB_TESTS", "").strip().lower() in ("1", "true", "yes")


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestMartReaderRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        from api.services.config import DATABASE_URL
        from api.services.mart import schema

        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS public.mart_role_probe (x int)")
            schema.ensure_schema(conn)
            schema.ensure_reader_role(conn, statement_timeout_ms=500)

    def test_reader_is_limited_to_read_only_mart(self) -> None:
        import psycopg

        from api.services.mart.safe_sql import run_readonly

        self.assertGreaterEqual(run_readonly("SELECT count(*) AS n FROM mart.persons", timeout_ms=500)[0]["n"], 0)
        denied = {
            "SELECT * FROM public.mart_role_probe": psycopg.errors.InsufficientPrivilege,
            "UPDATE mart.persons SET gender = gender": psycopg.errors.ReadOnlySqlTransaction,
            "SELECT pg_sleep(2)": psycopg.errors.QueryCanceled,
        }
        for sql, error in denied.items():
            with self.subTest(sql=sql), self.assertRaises(error):
                run_readonly(sql, timeout_ms=500)


if __name__ == "__main__":
    unittest.main()
