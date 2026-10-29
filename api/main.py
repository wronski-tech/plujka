from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict, Field

from api.services import answer, config, db, feedback_store, opensearch_store, router
from api.services.mart import examples as sql_examples
from api.services.mart import schema as mart_schema
from api.services.rate_limit import SlidingWindowLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("plujka.api")
ask_limiter = SlidingWindowLimiter(config.ASK_RATE_LIMIT_PER_MINUTE)

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness probe and approximate KBW table stats"},
    {"name": "catalog", "description": "KBW mirror file inventory (kbw_dane_files)"},
    {"name": "ask", "description": "Natural language → routed intent + SQL + rows"},
    {"name": "feedback", "description": "Thumbs up/down for answer quality"},
    {"name": "hints", "description": "OpenSearch suggestions over logged questions"},
]


def production_problems() -> list[str]:
    """Settings that are unsafe in production (default passwords shipped in docker-compose)."""
    problems = []
    if conninfo_to_dict(config.MART_DATABASE_URL).get("password") == config.MART_DEFAULT_READER_PASSWORD:
        problems.append("MART_DATABASE_URL uses the default reader password")
    if conninfo_to_dict(config.DATABASE_URL).get("password") == "plujka":
        problems.append("DATABASE_URL uses the default owner password")
    if config.ASK_RATE_LIMIT_PER_MINUTE <= 0:
        problems.append("ASK_RATE_LIMIT_PER_MINUTE is disabled")
    return problems


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Check production settings, initialize the DB schema and OpenSearch indexes once at startup."""
    problems = production_problems()
    if problems and config.PLUJKA_ENV == "production":
        raise RuntimeError("Refusing to start with unsafe production settings: " + "; ".join(problems))
    for problem in problems:
        log.warning("unsafe for production: %s", problem)
    db.init_database()
    opensearch_store.ensure_index()
    try:
        sql_examples.ensure_index()
    except Exception as exc:  # examples fall back to the curated file
        log.warning("sql examples index unavailable: %s", exc)
    yield


app = FastAPI(
    title="Plujka API",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)


class EntityRef(BaseModel):
    """An explicit entity choice, e.g. an option picked from a clarification."""

    kind: str
    ref: str


class AskRequest(BaseModel):
    question: str
    entity_refs: list[EntityRef] | None = Field(
        default=None, description="Entity choices from a previous clarification (kind + ref from its options)"
    )


class HealthResponse(BaseModel):
    """``GET /health`` — optional ``kbw_stats`` when ``details=true``."""

    status: Literal["ok"] = "ok"
    data_ready: bool = Field(description="True when kbw_facts has rows or the mart has at least one election")
    kbw_stats: dict[str, int] | None = Field(
        default=None,
        description="Approximate live row counts (pg_stat_user_tables) when details=true",
    )
    mart_elections: list[dict[str, Any]] | None = Field(
        default=None, description="Elections loaded into the mart (election, year, round, loaded_at) when details=true"
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
    answer: dict[str, Any] | None = Field(
        default=None,
        description="Polish answer text: {text, source: llm|fallback, verified}; LLM text only when its numbers match the rows",
    )
    clarification: dict[str, Any] | None = Field(
        default=None,
        description="Set when the question is ambiguous: {prompt, kind, options: [{label, kind, ref}]}; re-ask with entity_refs",
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
    elections = mart_schema.loaded_elections()
    payload: dict[str, Any] = {"status": "ok", "data_ready": bool(elections) or db.kbw_data_ready()}
    if details:
        payload["kbw_stats"] = db.kbw_health_snapshot()
        payload["mart_elections"] = elections
    return HealthResponse.model_validate(payload)


@app.get("/kbw/catalog/summary", response_model=KbwCatalogSummaryResponse, tags=["catalog"])
def kbw_catalog_summary() -> KbwCatalogSummaryResponse:
    """Aggregate counts for mirror files recorded in ``kbw_dane_files``."""
    return KbwCatalogSummaryResponse.model_validate(db.kbw_dane_files_catalog_summary())


def _telemetry(result: dict[str, Any], started: float) -> dict[str, Any]:
    """Flat telemetry fields for the question log (status, latency, rows, answer source, tokens)."""
    meta = (result.get("params") or {}).get("_dynamic_sql_meta") or {}
    tokens = dict(meta.get("tokens") or {})
    for name, value in ((result.get("answer") or {}).get("tokens") or {}).items():
        tokens[name] = tokens.get(name, 0) + value
    return {
        "status": meta.get("status") or "template",
        "latency_ms": int((time.monotonic() - started) * 1000),
        "row_count": len(result.get("result") or []),
        "answer_source": (result.get("answer") or {}).get("source"),
        "prompt_tokens": tokens.get("prompt_tokens", 0),
        "completion_tokens": tokens.get("completion_tokens", 0),
        "error": meta.get("error") or (result.get("params") or {}).get("_dynamic_sql_error"),
    }


@app.post("/ask", response_model=AskResponse, tags=["ask"])
def ask(request: AskRequest, http_request: Request) -> AskResponse:
    """Answer a question: route it to SQL, run it, and describe the rows in Polish."""
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question is required.")
    client = http_request.client.host if http_request.client else "unknown"
    if not ask_limiter.allow(client):
        raise HTTPException(status_code=429, detail="Too many questions; try again in a minute.")

    started = time.monotonic()
    refs = [r.model_dump() for r in request.entity_refs] if request.entity_refs else None
    result = router.route_question(request.question, entity_refs=refs)
    if not result.get("clarification"):
        result["answer"] = answer.compose_answer(request.question, result.get("result") or [])
    telemetry = _telemetry(result, started)
    log.info(
        "ask intent=%s status=%s rows=%s latency_ms=%s answer=%s tokens=%s/%s",
        result["intent"], telemetry["status"], telemetry["row_count"], telemetry["latency_ms"],
        telemetry["answer_source"], telemetry["prompt_tokens"], telemetry["completion_tokens"],
    )
    opensearch_store.log_question(
        question=request.question,
        detected_intent=result["intent"],
        sql=result["sql"],
        params=result["params"],
        telemetry=telemetry,
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
        # A confirmed LLM-written query becomes a few-shot example for similar questions.
        response = request.ask_response or {}
        if response.get("intent") == "dynamic_sql" and response.get("sql"):
            sql_examples.add_example(request.question.strip(), response["sql"], source="user_confirmed")
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
