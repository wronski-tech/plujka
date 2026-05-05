from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from api.services import db, opensearch_store, router, seed

app = FastAPI(title="Plujka API", version="0.1.0")


class AskRequest(BaseModel):
    question: str


@app.on_event("startup")
def startup() -> None:
    db.init_database()
    seed.seed_if_empty()
    opensearch_store.ensure_index()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
