"""Shared pre-handler guards for the HTTP layer."""

from fastapi import HTTPException

from app.search.encoder import is_model_ready

# Long enough that a client backing off doesn't hammer a process that is
# still loading, short enough that a warm process is picked up promptly.
# Model warmup is single-digit seconds once the model ships in the image
# (see Dockerfile) — this is not sized for a cold HuggingFace download.
MODEL_RETRY_AFTER_SECONDS = 5


def require_model_ready() -> None:
    """Refuse a request that needs the embedding model before it is warm.

    Returning 503 + Retry-After is deliberately better than letting the
    request through: the alternative is that the first caller after a restart
    blocks a threadpool thread for the whole model load and gets a response
    seconds late, while every concurrent caller queues behind it. Failing fast
    with a retriable status lets a proxy route the request to a warm replica
    instead (see observability/nginx/nginx.conf's proxy_next_upstream), which
    is what "don't route search traffic too early" actually means in practice.
    """
    if not is_model_ready():
        raise HTTPException(
            status_code=503,
            detail="Embedding model is still warming up; retry shortly.",
            headers={"Retry-After": str(MODEL_RETRY_AFTER_SECONDS)},
        )
