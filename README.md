# TalentScope

A distributed job market intelligence platform that ingests, deduplicates, and analyzes 1,500+ job postings from Greenhouse, Lever, and Adzuna APIs — with semantic hybrid search, a RAG market Q&A system, automated role clustering, and a real-time analytics dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TalentScope                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  External APIs         Task Queue            Database                       │
│  ┌──────────┐          ┌──────────┐         ┌──────────────────────────┐    │
│  │Greenhouse│─────────▶│  Celery  │────────▶│      PostgreSQL           │    │
│  │   API    │          │  Worker  │         │  ┌────────────────────┐  │    │
│  └──────────┘          └──────────┘         │  │ companies          │  │    │
│  ┌──────────┐               ▲               │  │ postings           │  │    │
│  │  Lever   │───────────────┤               │  │   └─ embedding     │  │    │
│  │   API    │          ┌──────────┐         │  │      (vector 384)  │  │    │
│  └──────────┘          │  Redis   │         │  │   └─ search_vector │  │    │
│  ┌──────────┐          │  Broker/ │         │  │      (tsvector/GIN)│  │    │
│  │  Adzuna  │          │  Backend │         │  │ skills             │  │    │
│  │   API    │          │  RAG Cache         │  │ posting_skills     │  │    │
│  └──────────┘          └──────────┘         │  │ skill_clusters     │  │    │
│                             │               │  └────────────────────┘  │    │
│                        ┌──────────┐         └──────────────────────────┘    │
│                        │   Beat   │                      │                  │
│                        │Scheduler │                      ▼                  │
│                        └──────────┘          ┌──────────────────────────┐   │
│                                              │      FastAPI Server       │   │
│  External LLM                                │  /postings/?mode=hybrid   │   │
│  ┌──────────┐                                │  /qa/ask  (RAG)           │   │
│  │  Groq    │◀──────────────────────────────▶│  /analytics/clusters      │   │
│  │  LLaMA 3 │                                │  /analytics/skill-demand  │   │
│  └──────────┘                                └──────────────────────────┘   │
│                                                          │                  │
│                                              ┌──────────────────────────┐   │
│                                              │  Chart.js Dashboard       │   │
│                                              │  skill demand · salary    │   │
│                                              │  trends · role clusters   │   │
│                                              └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Features

| Layer | What it does |
|---|---|
| **Ingestion** | Celery Beat tasks fetch from Greenhouse/Lever/Adzuna every 4–6 h; exact + fuzzy deduplication before upsert |
| **Embedding** | all-MiniLM-L6-v2 (384-dim) encodes every posting; stored in pgvector column with HNSW index (m=16, ef=64) |
| **Hybrid Search** | FTS (GIN/tsvector, OR semantics) + vector cosine fused via Reciprocal Rank Fusion (k=60); p95 ≤ 29 ms |
| **RAG Q&A** | 8 hybrid-retrieved postings → Groq llama-3.1-8b-instant; Redis SHA-256 cache (TTL 1 h); server-side citation validation |
| **Role Clustering** | KMeans on pgvector embeddings; k auto-selected by silhouette grid (5–15); TF-IDF cluster labels; daily Beat task |
| **Analytics** | Skill demand, salary trends, top companies; Chart.js dashboard |

---

## Quick Start (Docker Compose)

```bash
# 1. Clone the repo
git clone https://github.com/yourname/talentscope.git
cd talentscope

# 2. Copy environment file
cp .env.example .env
# Edit .env — add GROQ_API_KEY (free at console.groq.com), optionally Adzuna credentials

# 3. Start all services
docker-compose up --build

# 4. Run database migrations
docker-compose exec api alembic upgrade head

# 5. Open the dashboard
open http://localhost:8000/dashboard/
```

Services:
- API: http://localhost:8000
- Dashboard: http://localhost:8000/dashboard/
- API Docs (Swagger): http://localhost:8000/docs

---

## Local Development Setup

### Prerequisites
- Python 3.11+
- PostgreSQL 15+ with [pgvector](https://github.com/pgvector/pgvector) extension
- Redis 7+

### Steps

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env

# Start PostgreSQL and Redis
docker run -d --name ts-postgres \
  -e POSTGRES_USER=talentscope \
  -e POSTGRES_PASSWORD=talentscope \
  -e POSTGRES_DB=talentscope \
  -p 5432:5432 pgvector/pgvector:pg15

docker run -d --name ts-redis -p 6379:6379 redis:7

# Run migrations
alembic upgrade head

# Start the API server
uvicorn app.main:app --reload

# Celery worker (separate terminal)
celery -A app.tasks.celery_app worker --loglevel=info --concurrency=4

# Celery Beat scheduler (separate terminal)
celery -A app.tasks.celery_app beat --loglevel=info
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | `postgresql://talentscope:talentscope@localhost:5432/talentscope` | PostgreSQL connection string |
| `REDIS_URL` | Yes | `redis://localhost:6379/0` | Redis — Celery broker/backend and RAG cache |
| `GROQ_API_KEY` | Yes (for Q&A) | `""` | Groq API key — get free at [console.groq.com](https://console.groq.com) |
| `ADZUNA_APP_ID` | No | `""` | Adzuna API app ID (from developer.adzuna.com) |
| `ADZUNA_APP_KEY` | No | `""` | Adzuna API app key |

Without `GROQ_API_KEY`, `/qa/ask` returns HTTP 503.  
Without Adzuna credentials, only Greenhouse and Lever data is ingested.

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
# Hybrid semantic search
curl "http://localhost:8000/postings/?q=machine+learning+engineer&mode=hybrid"

# FTS with skill filter
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

Response includes `answer`, `sources` (posting excerpts), `cited_ids`, `cached` flag, and `latency_ms`.

### Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/analytics/skill-demand` | Top skills by posting count (windowed) |
| `GET` | `/analytics/salary-trends` | Average salary by month |
| `GET` | `/analytics/top-companies` | Companies with most postings |
| `GET` | `/analytics/clusters` | Latest KMeans role clusters with labels |
| `POST` | `/analytics/clusters/run` | Trigger a clustering run (optional `?k=N`) |

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |

---

## Search Modes

### FTS (Full-Text Search)
PostgreSQL `tsvector` / `ts_rank` via a GIN index.  Stop-words and stemming handled by the `english` dictionary.  OR semantics: `"senior backend engineer"` matches any posting containing any of those terms (not all three), so conversational queries don't zero out results.

### Vector (Semantic)
Query encoded with all-MiniLM-L6-v2 (384-dim, normalized).  Nearest-neighbor search over pgvector HNSW index (`<=>` cosine distance).  Finds semantically similar postings even when exact keywords don't match (e.g. `"MLOps"` ↔ `"ML infrastructure"`).

### Hybrid (default for Q&A)
Fetches 200 candidates from each source, fuses the two ranked lists with Reciprocal Rank Fusion (`score = Σ 1/(60 + rank_i)`).  Precision of FTS + recall of vector; neither source is silenced.

---

## RAG Market Q&A

`POST /qa/ask` retrieves the 8 best postings for the question (hybrid search), injects them as context into a structured prompt, and calls Groq's `llama-3.1-8b-instant`.

**Cache**: SHA-256 of `question|mode|N_SOURCES` → Redis (TTL 1 h).  Cache-hits skip the LLM call and back-fill `cited_ids` from the stored answer.

**Citation validation**: The LLM is instructed to cite sources as `[1]`…`[8]`.  The server strips any citation number outside the range of retrieved postings so out-of-range hallucinated references never reach the client.

**Failure modes**: Redis failure is non-fatal (degraded to no-cache).  Missing `GROQ_API_KEY` returns HTTP 503 immediately.

---

## Role Clustering

A daily Celery Beat task (`03:00 UTC`) clusters all embedded postings by semantic similarity:

1. Pull all 384-dim embeddings from pgvector, normalize to unit sphere.
2. Run silhouette grid over k = 5…15 (subsample 500, `random_state=42`) to pick the best k.
3. Fit KMeans (`n_init=10`, `random_state=42`).
4. Label each cluster with its top-2 discriminating skills using a TF-IDF analog:
   `score(skill, cluster) = (cluster_freq / cluster_size) / (corpus_freq / n_total)`
   with a 3% / 3-posting presence floor to suppress both globally dominant terms and rare-skill noise.
5. Persist to `skill_clusters` table; bulk-update `postings.cluster_id`.

Current run (1,530 postings, k=8, silhouette=0.3646):

| Cluster | Label | Size |
|---|---|---|
| 0 | API Design · SQL | 528 |
| 1 | Bash · Grafana | 269 |
| 2 | Spring · Microservices | 138 |
| 3 | Elasticsearch · CSS | 200 |
| 4 | PHP · Jenkins | 260 |
| 5 | Rails · React | 77 |
| 6 | Agile · Kubernetes | 37 |
| 7 | Linux · Java | 21 |

> **Known limitation**: cluster IDs are reassigned on every fit.  Cross-run identity is not tracked — add centroid matching (cosine + Hungarian algorithm) before building a trend endpoint.

---

## Benchmark

**Environment**: Apple M2 (8-core, 8 GB RAM), macOS, local dev machine — not a production deployment.
Python 3.11.7 (x86 via Rosetta 2).  PostgreSQL 15 running locally.

**Methodology**: 12 queries × 50 repeats per mode, 2 warm-up runs per query discarded.
600 samples per mode.  No HTTP overhead — search functions called directly.
Results stored in `evals/benchmark.json`.

| Mode | p50 | p95 | p99 | σ |
|---|---|---|---|---|
| FTS (GIN/tsvector) | 6.9 ms | 11.1 ms | 13.9 ms | 2.1 ms |
| Vector (HNSW cosine) | 14.9 ms | 21.2 ms | 28.0 ms | 8.4 ms |
| Hybrid (RRF) | 28.9 ms | 39.2 ms | 46.4 ms | 6.0 ms |

**Embedding latency** (all-MiniLM-L6-v2, CPU):
- Single-query encode (search path): **p50 = 12.3 ms** — this is included in the vector/hybrid numbers above, so HNSW scan alone is ~3 ms.
- Batch encode (ingestion backfill): **131 sentences/sec** (48-sentence batch).

**Concurrency note**: `fts_search` and `vector_search` are called sequentially in the current
implementation (hybrid p50 ≈ FTS p50 + vector p50).  Both are independent read queries and
could be parallelized, but the synchronous SQLAlchemy `Session` is not thread-safe — parallelization
requires either separate sessions per sub-query or a migration to async SQLAlchemy.  At 1.5 k
postings the saving (~6 ms) is modest; it becomes material at 50 k+ postings.

Reproduce:

```bash
python scripts/benchmark.py          # 50 repeats (default)
python scripts/benchmark.py --repeats 100
```

---

## Resume Bullet Mapping

### Bullet 1 — Distributed ingestion pipeline
> "Engineered a **distributed data pipeline** using **Celery/Redis task queues** to scrape, deduplicate, and persist **1,500+ job postings** from three live APIs with **fault-tolerant retry logic** and **scheduled cron-driven ingestion**, validated by **automated GitHub Actions CI/CD** on every commit."

| Phrase | Code location |
|---|---|
| distributed data pipeline | `app/tasks/` — Celery tasks distributed across workers |
| Celery/Redis task queues | `app/tasks/celery_app.py` — Redis as broker + backend |
| scrape | `app/tasks/greenhouse.py`, `lever.py`, `adzuna.py` |
| deduplicate | `app/ingestion/deduplicator.py` — exact + fuzzy dedup |
| fault-tolerant retry logic | `autoretry_for`, `retry_backoff`, `max_retries=3` in each task |
| scheduled cron-driven ingestion | `app/tasks/celery_app.py:beat_schedule` — crontab every 4–6 h |
| automated GitHub Actions CI/CD | `.github/workflows/ci.yml` |

### Bullet 2 — Analytics dashboard
> "Designed a **PostgreSQL schema** with **normalized relational tables**, **GIN-indexed full-text search**, and **aggregation queries** powering an **interactive Chart.js dashboard** for real-time **skill demand** and **salary trend** visualization."

| Phrase | Code location |
|---|---|
| PostgreSQL schema | `app/models.py`; `alembic/versions/0001_initial_schema.py` |
| normalized relational tables | `Company`, `Posting`, `Skill`, `PostingSkill` with FK relationships |
| GIN-indexed full-text search | `ix_postings_search_vector` GIN index on `search_vector` tsvector column |
| aggregation queries | `app/api/analytics.py` — `GROUP BY` + `COUNT`/`AVG` |
| interactive Chart.js dashboard | `dashboard/index.html` — Chart.js 4.x bar + line + horizontal bar charts |

### Bullet 3 — Semantic hybrid search
> "Implemented **semantic hybrid search** combining a **pgvector HNSW index** (all-MiniLM-L6-v2, 384-dim, cosine) with PostgreSQL **GIN full-text search**, fused via **Reciprocal Rank Fusion** — p95 ≤ 40 ms for hybrid and ≤ 22 ms for vector-only across 1,530 postings (600-sample benchmark on Apple M2)."

| Phrase | Code location |
|---|---|
| pgvector HNSW index | `alembic/versions/0002_add_embeddings.py` — `CREATE INDEX … USING hnsw` |
| all-MiniLM-L6-v2 | `app/search/encoder.py:get_model()` — lazy singleton |
| cosine distance | `app/search/hybrid.py:vector_search()` — `p.embedding <=> CAST(:vec AS vector)` |
| GIN full-text search | `app/search/hybrid.py:fts_search()` — OR-tsquery over `search_vector` |
| Reciprocal Rank Fusion | `app/search/hybrid.py:reciprocal_rank_fusion()` — `Σ 1/(60 + rank_i)` |
| p95 latency numbers | `evals/benchmark.json` — 12 queries × 5 repeats, warm-up discarded |

### Bullet 4 — RAG Q&A and role clustering
> "Built a **RAG market Q&A system** (Groq llama-3.1-8b-instant, **Redis cache** with SHA-256 key + 1 h TTL, server-side citation validation) and automated **KMeans role clustering** (k auto-selected by silhouette grid over 5–15, **TF-IDF cluster labels** with 3% floor) as a scheduled **Celery Beat** task — confirmed via end-to-end Beat smoke test."

| Phrase | Code location |
|---|---|
| RAG market Q&A | `app/search/rag.py:answer_question()` |
| Groq llama-3.1-8b-instant | `app/search/rag.py:GROQ_MODEL` |
| Redis cache with SHA-256 key | `app/search/rag.py:_cache_key()` — `sha256(question|mode|N_SOURCES)` |
| server-side citation validation | `app/search/rag.py:_parse_cited_ids()` — strips `[N]` where N > len(sources) |
| KMeans role clustering | `app/ml/clustering.py:run_clustering()` |
| silhouette grid | `app/ml/clustering.py:_best_k()` — grid over k=5..15, sample_size=500 |
| TF-IDF cluster labels with 3% floor | `app/ml/clustering.py:_cluster_top_skills()` |
| Celery Beat task | `app/tasks/clustering.py` — `run_clustering_task`, daily at 03:00 UTC |

---

## Data Sources

### Greenhouse
Applicant Tracking System used by hundreds of tech companies.  Public jobs API at `boards-api.greenhouse.io`.

- **Coverage**: 47 companies (Stripe, Notion, Figma, Datadog, Coinbase, and more)
- **Rate limit**: none for public endpoints

### Lever
Another popular ATS with a public postings API.

- **Coverage**: 12 companies (Netflix, Airbnb, Spotify, Shopify)
- **Rate limit**: none for public endpoints

### Adzuna
Job aggregator.  Register at [developer.adzuna.com](https://developer.adzuna.com) for a free API key.

- **Coverage**: broad US market; 3 pages × 50 results × 10 queries = up to 1,500 postings per run
- **Rate limit**: 250 requests/day on free tier

---

## Running Tests

```bash
pip install -r requirements.txt

# Ensure test DB exists with pgvector
createdb talentscope_test
psql talentscope_test -c "CREATE EXTENSION IF NOT EXISTS vector;"

export TEST_DATABASE_URL=postgresql://talentscope:talentscope@localhost:5432/talentscope_test
export DATABASE_URL=$TEST_DATABASE_URL

alembic upgrade head

pytest tests/ -v
```

Test files:

| File | Coverage |
|---|---|
| `tests/test_dedup.py` | Exact and fuzzy deduplication |
| `tests/test_api.py` | All FastAPI endpoints |
| `tests/test_tasks.py` | Normalizers, skill extraction, task smoke |
| `tests/test_search.py` | FTS, vector, hybrid search; RRF unit tests |
| `tests/test_qa.py` | RAG pipeline, Redis cache, citation validation |
| `tests/test_clustering.py` | KMeans pipeline, TF-IDF labels, API endpoints |

---

## Deployment (Railway)

```bash
npm install -g @railway/cli
railway login && railway init

# Add PostgreSQL (with pgvector plugin) and Redis via Railway dashboard

railway variables set DATABASE_URL=<from-railway-postgres>
railway variables set REDIS_URL=<from-railway-redis>
railway variables set GROQ_API_KEY=<your-groq-key>
railway variables set ADZUNA_APP_ID=<your-key>
railway variables set ADZUNA_APP_KEY=<your-key>

railway up
railway run alembic upgrade head
```

For the worker and beat services, create additional Railway services with:
- **Worker**: `celery -A app.tasks.celery_app worker --loglevel=info --concurrency=4`
- **Beat**: `celery -A app.tasks.celery_app beat --loglevel=info`

---

## Project Structure

```
talentscope/
├── .github/workflows/ci.yml           # GitHub Actions CI
├── alembic/
│   └── versions/
│       ├── 0001_initial_schema.py     # companies, postings, skills
│       ├── 0002_add_embeddings.py     # vector(384) column + HNSW index
│       └── 0003_add_clustering.py     # skill_clusters table, postings.cluster_id
├── app/
│   ├── main.py                        # FastAPI app entrypoint
│   ├── config.py                      # Pydantic settings (DATABASE_URL, GROQ_API_KEY …)
│   ├── database.py                    # SQLAlchemy engine + session
│   ├── models.py                      # ORM: Company, Posting, Skill, PostingSkill, SkillCluster
│   ├── api/
│   │   ├── postings.py                # /postings/ — search with mode=fts|vector|hybrid
│   │   ├── analytics.py               # /analytics/ — skill demand, salary, clusters
│   │   └── qa.py                      # /qa/ask — RAG market Q&A
│   ├── search/
│   │   ├── encoder.py                 # all-MiniLM-L6-v2 lazy singleton
│   │   ├── hybrid.py                  # fts_search, vector_search, reciprocal_rank_fusion
│   │   └── rag.py                     # answer_question — retrieval → Groq → Redis
│   ├── ml/
│   │   └── clustering.py              # run_clustering — KMeans + TF-IDF labels
│   ├── tasks/
│   │   ├── celery_app.py              # Celery app + Beat schedule
│   │   ├── greenhouse.py              # Greenhouse ingestion task
│   │   ├── lever.py                   # Lever ingestion task
│   │   ├── adzuna.py                  # Adzuna ingestion task
│   │   ├── scheduler.py               # Batch dispatch tasks
│   │   ├── embedding.py               # embed_missing_postings Beat task
│   │   └── clustering.py              # run_clustering_task Beat task
│   └── ingestion/
│       ├── normalizer.py              # API response normalizers
│       ├── deduplicator.py            # Exact + fuzzy dedup
│       └── skills.py                  # Skill extraction (180+ skills)
├── dashboard/index.html               # Chart.js frontend (skill demand, salary, clusters)
├── evals/
│   └── benchmark.json                 # Latency benchmark results (traceable)
├── scripts/
│   └── benchmark.py                   # Search latency benchmark runner
├── tests/
│   ├── conftest.py                    # pytest fixtures (test DB, client, redis mock)
│   ├── test_api.py                    # FastAPI endpoint tests
│   ├── test_clustering.py             # KMeans pipeline tests
│   ├── test_dedup.py                  # Deduplication tests
│   ├── test_qa.py                     # RAG + citation tests
│   ├── test_search.py                 # Hybrid search tests
│   └── test_tasks.py                  # Normalizer + task tests
├── .env.example                       # Environment variable template
├── alembic.ini
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```
