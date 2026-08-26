"""
Observability: Prometheus metrics (/metrics) + OpenTelemetry tracing.

Metrics
-------
- http_request_duration_seconds{method,path,status} / http_requests_total  — API latency
- db_query_duration_seconds{operation}                                     — DB latency (SQLAlchemy event hook)
- celery_task_total{task_name,state} / celery_task_duration_seconds        — jobs/sec, failures, retries (via rate())
- celery_queue_depth{queue}                                                — worker queue depth (live Redis LLEN)
- celery_active_workers                                                    — from monitoring.get_worker_heartbeats()
- ingestion_lag_seconds{source}                                            — time since last successful CollectionRun
- process CPU/RSS — prometheus_client's default ProcessCollector, included automatically

Scope note: celery_task_total/duration are updated for *every* task (see
app.tasks.monitoring's signal handlers) — cheap in-memory counter work,
unlike the task_executions DB writes which are deliberately scoped down to
a coarse-grained allowlist to avoid write amplification on high-frequency
per-posting tasks. This module has no import-time dependency on
app.tasks.monitoring (it's called *from* there) to avoid a cycle.

OpenTelemetry
-------------
Enabled only when OTEL_EXPORTER_OTLP_ENDPOINT is set — avoids exporter
no-op overhead and startup noise in setups without a collector running
(e.g. plain `pytest`, `uvicorn --reload` without docker-compose's otel
service). Instruments FastAPI + SQLAlchemy + httpx: the request-path trace
that matters most for tying API latency to DB latency to outbound ingestion
HTTP calls. Celery isn't auto-instrumented — task lifecycle is already
covered by the Prometheus counters above and the task_executions/
failed_tasks tables; broker-crossing trace-context propagation is
meaningfully more integration work than this phase is scoped to.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import event

logger = logging.getLogger(__name__)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "path", "status"],
)
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "HTTP requests", ["method", "path", "status"],
)
DB_QUERY_DURATION = Histogram(
    "db_query_duration_seconds", "DB query latency by statement verb", ["operation"],
)
CELERY_TASK_TOTAL = Counter(
    "celery_task_total", "Celery task outcomes", ["task_name", "state"],
)
CELERY_TASK_DURATION = Histogram(
    "celery_task_duration_seconds", "Celery task duration", ["task_name"],
)

# multiprocess_mode="mostrecent": these three are single-logical-value
# gauges computed fresh on every scrape (in whichever process handles the
# HTTP request), not per-child state. Without this, prometheus_client's
# default multiprocess mode ("all") reports one series per pid that has
# ever imported this module — including phantom 0.0 entries from forked
# task-worker children that never call .set() on these at all — so a naive
# `celery_active_workers` query in Grafana would show a real series plus
# flat-zero noise from every other pid. "mostrecent" collapses that to the
# single latest value regardless of which pid wrote it.
CELERY_QUEUE_DEPTH = Gauge(
    "celery_queue_depth", "Pending messages per Celery queue", ["queue"],
    multiprocess_mode="mostrecent",
)
CELERY_ACTIVE_WORKERS = Gauge(
    "celery_active_workers", "Workers that responded to the last heartbeat ping",
    multiprocess_mode="mostrecent",
)
INGESTION_LAG_SECONDS = Gauge(
    "ingestion_lag_seconds", "Seconds since the last successful ingestion run", ["source"],
    multiprocess_mode="mostrecent",
)

_QUEUES = ("ingestion", "embedding", "maintenance")


# ---------------------------------------------------------------------------
# HTTP (FastAPI)
# ---------------------------------------------------------------------------

def setup_http_metrics(app: FastAPI) -> None:
    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        # Route path template (e.g. "/postings/{id}"), not the raw URL, to
        # keep label cardinality bounded regardless of query params/path IDs.
        route = request.scope.get("route")
        path = route.path if route is not None else request.url.path
        labels = {"method": request.method, "path": path, "status": str(response.status_code)}
        HTTP_REQUEST_DURATION.labels(**labels).observe(elapsed)
        HTTP_REQUESTS_TOTAL.labels(**labels).inc()
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        # Deliberately does NOT call _refresh_pull_metrics(): queue depth /
        # active workers / ingestion lag are Celery-subsystem metrics, and
        # both this module-level import (main.py imports setup_db_metrics
        # from here too) and the worker's own /metrics endpoint would
        # otherwise report identical live values under two different `job`
        # labels — same data, needlessly duplicated. Their home is the
        # worker's endpoint (see start_worker_metrics_server below).
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# DB (SQLAlchemy engine events — fires for API requests and Celery tasks alike)
# ---------------------------------------------------------------------------

def setup_db_metrics(engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("query_start_time", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        stack = conn.info.get("query_start_time")
        if not stack:
            return
        elapsed = time.perf_counter() - stack.pop()
        verb = statement.strip().split(None, 1)[0].upper() if statement.strip() else "OTHER"
        if verb not in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            verb = "OTHER"
        DB_QUERY_DURATION.labels(operation=verb).observe(elapsed)


# ---------------------------------------------------------------------------
# Celery task outcomes (called from app.tasks.monitoring's signal handlers)
# ---------------------------------------------------------------------------

def record_task_outcome(task_name: str, state: str, duration_seconds: float | None = None) -> None:
    CELERY_TASK_TOTAL.labels(task_name=task_name, state=state).inc()
    if duration_seconds is not None:
        CELERY_TASK_DURATION.labels(task_name=task_name).observe(duration_seconds)


# ---------------------------------------------------------------------------
# Scrape-time refresh: cheaper to compute fresh than maintain incrementally
# ---------------------------------------------------------------------------

def _refresh_pull_metrics() -> None:
    _refresh_queue_depth()
    _refresh_active_workers()
    _refresh_ingestion_lag()


def _refresh_queue_depth() -> None:
    from app.tasks.redis_utils import get_redis
    rc = get_redis()
    if rc is None:
        return
    try:
        for queue in _QUEUES:
            CELERY_QUEUE_DEPTH.labels(queue=queue).set(rc.llen(queue))
    except Exception:
        logger.warning("Failed to refresh celery_queue_depth", exc_info=True)


def _refresh_active_workers() -> None:
    from app.tasks.monitoring import get_worker_heartbeats
    try:
        CELERY_ACTIVE_WORKERS.set(len(get_worker_heartbeats()))
    except Exception:
        logger.warning("Failed to refresh celery_active_workers", exc_info=True)


def _refresh_ingestion_lag() -> None:
    from sqlalchemy import func, select
    from app.database import SessionLocal
    from app.models import CollectionRun

    db = SessionLocal()
    try:
        rows = db.execute(
            select(CollectionRun.source, func.max(CollectionRun.run_at))
            .where(CollectionRun.status == "ok")
            .group_by(CollectionRun.source)
        ).all()
        now = datetime.now(timezone.utc)
        for source, last_run_at in rows:
            if last_run_at is None:
                continue
            if last_run_at.tzinfo is None:
                last_run_at = last_run_at.replace(tzinfo=timezone.utc)
            INGESTION_LAG_SECONDS.labels(source=source).set((now - last_run_at).total_seconds())
    except Exception:
        logger.warning("Failed to refresh ingestion_lag_seconds", exc_info=True)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Celery worker metrics server
# ---------------------------------------------------------------------------
#
# Celery's default prefork pool forks N child processes to run tasks;
# prometheus_client's in-memory registry is per-process, so a Counter
# incremented in one child is invisible to any single HTTP endpoint unless
# every process shares PROMETHEUS_MULTIPROC_DIR (set in the container
# environment, not here — it must exist before prometheus_client is first
# imported in each process). With it set, Counter/Histogram/Gauge writes go
# to per-process mmap'd files instead of memory, and this server aggregates
# across all of them at scrape time via MultiProcessCollector. The API
# process never sets this env var — it's single-process (plain uvicorn), so
# its own /metrics uses the simple in-memory registry via generate_latest().

def start_worker_metrics_server(port: int = 9808) -> None:
    """Call once from Celery's worker_init (the parent process, before the
    pool forks children) — starts a small HTTP server on its own thread."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        logger.warning("PROMETHEUS_MULTIPROC_DIR not set — worker metrics server not started")
        return

    import threading
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    from prometheus_client import CollectorRegistry, make_wsgi_app, multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    base_wsgi_app = make_wsgi_app(registry)

    def wsgi_app(environ, start_response):
        _refresh_pull_metrics()
        return base_wsgi_app(environ, start_response)

    class _QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):  # noqa: D401 — silence per-request access logs
            pass

    server = make_server("0.0.0.0", port, wsgi_app, handler_class=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="prometheus-metrics")
    thread.start()
    logger.info("Worker Prometheus metrics server listening on :%d/metrics", port)


def mark_worker_process_dead(pid: int) -> None:
    """Call from worker_process_shutdown — without this, a dead child's
    last-written metric file lingers and gets double-counted (or counted as
    a phantom process) once a new child reuses file-based aggregation."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    from prometheus_client import multiprocess
    multiprocess.mark_process_dead(pid)


# ---------------------------------------------------------------------------
# OpenTelemetry tracing
# ---------------------------------------------------------------------------

def setup_tracing(*, app: FastAPI | None = None, engine=None) -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled")
        return

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service_name = os.environ.get("OTEL_SERVICE_NAME", "talentscope")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)

    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        SQLAlchemyInstrumentor().instrument(engine=engine)

    logger.info("OpenTelemetry tracing enabled (service=%s) -> %s", service_name, endpoint)
