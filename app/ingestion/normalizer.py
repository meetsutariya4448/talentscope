import math
import re
from collections.abc import Mapping
from datetime import datetime
from html import unescape


def _mapping_text(value, key: str) -> str:
    """Read a string from provider metadata without trusting its shape."""
    if not isinstance(value, Mapping):
        return ""
    text = value.get(key, "")
    return text if isinstance(text, str) else ""


def normalize_greenhouse(job: dict, company_id: int) -> dict:
    """Normalize a Greenhouse API job record to common shape."""
    title = job.get("title", "")
    location = _mapping_text(job.get("location"), "name")
    description = _strip_html(job.get("content", ""))
    url = job.get("absolute_url", "")
    source_id = str(job.get("id", ""))
    posted_at = None
    if job.get("updated_at"):
        try:
            posted_at = datetime.fromisoformat(job["updated_at"].replace("Z", "+00:00"))
        except Exception:
            pass
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
    title = job.get("text", "")
    location = _mapping_text(job.get("categories"), "location")
    description = _strip_html(
        (job.get("descriptionPlain") or job.get("description") or "")
    )
    url = job.get("hostedUrl", "")
    source_id = job.get("id", "")
    posted_at = None
    if job.get("createdAt"):
        try:
            posted_at = datetime.utcfromtimestamp(job["createdAt"] / 1000)
        except Exception:
            pass
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
    title = job.get("title", "")
    location = job.get("location", "")
    description = _strip_html(job.get("descriptionHtml") or "")
    url = job.get("jobUrl") or job.get("applyUrl") or ""
    source_id = str(job.get("id", ""))
    posted_at = None
    if job.get("publishedAt"):
        try:
            posted_at = datetime.fromisoformat(job["publishedAt"].replace("Z", "+00:00"))
        except Exception:
            pass
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
    title = job.get("title", "")
    location = _mapping_text(job.get("location"), "display_name")
    description = job.get("description", "")
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    url = job.get("redirect_url", "")
    source_id = job.get("id", "")
    posted_at = None
    if job.get("created"):
        try:
            posted_at = datetime.fromisoformat(job["created"].replace("Z", "+00:00"))
        except Exception:
            pass
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
    return parsed if math.isfinite(parsed) else None


def _strip_html(html: str) -> str:
    if not html:
        return ""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean
