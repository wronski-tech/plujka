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
# Password of the read-only role that ships in docker-compose; refused when PLUJKA_ENV=production.
MART_DEFAULT_READER_PASSWORD = "plujka_mart_reader"
# "development" or "production". Production refuses to start with default credentials.
PLUJKA_ENV = os.getenv("PLUJKA_ENV", "development").strip().lower()
# Validated question -> SQL pairs are reused for this long (seconds); 0 disables the cache.
SQL_CACHE_TTL_SECONDS = int(os.getenv("SQL_CACHE_TTL_SECONDS", "3600"))
SQL_CACHE_MAX_ENTRIES = int(os.getenv("SQL_CACHE_MAX_ENTRIES", "512"))
# OpenSearch index of verified question -> SQL examples used as few-shot prompts.
SQL_EXAMPLES_INDEX = os.getenv("SQL_EXAMPLES_INDEX", "sql_examples")
SQL_EXAMPLES_K = int(os.getenv("SQL_EXAMPLES_K", "4"))
# Natural-language answer written by the LLM from SQL rows (numbers are checked against the rows).
ANSWER_SUMMARY_ENABLED = os.getenv("ANSWER_SUMMARY_ENABLED", "1").strip().lower() in ("1", "true", "yes")
# Requests per minute and client IP for POST /ask; 0 disables the limit.
ASK_RATE_LIMIT_PER_MINUTE = int(os.getenv("ASK_RATE_LIMIT_PER_MINUTE", "30"))
# Optional Redis (redis://host:6379/0) that shares the rate limit across API workers and replicas.
REDIS_URL = os.getenv("REDIS_URL", "").strip()
# Optional comma-separated API keys; when set, POST /ask and POST /feedback require X-API-Key.
API_KEYS = frozenset(k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip())
SEED_SAMPLE_CSV = os.getenv("SEED_SAMPLE_CSV", "data/sample/sejm_results_sample_1000.csv")
FEEDBACK_JSONL_PATH = os.getenv("FEEDBACK_JSONL_PATH", "data/feedback/feedback.jsonl")
# Skip semantic (embedding) hints for very short fragments to save latency / OpenAI calls.
QUESTION_HINTS_SEMANTIC_MIN_CHARS = int(os.getenv("QUESTION_HINTS_SEMANTIC_MIN_CHARS", "6"))
