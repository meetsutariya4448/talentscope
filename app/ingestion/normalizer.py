import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from html import unescape


_NON_CONTENT_HTML_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)


def _text(value) -> str:
    """Return provider text only when its JSON type is actually a string."""
    return value if isinstance(value, str) else ""


def _first_text(*values) -> str:
    """Return the first nonempty string without trusting truthy non-strings."""
    return next((value for value in values if isinstance(value, str) and value), "")


def _mapping_text(value, key: str) -> str:
    """Read a string from provider metadata without trusting its shape."""
    if not isinstance(value, Mapping):
        return ""
    return _text(value.get(key))


def _source_id(job: Mapping) -> str:
    """Normalize a provider ID without allowing empty-key collisions."""
    value = job.get("id")
    # Public APIs represent identifiers as JSON strings or integers. Coercing
    # floats, lists, or objects creates unstable keys such as "nan" or a
    # Python container representation and can collapse unrelated postings.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("provider job is missing a valid id")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("provider job is missing a valid id")
    return normalized


def _iso_timestamp(value) -> datetime | None:
    """Parse provider ISO timestamps into timezone-aware UTC datetimes."""
    if not isinstance(value, str) or not value:
        return None
    encoded = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(encoded)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _epoch_millis_timestamp(value) -> datetime | None:
    """Parse numeric epoch milliseconds without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_greenhouse(job: dict, company_id: int) -> dict:
    """Normalize a Greenhouse API job record to common shape."""
    title = _text(job.get("title"))
    location = _mapping_text(job.get("location"), "name")
    description = _strip_html(job.get("content", ""))
    url = _text(job.get("absolute_url"))
    source_id = _source_id(job)
    posted_at = _iso_timestamp(job.get("updated_at"))
    return {
        "company_id": company_id,
        "title": title,
        "location": location,
        "description": description,
        "salary_min": None,
        "salary_max": None,
        "currency": "USD",
        "source": "greenhouse",
        "source_id": source_id,
        "url": url,
        "posted_at": posted_at,
    }


def normalize_lever(job: dict, company_id: int) -> dict:
    """Normalize a Lever API posting to common shape."""
    title = _text(job.get("text"))
    location = _mapping_text(job.get("categories"), "location")
    description = _strip_html(_first_text(
        job.get("descriptionPlain"), job.get("description")
    ))
    url = _text(job.get("hostedUrl"))
    source_id = _source_id(job)
    posted_at = _epoch_millis_timestamp(job.get("createdAt"))
    return {
        "company_id": company_id,
        "title": title,
        "location": location,
        "description": description,
        "salary_min": None,
        "salary_max": None,
        "currency": "USD",
        "source": "lever",
        "source_id": source_id,
        "url": url,
        "posted_at": posted_at,
    }


def normalize_ashby(job: dict, company_id: int) -> dict:
    """Normalize an Ashby public job-board API posting to common shape."""
    title = _text(job.get("title"))
    location = _text(job.get("location"))
    description = _strip_html(job.get("descriptionHtml") or "")
    url = _first_text(job.get("jobUrl"), job.get("applyUrl"))
    source_id = _source_id(job)
    posted_at = _iso_timestamp(job.get("publishedAt"))
    return {
        "company_id": company_id,
        "title": title,
        "location": location,
        "description": description,
        "salary_min": None,
        "salary_max": None,
        "currency": "USD",
        "source": "ashby",
        "source_id": source_id,
        "url": url,
        "posted_at": posted_at,
    }


def normalize_adzuna(job: dict) -> dict:
    """Normalize an Adzuna API result to common shape."""
    title = _text(job.get("title"))
    location = _mapping_text(job.get("location"), "display_name")
    description = _text(job.get("description"))
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    url = _text(job.get("redirect_url"))
    source_id = _source_id(job)
    posted_at = _iso_timestamp(job.get("created"))
    company_name = _mapping_text(job.get("company"), "display_name")
    return {
        "company_id": None,  # Adzuna postings don't always map to our company list
        "company_name": company_name,
        "title": title,
        "location": location,
        "description": description,
        "salary_min": _optional_float(salary_min),
        "salary_max": _optional_float(salary_max),
        "currency": "USD",
        "source": "adzuna",
        "source_id": source_id,
        "url": url,
        "posted_at": posted_at,
    }


def _optional_float(value) -> float | None:
    """Parse optional numeric API fields without poisoning a whole batch."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _strip_html(html: object) -> str:
    if not isinstance(html, str) or not html:
        return ""
    html = _NON_CONTENT_HTML_RE.sub(" ", html)
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean
