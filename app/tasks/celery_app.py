from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, worker_init, worker_process_shutdown, worker_ready
from app.config import settings

app = Celery(
    "talentscope",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.greenhouse",
        "app.tasks.lever",
        "app.tasks.ashby",
        "app.tasks.adzuna",
        "app.tasks.scheduler",
        "app.tasks.panel",
        "app.tasks.embedding",
        "app.tasks.clustering",
        "app.tasks.monitoring",
    ],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    # A task killed mid-flight (worker OOM-killed, node evicted) is redelivered
    # to another worker rather than silently dropped — the other half of the
    # at-least-once contract that task_acks_late alone doesn't guarantee.
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Job timeout: a hung task (stuck HTTP call, runaway query) is hard-killed
    # after 10 minutes rather than wedging a worker slot forever. The soft
    # limit fires 1 minute earlier so a task can catch SoftTimeLimitExceeded
    # and clean up (e.g. close a partially-open DB transaction) before the kill.
    task_time_limit=600,
    task_soft_time_limit=540,
    # Named queues separate cheap I/O-bound fetches from CPU-bound embedding
    # inference and maintenance work, so a backlog in one class of task can't
    # starve worker slots the others need.
    task_routes={
        "app.tasks.greenhouse.*": {"queue": "ingestion"},
        "app.tasks.lever.*": {"queue": "ingestion"},
        "app.tasks.ashby.*": {"queue": "ingestion"},
        "app.tasks.adzuna.*": {"queue": "ingestion"},
        "app.tasks.scheduler.*": {"queue": "ingestion"},
        "app.tasks.embedding.*": {"queue": "embedding"},
        "app.tasks.clustering.*": {"queue": "maintenance"},
        "app.tasks.panel.*": {"queue": "maintenance"},
        "app.tasks.monitoring.*": {"queue": "maintenance"},
    },
    task_annotations={
        # Coarse backpressure on embedding throughput: bounds how fast the
        # backfill can burn through a large backlog so it can't starve other
        # tasks sharing the embedding queue's worker slots.
        "app.tasks.embedding.embed_posting": {"rate_limit": "300/m"},
    },
)

# Celery Beat schedule
app.conf.beat_schedule = {
    "ingest-greenhouse-batch": {
        "task": "app.tasks.scheduler.dispatch_greenhouse_batch",
        "schedule": crontab(minute=0, hour="*/4"),  # every 4 hours
    },
    "ingest-lever-batch": {
        "task": "app.tasks.scheduler.dispatch_lever_batch",
        "schedule": crontab(minute=30, hour="*/4"),  # every 4 hours offset by 30m
    },
    "ingest-ashby-batch": {
        "task": "app.tasks.scheduler.dispatch_ashby_batch",
        "schedule": crontab(minute=45, hour="*/4"),  # every 4 hours offset by 45m
    },
    "ingest-adzuna-batch": {
        "task": "app.tasks.scheduler.dispatch_adzuna_batch",
        "schedule": crontab(minute=0, hour="*/6"),  # every 6 hours
    },
    "embed-missing-postings": {
        "task": "app.tasks.embedding.embed_missing_postings",
        "schedule": crontab(minute=15, hour="*"),  # hourly, offset to avoid congestion
    },
    "posting-panel-daily-rollup": {
        "task": "app.tasks.panel.run_daily_rollup",
        "schedule": crontab(minute=0, hour=7),     # daily at 07:00 UTC, after all source cadences have run
    },
    "recluster-postings": {
        "task": "app.tasks.clustering.run_clustering_task",
        "schedule": crontab(minute=0, hour=3),     # daily at 03:00 UTC after overnight ingest
    },
    "record-worker-heartbeats": {
        "task": "app.tasks.monitoring.record_worker_heartbeats",
        "schedule": crontab(minute="*"),           # every minute — see HEARTBEAT_TTL in monitoring.py
    },
}


@worker_ready.connect
@beat_init.connect
def _sync_monitored_companies_on_startup(**kwargs):
    """Sync config/target_companies.yml into monitored_companies so
    postings.left_truncated has a real monitoring_started_at to compute
    against, rather than only the YAML file knowing when a company was added."""
    from app.tasks.scheduler import sync_monitored_companies_from_yaml
    sync_monitored_companies_from_yaml()


@worker_init.connect
def _setup_worker_observability(**kwargs):
    """Fires once in the parent worker process before the prefork pool
    forks children — DB query metrics and tracing register event
    listeners/instrumentation that fork() carries into every child; the
    metrics HTTP server binds its port here too, once, not once per child."""
    from app.database import engine
    from app.observability import setup_db_metrics, setup_tracing, start_worker_metrics_server
    setup_db_metrics(engine)
    setup_tracing(engine=engine)
    start_worker_metrics_server()


@worker_process_shutdown.connect
def _cleanup_worker_metrics(pid, **kwargs):
    from app.observability import mark_worker_process_dead
    mark_worker_process_dead(pid)
