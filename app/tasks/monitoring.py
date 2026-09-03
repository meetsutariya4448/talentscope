import json
import logging
import socket
import time
from datetime import datetime, timezone

from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    worker_shutdown,
)

from app.tasks.celery_app import app as celery_app
from app.tasks.redis_utils import get_redis as _get_redis
from app.database import SessionLocal
from app.models import FailedTask, TaskExecution
from app.observability import record_task_outcome

logger = logging.getLogger(__name__)

# In-process, per-task-id start-time tracking so postrun/failure can report
# a duration to Prometheus. Not persisted anywhere — a task's own worker
# process is always the one that fires both its prerun and postrun/failure
# signals, so this never needs to survive a process boundary.
_task_start_times: dict[str, float] = {}

HEARTBEAT_NS = "talentscope:worker:heartbeat"
# Beat fires the ping every minute; a key aging out means the worker missed
# at least one ping outright (crashed, deadlocked, or was killed), not just
# ordinary jitter.
HEARTBEAT_TTL = 90

# Explicit task-state tracking is opt-in per task name, not global: fetch/
# dispatch/maintenance tasks run at most a few dozen times a day, so a
# task_executions row per run is cheap and useful. embed_posting runs once
# per posting (thousands of times) — tracking every one would be write
# amplification for state nobody queries at that granularity; its outcome is
# already visible in postings.embedding being set or not.
_TRACK_STATE_FOR = {
    "app.tasks.greenhouse.fetch_greenhouse",
    "app.tasks.lever.fetch_lever",
    "app.tasks.ashby.fetch_ashby",
    "app.tasks.adzuna.fetch_adzuna",
    "app.tasks.scheduler.dispatch_greenhouse_batch",
    "app.tasks.scheduler.dispatch_lever_batch",
    "app.tasks.scheduler.dispatch_ashby_batch",
    "app.tasks.scheduler.dispatch_adzuna_batch",
    "app.tasks.embedding.embed_missing_postings",
    "app.tasks.clustering.run_clustering_task",
    "app.tasks.panel.run_daily_rollup",
}


def _safe_json(value) -> str | None:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return None


def _hostname() -> str:
    return socket.gethostname()


def _update_task_execution(task_id, *, state, finished, error=None):
    if not task_id:
        return
    db = SessionLocal()
    try:
        row = db.query(TaskExecution).filter_by(task_id=task_id).first()
        if row is None:
            return
        row.state = state
        if state == "RETRY":
            row.retries = (row.retries or 0) + 1
        if error:
            row.error = error
        if finished:
            row.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to update task_execution %s", task_id)
    finally:
        db.close()


@task_prerun.connect
def _on_task_prerun(sender=None, task_id=None, args=None, kwargs=None, **_):
    if task_id is not None:
        _task_start_times[task_id] = time.perf_counter()

    if sender is None or sender.name not in _TRACK_STATE_FOR:
        return
    db = SessionLocal()
    try:
        db.add(TaskExecution(
            task_id=task_id,
            task_name=sender.name,
            state="STARTED",
            worker_hostname=_hostname(),
            args=_safe_json(args),
            started_at=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to record task_prerun for %s", task_id)
    finally:
        db.close()


def _pop_duration(task_id: str | None) -> float | None:
    if task_id is None:
        return None
    start = _task_start_times.pop(task_id, None)
    return (time.perf_counter() - start) if start is not None else None


@task_postrun.connect
def _on_task_postrun(sender=None, task_id=None, state=None, **_):
    task_name = sender.name if sender is not None else "unknown"
    # task_retry already records this intermediate state. Celery reuses the
    # task id for the next attempt, so retain its original start time and wait
    # for a terminal postrun/failure signal before finishing the execution.
    if state == "RETRY":
        return
    # task_failure fires separately (and first) for exceptions and records
    # its own outcome — only report success here to avoid double-counting.
    if state != "FAILURE":
        record_task_outcome(task_name, state or "SUCCESS", _pop_duration(task_id))
    else:
        _task_start_times.pop(task_id, None)

    if sender is None or sender.name not in _TRACK_STATE_FOR or state == "FAILURE":
        return
    _update_task_execution(task_id, state=state or "SUCCESS", finished=True)


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None, traceback=None, args=None, kwargs=None, **_):
    """
    Fires once Celery gives up on a task — max_retries exhausted, or a
    non-retryable failure. This is the dead-letter write: a poison message
    stops retrying (autoretry_for + max_retries bounds it) and lands here for
    triage instead of silently vanishing once the Redis result backend's TTL
    expires.
    """
    task_name = sender.name if sender is not None else "unknown"
    retries = getattr(getattr(sender, "request", None), "retries", 0) if sender is not None else 0
    record_task_outcome(task_name, "FAILURE", _pop_duration(task_id))

    db = SessionLocal()
    try:
        db.add(FailedTask(
            task_id=task_id,
            task_name=task_name,
            args=_safe_json(args),
            kwargs=_safe_json(kwargs),
            exception=str(exception),
            traceback=str(traceback) if traceback else None,
            retries=retries,
            failed_at=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to record dead-letter entry for %s", task_id)
    finally:
        db.close()

    if task_name in _TRACK_STATE_FOR:
        _update_task_execution(task_id, state="FAILURE", finished=True, error=str(exception))


@task_retry.connect
def _on_task_retry(sender=None, request=None, reason=None, **_):
    task_id = getattr(request, "id", None) if request is not None else None
    task_name = sender.name if sender is not None else "unknown"
    # A retry keeps the same task_id in Celery, so don't pop the start-time
    # entry — the eventual postrun/failure duration should span from the
    # *original* prerun, not restart the clock at each retry attempt.
    record_task_outcome(task_name, "RETRY")

    if sender is None or sender.name not in _TRACK_STATE_FOR:
        return
    _update_task_execution(task_id, state="RETRY", finished=False, error=str(reason))


@worker_shutdown.connect
def _on_worker_shutdown(**_):
    logger.warning("Worker shutting down gracefully: %s", _hostname())


# ---------------------------------------------------------------------------
# Worker heartbeats
# ---------------------------------------------------------------------------

@celery_app.task(name="app.tasks.monitoring.record_worker_heartbeats")
def record_worker_heartbeats():
    """
    Beat-scheduled every minute. Pings every worker connected to the broker
    via Celery's control bus and writes a TTL'd Redis key per responding
    worker. A worker's key aging out (missed HEARTBEAT_TTL seconds) means it
    stopped responding since the last successful ping — crashed, deadlocked,
    or killed — independent of whether its broker connection looks alive.
    """
    rc = _get_redis()
    if rc is None:
        logger.warning("record_worker_heartbeats: Redis unavailable, skipping")
        return {"workers": 0}

    pings = celery_app.control.inspect(timeout=2).ping() or {}
    now = datetime.now(timezone.utc).isoformat()
    for hostname in pings:
        rc.set(f"{HEARTBEAT_NS}:{hostname}", now, ex=HEARTBEAT_TTL)
    return {"workers": len(pings)}


def get_worker_heartbeats() -> dict[str, str]:
    """Read all live worker heartbeats. A worker absent here has either never
    started or missed its last HEARTBEAT_TTL-second window."""
    rc = _get_redis()
    if rc is None:
        return {}
    result: dict[str, str] = {}
    for key in rc.scan_iter(match=f"{HEARTBEAT_NS}:*", count=100):
        key_str = key.decode() if isinstance(key, bytes) else key
        hostname = key_str[len(f"{HEARTBEAT_NS}:"):]
        val = rc.get(key_str)
        if val is not None:
            result[hostname] = val.decode() if isinstance(val, bytes) else val
    return result
