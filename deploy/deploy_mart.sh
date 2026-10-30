#!/usr/bin/env bash
# Deploy the mart built and checked by CI (release asset mart.dump) without downtime: restore it into
# the mart database that is not in use (plujka_mart_a / plujka_mart_b), rebuild persons and views,
# check it, then point the API at it (MART_DB in .env) and restart the API. The previous mart database
# stays for rollback: set MART_DB back and run `docker compose ... up -d api`.
#
# Usage (on the server, in /srv/plujka): deploy/deploy_mart.sh [release-tag]   (default: mart-latest)
set -euo pipefail
cd "$(dirname "$0")/.."

tag="${1:-mart-latest}"
repo="${PLUJKA_REPO:-wronski-tech/plujka}"
dc=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
work=/mnt/data/mart-deploy

set -a
# shellcheck disable=SC1091
source .env
set +a
current="${MART_DB:-plujka_mart_a}"
if [ "$current" = plujka_mart_a ]; then target=plujka_mart_b; else target=plujka_mart_a; fi
echo "deploy mart $tag into $target (in use: $current)"

mkdir -p "$work"
base="https://github.com/$repo/releases/download/$tag"
curl -fsSL -o "$work/mart.dump" "$base/mart.dump"
curl -fsSL -o "$work/mart.dump.sha256" "$base/mart.dump.sha256"
(cd "$work" && sha256sum -c mart.dump.sha256)

psql() { "${dc[@]}" exec -T postgres psql -U plujka -v ON_ERROR_STOP=1 "$@"; }
psql -d plujka -c "DROP DATABASE IF EXISTS $target" -c "CREATE DATABASE $target"
# A schema-only dump does not carry extensions; the trigram index on v_person_history needs pg_trgm.
psql -d "$target" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
# Tables and data only: the materialized views are rebuilt by build_mart.py --derived-only, which
# analyzes them in order (pg_restore's own REFRESH runs without statistics and takes far longer).
"${dc[@]}" exec -T postgres pg_restore -U plujka -d "$target" --no-owner --no-privileges \
  --section=pre-data --section=data < "$work/mart.dump"
"${dc[@]}" run --rm \
  -e DATABASE_URL="postgresql://plujka:$POSTGRES_PASSWORD@postgres:5432/$target" \
  -e MART_DATABASE_URL="postgresql://plujka_mart_reader:$MART_READER_PASSWORD@postgres:5432/$target" \
  loader python -u scripts/build_mart.py --derived-only

elections=$(psql -d "$target" -Atc "SELECT count(*) FROM mart.elections")
if [ "$elections" -eq 0 ]; then echo "mart in $target is empty; API left on $current" >&2; exit 1; fi

if grep -q '^MART_DB=' .env; then sed -i "s/^MART_DB=.*/MART_DB=$target/" .env; else echo "MART_DB=$target" >> .env; fi
"${dc[@]}" up -d api
for _ in $(seq 1 30); do
  if "${dc[@]}" exec -T api python -c "import json,urllib.request; \
assert json.load(urllib.request.urlopen('http://localhost:8000/health'))['data_ready']" 2>/dev/null; then
    echo "API serves $target ($elections elections); previous mart kept in $current"
    rm -f "$work/mart.dump"
    exit 0
  fi
  sleep 5
done
echo "API not healthy on $target; roll back with MART_DB=$current" >&2
exit 1
