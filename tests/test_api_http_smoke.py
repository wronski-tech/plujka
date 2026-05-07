"""HTTP smoke tests via FastAPI TestClient (PostgreSQL required).

Skip unless ``PLUJKA_RUN_DB_TESTS=1``. CI integration job sets this. Lifespan runs
``db.init_database``; OpenSearch is not required — ``ensure_index`` / ``log_question``
are patched for these tests.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

_RUN = os.environ.get("PLUJKA_RUN_DB_TESTS", "").strip().lower() in ("1", "true", "yes")

_ROUTER_STUB = {
    "question": "smoke",
    "intent": "kbw_party_vote_share",
    "entity": None,
    "year": 2099,
    "years": [],
    "sql": "SELECT 1 AS ok",
    "params": {},
    "result": [{"ok": 1}],
}


@unittest.skipUnless(_RUN, "Set PLUJKA_RUN_DB_TESTS=1 to run HTTP smoke tests")
class TestApiHttpSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._patch_ensure = patch("api.main.opensearch_store.ensure_index", lambda: None)
        cls._patch_ensure.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._patch_ensure.stop()

    def test_get_health(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import app

        with TestClient(app) as client:
            response = client.get("/health")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body.get("status"), "ok")
            self.assertIn("data_ready", body)

    def test_get_health_details(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import app

        with TestClient(app) as client:
            response = client.get("/health", params={"details": "true"})
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body.get("status"), "ok")
            self.assertIn("kbw_stats", body)

    def test_get_kbw_catalog_summary(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import app

        with TestClient(app) as client:
            response = client.get("/kbw/catalog/summary")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertIn("total_files", body)
            self.assertIn("by_year", body)
            self.assertIn("by_file_kind", body)

    def test_post_ask_stubbed_router(self) -> None:
        from fastapi.testclient import TestClient

        from api.main import app

        with patch("api.main.router.route_question", return_value=_ROUTER_STUB):
            with patch("api.main.opensearch_store.log_question", lambda *a, **k: None):
                with TestClient(app) as client:
                    response = client.post("/ask", json={"question": "smoke test"})
                    self.assertEqual(response.status_code, 200)
                    body = response.json()
                    self.assertEqual(body.get("intent"), "kbw_party_vote_share")
                    self.assertEqual(body.get("result"), [{"ok": 1}])


if __name__ == "__main__":
    unittest.main()
