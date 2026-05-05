from __future__ import annotations

import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://plujka:plujka@localhost:5432/plujka")
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "128"))
SEED_SAMPLE_CSV = os.getenv("SEED_SAMPLE_CSV", "data/sample/sejm_results_sample_1000.csv")
