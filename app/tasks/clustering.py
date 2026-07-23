from app.tasks.celery_app import celery_app
from app.database import SessionLocal
import logging

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.clustering.run_clustering_task",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=3,
)
def run_clustering_task(self, k: int | None = None):
    """Celery task: cluster all embedded postings and persist results."""
    from app.ml.clustering import run_clustering

    db = SessionLocal()
    try:
        result = run_clustering(db, k=k)
        logger.info("Clustering done: %s", result.get("k"))
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
