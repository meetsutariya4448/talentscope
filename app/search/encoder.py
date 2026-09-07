import logging
import threading
import time

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"

# One model instance per process. Loaded lazily so the API and workers both
# avoid the ~3 s startup penalty until the first search or embed request.
_model = None
_model_lock = threading.Lock()

# Flipped once the model is not just constructed but has completed a real
# encode() — see warm_model(). /ready (app/main.py) gates on this, and the
# vector/hybrid search paths refuse traffic until it is set, so a cold
# process never serves a search request by loading the model inside it.
_ready = threading.Event()
_load_seconds: float | None = None


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
                logger.info("Loading sentence-transformer model (%s)…", MODEL_NAME)
                _model = SentenceTransformer(MODEL_NAME)
                logger.info("Model loaded.")
    # A lazy first caller that got here without warm_model() still counts as
    # warm from this point on — the expensive part is behind it either way.
    _ready.set()
    return _model


def warm_model() -> float:
    """Load the model AND run one real encode, then mark the process ready.

    Constructing SentenceTransformer is not sufficient on its own: the first
    encode() is what forces the lazy per-submodule tensor materialisation and
    the tokenizer's first-call setup, which is where a meaningful part of the
    cold-start cost actually lives. Warming without it would flip readiness
    while the first real search still paid a visible penalty.

    Safe to call from several threads/processes; the underlying construction
    is already guarded by get_model()'s lock. Returns seconds elapsed.
    """
    global _load_seconds
    start = time.perf_counter()
    model = get_model()
    model.encode(["warmup"], normalize_embeddings=True)
    elapsed = time.perf_counter() - start
    _load_seconds = elapsed
    _ready.set()
    logger.info("Embedding model warm after %.2f s (%s)", elapsed, MODEL_NAME)
    return elapsed


def is_model_ready() -> bool:
    """True once this process can serve a vector search without loading anything."""
    return _ready.is_set()


def model_load_seconds() -> float | None:
    """Seconds the warmup took, or None if warm_model() never ran here."""
    return _load_seconds
