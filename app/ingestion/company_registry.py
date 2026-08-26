from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.models import MonitoredCompany

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "target_companies.yml"


def load_target_companies(path: Path = CONFIG_PATH) -> dict[str, list[dict]]:
    """Human-editable source of truth for which boards we monitor. Grow this
    file toward the project's 200-400 company target by adding entries here —
    verify each token actually returns postings before adding it; a bad token
    just shows up as a permanent 'http_error' and pollutes the health check."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {source: (entries or []) for source, entries in data.items()}


def sync_monitored_companies(db: Session, target_companies: dict[str, list[dict]]) -> None:
    """Sync target_companies.yml into monitored_companies, recording
    monitoring_started_at for anything new. This timestamp is what
    postings.left_truncated is computed against — a company added mid-project
    has postings of unknown true age, and this is how that gets flagged
    instead of silently mislabeled as freshly posted."""
    now = datetime.now(timezone.utc)
    seen: set[tuple[str, str]] = set()

    for source, entries in target_companies.items():
        for entry in entries:
            token = entry["token"]
            seen.add((source, token))
            monitored = (
                db.query(MonitoredCompany)
                .filter_by(source=source, company_token=token)
                .first()
            )
            if monitored is None:
                db.add(
                    MonitoredCompany(
                        source=source,
                        company_token=token,
                        display_name=entry.get("name", token),
                        monitoring_started_at=now,
                        is_active=True,
                    )
                )
            elif not monitored.is_active:
                monitored.is_active = True
                monitored.monitoring_stopped_at = None

    for monitored in db.query(MonitoredCompany).filter_by(is_active=True).all():
        if (monitored.source, monitored.company_token) not in seen:
            monitored.is_active = False
            monitored.monitoring_stopped_at = now

    db.commit()
