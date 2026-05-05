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
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Request flow, seeding, where to edit code |
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

On first start the API seeds the database in a **background thread**. Until seeding finishes, `GET /health` returns `data_ready: false`; the Streamlit app shows a loading banner and then clears it when data is ready.

### Configuration (compose)

| Variable | Purpose |
| -------- | ------- |
| `OPENAI_API_KEY` | OpenAI API access (optional) |
| `OPENAI_CHAT_MODEL` | Chat model for routing (default `gpt-4o-mini`) |
| `OPENAI_EMBEDDING_MODEL` | Embeddings model (default `text-embedding-3-small`) |
| `OPENSEARCH_INITIAL_ADMIN_PASSWORD` | OpenSearch admin password (see `docker-compose.yml` default) |
| `QUESTION_HINTS_SEMANTIC_MIN_CHARS` | Min length of `q` before kNN hints run (default `6`) |

The API container mounts `./data` at `/app/data` (sample CSV path is set via `SEED_SAMPLE_CSV` in Compose).

## API

- **`GET /health`** — `{"status": "ok", "data_ready": true|false}`  
- **`POST /ask`** — body `{"question": "..."}` → JSON with `result`, `intent`, `entity`, `sql`, `params`
- **`POST /feedback`** — `{"rating": "thumbs_up"|"thumbs_down", "question": "...", "ask_response": {...}?}` — thumbs-down entries append to `data/feedback/feedback.jsonl` on the API host with `needs_fix: true` (full `ask_response` snapshot for review)
- **`POST /question-hints`** — `{"q": "...", "limit": 8, "exclude_question": null}` → `{"text_hits": [...], "semantic_hits": [...]}` — OpenSearch full-text + kNN over logged questions (`question_logs`). Semantic branch is skipped for very short `q` (see `QUESTION_HINTS_SEMANTIC_MIN_CHARS`).

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
