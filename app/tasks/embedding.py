import logging
from sqlalchemy import select
from app.tasks.celery_app import app as celery_app
from app.database import SessionLocal
from app.models import Posting

logger = logging.getLogger(__name__)

# Lazy singleton — model is ~80 MB and takes ~3 s to load; load once per worker process.
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading sentence-transformer model (all-MiniLM-L6-v2)…")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Model loaded.")
    return _model


def _build_text(posting: Posting) -> str:
    return f"{posting.title} {posting.description or ''}".strip()


@celery_app.task(
    name="app.tasks.embedding.embed_posting",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    max_retries=3,
)
def embed_posting(self, posting_id: int):
    """Compute and store the 384-d embedding for a single posting."""
    db = SessionLocal()
    try:
        posting = db.get(Posting, posting_id)
        if not posting:
            logger.warning(f"embed_posting: posting {posting_id} not found, skipping")
            return {"posting_id": posting_id, "skipped": True}

        content = _build_text(posting)
        # normalize_embeddings=True matches the model's training convention (MNR loss on unit sphere)
        # and keeps inner-product search viable as a future optimisation (dot product == cosine on unit vectors).
        # Cosine similarity is scale-invariant, so vector_cosine_ops ranks identically either way.
        vec = _get_model().encode(content, normalize_embeddings=True).tolist()

        # Assign via ORM so pgvector.sqlalchemy.Vector handles type serialization
        posting.embedding = vec
        db.commit()
        logger.debug(f"Embedded posting {posting_id}")
        return {"posting_id": posting_id, "embedded": True}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.embedding.embed_missing_postings",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
    max_retries=3,
)
def embed_missing_postings(self, batch_size: int = 200):
    """
    Backfill: find postings with no embedding and dispatch embed_posting for each.
    Runs every hour via Celery Beat so newly ingested postings are never stuck.
    """
    db = SessionLocal()
    try:
        ids = db.execute(
            select(Posting.id)
            .where(Posting.embedding.is_(None))
            .order_by(Posting.id)
            .limit(batch_size)
        ).scalars().all()

        for pid in ids:
            embed_posting.delay(pid)

        logger.info(f"embed_missing_postings: dispatched {len(ids)} tasks")
        return {"dispatched": len(ids)}
    finally:
        db.close()
