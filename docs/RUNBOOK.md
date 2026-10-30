# Runbook

What to check when production misbehaves. Commands assume the Compose deployment of
[DEPLOYMENT.md](DEPLOYMENT.md) in `/srv/plujka`; `dc` stands for `docker compose`.

## First look

```bash
curl -s https://<host>/api/health?details=true | jq '{status, data_ready, mart: (.mart_elections | length)}'
dc ps                       # every service "running" / "healthy"
dc logs --since 15m api | tail -50
df -h / /mnt/data           # data volume
```

`data_ready: false` means the mart is empty or unreachable: see "Database" and "Mart deployment".

## OpenAI unavailable or over budget

Symptoms: `/ask` answers only plain Sejm questions (legacy templates), logs show OpenAI errors, or
clients get 429 from `DAILY_TOKEN_LIMIT_PER_CLIENT`.

- Check the provider status page and the key's usage limits.
- A 429 from the token budget is per key or IP and resets at 00:00 UTC; raise the limit only after
  checking the token dashboards.
- Nothing to restart: the API retries OpenAI on the next question.

## Database

Symptoms: `/health` fails or `data_ready: false`, `/persons` returns 503, logs show connection errors.

```bash
dc logs --since 15m postgres | tail -50
dc exec postgres pg_isready -U plujka
dc exec postgres psql -U plujka -c "SELECT count(*) FROM mart.elections"
```

- Disk full: free space (old backups, `docker system prune`), then `dc restart postgres`.
- Reader timeouts (`canceling statement due to statement timeout`) on many questions: look for a
  long-running query in `pg_stat_activity`; a mart rebuild holds locks on the views (see below).
- Data lost: restore from backup ([DEPLOYMENT.md](DEPLOYMENT.md#backups)), then deploy the mart.

## Mart deployment or build failed

A failed family-year build rolls back its own transaction and a failed derived rebuild rolls back
persons and views, so the previous mart stays in place. The log names the failing check.

- A check failed: the data or the build code changed; never weaken the check (`AGENTS.md`). Deploy the
  previous mart release instead and fix the cause in a pull request.
- While `derived.rebuild` runs (about 4 minutes) questions wait for locks on the views; announce the
  window or deploy into a second database.

## OpenSearch down

Questions still work: the question log and hints fail with `OpenSearch ... failed` in the API log.
Few-shot SQL examples are missing until OpenSearch is back, so answers may be worse.

```bash
dc logs --since 15m opensearch | tail -50
dc restart opensearch
```

## Redis down

The rate limit falls back to per-process windows (`redis rate limit unavailable` in the log); no
action is needed beyond restarting Redis.

## Abuse or traffic spike

- Lower `ASK_RATE_LIMIT_PER_MINUTE` or `DAILY_TOKEN_LIMIT_PER_CLIENT`, set `API_KEYS`, restart the API.
- Block an address at the firewall or in Caddy.

## After an incident

Write down cause, impact (time, failed questions) and the fix in the issue tracker; add a check or an
alert that would have caught it earlier.
