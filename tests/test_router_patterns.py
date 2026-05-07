"""Small router helpers (no DB)."""

from __future__ import annotations

import unittest

from api.services.router import _gmina_ilike_from_question


class TestGminaPattern(unittest.TestCase):
    def test_w_gminie(self) -> None:
        p = _gmina_ilike_from_question("wyniki w gminie Zakroczym 2023")
        self.assertEqual(p, "%Zakroczym%")

    def test_missing(self) -> None:
        self.assertIsNone(_gmina_ilike_from_question("głosy PiS 2023"))


if __name__ == "__main__":
    unittest.main()
