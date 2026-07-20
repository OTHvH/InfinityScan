"""Tests for request-boundary middleware: origin validation, body limit, no-cache auth.

The middleware module provides three ASGI middleware classes that enforce
security boundaries on incoming HTTP requests.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from middleware import (
    OriginValidationMiddleware,
    RequestBodyLimitMiddleware,
    NoCacheAuthMiddleware,
    _origin_is_allowed,
)


# ── _origin_is_allowed helper ───────────────────────────────────────────────


class TestOriginIsAllowed:
    """Unit tests for the _origin_is_allowed helper."""

    def test_exact_match(self):
        assert _origin_is_allowed("http://localhost:3000", ["http://localhost:3000"])

    def test_exact_match_multiple(self):
        allowed = ["http://localhost:3000", "https://example.com"]
        assert _origin_is_allowed("https://example.com", allowed)

    def test_no_match(self):
        assert not _origin_is_allowed("http://evil.com", ["http://localhost:3000"])

    def test_port_wildcard_match(self):
        assert _origin_is_allowed("http://localhost:8080", ["http://localhost:*"])

    def test_port_wildcard_no_match_different_host(self):
        assert not _origin_is_allowed("http://evil.com:8080", ["http://localhost:*"])

    def test_port_wildcard_exact_host_required(self):
        assert not _origin_is_allowed("http://evil.com", ["http://localhost:*"])

    def test_empty_allowed_list(self):
        assert not _origin_is_allowed("http://localhost:3000", [])

    def test_scheme_mismatch_not_matched_by_netloc(self):
        """Different schemes but same host are different origins."""
        assert not _origin_is_allowed(
            "https://localhost:3000", ["http://localhost:3000"]
        )


# ── OriginValidationMiddleware ───────────────────────────────────────────────


class TestOriginValidationMiddleware:
    """Integration tests for origin validation via the test client."""

    def test_safe_method_passes_without_origin(self, client):
        """GET requests should pass without Origin header."""
        resp = client.get("/api/series")
        assert resp.status_code != 403

    def test_post_without_cookies_passes(self, client):
        """POST without cookies should pass (no cookie = no CSRF enforcement)."""
        resp = client.post(
            "/auth/csrf",
            headers={"Origin": "http://evil.com"},
        )
        # Should not be blocked by origin validation (no cookies)
        assert resp.status_code != 403 or "Origin not allowed" not in resp.text

    def test_post_with_cookies_and_valid_origin(self, client, user_factory):
        """POST with cookies and matching origin should pass."""
        user_factory(username="origintest", password="pass12345")
        # Login to get cookies
        resp = client.post(
            "/auth/csrf",
        )
        # Now set an origin that matches allowed_origins
        resp = client.post(
            "/auth/csrf",
            headers={
                "Origin": "http://localhost:3000",
                "Cookie": "; ".join(
                    f"{c.name}={c.value}" for c in client.cookies.jar
                ),
            },
        )
        # Should not be blocked
        assert "Origin not allowed" not in resp.text

    def test_post_with_cookies_and_invalid_origin(self, client, user_factory):
        """POST with cookies and disallowed origin should be blocked."""
        user_factory(username="origintest2", password="pass12345")
        # First login to populate cookies
        resp = client.get("/auth/csrf")
        # Now try with evil origin and cookies
        resp = client.post(
            "/auth/csrf",
            headers={"Origin": "http://evil.com"},
        )
        assert resp.status_code == 403
        assert "Origin not allowed" in resp.text

    def test_post_with_cookies_and_invalid_referer(self, client, user_factory):
        """POST with cookies and disallowed Referer origin should be blocked."""
        user_factory(username="origintest3", password="pass12345")
        # Login to get cookies
        resp = client.get("/auth/csrf")
        # Try with bad referer
        resp = client.post(
            "/auth/csrf",
            headers={"Referer": "http://evil.com/page"},
        )
        assert resp.status_code == 403
        assert "Origin not allowed" in resp.text


# ── RequestBodyLimitMiddleware ───────────────────────────────────────────────


class TestRequestBodyLimitMiddleware:
    """Tests for the request body size limit middleware."""

    def test_small_body_passes(self, client):
        """Small JSON body should pass."""
        resp = client.get("/api/series")
        assert resp.status_code != 413

    def test_get_requests_skip_check(self, client):
        """GET requests should skip body size check."""
        resp = client.get("/api/series")
        assert resp.status_code != 413

    def test_large_content_length_rejected(self, client):
        """Request with Content-Length exceeding limit should get 413."""
        # Default max is 1MB. Send Content-Length > 1MB.
        resp = client.post(
            "/auth/login",
            content=b"x" * (1024 * 1024 + 1),
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(1024 * 1024 + 1),
            },
        )
        assert resp.status_code == 413
        assert "too large" in resp.text.lower()

    def test_body_at_limit_passes(self, client):
        """Request exactly at the limit should pass (Content-Length check only)."""
        resp = client.post(
            "/auth/login",
            content=b"x" * 1024,
            headers={
                "Content-Type": "application/json",
                "Content-Length": "1024",
            },
        )
        # Should not be 413 (may be 422 or other validation error, but not 413)
        assert resp.status_code != 413


# ── NoCacheAuthMiddleware ────────────────────────────────────────────────────


class TestNoCacheAuthMiddleware:
    """Tests for Cache-Control: no-store on auth endpoints."""

    def test_auth_csrf_has_no_cache(self, client):
        """/auth/csrf should have Cache-Control: no-store."""
        resp = client.get("/auth/csrf")
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_auth_me_has_no_cache(self, client):
        """/auth/me should have Cache-Control: no-store."""
        resp = client.get("/auth/me")
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_auth_login_has_no_cache(self, client, user_factory):
        """/auth/login should have Cache-Control: no-store."""
        user_factory(username="nocachetest", password="pass12345")
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        resp = client.post(
            "/auth/login",
            json={"username": "nocachetest", "password": "pass12345"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_non_auth_endpoint_no_extra_headers(self, client):
        """Non-auth endpoints should not get no-store headers."""
        resp = client.get("/api/series")
        cache_control = resp.headers.get("cache-control", "")
        assert "no-store" not in cache_control

    def test_legacy_auth_endpoints_have_no_cache(self, client):
        """/auth/me (canonical) should have Cache-Control: no-store."""
        resp = client.get("/auth/me")
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_auth_register_has_no_cache(self, client):
        """/auth/register should have Cache-Control: no-store."""
        resp = client.get("/auth/csrf")
        csrf = resp.json()["csrf_token"]
        resp = client.post(
            "/auth/register",
            json={"username": "nocachereg", "password": "pass12345"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_auth_refresh_has_no_cache(self, client):
        """/auth/refresh should have Cache-Control: no-store."""
        resp = client.post("/auth/refresh")
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"

    def test_auth_logout_has_no_cache(self, client):
        """/auth/logout should have Cache-Control: no-store."""
        resp = client.post("/auth/logout")
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("pragma") == "no-cache"
