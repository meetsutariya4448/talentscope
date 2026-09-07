#!/usr/bin/env bash
# Alternate configurations until each has TARGET unstalled trials, or ATTEMPTS
# is exhausted. Stalled trials are kept in the record (they are evidence about
# the host) but do not count toward the target — a trial whose 45s load test
# took 945s of wall clock measured the laptop, not the application.
SP="$(cd "$(dirname "$0")" && pwd)"
cd ~/Desktop/talentscope
TARGET=5
ATTEMPTS=16

clean_count() {  # $1 = config label (omp1|omp10)
  [ -f evals/thread-ab/trials.jsonl ] || { echo 0; return; }
  python3 - "$1" <<'PY'
import json,sys,pathlib
cfg=sys.argv[1]; n=0
p=pathlib.Path("evals/thread-ab/trials.jsonl")
for line in p.read_text().splitlines():
    if not line.strip(): continue
    r=json.loads(line)
    if r["config"]==cfg and not r["timing"]["host_stall_detected"]: n+=1
print(n)
PY
}

i=0
while [ $i -lt $ATTEMPTS ]; do
  c1=$(clean_count omp1); c10=$(clean_count omp10)
  echo "--- progress: omp1 clean=$c1  omp10 clean=$c10  (target $TARGET each, attempt $((i+1))/$ATTEMPTS)"
  [ "$c1" -ge "$TARGET" ] && [ "$c10" -ge "$TARGET" ] && break
  i=$((i+1))
  # Always alternate, so drifting background load is shared between configs.
  if [ "$c1" -le "$c10" ]; then bash $SP/trial.sh 1 "$i" || true
  else bash $SP/trial.sh 10 "$i" || true; fi
  c1=$(clean_count omp1); c10=$(clean_count omp10)
  [ "$c1" -ge "$TARGET" ] && [ "$c10" -ge "$TARGET" ] && break
  i=$((i+1))
  if [ "$c10" -le "$c1" ]; then bash $SP/trial.sh 10 "$i" || true
  else bash $SP/trial.sh 1 "$i" || true; fi
done
echo "=== AB COMPLETE (omp1 clean=$(clean_count omp1), omp10 clean=$(clean_count omp10)) ==="
