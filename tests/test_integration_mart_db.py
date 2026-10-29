"""PostgreSQL checks of the mart (``PLUJKA_RUN_DB_TESTS=1``; needs CREATEROLE on DATABASE_URL).

The read-only role must deny writes, other schemas and long queries; person linking must split
namesakes by birth year and apply manual overrides. The linking test fills the candidate tables
inside a transaction that is rolled back, so a built mart is left untouched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

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


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestPersonLinking(unittest.TestCase):
    CANDIDATE_TABLES = ("sejm_candidates", "senat_candidates", "prezydent_candidates", "pe_candidates",
                        "sejmik_candidates", "wbp_candidates", "rada_candidates")

    def _link(self, conn, overrides: str) -> None:
        from api.services.mart import derived

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "person_overrides.csv"
            path.write_text("election,year,full_name,place,person,note\n" + overrides, encoding="utf-8")
            derived.link_persons(conn, path)

    def _fixture(self, conn) -> None:
        from api.services.mart import schema

        schema.ensure_schema(conn)
        conn.execute("TRUNCATE " + ", ".join(f"mart.{t}" for t in self.CANDIDATE_TABLES))
        sejm = [  # year, id, district, full name, last, first, gender
            (2019, 1, 1, "Jan KOWALSKI", "KOWALSKI", "Jan", "Mężczyzna"),
            (2023, 1, 5, "Jan KOWALSKI", "KOWALSKI", "Jan", "Mężczyzna"),
            (2023, 2, 3, "Anna NOWAK", "NOWAK", "Anna", "Kobieta"),
            (2015, 3, 7, "Marek LIS", "LIS", "Marek", "Mężczyzna"),
            (2019, 4, 2, "Ewa MALINOWSKA", "MALINOWSKA", "Ewa", "Kobieta"),
        ]
        conn.cursor().executemany(
            "INSERT INTO mart.sejm_candidates (year, candidate_id, district_no, committee_id, list_no, list_position,"
            " full_name, last_name, first_names, gender, votes, elected) VALUES (%s, %s, %s, 1, 1, 1, %s, %s, %s, %s, 10, false)",
            sejm,
        )
        wbp = [(2018, 1, "020101", "Anna NOWAK", "NOWAK", "Anna", "Kobieta", 40),
               (2018, 2, "020102", "Marek LIS", "LIS", "Marek", "Mężczyzna", 50)]
        conn.cursor().executemany(
            "INSERT INTO mart.wbp_candidates (year, round, candidate_id, teryt_gmina, full_name, last_name, first_names,"
            " gender, age, elected) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, false)",
            wbp,
        )
        rada = [(2024, 1, "020103", "Anna NOWAK", "NOWAK", "Anna", "Kobieta", 48),
                (2024, 2, "020104", "Marek LIS", "LIS", "Marek", "Mężczyzna", 55),
                (2018, 3, "020105", "Ewa WIŚNIEWSKA", "WIŚNIEWSKA", "Ewa", "Kobieta", 44)]
        conn.cursor().executemany(
            "INSERT INTO mart.rada_candidates (year, candidate_id, council_level, teryt, district_no, committee_id,"
            " full_name, last_name, first_names, gender, age, elected) VALUES (%s, %s, 'gmina', %s, 1, 1, %s, %s, %s, %s, %s, false)",
            rada,
        )

    def _person(self, conn, table: str, year: int, name: str):
        return conn.execute(f"SELECT person_id FROM mart.{table} WHERE year = %s AND full_name = %s", (year, name)).fetchone()[0]

    def test_birth_years_split_namesakes_and_overrides_win(self) -> None:
        import psycopg

        from api.services.config import DATABASE_URL

        with psycopg.connect(DATABASE_URL) as conn:
            try:
                self._fixture(conn)
                self._link(
                    conn,
                    "sejm,2023,Jan KOWALSKI,5,jan-2023,different person than in 2019\n"
                    "sejm,2019,Ewa MALINOWSKA,,ewa,changed surname\n"
                    "rada,2018,Ewa WIŚNIEWSKA,020105,ewa,changed surname\n",
                )
                jan_2019 = self._person(conn, "sejm_candidates", 2019, "Jan KOWALSKI")
                jan_2023 = self._person(conn, "sejm_candidates", 2023, "Jan KOWALSKI")
                self.assertNotEqual(jan_2019, jan_2023)
                rule = conn.execute("SELECT match_rule FROM mart.persons WHERE person_id = %s", (jan_2023,)).fetchone()[0]
                self.assertEqual(rule, "manual")
                # Marek: born 1968 (wbp 2018, age 50) and 1969 (council 2024, age 55): one person.
                marek = self._person(conn, "sejm_candidates", 2015, "Marek LIS")
                self.assertEqual(self._person(conn, "wbp_candidates", 2018, "Marek LIS"), marek)
                self.assertEqual(self._person(conn, "rada_candidates", 2024, "Marek LIS"), marek)
                # Anna: born 1978 and 1976 -> two people with one name; neither local candidacy is linked.
                self.assertIsNone(self._person(conn, "wbp_candidates", 2018, "Anna NOWAK"))
                self.assertIsNone(self._person(conn, "rada_candidates", 2024, "Anna NOWAK"))
                # Override links a local candidacy under another surname to the national person.
                ewa = self._person(conn, "sejm_candidates", 2019, "Ewa MALINOWSKA")
                self.assertEqual(self._person(conn, "rada_candidates", 2018, "Ewa WIŚNIEWSKA"), ewa)
            finally:
                conn.rollback()

    def test_override_matching_nothing_fails(self) -> None:
        import psycopg

        from api.services.config import DATABASE_URL

        with psycopg.connect(DATABASE_URL) as conn:
            try:
                self._fixture(conn)
                with self.assertRaisesRegex(ValueError, r"lines \[3\]"):
                    self._link(conn, "sejm,2019,Jan KOWALSKI,1,jan,ok\nsejm,2019,Jan KOWALSKY,1,jan,typo\n")
            finally:
                conn.rollback()


if __name__ == "__main__":
    unittest.main()
