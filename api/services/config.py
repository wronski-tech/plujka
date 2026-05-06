from __future__ import annotations

import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://plujka:plujka@localhost:5432/plujka")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "128"))
SEED_SAMPLE_CSV = os.getenv("SEED_SAMPLE_CSV", "data/sample/sejm_results_sample_1000.csv")
FEEDBACK_JSONL_PATH = os.getenv("FEEDBACK_JSONL_PATH", "data/feedback/feedback.jsonl")
# Skip semantic (embedding) hints for very short fragments to save latency / OpenAI calls.
QUESTION_HINTS_SEMANTIC_MIN_CHARS = int(os.getenv("QUESTION_HINTS_SEMANTIC_MIN_CHARS", "6"))


def _env_truthy(name: str) -> bool:
    """Env truthy."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


# If true on API startup: wipe imported election tables and run the full seed pipeline again
# (use after adding/replacing CSV under data/). Does not remove Postgres/OpenSearch volumes.
FORCE_RESEED = _env_truthy("FORCE_RESEED")
# If set, POST /reseed accepts header X-Reseed-Token: <value> to trigger the same reload without restart.
RESEED_TOKEN = os.getenv("RESEED_TOKEN", "").strip()
