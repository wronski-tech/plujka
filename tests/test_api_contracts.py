"""Pydantic response models for FastAPI (no server startup required)."""

from __future__ import annotations

import unittest

from api.main import (
    AskResponse,
    FeedbackOkResponse,
    HealthResponse,
    KbwCatalogSummaryResponse,
    QuestionHintsResponse,
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


if __name__ == "__main__":
    unittest.main()
