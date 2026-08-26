import logging
import threading

logger = logging.getLogger(__name__)

# One model instance per process. Loaded lazily so the API and workers both
# avoid the ~3 s startup penalty until the first search or embed request.
_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    # Double-checked locking: the uncontended fast path (near-100% of
    # calls, once warm) never touches the lock. Without it, FastAPI's
    # threadpool (sync def handlers each run on their own OS thread) lets
    # two concurrent requests both see `_model is None` and both construct
    # SentenceTransformer(...) at once — found under real load testing at
    # just 2 concurrent vector-search requests: PyTorch's meta-tensor
    # module init isn't safe against that and raised "Cannot copy out of
    # meta tensor; no data!", a real request-serving 500 on a cold process
    # under nothing more than ordinary concurrent traffic.
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading sentence-transformer model (all-MiniLM-L6-v2)…")
                _model = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("Model loaded.")
    return _model
