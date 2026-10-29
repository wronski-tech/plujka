"""Pydantic response models for FastAPI (no server startup required)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import (
    AskResponse,
    FeedbackOkResponse,
    HealthResponse,
    KbwCatalogSummaryResponse,
    QuestionHintsResponse,
    app,
)


class TestApiResponseModels(unittest.TestCase):
    def test_health_response_minimal(self) -> None:
        h = HealthResponse.model_validate({"status": "ok", "data_ready": False})
        self.assertIsNone(h.kbw_stats)

    def test_health_response_with_stats(self) -> None:
        h = HealthResponse.model_validate(
            {"status": "ok", "data_ready": True, "kbw_stats": {"kbw_facts": 42}}
        )
        self.assertEqual(h.kbw_stats["kbw_facts"], 42)

    def test_ask_response_with_geo_meta(self) -> None:
        r = AskResponse.model_validate(
            {
                "question": "q",
                "intent": "kbw_candidate_geo_votes_detail",
                "entity": "Kowalski",
                "year": 2099,
                "years": [],
                "sql": "SELECT 1",
                "params": {},
                "result": [{"votes": 1}],
                "candidate_geo_source": "kbw_facts",
            }
        )
        self.assertEqual(r.candidate_geo_source, "kbw_facts")

    def test_ask_response_mandate_meta(self) -> None:
        r = AskResponse.model_validate(
            {
                "question": "q",
                "intent": "kbw_sejm_mandate_vote_extremes",
                "entity": None,
                "year": 2023,
                "years": [],
                "sql": "SELECT 1",
                "params": {},
                "result": [],
                "mandate_extremes_source": "kbw_fallback",
            }
        )
        self.assertEqual(r.mandate_extremes_source, "kbw_fallback")

    def test_feedback_ok(self) -> None:
        self.assertTrue(FeedbackOkResponse().ok)

    def test_question_hints_empty(self) -> None:
        h = QuestionHintsResponse(text_hits=[], semantic_hits=[])
        self.assertEqual(h.text_hits, [])

    def test_catalog_summary_shape(self) -> None:
        s = KbwCatalogSummaryResponse(
            total_files=0, by_year={}, by_file_kind={}
        )
        self.assertEqual(s.total_files, 0)


class TestAskEndpoint(unittest.TestCase):
    """``POST /ask`` with the router stubbed (no lifespan, no database)."""

    ROUTED = {
        "question": "q", "intent": "dynamic_sql", "sql": "SELECT 1", "params": {},
        "result": [{"party": "PiS", "pct": Decimal("34.27"), "votes": Decimal("7640854"), "year": 2023}],
    }

    def setUp(self) -> None:
        self.patches = [
            patch("api.main.router.route_question", return_value=dict(self.ROUTED)),
            patch("api.main.answer.compose_answer", return_value={"text": "t", "source": "fallback"}),
            patch("api.main.opensearch_store.log_question"),
        ]
        self.route = self.patches[0].start()
        for p in self.patches[1:]:
            p.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()

    def test_numbers_are_json_numbers_and_previous_turn_is_passed(self) -> None:
        previous = {"question": "Frekwencja w Krakowie w 2023?", "sql": "SELECT 2"}
        response = self.client.post("/ask", json={"question": "A w 2019?", "previous": previous})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], [{"party": "PiS", "pct": 34.27, "votes": 7640854, "year": 2023}])
        self.assertEqual(self.route.call_args.kwargs["previous"], {**previous, "entity_refs": None})

    def test_api_keys_when_configured(self) -> None:
        with patch("api.main.config.API_KEYS", frozenset({"secret-1"})):
            self.assertEqual(self.client.post("/ask", json={"question": "q"}).status_code, 401)
            bad = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "nope"})
            self.assertEqual(bad.status_code, 401)
            good = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "secret-1"})
            self.assertEqual(good.status_code, 200)
            self.assertEqual(self.client.post("/feedback", json={"rating": "thumbs_up", "question": "q"}).status_code, 401)
        self.assertEqual(self.client.post("/ask", json={"question": "q"}).status_code, 200)

    def test_daily_token_budget_per_key(self) -> None:
        from api.services.rate_limit import TokenBudget

        routed = {**self.ROUTED, "params": {"_dynamic_sql_meta": {"tokens": {"prompt_tokens": 60, "completion_tokens": 50}}}}
        self.route.return_value = routed
        with patch("api.main.config.API_KEYS", frozenset({"k1", "k2"})), patch("api.main.token_budget", TokenBudget(100)):
            first = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"})
            second = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"})
            other = self.client.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k2"})
        # The first question may use the budget up (110 >= 100); the next one from that key is refused.
        self.assertEqual((first.status_code, second.status_code, other.status_code), (200, 429, 200))
        self.assertIn("token", second.json()["detail"])


if __name__ == "__main__":
    unittest.main()
