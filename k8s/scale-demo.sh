#!/usr/bin/env bash
# Demonstrates worker throughput at 2, 4, and 8 replicas: seeds a batch of
# un-embedded synthetic postings tagged with a unique run id, dispatches
# embed_posting for all of them (real CPU-bound work — sentence-transformers
# inference, not a sleep() stand-in), scales the worker Deployment, and
# polls the database for completion count.
#
# Completion is measured via the DATABASE, not any single worker pod's
# /metrics — each replica aggregates only its own forked children's
# Prometheus counters (see app/observability.py's module docstring), so at
# replica counts > 1 the work is spread across pods with independent
# counters. Scraping one arbitrary pod (which is all `kubectl port-forward
# svc/worker` can reach — it sticks to one endpoint) would undercount
# cluster-wide throughput. The database's posting.embedding column being
# non-null is unambiguous ground truth regardless of which pod did the work.
#
# The worker HPA (k8s/21-worker.yaml) is paused for the duration — it would
# otherwise fight kubectl scale by reacting to the very load this generates.
set -euo pipefail
cd "$(dirname "$0")/.."

NS=talentscope
BATCH_SIZE="${BATCH_SIZE:-150}"
RESULTS_FILE=evals/k8s-scaling.md
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"

echo "Pausing worker HPA for the manual scaling demo..."
kubectl delete hpa/worker -n "$NS" --ignore-not-found

api_pod() { kubectl get pods -n "$NS" -l app=api -o jsonpath='{.items[0].metadata.name}'; }

seed_and_dispatch() {
  local tag=$1
  kubectl exec -n "$NS" "$(api_pod)" -- python -c "
from app.database import SessionLocal
from app.models import Company, Posting
from app.tasks.embedding import embed_posting

db = SessionLocal()
co = db.query(Company).filter_by(slug='k8s-scale-demo').first()
if not co:
    co = Company(name='K8s Scale Demo', slug='k8s-scale-demo')
    db.add(co)
    db.flush()

ids = []
for i in range($BATCH_SIZE):
    p = Posting(
        company_id=co.id, title=f'Scale Demo Role $tag-{i}',
        description='Python Kubernetes distributed systems at scale',
        source='k8s-scale-demo', source_id='$tag-' + str(i), currency='USD',
    )
    db.add(p)
    db.flush()
    ids.append(p.id)
db.commit()
db.close()

for pid in ids:
    embed_posting.delay(pid)
print(f'dispatched {len(ids)} embed_posting tasks')
"
}

completed_count() {
  local tag=$1
  kubectl exec -n "$NS" "$(api_pod)" -- python -c "
from sqlalchemy import select, func
from app.database import SessionLocal
from app.models import Posting
db = SessionLocal()
n = db.execute(
    select(func.count()).select_from(Posting)
    .where(Posting.source == 'k8s-scale-demo')
    .where(Posting.source_id.like('$tag-%'))
    .where(Posting.embedding.isnot(None))
).scalar()
print(n)
db.close()
" 2>/dev/null | tail -1
}

run_at_replicas() {
  local n=$1
  local tag="run${n}-$(date +%s)"
  echo "=== Scaling worker to $n replicas ==="
  kubectl scale deployment/worker -n "$NS" --replicas="$n"
  kubectl rollout status deployment/worker -n "$NS" --timeout=180s
  sleep 5   # let readiness settle post-rollout before timing starts

  local start end elapsed rate done_count waited=0
  start=$(date +%s)
  seed_and_dispatch "$tag"
  echo "Waiting for batch to complete (tag=$tag)..."
  while true; do
    done_count=$(completed_count "$tag")
    if [ "${done_count:-0}" -ge "$BATCH_SIZE" ]; then
      break
    fi
    if [ "$waited" -ge "$TIMEOUT_SECONDS" ]; then
      echo "Timed out waiting for batch at $n replicas ($done_count/$BATCH_SIZE done)"
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done
  end=$(date +%s)
  elapsed=$((end - start))
  rate=$(echo "scale=2; $done_count / $elapsed" | bc)
  echo "$n replicas: $done_count/$BATCH_SIZE tasks in ${elapsed}s -> ${rate} tasks/sec"
  echo "| $n | $done_count | $elapsed | $rate |" >> "$RESULTS_FILE.tmp"
}

mkdir -p evals
{
  echo "# Kubernetes worker scaling: throughput vs. replica count"
  echo ""
  echo "Workload: embed_posting (real sentence-transformers inference, not a"
  echo "sleep stand-in) on $BATCH_SIZE synthetic postings per run, dispatched"
  echo "after scaling the worker Deployment to each replica count. Completion"
  echo "measured against the database (postings.embedding IS NOT NULL),"
  echo "not any single worker pod's local metrics — see k8s/scale-demo.sh."
  echo ""
  echo "| Worker replicas | Completed | Elapsed (s) | Throughput (tasks/sec) |"
  echo "|---|---|---|---|"
} > "$RESULTS_FILE.tmp.header"
rm -f "$RESULTS_FILE.tmp"
touch "$RESULTS_FILE.tmp"

for n in 2 4 8; do
  run_at_replicas "$n"
done

cat "$RESULTS_FILE.tmp.header" "$RESULTS_FILE.tmp" > "$RESULTS_FILE"
rm -f "$RESULTS_FILE.tmp" "$RESULTS_FILE.tmp.header"
echo "Wrote $RESULTS_FILE"
