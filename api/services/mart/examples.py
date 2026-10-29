"""Verified question -> SQL examples used as few-shot prompts for ``dynamic_sql``.

Sources of examples:
- ``sql_examples.jsonl`` next to this file: curated pairs, reviewed in git (``source = curated``).
  Add one with ``scripts/add_sql_example.py``, which validates and executes the SQL first.
- Thumbs-up feedback on a ``dynamic_sql`` answer (``source = user_confirmed``).

Examples live in the OpenSearch index ``SQL_EXAMPLES_INDEX`` and are retrieved by text match plus
kNN on the question embedding. When OpenSearch is unavailable the curated file is searched locally by
word overlap, so prompts still get examples.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from api.services.config import SQL_EXAMPLES_INDEX, SQL_EXAMPLES_K
from api.services.mart import entities
from api.services.mart.safe_sql import validate_mart_sql

log = logging.getLogger(__name__)

SEED_PATH = Path(__file__).with_name("sql_examples.jsonl")


def example_id(question: str) -> str:
    """Stable document id: the same question (case/diacritics/spaces ignored) is stored once."""
    return hashlib.sha1(" ".join(entities.fold(question).split()).encode()).hexdigest()


@lru_cache(maxsize=1)
def load_seed() -> tuple[dict[str, str], ...]:
    """Curated examples from ``sql_examples.jsonl`` (one JSON object per line: question, sql)."""
    if not SEED_PATH.exists():
        return ()
    rows = [json.loads(line) for line in SEED_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return tuple({"question": r["question"], "sql": r["sql"], "source": "curated"} for r in rows)


def _client():
    """OpenSearch client (imported lazily so the module works without opensearch-py in tests)."""
    from api.services.opensearch_store import _client as opensearch_client

    return opensearch_client()


def _embedding(text: str) -> list[float]:
    """128-dim embedding shared with the question log index."""
    from api.services.opensearch_store import _embedding_for_knn

    return _embedding_for_knn(text)


def ensure_index() -> None:
    """Create the examples index if missing and (re)index the curated examples (idempotent)."""
    client = _client()
    if not client.indices.exists(SQL_EXAMPLES_INDEX):
        client.indices.create(
            index=SQL_EXAMPLES_INDEX,
            body={
                "settings": {"index": {"knn": True}},
                "mappings": {
                    "properties": {
                        "question": {"type": "text"},
                        "sql": {"type": "text", "index": False},
                        "source": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        "question_embedding": {
                            "type": "knn_vector",
                            "dimension": 128,
                            "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "nmslib"},
                        },
                    }
                },
            },
        )
    for example in load_seed():
        _index(client, example["question"], example["sql"], "curated")
    log.info("sql examples index ready (%s curated)", len(load_seed()))


def _index(client, question: str, sql: str, source: str) -> None:
    """Write one example document."""
    client.index(
        index=SQL_EXAMPLES_INDEX,
        id=example_id(question),
        body={
            "question": question,
            "sql": sql,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "question_embedding": _embedding(question),
        },
    )


def add_example(question: str, sql: str, source: str = "user_confirmed") -> bool:
    """Validate ``sql`` and store the pair; return False when it is rejected or OpenSearch fails.

    A user-confirmed example never overwrites a curated one with the same question.
    """
    try:
        sql = validate_mart_sql(sql)
    except ValueError as exc:
        log.warning("sql example rejected: %s", exc)
        return False
    if source != "curated" and example_id(question) in {example_id(e["question"]) for e in load_seed()}:
        return False
    try:
        _index(_client(), question, sql, source)
    except Exception as exc:  # examples are an optimisation; never fail the request
        log.warning("could not store sql example: %s", exc)
        return False
    return True


def _local_similar(question: str, k: int) -> list[dict[str, Any]]:
    """Curated examples ranked by shared word stems (used when OpenSearch is unavailable)."""
    stems, _ = entities.question_terms(question)
    scored = []
    for example in load_seed():
        other, _ = entities.question_terms(example["question"])
        overlap = len(stems & other) / (len(stems | other) or 1)
        if overlap > 0:
            scored.append((overlap, example))
    scored.sort(key=lambda item: -item[0])
    return [dict(example, score=round(score, 3)) for score, example in scored[:k]]


def similar_examples(question: str, k: int = SQL_EXAMPLES_K) -> list[dict[str, Any]]:
    """Up to ``k`` examples similar to ``question``: kNN hits first, then text hits, no duplicates."""
    if k <= 0:
        return []
    try:
        client = _client()
        source = ["question", "sql", "source"]
        knn = client.search(
            index=SQL_EXAMPLES_INDEX,
            body={"size": k, "_source": source,
                  "query": {"knn": {"question_embedding": {"vector": _embedding(question), "k": k * 2}}}},
        )
        text = client.search(
            index=SQL_EXAMPLES_INDEX,
            body={"size": k, "_source": source, "query": {"match": {"question": {"query": question, "fuzziness": "AUTO"}}}},
        )
    except Exception as exc:
        log.info("sql examples: OpenSearch unavailable (%s), using curated file", exc)
        return _local_similar(question, k)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for response in (knn, text):
        for hit in response.get("hits", {}).get("hits", []):
            if hit["_id"] in seen:
                continue
            seen.add(hit["_id"])
            out.append({**hit["_source"], "score": round(float(hit.get("_score") or 0), 3)})
    return out[:k]


def format_examples(examples: list[dict[str, Any]]) -> str:
    """Prompt section with the examples, or an empty string."""
    if not examples:
        return ""
    blocks = [f"-- Q: {e['question']}\n{e['sql'].strip()}" for e in examples]
    return "Verified examples (question -> SQL); adapt them, do not copy blindly:\n" + "\n\n".join(blocks)
