"""Tests for validated settings module.

Tests configuration validation including:
- Valid development and production configurations
- Required production settings
- Cookie security validation
- CORS and trusted host parsing
- Environment variable handling
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from settings import get_settings, reset_settings


PRODUCTION_ENV = {
    "APP_ENV": "production",
    "JWT_SECRET_KEY": "j" * 32,
    "CSRF_SECRET_KEY": "c" * 32,
    "DATABASE_URL": "postgresql+psycopg://user:pass@database/app",
    "COOKIE_SECURE": "true",
    "TRUSTED_HOSTS": "api.example.com",
    "ALLOWED_ORIGINS": "https://app.example.com",
    "CORS_ORIGINS": "https://app.example.com",
}


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset settings singleton before each test."""
    reset_settings()
    yield
    reset_settings()


class TestValidDevelopmentConfig:
    """Test valid development configuration."""

    def test_default_settings(self):
        """Default settings should be valid development config."""
        with patch.dict(os.environ, {"APP_ENV": "development"}):
            reset_settings()
            settings = get_settings()
            assert settings.app_env == "development"
            assert settings.app_name == "InfinityScan"

    def test_development_generates_ephemeral_distinct_secrets(self):
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            reset_settings()
            settings = get_settings()
        assert len(settings.secret_key.encode()) >= 32
        assert len(settings.csrf_secret_key.encode()) >= 32
        assert settings.secret_key != settings.csrf_secret_key

    def test_database_url_from_env(self):
        """DATABASE_URL should be read from environment."""
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///test.db"}):
            reset_settings()
            settings = get_settings()
            assert settings.database_url == "sqlite:///test.db"

    def test_cors_origins_parsing(self):
        """CORS_ORIGINS should be parsed as comma-separated list."""
        with patch.dict(os.environ, {"CORS_ORIGINS": "http://a.com, http://b.com"}):
            reset_settings()
            settings = get_settings()
            assert settings.cors_origins == ["http://a.com", "http://b.com"]

    def test_trusted_hosts_parsing(self):
        """TRUSTED_HOSTS should be parsed as comma-separated list."""
        with patch.dict(os.environ, {"TRUSTED_HOSTS": "example.com, *.example.com"}):
            reset_settings()
            settings = get_settings()
            assert settings.trusted_hosts == ["example.com", "*.example.com"]


class TestBooleanParsing:
    @pytest.mark.parametrize("value", ["2", "enabled", "truthy"])
    def test_unknown_boolean_values_are_rejected_without_echoing_value(self, value):
        with patch.dict(os.environ, {"COOKIE_SECURE": value}, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match="COOKIE_SECURE") as exc_info:
                get_settings()
        assert value not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1", True),
            ("TRUE", True),
            ("yes", True),
            ("on", True),
            ("0", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
        ],
    )
    def test_known_boolean_values_are_parsed(self, value, expected):
        with patch.dict(os.environ, {"COOKIE_SECURE": value}, clear=True):
            reset_settings()
            settings = get_settings()
        assert settings.cookie_secure is expected


class TestValidProductionConfig:
    """Test valid production configuration."""

    def test_production_with_secret(self):
        with patch.dict(os.environ, PRODUCTION_ENV, clear=True):
            reset_settings()
            settings = get_settings()
        assert settings.app_env == "production"
        assert settings.cookie_secure is True

    def test_production_with_secret_key_env(self):
        """SECRET_KEY remains a supported explicit JWT secret."""
        env = {**PRODUCTION_ENV, "SECRET_KEY": "s" * 32}
        env.pop("JWT_SECRET_KEY")
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            settings = get_settings()
        assert settings.secret_key == "s" * 32

    def test_explicit_empty_cors_selects_same_origin_topology(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "CORS_ORIGINS": ""},
            clear=True,
        ):
            reset_settings()
            settings = get_settings()
        assert settings.cors_origins == []

    def test_secret_length_is_measured_in_bytes(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "JWT_SECRET_KEY": "\u00e9" * 16},
            clear=True,
        ):
            reset_settings()
            settings = get_settings()
        assert len(settings.secret_key.encode()) == 32


class TestProductionRequirements:
    def test_production_without_secret_raises(self):
        env = PRODUCTION_ENV.copy()
        env.pop("JWT_SECRET_KEY")
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
                get_settings()

    def test_production_weak_jwt_secret_raises(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "JWT_SECRET_KEY": "short"},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
                get_settings()

    def test_production_missing_csrf_secret_raises(self):
        env = PRODUCTION_ENV.copy()
        env.pop("CSRF_SECRET_KEY")
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match="CSRF_SECRET_KEY"):
                get_settings()

    def test_production_weak_csrf_secret_raises(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "CSRF_SECRET_KEY": "short"},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="CSRF_SECRET_KEY"):
                get_settings()

    def test_production_secrets_must_be_distinct(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "CSRF_SECRET_KEY": PRODUCTION_ENV["JWT_SECRET_KEY"]},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="distinct"):
                get_settings()

    def test_production_cookie_secure_false_raises(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "COOKIE_SECURE": "false"},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="COOKIE_SECURE"):
                get_settings()

    def test_production_wildcard_cors_raises(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "CORS_ORIGINS": "https://*.example.com"},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="CORS_ORIGINS"):
                get_settings()

    def test_production_wildcard_allowed_origins_raises(self):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "ALLOWED_ORIGINS": "*"},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
                get_settings()

    @pytest.mark.parametrize(
        "name", ["TRUSTED_HOSTS", "ALLOWED_ORIGINS", "CORS_ORIGINS"]
    )
    def test_production_topology_variables_must_be_explicit(self, name):
        env = PRODUCTION_ENV.copy()
        env.pop(name)
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match=name):
                get_settings()

    @pytest.mark.parametrize("name", ["TRUSTED_HOSTS", "ALLOWED_ORIGINS"])
    def test_production_required_topology_values_must_not_be_empty(self, name):
        with patch.dict(os.environ, {**PRODUCTION_ENV, name: ""}, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match=name):
                get_settings()

    @pytest.mark.parametrize(
        "database_url",
        ["", "not-a-database-url", "sqlite:///production.db"],
    )
    def test_production_requires_postgresql_database_url(self, database_url):
        with patch.dict(
            os.environ,
            {**PRODUCTION_ENV, "DATABASE_URL": database_url},
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="DATABASE_URL") as exc_info:
                get_settings()
        if database_url:
            assert database_url not in str(exc_info.value)

    def test_staging_does_not_generate_secrets(self):
        with patch.dict(os.environ, {"APP_ENV": "staging"}, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
                get_settings()


class TestCookieValidation:
    """Test cookie security validation."""

    def test_samesite_none_requires_secure(self):
        """SameSite=None must require Secure=True."""
        with patch.dict(os.environ, {
            "COOKIE_SAME_SITE": "none",
            "COOKIE_SECURE": "false",
        }):
            reset_settings()
            with pytest.raises(ValueError, match="COOKIE_SECURE must be 'true'"):
                get_settings()

    def test_samesite_none_with_secure_ok(self):
        """SameSite=None with Secure=True should be valid."""
        with patch.dict(os.environ, {
            "COOKIE_SAME_SITE": "none",
            "COOKIE_SECURE": "true",
        }):
            reset_settings()
            settings = get_settings()
            assert settings.cookie_same_site == "none"
            assert settings.cookie_secure is True

    def test_invalid_samesite_raises(self):
        """Invalid SameSite value should raise."""
        with patch.dict(os.environ, {"COOKIE_SAME_SITE": "invalid"}):
            reset_settings()
            with pytest.raises(ValueError, match="COOKIE_SAME_SITE"):
                get_settings()


class TestInvalidEnvironment:
    """Test invalid environment configuration."""

    def test_invalid_app_env_raises(self):
        """Invalid APP_ENV should raise."""
        with patch.dict(os.environ, {"APP_ENV": "invalid_env"}):
            reset_settings()
            with pytest.raises(ValueError, match="APP_ENV"):
                get_settings()


class TestSettingsSingleton:
    """Test settings singleton behavior."""

    def test_singleton_returns_same_instance(self):
        """get_settings() should return the same instance."""
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_reset_settings_creates_new_instance(self):
        """reset_settings() should allow creating new instance."""
        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()
        assert settings1 is not settings2

    def test_settings_frozen(self):
        """Settings should be immutable."""
        settings = get_settings()
        with pytest.raises(AttributeError):
            settings.app_env = "modified"


class TestEnvironmentVariables:
    """Test environment variable handling."""

    def test_jwt_algorithm_default(self):
        """JWT_ALGORITHM should default to HS256."""
        settings = get_settings()
        assert settings.jwt_algorithm == "HS256"

    def test_access_token_ttl_default(self):
        """ACCESS_TOKEN_TTL_MINUTES should default to 30."""
        settings = get_settings()
        assert settings.access_token_ttl_minutes == 30

    def test_refresh_token_ttl_default(self):
        """REFRESH_TOKEN_TTL_DAYS should default to 30."""
        settings = get_settings()
        assert settings.refresh_token_ttl_days == 30

    def test_min_password_length_default(self):
        """MIN_PASSWORD_LENGTH should default to 8."""
        settings = get_settings()
        assert settings.min_password_length == 8

    def test_rate_limit_defaults(self):
        """Rate limits should have sensible defaults."""
        env = os.environ.copy()
        env.pop("REGISTER_RATE_LIMIT", None)
        env.pop("LOGIN_RATE_LIMIT", None)
        env.pop("CSRF_RATE_LIMIT", None)
        env.pop("REFRESH_RATE_LIMIT", None)
        env.pop("LOGOUT_RATE_LIMIT", None)
        env.pop("BOOKMARK_WRITE_RATE_LIMIT", None)
        env.pop("PROGRESS_WRITE_RATE_LIMIT", None)
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            settings = get_settings()
            assert settings.register_rate_limit == "5/minute"
            assert settings.login_rate_limit == "10/minute"
            assert settings.csrf_rate_limit == "30/minute"
            assert settings.refresh_rate_limit == "20/minute"
            assert settings.logout_rate_limit == "20/minute"
            assert settings.bookmark_write_rate_limit == "30/minute"
            assert settings.progress_write_rate_limit == "60/minute"

    def test_cors_methods_and_headers_defaults(self):
        """CORS methods and headers should have sensible defaults."""
        settings = get_settings()
        assert "GET" in settings.cors_allow_methods
        assert "POST" in settings.cors_allow_methods
        assert "content-type" in settings.cors_allow_headers
        assert "x-csrf-token" in settings.cors_allow_headers

    def test_max_body_bytes_default(self):
        """MAX_BODY_BYTES should default to 1MB."""
        settings = get_settings()
        assert settings.max_body_bytes == 1024 * 1024

    def test_max_body_bytes_from_env(self):
        """MAX_BODY_BYTES should be configurable."""
        with patch.dict(os.environ, {"MAX_BODY_BYTES": "2097152"}):
            reset_settings()
            settings = get_settings()
            assert settings.max_body_bytes == 2097152

    def test_allowed_origins_default(self):
        """ALLOWED_ORIGINS should have a default."""
        settings = get_settings()
        assert isinstance(settings.allowed_origins, list)
        assert len(settings.allowed_origins) > 0

    def test_allowed_origins_parsing(self):
        """ALLOWED_ORIGINS should be comma-separated list."""
        with patch.dict(os.environ, {"ALLOWED_ORIGINS": "http://a.com,http://b.com"}):
            reset_settings()
            settings = get_settings()
            assert settings.allowed_origins == ["http://a.com", "http://b.com"]

    def test_copymanga_api_default(self):
        """COPYMANGA_API should have default value."""
        settings = get_settings()
        assert settings.copymanga_api == "https://api.copymanga.tv"

    def test_jwt_issuer_audience_defaults(self):
        """JWT issuer and audience should have defaults."""
        settings = get_settings()
        assert settings.jwt_issuer == "infinityscan"
        assert settings.jwt_audience == "infinityscan"

    def test_cookie_domain_none_by_default(self):
        """COOKIE_DOMAIN should be None by default."""
        settings = get_settings()
        assert settings.cookie_domain is None


class TestObjectStorageSettings:
    """Test disabled-by-default and production storage validation."""

    def test_object_storage_is_disabled_by_default(self):
        env = os.environ.copy()
        env.pop("OBJECT_STORAGE_ENABLED", None)
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            settings = get_settings()
            assert settings.object_storage_enabled is False
            assert settings.s3_region == "auto"
            assert settings.s3_presign_ttl_seconds == 300
            assert settings.s3_max_retries == 4
            assert settings.integrity_max_concurrency == 8
            assert settings.integrity_inventory_page_size == 1000

    @pytest.mark.parametrize(
        ("name", "value", "message"),
        [
            ("S3_MAX_RETRIES", "11", "S3_MAX_RETRIES"),
            ("INTEGRITY_MAX_CONCURRENCY", "0", "INTEGRITY_MAX_CONCURRENCY"),
            ("INTEGRITY_INVENTORY_PAGE_SIZE", "1001", "INTEGRITY_INVENTORY_PAGE_SIZE"),
            ("INTEGRITY_ISSUE_LIMIT", "0", "INTEGRITY_ISSUE_LIMIT"),
            ("INTEGRITY_DELETE_MIN_AGE_SECONDS", "-1", "INTEGRITY_DELETE_MIN_AGE_SECONDS"),
        ],
    )
    def test_integrity_storage_bounds_are_validated(self, name, value, message):
        with patch.dict(os.environ, {name: value}):
            reset_settings()
            with pytest.raises(ValueError, match=message):
                get_settings()

    def test_enabled_development_storage_reads_configuration(self):
        with patch.dict(
            os.environ,
            {
                "OBJECT_STORAGE_ENABLED": "true",
                "S3_ENDPOINT_URL": "http://minio:9000",
                "S3_REGION": "us-east-1",
                "S3_BUCKET": "test-bucket",
                "S3_ACCESS_KEY_ID": "access",
                "S3_SECRET_ACCESS_KEY": "secret",
                "S3_FORCE_PATH_STYLE": "true",
            },
        ):
            reset_settings()
            settings = get_settings()
            assert settings.object_storage_enabled is True
            assert settings.s3_endpoint_url == "http://minio:9000"
            assert settings.s3_force_path_style is True

    def test_production_enabled_storage_requires_complete_settings(self):
        with patch.dict(
            os.environ,
            {
                **PRODUCTION_ENV,
                "OBJECT_STORAGE_ENABLED": "true",
                "S3_REGION": "auto",
                "S3_BUCKET": "prod-bucket",
                "S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
                "S3_ACCESS_KEY_ID": "access",
            },
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="S3_SECRET_ACCESS_KEY"):
                get_settings()

    def test_production_enabled_storage_requires_r2_auto_region(self):
        with patch.dict(
            os.environ,
            {
                **PRODUCTION_ENV,
                "OBJECT_STORAGE_ENABLED": "true",
                "S3_REGION": "us-east-1",
                "S3_BUCKET": "prod-bucket",
                "S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
                "S3_ACCESS_KEY_ID": "access",
                "S3_SECRET_ACCESS_KEY": "secret",
            },
            clear=True,
        ):
            reset_settings()
            with pytest.raises(ValueError, match="S3_REGION=auto"):
                get_settings()
