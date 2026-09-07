from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "postgresql://talentscope:talentscope@localhost:5432/talentscope"
    redis_url: str = "redis://localhost:6379/0"
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    groq_api_key: str = ""

    # Connection pool sizing. Defaults assume one api process + Celery workers
    # sharing the DB: pool_size covers steady-state concurrent requests,
    # max_overflow absorbs bursts before new connections start blocking.
    # pool_pre_ping issues a cheap SELECT 1 before handing out a pooled
    # connection so a connection gone stale (DB restart, idle proxy timeout)
    # is detected and replaced instead of surfacing as a query-time error.
    # pool_recycle forces a periodic reconnect so no connection outlives
    # whatever idle timeout sits in front of Postgres in production (e.g. an
    # RDS proxy or pgbouncer) — SQLAlchemy's own default never recycles.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_recycle_seconds: int = Field(default=1800, ge=1)
    db_pool_pre_ping: bool = True

    # HNSW runtime search width for vector_search() (app/search/hybrid.py).
    # None means "don't override" — pgvector auto-raises the effective value
    # to at least the query's LIMIT regardless (see docs/db-engineering.md),
    # so this only matters as an override *above* TOP_K to trade latency for
    # closer-to-exact recall on the RRF candidate pool. Tune via
    # scripts/db_engineering_report.py's ef_search sweep before changing.
    vector_ef_search: int | None = Field(default=None, ge=1)

    # Warm the sentence-transformers model in a background thread at startup
    # instead of on the first vector/hybrid request. /ready stays 503 until it
    # finishes, so a cold process is never advertised as able to serve search.
    # Turned off in tests and in CI, where loading a 90 MB model per test
    # session buys nothing.
    embedding_warmup_enabled: bool = True

    # Root log level for the application's own loggers. uvicorn and celery
    # each configure only their own logger trees, so without an explicit
    # setup here anything logged by app.* — including the embedding warmup
    # and every logger.warning in the ingestion path — is emitted to a
    # handler-less logger and silently dropped.
    log_level: str = "INFO"

    # Celery rate limit for embed_posting, in Celery's own notation
    # ("300/m", "50/s", or "" to disable). Made configurable because it, not
    # the CPU budget, turned out to be the binding constraint on ingestion
    # throughput: at the 300/m default a worker with 2 dedicated CPUs idles
    # at roughly 6% while the queue drains at the limit. Leave it at the
    # default in normal operation — it is deliberate backpressure — and raise
    # it only to find where the CPU budget itself starts to bind
    # (evals/cpu-budget.md).
    embed_rate_limit: str = "300/m"

    # --- Paid-LLM spend controls (app/search/budget.py) ---
    # POST /qa/ask is public, unauthenticated and CORS-*, and every uncached
    # request is a billed Groq call. These bound that: a global ceiling on
    # provider calls per UTC day, and a per-client burst limit. Exceeding
    # either degrades the response to retrieval-only rather than failing it.
    qa_llm_enabled: bool = True
    qa_daily_budget: int = Field(default=200, ge=0)
    qa_rate_limit_per_min: int = Field(default=5, ge=0)
    # When Redis is down the daily counter cannot be maintained. True means
    # refuse the provider call rather than spend uncounted — the Redis cache
    # was previously the only brake, and it disabled itself in exactly this
    # situation.
    qa_require_budget_counter: bool = True

    # The provider's chat model. Configurable rather than hardcoded because
    # hosted model names are decommissioned without notice: the previous
    # hardcoded value, llama-3.1-8b-instant, returned
    # "model_not_found / does not exist or you do not have access to it" from
    # Groq on 2026-09-06, which surfaced as a blanket 503 from /qa/ask with no
    # indication that the cause was a retired model name. Check the current
    # list with:
    #   curl -H "Authorization: Bearer $GROQ_API_KEY" \
    #        https://api.groq.com/openai/v1/models
    groq_model: str = "openai/gpt-oss-20b"

settings = Settings()
