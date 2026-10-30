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

Integration tests against PostgreSQL: `make test-integration` with `DATABASE_URL` set and `PLUJKA_RUN_DB_TESTS=1` (see `tests/test_integration_kbw_db.py`). CI runs the same against `pgvector/pgvector:pg16` after the unit job passes, then builds the whole mart from the committed parquet and checks the curated SQL examples; a separate job builds the Docker images and smoke-tests `docker compose`. `make lint` runs ruff (`ruff.toml`, errors only) and mypy (`mypy.ini`; modules in its baseline still have old type errors, remove a module once it passes); the CI lint job also checks links between Markdown files.

## Pull requests

- Keep changes focused and consistent with existing style (see [.github/pull_request_template.md](.github/pull_request_template.md)).
- Describe **what** changed and **why** in the PR body.
- If behavior changes, mention how you verified it (manual steps or commands).

## Releasing

1. Move the `[Unreleased]` entries of `CHANGELOG.md` to a new `## [x.y.z] - YYYY-MM-DD` section.
2. Commit, then `git tag vx.y.z && git push origin vx.y.z`.
3. `.github/workflows/release.yml` builds the images and publishes the GitHub release with that section as notes.

## Security

Do not commit secrets. Use `.env` locally (ignored by git) or your shell environment. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.
