# TalentScope

A distributed job market intelligence platform that ingests, deduplicates, and analyzes 50,000+ job postings from Greenhouse, Lever, and Adzuna APIs — with a real-time analytics dashboard powered by FastAPI and Chart.js.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          TalentScope                                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   External APIs          Task Queue          Database               │
│   ┌──────────┐           ┌────────┐         ┌───────────────────┐   │
│   │Greenhouse│──────────▶│ Celery │────────▶│   PostgreSQL      │   │
│   │   API    │           │ Worker │         │  ┌─────────────┐  │   │
│   └──────────┘           └────────┘         │  │  companies  │  │   │
│   ┌──────────┐               ▲              │  │  postings   │  │   │
│   │  Lever   │───────────────┤              │  │  skills     │  │   │
│   │   API    │           ┌────────┐         │  │posting_skills│ │   │
│   └──────────┘           │ Redis  │         │  └─────────────┘  │   │
│   ┌──────────┐           │Broker/ │         └───────────────────┘   │
│   │  Adzuna  │           │Backend │                  │              │
│   │   API    │           └────────┘                  │              │
│   └──────────┘               │                       ▼              │
│                          ┌────────┐         ┌───────────────────┐   │
│                          │  Beat  │         │  FastAPI Server   │   │
│                          │Scheduler         │  /postings/       │   │
│                          └────────┘         │  /analytics/      │   │
│                                             │  /dashboard/      │   │
│                                             └───────────────────┘   │
│                                                      │              │
│                                             ┌───────────────────┐   │
│                                             │  Chart.js Frontend│   │
│                                             │  dashboard/index  │   │
│                                             └───────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Quick Start (Docker Compose)

```bash
# 1. Clone the repo
git clone https://github.com/yourname/talentscope.git
cd talentscope

# 2. Copy environment file
cp .env.example .env
# Edit .env with your Adzuna credentials (optional)

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
- API Docs: http://localhost:8000/docs

---

## Local Development Setup

### Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Redis 7+

### Steps

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env

# Start PostgreSQL and Redis (using Docker)
docker run -d --name ts-postgres \
  -e POSTGRES_USER=talentscope \
  -e POSTGRES_PASSWORD=talentscope \
  -e POSTGRES_DB=talentscope \
  -p 5432:5432 postgres:15

docker run -d --name ts-redis -p 6379:6379 redis:7

# Run migrations
alembic upgrade head

# Start the API server
uvicorn app.main:app --reload

# In another terminal: start Celery worker
celery -A app.tasks.celery_app worker --loglevel=info --concurrency=4

# In another terminal: start Celery Beat scheduler
celery -A app.tasks.celery_app beat --loglevel=info
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | `postgresql://talentscope:talentscope@localhost:5432/talentscope` | PostgreSQL connection string |
| `REDIS_URL` | Yes | `redis://localhost:6379/0` | Redis connection string (broker + backend) |
| `ADZUNA_APP_ID` | No | `""` | Adzuna API app ID (from developer.adzuna.com) |
| `ADZUNA_APP_KEY` | No | `""` | Adzuna API app key |

Without Adzuna credentials, only Greenhouse and Lever data will be ingested.

---

## API Endpoints

### Postings

| Method | Path | Description |
|---|---|---|
| `GET` | `/postings/` | Search job postings with filters |
| `GET` | `/postings/stats` | Total count and breakdown by source |

**Query parameters for `GET /postings/`:**
- `q` — Full-text search query (uses PostgreSQL tsvector)
- `skill` — Filter by skill name (e.g. `Python`, `React`)
- `location` — Partial match on location string
- `page` — Page number (default: 1)
- `page_size` — Results per page (default: 20, max: 100)

**Example:**
```bash
curl "http://localhost:8000/postings/?q=machine+learning&skill=Python&location=Remote&page=1"
```

### Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/analytics/skill-demand` | Top skills by posting count |
| `GET` | `/analytics/salary-trends` | Average salary by month |
| `GET` | `/analytics/top-companies` | Companies with most postings |

**Query parameters for `/analytics/skill-demand`:**
- `window` — Time window: `7d`, `30d`, `90d`, `all` (default: `30d`)
- `limit` — Number of skills to return (default: 20, max: 50)

**Query parameters for `/analytics/salary-trends`:**
- `role` — Keyword filter on job title
- `location` — Location filter

### Health

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns `{"status": "ok"}` |

---

## Resume Bullet Mapping

These two resume bullets map directly to the following code locations:

### Bullet 1
> "Engineered a **distributed data pipeline** using **Celery/Redis task queues** to **scrape, deduplicate, and persist 50,000+ job postings** with **fault-tolerant retry logic**, **scheduled cron-driven ingestion**, and **automated GitHub Actions CI/CD** on each commit."

| Phrase | Code Location |
|---|---|
| "distributed data pipeline" | `app/tasks/` — Celery tasks distributed across workers |
| "Celery/Redis task queues" | `app/tasks/celery_app.py` — Celery app with Redis as broker/backend |
| "scrape" | `app/tasks/greenhouse.py:fetch_greenhouse()`, `app/tasks/lever.py:fetch_lever()`, `app/tasks/adzuna.py:fetch_adzuna()` |
| "deduplicate" | `app/ingestion/deduplicator.py` — `is_exact_duplicate()` and `find_fuzzy_duplicate()` |
| "persist 50,000+ job postings" | `app/tasks/greenhouse.py:_upsert_posting()` — inserts into PostgreSQL via SQLAlchemy |
| "fault-tolerant retry logic" | `app/tasks/greenhouse.py` L62-66 — `autoretry_for`, `retry_backoff`, `retry_backoff_max`, `max_retries=3` |
| "scheduled cron-driven ingestion" | `app/tasks/celery_app.py:beat_schedule` — crontab schedules every 4-6 hours |
| "automated GitHub Actions CI/CD" | `.github/workflows/ci.yml` — runs on every push to main |

### Bullet 2
> "Designed a **PostgreSQL schema** with **normalized relational tables**, **indexed full-text search columns**, and **optimized aggregation queries** powering an **interactive Chart.js analytics dashboard** for real-time **skill demand** and **salary trend visualization**."

| Phrase | Code Location |
|---|---|
| "PostgreSQL schema" | `app/models.py` — SQLAlchemy models; `alembic/versions/0001_initial_schema.py` — migration |
| "normalized relational tables" | `app/models.py` — `Company`, `Posting`, `Skill`, `PostingSkill` with FK relationships |
| "indexed full-text search columns" | `app/models.py:Posting.search_vector` (TSVECTOR) + `ix_postings_search_vector` GIN index; also `ix_postings_company_title_location`, `ix_postings_posted_at`, `ix_postings_source` |
| "optimized aggregation queries" | `app/api/analytics.py:skill_demand()`, `salary_trends()`, `top_companies()` — GROUP BY + COUNT/AVG |
| "interactive Chart.js analytics dashboard" | `dashboard/index.html` — Chart.js 4.x bar + line charts |
| "skill demand" | `app/api/analytics.py:skill_demand()` + `dashboard/index.html:loadSkillDemand()` |
| "salary trend visualization" | `app/api/analytics.py:salary_trends()` + `dashboard/index.html:loadSalaryTrends()` |

---

## Data Sources

### Greenhouse
Greenhouse is an Applicant Tracking System (ATS) used by hundreds of tech companies. Their public jobs API (`boards-api.greenhouse.io`) exposes job listings for any company with a public board.

- **Endpoint**: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- **Coverage**: 47 companies including Stripe, Notion, Figma, Datadog, Coinbase, and more
- **Rate limit**: None for public endpoints

### Lever
Lever is another popular ATS with a public postings API.

- **Endpoint**: `https://api.lever.co/v0/postings/{slug}?mode=json`
- **Coverage**: 12 companies including Netflix, Airbnb, Spotify, Shopify
- **Rate limit**: None for public endpoints

### Adzuna
Adzuna is a job aggregator covering millions of postings across multiple job boards.

- **Endpoint**: `https://api.adzuna.com/v1/api/jobs/us/search/{page}`
- **Registration**: Free API key at [developer.adzuna.com](https://developer.adzuna.com)
- **Coverage**: Broad US market across 10 role categories
- **Ingestion**: 3 pages × 50 results × 10 queries = up to 1,500 postings per run

---

## Running Tests

```bash
# Install dependencies
pip install -r requirements.txt

# Ensure test database exists
createdb talentscope_test  # or use Docker

# Set test database URL
export TEST_DATABASE_URL=postgresql://talentscope:talentscope@localhost:5432/talentscope_test
export DATABASE_URL=$TEST_DATABASE_URL

# Run migrations against test DB
alembic upgrade head

# Run all tests
pytest tests/ -v

# Run specific test files
pytest tests/test_dedup.py -v
pytest tests/test_api.py -v
pytest tests/test_tasks.py -v
```

Test coverage:
- `tests/test_dedup.py` — Exact and fuzzy deduplication logic
- `tests/test_api.py` — All FastAPI endpoints (health, search, analytics)
- `tests/test_tasks.py` — Normalizers, skill extraction, task smoke test

---

## Deployment (Railway)

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and initialize
railway login
railway init

# Add PostgreSQL and Redis plugins via Railway dashboard

# Set environment variables
railway variables set DATABASE_URL=<from-railway-postgres>
railway variables set REDIS_URL=<from-railway-redis>
railway variables set ADZUNA_APP_ID=<your-key>
railway variables set ADZUNA_APP_KEY=<your-key>

# Deploy
railway up

# Run migrations
railway run alembic upgrade head
```

For the Celery worker and beat services, create additional Railway services pointing to the same repo with the commands:
- Worker: `celery -A app.tasks.celery_app worker --loglevel=info --concurrency=4`
- Beat: `celery -A app.tasks.celery_app beat --loglevel=info`

---

## Project Structure

```
talentscope/
├── .github/workflows/ci.yml       # GitHub Actions CI pipeline
├── alembic/
│   ├── versions/
│   │   └── 0001_initial_schema.py # Initial DB migration
│   ├── env.py                     # Alembic env config
│   └── script.py.mako             # Migration template
├── app/
│   ├── main.py                    # FastAPI app entrypoint
│   ├── config.py                  # Pydantic settings
│   ├── database.py                # SQLAlchemy engine + session
│   ├── models.py                  # ORM models (Company, Posting, Skill)
│   ├── api/
│   │   ├── postings.py            # /postings/ endpoints
│   │   └── analytics.py          # /analytics/ endpoints
│   ├── tasks/
│   │   ├── celery_app.py          # Celery app + beat schedule
│   │   ├── greenhouse.py          # Greenhouse ingestion task
│   │   ├── lever.py               # Lever ingestion task
│   │   ├── adzuna.py              # Adzuna ingestion task
│   │   └── scheduler.py           # Batch dispatch tasks
│   └── ingestion/
│       ├── normalizer.py          # API response normalizers
│       ├── deduplicator.py        # Exact + fuzzy dedup
│       └── skills.py              # Skill extraction (180+ skills)
├── dashboard/index.html            # Chart.js analytics frontend
├── tests/
│   ├── conftest.py                # pytest fixtures
│   ├── test_dedup.py              # Deduplication tests
│   ├── test_api.py                # API endpoint tests
│   └── test_tasks.py              # Normalizer + task tests
├── .env.example                   # Environment variable template
├── alembic.ini                    # Alembic configuration
├── docker-compose.yml             # Full stack Docker setup
├── Dockerfile                     # API/worker container
└── requirements.txt               # Python dependencies
```
