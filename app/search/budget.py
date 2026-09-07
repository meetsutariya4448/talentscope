"""Spend controls for the paid-LLM path.

Before this module, `POST /qa/ask` was public, unauthenticated, CORS-`*`, and
every uncached request became a billed Groq call. The only brake was the
exact-string-match Redis response cache in app/search/rag.py — which silently
disabled itself whenever Redis was unavailable, i.e. the one failure mode where
you would most want a brake.

Two independent limits, both counted in Redis:

  * a global daily budget on *actual provider calls*, so total spend per day is
    bounded no matter who is asking; and
  * a per-client per-minute rate limit, so one caller cannot consume the day's
    budget in a burst.

Only a real provider call consumes budget. A cache hit must not, or a popular
question would exhaust the day's allowance while costing nothing.

When Redis is unavailable and `qa_require_budget_counter` is on, the answer is
to refuse the LLM call, not to proceed uncounted: an unbounded spend path is
worse than a degraded response. Retrieval still runs either way, so the caller
gets sources back rather than an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings
from app.tasks.redis_utils import get_redis

logger = logging.getLogger(__name__)

# Sentinel distinguishing "caller did not supply a client, look one up" from
# "caller looked one up and got None because Redis is down". Without this,
# app/api/qa.py passing its own None would silently cause a second lookup here
# — and a fail-closed check that quietly reconnects is not fail-closed.
_UNSET = object()

BUDGET_KEY_PREFIX = "qa:budget:"
RATE_KEY_PREFIX = "qa:rate:"

# Budget counters are keyed by UTC date and expire two days later — long
# enough that a counter cannot vanish mid-day in any timezone, short enough
# that yesterday's keys do not accumulate.
BUDGET_TTL_SECONDS = 2 * 24 * 3600
RATE_WINDOW_SECONDS = 60


class Outcome:
    """Why a Q&A request was or wasn't allowed to call the provider."""

    ALLOWED = "allowed"
    CACHED = "cached"
    LLM_DISABLED = "llm_disabled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"
    COUNTER_UNAVAILABLE = "counter_unavailable"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    outcome: str
    # Seconds the caller should wait before retrying, when that is knowable.
    retry_after: int | None = None
    remaining: int | None = None


def _budget_key(now: datetime) -> str:
    return f"{BUDGET_KEY_PREFIX}{now:%Y%m%d}"


def _rate_key(client_id: str, now: datetime) -> str:
    return f"{RATE_KEY_PREFIX}{client_id}:{int(now.timestamp()) // RATE_WINDOW_SECONDS}"


def check_rate_limit(client_id: str, redis_client=_UNSET) -> Decision:
    """Fixed-window per-client limit.

    A fixed window can allow up to 2x the limit across a window boundary. That
    is accepted deliberately: this is a spend guard backed by a hard daily
    budget, not a fairness scheduler, and a sliding window costs more Redis
    round-trips on the request path than the extra precision is worth here.
    """
    limit = settings.qa_rate_limit_per_min
    if limit <= 0:
        return Decision(True, Outcome.ALLOWED)

    rc = get_redis() if redis_client is _UNSET else redis_client
    if rc is None:
        # Rate limiting is a fairness control; the daily budget below is the
        # real spend bound. Not being able to count here is not itself a
        # reason to refuse.
        logger.warning("Q&A rate limit: Redis unavailable, not enforcing per-client limit")
        return Decision(True, Outcome.ALLOWED)

    now = datetime.now(timezone.utc)
    key = _rate_key(client_id, now)
    try:
        count = rc.incr(key)
        if count == 1:
            rc.expire(key, RATE_WINDOW_SECONDS)
    except Exception:
        logger.warning("Q&A rate limit: Redis error, not enforcing", exc_info=True)
        return Decision(True, Outcome.ALLOWED)

    if count > limit:
        retry_after = RATE_WINDOW_SECONDS - (int(now.timestamp()) % RATE_WINDOW_SECONDS)
        return Decision(False, Outcome.RATE_LIMITED, retry_after=max(retry_after, 1))
    return Decision(True, Outcome.ALLOWED, remaining=max(limit - count, 0))


def consume_budget(redis_client=_UNSET) -> Decision:
    """Claim one unit of today's provider-call budget.

    Increments first and compares afterwards, so two concurrent requests cannot
    both observe "one left" and both proceed. A request that overshoots does not
    decrement its increment back: the counter is a spend ceiling, and letting it
    drift upward is the safe direction to be wrong in.
    """
    budget = settings.qa_daily_budget
    if budget <= 0:
        # 0 means "no LLM calls at all today" — an explicit, useful setting for
        # a public demo, distinct from the qa_llm_enabled flag.
        return Decision(False, Outcome.BUDGET_EXHAUSTED, remaining=0)

    rc = get_redis() if redis_client is _UNSET else redis_client
    if rc is None:
        if settings.qa_require_budget_counter:
            logger.warning(
                "Q&A budget: Redis unavailable and qa_require_budget_counter is on — "
                "refusing the provider call rather than spending uncounted"
            )
            return Decision(False, Outcome.COUNTER_UNAVAILABLE)
        logger.warning("Q&A budget: Redis unavailable, proceeding uncounted")
        return Decision(True, Outcome.ALLOWED)

    now = datetime.now(timezone.utc)
    key = _budget_key(now)
    try:
        used = rc.incr(key)
        if used == 1:
            rc.expire(key, BUDGET_TTL_SECONDS)
    except Exception:
        logger.warning("Q&A budget: Redis error while counting", exc_info=True)
        if settings.qa_require_budget_counter:
            return Decision(False, Outcome.COUNTER_UNAVAILABLE)
        return Decision(True, Outcome.ALLOWED)

    if used > budget:
        return Decision(False, Outcome.BUDGET_EXHAUSTED, remaining=0)
    return Decision(True, Outcome.ALLOWED, remaining=max(budget - used, 0))


def budget_remaining(redis_client=_UNSET) -> int | None:
    """Today's remaining provider calls, or None if it cannot be determined."""
    rc = get_redis() if redis_client is _UNSET else redis_client
    if rc is None:
        return None
    try:
        raw = rc.get(_budget_key(datetime.now(timezone.utc)))
    except Exception:
        return None
    used = int(raw) if raw else 0
    return max(settings.qa_daily_budget - used, 0)
