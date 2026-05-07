"""Unit tests for KBW geography SQL helpers."""

from __future__ import annotations

import unittest

from api.services.kbw_geo import district_expr_sql


class TestDistrictExprSql(unittest.TestCase):
    def test_default_alias_f(self) -> None:
        s = district_expr_sql()
        self.assertIn("f.geography", s)
        self.assertIn("Numer okręgu", s)

    def test_custom_alias(self) -> None:
        s = district_expr_sql("fx")
        self.assertIn("fx.geography", s)
        self.assertNotIn("f.geography", s)


if __name__ == "__main__":
    unittest.main()
