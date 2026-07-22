import logging

logger = logging.getLogger(__name__)

# One model instance per process. Loaded lazily so the API and workers both
# avoid the ~3 s startup penalty until the first search or embed request.
_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading sentence-transformer model (all-MiniLM-L6-v2)…")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Model loaded.")
    return _model
