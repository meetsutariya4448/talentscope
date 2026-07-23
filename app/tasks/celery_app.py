from celery import Celery
from celery.schedules import crontab
from app.config import settings

app = Celery(
    "talentscope",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.tasks.greenhouse",
        "app.tasks.lever",
        "app.tasks.adzuna",
        "app.tasks.scheduler",
        "app.tasks.embedding",
        "app.tasks.clustering",
    ],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
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
    "ingest-adzuna-batch": {
        "task": "app.tasks.scheduler.dispatch_adzuna_batch",
        "schedule": crontab(minute=0, hour="*/6"),  # every 6 hours
    },
    "embed-missing-postings": {
        "task": "app.tasks.embedding.embed_missing_postings",
        "schedule": crontab(minute=15, hour="*"),  # hourly, offset to avoid congestion
    },
    "recluster-postings": {
        "task": "app.tasks.clustering.run_clustering_task",
        "schedule": crontab(minute=0, hour=3),     # daily at 03:00 UTC after overnight ingest
    },
}
