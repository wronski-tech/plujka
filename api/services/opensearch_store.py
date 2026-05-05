from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from opensearchpy import OpenSearch

from api.services.config import OPENSEARCH_URL
from api.services.embeddings import embed_text

INDEX_NAME = "question_logs"

logger = logging.getLogger(__name__)


def _embedding_for_knn(text: str) -> list[float]:
    vector = embed_text(text)
    if len(vector) >= 128:
        return vector[:128]
    return list(vector) + [0.0] * (128 - len(vector))


def _client() -> OpenSearch:
    return OpenSearch(hosts=[OPENSEARCH_URL], use_ssl=False, verify_certs=False)


def ensure_index() -> None:
    client = _client()
    if client.indices.exists(INDEX_NAME):
        return
    mapping = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "question": {"type": "text"},
                "detected_intent": {"type": "keyword"},
                "sql": {"type": "text"},
                "params": {"type": "object"},
                "created_at": {"type": "date"},
                "question_embedding": {
                    "type": "knn_vector",
                    "dimension": 128,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "nmslib",
                    },
                },
            }
        },
    }
    client.indices.create(index=INDEX_NAME, body=mapping)


def log_question(question: str, detected_intent: str, sql: str, params: dict) -> None:
    client = _client()
    payload = {
        "question": question,
        "detected_intent": detected_intent,
        "sql": sql,
        "params": params,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "question_embedding": _embedding_for_knn(question),
    }
    client.index(index=INDEX_NAME, body=payload)


def search_hints_text(query: str, limit: int) -> list[dict[str, str]]:
    q = query.strip()
    if len(q) < 2:
        return []
    try:
        client = _client()
        body: dict[str, Any] = {
            "size": limit,
            "_source": ["question", "detected_intent"],
            "query": {
                "match": {
                    "question": {
                        "query": q,
                        "fuzziness": "AUTO",
                    }
                }
            },
        }
        resp = client.search(index=INDEX_NAME, body=body)
    except Exception:
        logger.exception("OpenSearch text hints failed")
        return []
    out: list[dict[str, str]] = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        qtext = (src.get("question") or "").strip()
        if not qtext:
            continue
        out.append(
            {
                "question": qtext,
                "intent": str(src.get("detected_intent") or ""),
            }
        )
    return out


def search_hints_semantic(query: str, limit: int) -> list[dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    try:
        client = _client()
        vector = _embedding_for_knn(q)
        body: dict[str, Any] = {
            "size": limit,
            "_source": ["question", "detected_intent"],
            "query": {
                "knn": {
                    "question_embedding": {
                        "vector": vector,
                        "k": max(limit * 2, 10),
                    }
                }
            },
        }
        resp = client.search(index=INDEX_NAME, body=body)
    except Exception:
        logger.exception("OpenSearch semantic hints failed")
        return []
    out: list[dict[str, Any]] = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        qtext = (src.get("question") or "").strip()
        if not qtext:
            continue
        out.append(
            {
                "question": qtext,
                "intent": str(src.get("detected_intent") or ""),
                "score": float(hit.get("_score") or 0.0),
            }
        )
    return out
