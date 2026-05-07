"""Sanity checks on parameterized analytics SQL (no database required)."""

from __future__ import annotations

import unittest

from api.services import db, kbw_analytics


class TestKbwAnalyticsSqlBuilders(unittest.TestCase):
    def test_db_exports_candidate_sync(self) -> None:
        self.assertTrue(hasattr(db, "sync_kbw_candidates_from_person_facts"))

    def test_db_exports_geo_votes_backfill(self) -> None:
        self.assertTrue(hasattr(db, "backfill_kbw_candidate_geo_votes_from_facts"))

    def test_db_exports_health_snapshot(self) -> None:
        self.assertTrue(hasattr(db, "kbw_health_snapshot"))

    def test_db_exports_catalog_summary(self) -> None:
        self.assertTrue(hasattr(db, "kbw_dane_files_catalog_summary"))

    def test_committee_gap_uses_view(self) -> None:
        sql, params = kbw_analytics.sql_committee_gap_by_district(
            year=2023,
            left_pattern="%Platforma%",
            right_pattern="%PiS%",
            prefer_csv_sources=True,
        )
        self.assertIn("kbw_v_sejm_district_list_agg", sql)
        self.assertEqual(params["year"], 2023)
        self.assertTrue(params["prefer_csv"])

    def test_mandate_kbw_fallback_sql(self) -> None:
        sql, params = kbw_analytics.sql_sejm_mandate_vote_extremes_from_kbw_facts(year=2023)
        self.assertIn("cand_nat", sql)
        self.assertIn("kbw_national_min", sql)
        self.assertEqual(params["year"], 2023)

    def test_mandate_pkw_sql(self) -> None:
        sql, params = kbw_analytics.sql_sejm_mandate_vote_extremes(year=2019)
        self.assertIn("elected_candidates", sql)
        self.assertIn("sejm_candidate_ballots", sql)
        self.assertEqual(params["year"], 2019)

    def test_candidate_geo_votes_sql(self) -> None:
        sql, params = kbw_analytics.sql_candidate_geo_votes_detail(
            year=2023,
            candidate_pattern="%Kowalski%",
            gmina_pattern="%Warszawa%",
            limit=40,
        )
        self.assertIn("kbw_candidate_geo_votes", sql)
        self.assertEqual(params["limit"], 40)
        self.assertEqual(params["gmina_pattern"], "%Warszawa%")

    def test_candidate_geo_from_facts_sql(self) -> None:
        sql, params = kbw_analytics.sql_candidate_geo_votes_detail_from_facts(
            year=2023,
            candidate_pattern="%Nowak%",
            gmina_pattern=None,
            limit=25,
        )
        self.assertIn("kbw_facts", sql)
        self.assertIn("kandydat", sql)
        self.assertEqual(params["limit"], 25)


if __name__ == "__main__":
    unittest.main()
