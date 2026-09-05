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
