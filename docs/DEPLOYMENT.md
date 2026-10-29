# Deployment and operations

How to run Plujka beyond a laptop: access control, a shared rate limit, backups, telemetry
dashboards and how the staged data is versioned. Configuration variables are listed in
[MART.md](MART.md#configuration).

## Checklist

1. `PLUJKA_ENV=production` — the API refuses to start with the default database passwords or a
   disabled rate limit.
2. Strong owner password (`DATABASE_URL`) and reader password (`MART_READER_PASSWORD`); rebuild the
   mart once so the build sets the reader password.
3. `API_KEYS` when the API is not meant to be public; give the UI one of them in `PLUJKA_API_KEY`.
4. `REDIS_URL` when more than one API worker or replica serves `/ask`.
5. A scheduled backup (`scripts/backup_db.sh`) and a restore test.
6. `OPENAI_API_KEY`; without it every question falls back to the legacy Sejm templates.

## Access: API keys

`API_KEYS` is a comma-separated list. When it is set, `POST /ask` and `POST /feedback` return 401
without a matching `X-API-Key` header; `GET /health`, `/docs` and `POST /question-hints` stay open.
The rate limit then counts per key instead of per IP. Rotate a key by adding the new one, switching
clients, then removing the old one (restart the API after each change).

```bash
curl -s localhost:8000/ask -H 'Content-Type: application/json' -H 'X-API-Key: <key>' \
  -d '{"question": "Frekwencja w wyborach do Sejmu 2023"}'
```

## Rate limit shared through Redis

Without `REDIS_URL` each API process keeps its own sliding window, so N workers allow up to N times
`ASK_RATE_LIMIT_PER_MINUTE`. With `REDIS_URL` every process increments one counter per client and
minute in Redis (`plujka:ratelimit:<key>:<minute>`, expiring after two minutes). If Redis is
unreachable the process logs `redis rate limit unavailable` and falls back to its own window, so
an outage of Redis never blocks questions.

```bash
REDIS_URL=redis://redis:6379/0 docker compose --profile redis up -d
```

The bundled Redis keeps nothing on disk (counters are disposable).

## Backups

`scripts/backup_db.sh` writes a compressed `pg_dump` (custom format) to `backups/` (gitignored) and
keeps the newest `BACKUP_KEEP` dumps (default 7):

```bash
DATABASE_URL=postgresql://plujka:<password>@db:5432/plujka scripts/backup_db.sh /var/backups/plujka
# Only the non-rebuildable part (question log tables, legacy kbw_facts); mart comes from parquet:
BACKUP_SCHEMAS=public scripts/backup_db.sh
```

Restore into an empty database, then rebuild or check the mart:

```bash
pg_restore --clean --if-exists --no-owner -d "$DATABASE_URL" backups/plujka-YYYYmmdd-HHMMSS.dump
DATABASE_URL=... python scripts/build_mart.py   # recreates plujka_mart_reader and its grants
```

What a database backup does not cover: thumbs-down feedback (`data/feedback/feedback.jsonl` on the
API host) and the OpenSearch indexes (`question_logs`, `sql_examples`). Copy the feedback file with
the dump; the examples index is rebuilt from `sql_examples.jsonl` at startup, and the question log
can be snapshotted with OpenSearch snapshots if its history matters.

Example cron entry (03:15 every night):

```cron
15 3 * * * cd /srv/plujka && DATABASE_URL=... scripts/backup_db.sh /var/backups/plujka >> /var/log/plujka-backup.log 2>&1
```

## Telemetry dashboards

Every `/ask` writes a document to the OpenSearch index `question_logs` with `created_at`,
`status` (`first_pass`, `repaired`, `cached`, `clarify`, `rejected`, `failed`, `skipped`,
`template`), `latency_ms`, `row_count`, `answer_source`, `prompt_tokens`, `completion_tokens`,
`error`, `detected_intent`, `question` and `sql`.

```bash
docker compose --profile observability up -d opensearch-dashboards   # http://localhost:5601
```

In Dashboards create an index pattern `question_logs` with time field `created_at`, then for
example:

- share of answered questions: pie of `status`;
- questions that failed: Discover with `status:(failed OR rejected OR skipped)`, columns
  `question`, `error`, `sql` — the best source for new curated examples (`scripts/add_sql_example.py`);
- cost: sum of `prompt_tokens` and `completion_tokens` per day;
- latency: 50th/95th percentile of `latency_ms` per day.

The bundled OpenSearch runs without its security plugin; do not expose ports 9200 or 5601 publicly.

## Staged data in Git

The staged parquet (`data/kbw_stage_parquet`, `data/pkw_stage_parquet`, about 260 MB) is committed
as plain Git files, not Git LFS. CI checks out the repository and builds the whole mart from these
files on every push, and GitHub's free LFS bandwidth (1 GB per month) would be used up by those
checkouts within days. No parquet file is near GitHub's 100 MB file limit. If the data grows past a
few hundred MB, move it to release assets (one archive per election family) and download it in CI
and the loader instead.
