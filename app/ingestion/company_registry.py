from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.models import MonitoredCompany

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "target_companies.yml"
SUPPORTED_SOURCES = {"greenhouse", "lever", "ashby"}
MAX_COMPANY_FIELD_LENGTH = 255


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses mappings with duplicate keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ValueError("target company config contains an invalid mapping key") from exc
        if duplicate:
            raise ValueError(f"duplicate YAML mapping key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _valid_company_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and len(value) <= MAX_COMPANY_FIELD_LENGTH
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def load_target_companies(path: Path = CONFIG_PATH) -> dict[str, list[dict]]:
    """Human-editable source of truth for which boards we monitor. Grow this
    file toward the project's 200-400 company target by adding entries here —
    verify each token actually returns postings before adding it; a bad token
    just shows up as a permanent 'http_error' and pollutes the health check."""
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f, Loader=_UniqueKeyLoader) or {}
    if not isinstance(data, dict):
        raise ValueError("target company config must be a mapping of sources to entries")

    validated: dict[str, list[dict]] = {}
    for source, entries in data.items():
        if source not in SUPPORTED_SOURCES:
            raise ValueError(f"unsupported target company source: {source}")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ValueError(f"target companies for {source} must be a list")

        seen_tokens: set[str] = set()
        validated_entries: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"target company entry for {source} must be a mapping")
            unexpected = set(entry) - {"token", "name"}
            if unexpected:
                fields = ", ".join(sorted(str(field) for field in unexpected))
                raise ValueError(
                    f"target company entry for {source} has unsupported fields: {fields}"
                )
            token = entry.get("token")
            if not _valid_company_text(token):
                raise ValueError(
                    f"target company token for {source} must be a trimmed, safe string "
                    f"of at most {MAX_COMPANY_FIELD_LENGTH} characters"
                )
            if token in seen_tokens:
                raise ValueError(f"duplicate target company token for {source}: {token}")
            name = entry.get("name")
            if name is not None and not _valid_company_text(name):
                raise ValueError(
                    f"target company name for {source}/{token} must be a trimmed, safe "
                    f"string of at most {MAX_COMPANY_FIELD_LENGTH} characters"
                )
            seen_tokens.add(token)
            validated_entries.append(entry)
        validated[source] = validated_entries
    return validated


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
            else:
                monitored.display_name = entry.get("name", token)
                if not monitored.is_active:
                    monitored.is_active = True
                    monitored.monitoring_started_at = now
                    monitored.monitoring_stopped_at = None

    for monitored in db.query(MonitoredCompany).filter_by(is_active=True).all():
        if (monitored.source, monitored.company_token) not in seen:
            monitored.is_active = False
            monitored.monitoring_stopped_at = now

    db.commit()
