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


def _bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "")
    if not val:
        return default
    return val.lower() in ("1", "true", "yes")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Settings:
    """Immutable, validated application settings."""

    # ── Environment ─────────────────────────────────────────────────────
    app_env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "development"))
    app_name: str = "InfinityScan"
    app_version: str = "0.2.0"

    # ── Database ────────────────────────────────────────────────────────
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", ""), repr=False
    )

    # ── JWT / Sessions ──────────────────────────────────────────────────
    secret_key: str = field(
        default_factory=lambda: os.environ.get("JWT_SECRET_KEY")
        or os.environ.get("SECRET_KEY")
        or secrets.token_hex(32),
        repr=False,
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
    cookie_path: str = field(default_factory=lambda: os.environ.get("COOKIE_PATH", "/"))

    # ── CSRF ────────────────────────────────────────────────────────────
    csrf_secret_key: str = field(
        default_factory=lambda: os.environ.get("CSRF_SECRET_KEY")
        or secrets.token_hex(32),
        repr=False,
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
            "accept,accept-language,content-type,content-length,authorization,x-csrf-token,x-request-id",
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
        default_factory=lambda: os.environ.get("COPYMANGA_TOKEN", ""), repr=False
    )

    # ── Provider adapter settings ─────────────────────────────────────
    copymanga_enabled: bool = field(
        default_factory=lambda: _bool("COPYMANGA_ENABLED", True)
    )
    copymanga_timeout: float = field(
        default_factory=lambda: _float("COPYMANGA_TIMEOUT", 15.0)
    )
    copymanga_max_response_bytes: int = field(
        default_factory=lambda: int(os.environ.get("COPYMANGA_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))
    )
    copymanga_max_redirects: int = field(
        default_factory=lambda: int(os.environ.get("COPYMANGA_MAX_REDIRECTS", "5"))
    )
    copymanga_cache_ttl: int = field(
        default_factory=lambda: int(os.environ.get("COPYMANGA_CACHE_TTL", "300"))
    )
    local_content_enabled: bool = field(
        default_factory=lambda: _bool("LOCAL_CONTENT_ENABLED", True)
    )
    local_content_root: str = field(
        default_factory=lambda: os.environ.get("LOCAL_CONTENT_ROOT", "")
    )
    provider_mirroring_enabled: bool = field(
        default_factory=lambda: _bool("PROVIDER_MIRRORING_ENABLED", False)
    )

    # ── S3 / CDN ────────────────────────────────────────────────────────
    s3_public_url: str = field(
        default_factory=lambda: os.environ.get("S3_PUBLIC_URL", "").rstrip("/")
    )

    # ── Object storage ──────────────────────────────────────────────────
    object_storage_enabled: bool = field(
        default_factory=lambda: _bool("OBJECT_STORAGE_ENABLED", False)
    )
    s3_endpoint_url: str | None = field(
        default_factory=lambda: os.environ.get("S3_ENDPOINT_URL") or None
    )
    s3_region: str = field(default_factory=lambda: os.environ.get("S3_REGION", "auto"))
    s3_bucket: str = field(default_factory=lambda: os.environ.get("S3_BUCKET", ""))
    s3_access_key_id: str = field(
        default_factory=lambda: os.environ.get("S3_ACCESS_KEY_ID", ""), repr=False
    )
    s3_secret_access_key: str = field(
        default_factory=lambda: os.environ.get("S3_SECRET_ACCESS_KEY", ""), repr=False
    )
    s3_presign_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("S3_PRESIGN_TTL_SECONDS", "300"))
    )
    s3_force_path_style: bool = field(
        default_factory=lambda: _bool("S3_FORCE_PATH_STYLE", False)
    )
    s3_connect_timeout: float = field(
        default_factory=lambda: _float("S3_CONNECT_TIMEOUT", 5.0)
    )
    s3_read_timeout: float = field(
        default_factory=lambda: _float("S3_READ_TIMEOUT", 30.0)
    )
    s3_max_retries: int = field(
        default_factory=lambda: int(os.environ.get("S3_MAX_RETRIES", "4"))
    )
    integrity_max_concurrency: int = field(
        default_factory=lambda: int(os.environ.get("INTEGRITY_MAX_CONCURRENCY", "8"))
    )
    integrity_inventory_page_size: int = field(
        default_factory=lambda: int(os.environ.get("INTEGRITY_INVENTORY_PAGE_SIZE", "1000"))
    )
    integrity_issue_limit: int = field(
        default_factory=lambda: int(os.environ.get("INTEGRITY_ISSUE_LIMIT", "1000"))
    )
    integrity_delete_min_age_seconds: int = field(
        default_factory=lambda: int(os.environ.get("INTEGRITY_DELETE_MIN_AGE_SECONDS", "86400"))
    )
    import_pending_stale_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IMPORT_PENDING_STALE_SECONDS", "300"))
    )
    import_scanning_stale_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IMPORT_SCANNING_STALE_SECONDS", "900"))
    )
    import_uploading_stale_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IMPORT_UPLOADING_STALE_SECONDS", "1800"))
    )
    import_verifying_stale_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IMPORT_VERIFYING_STALE_SECONDS", "900"))
    )
    import_lease_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IMPORT_LEASE_SECONDS", "120"))
    )
    import_recovery_batch_size: int = field(
        default_factory=lambda: int(os.environ.get("IMPORT_RECOVERY_BATCH_SIZE", "25"))
    )
    session_retention_days: int = field(
        default_factory=lambda: int(os.environ.get("SESSION_RETENTION_DAYS", "30"))
    )
    session_maintenance_batch_size: int = field(
        default_factory=lambda: int(os.environ.get("SESSION_MAINTENANCE_BATCH_SIZE", "500"))
    )
    audit_retention_days: int = field(
        default_factory=lambda: int(os.environ.get("AUDIT_RETENTION_DAYS", "365"))
    )
    audit_prune_batch_size: int = field(
        default_factory=lambda: int(os.environ.get("AUDIT_PRUNE_BATCH_SIZE", "500"))
    )
    storage_public_base_url: str | None = field(
        default_factory=lambda: os.environ.get("STORAGE_PUBLIC_BASE_URL") or None
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
    if not s.cookie_path.startswith("/"):
        raise ValueError("COOKIE_PATH must start with '/'")

    if s.cookie_same_site == "none" and not s.cookie_secure:
        raise ValueError(
            "COOKIE_SECURE must be 'true' when COOKIE_SAME_SITE is 'none'. "
            "Browsers reject SameSite=None cookies without Secure."
        )

    if s.s3_presign_ttl_seconds < 1 or s.s3_presign_ttl_seconds > 3600:
        raise ValueError("S3_PRESIGN_TTL_SECONDS must be between 1 and 3600 seconds")
    if s.s3_connect_timeout <= 0 or s.s3_read_timeout <= 0:
        raise ValueError("S3_CONNECT_TIMEOUT and S3_READ_TIMEOUT must be positive")
    if s.s3_max_retries < 0 or s.s3_max_retries > 10:
        raise ValueError("S3_MAX_RETRIES must be between 0 and 10")
    if s.integrity_max_concurrency < 1 or s.integrity_max_concurrency > 32:
        raise ValueError("INTEGRITY_MAX_CONCURRENCY must be between 1 and 32")
    if s.integrity_inventory_page_size < 1 or s.integrity_inventory_page_size > 1000:
        raise ValueError("INTEGRITY_INVENTORY_PAGE_SIZE must be between 1 and 1000")
    if s.integrity_issue_limit < 1 or s.integrity_issue_limit > 10000:
        raise ValueError("INTEGRITY_ISSUE_LIMIT must be between 1 and 10000")
    if s.integrity_delete_min_age_seconds < 0:
        raise ValueError("INTEGRITY_DELETE_MIN_AGE_SECONDS must not be negative")
    stale_thresholds = (
        s.import_pending_stale_seconds,
        s.import_scanning_stale_seconds,
        s.import_uploading_stale_seconds,
        s.import_verifying_stale_seconds,
    )
    if any(value < 1 for value in stale_thresholds):
        raise ValueError("Import stale thresholds must be positive")
    if s.import_lease_seconds < 1:
        raise ValueError("IMPORT_LEASE_SECONDS must be positive")
    if s.import_recovery_batch_size < 1 or s.import_recovery_batch_size > 1000:
        raise ValueError("IMPORT_RECOVERY_BATCH_SIZE must be between 1 and 1000")
    if s.session_retention_days < 0:
        raise ValueError("SESSION_RETENTION_DAYS must not be negative")
    if (
        s.session_maintenance_batch_size < 1
        or s.session_maintenance_batch_size > 1000
    ):
        raise ValueError("SESSION_MAINTENANCE_BATCH_SIZE must be between 1 and 1000")
    if s.audit_retention_days < 1:
        raise ValueError("AUDIT_RETENTION_DAYS must be positive")
    if s.audit_prune_batch_size < 1 or s.audit_prune_batch_size > 1000:
        raise ValueError("AUDIT_PRUNE_BATCH_SIZE must be between 1 and 1000")

    if s.object_storage_enabled:
        required = {
            "S3_REGION": s.s3_region,
            "S3_BUCKET": s.s3_bucket,
            "S3_ACCESS_KEY_ID": s.s3_access_key_id,
            "S3_SECRET_ACCESS_KEY": s.s3_secret_access_key,
        }
        if s.app_env == "production":
            required["S3_ENDPOINT_URL"] = s.s3_endpoint_url or ""
            if s.s3_region != "auto":
                raise ValueError("Production R2 storage requires S3_REGION=auto")
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "Object storage is enabled but required settings are missing: "
                + ", ".join(missing)
            )


def reset_settings() -> None:
    """Reset the cached settings singleton.  Only for testing."""
    global _settings
    _settings = None
