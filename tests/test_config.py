import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_loads_default_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DB_POOL_SIZE=7\n", encoding="utf-8")

    configured = Settings()

    assert configured.db_pool_size == 7


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("db_pool_size", 0),
        ("db_max_overflow", -1),
        ("db_pool_recycle_seconds", 0),
        ("vector_ef_search", 0),
    ],
)
def test_settings_reject_invalid_runtime_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_settings_accept_documented_runtime_bounds():
    configured = Settings(
        _env_file=None,
        db_pool_size=1,
        db_max_overflow=0,
        db_pool_recycle_seconds=1,
        vector_ef_search=1,
    )

    assert configured.db_pool_size == 1
    assert configured.db_max_overflow == 0
    assert configured.db_pool_recycle_seconds == 1
    assert configured.vector_ef_search == 1
