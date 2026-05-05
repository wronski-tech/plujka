from __future__ import annotations

import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from api.services import db, opensearch_store, router, seed

app = FastAPI(title="Plujka API", version="0.1.0")


class AskRequest(BaseModel):
    question: str


def _run_seed_background() -> None:
    try:
        seed.seed_if_empty()
    except Exception:
        # Logged by uvicorn if unhandled; keep thread alive for ops
        import logging

        logging.exception("Background seed failed")


@app.on_event("startup")
def startup() -> None:
    db.init_database()
    opensearch_store.ensure_index()
    threading.Thread(target=_run_seed_background, name="seed", daemon=True).start()


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "data_ready": seed.seed_complete.is_set()}


@app.post("/ask")
def ask(request: AskRequest) -> dict:
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
