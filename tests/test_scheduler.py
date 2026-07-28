"""
Tests for scheduler batch rotation and Redis cursor persistence.

Property under test: _next_batch() steps an atomic Redis counter so the batch
cursor survives worker restarts (module reload).  With the old module-global
implementation a reload reset _gh_batch_index to 0; with Redis the counter
persists and the next dispatch returns a different batch.
"""
import importlib
import math
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_mock(initial_value: int = 0) -> MagicMock:
    """
    Return a MagicMock Redis client whose incr() increments a shared counter.
    The counter starts at initial_value so tests can seed a mid-cycle position.
    """
    mock_redis = MagicMock()
    counter = [initial_value]

    def fake_incr(key):
        counter[0] += 1
        return counter[0]

    mock_redis.incr.side_effect = fake_incr
    return mock_redis


def _make_db_mock():
    """Return (mock_SessionLocal, mock_session) with a usable company stub."""
    mock_company = MagicMock()
    mock_company.id = 1
    mock_session = MagicMock()
    mock_session.query.return_value.filter_by.return_value.first.return_value = mock_company
    mock_sessionlocal = MagicMock(return_value=mock_session)
    return mock_sessionlocal, mock_session


# ---------------------------------------------------------------------------
# Unit tests for _next_batch
# ---------------------------------------------------------------------------

class TestNextBatch:
    def test_first_call_returns_batch_0(self):
        from app.tasks.scheduler import _next_batch
        rc = _make_redis_mock()
        items = list(range(46))
        result = _next_batch(rc, "key", items, 10)
        assert result == items[0:10]

    def test_second_call_returns_batch_1(self):
        from app.tasks.scheduler import _next_batch
        rc = _make_redis_mock()
        items = list(range(46))
        _next_batch(rc, "key", items, 10)   # consume batch 0
        result = _next_batch(rc, "key", items, 10)
        assert result == items[10:20]

    def test_last_batch_is_shorter(self):
        """46 items / 10 = last batch has 6 items."""
        from app.tasks.scheduler import _next_batch
        rc = _make_redis_mock(initial_value=4)  # next INCR returns 5 → batch_no=4
        items = list(range(46))
        result = _next_batch(rc, "key", items, 10)
        assert result == items[40:46]
        assert len(result) == 6

    def test_wraparound(self):
        """After n_batches calls, the next call returns batch 0 again."""
        from app.tasks.scheduler import _next_batch, GREENHOUSE_COMPANIES, BATCH_SIZE
        n_batches = math.ceil(len(GREENHOUSE_COMPANIES) / BATCH_SIZE)
        rc = _make_redis_mock()
        # consume all batches
        for _ in range(n_batches):
            _next_batch(rc, "key", GREENHOUSE_COMPANIES, BATCH_SIZE)
        # (n_batches + 1)th call wraps to batch 0
        result = _next_batch(rc, "key", GREENHOUSE_COMPANIES, BATCH_SIZE)
        assert result == GREENHOUSE_COMPANIES[:BATCH_SIZE]


# ---------------------------------------------------------------------------
# Restart / cursor-persistence property
# ---------------------------------------------------------------------------

class TestRestartSurvival:
    def test_cursor_survives_module_reload(self):
        """
        Worker restart (importlib.reload) must not reset the batch cursor.

        With the old module-global implementation, reload resets
        _gh_batch_index = 0, causing the second dispatch to return batch 0
        again.  With the Redis cursor, the counter is external — reload has
        no effect on it, so the second dispatch returns batch 1.
        """
        import app.tasks.scheduler as scheduler

        mock_redis = _make_redis_mock()
        mock_sessionlocal, _ = _make_db_mock()

        with patch.object(scheduler, "_get_redis", return_value=mock_redis), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_greenhouse"):
            result1 = scheduler.dispatch_greenhouse_batch()
        batch1 = result1["dispatched"]

        # Simulate worker restart: reload resets all module-level state.
        scheduler = importlib.reload(scheduler)

        # Same mock_redis — counter is still 1 from the previous dispatch.
        with patch.object(scheduler, "_get_redis", return_value=mock_redis), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_greenhouse"):
            result2 = scheduler.dispatch_greenhouse_batch()
        batch2 = result2["dispatched"]

        assert batch2 != batch1, "Second batch should differ from first after restart"
        # Specifically: it should be batch 1 (indices 10-19), not batch 0 again
        from app.tasks.scheduler import GREENHOUSE_COMPANIES, BATCH_SIZE
        assert batch2 != [t for t, _ in GREENHOUSE_COMPANIES[:BATCH_SIZE]], (
            "Cursor was reset by module reload — Redis persistence not working"
        )


# ---------------------------------------------------------------------------
# Full rotation coverage
# ---------------------------------------------------------------------------

class TestFullRotation:
    def test_greenhouse_all_companies_covered_in_one_cycle(self):
        """
        Over ceil(46/10) = 5 consecutive dispatches every Greenhouse company
        appears in exactly one batch.
        """
        import app.tasks.scheduler as scheduler
        from app.tasks.scheduler import GREENHOUSE_COMPANIES, BATCH_SIZE

        mock_redis = _make_redis_mock()
        mock_sessionlocal, _ = _make_db_mock()
        n_batches = math.ceil(len(GREENHOUSE_COMPANIES) / BATCH_SIZE)

        all_dispatched: list[str] = []
        with patch.object(scheduler, "_get_redis", return_value=mock_redis), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_greenhouse"):
            for _ in range(n_batches):
                result = scheduler.dispatch_greenhouse_batch()
                all_dispatched.extend(result["dispatched"])

        expected = {t for t, _ in GREENHOUSE_COMPANIES}
        assert set(all_dispatched) == expected, (
            f"Missing: {expected - set(all_dispatched)}"
        )
        # No company dispatched twice in one cycle
        assert len(all_dispatched) == len(GREENHOUSE_COMPANIES), (
            "Duplicate dispatches within a single cycle"
        )

    def test_lever_all_companies_covered_in_one_cycle(self):
        """Over ceil(12/10) = 2 dispatches every Lever company appears."""
        import app.tasks.scheduler as scheduler
        from app.tasks.scheduler import LEVER_COMPANIES, BATCH_SIZE

        mock_redis = _make_redis_mock()
        mock_sessionlocal, _ = _make_db_mock()
        n_batches = math.ceil(len(LEVER_COMPANIES) / BATCH_SIZE)

        all_dispatched: list[str] = []
        with patch.object(scheduler, "_get_redis", return_value=mock_redis), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_lever"):
            for _ in range(n_batches):
                result = scheduler.dispatch_lever_batch()
                all_dispatched.extend(result["dispatched"])

        expected = {s for s, _ in LEVER_COMPANIES}
        assert set(all_dispatched) == expected, (
            f"Missing: {expected - set(all_dispatched)}"
        )

    def test_clean_wraparound_returns_batch_0_after_full_cycle(self):
        """
        Dispatch n_batches + 1 times.  The last batch must equal the first.
        """
        import app.tasks.scheduler as scheduler
        from app.tasks.scheduler import GREENHOUSE_COMPANIES, BATCH_SIZE

        mock_redis = _make_redis_mock()
        mock_sessionlocal, _ = _make_db_mock()
        n_batches = math.ceil(len(GREENHOUSE_COMPANIES) / BATCH_SIZE)

        results: list[list[str]] = []
        with patch.object(scheduler, "_get_redis", return_value=mock_redis), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_greenhouse"):
            for _ in range(n_batches + 1):
                r = scheduler.dispatch_greenhouse_batch()
                results.append(r["dispatched"])

        assert results[-1] == results[0], (
            f"Wraparound failed: last={results[-1]!r} first={results[0]!r}"
        )


# ---------------------------------------------------------------------------
# Fallback when Redis is unavailable
# ---------------------------------------------------------------------------

class TestRedisFallback:
    def test_greenhouse_falls_back_to_batch_0_when_redis_none(self):
        """_get_redis() returns None → dispatch uses batch 0 without raising."""
        import app.tasks.scheduler as scheduler
        from app.tasks.scheduler import GREENHOUSE_COMPANIES, BATCH_SIZE

        mock_sessionlocal, _ = _make_db_mock()

        with patch.object(scheduler, "_get_redis", return_value=None), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_greenhouse"):
            result = scheduler.dispatch_greenhouse_batch()

        expected = [t for t, _ in GREENHOUSE_COMPANIES[:BATCH_SIZE]]
        assert result["dispatched"] == expected

    def test_greenhouse_falls_back_when_incr_raises(self):
        """Redis connected but INCR raises → falls back to batch 0."""
        import app.tasks.scheduler as scheduler
        from app.tasks.scheduler import GREENHOUSE_COMPANIES, BATCH_SIZE

        mock_redis = MagicMock()
        mock_redis.incr.side_effect = Exception("READONLY Redis replica")
        mock_sessionlocal, _ = _make_db_mock()

        with patch.object(scheduler, "_get_redis", return_value=mock_redis), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_greenhouse"):
            result = scheduler.dispatch_greenhouse_batch()

        expected = [t for t, _ in GREENHOUSE_COMPANIES[:BATCH_SIZE]]
        assert result["dispatched"] == expected

    def test_lever_falls_back_to_batch_0_when_redis_none(self):
        """Lever batch dispatcher also falls back gracefully."""
        import app.tasks.scheduler as scheduler
        from app.tasks.scheduler import LEVER_COMPANIES, BATCH_SIZE

        mock_sessionlocal, _ = _make_db_mock()

        with patch.object(scheduler, "_get_redis", return_value=None), \
             patch.object(scheduler, "SessionLocal", mock_sessionlocal), \
             patch.object(scheduler, "fetch_lever"):
            result = scheduler.dispatch_lever_batch()

        expected = [s for s, _ in LEVER_COMPANIES[:BATCH_SIZE]]
        assert result["dispatched"] == expected
