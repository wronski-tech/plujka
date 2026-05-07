# Architecture

Plujka answers Polish election (PKW-style) questions by mapping natural language to **fixed SQL templates** and executing them against **PostgreSQL**. Optional **OpenAI** improves intent extraction and embeddings; without it, the stack uses deterministic fallbacks.

## Request path (`POST /ask`)

```mermaid
flowchart LR
  Q[Question] --> E[embed_text]
  Q --> L[extract_intent_and_entity]
  E --> R[route_question]
  L --> R
  R --> T[SQL template + params]
  T --> DB[(PostgreSQL)]
  R --> OS[OpenSearch log]
```

1. **Embedding** — `embed_text(question)` for semantic fallback routing (and OpenSearch logging).
2. **LLM** — `extract_intent_and_entity(question)` proposes an intent and entity when OpenAI is configured; unknown intents fall back to semantic matching against predefined intent phrases.
3. **Router** — `route_question` in `api/services/router.py` resolves year(s), location hints (districts, gminy, etc.), picks `SQL_TEMPLATES[intent]`, binds parameters, runs `db.run_sql`.
4. **Persistence** — Results come from PostgreSQL only; OpenSearch stores question metadata and embeddings for audit/search (`api/services/opensearch_store.py`).

## Startup and data loading

On API startup (`lifespan` in `api/main.py`):

1. `db.init_database()` — schema / migrations as implemented in `api/services/db.py`.
2. `opensearch_store.ensure_index()` — creates the `question_logs` index if missing.

KBW rows are **not** imported by the API. Run the **`loader` service** (Compose profile `tools`): it stages mirror CSVs to Parquet, then runs `scripts/import_kbw_facts.py` into `kbw_facts`. That script **profiles the mirror tree into `kbw_dane_files`** first so `GET /kbw/catalog/summary` is meaningful after an import even when `kbw_facts` was empty before.

`GET /health` returns `data_ready: true` when `kbw_facts` has at least one row. Streamlit polls `/health` so users see when the database has been populated.

`GET /kbw/catalog/summary` returns counts from `kbw_dane_files` (mirror inventory filled by `kbw_catalog.profile_kbw_dane_files`, typically from the loader). The Streamlit UI exposes catalog rollups and `GET /health?details=1` (`kbw_stats`) in collapsible `@st.fragment` sections that refresh on an interval so full-page reruns stay cheap.

## Question hints (`POST /question-hints`)

The Streamlit app calls this while the user types (≥ 2 characters) and after an answer to show **related past questions**. Responses combine **full-text** `match` on `question` and **kNN** on `question_embedding` (same 128-dim vectors as `log_question`). Short prefixes skip semantic search until `QUESTION_HINTS_SEMANTIC_MIN_CHARS` to limit embedding latency and OpenAI usage.

## Feedback (`POST /feedback`)

The Streamlit UI sends thumbs-up/down after each answer. **Thumbs-down** requests append one JSON line per event to `FEEDBACK_JSONL_PATH` (default `data/feedback/feedback.jsonl`) via `api/services/feedback_store.py`, including `needs_fix: true` and the full **`ask_response`** payload (question routing snapshot: `result`, `intent`, `sql`, `params`, etc.) for offline review. Thumbs-up lines omit `ask_response` and set `needs_fix: false`. In Docker, `./data` is mounted into the API container so logs land on the host under `data/feedback/`.

## Configuration

Central defaults and env vars are in `api/services/config.py` (`DATABASE_URL`, `OPENSEARCH_URL`, OpenAI models, `EMBEDDING_DIM`, `FEEDBACK_JSONL_PATH`). Docker Compose wires these for containers (see `docker-compose.yml`).

## Where to change behavior

| Concern              | Location                          |
| -------------------- | --------------------------------- |
| Intents / SQL shapes | `api/services/sql_templates.py`   |
| Routing rules        | `api/services/router.py`          |
| LLM prompts / parsing| `api/services/llm.py`             |
| Embeddings           | `api/services/embeddings.py`      |
| KBW import           | `api/services/kbw_import.py`, `scripts/import_kbw_facts.py`, `loader` service |
| Legacy PKW helpers   | `api/services/seed.py`            |
| UI                   | `streamlit_app/app.py`            |
| Feedback JSONL       | `api/services/feedback_store.py`  |
