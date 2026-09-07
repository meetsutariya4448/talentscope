#!/usr/bin/env bash
# Runs one load-test stage and captures the supporting signals k6 itself
# doesn't see: container CPU/memory, Postgres connection-pool state, and
# Redis client/memory pressure — sampled immediately after the stage so
# they reflect load, not idle.
#
# Output naming carries the *condition*, not just the VU count. It used to be
# `vus${VUS}.json`, which meant every comparison re-run at the same VU level
# under a different condition overwrote its own control — the damage is
# recorded in evals/k6-runs/README.md. --label is what distinguishes them.
#
# Each stage also appends one JSON object to evals/load-test-raw.jsonl. The
# header of this script claimed that for a long time while the diagnostics
# below only ever went to stdout and were lost with the terminal.
#
# Usage:
#   bash k6/run-stage.sh VUS [DURATION] [--label LABEL] [--base-url URL]
#   bash k6/run-stage.sh 20 60s --label budget-api2-w2-omp1
set -euo pipefail
cd "$(dirname "$0")/.."

VUS="${1:?usage: run-stage.sh VUS [DURATION] [--label LABEL] [--base-url URL]}"
shift
DURATION="30s"
if [ "${1:-}" != "" ] && [[ "${1}" != --* ]]; then
  DURATION="$1"; shift
fi

LABEL="${LABEL:-unlabeled}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
while [ $# -gt 0 ]; do
  case "$1" in
    --label)    LABEL="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The label becomes part of a filename; keep it boring.
if ! printf '%s' "$LABEL" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
  echo "--label '$LABEL' must be alphanumeric plus . - _ (it becomes a filename)" >&2
  exit 2
fi

OUT_DIR=evals/k6-runs
RAW_JSONL=evals/load-test-raw.jsonl
mkdir -p "$OUT_DIR"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${TS}-vus${VUS}-${DURATION}-${LABEL}"
SUMMARY_JSON="$OUT_DIR/${RUN_ID}.json"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_DIRTY=false
[ -n "$(git status --porcelain 2>/dev/null)" ] && GIT_DIRTY=true

echo "=== Stage: VUS=$VUS DURATION=$DURATION LABEL=$LABEL ==="
echo "    run_id : $RUN_ID"
echo "    target : $BASE_URL"

# k6 exits 99 when a threshold is breached. That must NOT abort this script:
# load_test.js documents its thresholds as "a marker in the summary", not a
# pass/fail gate, and under `set -e` a breach was killing the run before the
# diagnostics and the JSONL row were written — losing the record for exactly
# the overloaded stages worth recording. The code is captured and stored
# instead, because "did this stage breach its thresholds" is real signal.
K6_EXIT=0
k6 run --env VUS="$VUS" --env DURATION="$DURATION" --env BASE_URL="$BASE_URL" \
  --summary-export="$SUMMARY_JSON" \
  k6/load_test.js || K6_EXIT=$?

if [ "$K6_EXIT" = "99" ]; then
  echo ""
  echo "NOTE: k6 exited 99 — one or more thresholds breached (see summary above)."
elif [ "$K6_EXIT" != "0" ]; then
  echo ""
  echo "WARNING: k6 exited $K6_EXIT (not a threshold breach)."
fi

# --- post-stage diagnostics, captured to variables so they can be recorded
#     rather than only printed ---
#
# Every capture below ends in `|| true`. These are diagnostics: none of them is
# allowed to abort the run, and under `set -e` with `pipefail` they otherwise
# do. The sharpest case is the api error-log grep — grep exits 1 when it finds
# nothing, so a *healthy* api aborted the script right before the JSONL row was
# written, which is why no row was ever recorded despite the header promising
# one since the file was created.
capture() { "$@" 2>/dev/null || true; }

# Only the containers actually running — naming a container that isn't up
# (api2 exists only under docker-compose.proxy.yml) makes docker stats fail.
RUNNING_CONTAINERS="$(docker compose ps --format '{{.Name}}' 2>/dev/null | tr '\n' ' ')"
DOCKER_STATS="$(capture docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' $RUNNING_CONTAINERS)" || true

PG_CONNS="$(capture docker compose exec -T postgres psql -U talentscope -d talentscope -tAc \
  "SELECT count(*)||' total, '||count(*) FILTER (WHERE state='active')||' active, '||count(*) FILTER (WHERE state='idle')||' idle' FROM pg_stat_activity WHERE datname='talentscope';")"

PG_MAXCONN="$(capture docker compose exec -T postgres psql -U talentscope -d talentscope -tAc "SHOW max_connections;")"

REDIS_CLIENTS="$(capture docker compose exec -T redis redis-cli INFO clients | grep -E 'connected_clients|blocked_clients' | tr -d '\r' | tr '\n' ' ')" || true
REDIS_MEM="$(capture docker compose exec -T redis redis-cli INFO memory | grep -E 'used_memory_human|used_memory_peak_human' | tr -d '\r' | tr '\n' ' ')" || true

QUEUE_DEPTHS=""
for q in ingestion embedding maintenance; do
  depth="$(capture docker compose exec -T redis redis-cli LLEN "$q" | tr -d '\r')"
  QUEUE_DEPTHS="${QUEUE_DEPTHS}${q}=${depth:-0} "
done

EMBEDDED_COUNT="$(capture docker compose exec -T postgres psql -U talentscope -d talentscope -tAc \
  "SELECT count(*) FROM postings WHERE embedding IS NOT NULL;")"
POSTINGS_COUNT="$(capture docker compose exec -T postgres psql -U talentscope -d talentscope -tAc \
  "SELECT count(*) FROM postings;")"

API_ERRORS="$(capture docker compose logs api --tail 200 | grep -iE 'error|exception|timeout|pool' | tail -10)" || true

echo ""
echo "--- Container resources immediately post-stage ---"
echo "$DOCKER_STATS"
echo "--- Postgres connections --- "
echo "$PG_CONNS  (max_connections=$PG_MAXCONN)"
echo "--- Redis ---"
echo "$REDIS_CLIENTS"
echo "$REDIS_MEM"
echo "--- Celery queue depth ---"
echo "$QUEUE_DEPTHS"
echo "--- Corpus ---"
echo "postings=$POSTINGS_COUNT embedded=$EMBEDDED_COUNT"
echo "--- api container recent error/warning log lines ---"
echo "${API_ERRORS:-  (none)}"

# --- append the row this script has always claimed to append ---
# Exported so the recorder below can read them; the diagnostics are captured
# into shell variables above specifically so they can be *recorded* and not
# just echoed to a terminal that will be closed.
export VUS DURATION LABEL BASE_URL GIT_SHA GIT_DIRTY
export DOCKER_STATS PG_CONNS PG_MAXCONN REDIS_CLIENTS REDIS_MEM QUEUE_DEPTHS
export POSTINGS_COUNT EMBEDDED_COUNT API_ERRORS K6_EXIT

python3 - "$RUN_ID" "$SUMMARY_JSON" "$RAW_JSONL" <<'PY'
import json, os, subprocess, sys, datetime

run_id, summary_path, raw_path = sys.argv[1], sys.argv[2], sys.argv[3]

def env(name):
    return os.environ.get(name)

row = {
    "run_id":   run_id,
    "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "vus":      int(env("VUS") or 0),
    "duration": env("DURATION"),
    "label":    env("LABEL"),
    "base_url": env("BASE_URL"),
    "git_sha":  env("GIT_SHA"),
    "git_dirty": env("GIT_DIRTY") == "true",
    "summary_file": summary_path,
    # 99 = k6 threshold breach; 0 = clean. Recorded rather than fatal.
    "k6_exit_code": int(env("K6_EXIT") or 0),
    "thresholds_breached": (env("K6_EXIT") or "0") == "99",
    "cpu_budget": {
        "OMP_NUM_THREADS":   env("OMP_NUM_THREADS"),
        "TORCH_NUM_THREADS": env("TORCH_NUM_THREADS"),
        "API_CPUS":          env("API_CPUS"),
        "WORKER_CPUS":       env("WORKER_CPUS"),
        "WORKER_CONCURRENCY": env("WORKER_CONCURRENCY"),
    },
    "diagnostics": {
        "docker_stats":    env("DOCKER_STATS"),
        "pg_connections":  env("PG_CONNS"),
        "pg_max_connections": env("PG_MAXCONN"),
        "redis_clients":   env("REDIS_CLIENTS"),
        "redis_memory":    env("REDIS_MEM"),
        "queue_depths":    env("QUEUE_DEPTHS"),
        "postings":        env("POSTINGS_COUNT"),
        "embedded":        env("EMBEDDED_COUNT"),
        "api_error_lines": env("API_ERRORS"),
    },
}

# Fold in the headline k6 metrics so the JSONL is useful on its own.
try:
    with open(summary_path) as fh:
        summary = json.load(fh)
    m = summary.get("metrics", {})
    dur = m.get("http_req_duration", {})
    row["k6"] = {
        "http_reqs":      m.get("http_reqs", {}).get("count"),
        "rps":            m.get("http_reqs", {}).get("rate"),
        "failed_rate":    m.get("http_req_failed", {}).get("value"),
        "p50_ms":         dur.get("med"),
        "p95_ms":         dur.get("p(95)"),
        "p99_ms":         dur.get("p(99)"),
        "max_ms":         dur.get("max"),
        "vus_max":        m.get("vus_max", {}).get("value"),
    }
except Exception as exc:  # a missing/short summary must not lose the row
    row["k6_error"] = str(exc)

os.makedirs(os.path.dirname(raw_path) or ".", exist_ok=True)
with open(raw_path, "a") as fh:
    fh.write(json.dumps(row) + "\n")
print(f"\nAppended → {raw_path}  ({run_id})")
print(f"Summary  → {summary_path}")
PY
