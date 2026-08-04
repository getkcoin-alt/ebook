"""Settings parsing, especially from environment variables.

These tests exist because of a production incident: every other test constructs
`Settings(...)` in Python, which bypasses `EnvSettingsSource` entirely. The first
real deploy crash-looped on `CORS_ORIGINS=http://localhost:3000` — pydantic-settings
ran `json.loads` on it before any validator, and a bare URL is not JSON.

Anything read from the environment gets a test here that goes *through* the
environment.
"""

from __future__ import annotations

import pytest

from knowledgeos_core import ServiceSettings


def _settings(monkeypatch, **env: str) -> ServiceSettings:
    """Build settings strictly from environment variables."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Ignore any .env in the working directory so the test is hermetic.
    return ServiceSettings(_env_file=None)  # type: ignore[call-arg]


class TestCsvListFromEnvironment:
    def test_single_bare_value(self, monkeypatch):
        # The exact value that crash-looped the first production deploy.
        settings = _settings(monkeypatch, CORS_ORIGINS="http://localhost:3000")
        assert settings.cors_origins == ["http://localhost:3000"]

    def test_comma_separated(self, monkeypatch):
        settings = _settings(monkeypatch, CORS_ORIGINS="https://a.com,https://b.com,https://c.com")
        assert settings.cors_origins == ["https://a.com", "https://b.com", "https://c.com"]

    def test_whitespace_is_trimmed(self, monkeypatch):
        settings = _settings(monkeypatch, CORS_ORIGINS=" https://a.com , https://b.com ")
        assert settings.cors_origins == ["https://a.com", "https://b.com"]

    def test_json_array_still_accepted(self, monkeypatch):
        # A committed .env may well use JSON; both forms must work.
        settings = _settings(monkeypatch, CORS_ORIGINS='["https://a.com","https://b.com"]')
        assert settings.cors_origins == ["https://a.com", "https://b.com"]

    def test_empty_string_is_empty_list(self, monkeypatch):
        assert _settings(monkeypatch, CORS_ORIGINS="").cors_origins == []

    def test_malformed_json_falls_back_to_csv(self, monkeypatch):
        # A stray bracket should not stop a service from booting.
        settings = _settings(monkeypatch, CORS_ORIGINS="[https://a.com")
        assert settings.cors_origins == ["[https://a.com"]

    def test_trailing_comma_ignored(self, monkeypatch):
        assert _settings(monkeypatch, TRUSTED_HOSTS="a.com,b.com,").trusted_hosts == [
            "a.com",
            "b.com",
        ]

    def test_wildcard_host(self, monkeypatch):
        assert _settings(monkeypatch, TRUSTED_HOSTS="*").trusted_hosts == ["*"]

    def test_default_applies_when_unset(self, monkeypatch):
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        assert _settings(monkeypatch).cors_origins == ["http://localhost:3000"]


class TestDatabaseUrlFromEnvironment:
    @pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
    def test_scheme_is_rewritten_for_asyncpg(self, monkeypatch, scheme):
        # Railway and Heroku both hand out `postgres://`, which SQLAlchemy's async
        # engine cannot use.
        settings = _settings(monkeypatch, DATABASE_URL=f"{scheme}://u:p@host.internal:5432/db")
        assert settings.database_url == "postgresql+asyncpg://u:p@host.internal:5432/db"

    def test_sync_url_uses_a_blocking_driver(self, monkeypatch):
        settings = _settings(monkeypatch, DATABASE_URL="postgres://u:p@h:5432/db")
        # Alembic and Celery beat cannot use asyncpg.
        assert settings.sync_database_url == "postgresql+psycopg://u:p@h:5432/db"

    def test_password_with_special_characters_survives(self, monkeypatch):
        settings = _settings(monkeypatch, DATABASE_URL="postgres://u:a%2Fb%40c@h:5432/db")
        assert "a%2Fb%40c" in (settings.database_url or "")

    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert _settings(monkeypatch).database_url is None


class TestEnvironmentFlags:
    def test_production_closes_docs(self, monkeypatch):
        settings = _settings(monkeypatch, ENVIRONMENT="production")
        assert settings.is_production is True
        # Swagger must not be public in production; the gateway serves an
        # aggregated spec instead.
        assert settings.docs_enabled is False

    @pytest.mark.parametrize("env", ["local", "test", "staging"])
    def test_non_production_opens_docs(self, monkeypatch, env):
        settings = _settings(monkeypatch, ENVIRONMENT=env)
        assert settings.is_production is False
        assert settings.docs_enabled is True

    def test_numeric_and_boolean_coercion(self, monkeypatch):
        settings = _settings(monkeypatch, PORT="9000", DEBUG="true", DB_POOL_SIZE="42")
        assert settings.port == 9000
        assert settings.debug is True
        assert settings.db_pool_size == 42

    def test_unknown_variables_are_ignored(self, monkeypatch):
        # extra="ignore": Railway injects many RAILWAY_* variables, and an
        # unrecognised one must never stop a service from booting.
        settings = _settings(monkeypatch, RAILWAY_PRIVATE_DOMAIN="auth.railway.internal")
        assert settings.service_name  # constructed fine
