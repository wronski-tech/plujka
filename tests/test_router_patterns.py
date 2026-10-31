"""Small router helpers (no DB)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.services import definitions, router, scope
from api.services.router import _gmina_ilike_from_question, _kbw_geo_pattern_from_question


class TestGminaPattern(unittest.TestCase):
    def test_w_gminie(self) -> None:
        p = _gmina_ilike_from_question("wyniki w gminie Zakroczym 2023")
        self.assertEqual(p, "%Zakroczym%")

    def test_missing(self) -> None:
        self.assertIsNone(_gmina_ilike_from_question("głosy PiS 2023"))


class TestKbwGeoPattern(unittest.TestCase):
    def test_wroclawiu_stems_to_wroclaw(self) -> None:
        self.assertEqual(
            _kbw_geo_pattern_from_question("wynik ko we Wrocławiu"),
            "%wroclaw%",
        )

    def test_krakowie_stems_to_krakow(self) -> None:
        self.assertEqual(_kbw_geo_pattern_from_question("głosy w Krakowie"), "%krakow%")

    def test_nominative_after_w(self) -> None:
        self.assertEqual(_kbw_geo_pattern_from_question("wynik ko w Wrocław"), "%wroclaw%")


class TestMartOnlyElections(unittest.TestCase):
    def test_non_sejm_election_skips_sejm_templates(self) -> None:
        # "mandatów" would match the Sejm elected-candidates template; a sejmik question must not.
        rows = [{"seats": 239}]
        meta = {"status": "first_pass"}
        with patch.object(router, "try_dynamic_query", return_value=(rows, "SELECT 239", None, meta)) as dyn, \
                patch.object(router, "extract_intent_and_entity", side_effect=AssertionError("template routing used")):
            response = router.route_question("Ile mandatów w sejmikach zdobyło PiS w 2024?")
        self.assertEqual((response["intent"], response["result"]), ("dynamic_sql", rows))
        dyn.assert_called_once()

    def test_gus_question_without_rows_skips_sejm_templates(self) -> None:
        empty = ([], "SELECT 1 WHERE false", None, {"status": "first_pass"})
        with patch.object(router, "try_dynamic_query", return_value=empty), \
                patch.object(router, "_route_templates", side_effect=AssertionError("template routing used")):
            for question in ("Bezrobocie a wynik PiS w powiatach w 2023", "Who won the presidential election in 2015?"):
                response = router.route_question(question)
                self.assertEqual(response["intent"], "dynamic_sql")


class TestDefinitions(unittest.TestCase):
    def test_definition_questions_are_answered_from_the_docs(self) -> None:
        with patch.object(router, "try_dynamic_query", side_effect=AssertionError("model asked")):
            response = router.route_question("Czym różni się metoda D'Hondta od Sainte-Laguë?")
            english = router.route_question("What is the electoral threshold?")
        self.assertEqual(response["intent"], "definition")
        self.assertEqual(response["answer"]["sources"],
                         ["docs/DEFINITIONS.md#metoda-dhondta", "docs/DEFINITIONS.md#metoda-sainte-laguë"])
        self.assertIn("1, 3, 5", response["answer"]["text"])
        self.assertTrue(english["answer"]["text"].startswith("The electoral threshold"))

    def test_data_questions_still_go_to_sql(self) -> None:
        # A year, a place ("in Warsaw") or no request for a meaning: the rows answer it, not the docs.
        for question in ("Jaka była frekwencja w 2023?", "What is the turnout in Warsaw?", "Jakie komitety przekroczyły próg?",
                         "Co to jest frekwencja w wyborach 2019?", "Kto to jest wójt Gdowa?"):
            with self.subTest(question=question):
                self.assertEqual(definitions.find_definitions(question), [])


class TestOutOfScope(unittest.TestCase):
    def test_polls_forecasts_and_future_results_are_refused_without_the_model(self) -> None:
        with patch.object(router, "try_dynamic_query", side_effect=AssertionError("model asked")):
            for question in ("Kto wygra wybory w 2027?", "Ile głosów dostanie PiS?", "Najnowsze sondaże", "Will Konfederacja enter the Sejm?"):
                with self.subTest(question=question):
                    response = router.route_question(question)
                    self.assertEqual((response["intent"], response["answer"]["source"], response["sql"]), ("out_of_scope", "refusal", ""))
        self.assertTrue(router.route_question("Who will win the next election?")["answer"]["text"].startswith("I can't answer"))

    def test_past_results_and_seat_simulations_are_answered(self) -> None:
        # Past tense ("wygrał", "dostał"), the seat simulator's conditional and look-alike place names.
        for question in ("Kto wygrał wybory w 2023?", "Ile mandatów dostał PiS?", "Ile mandatów miałby PiS metodą Sainte-Laguë?",
                         "Gdyby PiS i Konfederacja szły razem, ile mandatów by miały?", "Wyniki w gminie Wygoda"):
            with self.subTest(question=question):
                self.assertFalse(scope.is_out_of_scope(question))


class TestTemplateFallback(unittest.TestCase):
    SKIPPED = ([], None, "OPENAI_API_KEY is not set", {"status": "skipped"})

    def test_templates_answer_when_dynamic_sql_has_nothing(self) -> None:
        legacy = {"intent": "sejm_votes_by_gmina", "params": {"year": 2023}, "result": [{"votes": 1}]}
        with patch.object(router, "try_dynamic_query", return_value=self.SKIPPED), \
                patch.object(router, "_route_templates", return_value=legacy):
            response = router.route_question("Wyniki list według gmin 2023")
        self.assertEqual((response["intent"], response["result"]), ("sejm_votes_by_gmina", [{"votes": 1}]))
        self.assertEqual(response["params"]["year"], 2023)
        self.assertEqual(response["params"]["_dynamic_sql_meta"], {"status": "skipped"})

    def test_follow_up_never_falls_back_to_templates(self) -> None:
        previous = {"question": "Frekwencja w Krakowie w 2023?", "sql": "SELECT 1"}
        with patch.object(router, "try_dynamic_query", return_value=self.SKIPPED) as dyn, \
                patch.object(router, "_route_templates", side_effect=AssertionError("template routing used")):
            response = router.route_question("A w 2019?", previous=previous)
        self.assertEqual((response["intent"], response["result"]), ("dynamic_sql", []))
        self.assertEqual(dyn.call_args.args[2], previous)


if __name__ == "__main__":
    unittest.main()
