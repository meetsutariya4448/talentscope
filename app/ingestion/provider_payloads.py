from collections.abc import Mapping


def extract_job_list(payload: object, *, key: str | None = None) -> list[dict]:
    """Return a provider's job list only when the response shape is trustworthy.

    Authoritative boards use an empty list to mean that every known posting has
    closed. Treating a malformed response as that empty list would therefore
    turn an upstream schema problem into false disappearance data.
    """
    if key is not None:
        if not isinstance(payload, Mapping):
            raise ValueError("provider response must be an object")
        payload = payload.get(key)

    if not isinstance(payload, list):
        raise ValueError("provider job collection must be a list")
    if not all(isinstance(job, Mapping) for job in payload):
        raise ValueError("provider job collection contains a non-object entry")
    return [dict(job) for job in payload]
