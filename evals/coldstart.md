# Cold start: readiness gating for vector search

**Date**: 2026-09-06 (UTC-7 timestamps in the raw logs)
**Host**: Apple Silicon, Docker Desktop VM — 10 CPUs, 8.32 GB RAM allocated
**Stack**: `docker-compose.yml` + `docker-compose.loadtest.yml` + `docker-compose.proxy.yml`
(no `--reload`; two api replicas `api`/`api2` behind nginx)
**Raw evidence**: `evals/coldstart/` — probe samples, warmup log lines, metric values

## The defect

`/ready` (`app/main.py`) checked the database and Redis only. The embedding model is a
per-process lazy singleton (`app/search/encoder.py`) and nothing warmed it — no
`lifespan`, no startup event, no Celery signal. A freshly started process therefore
reported itself Ready, and the first `mode=vector` / `mode=hybrid` request loaded the
model *inside the request*.

## The fix

1. Background warmup thread at startup (`app/main.py` `lifespan`) — a daemon thread, so
   uvicorn binds immediately and `/health` stays 200 throughout. A blocking warmup would
   make liveness fail during the load, and the container would be killed and restarted
   into the same load.
2. `/ready` gained a third check, `embedding_model`, and returns 503 until it passes.
3. The vector/hybrid request paths refuse with **503 + `Retry-After: 5`**
   (`app/api/guards.py`) rather than loading the model inline.
4. The model is baked into the image (`Dockerfile`) with `HF_HUB_OFFLINE=1`, so warmup
   never depends on a HuggingFace download.

## Measured: warmup is 97% first-encode, not construction

From `evals/coldstart/warmup-log.txt`:

```
21:54:43,524  Loading sentence-transformer model (all-MiniLM-L6-v2)…
21:54:43,604  Model loaded.
21:54:43,619  Embedding model warm after 2.56 s
```

| Phase | Duration | Share |
|---|---|---|
| `SentenceTransformer(...)` construction | 0.080 s | 3% |
| First real `encode()` | ~2.48 s | 97% |
| **Total to serve-ready** | **2.56 s** | |

This is why `warm_model()` runs an actual `encode()` and does not just construct the
model. A warmup that flipped readiness on construction alone would have advertised the
process as ready with 97% of the cold-start cost still ahead of it — the same bug in a
new place. Confirmed independently by `embedding_model_load_seconds 2.5636671259999844`
in `evals/coldstart/warmup-metrics.txt`.

## Measured: a restarting replica is not sent search traffic

Method: two probes at 4 req/s against `/postings/?q=python+engineer&mode=vector`, one
aimed directly at the replica being restarted, one at nginx. `docker compose restart api`
fired 8 s in. 75 s total per probe. Raw samples in
`evals/coldstart/restart-probe-{direct,proxy}.txt` (`epoch label status latency_s`),
restart instant in `restart-timestamp.txt`.

| | Direct to restarting replica | Through nginx |
|---|---|---|
| Samples (post-restart) | 172 | 173 |
| Status codes | 180× 200, 7× **503**, 5× conn-refused | **193× 200** |
| Non-200 window | t+0.6 s .. t+4.4 s | none |
| Slowest request | 0.648 s | 0.237 s |
| Requests over 1 s | **0** | **0** |

Two things to read off this:

- **The gate fires.** The restarting replica returned 503 for the ~3.8 s it was up but
  cold, instead of accepting the request and loading the model inside it.
- **No request paid the load.** The slowest post-restart request anywhere was 0.648 s
  against a 2.56 s model load. Without the gate, the first vector request after the
  restart would have blocked for roughly that 2.56 s, with concurrent requests queued
  behind it.
- **Clients saw nothing.** Every one of the 193 requests through nginx returned 200,
  because `proxy_next_upstream ... http_503` retried the refused request against the warm
  replica.

## Boundaries on these numbers

- Single run, one restart, on one host — not a repeated-trial statistical result.
- 2.56 s warmup is with the model **already in the image**. A cold HuggingFace download
  is unbounded and is not measured here; `HF_HUB_OFFLINE=1` exists so it cannot happen.
- The 503-then-retry behaviour is nginx's passive health checking, not an orchestrator
  reading `/ready`. Kubernetes would remove the pod from the Service endpoint list
  instead; `/ready` returning 503 is what drives both, but only the compose path was
  exercised here.
- `000` in the raw files is curl's connection-refused, i.e. the container was down —
  distinct from the 503s, which came from a running-but-cold process.
