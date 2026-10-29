"""Small router helpers (no DB)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.services import router
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


if __name__ == "__main__":
    unittest.main()
