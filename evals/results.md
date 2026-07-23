# TalentScope — Search Latency Eval Notes

## Vector-mode tail latency investigation (2026-07-23)

**Trigger**: `evals/benchmark.json` (600 samples) recorded `max_ms=165.9` for vector
mode and `stdev=8.4 ms` — noticeably higher variance than FTS (σ=2.1 ms).
The user asked whether the tail events cluster around specific queries or run
positions, indicating a systemic issue, vs. scattered OS jitter.

---

### Methodology

Three follow-up probe runs, each replaying the benchmark's 12 queries using
`vector_search()` directly (no HTTP layer):

| Run | Samples/mode | Warm-up | Outlier threshold |
|---|---|---|---|
| A — outlier_probe.py | 600 (50×12) | 2 per query | >50 ms |
| B — inline distribution probe | 600 (50×12) | 2 per query | none (full tail) |
| C — per-query breakdown | 360 (30×12) | **5 per query** | p95 per query |

Run C used 5 warm-up calls per query to isolate whether early-position
latency (plan-cache cold start) was driving the spikes.

---

### Results

**Run A**: 0 outliers >50 ms.  Max observed: ~30 ms.

**Run B** (full distribution):
```
n=600   min=10.5 ms   max=95.7 ms
p90=26.6 ms   p95=36.9 ms   p99=57.7 ms   p99.9=89.7 ms
```
Top-10 slowest: q04 (`data engineer spark kafka pipeline`) appeared 6 times,
scattered across positions 1, 2, 3, 5, 6, 32 in the repeat sequence.
q00, q02, q07 each contributed 1 entry.

**Run C** (per-query p50/p95/max, 5 warm-ups):
```
q03  backend Go microservices distributed   p50=15.8  p95=61.9  max=94.1  ← flagged
q04  data engineer spark kafka pipeline     p50=15.0  p95=28.4  max=37.1
all others                                  p50=11-15  p95=13-26  max=14-30
```
In this run, q03 was the tail contributor; q04 was unremarkable.

---

### Interpretation

**The slow query shifts between runs.**  q04 was the worst in run B; q03 was
the worst in run C; run A had no outliers at all.  Per-query p50 is tight
across all 12 queries (12–16 ms) in every run — no query is structurally
slower than the others.

The occasional >50 ms spikes (~1–2% of samples) are consistent with:
- **Python GC pauses**: the GC can pause a thread for 10–80 ms without notice;
  `gc.disable()` would likely eliminate most of these but is not appropriate
  in production.
- **OS scheduler preemption**: on a loaded dev machine, a 10–50 ms preemption
  is common and shows up as a one-off latency spike on whichever query is
  running at that moment.

The original 165.9 ms max from the benchmark is a tail sample of this same
distribution — it did not reproduce in any subsequent run (max observed across
all three probe runs: 94 ms).

---

### Conclusion

**Not a systemic issue.**  No query text, embedding neighborhood, or run
position is responsible for the tail latency.  The spikes are OS/GC jitter on
a local dev machine.

Numbers to use in interviews or on a resume:
- **p50 = 13–15 ms** (stable across all runs)
- **p95 = 20–40 ms** (run-to-run variance; quote the conservative end: ~40 ms)
- **p99 = 30–60 ms** (similarly, quote ~60 ms)
- Spike ceiling: ~95 ms, observed in 1–2 of every 600 samples

For a production deployment (dedicated Postgres, stable process, no GC
contention from other requests), the tail would narrow substantially.  On this
local dev machine, p95 ≤ 40 ms and p99 ≤ 60 ms are the honest numbers.

The README benchmark section already states "local dev machine — not a
production environment."

---

### Future work (not blocking)

- Run with `gc.freeze()` before the loop and compare tail widths to quantify
  GC contribution.
- Parallelize `fts_search` + `vector_search` in hybrid mode (requires separate
  sessions or async SQLAlchemy); would save ~10–15 ms at p50 at scale.
