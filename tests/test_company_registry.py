from pathlib import Path

import pytest

from app.ingestion.company_registry import load_target_companies


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
