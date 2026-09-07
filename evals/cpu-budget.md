# Inference CPU budgets, measured with ingestion active

**Date**: 2026-09-07 (UTC)
**Host**: Apple Silicon, Docker Desktop VM — 10 CPUs, 8.32 GB RAM
**Stack**: `docker-compose.yml` + `docker-compose.loadtest.yml` + `docker-compose.cpubudget.yml`,
isolated (no proxy replica, no Grafana/cAdvisor containers running)
**Load**: `k6/load_test.js`, 60 VUs, 45 s per cell
**Raw**: `evals/load-test-raw.jsonl` (one row per run, with the budget envelope and
post-stage diagnostics), summaries in `evals/k6-runs/<timestamp>-vus60-45s-<label>.json`

## Why this had to be measured rather than assumed

There were **no resource constraints anywhere in the repo** before this — not in
`docker-compose.yml`, not in either loadtest overlay. Every previous load test ran
unconstrained against all 10 host cores, so the API and the ingestion worker never
actually had to share anything. "API and ingestion coexist" had never been tested.

`docker-compose.cpubudget.yml` adds the first explicit budgets.

## The result that matters: threads, not cores

> **Superseded for the thread comparison.** The table immediately below is a
> **single run per cell**. The same comparison was later replicated properly —
> 10 alternating trials, dataset reset before each, 5 per configuration — in
> [`evals/thread-ab.md`](thread-ab.md). Replication put the effect **lower**:
> **4.8× throughput** and **13.9× p95**, not the 6.7× and 16× below. Cite
> `thread-ab.md`, not this section. The rest of this document (the rate-limit
> finding, the memory finding, the recommended settings) still stands.


Identical CPU budget (api 2.0, worker 2.0), identical load, ingestion active in both.
The only difference is `OMP_NUM_THREADS`.

| | `OMP_NUM_THREADS=1` | `OMP_NUM_THREADS=10` | ratio |
|---|---|---|---|
| API req/s | **100.3** | 15.0 | **6.7×** |
| API p50 | **87.1 ms** | 1537.4 ms | **17.6×** |
| API p95 | **720.0 ms** | 11598.9 ms | **16.1×** |
| API max | 1844.7 ms | 15662.4 ms | 8.5× |
| k6 thresholds | passed | **breached** | |
| Embedding throughput (mean) | **28.4/s** | 3.9/s | **7.3×** |
| Embedding throughput (peak) | 52.7/s | 6.0/s | 8.8× |
| Worker CPU post-stage | 0.14% (drained) | 202.7% (pinned at its 2-CPU limit) | |

Both tiers get *worse* when the thread count goes up. Nothing was traded for anything.

### Why

A container's CPU limit is a cgroup quota. `os.cpu_count()` does not see it:

```
# same container, --cpus=2.0, measured 2026-09-06
os.cpu_count()          -> 10        # the host's cores, not the quota
torch.get_num_threads() -> 10        # without OMP_NUM_THREADS
torch.get_num_threads() -> 1         # with OMP_NUM_THREADS=1
```

So torch starts 10 OpenMP threads inside a 2-CPU budget — 5× oversubscription — and
spends the quota context-switching between threads that cannot run in parallel. Setting
a CPU limit without also pinning the thread count is the trap; the limit alone makes
things worse than no limit, because the same oversubscription is now squeezed into less
CPU.

`evals/load-test.md` had already identified thread oversubscription as the bottleneck.
This is where that finding becomes a setting rather than an observation.

## Full matrix

All cells: 60 VUs, 45 s, ingestion dispatching 1400 `embed_posting` tasks unless noted.

| Cell | api CPU | worker CPU | OMP | conc | req/s | p50 | p95 | embed/s | notes |
|---|---|---|---|---|---|---|---|---|---|
| `api2-w2-omp1-noingest` | 2.0 | 2.0 | 1 | 2 | 103.3 | 43.5 ms | 460.5 ms | — | control: API alone |
| `api2-w2-omp1-ingest` | 2.0 | 2.0 | 1 | 2 | **100.3** | 87.1 ms | 720.0 ms | **28.4** | the recommended budget |
| `api2-w2-omp1-ratelimited` | 2.0 | 2.0 | 1 | 2 | 116.6 | 48.7 ms | 563.1 ms | 2.1 | default `EMBED_RATE_LIMIT=300/m` |
| `api2-w2-omp10-ingest` | 2.0 | 2.0 | 10 | 2 | 15.0 | 1537.4 ms | 11598.9 ms | 3.9 | thresholds breached |
| `api1-w1-omp1-ingest` | 1.0 | 1.0 | 1 | 1 | 58.0 | 208.7 ms | 1680.4 ms | 25.9 | half the budget |

Zero failed requests in every cell, including the one that breached the latency
thresholds — the API degraded in latency, never in correctness.

## Coexistence cost is small when the budget is right

At the recommended setting, adding full-rate ingestion to the API's load costs:

- **3% of API throughput** (103.3 → 100.3 req/s)
- **43.5 → 87.1 ms** at p50, **460 → 720 ms** at p95

for 28.4 embeddings/s of concurrent ingestion. That is the answer to "can API and
ingestion coexist within an explicit budget": yes, at 2 CPUs each with threads pinned.

Halving the budget to 1 CPU each still works — 58 req/s, p95 1.68 s, 25.9 embed/s — but
p95 is 2.3× worse and it sits close to the 2 s threshold. 2+2 is the recommendation;
1+1 is the floor.

## Second finding: the rate limit, not CPU, was the real ceiling

`embed_posting` carried a hardcoded `rate_limit: "300/m"` (5/s). At that setting the
worker drained the queue at **2.1/s mean, 5.3/s peak** while sitting at ~6% CPU — the
CPU budget was nowhere near binding, the configured rate limit was. Removing it gave
**28.4/s**, a **13.5× increase**, and cost the API only ~3% throughput.

The limit is deliberate backpressure and the default is unchanged. But it is now
`EMBED_RATE_LIMIT` (`app/config.py`) rather than a hardcoded constant, because a value
that dominates throughput by more than an order of magnitude should be tunable without
a code change — and because measuring a "CPU budget" against a workload that is actually
rate-limited measures nothing.

## Third finding: a 2 GiB worker at 99.7% memory

An early run showed the worker pinned at **1.994 GiB of a 2 GiB limit** with 13 celery
processes despite `--concurrency=2`. Cause: a YAML folded scalar (`>`) in the compose
overlay preserved the newline on a more-indented continuation line, so `sh -c` received
two commands and every flag after the break — including `--concurrency` — was silently
dropped. Celery fell back to its default of `os.cpu_count()` = 10 children, each holding
its own **380–420 MB** copy of the model.

Two consequences now encoded in the configuration:

- Worker commands are written on one line (`docker-compose.yml`, `docker-compose.cpubudget.yml`).
- `WORKER_MEMORY` must scale with `WORKER_CONCURRENCY`: budget ~450 MB per child plus
  ~150 MB for the parent. The 380–420 MB measured here is higher than the ~280 MB
  assumed in `k8s/21-worker.yaml`.

Warmup time also degrades sharply under contention: **2.4 s per child** at concurrency 2,
versus **15.3–16.5 s per child** when 10 children warmed simultaneously inside the same
2-CPU budget.

## Recommended settings

```bash
API_CPUS=2.0  API_MEMORY=2g
WORKER_CPUS=2.0  WORKER_CONCURRENCY=2  WORKER_MEMORY=1400m
OMP_NUM_THREADS=1        # and TORCH_NUM_THREADS / MKL_NUM_THREADS
EMBED_RATE_LIMIT=300/m   # deliberate backpressure; raise knowingly
```

These are the defaults in `docker-compose.cpubudget.yml` and `docker-compose.deploy.yml`.

## Boundaries on these numbers

- **Single run per cell.** No repetition, no confidence intervals. Differences of a few
  percent between cells are not meaningful. The thread-pinning difference was
  later replicated across 10 alternating trials (evals/thread-ab.md), which is
  the number to cite; the single-run 6.7×/16× figures here overstated it.
- One host, one Docker Desktop VM (10 CPUs, 8.32 GB). The *shape* of the thread-
  oversubscription result should hold anywhere the quota is below the host core count;
  the absolute numbers should not be quoted as capacity for any other hardware.
- 45 s per cell measures steady-state under constant load, not sustained behaviour,
  memory growth, or recovery from saturation.
- Corpus was 1,500 synthetic postings (`k6/seed_for_load_test.py`), not production data.
- `/qa/ask` is excluded from the k6 mix (it calls a paid provider), so these numbers
  describe search and analytics traffic only.
- The `api2-w2-omp1-noingest` control shows an 8.0 s max against a 460 ms p95 — a single
  outlier, most plausibly host scheduling noise. It is left in rather than trimmed.
