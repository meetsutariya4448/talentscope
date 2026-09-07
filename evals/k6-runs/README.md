# What these files are, and what the old naming destroyed

Written 2026-09-06, while fixing the run-naming defect in `k6/run-stage.sh`.

## The defect

`run-stage.sh` named its output `vus${VUS}.json` — VU count only. No duration, no
timestamp, and critically **no condition**. But almost every comparison in
`evals/load-test.md` deliberately re-runs the *same* VU count under a *different*
condition (worker paused vs. running, 1 vs. 4 uvicorn processes). So each comparison
overwrote its own control, and only the last run at each VU level survived.

`evals/load-test.md` (lines 47–56) already conceded this. This file records the
specific consequences so the surviving artifacts are not read as something they aren't.

Fixed by naming runs `<timestamp>-vus<N>-<duration>-<label>.json` and requiring a
`--label` describing the condition. The files below are left exactly as they were —
rewriting them would be inventing history, not preserving it.

## What actually survives

Re-read from the files themselves on 2026-09-06 (`vus_max` confirms each filename's VU
count is at least internally accurate):

| File | vus_max | reqs | med | p95 | req/s | What this run actually is |
|---|---|---|---|---|---|---|
| `vus2.json` | 2 | 4 | 11613.6 ms | 24042.9 ms | 0.2 | Cold-start smoke test — 4 requests total. The 11.6 s median *is* the pre-warmup model load being paid inside requests, i.e. the bug fixed in `evals/coldstart.md`. Not a capacity measurement. |
| `vus5.json` | 5 | 235 | 156.7 ms | 1017.8 ms | 7.6 | Realistic ramp, 5 VUs. Matches `load-test.md`. ✅ |
| `vus10.json` | 10 | 356 | 224.1 ms | 1433.6 ms | 9.2 | Realistic ramp, 10 VUs. Matches `load-test.md`. ✅ |
| `vus20.json` | 20 | 350 | 723.2 ms | 4774.4 ms | 11.3 | **NOT the 20-VU realistic-ramp row.** `load-test.md` reports 415 ms / 2.67 s / 17.8 req/s there. This file is the "4 uvicorn processes, worker running" variant that overwrote it. |
| `vus40.json` | 40 | 1655 | 175.3 ms | 1450.2 ms | 54.1 | The "4 uvicorn processes, worker paused" isolated best case. **The 40-VU realistic-conditions run (1.04 s / 10.19 s / 12.7 req/s) is gone entirely** — no file survives for it. |
| `vus60.json` | 60 | 810 | 561.3 ms | 6801.0 ms | 25.5 | **Label conflict, unresolved.** These numbers are digit-for-digit what `load-test.md` attributes to its *40*-VU isolated single-process row, but the file's own `vus_max` says 60. Either the doc's row label or the run's VU argument is wrong, and the condition-less filename makes it unresolvable after the fact. Do not cite either reading. |
| `vus80.json` | 80 | 1309 | 356.2 ms | 6524.9 ms | 37.7 | Appears in no table in any doc. |

## Consequences for claims

- **54.1 req/s** (`vus40.json`) is a best-case isolated number — 4 uvicorn processes with
  the ingestion worker paused. `evals/load-test.md` already flags it; keep the caveat.
- The **realistic-conditions 40-VU result cannot be reproduced from these files.** It
  exists only as a row in `load-test.md`'s narrative.
- `TALENTSCOPE_RESUME_EVIDENCE.md` states the ramp covered "2, 5, 10, 20, 40, 60, 80".
  Seven files exist, but per the above they are not seven comparable points on one ramp.
- Nothing here carries a timestamp, so none of it can be tied to a commit.

Runs recorded after 2026-09-06 carry `run_id`, git SHA, label and CPU-budget envelope
both in the filename and in `evals/load-test-raw.jsonl`.
