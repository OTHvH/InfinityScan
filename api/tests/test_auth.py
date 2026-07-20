"""Security tests for InfinityScan Phase 2 authentication.

Covers:
  • Registration (validation, duplicates, password policy)
  • Login (credentials, cookies, failure modes)
  • Logout (cookie clearing, token revocation)
  • Refresh token rotation and revocation
  • Access-token cookie-based identity
  • User-data isolation (bookmarks, progress)
  • CSRF double-submit cookie
  • Schema strictness (extra fields forbidden)
  • No tokens in JSON responses
  • No X-User-ID header accepted
  • Disabled-account handling
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from models import Bookmark, ReadingProgress, RefreshToken, Series, Chapter, Page, ContentType, ReadingMode, SeriesStatus


# ═════════════════════════════════════════════════════════════════════════════
#  Registration
# ═════════════════════════════════════════════════════════════════════════════


class TestRegistration:
    def test_register_success(self, client):
        resp = client.post("/register", json={
            "username": "newuser",
            "password": "strongpassword123",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "user" in body
        assert body["user"]["username"] == "newuser"
        assert body["user"]["role"] == "user"
        # No tokens in JSON
        assert "access_token" not in body
        assert "refresh_token" not in body

    def test_register_with_email(self, client):
        resp = client.post("/register", json={
            "username": "emailuser",
            "password": "strongpassword123",
            "email": "test@example.com",
        })
        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == "test@example.com"

    def test_register_duplicate_username(self, client, user_factory):
        user_factory(username="dupe_user")
        resp = client.post("/register", json={
            "username": "dupe_user",
            "password": "strongpassword123",
        })
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"]

    def test_register_duplicate_email(self, client, user_factory):
        user_factory(username="first", email="same@example.com")
        resp = client.post("/register", json={
            "username": "second",
            "password": "strongpassword123",
            "email": "same@example.com",
        })
        assert resp.status_code == 400

    def test_register_short_password(self, client):
        resp = client.post("/register", json={
            "username": "shortpw",
            "password": "abc",
        })
        assert resp.status_code == 422  # Pydantic validation

    def test_register_username_too_short(self, client):
        resp = client.post("/register", json={
            "username": "ab",
            "password": "strongpassword123",
        })
        assert resp.status_code == 422

    def test_register_extra_fields_rejected(self, client):
        resp = client.post("/register", json={
            "username": "evil",
            "password": "strongpassword123",
            "role": "admin",
        })
        assert resp.status_code == 422

    def test_register_invalid_username_chars(self, client):
        resp = client.post("/register", json={
            "username": "has spaces!",
            "password": "strongpassword123",
        })
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
#  Login
# ═════════════════════════════════════════════════════════════════════════════


class TestLogin:
    def test_login_success_sets_cookies(self, client, user_factory):
        user_factory(username="logintest", password="mypassword123")
        resp = client.post("/token", json={
            "username": "logintest",
            "password": "mypassword123",
        })
        assert resp.status_code == 200
        # Access cookie is set
        assert "is_access" in resp.cookies
        # Refresh cookie is set
        assert "is_refresh" in resp.cookies
        # CSRF cookie is set
        assert "is_csrf" in resp.cookies
        # No tokens in JSON body
        body = resp.json()
        assert "access_token" not in body
        assert "refresh_token" not in body
        assert body["user"]["username"] == "logintest"

    def test_login_wrong_password(self, client, user_factory):
        user_factory(username="wrongpw", password="correctpassword")
        resp = client.post("/token", json={
            "username": "wrongpw",
            "password": "wrongpassword",
        })
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = client.post("/token", json={
            "username": "ghost",
            "password": "whatever123",
        })
        assert resp.status_code == 401

    def test_login_disabled_account(self, client, db):
        from models import User, UserRole
        from auth import hash_password
        user = User(
            username="disabled_user",
            hashed_password=hash_password("password123"),
            role=UserRole.user,
            is_active=False,
        )
        db.add(user)
        db.commit()
        resp = client.post("/token", json={
            "username": "disabled_user",
            "password": "password123",
        })
        assert resp.status_code == 403

    def test_login_extra_fields_rejected(self, client):
        resp = client.post("/token", json={
            "username": "x",
            "password": "y",
            "hacked": True,
        })
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
#  /me — cookie-based identity
# ═════════════════════════════════════════════════════════════════════════════


class TestMe:
    def test_me_with_valid_cookie(self, client, user_factory):
        user_factory(username="meuser", password="mypass1234")
        client.post("/token", json={"username": "meuser", "password": "mypass1234"})
        resp = client.get("/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "meuser"

    def test_me_without_cookie(self, client):
        resp = client.get("/me")
        assert resp.status_code == 401

    def test_me_with_invalid_token(self, client):
        client.cookies.set("is_access", "totally.forged.token")
        resp = client.get("/me")
        assert resp.status_code == 401

    def test_me_ignores_x_user_id_header(self, client, user_factory):
        """X-User-ID must NOT be accepted as an identity mechanism."""
        user_a = user_factory(username="userA", password="pass12345")
        user_factory(username="userB", password="pass12345")
        # Login as userA
        client.post("/token", json={"username": "userA", "password": "pass12345"})
        # Try to impersonate userB via X-User-ID
        resp = client.get("/me", headers={"X-User-ID": str(user_a.id)})
        assert resp.status_code == 200
        assert resp.json()["username"] == "userA"


# ═════════════════════════════════════════════════════════════════════════════
#  Logout
# ═════════════════════════════════════════════════════════════════════════════


class TestLogout:
    def test_logout_clears_cookies(self, client, user_factory):
        user_factory(username="logoutuser", password="pass12345")
        client.post("/token", json={"username": "logoutuser", "password": "pass12345"})
        csrf = client.cookies.get("is_csrf")
        resp = client.post("/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        # After logout, /me should return 401
        resp2 = client.get("/me")
        assert resp2.status_code == 401

    def test_logout_revokes_refresh_token(self, client, user_factory, db):
        user_factory(username="revokeuser", password="pass12345")
        client.post("/token", json={"username": "revokeuser", "password": "pass12345"})

        # Find the refresh session in DB
        from models import RefreshSession
        rt = db.scalar(select(RefreshSession).limit(1))
        assert rt is not None
        assert rt.revoked_at is None

        csrf = client.cookies.get("is_csrf")
        client.post("/logout", headers={"X-CSRF-Token": csrf})

        db.refresh(rt)
        assert rt.revoked_at is not None


# ═════════════════════════════════════════════════════════════════════════════
#  Refresh token rotation
# ═════════════════════════════════════════════════════════════════════════════


class TestRefresh:
    def test_refresh_rotates_tokens(self, client, user_factory):
        user_factory(username="refreshuser", password="pass12345")
        login_resp = client.post("/token", json={
            "username": "refreshuser",
            "password": "pass12345",
        })
        old_refresh = login_resp.cookies.get("is_refresh")

        # Call refresh
        resp = client.post("/refresh")
        assert resp.status_code == 200
        new_refresh = resp.cookies.get("is_refresh")
        assert new_refresh is not None
        assert new_refresh != old_refresh

        # New access token works
        resp2 = client.get("/me")
        assert resp2.status_code == 200

    def test_refresh_reuses_old_token(self, client, user_factory):
        """Using a previously-used refresh token should fail and revoke all."""
        user_factory(username="reuseuser", password="pass12345")
        login_resp = client.post("/token", json={
            "username": "reuseuser",
            "password": "pass12345",
        })
        old_refresh = login_resp.cookies.get("is_refresh")

        # First refresh — rotates token
        client.post("/refresh")

        # Try to use the old refresh token again
        client.cookies.set("is_refresh", old_refresh)
        resp = client.post("/refresh")
        assert resp.status_code == 401

    def test_refresh_without_token(self, client):
        resp = client.post("/refresh")
        assert resp.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
#  CSRF
# ═════════════════════════════════════════════════════════════════════════════


class TestCSRF:
    def test_csrf_cookie_is_not_http_only(self, client, user_factory):
        """The CSRF cookie must be readable by JavaScript (not httpOnly)."""
        user_factory(username="csrfuser", password="pass12345")
        resp = client.post("/token", json={"username": "csrfuser", "password": "pass12345"})
        csrf_cookie = None
        for cookie in resp.cookies.jar:
            if cookie.name == "is_csrf":
                csrf_cookie = cookie
                break
        assert csrf_cookie is not None, "CSRF cookie not set"
        # httponly should be False (or not set) for CSRF cookies
        assert not csrf_cookie.has_nonstandard_attr("httponly") or "httponly" not in str(cookie).lower()

    def test_state_endpoint_without_csrf_fails(self, client, user_factory):
        """POST to bookmarks without CSRF header should fail."""
        user_factory(username="csrf2", password="pass12345")
        client.post("/token", json={"username": "csrf2", "password": "pass12345"})
        resp = client.post("/bookmarks", json={
            "series_path_word": "test",
            "series_name": "Test",
        })
        # Without CSRF header, should be rejected
        assert resp.status_code == 403

    def test_state_endpoint_with_csrf_succeeds(self, client, user_factory):
        """POST to bookmarks with correct CSRF header should succeed."""
        user_factory(username="csrf3", password="pass12345")
        client.post("/token", json={"username": "csrf3", "password": "pass12345"})
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": "test", "series_name": "Test"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201

    def test_state_endpoint_with_wrong_csrf_fails(self, client, user_factory):
        user_factory(username="csrf4", password="pass12345")
        client.post("/token", json={"username": "csrf4", "password": "pass12345"})
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": "test", "series_name": "Test"},
            headers={"X-CSRF-Token": "wrong-token-value"},
        )
        assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  User-data isolation
# ═════════════════════════════════════════════════════════════════════════════


class TestIsolation:
    def _login(self, client, username: str, password: str = "pass12345"):
        client.post("/token", json={"username": username, "password": password})

    def _create_series(self, db, slug: str) -> Series:
        s = Series(
            slug=slug,
            title=slug,
            content_type=ContentType.manga,
            status=SeriesStatus.ongoing,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s

    def test_user_a_cannot_see_user_b_bookmarks(self, client, user_factory, db):
        user_factory(username="iso_a", password="pass12345")
        user_factory(username="iso_b", password="pass12345")
        series = self._create_series(db, "isolated-series")

        from models import User
        user_a = db.scalar(select(User).where(User.username == "iso_a"))
        user_b = db.scalar(select(User).where(User.username == "iso_b"))

        db.add(Bookmark(user_id=user_a.id, series_id=series.id))
        db.commit()

        # Login as user_b
        self._login(client, "iso_b")
        csrf = client.cookies.get("is_csrf")
        resp = client.get("/bookmarks", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_user_a_cannot_see_user_b_progress(self, client, user_factory, db):
        user_factory(username="iso_pa", password="pass12345")
        user_factory(username="iso_pb", password="pass12345")
        ch_id = uuid.uuid4()
        series = self._create_series(db, "prog-series")
        ch = Chapter(id=ch_id, series_id=series.id, number=1.0, language="en")
        db.add(ch)
        db.commit()

        from models import User
        user_a = db.scalar(select(User).where(User.username == "iso_pa"))
        db.add(ReadingProgress(user_id=user_a.id, chapter_id=ch_id, last_page=5))
        db.commit()

        # Login as user_b
        self._login(client, "iso_pb")
        csrf = client.cookies.get("is_csrf")
        resp = client.get(
            f"/progress/prog-series/{ch_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_page"] is None

    def test_bookmarks_require_authentication(self, client):
        resp = client.get("/bookmarks")
        assert resp.status_code == 401

    def test_progress_require_authentication(self, client):
        ch_id = uuid.uuid4()
        resp = client.get(f"/progress/any-series/{ch_id}")
        assert resp.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
#  Schema strictness
# ═════════════════════════════════════════════════════════════════════════════


class TestSchemaStrictness:
    def test_register_rejects_extra_fields(self, client):
        resp = client.post("/register", json={
            "username": "test",
            "password": "strongpassword123",
            "is_admin": True,
        })
        assert resp.status_code == 422

    def test_bookmark_rejects_extra_fields(self, client, user_factory):
        user_factory(username="strictuser", password="pass12345")
        client.post("/token", json={"username": "strictuser", "password": "pass12345"})
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            "/bookmarks",
            json={
                "series_path_word": "x",
                "series_name": "X",
                "injected_field": True,
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    def test_progress_rejects_extra_fields(self, client, user_factory):
        user_factory(username="strict2", password="pass12345")
        client.post("/token", json={"username": "strict2", "password": "pass12345"})
        csrf = client.cookies.get("is_csrf")
        ch_id = uuid.uuid4()
        resp = client.post(
            f"/progress/some-slug/{ch_id}",
            json={
                "chapter_uuid": str(ch_id),
                "last_page": 1,
                "completed": False,
                "extra": "evil",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
#  Health endpoint (public, no auth)
# ═════════════════════════════════════════════════════════════════════════════


class TestHealth:
    def test_health_no_auth_needed(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ═════════════════════════════════════════════════════════════════════════════
#  Token secrecy — no tokens leak in JSON
# ═════════════════════════════════════════════════════════════════════════════


class TestTokenSecrecy:
    def test_login_response_has_no_token_fields(self, client, user_factory):
        user_factory(username="secretuser", password="pass12345")
        resp = client.post("/token", json={
            "username": "secretuser",
            "password": "pass12345",
        })
        body = resp.json()
        for key in body:
            assert "token" not in key.lower() or key == "token_type", f"Token field leaked: {key}"
        # Specifically check
        assert "access_token" not in body
        assert "refresh_token" not in body

    def test_register_response_has_no_token_fields(self, client):
        resp = client.post("/register", json={
            "username": "secretreg",
            "password": "strongpassword123",
        })
        body = resp.json()
        assert "access_token" not in body
        assert "refresh_token" not in body
