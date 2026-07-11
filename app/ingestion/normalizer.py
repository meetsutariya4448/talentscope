from datetime import datetime
from typing import Optional
import re


def normalize_greenhouse(job: dict, company_id: int) -> dict:
    """Normalize a Greenhouse API job record to common shape."""
    title = job.get("title", "")
    location = ""
    if job.get("location"):
        location = job["location"].get("name", "")
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
    location = ""
    if job.get("categories"):
        location = job["categories"].get("location", "")
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


def normalize_adzuna(job: dict) -> dict:
    """Normalize an Adzuna API result to common shape."""
    title = job.get("title", "")
    location = ""
    if job.get("location"):
        display_name = job["location"].get("display_name", "")
        location = display_name
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
    company_name = ""
    if job.get("company"):
        company_name = job["company"].get("display_name", "")
    return {
        "company_id": None,  # Adzuna postings don't always map to our company list
        "company_name": company_name,
        "title": title,
        "location": location,
        "description": description,
        "salary_min": float(salary_min) if salary_min else None,
        "salary_max": float(salary_max) if salary_max else None,
        "currency": "USD",
        "source": "adzuna",
        "source_id": source_id,
        "url": url,
        "posted_at": posted_at,
    }


def _strip_html(html: str) -> str:
    if not html:
        return ""
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean
