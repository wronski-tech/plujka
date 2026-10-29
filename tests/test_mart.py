"""Unit tests for the relational mart and the dynamic SQL path (no DB).

Covers the SQL guard, entity matching and clarification, strict staging, header parsers, the
answer number check, the rate limiter, curated SQL examples and the ``try_dynamic_query`` flow with
the LLM and the database stubbed.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import duckdb
import psycopg

from api.services import answer, dynamic_sql
from api.services.mart import entities, examples
from api.services.mart.build_common import parse_blocks_2015, parse_list_position, python_name_key
from api.services.mart.build_referendum import answer_columns
from api.services.mart.safe_sql import UnsafeSqlError, validate_mart_sql
from api.services.rate_limit import SlidingWindowLimiter

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

    def test_referendum_answer_columns_both_layouts(self) -> None:
        # 2023: named columns in any order; 2015: "Pytanie N" then valid, yes, no without names.
        named = answer_columns(["Gmina", "Sprawa 1 - Liczba głosów tak", "Sprawa 1 - Liczba głosów ważnych",
                                "Sprawa 1 - Liczba głosów nie", "Sprawa 1 - Liczba głosów nieważnych"])
        self.assertEqual(named[1]["yes_votes"], "Sprawa 1 - Liczba głosów tak")
        self.assertEqual(named[1]["invalid_votes"], "Sprawa 1 - Liczba głosów nieważnych")
        blocks = answer_columns(["Nr obw.", "Pytanie 1", "c1", "c2", "c3", "Pytanie 2", "c5", "c6", "c7"])
        self.assertEqual(blocks[2], {"invalid_votes": "Pytanie 2", "valid_votes": "c5", "yes_votes": "c6", "no_votes": "c7"})


def _xlsx(path: Path, sheets: list[tuple[str, str, list[list[str]]]]) -> None:
    """Write a minimal XLSX: (title, worksheet file, rows) in workbook order, inline strings."""
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    book = "".join(f'<sheet name="{t}" sheetId="{i}" r:id="rId{i}"/>' for i, (t, _, _) in enumerate(sheets, 1))
    rels = "".join(f'<Relationship Id="rId{i}" Target="worksheets/{f}"/>' for i, (_, f, _) in enumerate(sheets, 1))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", f"<workbook {ns} {rel_ns}><sheets>{book}</sheets></workbook>")
        zf.writestr("xl/_rels/workbook.xml.rels",
                    f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        for _, file_name, rows in sheets:
            body = "".join(
                "<row>" + "".join(f'<c t="inlineStr"><is><t>{v}</t></is></c>' for v in row) + "</row>" for row in rows
            )
            zf.writestr(f"xl/worksheets/{file_name}", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")


class TestMultiSheetStaging(unittest.TestCase):
    def test_one_parquet_per_sheet_in_workbook_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "okregi.xlsx"
            # Workbook order differs from worksheet file order on purpose (sheet N = district N).
            _xlsx(book, [("Okręg 1", "sheet2.xml", [["Nr", "Głosy"], ["1", "10"], ["2", "20"]]),
                         ("Okręg 2", "sheet1.xml", [["Nr", "Głosy"], ["1", "30"]])])
            rows = stage.excel_to_parquet(book, Path(tmp) / "okregi.parquet")
            self.assertEqual(rows, 3)
            files = sorted(Path(tmp).glob("okregi__sheet*.parquet"))
            self.assertEqual([f.name.split("_")[2] for f in files], ["sheet01", "sheet02"])
            con = duckdb.connect()
            first = con.execute(f"SELECT \"Głosy\" FROM read_parquet('{files[0]}') ORDER BY 1").fetchall()
            self.assertEqual(first, [("10",), ("20",)])


class TestAnswerNumbers(unittest.TestCase):
    ROWS = [{"committee_name": "KOMITET WYBORCZY PRAWO I SPRAWIEDLIWOŚĆ", "votes": 7640854, "pct": Decimal("35.38"), "seats": 194}]

    def test_accepts_polish_formatting_and_rounding(self) -> None:
        text = "PiS zdobył 7 640 854 głosów (35,4%) i 194 mandaty w 2023 roku."
        self.assertEqual(answer.unsupported_numbers(text, self.ROWS, "Wynik PiS w 2023?"), [])
        self.assertEqual(answer.unsupported_numbers("Wynik: 7,640,854 i 35.38%.", self.ROWS, ""), [])

    def test_rejects_numbers_not_in_rows(self) -> None:
        # Off by one vote, a computed difference and a year that is in neither the rows nor the question.
        text = "PiS zdobył 7 640 855 głosów, o 4,5 pkt mniej niż w 2019."
        self.assertEqual(answer.unsupported_numbers(text, self.ROWS, "Wynik PiS w 2023?"), ["7 640 855", "4,5", "2019"])

    def test_compose_answer_falls_back_without_llm(self) -> None:
        with patch.object(answer, "OPENAI_API_KEY", ""):
            result = answer.compose_answer("Wynik PiS?", self.ROWS)
        self.assertEqual(result["source"], "fallback")
        self.assertIn("7 640 854", result["text"])
        self.assertIn("year: 2023;", answer.fallback_answer([{"year": 2023, "district_no": 19, "votes": 202424}]))
        with patch.object(answer, "OPENAI_API_KEY", "k"), patch.object(answer, "ANSWER_SUMMARY_ENABLED", True), \
                patch.object(answer, "_llm_answer", return_value=("PiS miał 40% głosów.", {})):
            result = answer.compose_answer("Wynik PiS?", self.ROWS)
        self.assertEqual((result["source"], result["rejected_numbers"]), ("fallback", ["40"]))


class TestRateLimiter(unittest.TestCase):
    def test_sliding_window_per_key(self) -> None:
        limiter = SlidingWindowLimiter(2, window_seconds=60)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("a", now=10))
        self.assertFalse(limiter.allow("a", now=59))
        self.assertTrue(limiter.allow("b", now=59))
        self.assertTrue(limiter.allow("a", now=60.5))  # the event at t=0 left the window
        self.assertFalse(limiter.allow("a", now=61))


def _person(name: str, ref: str) -> entities.EntityMatch:
    return entities.EntityMatch("person", name, ref, "sejm 2023", [2023])


class TestClarification(unittest.TestCase):
    JAROSLAW = _person("Jarosław Aleksander KACZYŃSKI", "1")
    MARTA = _person("Marta KACZYŃSKA", "2")
    LECH = _person("Lech Aleksander KACZYŃSKI", "3")

    def test_given_name_narrows_to_one_person(self) -> None:
        narrowed = entities.narrow_matches("Ile głosów dostał Jarosław Kaczyński?", [self.JAROSLAW, self.LECH])
        self.assertEqual(narrowed, [self.JAROSLAW])
        self.assertIsNone(entities.find_ambiguity(narrowed))

    def test_given_name_is_not_a_place(self) -> None:
        towns = [entities.EntityMatch("gmina", n, r, "powiat jarosławski", [2023]) for n, r in (("gm. Jarosław", "180404"), ("m. Jarosław", "180401"))]
        narrowed = entities.narrow_matches("W których wyborach startował Jarosław Kaczyński?", [*towns, self.JAROSLAW, self.LECH])
        self.assertEqual(narrowed, [self.JAROSLAW])
        self.assertIsNone(entities.find_ambiguity(narrowed))

    def test_surname_only_asks_which_person(self) -> None:
        narrowed = entities.narrow_matches("Ile głosów dostał Kaczyński?", [self.JAROSLAW, self.LECH])
        clarification = entities.find_ambiguity(narrowed)
        self.assertEqual(clarification["kind"], "person")
        self.assertEqual([o["ref"] for o in clarification["options"]], ["1", "3"])

    def test_same_name_gminas_are_ambiguous_but_place_kinds_are_not(self) -> None:
        town = entities.EntityMatch("gmina", "m. Bolesławiec", "020101", "powiat bolesławiecki", [2023])
        rural = entities.EntityMatch("gmina", "gm. Bolesławiec", "020102", "powiat bolesławiecki", [2023])
        powiat = entities.EntityMatch("powiat", "bolesławiecki", "0201", "dolnośląskie", [2023])
        self.assertEqual(entities.find_ambiguity([town, rural])["kind"], "gmina")
        self.assertIsNone(entities.find_ambiguity([town, powiat]))

    def test_surname_that_is_a_place_is_dropped(self) -> None:
        krak = _person("Jan KRAK", "9")
        city = entities.EntityMatch("gmina", "m. Kraków", "126101", "powiat Kraków", [2023])
        self.assertEqual(entities.narrow_matches("Frekwencja w Krakowie", [krak, city]), [city])


class TestCuratedExamples(unittest.TestCase):
    def test_every_curated_example_passes_the_guard(self) -> None:
        seed = examples.load_seed()
        self.assertGreaterEqual(len(seed), 15)
        for example in seed:
            with self.subTest(question=example["question"]):
                validate_mart_sql(example["sql"])
        ids = [examples.example_id(e["question"]) for e in seed]
        self.assertEqual(len(ids), len(set(ids)), "duplicate questions in sql_examples.jsonl")

    def test_local_search_ranks_by_shared_words(self) -> None:
        hits = examples._local_similar("frekwencja w drugiej turze wyborów prezydenckich 2025", k=2)
        self.assertIn("frekwencja", hits[0]["question"].lower())


class TestDynamicQueryFlow(unittest.TestCase):
    """``try_dynamic_query`` with the LLM, the mart context and the database stubbed."""

    GOOD = "SELECT turnout_pct FROM mart.v_turnout WHERE year = 2023 AND level = 'country' LIMIT 5"

    def setUp(self) -> None:
        dynamic_sql.SQL_CACHE._data.clear()
        self.patches = [
            patch.object(dynamic_sql, "build_context", return_value=("ctx", "load-1", [])),
            patch.object(entities, "resolve_entities", return_value=[]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_repair_then_cache(self) -> None:
        def run(sql: str):
            if "turnout_pct" not in sql:
                raise psycopg.errors.UndefinedColumn('column "frekwencja" does not exist')
            return [{"turnout_pct": Decimal("74.38")}]

        llm = [("SELECT frekwencja FROM mart.v_turnout LIMIT 5", {"prompt_tokens": 10}), (self.GOOD, {"prompt_tokens": 12})]
        with patch.object(dynamic_sql, "_llm_sql", side_effect=llm) as fake_llm, \
                patch.object(dynamic_sql.safe_sql, "run_readonly", side_effect=run):
            rows, sql, error, meta = dynamic_sql.try_dynamic_query("Frekwencja w 2023?")
            self.assertEqual((meta["status"], rows, error), ("repaired", [{"turnout_pct": Decimal("74.38")}], None))
            self.assertIn("frekwencja", fake_llm.call_args.kwargs["previous_error"])
            self.assertEqual(meta["tokens"]["prompt_tokens"], 22)
            # Same question (different case/spacing) and same mart load: no LLM call.
            rows, cached_sql, _, meta = dynamic_sql.try_dynamic_query("frekwencja  w 2023?")
            self.assertEqual((meta["status"], cached_sql), ("cached", sql))
            self.assertEqual(fake_llm.call_count, 2)

    def test_unsafe_sql_is_rejected_without_running(self) -> None:
        with patch.object(dynamic_sql, "_llm_sql", return_value=("DELETE FROM mart.persons", {})), \
                patch.object(dynamic_sql.safe_sql, "run_readonly") as run:
            rows, sql, error, meta = dynamic_sql.try_dynamic_query("Usuń dane")
        self.assertEqual((meta["status"], rows, sql), ("rejected", [], None))
        run.assert_not_called()

    def test_ambiguous_person_asks_before_writing_sql(self) -> None:
        people = [TestClarification.JAROSLAW, TestClarification.LECH]
        with patch.object(entities, "resolve_entities", return_value=people), \
                patch.object(dynamic_sql, "_llm_sql") as fake_llm:
            _, _, _, meta = dynamic_sql.try_dynamic_query("Ile głosów dostał Kaczyński?")
            self.assertEqual(meta["status"], "clarify")
            self.assertEqual(len(meta["clarification"]["options"]), 2)
            fake_llm.assert_not_called()
        # The user's choice pins the person; the question is answered.
        with patch.object(entities, "resolve_entities", return_value=people), \
                patch.object(entities, "lookup_refs", return_value=[TestClarification.LECH]), \
                patch.object(dynamic_sql, "_llm_sql", return_value=(self.GOOD, {})), \
                patch.object(dynamic_sql.safe_sql, "run_readonly", return_value=[{"turnout_pct": 1}]):
            _, _, _, meta = dynamic_sql.try_dynamic_query("Ile głosów dostał Kaczyński?", [{"kind": "person", "ref": "3"}])
        self.assertEqual(meta["status"], "first_pass")
        self.assertEqual([m["ref"] for m in meta["entities"]], ["3"])


if __name__ == "__main__":
    unittest.main()
