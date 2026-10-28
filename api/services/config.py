from __future__ import annotations

import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://plujka:plujka@localhost:5432/plujka")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "128"))
# Read-only login role for LLM-generated SQL over the `mart` schema (created by scripts/build_mart.py).
MART_DATABASE_URL = os.getenv(
    "MART_DATABASE_URL",
    "postgresql://plujka_mart_reader:plujka_mart_reader@localhost:5432/plujka",
)
# Server-side limit for one LLM-generated query (milliseconds).
MART_STATEMENT_TIMEOUT_MS = int(os.getenv("MART_STATEMENT_TIMEOUT_MS", "5000"))
# Upper bound on rows returned by LLM-generated SQL (a larger or missing LIMIT is replaced).
MART_MAX_ROWS = int(os.getenv("MART_MAX_ROWS", "500"))
SEED_SAMPLE_CSV = os.getenv("SEED_SAMPLE_CSV", "data/sample/sejm_results_sample_1000.csv")
FEEDBACK_JSONL_PATH = os.getenv("FEEDBACK_JSONL_PATH", "data/feedback/feedback.jsonl")
# Skip semantic (embedding) hints for very short fragments to save latency / OpenAI calls.
QUESTION_HINTS_SEMANTIC_MIN_CHARS = int(os.getenv("QUESTION_HINTS_SEMANTIC_MIN_CHARS", "6"))
