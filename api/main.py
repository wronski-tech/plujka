from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from api.services import config, db, feedback_store, opensearch_store, router

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness probe and approximate KBW table stats"},
    {"name": "catalog", "description": "KBW mirror file inventory (kbw_dane_files)"},
    {"name": "ask", "description": "Natural language → routed intent + SQL + rows"},
    {"name": "feedback", "description": "Thumbs up/down for answer quality"},
    {"name": "hints", "description": "OpenSearch suggestions over logged questions"},
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize DB schema and OpenSearch index once at startup."""
    db.init_database()
    opensearch_store.ensure_index()
    yield


app = FastAPI(
    title="Plujka API",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)


class AskRequest(BaseModel):
    question: str


class HealthResponse(BaseModel):
    """``GET /health`` — optional ``kbw_stats`` when ``details=true``."""

    status: Literal["ok"] = "ok"
    data_ready: bool = Field(description="True when at least one row exists in kbw_facts")
    kbw_stats: dict[str, int] | None = Field(
        default=None,
        description="Approximate live row counts (pg_stat_user_tables) when details=true",
    )


class AskResponse(BaseModel):
    """Shape returned by ``router.route_question`` (stable fields + optional analytics meta)."""

    model_config = ConfigDict(extra="ignore")

    question: str
    intent: str
    entity: str | None = None
    year: int | None = None
    years: list[int] = Field(default_factory=list)
    sql: str
    params: dict[str, Any]
    result: list[dict[str, Any]]
    candidate_geo_source: str | None = Field(
        default=None,
        description="kbw_candidate_geo_votes vs kbw_facts scan for kbw_candidate_geo_votes_detail",
    )
    mandate_extremes_source: str | None = Field(
        default=None,
        description="pkw vs kbw_fallback for kbw_sejm_mandate_vote_extremes",
    )


class FeedbackRequest(BaseModel):
    rating: Literal["thumbs_up", "thumbs_down"]
    question: str
    ask_response: dict[str, Any] | None = None


class QuestionHintsRequest(BaseModel):
    q: str
    limit: int = Field(default=8, ge=1, le=20)
    exclude_question: str | None = None


class FeedbackOkResponse(BaseModel):
    ok: Literal[True] = True


class QuestionHintsResponse(BaseModel):
    """Logged-question suggestions from OpenSearch (shape of hit docs varies slightly)."""

    text_hits: list[dict[str, Any]]
    semantic_hits: list[dict[str, Any]]


class KbwCatalogSummaryResponse(BaseModel):
    """Rollups over ``kbw_dane_files`` after ``kbw_catalog.profile_kbw_dane_files``."""

    total_files: int
    by_year: dict[str, int]
    by_file_kind: dict[str, int]


@app.get(
    "/health",
    response_model=HealthResponse,
    response_model_exclude_none=True,
    tags=["health"],
)
def health(details: bool = Query(False, description="Include approximate KBW table row counts")) -> HealthResponse:
    """Health."""
    payload: dict[str, Any] = {"status": "ok", "data_ready": db.kbw_data_ready()}
    if details:
        payload["kbw_stats"] = db.kbw_health_snapshot()
    return HealthResponse.model_validate(payload)


@app.get("/kbw/catalog/summary", response_model=KbwCatalogSummaryResponse, tags=["catalog"])
def kbw_catalog_summary() -> KbwCatalogSummaryResponse:
    """Aggregate counts for mirror files recorded in ``kbw_dane_files``."""
    return KbwCatalogSummaryResponse.model_validate(db.kbw_dane_files_catalog_summary())


@app.post("/ask", response_model=AskResponse, tags=["ask"])
def ask(request: AskRequest) -> AskResponse:
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
    return AskResponse.model_validate(result)


@app.post("/feedback", response_model=FeedbackOkResponse, tags=["feedback"])
def feedback(request: FeedbackRequest) -> FeedbackOkResponse:
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
    return FeedbackOkResponse()


@app.post("/question-hints", response_model=QuestionHintsResponse, tags=["hints"])
def question_hints(body: QuestionHintsRequest) -> QuestionHintsResponse:
    """Full-text and kNN suggestions over logged questions (OpenSearch)."""
    q = body.q.strip()
    if len(q) < 2:
        return QuestionHintsResponse(text_hits=[], semantic_hits=[])

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

    return QuestionHintsResponse(
        text_hits=text_hits[:limit],
        semantic_hits=semantic_hits[:limit],
    )
