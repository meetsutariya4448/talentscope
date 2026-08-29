import logging
from sqlalchemy import select
from app.tasks.celery_app import app as celery_app
from app.tasks.redis_utils import get_redis as _get_redis
from app.database import SessionLocal
from app.models import Posting
from app.search.encoder import get_model

logger = logging.getLogger(__name__)

# Re-export for callers that import _get_model directly (e.g. tests)
_get_model = get_model

# In-flight guard for the hourly backfill: without it, a batch of postings
# still embedding from the previous firing gets re-selected (still
# embedding IS NULL) and re-dispatched, doubling work under any backlog.
# TTL matches the beat cadence — if a claimed task hasn't cleared its key
# within an hour it's presumed lost, and the id becomes eligible again.
PENDING_NS = "talentscope:embed:pending"
PENDING_TTL = 3600


def _release_pending(redis_client, posting_id: int) -> None:
    try:
        redis_client.delete(f"{PENDING_NS}:{posting_id}")
    except Exception:
        logger.warning("Failed to clear pending-embed marker for posting %s", posting_id)


def _clear_pending(posting_id: int) -> None:
    rc = _get_redis()
    if rc is None:
        return
    _release_pending(rc, posting_id)


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
        vec = get_model().encode(content, normalize_embeddings=True).tolist()

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
        _clear_pending(posting_id)


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

        rc = _get_redis()
        dispatched = 0
        skipped_in_flight = 0
        for pid in ids:
            if rc is not None:
                # Atomic claim: only the first caller to see this id un-set
                # gets to dispatch it, so an overlapping firing (previous
                # hour's batch still embedding) skips ids already in flight
                # instead of re-dispatching duplicate work.
                claimed = rc.set(f"{PENDING_NS}:{pid}", "1", nx=True, ex=PENDING_TTL)
                if not claimed:
                    skipped_in_flight += 1
                    continue
            try:
                embed_posting.delay(pid)
            except Exception:
                # The task was never accepted by the broker, so release the
                # claim immediately.  Leaving it behind would make the retry
                # skip this posting until the one-hour TTL expires.
                if rc is not None:
                    _release_pending(rc, pid)
                raise
            dispatched += 1

        logger.info(
            f"embed_missing_postings: dispatched {dispatched} tasks, "
            f"skipped {skipped_in_flight} already in flight"
        )
        return {"dispatched": dispatched, "skipped_in_flight": skipped_in_flight}
    finally:
        db.close()
