# Plujka

Deterministic PKW (election) analytics: natural-language questions are routed to intent-specific SQL against PostgreSQL, with optional OpenAI for routing/embeddings and OpenSearch for question logging.

**Flow:** question → embedding → semantic router → intent → SQL template + parameters → PostgreSQL.

## Stack

| Piece        | Role                                                |
| ------------ | --------------------------------------------------- |
| FastAPI      | `/health`, `/ask`                                   |
| Streamlit    | Web UI (`8501`)                                     |
| PostgreSQL   | Data store (pgvector image)                         |
| OpenSearch   | Logs / audit of questions                           |

## Documentation

| Doc | Purpose |
| --- | ------- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local dev, Docker, PR expectations |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Request flow, data loading, where to edit code |
| [docs/DATA.md](docs/DATA.md) | Sample CSVs, PKW downloads, attribution |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## Quick start (Docker Compose)

1. **Optional — OpenAI** (better routing/embeddings; otherwise deterministic fallbacks):

   ```bash
   export OPENAI_API_KEY=your_key
   export OPENAI_CHAT_MODEL=gpt-4o-mini
   export OPENAI_EMBEDDING_MODEL=text-embedding-3-small
   ```

2. **Run everything:**

   ```bash
   docker compose up --build
   ```

3. **URLs:**

   | Service     | URL                       |
   | ----------- | ------------------------- |
   | Streamlit   | http://localhost:8501     |
   | API         | http://localhost:8000     |
   | OpenSearch  | http://localhost:9200     |
   | PostgreSQL  | localhost:5432 (`plujka` / `plujka`) |

The API does **not** import election data on startup. Load KBW into PostgreSQL with the **`loader` container** (`docker compose --profile tools run --rm loader`). Until `kbw_facts` has rows, `GET /health` returns `data_ready: false`; Streamlit shows a short notice until data is present. Above the question box, expandable panels call **`/kbw/catalog/summary`** and **`/health?details=1`** for mirror inventory and approximate table sizes (Streamlit fragments refresh on an interval).

### Configuration (compose)

| Variable | Purpose |
| -------- | ------- |
| `OPENAI_API_KEY` | OpenAI API access (optional) |
| `OPENAI_CHAT_MODEL` | Chat model for routing (default `gpt-4o-mini`) |
| `OPENAI_EMBEDDING_MODEL` | Embeddings model (default `text-embedding-3-small`) |
| `OPENSEARCH_INITIAL_ADMIN_PASSWORD` | OpenSearch admin password (see `docker-compose.yml` default) |
| `QUESTION_HINTS_SEMANTIC_MIN_CHARS` | Min length of `q` before kNN hints run (default `6`) |

The API container mounts `./data` at `/app/data` (mirror and imports read from there).

## Tests

```bash
make test
```

Uses `unittest` only (no DB). Optional Postgres-backed checks: `make test-integration` (requires `DATABASE_URL` and `PLUJKA_RUN_DB_TESTS=1`), including DB helpers, **`TestClient` HTTP smoke** (`/health`, `/kbw/catalog/summary`, stubbed `/ask`), and the KBW fixture router test. **GitHub Actions** (`.github/workflows/ci.yml`) runs the unit job first, then an **integration** job with `pgvector/pgvector:pg16` and the same env vars.

## API

- **`GET /health`** — `HealthResponse` w `/docs`: `status`, `data_ready`; opcjonalnie **`kbw_stats`** przy **`?details=1`** (przybliżone liczniki z `pg_stat_user_tables`).
- **`GET /kbw/catalog/summary`** — `KbwCatalogSummaryResponse`: liczba wpisów w **`kbw_dane_files`** oraz rozbicie po roku i `file_kind` (po uruchomieniu profilowania katalogu, np. z loadera / `kbw_catalog.profile_kbw_dane_files`).
- **`POST /ask`** — body `{"question": "..."}` → JSON (`AskResponse` in `/docs`): `result`, `intent`, `entity`, `year`, `years`, `sql`, `params`; opcjonalnie **`candidate_geo_source`**, **`mandate_extremes_source`** (meta przy wybranych intentach KBW).
- **`POST /feedback`** — `{"rating": "thumbs_up"|"thumbs_down", "question": "...", "ask_response": {...}?}` → **`FeedbackOkResponse`** `{ "ok": true }`; thumbs-down dopisuje wpis do `data/feedback/feedback.jsonl` na hoście API (`needs_fix: true`, pełny snapshot `ask_response`).
- **`POST /question-hints`** — `QuestionHintsRequest` → **`QuestionHintsResponse`** (`text_hits`, `semantic_hits`) — OpenSearch full-text + kNN nad zalogowanymi pytaniami. Semantyka wyłączona dla bardzo krótkiego `q` (patrz `QUESTION_HINTS_SEMANTIC_MIN_CHARS`).

Docs: http://localhost:8000/docs when the API is running.

## Data preparation

### Sample data (local scripts)

```bash
python3 scripts/prepare_sample_data.py
```

Creates:

- `data/raw/wyniki_gl_na_listy_po_obwodach_sejm_utf8.csv`
- `data/sample/sejm_results_sample_1000.csv`

### Full PKW CSV archives (official site)

```bash
python3 scripts/download_all_pkw_csv.py
```

Downloads archives into `data/pkw_all/zip/` and extracts CSVs into `data/pkw_all/csv/`.

Example — 2023 and 2019 bundles:

```bash
python3 scripts/download_all_pkw_csv.py \
  --page-url "https://sejmsenat2023.pkw.gov.pl/sejmsenat2023/pl/dane_w_arkuszach" \
  --page-url "https://sejmsenat2019.pkw.gov.pl/sejmsenat2019/pl/dane_w_arkuszach"
```

Outputs are grouped by election prefix, e.g. `data/pkw_all/sejmsenat2023/zip/`, `data/pkw_all/sejmsenat2023/csv/`, and the same for `sejmsenat2019`.

## Repo hygiene

Secrets and local env files should stay out of git; see `.gitignore` (e.g. `.env`).
