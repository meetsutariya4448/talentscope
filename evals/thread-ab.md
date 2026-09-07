# Inference-thread A/B: 10 alternating trials

**Date**: 2026-09-07
**Commit under test**: `fdd0585` (CI-verified — GitHub Actions run 34076827737, 171 tests passed)
**Image**: `talentscope:fdd0585` (identical in every trial)
**Raw data**: `evals/thread-ab/trials.jsonl` (one row per trial), k6 summaries alongside as
`<timestamp>-omp<N>-t<i>.k6.json`

## Question

At a fixed container CPU quota, does pinning the OpenMP/torch thread pool to that
quota change API throughput and latency, and ingestion throughput, when nothing else
differs?

This replaces an earlier single-run-per-cell comparison. That measurement reported
6.7× throughput and 16× p95; replicated properly the effect is **smaller** — 4.8× and
13.9×. The single-run numbers should not be used.

## Method

- **Dataset reset before every trial.** Each trial drops the database and restores the
  same `pg_dump -Fc` snapshot (1,732 postings, all embedded), so every trial starts
  from byte-identical data.
- **Alternating A/B/A/B** throughout, so background load on a shared machine drifts
  across both configurations rather than favouring one.
- **Top-up design**: trials ran until each configuration had 5 unstalled results.
  All 10 trials were clean, so none were discarded.
- **Ingestion active in every trial**: 1,400 `embed_posting` tasks dispatched before
  the load starts, with `EMBED_RATE_LIMIT` empty so CPU, not a configured rate cap,
  is the binding constraint.
- **Load**: k6, 60 VUs, 45 s, the standard mix in `k6/load_test.js` (FTS / vector /
  hybrid search plus analytics). `/qa/ask` is excluded — it calls a paid provider.

### Held fixed, and re-verified inside every trial

Read back from the running containers each time, not assumed from the compose file:

| | value |
|---|---|
| api CPU / memory limit | 2.0 CPU / 2 GiB |
| worker CPU / memory limit | 2.0 CPU / 1.4 GiB |
| worker concurrency (celery processes) | 2 (3 processes: parent + 2 children) |
| `EMBED_RATE_LIMIT` | empty (no cap) |
| VUs / duration | 60 / 45 s |
| ingestion backlog | 1,400 tasks |
| dataset | 1,732 postings |
| image | `talentscope:fdd0585` |

**Varied**: `OMP_NUM_THREADS` / `TORCH_NUM_THREADS` / `MKL_NUM_THREADS` only —
confirmed in-container as `torch.get_num_threads()` = 1 vs 10 in every trial.

No tracked source file changed during the run (`git diff --quiet HEAD` clean
throughout); the only new files were the result artifacts.

## Why throughput is measured server-side

k6 reports `http_reqs.rate` as count ÷ wall-clock. A host stall inflates the wall
clock and destroys that rate — and `avg` with it — while leaving percentiles, which
are order statistics, intact. During earlier attempts on this machine one trial
recorded a 623 s `http_req_duration` inside an iteration whose own maximum was 1.7 s,
which is arithmetically impossible for real request latency.

Throughput here is therefore the API's own `http_requests_total` counter, diffed
across the measured window and filtered to application paths (`/ready`, `/health` and
`/metrics` excluded). Trials whose maximum request duration exceeded 60 s are flagged
`host_stall_detected` and excluded from medians. **In this run, zero trials stalled**
— k6 wall-clock was 46–50 s for a 45 s test in all ten.

## Results

Median across 5 trials per configuration, with observed min/max:

| Metric | `OMP_NUM_THREADS=1` | `OMP_NUM_THREADS=10` | Ratio |
|---|---|---|---|
| Throughput (2xx/s, server-side) | **100.1** (99.4–106.3) | 20.7 (15.8–25.0) | **4.8×** |
| p50 latency | **60.0 ms** (48.3–61.3) | 989.4 ms (737.7–1600.2) | **16.5× lower** |
| p95 latency | **665.5 ms** (601.8–683.7) | 9,238.7 ms (7,717.7–10,766.9) | **13.9× lower** |
| Ingestion | **29.9/s** (29.3–29.9) | 5.7/s (2.6–6.1) | **5.3×** |

**The two groups do not overlap.** The slowest `OMP=1` trial (100.0 req/s) still beat
the fastest `OMP=10` trial (24.8 req/s); the worst `OMP=1` p95 (684 ms) was still
better than the best `OMP=10` p95 (7,718 ms).

Both tiers improve together. Nothing was traded for anything.

### Request outcomes

Every request in the run succeeded:

| | `OMP=1` | `OMP=10` |
|---|---|---|
| 2xx | 23,434 | 4,901 |
| 429 rate-limited | 0 | 0 |
| 503 unavailable | 0 | 0 |
| other 4xx / 5xx | 0 | 0 |
| error rate | 0.0000 | 0.0000 |

The `OMP=10` configuration is slower, not broken: it served every request correctly,
just far fewer of them and far later. The k6 latency threshold (`p95 < 2000 ms`) was
breached in all five `OMP=10` trials and met in all five `OMP=1` trials.

Variability differs meaningfully between the groups. `OMP=1` is tight — throughput
spans 99.4–106.3 and ingestion 29.3–29.9. `OMP=10` is wide: throughput 15.8–25.0 and
ingestion 2.6–6.1, roughly a 2.3× spread. That is the signature of a saturated
system, where small scheduling differences change the outcome a lot.

## Mechanism

A container CPU limit is a cgroup quota, and `os.cpu_count()` does not see it:

```
# same container, --cpus=2.0
os.cpu_count()          -> 10        # the host's cores, not the quota
torch.get_num_threads() -> 10        # default
torch.get_num_threads() -> 1         # with OMP_NUM_THREADS=1
```

torch sizes its intra-op pool from the host's core count, so a container limited to
2 CPUs starts 10 OpenMP threads — 5× oversubscription — and spends the quota
context-switching between threads that cannot run in parallel.

## What this does and does not attribute

**Attributable**: the thread-pool setting. It was the only variable that differed,
and the eleven fixed parameters above were read back from the running containers in
every trial.

**Not attributable**: image, dataset, CPU and memory limits, worker concurrency,
ingestion backlog, rate-limit configuration, VU count and duration — all identical.

## Limitations

- **One host, one dataset size.** A shared Apple Silicon laptop (Docker Desktop VM:
  10 CPUs, 8.32 GB) with other applications resident and ~1.85 GB of swap in use. The
  *direction* should hold wherever a container quota sits below the host core count;
  the absolute numbers should not be quoted as capacity for other hardware.
- **5 trials per configuration**, medians with min/max — not a confidence interval.
  The claim rests on the groups being disjoint, not on a significance test.
- **45 s of steady-state load.** Says nothing about sustained behaviour, memory
  growth, or recovery from saturation.
- **Synthetic corpus** of 1,732 postings, not production data.
- **Search and analytics traffic only.** `/qa/ask` is excluded from the load mix.
- Earlier single-run figures (6.7× / 16×) are superseded by this run and should not
  be cited.
