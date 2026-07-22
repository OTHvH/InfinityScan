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

from settings import Settings, get_settings, reset_settings, _validate_settings


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


class TestValidProductionConfig:
    """Test valid production configuration."""

    def test_production_with_secret(self):
        """Production should be valid with explicit secret."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "JWT_SECRET_KEY": "a-very-long-secret-key-for-production",
            "CSRF_SECRET_KEY": "a-separate-csrf-secret-key-for-production",
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
            "COOKIE_SECURE": "true",
            "COOKIE_SAME_SITE": "lax",
        }):
            reset_settings()
            settings = get_settings()
            assert settings.app_env == "production"
            assert settings.cookie_secure is True

    def test_production_with_secret_key_env(self):
        """Production should accept SECRET_KEY as alternative env var."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "SECRET_KEY": "a" * 64,
            "CSRF_SECRET_KEY": "c" * 64,
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
            "COOKIE_SECURE": "true",
        }):
            reset_settings()
            settings = get_settings()
            assert settings.app_env == "production"


class TestProductionSecretRequired:
    """Test that production requires explicit secret key."""

    def test_production_without_secret_raises(self):
        """Production without JWT_SECRET_KEY or SECRET_KEY should raise."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
        }, clear=False):
            # Remove both secret key env vars if present
            env = os.environ.copy()
            env.pop("JWT_SECRET_KEY", None)
            env.pop("SECRET_KEY", None)
            with patch.dict(os.environ, env, clear=True):
                reset_settings()
                with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
                    get_settings()

    def test_production_weak_jwt_secret_raises(self):
        """Production with JWT secret < 32 bytes should raise."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "JWT_SECRET_KEY": "short",
            "CSRF_SECRET_KEY": "a" * 64,
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
            "COOKIE_SECURE": "true",
        }):
            reset_settings()
            with pytest.raises(ValueError, match="too weak"):
                get_settings()

    def test_production_missing_csrf_secret_raises(self):
        """Production without CSRF_SECRET_KEY should raise."""
        env = os.environ.copy()
        env["APP_ENV"] = "production"
        env["JWT_SECRET_KEY"] = "a" * 64
        env["DATABASE_URL"] = "postgresql+psycopg://user:pass@host/db"
        env["COOKIE_SECURE"] = "true"
        env.pop("CSRF_SECRET_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            reset_settings()
            with pytest.raises(ValueError, match="CSRF_SECRET_KEY"):
                get_settings()

    def test_production_cookie_secure_false_raises(self):
        """Production with COOKIE_SECURE=false should raise."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "JWT_SECRET_KEY": "a" * 64,
            "CSRF_SECRET_KEY": "a" * 64,
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
            "COOKIE_SECURE": "false",
        }):
            reset_settings()
            with pytest.raises(ValueError, match="COOKIE_SECURE"):
                get_settings()

    def test_production_wildcard_cors_raises(self):
        """Production with wildcard CORS origins should raise."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "JWT_SECRET_KEY": "a" * 64,
            "CSRF_SECRET_KEY": "a" * 64,
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
            "COOKIE_SECURE": "true",
            "CORS_ORIGINS": "*",
        }):
            reset_settings()
            with pytest.raises(ValueError, match="Wildcard.*CORS"):
                get_settings()

    def test_production_wildcard_allowed_origins_raises(self):
        """Production with wildcard ALLOWED_ORIGINS should raise."""
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "JWT_SECRET_KEY": "a" * 64,
            "CSRF_SECRET_KEY": "a" * 64,
            "DATABASE_URL": "postgresql+psycopg://user:pass@host/db",
            "COOKIE_SECURE": "true",
            "ALLOWED_ORIGINS": "*",
        }):
            reset_settings()
            with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
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
                "APP_ENV": "production",
                "JWT_SECRET_KEY": "a" * 64,
                "CSRF_SECRET_KEY": "b" * 64,
                "COOKIE_SECURE": "true",
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
                "APP_ENV": "production",
                "JWT_SECRET_KEY": "a" * 64,
                "CSRF_SECRET_KEY": "b" * 64,
                "COOKIE_SECURE": "true",
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
