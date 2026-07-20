"""Validated application settings for InfinityScan.

All environment-variable-driven settings live here with runtime validation.
Secrets are never logged or exposed in repr. Settings are frozen after creation.

Key validation rules:
- APP_ENV must be 'development' | 'staging' | 'production'
- JWT_SECRET_KEY is required in production
- COOKIE_SAME_SITE must be 'lax' | 'strict' | 'none'
- COOKIE_SECURE must be True when COOKIE_SAME_SITE is 'none'
- CORS_ORIGINS and TRUSTED_HOSTS are comma-separated lists
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import ClassVar


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable, validated application settings."""

    # ── Environment ─────────────────────────────────────────────────────
    app_env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "development"))
    app_name: str = "InfinityScan"
    app_version: str = "0.2.0"

    # ── Database ────────────────────────────────────────────────────────
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

    # ── JWT / Sessions ──────────────────────────────────────────────────
    secret_key: str = field(
        default_factory=lambda: os.environ.get("JWT_SECRET_KEY")
        or os.environ.get("SECRET_KEY")
        or secrets.token_hex(32)
    )
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = field(default_factory=lambda: os.environ.get("JWT_ISSUER", "infinityscan"))
    jwt_audience: str = field(default_factory=lambda: os.environ.get("JWT_AUDIENCE", "infinityscan"))

    # Access token lifetime (minutes)
    access_token_ttl_minutes: int = int(os.environ.get("ACCESS_TOKEN_TTL_MINUTES", "30"))
    # Refresh token lifetime (days)
    refresh_token_ttl_days: int = int(os.environ.get("REFRESH_TOKEN_TTL_DAYS", "30"))

    # ── Cookie names ────────────────────────────────────────────────────
    access_cookie_name: str = "is_access"
    refresh_cookie_name: str = "is_refresh"
    csrf_cookie_name: str = "is_csrf"

    # ── Cookie security ─────────────────────────────────────────────────
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))
    cookie_same_site: str = field(default_factory=lambda: os.environ.get("COOKIE_SAME_SITE", "lax"))
    cookie_domain: str | None = field(
        default_factory=lambda: os.environ.get("COOKIE_DOMAIN") or None
    )
    cookie_path: str = "/"

    # ── CSRF ────────────────────────────────────────────────────────────
    csrf_secret_key: str = field(
        default_factory=lambda: os.environ.get("CSRF_SECRET_KEY")
        or secrets.token_hex(32)
    )
    csrf_header_name: str = "x-csrf-token"
    csrf_token_ttl_seconds: int = int(os.environ.get("CSRF_TOKEN_TTL_SECONDS", "3600"))

    # ── Rate limits ─────────────────────────────────────────────────────
    register_rate_limit: str = field(
        default_factory=lambda: os.environ.get("REGISTER_RATE_LIMIT", "5/minute")
    )
    login_rate_limit: str = field(
        default_factory=lambda: os.environ.get("LOGIN_RATE_LIMIT", "10/minute")
    )
    csrf_rate_limit: str = field(
        default_factory=lambda: os.environ.get("CSRF_RATE_LIMIT", "30/minute")
    )
    refresh_rate_limit: str = field(
        default_factory=lambda: os.environ.get("REFRESH_RATE_LIMIT", "20/minute")
    )
    logout_rate_limit: str = field(
        default_factory=lambda: os.environ.get("LOGOUT_RATE_LIMIT", "20/minute")
    )
    bookmark_write_rate_limit: str = field(
        default_factory=lambda: os.environ.get("BOOKMARK_WRITE_RATE_LIMIT", "30/minute")
    )
    progress_write_rate_limit: str = field(
        default_factory=lambda: os.environ.get("PROGRESS_WRITE_RATE_LIMIT", "60/minute")
    )

    # ── Password policy ─────────────────────────────────────────────────
    min_password_length: int = int(os.environ.get("MIN_PASSWORD_LENGTH", "8"))
    max_password_length: int = int(os.environ.get("MAX_PASSWORD_LENGTH", "128"))

    # ── CORS ────────────────────────────────────────────────────────────
    cors_origins: list[str] = field(
        default_factory=lambda: _csv("CORS_ORIGINS", "http://localhost:3000")
    )
    cors_allow_methods: list[str] = field(
        default_factory=lambda: _csv("CORS_ALLOW_METHODS", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
    )
    cors_allow_headers: list[str] = field(
        default_factory=lambda: _csv(
            "CORS_ALLOW_HEADERS",
            "accept,accept-language,content-type,content-length,authorization,x-csrf-token",
        )
    )

    # ── Trusted hosts ───────────────────────────────────────────────────
    trusted_hosts: list[str] = field(
        default_factory=lambda: _csv("TRUSTED_HOSTS", "localhost,127.0.0.1")
    )

    # ── Request body limit ──────────────────────────────────────────────
    max_body_bytes: int = field(
        default_factory=lambda: int(os.environ.get("MAX_BODY_BYTES", str(1024 * 1024)))
    )

    # ── Origin validation ───────────────────────────────────────────────
    allowed_origins: list[str] = field(
        default_factory=lambda: _csv("ALLOWED_ORIGINS", "http://localhost:3000")
    )

    # ── CopyManga upstream ──────────────────────────────────────────────
    copymanga_api: str = field(
        default_factory=lambda: os.environ.get("COPYMANGA_API", "https://api.copymanga.tv").rstrip("/")
    )
    copymanga_token: str = field(
        default_factory=lambda: os.environ.get("COPYMANGA_TOKEN", "")
    )

    # ── S3 / CDN ────────────────────────────────────────────────────────
    s3_public_url: str = field(
        default_factory=lambda: os.environ.get("S3_PUBLIC_URL", "").rstrip("/")
    )

    # ── Logging ─────────────────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _validate_settings(_settings)
    return _settings


def _validate_settings(s: Settings) -> None:
    """Run-time validation of settings that cannot be enforced by type hints."""
    if s.app_env not in ("development", "staging", "production"):
        raise ValueError(f"APP_ENV must be 'development', 'staging', or 'production', got '{s.app_env}'")

    if s.app_env == "production":
        # In production, SECRET_KEY must be explicitly set (not auto-generated)
        if not os.environ.get("JWT_SECRET_KEY") and not os.environ.get("SECRET_KEY"):
            raise ValueError(
                "JWT_SECRET_KEY (or SECRET_KEY) must be explicitly set in production. "
                "Auto-generated keys are not allowed in production."
            )
        if not os.environ.get("CSRF_SECRET_KEY"):
            raise ValueError(
                "CSRF_SECRET_KEY must be explicitly set in production. "
                "Auto-generated keys are not allowed in production."
            )

        # Production: reject weak JWT secret (< 32 bytes hex = 64 chars)
        if len(s.secret_key) < 32:
            raise ValueError(
                "JWT secret key is too weak for production. "
                "Use at least 32 bytes (64 hex characters)."
            )

        # Production: cookies MUST be Secure
        if not s.cookie_secure:
            raise ValueError(
                "COOKIE_SECURE must be 'true' in production. "
                "Browsers reject non-secure cookies in HTTPS contexts."
            )

        # Production: reject wildcard CORS origins with credentials
        if "*" in s.cors_origins:
            raise ValueError(
                "Wildcard '*' CORS origins are not allowed in production with credentials. "
                "Use explicit origin domains."
            )

        # Production: reject wildcard in allowed_origins
        if "*" in s.allowed_origins:
            raise ValueError(
                "Wildcard '*' is not allowed in ALLOWED_ORIGINS in production."
            )

    if s.cookie_same_site not in ("lax", "strict", "none"):
        raise ValueError(
            f"COOKIE_SAME_SITE must be 'lax', 'strict', or 'none', got '{s.cookie_same_site}'"
        )

    if s.cookie_same_site == "none" and not s.cookie_secure:
        raise ValueError(
            "COOKIE_SECURE must be 'true' when COOKIE_SAME_SITE is 'none'. "
            "Browsers reject SameSite=None cookies without Secure."
        )


def reset_settings() -> None:
    """Reset the cached settings singleton.  Only for testing."""
    global _settings
    _settings = None