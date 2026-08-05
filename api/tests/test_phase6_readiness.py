"""Provider-neutral production readiness, proxy, logging, and header tests."""

from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import database
import main
from logging_config import JsonFormatter, redact_text, redact_value
from proxy import resolve_client_ip, resolve_forwarded_scheme
from settings import get_settings, reset_settings


PRODUCTION_ENV = {
    "APP_ENV": "production",
    "JWT_SECRET_KEY": "j" * 32,
    "CSRF_SECRET_KEY": "c" * 32,
    "DATABASE_URL": "postgresql+psycopg://user:p%40ss@database.example/app",
    "COOKIE_SECURE": "true",
    "TRUSTED_HOSTS": "api",
    "ALLOWED_ORIGINS": "https://app.example.com",
    "CORS_ORIGINS": "",
    "OBJECT_STORAGE_ENABLED": "true",
    "S3_ENDPOINT_URL": "https://account.r2.cloudflarestorage.com",
    "S3_REGION": "auto",
    "S3_BUCKET": "application-bucket",
    "S3_ACCESS_KEY_ID": "access",
    "S3_SECRET_ACCESS_KEY": "secret",
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    reset_settings()
    main._readiness_cache = None
    yield
    reset_settings()
    main._readiness_cache = None


def test_pool_options_are_bounded_and_preserve_encoded_password():
    cfg = SimpleNamespace(
        db_pool_size=5,
        db_max_overflow=5,
        db_pool_timeout_seconds=10.0,
        db_pool_recycle_seconds=1800,
        db_connect_timeout_seconds=10,
        db_statement_timeout_ms=30000,
        db_sslmode="verify-full",
        db_sslrootcert="/etc/ssl/certs/provider-ca.pem",
    )
    options = database._engine_options(
        "postgresql+psycopg://user:p%40ss@database.example/app",
        cfg,
    )
    assert options["pool_pre_ping"] is True
    assert options["pool_size"] == 5
    assert options["max_overflow"] == 5
    assert options["pool_timeout"] == 10.0
    assert options["pool_recycle"] == 1800
    assert options["connect_args"] == {
        "connect_timeout": 10,
        "sslmode": "verify-full",
        "options": "-c statement_timeout=30000",
        "sslrootcert": "/etc/ssl/certs/provider-ca.pem",
    }


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer"])
def test_production_rejects_non_tls_database_modes(sslmode):
    with patch.dict(os.environ, {**PRODUCTION_ENV, "DB_SSLMODE": sslmode}, clear=True):
        with pytest.raises(ValueError, match="DB_SSLMODE"):
            get_settings()


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_production_accepts_tls_database_modes(sslmode):
    with patch.dict(os.environ, {**PRODUCTION_ENV, "DB_SSLMODE": sslmode}, clear=True):
        assert get_settings().db_sslmode == sslmode


def test_production_rejects_private_database_host():
    with patch.dict(
        os.environ,
        {**PRODUCTION_ENV, "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1/app"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="local database host|private database host"):
            get_settings()


def test_production_rejects_development_database_host():
    with patch.dict(
        os.environ,
        {**PRODUCTION_ENV, "DATABASE_URL": "postgresql+psycopg://u:p@host.docker.internal/app"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="local database host"):
            get_settings()


def test_production_requires_object_storage():
    env = {key: value for key, value in PRODUCTION_ENV.items() if key != "OBJECT_STORAGE_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="OBJECT_STORAGE_ENABLED"):
            get_settings()


def test_production_requires_explicit_storage_region():
    env = {key: value for key, value in PRODUCTION_ENV.items() if key != "S3_REGION"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="S3_REGION"):
            get_settings()


def test_production_rejects_insecure_storage_endpoint():
    with patch.dict(
        os.environ,
        {**PRODUCTION_ENV, "S3_ENDPOINT_URL": "http://minio:9000"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="S3_ENDPOINT_URL"):
            get_settings()


def test_production_accepts_provider_neutral_https_storage_region():
    with patch.dict(
        os.environ,
        {**PRODUCTION_ENV, "S3_ENDPOINT_URL": "https://s3.example", "S3_REGION": "eu-west-1"},
        clear=True,
    ):
        assert get_settings().s3_region == "eu-west-1"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@account.r2.cloudflarestorage.com",
        "https://account.r2.cloudflarestorage.com?bucket=secret",
        "https://127.0.0.1:9000",
    ],
)
def test_production_rejects_unsafe_storage_endpoint_forms(endpoint):
    with patch.dict(os.environ, {**PRODUCTION_ENV, "S3_ENDPOINT_URL": endpoint}, clear=True):
        with pytest.raises(ValueError, match="S3_ENDPOINT_URL"):
            get_settings()


def test_invalid_trusted_proxy_cidr_fails_startup():
    with patch.dict(
        os.environ,
        {**PRODUCTION_ENV, "TRUST_FORWARDED_HEADERS": "true", "TRUSTED_PROXY_CIDRS": "not-a-cidr"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="TRUSTED_PROXY_CIDRS"):
            get_settings()


def _proxy_settings(**values):
    return SimpleNamespace(
        trust_forwarded_headers=True,
        trusted_proxy_cidrs=["10.0.0.0/8", "2001:db8::/32"],
        forwarded_header_max_length=2048,
        forwarded_max_hops=5,
        **values,
    )


def test_direct_forwarded_header_spoof_is_ignored():
    settings = _proxy_settings()
    headers = {b"x-forwarded-for": b"203.0.113.9"}
    assert resolve_client_ip("198.51.100.10", headers, settings) == "198.51.100.10"


def test_trusted_multi_hop_forwarding_selects_first_untrusted_address():
    settings = _proxy_settings()
    headers = {b"x-forwarded-for": b"203.0.113.9, 10.1.0.2"}
    assert resolve_client_ip("10.1.0.3", headers, settings) == "203.0.113.9"
    assert resolve_forwarded_scheme("10.1.0.3", {b"x-forwarded-proto": b"https"}, settings) == "https"


@pytest.mark.parametrize("address", ["not-an-ip", "203.0.113.9, not-an-ip"])
def test_malformed_forwarding_header_is_ignored(address):
    settings = _proxy_settings()
    assert resolve_client_ip("10.1.0.3", {b"x-forwarded-for": address.encode()}, settings) == "10.1.0.3"


def test_livez_does_not_call_dependencies(monkeypatch):
    monkeypatch.setattr(main, "is_database_ready", lambda: pytest.fail("database queried"))
    monkeypatch.setattr(main, "is_database_revision_ready", lambda: pytest.fail("migration queried"))
    assert main.livez() == {"status": "alive"}


def test_readyz_reports_all_healthy_dependencies(monkeypatch):
    monkeypatch.setattr(main, "_cfg", SimpleNamespace(object_storage_enabled=True, readiness_cache_seconds=5.0))
    monkeypatch.setattr(main, "is_database_ready", lambda: True)
    monkeypatch.setattr(main, "is_database_revision_ready", lambda: True)
    monkeypatch.setattr(main, "_media_storage", SimpleNamespace(readiness_check=lambda: True))
    response = main.readyz()
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ready",
        "checks": {"database": "ready", "migration": "ready", "storage": "ready"},
    }


@pytest.mark.parametrize(
    ("database", "migration", "storage", "failed"),
    [(False, True, True, "database"), (True, False, True, "migration"), (True, True, False, "storage")],
)
def test_readyz_returns_safe_503_component_states(monkeypatch, database, migration, storage, failed):
    monkeypatch.setattr(main, "_cfg", SimpleNamespace(object_storage_enabled=True, readiness_cache_seconds=0))
    monkeypatch.setattr(main, "is_database_ready", lambda: database)
    monkeypatch.setattr(main, "is_database_revision_ready", lambda: migration)
    monkeypatch.setattr(main, "_media_storage", SimpleNamespace(readiness_check=lambda: storage))
    response = main.readyz()
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"][failed] in {"unavailable", "mismatch"}
    assert "database.example" not in body
    assert "secret" not in json.dumps(body).lower()


def test_readyz_redacts_provider_exception_and_does_not_mutate_storage(monkeypatch, caplog):
    called = []

    def fail_storage():
        called.append(True)
        raise RuntimeError("https://user:password@private.example/bucket?X-Amz-Signature=secret")

    monkeypatch.setattr(main, "_cfg", SimpleNamespace(object_storage_enabled=True, readiness_cache_seconds=0))
    monkeypatch.setattr(main, "is_database_ready", lambda: True)
    monkeypatch.setattr(main, "is_database_revision_ready", lambda: True)
    monkeypatch.setattr(main, "_media_storage", SimpleNamespace(readiness_check=fail_storage))
    with caplog.at_level(logging.WARNING):
        response = main.readyz()
    assert response.status_code == 503
    assert called == [True]
    assert "password" not in caplog.text
    assert "X-Amz-Signature" not in caplog.text


def test_security_headers_are_present(client):
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "no-store" in response.headers["cache-control"]


def test_one_request_produces_one_access_log(client, caplog):
    with caplog.at_level(logging.DEBUG):
        response = client.get("/livez")
    assert response.status_code == 200
    access_records = [record for record in caplog.records if record.message == "request completed"]
    assert len(access_records) == 1
    assert access_records[0].request_id == response.headers["x-request-id"]
    assert access_records[0].client_ip == "unknown"


def test_authenticated_access_log_contains_user_context(client, auth_client, caplog):
    user = auth_client(username="logging_user")
    with caplog.at_level(logging.INFO):
        response = client.get("/auth/me")
    assert response.status_code == 200
    access_records = [record for record in caplog.records if record.message == "request completed"]
    assert len(access_records) == 1
    assert access_records[0].user_id == str(user.id)


def test_structured_logging_redacts_nested_secrets_and_signed_urls():
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "request %s",
        ("https://user:password@example.test/path?X-Amz-Signature=secret",),
        None,
    )
    record.request_id = "req-1"
    payload = json.loads(JsonFormatter().format(record))
    encoded = json.dumps(payload)
    assert "password" not in encoded
    assert "X-Amz-Signature" not in encoded
    assert redact_text("DATABASE_URL=postgresql://u:p@example/db") != "DATABASE_URL=postgresql://u:p@example/db"
    assert redact_value({"nested": {"password": "secret"}}) == {"nested": {"password": "[REDACTED]"}}


def test_structured_logging_redacts_header_and_custom_metadata():
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "Authorization: Bearer opaque-token Cookie: is_access=opaque",
        (),
        None,
    )
    record.metadata = {"Password": "secret", "safe": "value"}
    record.age_identity = "AGE-SECRET"
    # Importing the filter directly avoids depending on root logger setup.
    from logging_config import RedactionFilter

    RedactionFilter().filter(record)
    payload = json.dumps(JsonFormatter().format(record))
    assert "opaque-token" not in payload
    assert "is_access=opaque" not in payload
    assert "AGE-SECRET" not in payload
    assert "secret" not in payload
