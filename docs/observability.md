# TalentScope: Observability

Prometheus + Grafana + OpenTelemetry, validated end-to-end against the real
`docker-compose` stack (not just unit tests — every endpoint and dashboard
below was hit with `curl`/the Prometheus HTTP API while the stack was
running).

## Architecture

Two separate Prometheus scrape targets, not one:

| Target | Port | What it serves | Why separate |
|---|---|---|---|
| `api` | 8000 `/metrics` | HTTP request latency, DB query latency, process CPU/RSS | Single uvicorn process — plain in-memory `prometheus_client` registry |
| `worker` | 9808 `/metrics` | Celery task outcomes/duration, queue depth, active workers, ingestion lag | Celery's prefork pool forks N child processes; a Counter incremented in one child is invisible to any single in-memory registry, so this uses `prometheus_client`'s multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`, aggregated via `MultiProcessCollector` — see `app/observability.py`) |
| `cadvisor` | 8081 | Container-level CPU/memory for every service | Generic — no per-service code needed; the same component Kubernetes' kubelet bundles, so this doubles as a preview of Phase 4's metrics story |

`docs/architecture` reminder: `queue depth` / `active workers` / `ingestion
lag` are deliberately exposed **only** from the worker's endpoint, not the
api's — both processes import `app.observability` (the api needs
`setup_db_metrics`/`setup_http_metrics` from the same module), but exposing
these three from both would just duplicate identical live values under two
different `job` labels.

## Metrics reference

- `http_request_duration_seconds{method,path,status}` / `http_requests_total` — path is the route *template* (`/postings/`), not the raw URL, to keep cardinality bounded
- `db_query_duration_seconds{operation}` — SQLAlchemy `before/after_cursor_execute` engine events; fires for both the api's request-path queries and every Celery task's queries
- `celery_task_total{task_name,state}` / `celery_task_duration_seconds{task_name}` — every task, not just the `task_executions`-tracked allowlist (see `app/tasks/monitoring.py`) — cheap in-memory counter work vs. that table's deliberately-scoped-down DB writes
- `celery_queue_depth{queue}` — live `LLEN` on each of the three named queues (`ingestion`/`embedding`/`maintenance`)
- `celery_active_workers` — count of workers that answered the last `celery.control.inspect().ping()`, refreshed every minute by the `record-worker-heartbeats` beat task
- `ingestion_lag_seconds{source}` — seconds since the last `CollectionRun` with `status="ok"` per source; a source silently going stale (an ATS API change, a boards being deprecated) shows up here before anyone notices a search-result gap
- Default `prometheus_client` process/GC collectors — `process_cpu_seconds_total`, `process_resident_memory_bytes`, etc., automatically on the api's single-process registry

## Grafana

`observability/grafana/` — datasource and dashboard are provisioned
automatically on container start (no manual "add data source" click-through
needed). Dashboard UID `talentscope-overview`, rows: API, Database,
Ingestion/Celery, Infrastructure.

```bash
docker-compose up -d
open http://localhost:3000   # anonymous viewer access enabled for local dev
```

## `/health` vs `/ready`

Split deliberately (`app/main.py`), for Kubernetes probe semantics ahead of
Phase 4:

- **`/health`** (liveness): is the process alive at all? Never touches DB/Redis — a slow dependency shouldn't get a healthy process killed and restarted.
- **`/ready`** (readiness): can this instance serve traffic *right now*? Checks DB (`SELECT 1`) and Redis (`PING`), returns 503 if either fails — should pull the pod out of the Service's endpoint list, not restart it.

## OpenTelemetry tracing

Off by default — only enabled when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, to
avoid exporter overhead/no-op noise in setups without a collector (plain
`pytest`, `uvicorn --reload` without the full compose stack). Instruments
FastAPI + SQLAlchemy + httpx (the request → DB → outbound-ingestion-HTTP
path). Celery isn't auto-instrumented — task lifecycle is already covered by
the Prometheus counters above and the `task_executions`/`failed_tasks`
tables; broker-crossing trace-context propagation is materially more
integration work than this phase is scoped to. To try it locally, run an
OTLP-compatible collector (e.g. Jaeger's all-in-one image exposes an OTLP
HTTP receiver on 4318) and set `OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector>:4318` in `.env`.

## Known environment caveats (found while validating this end-to-end)

- **cAdvisor container labels on Docker Desktop for Mac**: `container_memory_usage_bytes{name=~"..."}` returns zero series on macOS — cAdvisor only sees cgroup-aggregate paths (`id="/docker"` etc.) without per-container `name` labels under Docker Desktop's VM, unlike native Linux. The dashboard panel and Prometheus target are still correct configuration — this will populate properly on a real Linux host or in Kubernetes (Phase 4), which is the config that actually matters for production. Not fixed here since it's an inherent Docker-Desktop-for-Mac limitation, not a misconfiguration.
- **Local Postgres port collision**: if a native/Homebrew Postgres is also running on this machine, it very likely listens on `127.0.0.1:5432`, which can silently intercept connections to `localhost:5432` ahead of docker-compose's `postgres` container (also mapped to host port 5432) depending on OS routing. Symptom: `alembic`/`psql` run from the host against `localhost:5432` hits unexpected/stale state. Connecting from *inside* a container (`docker-compose exec api ...`) is unaffected since it never touches the host's ambiguous loopback binding. Worth knowing before debugging a "why does my migration look wrong" mystery.
