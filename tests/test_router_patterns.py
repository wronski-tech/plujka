"""Small router helpers (no DB)."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
