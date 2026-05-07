# Contributing

## Prerequisites

- Docker and Docker Compose (recommended path)
- Python 3.11+ if you run scripts under `scripts/` on the host

## Run the stack locally

From the repository root:

```bash
docker compose up --build
```

Optional OpenAI (routing and embeddings):

```bash
export OPENAI_API_KEY=your_key
docker compose up --build
```

Without `OPENAI_API_KEY`, the API uses deterministic fallbacks for intent routing and embeddings.

## URLs

| Service    | URL                   |
| ---------- | --------------------- |
| Streamlit  | http://localhost:8501 |
| API        | http://localhost:8000 |
| API docs   | http://localhost:8000/docs |
| OpenSearch | http://localhost:9200 |

## Data for development

See [docs/DATA.md](docs/DATA.md) for preparing sample CSVs and downloading full PKW archives.

## Tests

From the repo root (no Docker DB needed for the default suite):

```bash
make test
```

Integration tests against PostgreSQL: `make test-integration` with `DATABASE_URL` set and `PLUJKA_RUN_DB_TESTS=1` (see `tests/test_integration_kbw_db.py`). CI runs the same against `pgvector/pgvector:pg16` after the unit job passes.

## Pull requests

- Keep changes focused and consistent with existing style (see [.github/pull_request_template.md](.github/pull_request_template.md)).
- Describe **what** changed and **why** in the PR body.
- If behavior changes, mention how you verified it (manual steps or commands).

## Security

Do not commit secrets. Use `.env` locally (ignored by git) or your shell environment. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.
