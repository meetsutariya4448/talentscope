import redis as redis_lib
from app.config import settings


def get_redis() -> redis_lib.Redis | None:
    """Best-effort Redis client. Returns None (never raises) so callers can
    fall back to degraded-but-correct behavior when Redis is unavailable."""
    try:
        client = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        client.ping()
        return client
    except Exception:
        return None
