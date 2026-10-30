#!/usr/bin/env bash
# Smoke test of a deployment through Caddy: API health with a loaded mart, a person search, one
# question, and the UI. Usage: deploy/smoke_test.sh https://plujka.example.org [api-key]
set -euo pipefail
base="${1:?usage: smoke_test.sh <base-url> [api-key]}"
key="${2:-}"
headers=(-H 'Content-Type: application/json')
[ -n "$key" ] && headers+=(-H "X-API-Key: $key")

fail() { echo "smoke test failed: $*" >&2; exit 1; }

health=$(curl -fsS "$base/api/health?details=true") || fail "GET /api/health"
echo "$health" | jq -e '.status == "ok" and .data_ready and (.mart_elections | length > 0)' >/dev/null \
  || fail "mart not loaded: $(echo "$health" | jq -c '{status, data_ready}')"

curl -fsS "${headers[@]}" "$base/api/persons?name=Tusk&limit=3" | jq -e '.persons | length > 0' >/dev/null \
  || fail "GET /api/persons found nobody"

curl -fsS "${headers[@]}" -X POST "$base/api/ask" -d '{"question": "Frekwencja w wyborach do Sejmu 2023"}' \
  | jq -e '.answer.text | length > 0' >/dev/null || fail "POST /api/ask"

curl -fsS "$base/_stcore/health" >/dev/null || fail "Streamlit health"
curl -fsS "$base/" >/dev/null || fail "UI page"
echo "smoke test passed: $base ($(echo "$health" | jq '.mart_elections | length') elections)"
