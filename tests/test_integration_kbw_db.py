"""Optional PostgreSQL integration checks.

Enable with::

    PLUJKA_RUN_DB_TESTS=1 PYTHONPATH=. python3 -m unittest tests.test_integration_kbw_db -v

Uses ``DATABASE_URL`` (see ``api/services/config.py`` default).
"""

from __future__ import annotations

import os
import unittest

_RUN = os.environ.get("PLUJKA_RUN_DB_TESTS", "").strip().lower() in ("1", "true", "yes")


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run DB integration tests")
class TestKbwDatabaseIntegration(unittest.TestCase):
    def test_init_and_analytics_view(self) -> None:
        from api.services import db

        db.init_database()
        rows = db.run_sql("SELECT 1 AS ok FROM kbw_v_sejm_district_list_agg LIMIT 1", {})
        self.assertIsInstance(rows, list)

    def test_backfill_person_facts_returns_int(self) -> None:
        from api.services import db

        db.init_database()
        n = db.backfill_kbw_person_election_facts(year=None)
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    def test_sync_candidates_returns_int(self) -> None:
        from api.services import db

        db.init_database()
        n = db.sync_kbw_candidates_from_person_facts(year=None)
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    def test_geo_votes_backfill_returns_int(self) -> None:
        from api.services import db

        db.init_database()
        n = db.backfill_kbw_candidate_geo_votes_from_facts(year=None)
        self.assertIsInstance(n, int)
        self.assertGreaterEqual(n, 0)

    def test_catalog_summary_structure(self) -> None:
        from api.services import db

        db.init_database()
        summary = db.kbw_dane_files_catalog_summary()
        self.assertIn("total_files", summary)
        self.assertIn("by_year", summary)
        self.assertIn("by_file_kind", summary)
        self.assertIsInstance(summary["total_files"], int)
        self.assertGreaterEqual(summary["total_files"], 0)


if __name__ == "__main__":
    unittest.main()
