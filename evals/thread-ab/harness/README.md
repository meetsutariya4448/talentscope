# Thread A/B harness

Reproduces `evals/thread-ab.md`. Preserved so the comparison can be re-run rather
than taken on trust.

```bash
# 1. Deploy the commit under test and seed a corpus
bash scripts/deploy.sh
bash scripts/dc.sh exec -T api python scripts/seed_demo.py --embed
bash scripts/dc.sh exec -T api python k6/seed_for_load_test.py

# 2. Capture the snapshot every trial resets to, and point SNAPSHOT= at it
bash scripts/backup_db.sh          # writes backups/<name>.dump.gz

# 3. Quiet the host: observability containers and beat are excluded so their
#    load does not land mid-measurement
bash scripts/dc.sh stop -t 5 prometheus grafana alertmanager otel-collector beat

# 4. Run until each configuration has 5 unstalled trials
bash evals/thread-ab/harness/run_ab.sh

# 5. Analyse
python evals/thread-ab/harness/analyze.py
```

## Files

| File | Purpose |
|---|---|
| `trial.sh` | One trial: reset dataset, apply config, verify equivalence, load, record |
| `run_ab.sh` | Alternates configurations until each has 5 unstalled trials (max 16 attempts) |
| `record_trial.py` | Writes one JSON line: config, held-fixed values, k6 metrics, per-status request counts, ingestion rate, stall flag |
| `analyze.py` | Medians, min/max, ratios, group separation, and an equivalence check across trials |

## Two decisions worth knowing about

**Throughput comes from the API's own counter, not k6's `rate`.** k6 computes
rate as count ÷ wall-clock, so a host stall destroys it. `record_trial.py` diffs
`http_requests_total` across the window and filters out `/ready`, `/health` and
`/metrics`, which are infrastructure traffic rather than load-test requests.

**Trials reset from a local dump, not through `scripts/restore_db.sh`.** The
product restore path pulls from S3, which would mean keeping LocalStack resident
purely to serve one file into a loop — on a memory-constrained host that was itself
a source of stalls. The product restore path is exercised separately in
`docs/incidents/2026-09-07-data-loss-restore.md`.
