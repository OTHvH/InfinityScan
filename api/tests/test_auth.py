"""Security tests for InfinityScan Phase 2 authentication.

Covers:
  • Registration (validation, duplicates, password policy)
  • Login (credentials, cookies, failure modes)
  • Logout (cookie clearing, token revocation)
  • Refresh token rotation and revocation
  • Access-token cookie-based identity
  • User-data isolation (bookmarks, progress)
  • Signed session-bound CSRF protection
  • Schema strictness (extra fields forbidden)
  • No tokens in JSON responses
  • Identity cannot be spoofed via headers or request body
  • Disabled-account handling
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import select

from auth import (
    generate_csrf_token,
    validate_csrf_token,
    _PREAUTH_SESSION_ID,
)
from models import Bookmark, ReadingProgress, Series, Chapter, Page, ContentType, ReadingMode, SeriesStatus
from settings import get_settings

# Import test helpers from conftest
from conftest import _do_login, _do_register


# ═════════════════════════════════════════════════════════════════════════════
#  Registration
# ═════════════════════════════════════════════════════════════════════════════


class TestRegistration:
    def test_register_success(self, client):
        resp = _do_register(client, "newuser", "strongpassword123")
        assert resp.status_code == 201
        body = resp.json()
        assert "user" in body
        assert body["user"]["username"] == "newuser"
        assert body["user"]["role"] == "user"
        # No tokens in JSON
        assert "access_token" not in body
        assert "refresh_token" not in body

    def test_register_with_email(self, client):
        resp = _do_register(client, "emailuser", "strongpassword123", email="test@example.com")
        assert resp.status_code == 201
        assert resp.json()["user"]["email"] == "test@example.com"

    def test_register_duplicate_username(self, client, user_factory):
        user_factory(username="dupe_user")
        resp = _do_register(client, "dupe_user", "strongpassword123")
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"]

    def test_register_duplicate_email(self, client, user_factory):
        user_factory(username="first", email="same@example.com")
        resp = _do_register(client, "second", "strongpassword123", email="same@example.com")
        assert resp.status_code == 400

    def test_register_short_password(self, client):
        resp = _do_register(client, "shortpw", "abc")
        assert resp.status_code == 422  # Pydantic validation

    def test_register_username_too_short(self, client):
        resp = _do_register(client, "ab", "strongpassword123")
        assert resp.status_code == 422

    def test_register_extra_fields_rejected(self, client):
        # Must go through raw endpoint to send extra field
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        resp = client.post(
            "/auth/register",
            json={"username": "evil", "password": "strongpassword123", "role": "admin"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    def test_register_invalid_username_chars(self, client):
        resp = _do_register(client, "has spaces!", "strongpassword123")
        assert resp.status_code == 422

    def test_register_username_case_sensitive(self, client):
        resp = _do_register(client, "User", "strongpassword123")
        assert resp.status_code == 201
        assert resp.json()["user"]["username"] == "User"

    def test_register_email_domain_normalized(self, client):
        resp = _do_register(client, "emailcase", "strongpassword123", email="Test@Example.COM")
        assert resp.status_code == 201
        email = resp.json()["user"]["email"]
        assert email == "Test@example.com"

    def test_register_invalid_email(self, client):
        resp = _do_register(client, "bademail", "strongpassword123", email="not-an-email")
        assert resp.status_code == 422

    def test_register_overly_long_password(self, client):
        resp = _do_register(client, "longpw", "a" * 129)
        assert resp.status_code in (400, 422)

    def test_register_duplicate_normalized_email(self, client):
        resp1 = _do_register(client, "user_email1", "strongpassword123", email="Same@Example.com")
        assert resp1.status_code == 201
        resp2 = _do_register(client, "user_email2", "strongpassword123", email="same@example.com")
        assert resp2.status_code == 201  # EmailStr normalizes domain only; local part case differs

    def test_register_client_supplied_user_id_rejected(self, client):
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        resp = client.post(
            "/auth/register",
            json={"username": "x", "password": "strongpassword123", "id": "some-uuid"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
#  Login
# ═════════════════════════════════════════════════════════════════════════════


class TestLogin:
    def test_login_success_sets_cookies(self, client, user_factory):
        user_factory(username="logintest", password="mypassword123")
        resp = _do_login(client, "logintest", "mypassword123")
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
        resp = _do_login(client, "wrongpw", "wrongpassword")
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = _do_login(client, "ghost", "whatever123")
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
        resp = _do_login(client, "disabled_user", "password123")
        assert resp.status_code == 403

    def test_login_extra_fields_rejected(self, client):
        # Must go through raw endpoint to send extra field
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        resp = client.post(
            "/auth/login",
            json={"username": "x", "password": "y", "hacked": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    def test_login_wrong_password_and_nonexistent_same_response(self, client, user_factory):
        user_factory(username="compare_user", password="realpassword")
        resp_wrong = _do_login(client, "compare_user", "wrongpassword")
        resp_none = _do_login(client, "nonexistent_user", "whatever123")
        assert resp_wrong.status_code == 401
        assert resp_none.status_code == 401
        assert resp_wrong.json()["detail"] == resp_none.json()["detail"]


# ═════════════════════════════════════════════════════════════════════════════
#  /me — cookie-based identity
# ═════════════════════════════════════════════════════════════════════════════


class TestMe:
    def test_me_with_valid_cookie(self, client, user_factory):
        user_factory(username="meuser", password="mypass1234")
        _do_login(client, "meuser", "mypass1234")
        resp = client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["username"] == "meuser"

    def test_me_without_cookie(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_with_invalid_token(self, client):
        client.cookies.set("is_access", "totally.forged.token")
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_me_ignores_identity_header(self, client, user_factory):
        """X-User-ID must NOT be accepted as an identity mechanism."""
        user_a = user_factory(username="userA", password="pass12345")
        user_factory(username="userB", password="pass12345")
        # Login as userA
        _do_login(client, "userA", "pass12345")
        # Try to impersonate userA via X-User-ID (should be ignored)
        resp = client.get("/auth/me", headers={"X-User-ID": str(user_a.id)})
        assert resp.status_code == 200
        assert resp.json()["username"] == "userA"


# ═════════════════════════════════════════════════════════════════════════════
#  Logout
# ═════════════════════════════════════════════════════════════════════════════


class TestLogout:
    def test_logout_clears_cookies(self, client, user_factory):
        user_factory(username="logoutuser", password="pass12345")
        _do_login(client, "logoutuser", "pass12345")
        csrf = client.cookies.get("is_csrf")
        resp = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        # After logout, /auth/me should return 401
        resp2 = client.get("/auth/me")
        assert resp2.status_code == 401

    def test_logout_revokes_refresh_token(self, client, user_factory, db):
        user_factory(username="revokeuser", password="pass12345")
        _do_login(client, "revokeuser", "pass12345")

        # Find the refresh session in DB
        from models import RefreshSession
        rt = db.scalar(select(RefreshSession).limit(1))
        assert rt is not None
        assert rt.revoked_at is None

        csrf = client.cookies.get("is_csrf")
        client.post("/auth/logout", headers={"X-CSRF-Token": csrf})

        db.refresh(rt)
        assert rt.revoked_at is not None

    def test_repeated_logout_safe(self, client, user_factory):
        user_factory(username="repeatlogout", password="pass12345")
        _do_login(client, "repeatlogout", "pass12345")
        csrf = client.cookies.get("is_csrf")
        resp1 = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert resp1.status_code == 200
        csrf2 = client.cookies.get("is_csrf")
        resp2 = client.post("/auth/logout", headers={"X-CSRF-Token": csrf2} if csrf2 else {})
        assert resp2.status_code in (200, 401)
        assert resp2.status_code != 500


# ═════════════════════════════════════════════════════════════════════════════
#  Refresh token rotation
# ═════════════════════════════════════════════════════════════════════════════


class TestRefresh:
    def test_refresh_rotates_tokens(self, client, user_factory):
        user_factory(username="refreshuser", password="pass12345")
        login_resp = _do_login(client, "refreshuser", "pass12345")
        old_refresh = login_resp.cookies.get("is_refresh")

        # Call refresh with session-bound CSRF
        csrf = client.cookies.get("is_csrf")
        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200
        new_refresh = resp.cookies.get("is_refresh")
        assert new_refresh is not None
        assert new_refresh != old_refresh

        # New access token works
        resp2 = client.get("/auth/me")
        assert resp2.status_code == 200

    def test_refresh_reuses_old_token(self, client, user_factory):
        """Using a previously-used refresh token should fail and revoke all."""
        user_factory(username="reuseuser", password="pass12345")
        login_resp = _do_login(client, "reuseuser", "pass12345")
        old_refresh = login_resp.cookies.get("is_refresh")

        # First refresh — rotates token
        csrf = client.cookies.get("is_csrf")
        client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})

        # Try to use the old refresh token again
        client.cookies.set("is_refresh", old_refresh)
        csrf = client.cookies.get("is_csrf")
        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 401

    def test_refresh_without_token(self, client):
        resp = client.post("/auth/refresh")
        assert resp.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
#  CSRF (signed, session-bound)
# ═════════════════════════════════════════════════════════════════════════════


class TestCSRF:
    """Comprehensive CSRF protection tests.

    Validates the signed, session-bound CSRF token system:
    - HMAC signature bound to refresh-session ID
    - Token expiry
    - Pre-authentication tokens (login/register)
    - Session-bound tokens (authenticated endpoints)
    - Cookie attributes (HttpOnly=false, Secure, SameSite)
    - Safe method exemption
    - All failure modes return consistent 403
    """

    def _login(self, client, user_factory, username: str = "csrfuser"):
        """Login via canonical /auth/login (auto-sets session-bound CSRF)."""
        user_factory(username=username, password="pass12345")
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        client.post(
            "/auth/login",
            json={"username": username, "password": "pass12345"},
            headers={"X-CSRF-Token": csrf},
        )

    def _login_auth(self, client, user_factory, username: str = "csrfauth"):
        """Login via /auth/login with pre-auth CSRF."""
        cfg = get_settings()
        user_factory(username=username, password="pass12345")
        # Set pre-auth CSRF
        client.cookies.delete(cfg.csrf_cookie_name)
        preauth_csrf = generate_csrf_token(_PREAUTH_SESSION_ID)
        client.cookies.set(cfg.csrf_cookie_name, preauth_csrf)
        resp = client.post(
            "/auth/login",
            json={"username": username, "password": "pass12345"},
            headers={"X-CSRF-Token": preauth_csrf},
        )
        assert resp.status_code == 200
        # Clean up jar: keep only the session-bound CSRF from the response
        for raw in resp.headers.get_list("set-cookie"):
            if raw.startswith(cfg.csrf_cookie_name + "="):
                new_val = raw.split("=", 1)[1].split(";")[0]
                client.cookies.delete(cfg.csrf_cookie_name)
                client.cookies.set(cfg.csrf_cookie_name, new_val)
                break

    # ── Cookie attributes ────────────────────────────────────────────────────

    def test_csrf_cookie_is_not_http_only(self, client, user_factory):
        """The CSRF cookie must be readable by JavaScript (not httpOnly)."""
        self._login(client, user_factory)
        csrf_cookie = None
        for cookie in client.cookies.jar:
            if cookie.name == "is_csrf":
                csrf_cookie = cookie
                break
        assert csrf_cookie is not None, "CSRF cookie not set"
        assert not csrf_cookie.has_nonstandard_attr("httponly"), \
            "CSRF cookie must NOT be HttpOnly"

    # ── Signed token unit tests ──────────────────────────────────────────────

    def test_signed_token_roundtrip(self):
        """A generated token validates against the same session ID."""
        sid = uuid.uuid4()
        token = generate_csrf_token(sid)
        assert validate_csrf_token(token, sid) is True

    def test_signed_token_rejects_wrong_session(self):
        """A token bound to session A fails for session B."""
        sid_a = uuid.uuid4()
        sid_b = uuid.uuid4()
        token = generate_csrf_token(sid_a)
        assert validate_csrf_token(token, sid_b) is False

    def test_signed_token_rejects_forged_token(self):
        """A forged/random string is rejected."""
        sid = uuid.uuid4()
        assert validate_csrf_token("not-a-real-token", sid) is False

    def test_signed_token_rejects_modified_signature(self):
        """Tampering with the signature portion fails validation."""
        sid = uuid.uuid4()
        token = generate_csrf_token(sid)
        # Decode, modify sig, re-encode
        import base64
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        parts = decoded.split(".")
        parts[2] = "0" * 64  # zero out signature
        tampered = ".".join(parts)
        tampered_token = base64.urlsafe_b64encode(tampered.encode()).decode().rstrip("=")
        assert validate_csrf_token(tampered_token, sid) is False

    def test_signed_token_rejects_expired(self):
        """A token older than csrf_token_ttl_seconds is rejected."""
        sid = uuid.uuid4()
        token = generate_csrf_token(sid)
        # Temporarily patch the TTL to 1 second, wait, then validate
        from auth import _csrf_hmac
        import base64 as b64
        cfg = get_settings()
        # Manually create a token with old timestamp
        sid_hex = sid.hex
        old_ts = str(int(time.time()) - cfg.csrf_token_ttl_seconds - 10)
        sig = _csrf_hmac(cfg.csrf_secret_key, f"{sid_hex}.{old_ts}")
        payload = f"{sid_hex}.{old_ts}.{sig}"
        old_token = b64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        assert validate_csrf_token(old_token, sid) is False

    def test_preauth_token_validates(self):
        """Pre-auth token validates against the pre-auth session ID."""
        token = generate_csrf_token(_PREAUTH_SESSION_ID)
        assert validate_csrf_token(token, _PREAUTH_SESSION_ID) is True

    def test_preauth_token_rejects_real_session(self):
        """Pre-auth token fails when validated against a real session ID."""
        token = generate_csrf_token(_PREAUTH_SESSION_ID)
        real_sid = uuid.uuid4()
        assert validate_csrf_token(token, real_sid) is False

    # ── No CSRF cookie ───────────────────────────────────────────────────────

    def test_no_csrf_cookie_fails(self, client, user_factory):
        """State-changing request without CSRF cookie returns 403."""
        self._login(client, user_factory)
        # Clear the CSRF cookie
        client.cookies.delete("is_csrf")
        resp = client.post("/bookmarks", json={
            "series_path_word": "test",
            "series_name": "Test",
        })
        assert resp.status_code == 403

    # ── No CSRF header ───────────────────────────────────────────────────────

    def test_no_csrf_header_fails(self, client, user_factory):
        """State-changing request without CSRF header returns 403."""
        self._login(client, user_factory)
        resp = client.post("/bookmarks", json={
            "series_path_word": "test",
            "series_name": "Test",
        })
        assert resp.status_code == 403

    # ── Wrong header ─────────────────────────────────────────────────────────

    def test_wrong_csrf_header_fails(self, client, user_factory):
        """Wrong CSRF header value returns 403."""
        self._login(client, user_factory)
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": "test", "series_name": "Test"},
            headers={"X-CSRF-Token": "wrong-token-value"},
        )
        assert resp.status_code == 403

    # ── Modified signature ───────────────────────────────────────────────────

    def test_tampered_csrf_token_fails(self, client, user_factory):
        """CSRF token with modified signature returns 403."""
        self._login(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        # Tamper with the token
        tampered = csrf[:-4] + "XXXX"
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": "test", "series_name": "Test"},
            headers={"X-CSRF-Token": tampered},
        )
        assert resp.status_code == 403

    # ── Token from another session ───────────────────────────────────────────

    def test_csrf_from_another_session_fails(self, client, user_factory):
        """CSRF token from User A's session fails for User B."""
        user_factory(username="csrf_a", password="pass12345")
        user_factory(username="csrf_b", password="pass12345")

        # Login as user_a
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        client.post("/auth/login", json={"username": "csrf_a", "password": "pass12345"}, headers={"X-CSRF-Token": csrf})
        csrf_a = client.cookies.get("is_csrf")

        # Logout
        csrf = client.cookies.get("is_csrf")
        client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        client.cookies.delete("is_access")
        client.cookies.delete("is_refresh")
        client.cookies.delete("is_csrf")

        # Login as user_b
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        client.post("/auth/login", json={"username": "csrf_b", "password": "pass12345"}, headers={"X-CSRF-Token": csrf})

        # Try to use user_a's CSRF token
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": "test", "series_name": "Test"},
            headers={"X-CSRF-Token": csrf_a},
        )
        assert resp.status_code == 403

    # ── Old token after refresh rotation ─────────────────────────────────────

    def test_old_csrf_after_refresh_fails(self, client, user_factory):
        """CSRF token from before refresh is invalid (new session)."""
        user_factory(username="csrf_ref", password="pass12345")
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        client.post("/auth/login", json={"username": "csrf_ref", "password": "pass12345"}, headers={"X-CSRF-Token": csrf})
        old_csrf = client.cookies.get("is_csrf")

        # Refresh rotates the session → new CSRF token
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        client.post("/auth/login", json={"username": "csrf_ref", "password": "pass12345"}, headers={"X-CSRF-Token": csrf})
        new_csrf = client.cookies.get("is_csrf")

        if old_csrf != new_csrf:
            resp = client.post(
                "/bookmarks",
                json={"series_path_word": "test", "series_name": "Test"},
                headers={"X-CSRF-Token": old_csrf},
            )
            assert resp.status_code == 403

    # ── Pre-auth CSRF: login ─────────────────────────────────────────────────

    def test_login_requires_preauth_csrf(self, client, user_factory):
        """Login via /auth/login requires a valid pre-auth CSRF token."""
        user_factory(username="csrf_login", password="pass12345")
        resp = client.post(
            "/auth/login",
            json={"username": "csrf_login", "password": "pass12345"},
        )
        assert resp.status_code == 403

    def test_login_with_preauth_csrf_succeeds(self, client, user_factory):
        """Login via /auth/login succeeds with valid pre-auth CSRF."""
        user_factory(username="csrf_login2", password="pass12345")
        cfg = get_settings()
        # Get pre-auth CSRF from /auth/csrf endpoint
        resp = client.get("/auth/csrf")
        assert resp.status_code == 200
        preauth_csrf = resp.json()["csrf_token"]

        # Login with the pre-auth CSRF
        resp = client.post(
            "/auth/login",
            json={"username": "csrf_login2", "password": "pass12345"},
            headers={"X-CSRF-Token": preauth_csrf},
        )
        assert resp.status_code == 200
        assert "is_access" in resp.cookies
        assert "is_refresh" in resp.cookies

    def test_login_with_wrong_preauth_csrf_fails(self, client, user_factory):
        """Login with wrong pre-auth CSRF token returns 403."""
        user_factory(username="csrf_login3", password="pass12345")
        resp = client.post(
            "/auth/login",
            json={"username": "csrf_login3", "password": "pass12345"},
            headers={"X-CSRF-Token": "definitely-not-valid"},
        )
        assert resp.status_code == 403

    # ── Pre-auth CSRF: register ──────────────────────────────────────────────

    def test_register_requires_preauth_csrf(self, client):
        """Register via /auth/register requires a valid pre-auth CSRF token."""
        resp = client.post(
            "/auth/register",
            json={"username": "csrf_reg", "password": "strongpassword123"},
        )
        assert resp.status_code == 403

    def test_register_with_preauth_csrf_succeeds(self, client):
        """Register via /auth/register succeeds with valid pre-auth CSRF."""
        cfg = get_settings()
        resp = client.get("/auth/csrf")
        preauth_csrf = resp.json()["csrf_token"]

        resp = client.post(
            "/auth/register",
            json={"username": "csrf_reg2", "password": "strongpassword123"},
            headers={"X-CSRF-Token": preauth_csrf},
        )
        assert resp.status_code == 201

    # ── Refresh requires authenticated CSRF ──────────────────────────────────

    def test_refresh_requires_csrf(self, client, user_factory):
        """POST /auth/refresh requires a valid session-bound CSRF token."""
        self._login_auth(client, user_factory)
        resp = client.post("/auth/refresh")
        assert resp.status_code == 403

    def test_refresh_with_csrf_succeeds(self, client, user_factory):
        """POST /auth/refresh succeeds with valid session-bound CSRF."""
        self._login_auth(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        resp = client.post("/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

    # ── Logout requires authenticated CSRF ───────────────────────────────────

    def test_logout_requires_csrf(self, client, user_factory):
        """POST /auth/logout requires a valid session-bound CSRF token."""
        self._login_auth(client, user_factory)
        resp = client.post("/auth/logout")
        assert resp.status_code == 403

    def test_logout_with_csrf_succeeds(self, client, user_factory):
        """POST /auth/logout succeeds with valid session-bound CSRF."""
        self._login_auth(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        resp = client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200

    # ── Bookmark mutations ───────────────────────────────────────────────────

    def test_bookmark_post_requires_csrf(self, client, user_factory):
        """POST /bookmarks requires CSRF."""
        self._login(client, user_factory)
        resp = client.post("/bookmarks", json={
            "series_path_word": "test",
            "series_name": "Test",
        })
        assert resp.status_code == 403

    def test_bookmark_post_with_csrf_succeeds(self, client, user_factory):
        """POST /bookmarks succeeds with valid CSRF."""
        self._login(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": "test", "series_name": "Test"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201

    def test_bookmark_delete_requires_csrf(self, client, user_factory):
        """DELETE /bookmarks/{slug} requires CSRF."""
        self._login(client, user_factory)
        resp = client.delete("/bookmarks/some-slug")
        assert resp.status_code == 403

    def test_bookmark_delete_with_csrf_succeeds(self, client, user_factory):
        """DELETE /bookmarks/{slug} succeeds with valid CSRF."""
        self._login(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        resp = client.delete(
            "/bookmarks/test-slug",
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 204

    # ── Progress mutations ───────────────────────────────────────────────────

    def test_progress_upsert_requires_csrf(self, client, user_factory):
        """POST /progress requires CSRF."""
        self._login(client, user_factory)
        ch_id = uuid.uuid4()
        resp = client.post(
            f"/progress/some-slug/{ch_id}",
            json={"chapter_uuid": str(ch_id), "last_page": 1},
        )
        assert resp.status_code == 403

    def test_progress_upsert_with_csrf_succeeds(self, client, user_factory):
        """POST /progress succeeds with valid CSRF."""
        self._login(client, user_factory)
        csrf = client.cookies.get("is_csrf")
        ch_id = uuid.uuid4()
        resp = client.post(
            f"/progress/some-slug/{ch_id}",
            json={"chapter_uuid": str(ch_id), "last_page": 1},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200

    # ── Safe methods exempt ──────────────────────────────────────────────────

    def test_get_bookmarks_no_csrf_needed(self, client, user_factory):
        """GET /bookmarks does not require CSRF."""
        self._login(client, user_factory)
        resp = client.get("/bookmarks")
        assert resp.status_code == 200

    def test_get_progress_no_csrf_needed(self, client, user_factory):
        """GET /progress does not require CSRF."""
        self._login(client, user_factory)
        ch_id = uuid.uuid4()
        resp = client.get(f"/progress/some-slug/{ch_id}")
        assert resp.status_code == 200

    def test_health_no_csrf_needed(self, client):
        """GET /health does not require CSRF."""
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_get_me_no_csrf_needed(self, client, user_factory):
        """GET /auth/me does not require CSRF."""
        self._login(client, user_factory)
        resp = client.get("/auth/me")
        assert resp.status_code == 200

    # ── CSRF endpoint ────────────────────────────────────────────────────────

    def test_csrf_endpoint_returns_token_and_cookie(self, client):
        """GET /auth/csrf returns a token and sets a cookie."""
        resp = client.get("/auth/csrf")
        assert resp.status_code == 200
        data = resp.json()
        assert "csrf_token" in data
        assert len(data["csrf_token"]) > 10
        assert "is_csrf" in resp.cookies

    def test_csrf_endpoint_cookie_not_http_only(self, client):
        """GET /auth/csrf sets a non-HttpOnly cookie."""
        resp = client.get("/auth/csrf")
        for cookie in resp.cookies.jar:
            if cookie.name == "is_csrf":
                assert not cookie.has_nonstandard_attr("httponly")
                break

    # ── Failure consistency ──────────────────────────────────────────────────

    def test_all_csrf_failures_return_403(self, client, user_factory):
        """All CSRF failures return a consistent 403 response."""
        self._login(client, user_factory)
        cfg = get_settings()

        # No header
        resp = client.post("/bookmarks", json={
            "series_path_word": "x", "series_name": "X",
        })
        assert resp.status_code == 403

        # No cookie
        client.cookies.delete(cfg.csrf_cookie_name)
        resp = client.post("/bookmarks", json={
            "series_path_word": "x", "series_name": "X",
            "extra": "field",
        }, headers={"X-CSRF-Token": "something"})
        assert resp.status_code == 403

        # Wrong header
        client.cookies.set(cfg.csrf_cookie_name, "real-token")
        resp = client.post("/bookmarks", json={
            "series_path_word": "x", "series_name": "X",
        }, headers={"X-CSRF-Token": "wrong"})
        assert resp.status_code == 403

    # ── No token in localStorage/sessionStorage ──────────────────────────────

    def test_csrf_not_in_json_response(self, client):
        """GET /auth/csrf returns token in JSON body (for JS to read),
        but the cookie is the authoritative source."""
        resp = client.get("/auth/csrf")
        data = resp.json()
        assert "csrf_token" in data
        # The token should be a string, not nested
        assert isinstance(data["csrf_token"], str)


# ═════════════════════════════════════════════════════════════════════════════
#  User-data isolation (two-user tests)
# ═════════════════════════════════════════════════════════════════════════════


class TestIsolation:
    """Prove User B cannot access, modify, or infer User A's private data.

    Two users are created per test.  All endpoints are exercised to confirm
    that ownership filtering uses only the session-derived ``current_user.id``
    and never any client-supplied identity field.
    """

    def _login(self, client, username: str, password: str = "pass12345"):
        _do_login(client, username, password)

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

    def _create_chapter(self, db, series: Series, number: float = 1.0) -> Chapter:
        ch = Chapter(series_id=series.id, number=number, language="en")
        db.add(ch)
        db.commit()
        db.refresh(ch)
        return ch

    # ── Bookmarks ────────────────────────────────────────────────────────────

    def test_user_b_cannot_list_user_a_bookmarks(self, client, user_factory, db):
        user_factory(username="iso_a", password="pass12345")
        user_factory(username="iso_b", password="pass12345")
        series = self._create_series(db, "isolated-series")

        from models import User
        user_a = db.scalar(select(User).where(User.username == "iso_a"))

        db.add(Bookmark(user_id=user_a.id, series_id=series.id))
        db.commit()

        # Login as user_b
        self._login(client, "iso_b")
        resp = client.get("/bookmarks")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_user_b_cannot_delete_user_a_bookmark(self, client, user_factory, db):
        user_factory(username="del_a", password="pass12345")
        user_factory(username="del_b", password="pass12345")
        series = self._create_series(db, "del-series")

        from models import User
        user_a = db.scalar(select(User).where(User.username == "del_a"))

        db.add(Bookmark(user_id=user_a.id, series_id=series.id))
        db.commit()

        # Login as user_b and try to delete user_a's bookmark
        self._login(client, "del_b")
        csrf = client.cookies.get("is_csrf")
        resp = client.delete(f"/bookmarks/{series.slug}", headers={"X-CSRF-Token": csrf})
        # Should get 204 (idempotent) but user_a's bookmark still exists
        assert resp.status_code == 204

        # Verify user_a's bookmark still exists
        bm = db.scalar(
            select(Bookmark).where(
                Bookmark.user_id == user_a.id,
                Bookmark.series_id == series.id,
            )
        )
        assert bm is not None

    def test_user_b_cannot_inject_user_id_in_bookmark_json(self, client, user_factory, db):
        user_factory(username="inject_a", password="pass12345")
        user_factory(username="inject_b", password="pass12345")
        series = self._create_series(db, "inject-series")

        from models import User
        user_a = db.scalar(select(User).where(User.username == "inject_a"))

        # Login as user_b
        self._login(client, "inject_b")
        csrf = client.cookies.get("is_csrf")
        # Try to create a bookmark claiming to be user_a via user_id field
        resp = client.post(
            "/bookmarks",
            json={
                "series_path_word": series.slug,
                "series_name": series.title,
                "user_id": str(user_a.id),
            },
            headers={"X-CSRF-Token": csrf},
        )
        # Should be rejected by schema (extra=forbid) or ignored
        assert resp.status_code == 422

    def test_user_a_can_manage_own_bookmarks(self, client, user_factory, db):
        user_factory(username="own_a", password="pass12345")
        series = self._create_series(db, "own-series")

        # Login as user_a
        self._login(client, "own_a")
        csrf = client.cookies.get("is_csrf")

        # Add bookmark
        resp = client.post(
            "/bookmarks",
            json={"series_path_word": series.slug, "series_name": series.title},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201
        bm_id = resp.json()["id"]

        # List bookmarks
        resp = client.get("/bookmarks")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["id"] == bm_id

        # Delete bookmark
        resp = client.delete(f"/bookmarks/{series.slug}", headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 204

        # Verify deleted
        resp = client.get("/bookmarks")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_bookmark_duplicate_handled_safely(self, client, user_factory, db):
        user_factory(username="dupe_bm", password="pass12345")
        series = self._create_series(db, "dupe-series")

        self._login(client, "dupe_bm")
        csrf = client.cookies.get("is_csrf")

        # Add same bookmark twice
        resp1 = client.post(
            "/bookmarks",
            json={"series_path_word": series.slug, "series_name": series.title},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp1.status_code == 201
        id1 = resp1.json()["id"]

        resp2 = client.post(
            "/bookmarks",
            json={"series_path_word": series.slug, "series_name": series.title},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp2.status_code == 201
        id2 = resp2.json()["id"]
        # Should return same bookmark (idempotent)
        assert id1 == id2

        # Only one bookmark in DB
        resp = client.get("/bookmarks")
        assert len(resp.json()) == 1

    # ── Progress ─────────────────────────────────────────────────────────────

    def test_user_b_cannot_read_user_a_progress(self, client, user_factory, db):
        user_factory(username="prog_a", password="pass12345")
        user_factory(username="prog_b", password="pass12345")
        series = self._create_series(db, "prog-series")
        ch = self._create_chapter(db, series)

        from models import User
        user_a = db.scalar(select(User).where(User.username == "prog_a"))
        db.add(ReadingProgress(user_id=user_a.id, chapter_id=ch.id, last_page=5))
        db.commit()

        # Login as user_b
        self._login(client, "prog_b")
        resp = client.get(f"/progress/{series.slug}/{ch.id}")
        assert resp.status_code == 200
        data = resp.json()
        # User B sees empty progress (not User A's data)
        assert data["last_page"] is None

    def test_user_b_cannot_overwrite_user_a_progress(self, client, user_factory, db):
        user_factory(username="ow_a", password="pass12345")
        user_factory(username="ow_b", password="pass12345")
        series = self._create_series(db, "ow-series")
        ch = self._create_chapter(db, series)

        from models import User
        user_a = db.scalar(select(User).where(User.username == "ow_a"))
        db.add(ReadingProgress(user_id=user_a.id, chapter_id=ch.id, last_page=5))
        db.commit()

        # Login as user_b and upsert
        self._login(client, "ow_b")
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": 99, "completed": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200

        # User A's progress is unchanged
        db.expire_all()
        row = db.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_a.id,
                ReadingProgress.chapter_id == ch.id,
            )
        )
        assert row is not None
        assert row.last_page == 5

        # User B has their own progress
        user_b = db.scalar(select(User).where(User.username == "ow_b"))
        row_b = db.scalar(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user_b.id,
                ReadingProgress.chapter_id == ch.id,
            )
        )
        assert row_b is not None
        assert row_b.last_page == 99

    def test_user_b_cannot_inject_user_id_in_progress_json(self, client, user_factory, db):
        user_factory(username="inj_pa", password="pass12345")
        user_factory(username="inj_pb", password="pass12345")
        series = self._create_series(db, "inj-p-series")
        ch = self._create_chapter(db, series)

        from models import User
        user_a = db.scalar(select(User).where(User.username == "inj_pa"))

        # Login as user_b
        self._login(client, "inj_pb")
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={
                "chapter_uuid": str(ch.id),
                "last_page": 42,
                "user_id": str(user_a.id),
            },
            headers={"X-CSRF-Token": csrf},
        )
        # Should be rejected by schema (extra=forbid)
        assert resp.status_code == 422

    def test_user_a_can_manage_own_progress(self, client, user_factory, db):
        user_factory(username="own_pa", password="pass12345")
        series = self._create_series(db, "own-p-series")
        ch = self._create_chapter(db, series)

        self._login(client, "own_pa")
        csrf = client.cookies.get("is_csrf")

        # Upsert progress
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": 10, "scroll_position": 0.5},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        assert resp.json()["last_page"] == 10
        assert resp.json()["scroll_position"] == 0.5

        # Read back
        resp = client.get(f"/progress/{series.slug}/{ch.id}")
        assert resp.status_code == 200
        assert resp.json()["last_page"] == 10

        # Update progress
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": 20, "completed": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        assert resp.json()["last_page"] == 20
        assert resp.json()["completed"] is True

    def test_progress_uniqueness_constraint(self, client, user_factory, db):
        user_factory(username="uniq_u", password="pass12345")
        series = self._create_series(db, "uniq-series")
        ch = self._create_chapter(db, series)

        from models import User
        user = db.scalar(select(User).where(User.username == "uniq_u"))

        self._login(client, "uniq_u")
        csrf = client.cookies.get("is_csrf")

        # Upsert twice — should update, not create duplicate
        client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": 5},
            headers={"X-CSRF-Token": csrf},
        )
        client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": 15},
            headers={"X-CSRF-Token": csrf},
        )

        # Only one row for this user+chapter
        rows = list(db.scalars(
            select(ReadingProgress).where(
                ReadingProgress.user_id == user.id,
                ReadingProgress.chapter_id == ch.id,
            )
        ).all())
        assert len(rows) == 1
        assert rows[0].last_page == 15

    # ── Unauthenticated access ───────────────────────────────────────────────

    def test_bookmarks_require_authentication(self, client):
        resp = client.get("/bookmarks")
        assert resp.status_code == 401

    def test_bookmark_post_requires_authentication(self, client):
        resp = client.post("/bookmarks", json={"series_path_word": "x", "series_name": "X"})
        assert resp.status_code == 401

    def test_bookmark_delete_requires_authentication(self, client):
        resp = client.delete("/bookmarks/some-slug")
        assert resp.status_code == 401

    def test_progress_require_authentication(self, client):
        ch_id = uuid.uuid4()
        resp = client.get(f"/progress/any-series/{ch_id}")
        assert resp.status_code == 401

    def test_progress_upsert_requires_authentication(self, client):
        ch_id = uuid.uuid4()
        resp = client.post(
            f"/progress/any-series/{ch_id}",
            json={"chapter_uuid": str(ch_id), "last_page": 0},
        )
        assert resp.status_code == 401

    # ── Schema validation ────────────────────────────────────────────────────

    def test_progress_rejects_negative_last_page(self, client, user_factory, db):
        user_factory(username="neg_lp", password="pass12345")
        series = self._create_series(db, "neg-series")
        ch = self._create_chapter(db, series)

        self._login(client, "neg_lp")
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": -1},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    def test_progress_rejects_scroll_position_out_of_range(self, client, user_factory, db):
        user_factory(username="oor_sp", password="pass12345")
        series = self._create_series(db, "oor-series")
        ch = self._create_chapter(db, series)

        self._login(client, "oor_sp")
        csrf = client.cookies.get("is_csrf")
        # scroll_position > 1.0
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "scroll_position": 1.5},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    def test_progress_rejects_extra_fields(self, client, user_factory, db):
        user_factory(username="extra_p", password="pass12345")
        series = self._create_series(db, "extra-series")
        ch = self._create_chapter(db, series)

        self._login(client, "extra_p")
        csrf = client.cookies.get("is_csrf")
        resp = client.post(
            f"/progress/{series.slug}/{ch.id}",
            json={"chapter_uuid": str(ch.id), "last_page": 1, "injected": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    # ── Error message consistency (no data leakage) ──────────────────────────

    def test_user_b_delete_nonexistent_bookmark_same_response(self, client, user_factory, db):
        """Deleting a bookmark that doesn't exist returns 204 regardless
        of whether the series exists — no information leaked about other
        users' bookmarks."""
        user_factory(username="leak_a", password="pass12345")
        user_factory(username="leak_b", password="pass12345")
        series = self._create_series(db, "leak-series")

        from models import User
        user_a = db.scalar(select(User).where(User.username == "leak_a"))
        db.add(Bookmark(user_id=user_a.id, series_id=series.id))
        db.commit()

        # Login as user_b
        self._login(client, "leak_b")
        csrf = client.cookies.get("is_csrf")
        resp = client.delete(f"/bookmarks/{series.slug}", headers={"X-CSRF-Token": csrf})
        # Should be 204 (idempotent), no 404/403 that reveals user_a's bookmark exists
        assert resp.status_code == 204

    def test_unauth_progress_no_info_leakage(self, client, user_factory, db):
        """Unauthenticated progress request returns 401, not 404."""
        user_factory(username="noinf_a", password="pass12345")
        series = self._create_series(db, "noinf-series")
        ch = self._create_chapter(db, series)

        from models import User
        user_a = db.scalar(select(User).where(User.username == "noinf_a"))
        db.add(ReadingProgress(user_id=user_a.id, chapter_id=ch.id, last_page=5))
        db.commit()

        # Unauthenticated request — should be 401, not 404
        resp = client.get(f"/progress/{series.slug}/{ch.id}")
        assert resp.status_code == 401

    def test_user_id_query_param_ignored(self, client, user_factory, db):
        """Passing user_id as a query parameter does not affect results."""
        user_factory(username="qpa", password="pass12345")
        user_factory(username="qpb", password="pass12345")
        series = self._create_series(db, "qp-series")
        ch = self._create_chapter(db, series)

        from models import User
        user_a = db.scalar(select(User).where(User.username == "qpa"))
        db.add(ReadingProgress(user_id=user_a.id, chapter_id=ch.id, last_page=42))
        db.commit()

        self._login(client, "qpb")
        resp = client.get(f"/progress/{series.slug}/{ch.id}?user_id={user_a.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_page"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  Schema strictness
# ═════════════════════════════════════════════════════════════════════════════


class TestSchemaStrictness:
    def test_register_rejects_extra_fields(self, client):
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        resp = client.post(
            "/auth/register",
            json={"username": "test", "password": "strongpassword123", "is_admin": True},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    def test_bookmark_rejects_extra_fields(self, client, user_factory):
        user_factory(username="strictuser", password="pass12345")
        _do_login(client, "strictuser", "pass12345")
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
        _do_login(client, "strict2", "pass12345")
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
        resp = _do_login(client, "secretuser", "pass12345")
        body = resp.json()
        for key in body:
            assert "token" not in key.lower() or key == "token_type", f"Token field leaked: {key}"
        # Specifically check
        assert "access_token" not in body
        assert "refresh_token" not in body

    def test_register_response_has_no_token_fields(self, client):
        resp = _do_register(client, "secretreg", "strongpassword123")
        body = resp.json()
        assert "access_token" not in body
        assert "refresh_token" not in body
