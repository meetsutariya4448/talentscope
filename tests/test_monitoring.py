"""
Tests for explicit Celery task-state tracking (TaskExecution) and worker
heartbeats (app/tasks/monitoring.py). These are the pieces that make task
lifecycle observable beyond Celery's own ephemeral, TTL'd Redis result
backend — a durable record of what ran, retried, and finished.
"""
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import sessionmaker

from app.models import TaskExecution


def _mock_sender(name: str):
    sender = MagicMock()
    sender.name = name
    return sender


def _session_factory(db):
    """
    Signal handlers open and close their own session (correct production
    behavior — see app/tasks/monitoring.py). Patching SessionLocal to a
    factory bound to the same engine (rather than returning the fixture's
    `db` session directly) means each handler call gets its own session it's
    free to close, without invalidating `db` for the test's own assertions —
    the same pattern test_tasks.py::test_fetch_greenhouse_task_eager uses for
    app.ingestion.panel.SessionLocal.
    """
    return sessionmaker(bind=db.get_bind())


# ---------------------------------------------------------------------------
# task_prerun / task_postrun / task_retry: explicit state tracking
# ---------------------------------------------------------------------------

def test_prerun_only_tracks_allowlisted_tasks(db):
    import app.tasks.monitoring as monitoring_mod

    with patch.object(monitoring_mod, "SessionLocal", _session_factory(db)):
        monitoring_mod._on_task_prerun(
            sender=_mock_sender("app.tasks.embedding.embed_posting"),
            task_id="skip-me",
            args=(1,),
            kwargs={},
        )
    assert db.query(TaskExecution).filter_by(task_id="skip-me").first() is None


def test_prerun_then_postrun_records_success(db):
    import app.tasks.monitoring as monitoring_mod

    task_name = "app.tasks.greenhouse.fetch_greenhouse"
    with patch.object(monitoring_mod, "SessionLocal", _session_factory(db)):
        monitoring_mod._on_task_prerun(
            sender=_mock_sender(task_name), task_id="task-1", args=("acme", 1), kwargs={},
        )
        row = db.query(TaskExecution).filter_by(task_id="task-1").one()
        assert row.state == "STARTED"
        assert row.started_at is not None
        assert row.finished_at is None

        monitoring_mod._on_task_postrun(
            sender=_mock_sender(task_name), task_id="task-1", state="SUCCESS",
        )

    # `row` is already identity-mapped in `db` from the query above — a plain
    # re-query would return that same cached object without re-populating its
    # attributes from the row the other session just updated. refresh()
    # forces the reload.
    db.refresh(row)
    assert row.state == "SUCCESS"
    assert row.finished_at is not None


def test_retry_increments_retry_count_and_records_reason(db):
    import app.tasks.monitoring as monitoring_mod

    task_name = "app.tasks.scheduler.dispatch_lever_batch"
    with patch.object(monitoring_mod, "SessionLocal", _session_factory(db)):
        monitoring_mod._on_task_prerun(
            sender=_mock_sender(task_name), task_id="task-2", args=(), kwargs={},
        )
        request = MagicMock()
        request.id = "task-2"
        monitoring_mod._on_task_retry(
            sender=_mock_sender(task_name), request=request, reason=ConnectionError("redis down"),
        )
        monitoring_mod._on_task_retry(
            sender=_mock_sender(task_name), request=request, reason=ConnectionError("redis down"),
        )

    row = db.query(TaskExecution).filter_by(task_id="task-2").one()
    assert row.state == "RETRY"
    assert row.retries == 2
    assert row.finished_at is None  # still in flight
    assert "redis down" in row.error


def test_postrun_does_not_overwrite_a_failure_recorded_by_task_failure(db):
    """task_failure fires before task_postrun for a failed task (per Celery's
    signal order) and already marks the row FAILURE — postrun must not clobber
    that back to SUCCESS."""
    import app.tasks.monitoring as monitoring_mod

    task_name = "app.tasks.embedding.embed_missing_postings"
    with patch.object(monitoring_mod, "SessionLocal", _session_factory(db)):
        monitoring_mod._on_task_prerun(
            sender=_mock_sender(task_name), task_id="task-3", args=(), kwargs={},
        )
        monitoring_mod._update_task_execution("task-3", state="FAILURE", finished=True, error="boom")
        monitoring_mod._on_task_postrun(
            sender=_mock_sender(task_name), task_id="task-3", state="FAILURE",
        )

    row = db.query(TaskExecution).filter_by(task_id="task-3").one()
    assert row.state == "FAILURE"
    assert row.error == "boom"


# ---------------------------------------------------------------------------
# Worker heartbeats
# ---------------------------------------------------------------------------

def test_record_worker_heartbeats_writes_ttl_keys_for_each_responding_worker():
    import app.tasks.monitoring as monitoring_mod

    mock_redis = MagicMock()
    mock_inspect = MagicMock()
    mock_inspect.ping.return_value = {"worker1@host": {"ok": "pong"}, "worker2@host": {"ok": "pong"}}

    with patch.object(monitoring_mod, "_get_redis", return_value=mock_redis), \
         patch.object(monitoring_mod.celery_app.control, "inspect", return_value=mock_inspect):
        result = monitoring_mod.record_worker_heartbeats()

    assert result == {"workers": 2}
    assert mock_redis.set.call_count == 2
    for call in mock_redis.set.call_args_list:
        (key, _value), kwargs = call
        assert key.startswith(monitoring_mod.HEARTBEAT_NS)
        assert kwargs["ex"] == monitoring_mod.HEARTBEAT_TTL


def test_record_worker_heartbeats_skips_when_redis_unavailable():
    import app.tasks.monitoring as monitoring_mod

    with patch.object(monitoring_mod, "_get_redis", return_value=None):
        result = monitoring_mod.record_worker_heartbeats()

    assert result == {"workers": 0}


def test_get_worker_heartbeats_reads_back_live_keys():
    import app.tasks.monitoring as monitoring_mod

    mock_redis = MagicMock()
    mock_redis.keys.return_value = [f"{monitoring_mod.HEARTBEAT_NS}:worker1@host".encode()]
    mock_redis.get.return_value = b"2026-08-26T00:00:00+00:00"

    with patch.object(monitoring_mod, "_get_redis", return_value=mock_redis):
        heartbeats = monitoring_mod.get_worker_heartbeats()

    assert heartbeats == {"worker1@host": "2026-08-26T00:00:00+00:00"}


def test_get_worker_heartbeats_empty_when_redis_unavailable():
    import app.tasks.monitoring as monitoring_mod

    with patch.object(monitoring_mod, "_get_redis", return_value=None):
        assert monitoring_mod.get_worker_heartbeats() == {}
