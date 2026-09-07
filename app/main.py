import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.api import postings, analytics, qa
from app.config import settings
from app.database import SessionLocal, engine
from app.observability import (
    EMBEDDING_MODEL_LOAD_SECONDS,
    EMBEDDING_MODEL_READY,
    QA_BUDGET_REMAINING,
    setup_db_metrics,
    setup_http_metrics,
    setup_tracing,
)
from app.search.encoder import is_model_ready, warm_model
from app.tasks.redis_utils import get_redis
import os

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Attach a handler to the application's own logger tree.

    uvicorn configures `uvicorn*` loggers and celery configures `celery*`;
    neither touches `app.*`, so every logger.info/warning in this codebase was
    going to a logger with no handler and being discarded — including the
    embedding warmup timing and the /ready failure warnings, which are exactly
    the lines you want when diagnosing a cold start. Uses force=True because
    uvicorn has already installed its own root configuration by this point.
    """
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def _warm_embedding_model() -> None:
    """Background warmup body. Never raises into the thread's caller — a
    failed warmup must leave /ready reporting 503, not take the process down."""
    try:
        elapsed = warm_model()
        EMBEDDING_MODEL_LOAD_SECONDS.set(elapsed)
        EMBEDDING_MODEL_READY.set(1)
    except Exception:
        logger.exception("Embedding model warmup failed; /ready will stay degraded")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm in a *daemon thread*, not inline. Inline would block uvicorn from
    # binding the port, which means /health can't answer either — and a
    # liveness probe that fails during a slow model load gets the container
    # killed and restarted into the same slow load, forever. Warming off to
    # the side keeps liveness honest (the process is up) while /ready stays
    # 503 until the model can actually serve a search.
    _configure_logging()
    EMBEDDING_MODEL_READY.set(0)

    # Seed the Q&A budget gauge from the real remaining allowance before any
    # request touches it. A Prometheus Gauge that has never been .set() reads
    # 0, and 0 on *this* gauge means "budget exhausted" — so a freshly
    # restarted api fired QaBudgetExhausted until the first Q&A request
    # happened to arrive, which is an alert firing for the absence of traffic.
    try:
        from app.search.budget import budget_remaining
        remaining = budget_remaining()
        QA_BUDGET_REMAINING.set(
            settings.qa_daily_budget if remaining is None else remaining
        )
    except Exception:
        logger.warning("Could not seed the Q&A budget gauge", exc_info=True)

    if settings.embedding_warmup_enabled:
        threading.Thread(
            target=_warm_embedding_model, name="embedding-warmup", daemon=True
        ).start()
    else:
        logger.info("Embedding warmup disabled (EMBEDDING_WARMUP_ENABLED=false)")
    yield


app = FastAPI(title="TalentScope", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_http_metrics(app)
setup_db_metrics(engine)
setup_tracing(app=app, engine=engine)

app.include_router(postings.router, prefix="/postings", tags=["postings"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(qa.router, prefix="/qa", tags=["qa"])


@app.get("/health")
def health():
    """Liveness probe: is the process up and able to serve a request at
    all? Deliberately does not touch the DB/Redis/model — a slow or
    unavailable dependency should surface via /ready (and get retried/routed
    around), not cause Kubernetes to kill and restart a perfectly healthy
    process. In particular this stays 200 throughout the embedding-model
    warmup, which is exactly when a dependency-checking liveness probe would
    have caused a restart loop."""
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response):
    """Readiness probe: can this instance actually serve traffic right now?
    Checks the three hard dependencies — DB, Redis, and the embedding model —
    each cheaply. A failing check here should pull the pod out of the
    Service's endpoint list, not restart it (that's what /health is for).

    The model check is what stops a cold process being advertised as able to
    serve vector/hybrid search: it is lazy-loaded per process, so without this
    the first search request after a restart pays the whole load cost inside
    the request."""
    checks = {"database": False, "redis": False, "embedding_model": False}

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        logger.warning("/ready: database check failed", exc_info=True)
    finally:
        db.close()

    rc = get_redis()
    if rc is not None:
        checks["redis"] = True

    checks["embedding_model"] = is_model_ready()

    ok = all(checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "checks": checks}


# Serve dashboard
dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(dashboard_path):
    app.mount("/dashboard", StaticFiles(directory=dashboard_path, html=True), name="dashboard")
