#!/usr/bin/env bash
# One trial of the inference-thread A/B.
#   trial.sh <omp> <trial_index>
# Everything except OMP_NUM_THREADS is held fixed and re-verified each trial.
set -uo pipefail
cd ~/Desktop/talentscope
. scripts/_compose.sh

OMP="$1"; IDX="$2"
SNAPSHOT="talentscope-talentscope-20260907T025045Z-fdd0585.dump.gz"
OUT=evals/thread-ab
VUS=60; DURATION=45s; BACKLOG=1400

# --- fixed configuration, identical across configs ---
export API_CPUS=2.0 API_MEMORY=2g
export WORKER_CPUS=2.0 WORKER_MEMORY=1400m WORKER_CONCURRENCY=2
export EMBED_RATE_LIMIT=""          # unlimited: makes CPU the binding constraint
export OMP_NUM_THREADS="$OMP" TORCH_NUM_THREADS="$OMP" MKL_NUM_THREADS="$OMP"
export API_HOST_PORT=8000

LABEL="omp${OMP}-t${IDX}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${TS}-${LABEL}"
SUMMARY="${OUT}/${RUN_ID}.k6.json"
COMMIT="$(git rev-parse --short HEAD)"
DIRTY=false; [ -n "$(git status --porcelain)" ] && DIRTY=true

echo "### TRIAL ${IDX} omp=${OMP} (${RUN_ID})"

# 1. Stop the app tier BEFORE restoring. restore_db.sh terminates existing
# connections and drops the database, but a running api/worker reconnects from
# its pool immediately, so DROP DATABASE intermittently loses the race. Stopping
# first makes the reset deterministic instead of ~1-in-3 flaky.
# -t 5 caps the shutdown wait: the worker's 30s stop_grace_period is
# correct for production but adds ~45s of dead time to every trial.
# beat is not in the cycle at all - it is stopped once for the whole
# benchmark, because its scheduled ingestion would be uncontrolled load
# arriving mid-measurement.
dc stop -t 5 api worker >/dev/null 2>&1

# 2. Reset to the identical dataset, from the local dump.
# Deliberately NOT via scripts/restore_db.sh: that pulls from S3, which means
# keeping LocalStack resident purely to serve one file into a benchmark loop.
# The product restore path is exercised separately in the recovery drill
# (docs/incidents/2026-09-07-data-loss-restore.md); here the only requirement is
# that every trial starts from byte-identical data with minimum resident memory.
DUMP="backups/${SNAPSHOT}"
[ -f "$DUMP" ] || { echo "  MISSING DUMP $DUMP"; exit 1; }
dc exec -T postgres psql -U talentscope -d postgres -tAc \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='talentscope' AND pid <> pg_backend_pid();" >/dev/null 2>&1
dc exec -T postgres psql -U talentscope -d postgres -c "DROP DATABASE IF EXISTS talentscope;" >/dev/null 2>&1
dc exec -T postgres psql -U talentscope -d postgres -c "CREATE DATABASE talentscope;" >/dev/null 2>&1
dc exec -T postgres psql -U talentscope -d talentscope -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1
if ! gunzip -c "$DUMP" | dc exec -T postgres pg_restore -U talentscope -d talentscope --no-owner --exit-on-error >/tmp/ts_restore.log 2>&1; then
  echo "  RESTORE FAILED"; tail -3 /tmp/ts_restore.log; exit 1
fi

# 3. Recreate app services under this trial's config.
dc up -d --no-deps --force-recreate api worker >/dev/null 2>&1

# 3. Wait for readiness.
READY=false
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 localhost:8000/ready)" = "200" ] && { READY=true; break; }
  sleep 2
done
[ "$READY" = true ] || { echo "  NOT READY"; exit 1; }

# 4. Verify the environment actually applied (equivalence check).
API_NANO=$(docker inspect talentscope-deploy-api-1 --format '{{.HostConfig.NanoCpus}}')
API_MEM=$(docker inspect talentscope-deploy-api-1 --format '{{.HostConfig.Memory}}')
W_NANO=$(docker inspect talentscope-deploy-worker-1 --format '{{.HostConfig.NanoCpus}}')
W_MEM=$(docker inspect talentscope-deploy-worker-1 --format '{{.HostConfig.Memory}}')
API_TORCH=$(dc exec -T api python -c "import torch;print(torch.get_num_threads())" 2>/dev/null | tr -d ' \r')
W_PROCS=$(dc exec -T worker sh -c 'n=0; for p in $(ls /proc | grep -E "^[0-9]+$"); do [ -r /proc/$p/status ] || continue; grep -q "^Name:.*celery" /proc/$p/status 2>/dev/null && n=$((n+1)); done; echo $n' 2>/dev/null | tr -d ' \r')
IMG=$(docker inspect talentscope-deploy-api-1 --format '{{.Config.Image}}')

# 5. Clear queues and stale in-flight claims.
dc exec -T redis redis-cli DEL ingestion embedding maintenance >/dev/null 2>&1
dc exec -T redis redis-cli EVAL "for _,k in ipairs(redis.call('keys', ARGV[1])) do redis.call('del', k) end return 1" 0 'talentscope:embed:pending*' >/dev/null 2>&1

DATASET=$(dc exec -T postgres psql -U talentscope -d talentscope -tAc "SELECT count(*) FROM postings;" 2>/dev/null | tr -d ' \r')

# 6. Identical ingestion workload.
dc exec -T api python scripts/ingest_load.py "$BACKLOG" >/dev/null 2>&1

# 7. Server-side status counters BEFORE (application paths only).
curl -s localhost:8000/metrics | grep '^http_requests_total{' > /tmp/ts_before.txt

# 8. Ingestion progress: two point measurements bracketing the load window,
# not a 3s poll. Each `docker compose exec` costs seconds on a loaded host, so
# 40 of them per trial added ~9 minutes of harness overhead and competed with
# the very workload being measured. Mean rate over the k6 window is what gets
# reported anyway.
SAMP=/tmp/ts_emb_${IDX}_${OMP}.txt; : > "$SAMP"
EMB_T0=$(date +%s)
EMB_V0=$(dc exec -T postgres psql -U talentscope -d talentscope -tAc "SELECT count(*) FROM postings WHERE embedding IS NOT NULL;" 2>/dev/null | tr -d ' \r')
echo "$EMB_T0 ${EMB_V0:-0}" >> "$SAMP"

# 9. Load.
K6_EXIT=0
K6_START=$(date +%s)
k6 run --quiet --env VUS="$VUS" --env DURATION="$DURATION" --env BASE_URL=http://localhost:8000 \
  --summary-export="$SUMMARY" k6/load_test.js >/dev/null 2>&1 || K6_EXIT=$?

K6_END=$(date +%s)
EMB_T1=$(date +%s)
EMB_V1=$(dc exec -T postgres psql -U talentscope -d talentscope -tAc "SELECT count(*) FROM postings WHERE embedding IS NOT NULL;" 2>/dev/null | tr -d ' \r')
echo "$EMB_T1 ${EMB_V1:-0}" >> "$SAMP"

# 10. Status counters AFTER.
curl -s localhost:8000/metrics | grep '^http_requests_total{' > /tmp/ts_after.txt

# 11. Record.
COMMIT="$COMMIT" DIRTY="$DIRTY" LABEL="$LABEL" RUN_ID="$RUN_ID" OMP="$OMP" IDX="$IDX" \
API_NANO="$API_NANO" API_MEM="$API_MEM" W_NANO="$W_NANO" W_MEM="$W_MEM" \
API_TORCH="$API_TORCH" W_PROCS="$W_PROCS" IMG="$IMG" DATASET="$DATASET" \
VUS="$VUS" DURATION="$DURATION" BACKLOG="$BACKLOG" SUMMARY="$SUMMARY" SAMP="$SAMP" \
K6_START="$K6_START" K6_END="$K6_END" K6_EXIT="$K6_EXIT" \
python3 "$(dirname "$0")/record_trial.py"
echo "  done"
