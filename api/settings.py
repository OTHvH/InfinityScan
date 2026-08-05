"""Validated application settings for InfinityScan.

All environment-variable-driven settings live here with runtime validation.
Secrets are never logged or exposed in repr. Settings are frozen after creation.

Key validation rules:
- APP_ENV must be 'development' | 'staging' | 'production'
- Deployed environments require explicit JWT and CSRF secrets
- COOKIE_SAME_SITE must be 'lax' | 'strict' | 'none'
- COOKIE_SECURE must be True when COOKIE_SAME_SITE is 'none'
- CORS_ORIGINS and TRUSTED_HOSTS are comma-separated lists
"""

from __future__ import annotations

import os
import secrets
import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from runtime_secrets import runtime_value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be a boolean")


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _database_query_value(name: str) -> str | None:
    raw_url = runtime_value("DATABASE_URL") or ""
    if not raw_url:
        return None
    try:
        value = make_url(raw_url).query.get(name)
    except (ArgumentError, TypeError, ValueError):
        return None
    if isinstance(value, tuple):
        return str(value[0]) if value else None
    return str(value) if value is not None else None


def _db_sslmode() -> str:
    configured = os.environ.get("DB_SSLMODE") or _database_query_value("sslmode")
    if configured:
        return configured.strip().lower()
    return "require" if os.environ.get("APP_ENV", "development") == "production" else "prefer"


def _validate_endpoint_url(name: str, value: str, *, require_https: bool) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain credentials, query, or fragment")
    if require_https and parsed.scheme != "https":
        raise ValueError(f"{name} must use HTTPS in production")
    if require_https:
        host = parsed.hostname.lower().rstrip(".")
        if host in {
            "localhost",
            "minio",
            "host.docker.internal",
            "gateway.docker.internal",
            "docker.for.mac.localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError(f"{name} must not use a local endpoint in production")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified):
            raise ValueError(f"{name} must not use a private endpoint in production")


def _secret(name: str, *aliases: str) -> str:
    for variable in (name, *aliases):
        value = runtime_value(variable)
        if value:
            return value
    if os.environ.get("APP_ENV", "development") == "development":
        return secrets.token_hex(32)
    return ""


@dataclass(frozen=True)
class Settings:
    """Immutable, validated application settings."""

    # ── Environment ─────────────────────────────────────────────────────
    app_env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "development"))
    app_name: str = "InfinityScan"
    app_version: str = "0.2.0"

    # ── Database ────────────────────────────────────────────────────────
    database_url: str = field(
        default_factory=lambda: runtime_value("DATABASE_URL") or "", repr=False
    )
    db_pool_size: int = field(default_factory=lambda: _int("DB_POOL_SIZE", 5))
    db_max_overflow: int = field(default_factory=lambda: _int("DB_MAX_OVERFLOW", 5))
    db_pool_timeout_seconds: float = field(
        default_factory=lambda: _float("DB_POOL_TIMEOUT_SECONDS", 10.0)
    )
    db_pool_recycle_seconds: int = field(
        default_factory=lambda: _int("DB_POOL_RECYCLE_SECONDS", 1800)
    )
    db_connect_timeout_seconds: int = field(
        default_factory=lambda: _int("DB_CONNECT_TIMEOUT_SECONDS", 10)
    )
    db_statement_timeout_ms: int = field(
        default_factory=lambda: _int("DB_STATEMENT_TIMEOUT_MS", 30000)
    )
    db_sslmode: str = field(default_factory=_db_sslmode)
    db_sslrootcert: str | None = field(
        default_factory=lambda: os.environ.get("DB_SSLROOTCERT") or None,
        repr=False,
    )

    # ── JWT / Sessions ──────────────────────────────────────────────────
    secret_key: str = field(
        default_factory=lambda: _secret("JWT_SECRET_KEY", "SECRET_KEY"),
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
        default_factory=lambda: _secret("CSRF_SECRET_KEY"),
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
    trusted_proxy_cidrs: list[str] = field(
        default_factory=lambda: _csv("TRUSTED_PROXY_CIDRS")
    )
    trust_forwarded_headers: bool = field(
        default_factory=lambda: _bool("TRUST_FORWARDED_HEADERS", False)
    )
    forwarded_header_max_length: int = field(
        default_factory=lambda: _int("FORWARDED_HEADER_MAX_LENGTH", 2048)
    )
    forwarded_max_hops: int = field(
        default_factory=lambda: _int("FORWARDED_MAX_HOPS", 5)
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
        default_factory=lambda: runtime_value("S3_ACCESS_KEY_ID") or "", repr=False
    )
    s3_secret_access_key: str = field(
        default_factory=lambda: runtime_value("S3_SECRET_ACCESS_KEY") or "", repr=False
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
    log_format: str = field(
        default_factory=lambda: os.environ.get(
            "LOG_FORMAT",
            "json" if os.environ.get("APP_ENV", "development") == "production" else "text",
        )
    )
    readiness_cache_seconds: float = field(
        default_factory=lambda: _float("READINESS_CACHE_SECONDS", 5.0)
    )


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
        raise ValueError("APP_ENV must be development, staging, or production")

    if s.app_env in ("staging", "production"):
        if not s.secret_key:
            raise ValueError("JWT_SECRET_KEY or SECRET_KEY must be explicitly set")
        if not s.csrf_secret_key:
            raise ValueError("CSRF_SECRET_KEY must be explicitly set")

    if s.app_env == "production":
        try:
            database_url = make_url(s.database_url)
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a parseable PostgreSQL URL") from None
        if database_url.get_backend_name() != "postgresql":
            raise ValueError("DATABASE_URL must be a parseable PostgreSQL URL")
        database_host = (database_url.host or "").lower().rstrip(".")
        if not database_host:
            raise ValueError("DATABASE_URL must include a database host")
        if database_host in {
            "localhost",
            "db",
            "postgres",
            "host.docker.internal",
            "gateway.docker.internal",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("DATABASE_URL must not use a local database host in production")
        try:
            database_address = ipaddress.ip_address(database_host)
        except ValueError:
            database_address = None
        if database_address is not None and (
            database_address.is_private
            or database_address.is_loopback
            or database_address.is_link_local
            or database_address.is_reserved
            or database_address.is_unspecified
        ):
            raise ValueError("DATABASE_URL must not use a private database host in production")

        if s.db_sslmode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError("DB_SSLMODE must require PostgreSQL TLS in production")
        if s.db_sslrootcert and not s.db_sslrootcert.strip():
            raise ValueError("DB_SSLROOTCERT must not be empty when supplied")

        if len(s.secret_key.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET_KEY or SECRET_KEY must contain at least 32 bytes")
        if len(s.csrf_secret_key.encode("utf-8")) < 32:
            raise ValueError("CSRF_SECRET_KEY must contain at least 32 bytes")
        if s.secret_key == s.csrf_secret_key:
            raise ValueError("JWT_SECRET_KEY and CSRF_SECRET_KEY must be distinct")

        if not s.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")

        topology_variables = ("TRUSTED_HOSTS", "ALLOWED_ORIGINS", "CORS_ORIGINS")
        missing = [name for name in topology_variables if name not in os.environ]
        if missing:
            raise ValueError(
                ", ".join(missing) + " must be explicitly set in production"
            )
        if not s.trusted_hosts:
            raise ValueError("TRUSTED_HOSTS must not be empty in production")
        if not s.allowed_origins:
            raise ValueError("ALLOWED_ORIGINS must not be empty in production")

        if any("*" in origin for origin in s.cors_origins):
            raise ValueError("CORS_ORIGINS must not contain wildcards in production")
        if any("*" in origin for origin in s.allowed_origins):
            raise ValueError("ALLOWED_ORIGINS must not contain wildcards in production")

        if "OBJECT_STORAGE_ENABLED" not in os.environ or not s.object_storage_enabled:
            raise ValueError("OBJECT_STORAGE_ENABLED must be true in production")

    if s.db_pool_size < 1 or s.db_pool_size > 100:
        raise ValueError("DB_POOL_SIZE must be between 1 and 100")
    if s.db_max_overflow < 0 or s.db_max_overflow > 100:
        raise ValueError("DB_MAX_OVERFLOW must be between 0 and 100")
    if s.db_pool_timeout_seconds <= 0:
        raise ValueError("DB_POOL_TIMEOUT_SECONDS must be positive")
    if s.db_pool_recycle_seconds < 0:
        raise ValueError("DB_POOL_RECYCLE_SECONDS must not be negative")
    if s.db_connect_timeout_seconds <= 0:
        raise ValueError("DB_CONNECT_TIMEOUT_SECONDS must be positive")
    if s.db_statement_timeout_ms <= 0:
        raise ValueError("DB_STATEMENT_TIMEOUT_MS must be positive")
    if s.db_sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("DB_SSLMODE is not a supported PostgreSQL sslmode")
    if s.log_level.upper() not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    if s.forwarded_header_max_length < 128 or s.forwarded_header_max_length > 16384:
        raise ValueError("FORWARDED_HEADER_MAX_LENGTH must be between 128 and 16384")
    if s.forwarded_max_hops < 1 or s.forwarded_max_hops > 20:
        raise ValueError("FORWARDED_MAX_HOPS must be between 1 and 20")
    if s.trust_forwarded_headers and not s.trusted_proxy_cidrs:
        raise ValueError("TRUSTED_PROXY_CIDRS is required when forwarded headers are trusted")
    for cidr in s.trusted_proxy_cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError("TRUSTED_PROXY_CIDRS contains an invalid CIDR") from None
    if s.log_format not in {"json", "text"}:
        raise ValueError("LOG_FORMAT must be json or text")
    if s.readiness_cache_seconds < 0 or s.readiness_cache_seconds > 60:
        raise ValueError("READINESS_CACHE_SECONDS must be between 0 and 60")

    if s.cookie_same_site not in ("lax", "strict", "none"):
        raise ValueError("COOKIE_SAME_SITE must be lax, strict, or none")
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
            if "S3_REGION" not in os.environ:
                raise ValueError("S3_REGION must be explicitly set in production")
        missing = [
            name for name, value in required.items()
            if not value or (isinstance(value, str) and not value.strip())
        ]
        if missing:
            raise ValueError(
                "Object storage is enabled but required settings are missing: "
                + ", ".join(missing)
            )
        if s.app_env == "production" and s.s3_endpoint_url:
            _validate_endpoint_url(
                "S3_ENDPOINT_URL",
                s.s3_endpoint_url,
                require_https=True,
            )
    if s.s3_public_url:
        _validate_endpoint_url(
            "S3_PUBLIC_URL",
            s.s3_public_url,
            require_https=s.app_env == "production",
        )


def reset_settings() -> None:
    """Reset the cached settings singleton.  Only for testing."""
    global _settings
    _settings = None
