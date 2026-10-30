# Plujka

Polish election (PKW/KBW) analytics: a question in Polish becomes one checked, read-only SQL query over the relational `mart` schema in PostgreSQL; every number in the answer comes from that query. OpenAI writes the SQL and the answer text; OpenSearch keeps the question log and few-shot examples.

**Flow:** question (+ previous turn) → entity resolution → LLM SQL over `mart` → sqlglot guard → read-only role → rows → Polish answer checked against the rows. Without OpenAI, legacy Sejm SQL templates answer what they can.

## Data

| Election | Years |
| -------- | ----- |
| Sejm, Senate | 1991, 1993, 1997, 2001, 2005, 2007, 2011, 2015, 2019, 2023 |
| President | 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025 |
| European Parliament | 2004, 2009, 2014, 2019, 2024 |
| Referendums | 1996, 1997, 2003, 2015, 2023 |
| Sejmiki, councils | 1998, 2002, 2006, 2010, 2014, 2018, 2024 |
| Mayors (wójt / burmistrz / prezydent miasta) | 2002, 2006, 2010, 2014, 2018, 2024 |
| GUS statistics per gmina (population, unemployment, salary) | 1995–2024 |

Mostly at precinct level, reconciled with the official PKW figures by build checks; persons are linked
across elections. Details and known gaps: [docs/MART.md](docs/MART.md).

## Stack

| Piece        | Role                                                |
| ------------ | --------------------------------------------------- |
| FastAPI      | `/ask`, `/persons`, `/health`, `/feedback`          |
| Streamlit    | Web UI (`8501`): answer, table, charts, maps, person search |
| PostgreSQL   | `mart` schema (pgvector image)                      |
| DuckDB       | Mart build from the committed parquet (`scripts/build_mart.py`) |
| OpenSearch   | Question log and few-shot SQL examples              |
| Redis        | Optional shared rate limit and token budget         |

## Documentation

| Doc | Purpose |
| --- | ------- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Local dev, Docker, PR expectations |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Request flow, data loading, where to edit code |
| [docs/DATA.md](docs/DATA.md) | Sample CSVs, PKW downloads, attribution |
| [docs/MART.md](docs/MART.md) | Relational election model (`mart`), build, checks, guarded LLM SQL |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | API keys, shared rate limit, backups, dashboards, data in Git |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Planned work |
| [AGENTS.md](AGENTS.md) | Rules and commands for AI coding agents |
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

The API does **not** import election data on startup. Build the mart with the **`loader` container**: `docker compose --profile tools run --rm loader python -u scripts/build_mart.py --jobs 3` builds it from the committed parquet (about 10 minutes); the default loader command also re-stages a local KBW mirror and imports the legacy `kbw_facts`. Until the mart or `kbw_facts` has rows, `GET /health` returns `data_ready: false`; Streamlit shows a short notice until data is present. Above the question box, expandable panels call **`/kbw/catalog/summary`** and **`/health?details=1`** for mirror inventory and approximate table sizes (Streamlit fragments refresh on an interval).

### Configuration (compose)

| Variable | Purpose |
| -------- | ------- |
| `OPENAI_API_KEY` | OpenAI API access (optional) |
| `OPENAI_CHAT_MODEL` | Chat model for routing (default `gpt-4o-mini`) |
| `OPENAI_EMBEDDING_MODEL` | Embeddings model (default `text-embedding-3-small`) |
| `OPENSEARCH_INITIAL_ADMIN_PASSWORD` | OpenSearch admin password (see `docker-compose.yml` default) |
| `QUESTION_HINTS_SEMANTIC_MIN_CHARS` | Min length of `q` before kNN hints run (default `6`) |
| `API_KEYS` / `PLUJKA_API_KEY` | Optional keys required by `/ask` and `/feedback` / the key the UI sends |
| `REDIS_URL` | Optional shared rate limit and token budget (`docker compose --profile redis up -d`) |
| `DAILY_TOKEN_LIMIT_PER_CLIENT` | Optional OpenAI tokens per API key or IP and day for `/ask` |

Optional Compose profiles: `tools` (loader), `redis` (shared rate limit), `observability` (OpenSearch Dashboards on http://localhost:5601). See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

The API container mounts `./data` at `/app/data` (mirror and imports read from there).

## Tests

```bash
make test
```

Uses `unittest` only (no DB). Optional Postgres-backed checks: `make test-integration` (requires `DATABASE_URL` and `PLUJKA_RUN_DB_TESTS=1`), including DB helpers, **`TestClient` HTTP smoke** (`/health`, `/kbw/catalog/summary`, stubbed `/ask`), and the KBW fixture router test. **GitHub Actions** (`.github/workflows/ci.yml`) runs the unit job first, then an **integration** job with `pgvector/pgvector:pg16`: DB tests, a full `scripts/build_mart.py` from the committed parquet (every consistency check must pass) and `scripts/add_sql_example.py --check`.

## API

- **`GET /health`** — `HealthResponse` w `/docs`: `status`, `data_ready`; opcjonalnie **`kbw_stats`** przy **`?details=1`** (przybliżone liczniki z `pg_stat_user_tables`).
- **`GET /kbw/catalog/summary`** — `KbwCatalogSummaryResponse`: liczba wpisów w **`kbw_dane_files`** oraz rozbicie po roku i `file_kind` (po uruchomieniu profilowania katalogu, np. z loadera / `kbw_catalog.profile_kbw_dane_files`).
- **`POST /ask`** — body `{"question": "...", "entity_refs": [...]?, "previous": {"question", "sql", "entity_refs"}?}` → JSON (`AskResponse` in `/docs`): `result` (numbers as JSON numbers), `intent`, `sql`, `params`, `answer`, `clarification`; `previous` makes it a follow-up ("A w 2019?"). With `API_KEYS` set, send `X-API-Key`.
- **`GET /persons?name=`** and **`GET /persons/{person_id}`** — person search and every candidacy of a person (fixed SQL, read-only role).
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
