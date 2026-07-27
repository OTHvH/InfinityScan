"""Task 5 authentication, cookie, concurrency-safety, and log regressions."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select

from auth import _PREAUTH_SESSION_ID, generate_csrf_token, hash_refresh_token
from models import RefreshSession, User
from session import issue_refresh_session
from settings import get_settings


def _preauth(client) -> str:
    value = generate_csrf_token(_PREAUTH_SESSION_ID)
    client.cookies.set("is_csrf", value)
    return value


def _login(client, user_factory, username: str) -> None:
    user_factory(username=username)
    csrf = _preauth(client)
    response = client.post(
        "/auth/login",
        json={"username": username, "password": "strongpassword123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    values = {
        name: response.cookies.get(name)
        for name in ("is_access", "is_refresh", "is_csrf")
    }
    client.cookies.clear()
    for name, value in values.items():
        client.cookies.set(name, value)


def test_access_jwt_requires_type_and_exp(client, user_factory, db):
    user = user_factory(username="required-claims")
    raw, session_id = issue_refresh_session(db, user.id)
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    base = {
        "sub": str(user.id),
        "sid": str(session_id),
        "role": "user",
        "iat": now,
        "nbf": now,
        "jti": "required-claims",
        "iss": cfg.jwt_issuer,
        "aud": cfg.jwt_audience,
    }
    missing_exp = jwt.encode(base | {"type": "access"}, cfg.secret_key, algorithm=cfg.jwt_algorithm)
    client.cookies.set(cfg.access_cookie_name, missing_exp)
    assert client.get("/auth/me").status_code == 401

    wrong_type = jwt.encode(
        base | {"type": "refresh", "exp": now + timedelta(minutes=5)},
        cfg.secret_key,
        algorithm=cfg.jwt_algorithm,
    )
    client.cookies.set(cfg.access_cookie_name, wrong_type)
    assert client.get("/auth/me").status_code == 401
    assert raw


def test_expired_access_jwt_can_refresh_valid_session(client, user_factory):
    _login(client, user_factory, "expired-access-refresh")
    cfg = get_settings()
    access = client.cookies.get(cfg.access_cookie_name)
    payload = jwt.decode(access, options={"verify_signature": False})
    payload["exp"] = int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp())
    expired = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
    client.cookies.set(cfg.access_cookie_name, expired)

    response = client.post(
        "/auth/refresh",
        headers={"X-CSRF-Token": client.cookies.get(cfg.csrf_cookie_name)},
    )

    assert response.status_code == 200


def test_refresh_cookie_must_match_access_session(client, user_factory):
    _login(client, user_factory, "refresh-binding-a")
    access = client.cookies.get("is_access")
    csrf = client.cookies.get("is_csrf")
    refresh_a = client.cookies.get("is_refresh")
    other = client.__class__(client.app, raise_server_exceptions=False)
    _login(other, user_factory, "refresh-binding-b")
    refresh_b = other.cookies.get("is_refresh")

    client.cookies.clear()
    client.cookies.set("is_access", access)
    client.cookies.set("is_csrf", csrf)
    client.cookies.set("is_refresh", refresh_b)
    response = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 401
    assert refresh_a != refresh_b


def test_inactive_user_refresh_leaves_no_active_replacement(client, user_factory, db):
    _login(client, user_factory, "inactive-refresh")
    user = db.scalar(select(User).where(User.username == "inactive-refresh"))
    user.is_active = False
    db.commit()

    response = client.post(
        "/auth/refresh",
        headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
    )

    assert response.status_code == 401
    assert (
        db.scalar(
            select(RefreshSession.id).where(
                RefreshSession.user_id == user.id,
                RefreshSession.revoked_at.is_(None),
            )
        )
        is None
    )


def test_refresh_rotates_csrf_and_deletes_cookies_with_matching_attributes(
    client, user_factory
):
    _login(client, user_factory, "cookie-attributes")
    old_csrf = client.cookies.get("is_csrf")
    response = client.post("/auth/refresh", headers={"X-CSRF-Token": old_csrf})
    assert response.status_code == 200
    set_cookies = response.headers.get_list("set-cookie")
    assert len(set_cookies) == 3
    assert any("is_access=" in value and "HttpOnly" in value and "Path=/" in value for value in set_cookies)
    assert any("is_refresh=" in value and "HttpOnly" in value and "Path=/" in value for value in set_cookies)
    csrf_cookie = next(value for value in set_cookies if value.startswith("is_csrf="))
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    new_csrf = next(value.split("=", 1)[1].split(";", 1)[0] for value in set_cookies if value.startswith("is_csrf="))
    assert new_csrf != old_csrf

    logout = client.post(
        "/auth/logout", headers={"X-CSRF-Token": new_csrf}
    )
    assert logout.status_code == 200
    deleted = logout.headers.get_list("set-cookie")
    assert all("Max-Age=0" in value and "Path=/" in value for value in deleted)


def test_settings_repr_redacts_secrets():
    from settings import Settings

    settings = Settings(
        database_url="postgresql://user:db-pass-secret@example/db",
        secret_key="jwt-secret",
        csrf_secret_key="csrf-secret",
        copymanga_token="provider-secret",
        s3_access_key_id="access-secret",
        s3_secret_access_key="storage-secret",
    )
    rendered = repr(settings)
    assert all(secret not in rendered for secret in (
        "db-pass-secret",
        "jwt-secret",
        "csrf-secret",
        "provider-secret",
        "access-secret",
        "storage-secret",
    ))


def test_auth_logs_never_contain_token_or_password(caplog, db, user_factory):
    user = user_factory(username="log-safe")
    password = "sentinel-password-never-log"
    raw, _ = issue_refresh_session(db, user.id)
    with caplog.at_level(logging.DEBUG):
        from session import rotate_refresh_token

        result = rotate_refresh_token(db, raw)
        assert result is not None
        rotate_refresh_token(db, raw)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert password not in rendered
    assert raw not in rendered
    assert hash_refresh_token(raw) not in rendered


def test_trusted_host_rejects_untrusted_host(client):
    response = client.get("/auth/csrf", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_cors_credentials_are_explicit_and_origin_scoped(client):
    allowed = client.options(
        "/auth/refresh",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-CSRF-Token",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert allowed.headers.get("access-control-allow-credentials") == "true"

    denied = client.options(
        "/auth/refresh",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.headers.get("access-control-allow-origin") is None


def test_configured_rate_limits_return_429_for_each_write_surface(tmp_path):
    script = r'''
from fastapi.testclient import TestClient
from models import Base, User, UserRole
from auth import generate_csrf_token, _PREAUTH_SESSION_ID, hash_password
from database import _get_engine
from main import app
from session import issue_access_token, issue_refresh_session
import uuid

Base.metadata.create_all(_get_engine())
client = TestClient(app)

def csrf():
    value = generate_csrf_token(_PREAUTH_SESSION_ID)
    client.cookies.clear()
    client.cookies.set("is_csrf", value)
    return value

def authenticate(user):
    from sqlalchemy.orm import Session
    with Session(_get_engine()) as session:
        raw, session_id = issue_refresh_session(session, user.id, user_agent="rate-test")
    access = issue_access_token(user.id, session_id, user.role.value)
    values = {"is_access": access, "is_refresh": raw, "is_csrf": generate_csrf_token(session_id)}
    client.cookies.clear()
    for name, value in values.items():
        client.cookies.set(name, value)

def rate_limited(method, path, reset=None, **kwargs):
    first = method(path, **kwargs)
    if reset is not None:
        reset()
        headers = dict(kwargs.get("headers", {}))
        headers["X-CSRF-Token"] = client.cookies.get("is_csrf")
        kwargs["headers"] = headers
    second = method(path, **kwargs)
    assert second.status_code == 429, (first.status_code, second.status_code, second.text)

value = csrf()
registered = client.post("/auth/register", json={"username": "rate_register", "password": "strongpassword123"}, headers={"X-CSRF-Token": value})
assert registered.status_code == 201, registered.text
value = csrf()
limited_register = client.post("/auth/register", json={"username": "rate_register_2", "password": "strongpassword123"}, headers={"X-CSRF-Token": value})
assert limited_register.status_code == 429, limited_register.text

from sqlalchemy.orm import Session
with Session(_get_engine()) as session:
    user = User(username="rate-login", hashed_password=hash_password("strongpassword123"), role=UserRole.user)
    session.add(user)
    session.commit()
    session.refresh(user)
    value = csrf()
    assert client.post("/auth/login", json={"username": "rate-login", "password": "strongpassword123"}, headers={"X-CSRF-Token": value}).status_code == 200
    value = csrf()
    assert client.post("/auth/login", json={"username": "rate-login", "password": "strongpassword123"}, headers={"X-CSRF-Token": value}).status_code == 429

value = client.cookies.get("is_csrf")
authenticate(user)
value = client.cookies.get("is_csrf")
rate_limited(client.post, "/auth/refresh", reset=lambda: authenticate(user), headers={"X-CSRF-Token": value})
authenticate(user)
value = client.cookies.get("is_csrf")
rate_limited(client.post, "/auth/logout", reset=lambda: authenticate(user), headers={"X-CSRF-Token": value})
authenticate(user)
value = client.cookies.get("is_csrf")
rate_limited(client.post, "/auth/logout-all", reset=lambda: authenticate(user), headers={"X-CSRF-Token": value})
authenticate(user)
value = client.cookies.get("is_csrf")
rate_limited(client.post, "/bookmarks", json={"series_path_word": "rate-series", "series_name": "Rate"}, headers={"X-CSRF-Token": value})
authenticate(user)
value = client.cookies.get("is_csrf")
rate_limited(client.delete, "/bookmarks/rate-series", headers={"X-CSRF-Token": value})
authenticate(user)
value = client.cookies.get("is_csrf")
chapter = str(uuid.uuid4())
rate_limited(client.post, f"/progress/rate-series/{chapter}", json={"chapter_uuid": chapter, "last_page": 1, "scroll_position": 0.1, "completed": False}, headers={"X-CSRF-Token": value})
'''
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{tmp_path / 'rate-limits.db'}",
            "REGISTER_RATE_LIMIT": "1/minute",
            "LOGIN_RATE_LIMIT": "1/minute",
            "REFRESH_RATE_LIMIT": "1/minute",
            "LOGOUT_RATE_LIMIT": "1/minute",
            "BOOKMARK_WRITE_RATE_LIMIT": "1/minute",
            "PROGRESS_WRITE_RATE_LIMIT": "1/minute",
            "CSRF_RATE_LIMIT": "100/minute",
            "TRUSTED_HOSTS": "testserver",
            "COOKIE_SECURE": "false",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.path.dirname(__file__) + "/..",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
