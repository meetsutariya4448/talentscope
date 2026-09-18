from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.ingestion.company_registry import load_target_companies, sync_monitored_companies


def _write_config(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "targets.yml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_target_companies_accepts_valid_entries(tmp_path):
    path = _write_config(
        tmp_path,
        "greenhouse:\n  - {token: acme, name: Acme}\nlever: []\n",
    )

    assert load_target_companies(path) == {
        "greenhouse": [{"token": "acme", "name": "Acme"}],
        "lever": [],
    }


@pytest.mark.parametrize(
    "contents",
    [
        "- greenhouse\n",
        "unknown:\n  - {token: acme}\n",
        "greenhouse: {token: acme}\n",
        "greenhouse:\n  - {name: Acme}\n",
        "greenhouse:\n  - {token: ' acme'}\n",
        "greenhouse:\n  - {token: acme}\n  - {token: acme}\n",
        "greenhouse:\n  - {token: acme, name: ''}\n",
        "greenhouse:\n  - {token: acme, name: ' Acme'}\n",
        "greenhouse:\n  - {token: acme, name: 'Acme '}\n",
        "greenhouse:\n  - {token: acme, company_name: Acme}\n",
    ],
)
def test_load_target_companies_rejects_unsafe_config(contents, tmp_path):
    with pytest.raises(ValueError):
        load_target_companies(_write_config(tmp_path, contents))


def test_sync_updates_existing_company_display_name():
    monitored = MagicMock(
        source="greenhouse",
        company_token="acme",
        display_name="Old Acme",
        is_active=True,
    )
    db = MagicMock()
    query = db.query.return_value
    query.filter_by.return_value.first.return_value = monitored
    query.filter_by.return_value.all.return_value = [monitored]

    sync_monitored_companies(
        db,
        {"greenhouse": [{"token": "acme", "name": "Acme Corporation"}]},
    )

    assert monitored.display_name == "Acme Corporation"
    db.commit.assert_called_once_with()


def test_sync_restarts_monitoring_window_when_company_is_reactivated():
    old_start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    stopped_at = datetime(2025, 2, 1, tzinfo=timezone.utc)
    monitored = MagicMock(
        source="lever",
        company_token="acme",
        display_name="Acme",
        monitoring_started_at=old_start,
        monitoring_stopped_at=stopped_at,
        is_active=False,
    )
    db = MagicMock()
    query = db.query.return_value
    query.filter_by.return_value.first.return_value = monitored
    query.filter_by.return_value.all.return_value = []

    sync_monitored_companies(db, {"lever": [{"token": "acme", "name": "Acme"}]})

    assert monitored.is_active is True
    assert monitored.monitoring_started_at > old_start
    assert monitored.monitoring_stopped_at is None
    db.commit.assert_called_once_with()
