"""Unit tests for the relational mart: SQL guard, entity matching, strict staging, header parsers (no DB)."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import duckdb

from api.services.mart import entities
from api.services.mart.build_common import parse_blocks_2015, parse_list_position, python_name_key
from api.services.mart.safe_sql import UnsafeSqlError, validate_mart_sql

_spec = importlib.util.spec_from_file_location(
    "kbw_stage_duckdb", Path(__file__).resolve().parents[1] / "scripts" / "kbw_stage_duckdb.py"
)
stage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stage)


class TestSafeSql(unittest.TestCase):
    def test_accepts_mart_select_and_caps_limit(self) -> None:
        sql = validate_mart_sql(
            "WITH t AS (SELECT * FROM mart.v_sejm_turnout WHERE year = 2023) SELECT * FROM t LIMIT 100000",
            max_rows=50,
        )
        self.assertIn("LIMIT 50", sql)
        self.assertNotIn("100000", sql)
        # A smaller LIMIT is kept; a UNION gets an outer LIMIT.
        self.assertIn("LIMIT 5", validate_mart_sql("SELECT full_name FROM persons LIMIT 5", max_rows=50))
        union = validate_mart_sql("SELECT 1 FROM mart.persons UNION SELECT 2 FROM mart.persons", max_rows=50)
        self.assertTrue(union.rstrip().endswith("LIMIT 50"))

    def test_rejects_unsafe_queries(self) -> None:
        unsafe = [
            "DELETE FROM mart.persons",
            "SELECT 1; DROP TABLE mart.persons",
            "SELECT * FROM public.kbw_facts",
            "SELECT * FROM kbw_facts",
            "SELECT * FROM pg_catalog.pg_roles",
            "SELECT set_config('role', 'plujka', false)",
            "SELECT pg_sleep(10)",
            "SELECT * INTO mart.copy FROM mart.persons",
            "WITH x AS (DELETE FROM mart.persons RETURNING *) SELECT * FROM x",
            "SELECT * FROM mart.persons p JOIN information_schema.tables t ON true",
        ]
        for sql in unsafe:
            with self.subTest(sql=sql), self.assertRaises(UnsafeSqlError):
                validate_mart_sql(sql)


class TestEntityTerms(unittest.TestCase):
    def matches(self, question: str, kind: str, name: str) -> bool:
        stems, exact = entities.question_terms(question)
        return entities.score_entry(kind, entities.fold(name), stems, exact)

    def test_inflected_place_names(self) -> None:
        self.assertTrue(self.matches("Wyniki w Krakowie", "gmina", "Kraków"))
        self.assertTrue(self.matches("frekwencja w Białymstoku", "gmina", "Białystok"))
        self.assertTrue(self.matches("Nowym Sączu", "gmina", "Nowy Sącz"))

    def test_false_friends_do_not_match(self) -> None:
        self.assertFalse(self.matches("Wyniki w Krakowie", "gmina", "Krapkowice"))
        self.assertFalse(self.matches("now i wtedy", "person", "Nowak"))
        # Committee aliases must appear as whole words: "PiS" is not found inside "Pisz".
        self.assertTrue(self.matches("Ile głosów miał PiS?", "committee", "PiS"))
        self.assertFalse(self.matches("gmina Pisz", "committee", "PiS"))


class TestStrictStaging(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "t.parquet"
        self.con = duckdb.connect()

    def tearDown(self) -> None:
        self.con.close()
        self.tmp.cleanup()

    def test_semicolon_cp1250_and_quoted_newline(self) -> None:
        data = 'Nr;Siedziba;Wyborcy\n1;"Łódź,\nul. Długa";100\n2;Kraków;200\n'.encode("cp1250")
        self.assertEqual(stage.csv_to_parquet(self.con, data, self.out), 2)
        rows = self.con.execute(f"SELECT * FROM read_parquet('{self.out}') ORDER BY 1").fetchall()
        self.assertEqual(rows[0], ("1", "Łódź,\nul. Długa", "100"))

    def test_malformed_row_fails_without_parquet(self) -> None:
        data = b"a;b\n1;2\n3;4;5;6\n"
        with self.assertRaises(stage.StagingError):
            stage.csv_to_parquet(self.con, data, self.out)
        self.assertFalse(self.out.exists())

    def test_unique_headers(self) -> None:
        self.assertEqual(stage.unique_headers(["Typ", "Nr okr.\nSejm", "Typ", ""]), ["Typ", "Nr okr. Sejm", "Typ_1", "column3"])


class TestHeaderParsers(unittest.TestCase):
    def test_list_position_headers(self) -> None:
        refs = parse_list_position(
            ["Gmina", "Lista nr 2 - KOMITET X", "Jan KOWALSKI - nr na liście 1", "Lista nr 4 - KOMITET Y", "Anna NOWAK - nr na liście 3"]
        )
        self.assertEqual((refs["Anna NOWAK - nr na liście 3"].list_no, refs["Anna NOWAK - nr na liście 3"].position), (4, 3))
        self.assertEqual(len(refs), 2)

    def test_2015_blocks_assigned_by_name_overlap(self) -> None:
        # Block order differs from list numbers on purpose; assignment must follow candidate names.
        columns = ["Gmina", "KW B", "Anna NOWAK", "Ewa LIS", "Razem KW B", "KW A", "Jan KOWALSKI", "Razem KW A"]
        list_names = {1: {python_name_key("Jan KOWALSKI")}, 7: {python_name_key("Anna NOWAK"), python_name_key("Ewa LIS")}}
        refs, totals = parse_blocks_2015(columns, list_names)
        self.assertEqual(totals, {"Razem KW B": 7, "Razem KW A": 1})
        self.assertEqual(refs["Jan KOWALSKI"].list_no, 1)
        self.assertNotIn("Gmina", refs)


if __name__ == "__main__":
    unittest.main()
