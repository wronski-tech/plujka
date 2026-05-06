from __future__ import annotations

import threading
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from api.services import config, db, feedback_store, opensearch_store, router, seed

app = FastAPI(title="Plujka API", version="0.1.0")


class AskRequest(BaseModel):
    question: str


class FeedbackRequest(BaseModel):
    rating: Literal["thumbs_up", "thumbs_down"]
    question: str
    ask_response: dict[str, Any] | None = None


class QuestionHintsRequest(BaseModel):
    q: str
    limit: int = Field(default=8, ge=1, le=20)
    exclude_question: str | None = None


def _run_seed_background() -> None:
    """Run seed background."""
    try:
        seed.seed_if_empty(force=config.FORCE_RESEED)
    except Exception:
        # Logged by uvicorn if unhandled; keep thread alive for ops
        import logging

        logging.exception("Background seed failed")


@app.on_event("startup")
def startup() -> None:
    """Startup."""
    db.init_database()
    opensearch_store.ensure_index()
    threading.Thread(target=_run_seed_background, name="seed", daemon=True).start()


@app.get("/health")
def health() -> dict[str, str | bool]:
    """Health."""
    return {"status": "ok", "data_ready": seed.seed_complete.is_set()}


@app.post("/reseed")
def reseed(x_reseed_token: str | None = Header(None, alias="X-Reseed-Token")) -> dict[str, str | bool]:
    """Reload KBW mirror facts from data/kbw_mirror (same as FORCE_RESEED on startup). Requires RESEED_TOKEN."""
    if not config.RESEED_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Reseed is disabled. Set environment variable RESEED_TOKEN to enable.",
        )
    if not x_reseed_token or x_reseed_token.strip() != config.RESEED_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Reseed-Token header.")

    threading.Thread(target=seed.reseed_from_disk, name="reseed", daemon=True).start()
    return {"ok": True, "message": "Reseed started; poll GET /health until data_ready is true."}


@app.post("/ask")
def ask(request: AskRequest) -> dict:
    """Ask."""
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question is required.")

    result = router.route_question(request.question)
    opensearch_store.log_question(
        question=request.question,
        detected_intent=result["intent"],
        sql=result["sql"],
        params=result["params"],
    )
    return result


@app.post("/feedback")
def feedback(request: FeedbackRequest) -> dict[str, bool]:
    """Feedback."""
    if request.rating == "thumbs_down":
        if not request.ask_response:
            raise HTTPException(
                status_code=400,
                detail="ask_response is required for thumbs_down.",
            )
        feedback_store.append_feedback(
            {
                "rating": request.rating,
                "question": request.question.strip(),
                "needs_fix": True,
                "ask_response": request.ask_response,
            }
        )
    else:
        feedback_store.append_feedback(
            {
                "rating": request.rating,
                "question": request.question.strip(),
                "needs_fix": False,
            }
        )
    return {"ok": True}


@app.post("/question-hints")
def question_hints(body: QuestionHintsRequest) -> dict[str, Any]:
    """Full-text and kNN suggestions over logged questions (OpenSearch)."""
    q = body.q.strip()
    if len(q) < 2:
        return {"text_hits": [], "semantic_hits": []}

    limit = body.limit
    exclude = (body.exclude_question or "").strip().lower()

    def _drop_excluded(hits: list[dict]) -> list[dict]:
        """Drop excluded."""
        if not exclude:
            return hits
        return [h for h in hits if (h.get("question") or "").strip().lower() != exclude]

    text_hits = _drop_excluded(opensearch_store.search_hints_text(q, limit))

    semantic_hits: list[dict] = []
    if len(q) >= config.QUESTION_HINTS_SEMANTIC_MIN_CHARS:
        semantic_hits = _drop_excluded(opensearch_store.search_hints_semantic(q, limit))

    return {
        "text_hits": text_hits[:limit],
        "semantic_hits": semantic_hits[:limit],
    }
