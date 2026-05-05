from __future__ import annotations

from datetime import datetime, timezone

from opensearchpy import OpenSearch

from api.services.config import OPENSEARCH_URL
from api.services.embeddings import embed_text

INDEX_NAME = "question_logs"


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
        "question_embedding": embed_text(question)[:128],
    }
    client.index(index=INDEX_NAME, body=payload)
