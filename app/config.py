from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800
    db_pool_pre_ping: bool = True

    # HNSW runtime search width for vector_search() (app/search/hybrid.py).
    # None means "don't override" — pgvector auto-raises the effective value
    # to at least the query's LIMIT regardless (see docs/db-engineering.md),
    # so this only matters as an override *above* TOP_K to trade latency for
    # closer-to-exact recall on the RRF candidate pool. Tune via
    # scripts/db_engineering_report.py's ef_search sweep before changing.
    vector_ef_search: int | None = None

    class Config:
        env_file = ".env"


settings = Settings()
