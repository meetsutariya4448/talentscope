# Load test: how much traffic can TalentScope sustain, and on what?

**Answer up front**: on this hardware (8 physical CPU cores, Docker Desktop),
realistic sustained capacity with the background ingestion worker running
normally is roughly **10-20 concurrent users** before p95 latency crosses
into unacceptable territory (>2-3s). The limiting resource is **CPU**,
specifically `sentence-transformers`' per-request `model.encode()` call
(vector/hybrid search) and the Celery worker's embedding backlog
processing — both competing for the same physical cores with no thread
budget set. **Postgres connections, Redis, and the DB query layer itself
were never the bottleneck** at any point in this test — worth stating
explicitly since DB pooling was the a priori suspect going in (it was the
subject of Phase 2's tuning work), and it stayed comfortably idle
throughout.

## Setup

Per the explicit isolation requirement for this phase: LocalStack and its
containers stopped, kind cluster deleted, only `docker-compose.yml`'s
`postgres`/`redis`/`api`/`worker`/`beat` running (no prometheus/grafana/
cadvisor — `docker stats` used directly instead, to keep footprint
minimal). `docker-compose.loadtest.yml` overlay drops the api service's
dev-only `--reload` flag (incompatible with `--workers`, adds file-watcher
overhead — neither representative of what's being measured).

Corpus: 1,500 synthetic postings seeded for realistic scale (`k6/seed_for_load_test.py`
— deliberately *not* the 20k-row corpus `scripts/db_engineering_report.py`
uses; that one exists to stress-test individual indexes in isolation, a
different question from end-to-end request-serving capacity) plus ~2,500
real postings the app's own beat scheduler pulled from Greenhouse/Lever/Ashby
in the background during setup — real production-shaped data and real
background Celery activity, not purely synthetic.

Traffic mix (`k6/load_test.js`): 35% FTS search, 20% vector search, 20%
hybrid search, 10% skill-demand analytics, 8% top-companies, 7% posting
stats, with 100-600ms randomized "think time" between requests per virtual
user. `/qa/ask` deliberately excluded from the ramp — it calls the real
Groq API, and a load test that hits an external LLM provider's rate limits
measures Groq's capacity, not this system's.

Method: discrete stages (`k6/run-stage.sh VUS DURATION`), not one
unattended ramp to a fixed peak — resource state (`docker stats`, Postgres
`pg_stat_activity`, Redis `INFO`, Celery queue depth) checked between each
stage, ramp stopped once the machine was clearly saturated rather than
pushed further.

Note on `evals/k6-runs/*.json`: `run-stage.sh` names its output by VU
count alone (`vus20.json`), so a VU level re-run under a different
condition (worker paused vs. running, 1 vs. 4 uvicorn processes — several
comparisons below reuse the same VU counts deliberately) overwrites the
previous file. The numbers in every table below are transcribed directly
from each run's actual terminal output at the time, not reconstructed from
these files after the fact — the JSON files reflect only the *last* run at
each VU count (the final realistic combined-conditions test), kept as a
representative raw sample, not a complete run-by-run archive.

## Bug found before any real ramp data: a request-serving race

At just **2 concurrent VUs**, one vector-search request failed outright
with a 500:

```
NotImplementedError: Cannot copy out of meta tensor; no data! Please use
torch.nn.Module.to_empty() instead of torch.nn.Module.to() when moving
module from meta to a different device.
```

Root cause: `app/search/encoder.py`'s `get_model()` lazily constructs the
`SentenceTransformer` on first use with an unlocked `if _model is None`
check. FastAPI runs sync route handlers on a real OS threadpool, so two
concurrent requests on a cold process can both see `_model is None`
simultaneously and both call the constructor at once — which PyTorch's
meta-tensor module initialization isn't safe against. **Fixed** with a
double-checked-locking pattern (`threading.Lock`) before any load-test
numbers below were collected — otherwise every stage would be measuring a
process crashing under its own cold start, not steady-state behavior.
Regression test: `tests/test_encoder.py`, asserts the model is constructed
exactly once under 10-thread concurrent access.

## Second finding: a real cold-start cost, and a readiness gap

After the race fix, the *first* vector-touching request after a fresh
process start still took **9.6-19s** (real model load off disk/cache), with
concurrent requests during that window now correctly queuing behind the
lock instead of crashing. Once warm, latency settles to ~175-220ms for a
single request.

This exposes a real gap, not just a curiosity: `/ready` (`app/main.py`)
checks DB and Redis connectivity but never touches the model, so a freshly
deployed pod/container would be marked ready and start receiving real
traffic *before* the model is loaded — the first users to hit vector/hybrid
search on a fresh deploy eat a multi-second latency spike. Documented here
as a finding, not fixed in this pass; the concrete fix would be either an
eager warmup call at startup or extending `/ready`'s checks to include
model-loaded state.

## The ramp: realistic conditions (worker running normally)

| VUs | p50 | p95 | p99* | req/s | error rate |
|---|---|---|---|---|---|
| 5  | 157ms | 1.01s | ~2.1s | 7.6 | 0% |
| 10 | 224ms | 1.43s | ~13.9s (1 outlier) | 9.2 | 0% |
| 20 | 415ms | 2.67s | ~4.1s | 17.8 | 0% |
| 40 | 1.04s | 10.19s | ~12.6s | 12.7 (**down** from 20's 17.8) | 0% |

\* p99 approximated from max/tail shape at this sample size, not a precise
k6-computed percentile.

40 VUs is the stop point: **throughput went down, not up**, between 20 and
40 VUs (17.8 → 12.7 req/s) while p95 nearly quadrupled — the textbook
signature of a saturated system thrashing, not one still scaling. No
request ever hard-failed (0% error rate throughout) — the failure mode
here is pure latency collapse, not errors, worth noting since "no errors"
alone would be a misleading capacity signal on its own.

## Isolating the bottleneck: worker paused vs. running

Controlled comparison at the same 20-VU stage:

| Condition | p50 | p95 | req/s |
|---|---|---|---|
| Worker running (normal ingestion+embedding) | 415ms | 2.67s | 17.8 |
| **Worker paused** | 108ms | **935ms** | **32.7** |

Pausing the background Celery worker (which was steadily using ~700-750%
CPU — 7+ of 8 physical cores — processing a 1,000+ item embedding backlog
from real ingestion) **nearly doubled throughput and cut p95 latency by
2.9x**, even though the api container's own CPU usage was under 1% the
entire time by `docker stats`' point-in-time reading. This is the first
real bottleneck: **the worker and api tiers compete for the same finite
CPU pool on a single host**, and the worker's background embedding
processing wins that contention by default.

(Point-in-time `docker stats` snapshots after a stage had ended
consistently under-reported api CPU — e.g. showing 0.23% right after a run
that had visibly terrible latency. Live-sampled `docker stats` *during* a
run told the real story: the api container itself spikes to 600-700% CPU
under concurrent vector-search load once the worker's confound is removed.
Worth calling out as a methodology note — a single post-hoc snapshot is not
sufficient evidence of "not the bottleneck.")

## Isolated api ceiling (worker still paused)

| VUs | p50 | p95 | req/s |
|---|---|---|---|
| 20 | 108ms | 935ms | 32.7 |
| 40 | 561ms | 6.80s | 25.5 (down from 20's 32.7) |

Even fully isolated from the worker, a single uvicorn process saturates
somewhere between 20 and 40 VUs — confirmed via live CPU sampling during
the 40-VU run: the api container itself spiked to 600-700% CPU (using
6-7 of 8 physical cores on its own, via PyTorch's native code releasing the
GIL during `model.encode()`).

## Focused fix #1: `uvicorn --workers 4` (isolated from worker)

The standard prescribed remedy for "single Python process saturating under
load." Tested at 40 VUs, worker container still paused, api restarted with
`--workers 4` (`docker-compose.loadtest-workers4.yml`):

| Config | p50 | p95 | req/s |
|---|---|---|---|
| 1 uvicorn process | 561ms | 6.80s | 25.5 |
| **4 uvicorn processes** | **175ms** | **1.45s** | **54.1** |

A clear, real win in isolation: **2.1x throughput, 4.7x better p95**. This
was not a foregone conclusion — a single process was already observed
spiking to 600-700% CPU via GIL-released native code, so more OS processes
weren't guaranteed to help. They did: 4 separate processes (4 separate
GILs) parallelize this workload meaningfully better than one process's
threadpool does.

## Focused fix #1, retested under realistic conditions (worker running)

The critical follow-up: does the workers=4 fix still help once it's
competing with the worker's background load again — the condition that
actually matters, since a real deployment doesn't get to assume the
ingestion worker is paused?

| Config | Worker state | p50 | p95 | req/s |
|---|---|---|---|---|
| 1 uvicorn process | running | 415ms | 2.67s | 17.8 |
| **4 uvicorn processes** | **running** | **723ms** | **4.77s** | **11.3** |

**It got worse, not better.** Adding 4 api processes on top of the
worker's own 4 concurrent child processes — each independently defaulting
to using all 8 cores for PyTorch's internal thread pool
(`torch.get_num_threads()` confirmed = 8, no `OMP_NUM_THREADS` or
equivalent set anywhere in the image) — means up to 8 OS processes each
trying to claim 8 threads on 8 physical cores. That's the real root cause,
one layer below "CPU contention": **thread oversubscription**, not just
process count. Multiple uvicorn workers is a real fix only when nothing
else CPU-heavy is running on the same host; combined with the worker
tier's own unbounded thread usage, it makes the oversubscription worse.

## Answering the question

**How much traffic can TalentScope sustain on this hardware before latency
or errors become unacceptable, and what's the limiting resource first?**

- Under realistic conditions (background ingestion worker running, the
  actual production shape), sustained capacity is **~10-20 concurrent
  users** before p95 crosses into multi-second territory. Errors never
  occurred at any load level tested — the system degrades via latency
  collapse, not failed requests, which is a meaningfully different
  operational signature to alert on.
- The limiting resource is **CPU**, specifically **thread oversubscription
  from PyTorch's default full-core thread pool, multiplied across every
  process that touches the embedding model** (api workers and Celery
  worker children alike) — not the database, not Redis, not connection
  pooling (Phase 2's tuning target never became relevant here; Postgres
  never exceeded a dozen-odd connections against a pool budgeted for 30).
- The single highest-leverage fix suggested by this data, **not yet
  applied or tested** (would be a third focused test beyond this phase's
  scope): set `OMP_NUM_THREADS=1` (and `MKL_NUM_THREADS`/
  `OPENBLAS_NUM_THREADS` as applicable) on both the api and worker
  containers, so each process's PyTorch calls use exactly one thread and
  the *process/container* count becomes the only source of parallelism —
  avoiding the double-parallelism (N processes × M threads each) this test
  found. `uvicorn --workers N` is then likely to actually help under
  realistic combined load, which it currently does not.
- This also validates, from a different angle than Phase 4's Kubernetes
  work, the same underlying architectural conclusion: **api and worker
  need to not share a CPU pool** at meaningful scale. Kubernetes' separate
  Deployments are structurally the right answer — but only once they land
  on genuinely separate physical nodes; on this same single-host laptop,
  container/pod boundaries don't create more physical cores, which is
  exactly why Phase 4's 8-replica worker scaling test hit its own ceiling
  for the same root reason.
