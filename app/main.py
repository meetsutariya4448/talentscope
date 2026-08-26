import logging

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.api import postings, analytics, qa
from app.database import SessionLocal, engine
from app.observability import setup_db_metrics, setup_http_metrics, setup_tracing
from app.tasks.redis_utils import get_redis
import os

logger = logging.getLogger(__name__)

app = FastAPI(title="TalentScope", version="1.0.0")

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
    all? Deliberately does not touch the DB/Redis — a slow/unavailable
    dependency should surface via /ready (and get retried/routed around),
    not cause Kubernetes to kill and restart a perfectly healthy process."""
    return {"status": "ok"}


@app.get("/ready")
def ready(response: Response):
    """Readiness probe: can this instance actually serve traffic right now?
    Checks the two hard dependencies — DB and Redis — each with a cheap
    query/ping. A failing check here should pull the pod out of the
    Service's endpoint list, not restart it (that's what /health is for)."""
    checks = {"database": False, "redis": False}

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

    ok = all(checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "checks": checks}


# Serve dashboard
dashboard_path = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(dashboard_path):
    app.mount("/dashboard", StaticFiles(directory=dashboard_path, html=True), name="dashboard")
