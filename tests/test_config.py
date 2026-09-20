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


def test_settings_normalizes_and_validates_log_level():
    assert Settings(_env_file=None, log_level=" warning ").log_level == "WARNING"

    with pytest.raises(ValidationError, match="log level"):
        Settings(_env_file=None, log_level="verbose")


@pytest.mark.parametrize("value", ["300/m", " 50/S ", "1/h", ""])
def test_settings_accepts_documented_embed_rate_limits(value):
    configured = Settings(_env_file=None, embed_rate_limit=value)

    assert configured.embed_rate_limit == value.strip().lower()


@pytest.mark.parametrize("value", ["0/s", "fast", "10/day", "1.5/m"])
def test_settings_rejects_invalid_embed_rate_limits(value):
    with pytest.raises(ValidationError, match="embed rate limit"):
        Settings(_env_file=None, embed_rate_limit=value)


def test_settings_normalizes_and_rejects_blank_groq_model():
    configured = Settings(_env_file=None, groq_model=" openai/gpt-oss-20b ")

    assert configured.groq_model == "openai/gpt-oss-20b"

    with pytest.raises(ValidationError, match="Groq model must not be blank"):
        Settings(_env_file=None, groq_model="   ")
