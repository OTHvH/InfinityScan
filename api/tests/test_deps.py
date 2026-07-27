"""Tests for reusable authentication and authorization dependencies.

Covers:
  • get_current_session — cookie extraction, JWT validation, session validation
  • get_current_user    — user resolution, inactive user rejection
  • get_optional_user   — silent None on missing / invalid auth
  • require_active_user — explicit 403 for disabled accounts
  • require_role        — DB-role enforcement (JWT claim ignored)
  • require_admin       — shorthand admin gate
  • AuthContext         — typed context carrying user + session + payload
  • CSRF                — double-submit cookie validation
  • Identity isolation   — providing another UUID cannot change identity

HTTP behaviour:
  - missing authentication    → 401
  - invalid / malformed JWT   → 401
  - expired access token      → 401
  - wrong issuer / audience   → 401
  - revoked session           → 401
  - inactive account          → 403
  - wrong role                → 403
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

# ── Ensure api/ is importable and env is set before any app import ───────────

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("REGISTER_RATE_LIMIT", "999999/second")
os.environ.setdefault("LOGIN_RATE_LIMIT", "999999/second")

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth import generate_csrf_token, hash_password
from database import get_db
from deps import (
    get_current_session,
    get_current_user,
    get_optional_user,
    require_active_user,
    require_admin,
    require_csrf,
    require_role,
)
from models import Base, RefreshSession, User, UserRole
from session import issue_access_token, issue_refresh_session, revoke_session
from settings import get_settings


# ── Test-only mini-app ──────────────────────────────────────────────────────


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/session")
    def _session(s: RefreshSession = Depends(get_current_session)):
        return {"session_id": str(s.id), "user_id": str(s.user_id)}

    @app.get("/user")
    def _user(u: User = Depends(get_current_user)):
        return {"user_id": str(u.id), "username": u.username}

    @app.get("/optional")
    def _optional(u: User | None = Depends(get_optional_user)):
        if u is None:
            return {"authenticated": False}
        return {"authenticated": True, "user_id": str(u.id)}

    @app.get("/active")
    def _active(u: User = Depends(require_active_user)):
        return {"user_id": str(u.id)}

    @app.get("/admin-only")
    def _admin(u: User = Depends(require_admin)):
        return {"user_id": str(u.id), "role": u.role.value}

    @app.get("/editor-or-admin")
    def _editor_admin(u: User = Depends(require_role("admin", "editor"))):
        return {"user_id": str(u.id), "role": u.role.value}

    @app.get("/auth-context")
    def _ctx(
        session: RefreshSession = Depends(get_current_session),
        user: User = Depends(get_current_user),
    ):
        return {"user_id": str(user.id), "session_id": str(session.id)}

    @app.post("/csrf-protected")
    def _csrf_protected(
        _csrf: None = Depends(require_csrf),
        user: User = Depends(get_current_user),
    ):
        return {"ok": True}

    return app


# ── Fixtures ────────────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(_engine, "connect")
def _pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _tables():
    Base.metadata.create_all(_engine)
    yield
    Base.metadata.drop_all(_engine)


def _override_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def db():
    s = _TestSession()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def app():
    a = _build_app()
    a.dependency_overrides[get_db] = _override_db
    return a


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def user_factory(db):
    def _create(
        username: str | None = None,
        password: str = "strongpassword123",
        role: str = "user",
        active: bool = True,
    ):
        uname = username or f"user_{uuid.uuid4().hex[:8]}"
        role_enum = UserRole.admin if role == "admin" else UserRole.user
        user = User(
            username=uname,
            hashed_password=hash_password(password),
            role=role_enum,
            is_active=active,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return _create


# ── Helpers ──────────────────────────────────────────────────────────────────


def _set_cookie(client: TestClient, name: str, value: str) -> None:
    """Set a cookie on the test client."""
    client.cookies.set(name, value)


def _issue_token_pair(
    user: User, *, family_id: uuid.UUID | None = None
) -> tuple[str, uuid.UUID]:
    """Create a refresh session + access token for *user*.

    Returns ``(access_token, session_id)``.
    """
    raw, sid = issue_refresh_session(_TestSession(), user.id, family_id=family_id)
    access = issue_access_token(user.id, sid, user.role.value)
    return access, sid


def _login_user(
    client: TestClient, user: User, *, family_id: uuid.UUID | None = None
) -> uuid.UUID:
    """Set access + refresh + CSRF cookies.  Returns the session ID."""
    cfg = get_settings()
    raw, sid = issue_refresh_session(_TestSession(), user.id, family_id=family_id)
    access = issue_access_token(user.id, sid, user.role.value)
    csrf = generate_csrf_token(sid)
    client.cookies.set(cfg.access_cookie_name, access)
    client.cookies.set(cfg.refresh_cookie_name, raw)
    client.cookies.set(cfg.csrf_cookie_name, csrf)
    return sid


# ═══════════════════════════════════════════════════════════════════════════
# 1. get_current_session
# ═══════════════════════════════════════════════════════════════════════════


class TestGetCurrentSession:
    def test_no_cookie_returns_401(self, client):
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_malformed_cookie_returns_401(self, client):
        client.cookies.set("is_access", "not.a.valid.jwt")
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_expired_access_token_returns_401(self, client, user_factory, db):
        user = user_factory()
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "type": "access",
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": user.role.value,
            "iat": now - timedelta(hours=2),
            "nbf": now - timedelta(hours=2),
            "exp": now - timedelta(minutes=1),
            "jti": "deadbeef",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_wrong_issuer_returns_401(self, client, user_factory, db):
        user = user_factory()
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": user.role.value,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "bad_iss",
            "iss": "evil-issuer",
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_wrong_audience_returns_401(self, client, user_factory, db):
        user = user_factory()
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": user.role.value,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "bad_aud",
            "iss": cfg.jwt_issuer,
            "aud": "evil-audience",
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_wrong_secret_returns_401(self, client, user_factory, db):
        user = user_factory()
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": user.role.value,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "wrong_key",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, "completely-wrong-secret", algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_no_sid_claim_returns_401(self, client, user_factory, db):
        user = user_factory()
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "role": user.role.value,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "no_sid",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_invalid_sid_uuid_returns_401(self, client, user_factory, db):
        user = user_factory()
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "sid": "not-a-uuid",
            "role": user.role.value,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "bad_sid",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_nonexistent_session_returns_401(self, client, user_factory, db):
        user = user_factory()
        fake_sid = uuid.uuid4()
        access = issue_access_token(user.id, fake_sid, user.role.value)
        client.cookies.set(get_settings().access_cookie_name, access)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_revoked_session_returns_401(self, client, user_factory, db):
        user = user_factory()
        raw, sid = issue_refresh_session(db, user.id)
        revoke_session(db, sid)
        access = issue_access_token(user.id, sid, user.role.value)
        client.cookies.set(get_settings().access_cookie_name, access)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_expired_session_returns_401(self, client, user_factory, db):
        user = user_factory()
        raw, sid = issue_refresh_session(db, user.id)
        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        rs.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
        access = issue_access_token(user.id, sid, user.role.value)
        client.cookies.set(get_settings().access_cookie_name, access)
        resp = client.get("/session")
        assert resp.status_code == 401

    def test_valid_session_returns_200(self, client, user_factory, db):
        user = user_factory()
        raw, sid = issue_refresh_session(db, user.id)
        access = issue_access_token(user.id, sid, user.role.value)
        client.cookies.set(get_settings().access_cookie_name, access)
        resp = client.get("/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == str(sid)
        assert data["user_id"] == str(user.id)


# ═══════════════════════════════════════════════════════════════════════════
# 2. get_current_user
# ═══════════════════════════════════════════════════════════════════════════


class TestGetCurrentUser:
    def test_no_cookie_returns_401(self, client):
        resp = client.get("/user")
        assert resp.status_code == 401

    def test_valid_user_returns_200(self, client, user_factory, db):
        user = user_factory()
        _login_user(client, user)
        resp = client.get("/user")
        assert resp.status_code == 200
        assert resp.json()["username"] == user.username

    def test_inactive_user_returns_403(self, client, user_factory, db):
        user = user_factory(active=False)
        _login_user(client, user)
        resp = client.get("/user")
        assert resp.status_code == 403

    def test_providing_another_uuid_cannot_change_identity(self, client, user_factory, db):
        """A forged sub claim must not grant access to another user's identity."""
        user_a = user_factory(username="alice")
        user_b = user_factory(username="bob")
        raw, sid = issue_refresh_session(db, user_b.id)
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user_a.id),
            "sid": str(sid),
            "role": user_b.role.value,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "forged",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/user")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 3. get_optional_user
# ═══════════════════════════════════════════════════════════════════════════


class TestGetOptionalUser:
    def test_no_cookie_returns_none(self, client):
        resp = client.get("/optional")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    def test_invalid_token_returns_none(self, client):
        client.cookies.set("is_access", "garbage")
        resp = client.get("/optional")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    def test_valid_user_returns_authenticated(self, client, user_factory, db):
        user = user_factory()
        _login_user(client, user)
        resp = client.get("/optional")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is True
        assert resp.json()["user_id"] == str(user.id)

    def test_inactive_user_returns_none(self, client, user_factory, db):
        user = user_factory(active=False)
        _login_user(client, user)
        resp = client.get("/optional")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 4. require_active_user
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireActiveUser:
    def test_active_user_passes(self, client, user_factory, db):
        user = user_factory()
        _login_user(client, user)
        resp = client.get("/active")
        assert resp.status_code == 200

    def test_inactive_user_returns_403(self, client, user_factory, db):
        user = user_factory(active=False)
        _login_user(client, user)
        resp = client.get("/active")
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 5. require_role / require_admin
# ═══════════════════════════════════════════════════════════════════════════


class TestRequireRole:
    def test_admin_passes_admin_only(self, client, user_factory, db):
        user = user_factory(role="admin")
        _login_user(client, user)
        resp = client.get("/admin-only")
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_normal_user_fails_admin_dependency_with_403(self, client, user_factory, db):
        user = user_factory(role="user")
        _login_user(client, user)
        resp = client.get("/admin-only")
        assert resp.status_code == 403

    def test_admin_passes_multi_role(self, client, user_factory, db):
        user = user_factory(role="admin")
        _login_user(client, user)
        resp = client.get("/editor-or-admin")
        assert resp.status_code == 200

    def test_user_fails_multi_role(self, client, user_factory, db):
        user = user_factory(role="user")
        _login_user(client, user)
        resp = client.get("/editor-or-admin")
        assert resp.status_code == 403

    def test_role_from_db_not_jwt_claim(self, client, db):
        """A forged JWT with role=admin must not grant admin access."""
        user = User(
            username="normal",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        raw, sid = issue_refresh_session(db, user.id)
        cfg = get_settings()
        now = datetime.now(timezone.utc)
        payload = {
            "type": "access",
            "sub": str(user.id),
            "sid": str(sid),
            "role": "admin",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=30),
            "jti": "forged_role",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)
        resp = client.get("/admin-only")
        assert resp.status_code == 403

    def test_no_auth_fails_role_check(self, client):
        resp = client.get("/admin-only")
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════════
# 6. CSRF
# ═══════════════════════════════════════════════════════════════════════════


class TestCSRF:
    def test_no_csrf_header_fails(self, client, user_factory, db):
        user = user_factory()
        _login_user(client, user)
        resp = client.post("/csrf-protected")
        assert resp.status_code == 403

    def test_matching_csrf_succeeds(self, client, user_factory, db):
        user = user_factory()
        sid = _login_user(client, user)
        cfg = get_settings()
        csrf = generate_csrf_token(sid)
        client.cookies.set(cfg.csrf_cookie_name, csrf)
        resp = client.post("/csrf-protected", headers={"x-csrf-token": csrf})
        assert resp.status_code == 200

    def test_wrong_csrf_fails(self, client, user_factory, db):
        user = user_factory()
        _login_user(client, user)
        resp = client.post("/csrf-protected", headers={"x-csrf-token": "wrong"})
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# 7. AuthContext consistency
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthContextConsistency:
    def test_session_and_user_match(self, client, user_factory, db):
        """Both session.user_id and user.id must refer to the same user."""
        user = user_factory()
        _login_user(client, user)
        resp = client.get("/auth-context")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == str(user.id)
        assert uuid.UUID(data["session_id"])
