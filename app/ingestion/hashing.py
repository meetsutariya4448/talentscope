import hashlib
import re

_RELATIVE_DATE_PATTERNS = [
    re.compile(r"posted\s+\d+\+?\s*(day|week|month|hour)s?\s+ago", re.IGNORECASE),
    re.compile(r"\b\d+\+?\s*(day|week|month|hour)s?\s+ago\b", re.IGNORECASE),
    re.compile(r"\bjust\s+posted\b", re.IGNORECASE),
    re.compile(r"\bposted\s+today\b", re.IGNORECASE),
]
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_description(text: str | None) -> str:
    """
    Normalize posting description text before hashing, so description_hash only
    changes on substantive edits — not HTML reformatting, whitespace, casing, or
    a relative "posted N days ago" string ticking over. Un-normalized hashing
    would fill posting_description_versions with noise and make requirement
    drift unmeasurable.
    """
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    for pattern in _RELATIVE_DATE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def hash_text(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
