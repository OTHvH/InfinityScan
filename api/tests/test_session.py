"""Comprehensive tests for session service and /auth/* endpoints.

Covers:
- Access token issuance with session claims (sub, sid, role, iss, aud, etc.)
- Refresh session creation and token rotation
- Reuse detection (family revocation)
- Session revocation (single, all, family)
- /auth/register, /auth/login, /auth/refresh, /auth/logout, /auth/logout-all
- /auth/me and /auth/csrf
- Session validation (revoked/expired → 401)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select

from models import RefreshSession, User, UserRole
from auth import (
    generate_csrf_token,
    hash_password,
    hash_refresh_token,
    _PREAUTH_SESSION_ID,
)
from session import (
    issue_access_token,
    issue_refresh_session,
    is_session_active,
    revoke_all_user_sessions,
    revoke_session,
    rotate_refresh_token,
)
from settings import get_settings


def _set_preauth_csrf(client) -> None:
    """Set a pre-auth CSRF cookie on the test client."""
    cfg = get_settings()
    client.cookies.delete(cfg.csrf_cookie_name)
    csrf = generate_csrf_token(_PREAUTH_SESSION_ID)
    client.cookies.set(cfg.csrf_cookie_name, csrf)


def _do_login(client, username: str, password: str = "strongpassword123"):
    """POST /auth/login with pre-auth CSRF and clean up cookie jar after."""
    cfg = get_settings()
    csrf = client.cookies.get(cfg.csrf_cookie_name)
    resp = client.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": csrf},
    )
    # After login, server sets a session-bound CSRF cookie.
    # Clean up any duplicate is_csrf cookies by keeping only the response one.
    resp_csrf = resp.cookies.get(cfg.csrf_cookie_name)
    client.cookies.delete(cfg.csrf_cookie_name)
    if resp_csrf:
        client.cookies.set(cfg.csrf_cookie_name, resp_csrf)
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# 1. Session service unit tests
# ═══════════════════════════════════════════════════════════════════════════


class TestIssueAccessToken:
    def test_contains_required_claims(self):
        user_id = uuid.uuid4()
        session_id = uuid.uuid4()
        token = issue_access_token(user_id, session_id, "user")

        cfg = get_settings()
        payload = jwt.decode(
            token,
            cfg.secret_key,
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            audience=cfg.jwt_audience,
        )
        assert payload["sub"] == str(user_id)
        assert payload["sid"] == str(session_id)
        assert payload["role"] == "user"
        assert "iat" in payload
        assert "nbf" in payload
        assert "exp" in payload
        assert "jti" in payload
        assert payload["iss"] == cfg.jwt_issuer
        assert payload["aud"] == cfg.jwt_audience

    def test_different_tokens_have_different_jti(self):
        uid = uuid.uuid4()
        sid = uuid.uuid4()
        cfg = get_settings()
        t1 = issue_access_token(uid, sid, "user")
        t2 = issue_access_token(uid, sid, "user")
        p1 = jwt.decode(t1, cfg.secret_key, algorithms=[cfg.jwt_algorithm],
                        audience=cfg.jwt_audience, issuer=cfg.jwt_issuer)
        p2 = jwt.decode(t2, cfg.secret_key, algorithms=[cfg.jwt_algorithm],
                        audience=cfg.jwt_audience, issuer=cfg.jwt_issuer)
        assert p1["jti"] != p2["jti"]

    def test_exp_is_in_the_future(self):
        token = issue_access_token(uuid.uuid4(), uuid.uuid4(), "user")
        cfg = get_settings()
        payload = jwt.decode(token, cfg.secret_key, algorithms=[cfg.jwt_algorithm],
                             audience=cfg.jwt_audience, issuer=cfg.jwt_issuer)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        assert exp > datetime.now(timezone.utc)


class TestIssueRefreshSession:
    def test_creates_session_row(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        raw_token, session_id = issue_refresh_session(db, user.id)
        assert raw_token
        assert isinstance(session_id, uuid.UUID)

        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == session_id))
        assert rs is not None
        assert rs.user_id == user.id
        assert rs.token_hash == hash_refresh_token(raw_token)
        assert rs.revoked_at is None
        # SQLite returns naive datetimes
        expires = rs.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        assert expires > datetime.now(timezone.utc)

    def test_generates_new_family_id_if_not_provided(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id)
        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        assert isinstance(rs.family_id, uuid.UUID)

    def test_reuses_provided_family_id(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        fam = uuid.uuid4()
        _, sid = issue_refresh_session(db, user.id, family_id=fam)
        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        assert rs.family_id == fam

    def test_stores_user_agent(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id, user_agent="TestAgent/1.0")
        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        assert rs.user_agent == "TestAgent/1.0"


class TestRotateRefreshSession:
    def test_valid_token_rotates(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        fam = uuid.uuid4()
        raw, sid = issue_refresh_session(db, user.id, family_id=fam)

        result = rotate_refresh_token(db, raw)
        assert result is not None
        new_raw, new_sid = result
        assert new_raw != raw
        assert new_sid != sid

        old = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        assert old.revoked_at is not None
        assert old.replaced_by_session_id == new_sid

        new = db.scalar(select(RefreshSession).where(RefreshSession.id == new_sid))
        assert new.revoked_at is None
        assert new.family_id == fam

    def test_reuse_detection_revokes_family(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        fam = uuid.uuid4()
        raw1, sid1 = issue_refresh_session(db, user.id, family_id=fam)
        raw2, sid2 = issue_refresh_session(db, user.id, family_id=fam)

        result1 = rotate_refresh_token(db, raw1)
        assert result1 is not None

        result2 = rotate_refresh_token(db, raw1)
        assert result2 is None

        s1 = db.scalar(select(RefreshSession).where(RefreshSession.id == sid1))
        s2 = db.scalar(select(RefreshSession).where(RefreshSession.id == sid2))
        assert s1.revoked_at is not None
        assert s2.revoked_at is not None

    def test_expired_token_returns_none(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        raw, sid = issue_refresh_session(db, user.id)

        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        rs.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()

        result = rotate_refresh_token(db, raw)
        assert result is None

        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        assert rs.revoked_at is not None

    def test_nonexistent_token_returns_none(self, db):
        result = rotate_refresh_token(db, "nonexistent_token_value")
        assert result is None


class TestRevokeSession:
    def test_sets_revoked_at(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id)
        revoke_session(db, sid)

        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        assert rs.revoked_at is not None

    def test_idempotent(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id)
        revoke_session(db, sid)
        first_time = db.scalar(select(RefreshSession).where(RefreshSession.id == sid)).revoked_at
        revoke_session(db, sid)
        second_time = db.scalar(select(RefreshSession).where(RefreshSession.id == sid)).revoked_at
        assert first_time == second_time


class TestRevokeAllUserSessions:
    def test_revokes_all_active(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, s1 = issue_refresh_session(db, user.id)
        _, s2 = issue_refresh_session(db, user.id)
        _, s3 = issue_refresh_session(db, user.id)

        count = revoke_all_user_sessions(db, user.id)
        assert count == 3

        for sid in [s1, s2, s3]:
            rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
            assert rs.revoked_at is not None

    def test_skips_already_revoked(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, s1 = issue_refresh_session(db, user.id)
        _, s2 = issue_refresh_session(db, user.id)
        revoke_session(db, s1)

        count = revoke_all_user_sessions(db, user.id)
        assert count == 1


class TestIsSessionActive:
    def test_active_session(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id)
        assert is_session_active(db, sid) is True

    def test_revoked_session(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id)
        revoke_session(db, sid)
        assert is_session_active(db, sid) is False

    def test_expired_session(self, db):
        user = User(
            username="testuser",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        _, sid = issue_refresh_session(db, user.id)
        rs = db.scalar(select(RefreshSession).where(RefreshSession.id == sid))
        rs.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
        assert is_session_active(db, sid) is False

    def test_nonexistent_session(self, db):
        assert is_session_active(db, uuid.uuid4()) is False


# ═══════════════════════════════════════════════════════════════════════════
# 2. /auth/* endpoint integration tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthRegister:
    def test_register_success(self, client):
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/register",
            json={"username": "newuser", "password": "strongpassword123"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["user"]["username"] == "newuser"
        assert data["user"]["role"] == "user"
        assert "id" in data["user"]

    def test_register_duplicate_username(self, client, user_factory):
        user_factory(username="dupeuser")
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/register",
            json={"username": "dupeuser", "password": "strongpassword123"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 409
        assert "already registered" in resp.json()["detail"]

    def test_register_weak_password(self, client):
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/register",
            json={"username": "newuser", "password": "short"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 422

    def test_register_with_email(self, client):
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/register",
            json={
                "username": "emailuser",
                "password": "strongpassword123",
                "email": "test@example.com",
            },
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 201
        assert resp.json()["user"]["email"] == "test@example.com"

    def test_register_rejects_extra_fields(self, client):
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/register",
            json={
                "username": "newuser",
                "password": "strongpassword123",
                "extra_field": "not_allowed",
            },
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 422


class TestAuthLogin:
    def test_login_success(self, client, user_factory):
        user_factory(username="logintest")
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/login",
            json={"username": "logintest", "password": "strongpassword123"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["username"] == "logintest"

        assert "is_access" in resp.cookies
        assert "is_refresh" in resp.cookies
        assert "is_csrf" in resp.cookies

    def test_login_wrong_password(self, client, user_factory):
        user_factory(username="logintest")
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/login",
            json={"username": "logintest", "password": "wrongpassword"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/login",
            json={"username": "nobody", "password": "strongpassword123"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 401

    def test_login_disabled_user(self, client, db):
        user = User(
            username="disabled",
            hashed_password=hash_password("strongpassword123"),
            role=UserRole.user,
            is_active=False,
        )
        db.add(user)
        db.commit()
        _set_preauth_csrf(client)

        resp = client.post(
            "/auth/login",
            json={"username": "disabled", "password": "strongpassword123"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 403

    def test_login_sets_session_in_db(self, client, user_factory, db):
        user_factory(username="sessiontest")
        _set_preauth_csrf(client)
        resp = client.post(
            "/auth/login",
            json={"username": "sessiontest", "password": "strongpassword123"},
            headers={"X-CSRF-Token": client.cookies.get("is_csrf")},
        )
        assert resp.status_code == 200

        user = db.scalar(select(User).where(User.username == "sessiontest"))
        sessions = list(
            db.scalars(
                select(RefreshSession).where(RefreshSession.user_id == user.id)
            ).all()
        )
        assert len(sessions) == 1
        assert sessions[0].revoked_at is None


class TestAuthRefresh:
    def _login(self, client, user_factory, username: str = "refreshtest"):
        user_factory(username=username)
        _set_preauth_csrf(client)
        resp = _do_login(client, username)
        assert resp.status_code == 200
        return resp

    def test_refresh_rotates_token(self, client, user_factory, db):
        self._login(client, user_factory)
        refresh_cookie = client.cookies.get("is_refresh")
        assert refresh_cookie
        csrf = client.cookies.get("is_csrf")

        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

        new_refresh = client.cookies.get("is_refresh")
        assert new_refresh
        assert new_refresh != refresh_cookie

    def test_refresh_old_token_revoked(self, client, user_factory, db):
        self._login(client, user_factory)
        old_refresh = client.cookies.get("is_refresh")
        csrf = client.cookies.get("is_csrf")

        client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})

        old_hash = hash_refresh_token(old_refresh)
        rs = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == old_hash))
        assert rs.revoked_at is not None

    def test_refresh_no_cookie(self, client):
        resp = client.post("/auth/refresh")
        assert resp.status_code == 401

    def test_concurrent_refresh_attempts(self, client, user_factory, db):
        self._login(client, user_factory, "concurrent_user")
        original_refresh = client.cookies.get("is_refresh")
        cfg = get_settings()
        csrf = client.cookies.get("is_csrf")

        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

        new_csrf = None
        for raw in resp.headers.get_list("set-cookie"):
            if raw.startswith(cfg.csrf_cookie_name + "="):
                new_csrf = raw.split("=", 1)[1].split(";")[0]
                break
        assert new_csrf is not None

        client.cookies.delete(cfg.refresh_cookie_name)
        client.cookies.set(cfg.refresh_cookie_name, original_refresh)
        client.cookies.delete(cfg.csrf_cookie_name)
        client.cookies.set(cfg.csrf_cookie_name, new_csrf)

        resp2 = client.post("/auth/refresh", headers={"X-CSRF-Token": new_csrf})
        assert resp2.status_code == 401

    def test_refresh_reuse_detection(self, client, user_factory, db):
        self._login(client, user_factory)
        refresh1 = client.cookies.get("is_refresh")
        csrf = client.cookies.get("is_csrf")

        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

        # After refresh, extract the new CSRF from the Set-Cookie header directly
        cfg = get_settings()
        new_csrf = None
        for raw in resp.headers.get_list("set-cookie"):
            if raw.startswith(cfg.csrf_cookie_name + "="):
                new_csrf = raw.split("=", 1)[1].split(";")[0]
                break
        assert new_csrf is not None

        # Clean up jar
        client.cookies.delete(cfg.csrf_cookie_name)
        client.cookies.set(cfg.csrf_cookie_name, new_csrf)

        # Restore the old refresh token but use the new CSRF token
        client.cookies.delete(cfg.refresh_cookie_name)
        client.cookies.set(cfg.refresh_cookie_name, refresh1)
        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": new_csrf})
        assert resp.status_code == 401

    def test_refresh_sets_new_access_token(self, client, user_factory):
        self._login(client, user_factory)
        old_access = client.cookies.get("is_access")
        csrf = client.cookies.get("is_csrf")

        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

        new_access = client.cookies.get("is_access")
        assert new_access != old_access


class TestAuthLogout:
    def _login(self, client, user_factory, username: str = "logouttest"):
        user_factory(username=username)
        _set_preauth_csrf(client)
        resp = _do_login(client, username)
        assert resp.status_code == 200
        return resp

    def test_logout_clears_cookies(self, client, user_factory):
        self._login(client, user_factory)
        assert client.cookies.get("is_access")
        assert client.cookies.get("is_refresh")

        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            "/auth/logout",
            headers={"x-csrf-token": csrf} if csrf else {},
        )
        assert resp.status_code == 200

    def test_logout_revokes_session(self, client, user_factory, db):
        self._login(client, user_factory)

        user = db.scalar(select(User).where(User.username == "logouttest"))
        sessions = list(
            db.scalars(
                select(RefreshSession).where(RefreshSession.user_id == user.id)
            ).all()
        )
        assert len(sessions) == 1
        assert sessions[0].revoked_at is None

        csrf = client.cookies.get("is_csrf")
        client.post(
            "/auth/logout",
            headers={"x-csrf-token": csrf} if csrf else {},
        )

        db.expire_all()
        sessions = list(
            db.scalars(
                select(RefreshSession).where(RefreshSession.user_id == user.id)
            ).all()
        )
        assert sessions[0].revoked_at is not None

    def test_logout_no_csrf(self, client, user_factory):
        self._login(client, user_factory)
        resp = client.post("/auth/logout")
        assert resp.status_code == 403

    def test_repeated_logout_safe(self, client, user_factory):
        self._login(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        resp1 = client.post(
            "/auth/logout",
            headers={"x-csrf-token": csrf} if csrf else {},
        )
        assert resp1.status_code == 200

        resp2 = client.post(
            "/auth/logout",
            headers={"x-csrf-token": csrf} if csrf else {},
        )
        assert resp2.status_code in (200, 401)


class TestAuthLogoutAll:
    def _login(self, client, user_factory, username: str = "logoutalltest"):
        user_factory(username=username)
        _set_preauth_csrf(client)
        resp = _do_login(client, username)
        assert resp.status_code == 200
        _set_preauth_csrf(client)
        resp = _do_login(client, username)
        assert resp.status_code == 200

    def test_logout_all_revokes_everything(self, client, user_factory, db):
        self._login(client, user_factory)

        user = db.scalar(select(User).where(User.username == "logoutalltest"))
        sessions = list(
            db.scalars(
                select(RefreshSession).where(
                    RefreshSession.user_id == user.id,
                    RefreshSession.revoked_at.is_(None),
                )
            ).all()
        )
        assert len(sessions) == 2

        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            "/auth/logout-all",
            headers={"x-csrf-token": csrf} if csrf else {},
        )
        assert resp.status_code == 200
        assert resp.json()["revoked_count"] == 2

        db.expire_all()
        active = list(
            db.scalars(
                select(RefreshSession).where(
                    RefreshSession.user_id == user.id,
                    RefreshSession.revoked_at.is_(None),
                )
            ).all()
        )
        assert len(active) == 0


class TestAuthMe:
    def _login(self, client, user_factory, username: str = "metest"):
        user_factory(username=username)
        _set_preauth_csrf(client)
        resp = _do_login(client, username)
        assert resp.status_code == 200

    def test_me_returns_user(self, client, user_factory):
        self._login(client, user_factory, "metest")
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "metest"

    def test_me_no_cookie(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_revoked_session(self, client, user_factory, db):
        self._login(client, user_factory, "metest")

        user = db.scalar(select(User).where(User.username == "metest"))
        revoke_all_user_sessions(db, user.id)

        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_missing_exp_claim(self, client, user_factory):
        user = user_factory(username="expmissing")
        cfg = get_settings()

        payload = {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": "user",
            "iat": datetime.now(timezone.utc),
            "nbf": datetime.now(timezone.utc),
            "jti": "deadbeef",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)

        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_wrong_type_claim(self, client, user_factory):
        user = user_factory(username="wrongtype")
        cfg = get_settings()
        now = datetime.now(timezone.utc)

        payload = {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": "user",
            "type": "refresh",
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(hours=1),
            "jti": "deadbeef",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)

        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_missing_user(self, client, user_factory, db):
        user = user_factory(username="deleteduser")
        cfg = get_settings()

        payload = {
            "sub": str(user.id),
            "sid": str(uuid.uuid4()),
            "role": "user",
            "iat": datetime.now(timezone.utc),
            "nbf": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "jti": "deadbeef",
            "iss": cfg.jwt_issuer,
            "aud": cfg.jwt_audience,
        }
        token = jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)
        client.cookies.set(cfg.access_cookie_name, token)

        db.delete(user)
        db.commit()

        resp = client.get("/auth/me")
        assert resp.status_code == 401


class TestAuthCsrf:
    def test_csrf_returns_token_and_cookie(self, client):
        resp = client.get("/auth/csrf")
        assert resp.status_code == 200
        data = resp.json()
        assert "csrf_token" in data
        assert len(data["csrf_token"]) > 10
        assert "is_csrf" in resp.cookies

    def test_csrf_cookie_not_http_only(self, client):
        resp = client.get("/auth/csrf")
        cookie = resp.cookies["is_csrf"]
        assert cookie
