# TalentScope — Distributed Search & Data Platform

A distributed systems project built to prove out real engineering claims,
not just describe them: a Celery/Redis ingestion pipeline with dead-letter
handling and idempotency proven under simulated duplicate delivery, a
Postgres layer with EXPLAIN ANALYZE-backed index evidence, a Prometheus/
Grafana/OpenTelemetry observability stack validated end-to-end against a
running system, a Kubernetes deployment on a real 3-node cluster with
measured worker-scaling throughput, Terraform applied against LocalStack
with independently AWS-CLI-verified resources, and a k6 load test that
answers one concrete question — how much traffic can this actually
sustain, and what breaks first.

The domain underneath all of that happens to be job-market intelligence:
it ingests, deduplicates, and analyzes thousands of live postings from
Greenhouse, Lever, Ashby, and Adzuna, with semantic hybrid search, a RAG
market Q&A system, automated role clustering, and a daily survival-analysis
panel tracking how long postings stay open. That's the substrate the
distributed-systems work runs against — not the point of the project.

---

## Engineering highlights

Numbers below are all real, measured, and reproducible — not estimates.
Full narrative and methodology for each behind the linked doc.

| Area | Finding |
|---|---|
| **Ingestion resilience** | Idempotency proven directly (`tests/test_idempotency.py` simulates duplicate task delivery); dead-letter store + explicit task-state tracking; a real backpressure bug fixed (duplicate embedding dispatch under overlapping beat firings) |
| **Postgres** ([docs/db-engineering.md](docs/db-engineering.md)) | HNSW vs. brute-force vector search: **8.5x**. GIN vs. seq scan: **5.9x**. A methodology bug in the `ef_search` recall sweep caught and fixed *before* trusting the numbers |
| **Observability** ([docs/observability.md](docs/observability.md)) | Prometheus + Grafana + OpenTelemetry, validated with `curl` against every real endpoint — not just "the container started." One pre-existing deployment bug found and fixed along the way (`.env`'s `localhost` defaults silently broke `docker-compose up`) |
| **Kubernetes** ([docs/kubernetes.md](docs/kubernetes.md)) | Deployed to a real 3-node `kind` cluster. Docker image **9.01GB → 1.94GB** (found `sentence-transformers` silently pulling the CUDA build of torch on a deployment with no GPU). Worker throughput 2→4 replicas: **~2.5x**, real `sentence-transformers` inference, not a stand-in |
| **Terraform** ([docs/terraform.md](docs/terraform.md)) | 34 resources applied against LocalStack, independently re-verified via the AWS CLI afterward. Confirmed directly (not assumed) which services are LocalStack Pro-only, and architected around it rather than writing untestable Terraform |
| **Load testing** ([evals/load-test.md](evals/load-test.md)) | A real concurrency bug found and fixed at just 2 virtual users, before any capacity data was collected. Root-caused the actual bottleneck through controlled comparisons (worker paused vs. running; 1 vs. 4 uvicorn processes) down to CPU thread oversubscription — not the database, which was never the limiting factor |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                  TalentScope                                  │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  External APIs          Task Queue (Celery)         Database                  │
│  ┌───────────┐          ┌─────────────────┐        ┌───────────────────────┐  │
│  │ Greenhouse│─────────▶│ ingestion queue │───────▶│      PostgreSQL        │  │
│  │  Lever    │          │ embedding queue │        │  postings + embedding │  │
│  │  Ashby    │          │ maintenance     │        │  (pgvector HNSW)       │  │
│  │  Adzuna   │          │  queue          │        │  search_vector (GIN,   │  │
│  └───────────┘          └─────────────────┘        │   GENERATED column)    │  │
│                               ▲    │                │  posting_snapshots,    │  │
│                          ┌────┴────┴────┐           │  task_executions,      │  │
│                          │ Redis        │           │  failed_tasks (DLQ)    │  │
│                          │ broker/cache │           └───────────────────────┘  │
│                          │ heartbeats   │                       │              │
│                          └──────────────┘                       ▼              │
│                                                    ┌───────────────────────┐   │
│  External LLM                                      │    FastAPI Server     │   │
│  ┌──────────┐                                      │ /postings mode=hybrid │   │
│  │  Groq    │◀────────────────────────────────────▶│ /qa/ask   (RAG)       │   │
│  │  LLaMA 3 │                                      │ /health  /ready       │   │
│  └──────────┘                                      │ /metrics (Prometheus) │   │
│                                                     └───────────────────────┘   │
│                                                                 │               │
│  Observability                                     ┌───────────────────────┐  │
│  Prometheus · Grafana · OpenTelemetry ◀─────────────│  Chart.js Dashboard    │  │
│                                                      └───────────────────────┘  │
├──────────────────────────────────────────────────────────────────────────────┤
│  Deployment targets: docker-compose (local) · Kubernetes (kind) · Terraform   │
│  + LocalStack (AWS-shaped IaC)                                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Features

| Layer | What it does |
|---|---|
| **Ingestion** | Celery Beat fetches from Greenhouse/Lever/Ashby/Adzuna on a schedule; exact + fuzzy dedup; idempotent under redelivery; Redis-backed batch cursor survives worker restarts |
| **Resilience** | Named queues, job timeouts, dead-letter store, explicit task-state tracking, worker heartbeats, graceful shutdown, backpressure guards — see [Ingestion resilience](#ingestion-resilience) |
| **Posting panel** | Daily snapshots track posting presence/absence and description drift over time — the event history a survival analysis (Kaplan-Meier/Cox) reads directly |
| **Embedding** | all-MiniLM-L6-v2 (384-dim) encodes every posting; pgvector column with HNSW index (m=16, ef_construction=64) |
| **Hybrid Search** | FTS (GIN/tsvector, OR semantics) + vector cosine fused via Reciprocal Rank Fusion (k=60) |
| **RAG Q&A** | 8 hybrid-retrieved postings → Groq llama-3.1-8b-instant; Redis SHA-256 cache; server-side citation validation |
| **Role Clustering** | KMeans on pgvector embeddings; k auto-selected by silhouette grid (5–15); TF-IDF cluster labels; daily Beat task |
| **Analytics** | Skill demand, salary trends, top companies; Chart.js dashboard |
| **Observability** | Prometheus (`/metrics` on api + worker), Grafana (auto-provisioned dashboard), OpenTelemetry tracing |

---

## Quick Start (Docker Compose)

```bash
git clone https://github.com/meetsutariya4448/talentscope.git
cd talentscope

cp .env.example .env
# Edit .env — add GROQ_API_KEY (free at console.groq.com), optionally Adzuna credentials

docker-compose up --build
docker-compose exec api alembic upgrade head

open http://localhost:8000/dashboard/
```

Services:
- API: http://localhost:8000 (docs at `/docs`)
- Dashboard: http://localhost:8000/dashboard/
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (anonymous viewer access)
- cAdvisor (container CPU/mem): http://localhost:8081

---

## Kubernetes

Deployed and debugged against a real 3-node `kind` cluster — full narrative,
every real failure hit and its fix, and the worker-scaling throughput data:
[docs/kubernetes.md](docs/kubernetes.md).

```bash
kind create cluster --config k8s/kind-config.yaml
docker build -t talentscope:latest .
kind load docker-image talentscope:latest --name talentscope
kubectl apply -f k8s/99-metrics-server.yaml
bash k8s/apply.sh
```

Postgres (StatefulSet), Redis, api/worker (Deployments with HPAs), beat
(pinned singleton — no leader election, a second replica double-fires
every scheduled task), a migration Job, ConfigMap/Secret split. Demo the
worker-scaling throughput test (`bash k8s/scale-demo.sh`, real
`sentence-transformers` inference, not a stand-in) — 2→4 replicas showed a
genuine ~2.5x improvement; 8 replicas hit a real hardware ceiling on this
laptop rather than scaling further, documented honestly in
[evals/k8s-scaling.md](evals/k8s-scaling.md) instead of glossed over.

---

## Terraform + LocalStack

VPC/subnets/security groups, IAM, S3, Secrets Manager, CloudWatch — applied
against LocalStack and independently re-verified via the AWS CLI
afterward, not just trusted from `terraform apply`'s exit code. Full
narrative, including which AWS services are LocalStack Pro-only (confirmed
directly, not assumed) and how that shaped the architecture:
[docs/terraform.md](docs/terraform.md).

```bash
docker run -d --name talentscope-localstack -p 4566:4566 localstack/localstack:3.0
cd terraform
terraform init
cp terraform.tfvars.example terraform.tfvars
terraform apply
```

---

## Load Testing

k6 load test answering one question: how much traffic can this sustain on
real hardware before latency or errors become unacceptable, and what's the
limiting resource first? Full narrative, every table, and the concurrency
bug found along the way: [evals/load-test.md](evals/load-test.md).

```bash
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d postgres redis api worker beat
python k6/seed_for_load_test.py   # inside the api container
bash k6/run-stage.sh 20 30s
```

---

## Ingestion resilience

At-least-once Celery delivery (`task_acks_late` + `task_reject_on_worker_lost`)
with the resilience layer to match:

- **Idempotency**, proven directly — `tests/test_idempotency.py` simulates
  duplicate task delivery and asserts no duplicate rows, no duplicate
  skill links, correct backpressure behavior.
- **Dead-letter store** (`failed_tasks` table) — a task that exhausts
  retries lands here for triage instead of vanishing once Celery's Redis
  result backend TTLs out.
- **Explicit task-state tracking** (`task_executions` table) for
  coarse-grained tasks (fetch/dispatch/clustering/rollup) — scoped away
  from high-frequency per-posting tasks to avoid write amplification.
- **Worker heartbeats** — Redis TTL keys refreshed every minute via
  `celery.control.inspect().ping()`.
- **Backpressure** — a real bug found and fixed: `embed_missing_postings`
  was re-dispatching duplicate work for postings still embedding from the
  previous hourly firing. Fixed with a Redis claim-based in-flight guard.
- **Content-drift re-embedding** — a deeper bug found while fixing
  `search_vector` staleness: `postings.title`/`description` were never
  refreshed on re-fetch at all, only hash/version bookkeeping. Now content
  refreshes and re-embeds on real drift.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | `postgresql://talentscope:talentscope@localhost:5432/talentscope` | PostgreSQL connection string |
| `REDIS_URL` | Yes | `redis://localhost:6379/0` | Redis — Celery broker/backend and RAG cache |
| `GROQ_API_KEY` | Yes (for Q&A) | `""` | Groq API key — get free at [console.groq.com](https://console.groq.com) |
| `ADZUNA_APP_ID` | No | `""` | Adzuna API app ID (from developer.adzuna.com) |
| `ADZUNA_APP_KEY` | No | `""` | Adzuna API app key |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_RECYCLE_SECONDS` | No | `10` / `20` / `1800` | Connection pool sizing (`app/config.py`) |
| `VECTOR_EF_SEARCH` | No | unset | HNSW runtime search-width override — see [docs/db-engineering.md](docs/db-engineering.md) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | unset | Enables OpenTelemetry tracing when set; a no-op otherwise |
| `PROMETHEUS_MULTIPROC_DIR` | Worker only | unset | Aggregates Prometheus metrics across Celery's forked pool — see `app/observability.py` |

Without `GROQ_API_KEY`, `/qa/ask` returns HTTP 503.
Without Adzuna credentials, only Greenhouse/Lever/Ashby data is ingested.

---

## API Endpoints

### Postings

| Method | Path | Description |
|---|---|---|
| `GET` | `/postings/` | Search job postings (FTS, vector, or hybrid) |
| `GET` | `/postings/stats` | Total count and breakdown by source |

**Query parameters for `GET /postings/`:**

| Param | Default | Description |
|---|---|---|
| `q` | `""` | Search query |
| `mode` | `fts` | `fts` · `vector` · `hybrid` |
| `skill` | `""` | Filter by skill name (e.g. `Python`) |
| `location` | `""` | Partial match on location string |
| `page` | `1` | Page number |
| `page_size` | `20` | Results per page (max 100) |

```bash
curl "http://localhost:8000/postings/?q=machine+learning+engineer&mode=hybrid"
curl "http://localhost:8000/postings/?q=backend&skill=Go&mode=fts"
```

### Market Q&A (RAG)

| Method | Path | Description |
|---|---|---|
| `POST` | `/qa/ask` | Answer a market question using retrieved postings + LLM |

```bash
curl -X POST http://localhost:8000/qa/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What skills are most in demand for backend roles?", "mode": "hybrid"}'
```

### Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/analytics/skill-demand` | Top skills by posting count (windowed) |
| `GET` | `/analytics/salary-trends` | Average salary by month |
| `GET` | `/analytics/top-companies` | Companies with most postings |
| `GET` | `/analytics/clusters` | Latest KMeans role clusters with labels |
| `POST` | `/analytics/clusters/run` | Trigger a clustering run (optional `?k=N`) |

### Operations

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness — never touches DB/Redis |
| `GET` | `/ready` | Readiness — checks DB + Redis, 503 if either fails |
| `GET` | `/metrics` | Prometheus metrics (api process) |
| `GET` | `:9808/metrics` | Prometheus metrics (worker process, multiprocess-aggregated) |

---

## Search Modes

### FTS (Full-Text Search)
PostgreSQL `tsvector` / `ts_rank` via a GIN index, backed by a
`GENERATED ALWAYS AS ... STORED` column so it's always current — Postgres
recomputes it on every write, closing a staleness bug where an edited
posting kept matching stale text forever. OR semantics: `"senior backend
engineer"` matches any posting containing any of those terms, not all
three, so conversational queries don't zero out.

### Vector (Semantic)
Query encoded with all-MiniLM-L6-v2 (384-dim, normalized). Nearest-neighbor
search over pgvector's HNSW index (`<=>` cosine distance) — **8.5x faster**
than brute-force at 20k rows, measured (see
[docs/db-engineering.md](docs/db-engineering.md)). Finds semantically
similar postings even when exact keywords don't match (`"MLOps"` ↔ `"ML
infrastructure"`).

### Hybrid (default for Q&A)
Fetches 200 candidates from each source, fuses the two ranked lists with
Reciprocal Rank Fusion (`score = Σ 1/(60 + rank_i)`). Precision of FTS +
recall of vector; neither source is silenced.

---

## Benchmark

Direct search-path latency at real production corpus scale — complements
[docs/db-engineering.md](docs/db-engineering.md)'s EXPLAIN ANALYZE evidence,
which uses a larger synthetic corpus specifically to stress-test individual
indexes in isolation; this is the end-to-end number for the corpus this
app actually runs at.

**Environment**: Apple M2 (8-core, 8 GB RAM), macOS, local dev machine — not
a production deployment. Python 3.11.7 (x86 via Rosetta 2). PostgreSQL 15
running locally.

**Methodology**: 12 queries × 50 repeats per mode, 2 warm-up runs per query
discarded. 600 samples per mode. No HTTP overhead — search functions called
directly. Results stored in `evals/benchmark.json`.

| Mode | p50 | p95 | p99 | σ |
|---|---|---|---|---|
| FTS (GIN/tsvector) | 6.9 ms | 11.1 ms | 13.9 ms | 2.1 ms |
| Vector (HNSW cosine) | 14.9 ms | 21.2 ms | 28.0 ms | 8.4 ms |
| Hybrid (RRF) | 28.9 ms | 39.2 ms | 46.4 ms | 6.0 ms |

**Embedding latency** (all-MiniLM-L6-v2, CPU):
- Single-query encode (search path): **p50 = 12.3 ms** — included in the
  vector/hybrid numbers above, so HNSW scan alone is ~3 ms.
- Batch encode (ingestion backfill): **131 sentences/sec** (48-sentence batch).

**Concurrency note**: `fts_search` and `vector_search` are called
sequentially (hybrid p50 ≈ FTS p50 + vector p50) — both are independent
read queries and could be parallelized, but the synchronous SQLAlchemy
`Session` isn't thread-safe, so that needs either separate sessions per
sub-query or a migration to async SQLAlchemy. At this corpus size the
saving (~6 ms) is modest; [evals/load-test.md](evals/load-test.md) found a
much larger effect from CPU thread oversubscription under concurrent
load, which is the more material problem at this scale today.

Reproduce:

```bash
python scripts/benchmark.py          # 50 repeats (default)
python scripts/benchmark.py --repeats 100
```

---

## RAG Market Q&A

`POST /qa/ask` retrieves the 8 best postings for the question (hybrid
search), injects them as context into a structured prompt, and calls
Groq's `llama-3.1-8b-instant`.

**Cache**: SHA-256 of `question|mode|N_SOURCES` → Redis (TTL 1 h).
**Citation validation**: the server strips any citation number outside the
range of retrieved postings so out-of-range hallucinated references never
reach the client.
**Failure modes**: Redis failure is non-fatal (degraded to no-cache).
Missing `GROQ_API_KEY` returns HTTP 503 immediately.

---

## Role Clustering

A daily Celery Beat task (`03:00 UTC`) clusters all embedded postings by
semantic similarity: pulls embeddings (`ORDER BY id` — required for
reproducible KMeans centroid init), silhouette grid over k=5..15, fits
KMeans, labels each cluster by TF-IDF-scored discriminating skills, and
persists via a single set-based bulk `UPDATE ... FROM unnest()` (replaced
an earlier per-row update loop — see
[docs/db-engineering.md](docs/db-engineering.md)).

> **Known limitation**: cluster IDs are reassigned on every fit. Cross-run
> identity is not tracked — add centroid matching (cosine + Hungarian
> algorithm) before building a trend endpoint.

---

## Running Tests

```bash
pip install -r requirements.txt

createdb talentscope_test
psql talentscope_test -c "CREATE EXTENSION IF NOT EXISTS vector;"

export TEST_DATABASE_URL=postgresql://talentscope:talentscope@localhost:5432/talentscope_test
export DATABASE_URL=$TEST_DATABASE_URL

alembic upgrade head
pytest tests/ -v
```

**93 tests**, run against a real Postgres+pgvector instance (no mocked DB):

| File | Tests | Coverage |
|---|---|---|
| `tests/test_search.py` | 13 | FTS, vector, hybrid search; RRF unit tests |
| `tests/test_qa.py` | 12 | RAG pipeline, Redis cache, citation validation |
| `tests/test_panel.py` | 12 | Posting-panel survival tracking, resurrection, drift |
| `tests/test_scheduler.py` | 11 | Batch dispatch, Redis cursor restart-survival |
| `tests/test_clustering.py` | 10 | KMeans pipeline, TF-IDF labels, bulk update, API endpoints |
| `tests/test_tasks.py` | 8 | Normalizers, skill extraction, task smoke |
| `tests/test_monitoring.py` | 8 | Task-state tracking, dead-letter writes, heartbeats |
| `tests/test_api.py` | 7 | All FastAPI endpoints |
| `tests/test_idempotency.py` | 7 | Duplicate delivery, backpressure, dead-letter |
| `tests/test_dedup.py` | 4 | Exact and fuzzy deduplication |
| `tests/test_encoder.py` | 1 | Model-loading race under concurrent access |

---

## Deployment

**Kubernetes** ([above](#kubernetes)) is the primary deployment target —
StatefulSet/Deployment/HPA/probes, deployed against a real cluster, not
just written. **Terraform + LocalStack** ([above](#terraform--localstack))
provisions the AWS-shaped infrastructure a real deployment would sit on.

For a lighter-weight PaaS option:

```bash
npm install -g @railway/cli
railway login && railway init
# Add PostgreSQL (pgvector plugin) and Redis via the Railway dashboard
railway variables set DATABASE_URL=<from-railway-postgres>
railway variables set REDIS_URL=<from-railway-redis>
railway variables set GROQ_API_KEY=<your-groq-key>
railway up
railway run alembic upgrade head
```

Worker and beat need their own Railway services:
`celery -A app.tasks.celery_app worker --loglevel=info --concurrency=4` /
`celery -A app.tasks.celery_app beat --loglevel=info`.

---

## Further documentation

| Doc | Covers |
|---|---|
| [docs/db-engineering.md](docs/db-engineering.md) | EXPLAIN ANALYZE evidence for every index, connection pooling, bulk writes |
| [docs/observability.md](docs/observability.md) | Prometheus/Grafana/OpenTelemetry architecture, `/health` vs `/ready` |
| [docs/kubernetes.md](docs/kubernetes.md) | K8s manifests, every real failure hit and its fix |
| [docs/terraform.md](docs/terraform.md) | AWS architecture, LocalStack Pro service boundary |
| [evals/load-test.md](evals/load-test.md) | k6 methodology, the full bottleneck investigation, the answer |
| [evals/k8s-scaling.md](evals/k8s-scaling.md) | Worker replica scaling throughput data and its real hardware ceiling |
| [evals/results.md](evals/results.md) | Vector-search tail-latency investigation (Apple M2, local) |

---

## Resume Bullet Mapping

### Bullet 1 — Distributed ingestion pipeline
> "Engineered a **distributed data pipeline** using **Celery/Redis task queues** with **dead-letter handling**, **explicit task-state tracking**, and **idempotency proven under simulated duplicate delivery**, ingesting from four live APIs with **fault-tolerant retry logic**, **worker heartbeats**, and **scheduled cron-driven ingestion**."

| Phrase | Code location |
|---|---|
| distributed data pipeline | `app/tasks/` — named queues (ingestion/embedding/maintenance) via `task_routes` |
| dead-letter handling | `app/tasks/monitoring.py` — `failed_tasks` table, populated from `task_failure` signal |
| explicit task-state tracking | `app/tasks/monitoring.py` — `task_executions` table |
| idempotency proven under duplicate delivery | `tests/test_idempotency.py` |
| fault-tolerant retry logic | `autoretry_for`, `retry_backoff`, `task_reject_on_worker_lost` in `app/tasks/celery_app.py` |
| worker heartbeats | `app/tasks/monitoring.py:record_worker_heartbeats` |
| scheduled cron-driven ingestion | `app/tasks/celery_app.py:beat_schedule` |

### Bullet 2 — Postgres performance engineering
> "Produced **EXPLAIN ANALYZE-backed evidence** for every index in a Postgres/pgvector schema — **GIN full-text (5.9x)**, **composite B-tree (3.7x)**, **HNSW vector search (8.5x)** vs. sequential scan — caught and fixed a **methodology bug** in an `ef_search` recall sweep before trusting the results, and replaced an **n-round-trip update loop** with a single set-based `UPDATE ... FROM unnest()`."

| Phrase | Code location |
|---|---|
| EXPLAIN ANALYZE evidence | `scripts/db_engineering_report.py`, `docs/db-engineering.md` |
| methodology bug caught and fixed | `docs/db-engineering.md` — `ef_search` sweep's "ground truth" was itself approximate |
| bulk update | `app/ml/clustering.py:run_clustering()` |
| connection pooling | `app/database.py`, `app/config.py` |

### Bullet 3 — Observability
> "Instrumented a distributed system with **Prometheus** (multiprocess-aggregated across a Celery worker pool), **Grafana** (auto-provisioned dashboards), and **OpenTelemetry** tracing — validated end-to-end against a live running stack, not unit-tested in isolation — and found a **pre-existing deployment bug** along the way."

| Phrase | Code location |
|---|---|
| Prometheus, multiprocess-aggregated | `app/observability.py` |
| Grafana, auto-provisioned | `observability/grafana/` |
| OpenTelemetry tracing | `app/observability.py:setup_tracing` |
| pre-existing deployment bug found | `docs/observability.md` — `.env`'s `localhost` defaults broke `docker-compose up` |

### Bullet 4 — Kubernetes
> "Deployed a multi-service application to a real **3-node Kubernetes cluster** with **HorizontalPodAutoscalers**, **StatefulSets**, and **health/readiness probes** — reduced the Docker image **9.01GB → 1.94GB**, measured **~2.5x worker-scaling throughput** on real ML inference, and root-caused three separate real cluster failures to their actual fixes."

| Phrase | Code location |
|---|---|
| 3-node kind cluster | `k8s/kind-config.yaml` |
| HPA / StatefulSet / probes | `k8s/20-api.yaml`, `k8s/21-worker.yaml`, `k8s/10-postgres.yaml` |
| image size reduction | `.dockerignore`, CPU-only torch pin in `requirements.txt` |
| worker-scaling throughput | `k8s/scale-demo.sh`, `evals/k8s-scaling.md` |
| real cluster failures + fixes | `docs/kubernetes.md` |

### Bullet 5 — Terraform + AWS
> "Provisioned AWS infrastructure as code with **Terraform** — VPC/subnets/security groups, **least-privilege IAM**, S3, Secrets Manager, CloudWatch — applied against **LocalStack** and **independently re-verified via the AWS CLI**, with the compute architecture adapted around confirmed LocalStack Pro service boundaries rather than left untested."

| Phrase | Code location |
|---|---|
| VPC / security groups | `terraform/network.tf` |
| least-privilege IAM | `terraform/iam.tf` — no wildcard resource ARNs |
| Secrets Manager | `terraform/secrets.tf` |
| AWS CLI verification | `docs/terraform.md` |

### Bullet 6 — Load testing
> "Load-tested a distributed system with **k6**, found and fixed a **real concurrency bug** at 2 virtual users before collecting any capacity data, and root-caused the actual bottleneck through **controlled comparisons** — isolating CPU thread oversubscription as the limiting resource, not the database."

| Phrase | Code location |
|---|---|
| concurrency bug found and fixed | `app/search/encoder.py:get_model()` — double-checked locking |
| regression test | `tests/test_encoder.py` |
| controlled comparisons | `evals/load-test.md` — worker paused vs. running; 1 vs. 4 uvicorn processes |

### Bullet 7 — Semantic hybrid search
> "Implemented **semantic hybrid search** combining a **pgvector HNSW index** (all-MiniLM-L6-v2, 384-dim, cosine) with PostgreSQL **GIN full-text search**, fused via **Reciprocal Rank Fusion**."

| Phrase | Code location |
|---|---|
| pgvector HNSW index | `alembic/versions/0002_add_pgvector_embedding.py` |
| all-MiniLM-L6-v2 | `app/search/encoder.py:get_model()` |
| cosine distance | `app/search/hybrid.py:vector_search()` |
| Reciprocal Rank Fusion | `app/search/hybrid.py:reciprocal_rank_fusion()` |

### Bullet 8 — RAG Q&A and role clustering
> "Built a **RAG market Q&A system** (Groq llama-3.1-8b-instant, Redis-cached, server-side citation validation) and automated **KMeans role clustering** (silhouette-selected k, TF-IDF cluster labels) as a scheduled Celery Beat task."

| Phrase | Code location |
|---|---|
| RAG market Q&A | `app/search/rag.py:answer_question()` |
| citation validation | `app/search/rag.py:_parse_cited_ids()` |
| KMeans role clustering | `app/ml/clustering.py:run_clustering()` |
| Celery Beat task | `app/tasks/clustering.py` |

---

## Data Sources

### Greenhouse
Public jobs API at `boards-api.greenhouse.io` — no rate limit on public endpoints.

### Lever
Public postings API — no rate limit on public endpoints.

### Ashby
Public job-board API at `api.ashbyhq.com` — no rate limit on public endpoints.

### Adzuna
Job aggregator. Register at [developer.adzuna.com](https://developer.adzuna.com)
for a free API key — 250 requests/day on the free tier.

---

## Project Structure

```
talentscope/
├── .github/workflows/ci.yml           # GitHub Actions CI
├── alembic/versions/                  # 5 migrations: schema → embeddings → clustering → posting panel → task observability
├── app/
│   ├── main.py                        # FastAPI entrypoint — /health, /ready, /metrics
│   ├── config.py                      # Pydantic settings — DB pool, HNSW tuning, etc.
│   ├── database.py                    # SQLAlchemy engine + session (pooled)
│   ├── models.py                      # ORM: postings, panel tables, task_executions, failed_tasks
│   ├── observability.py               # Prometheus metrics + OpenTelemetry tracing
│   ├── api/                           # postings.py, analytics.py, qa.py
│   ├── search/                        # encoder.py, hybrid.py, rag.py
│   ├── ml/clustering.py               # KMeans + TF-IDF labels + bulk update
│   ├── tasks/                         # celery_app.py, monitoring.py, redis_utils.py,
│   │                                  #   greenhouse/lever/ashby/adzuna.py, scheduler.py,
│   │                                  #   embedding.py, clustering.py, panel.py
│   └── ingestion/                     # ingest.py, panel.py, hashing.py, normalizer.py,
│                                       #   company_registry.py, deduplicator.py, skills.py
├── dashboard/index.html               # Chart.js frontend
├── config/target_companies.yml        # Monitored-company registry source of truth
├── k8s/                               # Kubernetes manifests + apply.sh + scale-demo.sh
├── terraform/                         # AWS IaC — network/iam/storage/database/secrets/monitoring
├── observability/                     # Prometheus config + Grafana provisioning/dashboards
├── k6/                                # load_test.js, seed script, run-stage.sh
├── docs/                              # db-engineering.md, observability.md, kubernetes.md, terraform.md
├── evals/                             # benchmark.json, load-test.md, k8s-scaling.md, results.md
├── scripts/                           # benchmark.py, db_engineering_report.py, log_application.py
├── tests/                             # 93 tests, 11 files
├── docker-compose.yml                 # postgres, redis, api, worker, beat, prometheus, grafana, cadvisor
├── docker-compose.loadtest*.yml       # k6 test overlays
├── Dockerfile / .dockerignore
└── requirements.txt
```
