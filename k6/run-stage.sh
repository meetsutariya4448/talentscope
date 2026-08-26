#!/usr/bin/env bash
# Runs one load-test stage and captures the supporting signals k6 itself
# doesn't see: container CPU/memory, Postgres connection-pool state, and
# Redis client/memory pressure — sampled immediately after the stage so
# they reflect load, not idle. Appends a row to evals/load-test-raw.jsonl;
# evals/load-test.md is the hand-written narrative built from these runs.
set -euo pipefail
cd "$(dirname "$0")/.."

VUS="${1:?usage: run-stage.sh VUS [DURATION]}"
DURATION="${2:-30s}"
OUT_DIR=evals/k6-runs
mkdir -p "$OUT_DIR"
SUMMARY_JSON="$OUT_DIR/vus${VUS}.json"

echo "=== Stage: VUS=$VUS DURATION=$DURATION ==="

k6 run --env VUS="$VUS" --env DURATION="$DURATION" \
  --summary-export="$SUMMARY_JSON" \
  k6/load_test.js

echo ""
echo "--- Container resources immediately post-stage ---"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" \
  talentscope-api-1 talentscope-worker-1 talentscope-beat-1 talentscope-postgres-1 talentscope-redis-1

echo ""
echo "--- Postgres connections ---"
docker compose exec -T postgres psql -U talentscope -d talentscope -c \
  "SELECT count(*) AS total, count(*) FILTER (WHERE state = 'active') AS active, count(*) FILTER (WHERE state = 'idle') AS idle FROM pg_stat_activity WHERE datname = 'talentscope';" 2>/dev/null

echo "--- Postgres max_connections ---"
docker compose exec -T postgres psql -U talentscope -d talentscope -c "SHOW max_connections;" 2>/dev/null

echo "--- Redis ---"
docker compose exec -T redis redis-cli INFO clients 2>/dev/null | grep -E "connected_clients|blocked_clients"
docker compose exec -T redis redis-cli INFO memory 2>/dev/null | grep -E "used_memory_human|used_memory_peak_human"

echo "--- Celery queue depth ---"
for q in ingestion embedding maintenance; do
  depth=$(docker compose exec -T redis redis-cli LLEN "$q" 2>/dev/null | tr -d '\r')
  echo "  $q: $depth"
done

echo "--- api container recent error/warning log lines ---"
docker compose logs api --tail 200 2>/dev/null | grep -iE "error|exception|timeout|pool" | tail -10 || echo "  (none)"
