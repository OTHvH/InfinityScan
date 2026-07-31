"""Tests for the password hashing service (Argon2id + legacy bcrypt).

Covers:
  • New passwords hashed with Argon2id
  • Correct password verifies
  • Wrong password fails
  • Legacy bcrypt hashes verify
  • Legacy bcrypt rehashed to Argon2id after login
  • Oversized password rejected
  • Short password rejected
  • Hash never appears in response or logs
"""

from __future__ import annotations

import pytest

from conftest import _do_login, _do_register


class TestArgon2idHashing:
    """New passwords are hashed with Argon2id."""

    def test_hash_starts_with_argon2id(self):
        from auth import hash_password
        h = hash_password("strongpassword123")
        assert h.startswith("$argon2id$")

    def test_correct_password_verifies(self):
        from auth import hash_password, verify_password
        h = hash_password("strongpassword123")
        assert verify_password("strongpassword123", h) is True

    def test_wrong_password_fails(self):
        from auth import hash_password, verify_password
        h = hash_password("strongpassword123")
        assert verify_password("wrongpassword", h) is False

    def test_different_hashes_for_same_password(self):
        from auth import hash_password
        h1 = hash_password("strongpassword123")
        h2 = hash_password("strongpassword123")
        assert h1 != h2

    def test_oversized_password_rejected(self):
        from auth import hash_password
        with pytest.raises(ValueError, match="must not exceed"):
            hash_password("a" * 129)

    def test_short_password_rejected(self):
        from auth import hash_password
        with pytest.raises(ValueError, match="must be at least"):
            hash_password("short")

    def test_password_length_boundary_min(self):
        from auth import hash_password, verify_password
        h = hash_password("12345678")
        assert verify_password("12345678", h) is True

    def test_password_length_boundary_max(self):
        from auth import hash_password, verify_password
        pw = "a" * 128
        h = hash_password(pw)
        assert verify_password(pw, h) is True


class TestLegacyBcryptCompatibility:
    """Legacy bcrypt hashes are accepted and rehashed to Argon2id."""

    def _make_bcrypt_hash(self, password: str) -> str:
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        return ctx.hash(password)

    def test_bcrypt_hash_verifies(self):
        from auth import verify_password
        bc = self._make_bcrypt_hash("strongpassword123")
        assert verify_password("strongpassword123", bc) is True

    def test_bcrypt_wrong_password_fails(self):
        from auth import verify_password
        bc = self._make_bcrypt_hash("strongpassword123")
        assert verify_password("wrongpassword", bc) is False

    def test_verify_and_update_rehashes_bcrypt(self):
        from auth import verify_and_update_password
        bc = self._make_bcrypt_hash("strongpassword123")
        success, new_hash = verify_and_update_password("strongpassword123", bc)
        assert success is True
        assert new_hash is not None
        assert new_hash.startswith("$argon2id$")

    def test_verify_and_update_argon2_returns_none(self):
        from auth import hash_password, verify_and_update_password
        h = hash_password("strongpassword123")
        success, new_hash = verify_and_update_password("strongpassword123", h)
        assert success is True
        assert new_hash is None

    def test_bcrypt_login_upgrades_hash_in_db(self, client, user_factory, db):
        """Full integration: login with bcrypt hash -> DB updated to argon2id."""
        # Create user with legacy bcrypt hash
        user = user_factory(username="legacyuser", password="strongpassword123")

        # Overwrite the hash with a bcrypt one
        bc = self._make_bcrypt_hash("strongpassword123")
        user.hashed_password = bc
        db.commit()

        # Verify it's bcrypt before login
        assert bc.startswith("$2")

        # Login via the endpoint
        resp = _do_login(client, "legacyuser", "strongpassword123")
        assert resp.status_code == 200

        # Refresh from DB and confirm hash was upgraded
        db.refresh(user)
        assert user.hashed_password.startswith("$argon2id$")


class TestPasswordNeverInResponses:
    """Password hashes never leak in API responses."""

    def test_register_response_has_no_hash(self, client):
        resp = _do_register(client, "noleak1", "strongpassword123")
        body = resp.json()
        user = body["user"]
        assert "hashed_password" not in user
        assert "password" not in user
        assert "argon2" not in str(body).lower()
        assert "bcrypt" not in str(body).lower()

    def test_login_response_has_no_hash(self, client, user_factory):
        user_factory(username="noleak2", password="pass12345")
        resp = _do_login(client, "noleak2", "pass12345")
        body = resp.json()
        user = body["user"]
        assert "hashed_password" not in user
        assert "password" not in user
        assert "argon2" not in str(body).lower()

    def test_me_response_has_no_hash(self, client, user_factory):
        user_factory(username="noleak3", password="pass12345")
        _do_login(client, "noleak3", "pass12345")
        resp = client.get("/auth/me")
        body = resp.json()
        assert "hashed_password" not in body
        assert "password" not in body


class TestVerifyPasswordErrorHandling:
    """verify_password handles edge cases safely."""

    def test_garbage_hash_returns_false(self):
        from auth import verify_password
        assert verify_password("anything", "not-a-hash") is False

    def test_empty_hash_returns_false(self):
        from auth import verify_password
        assert verify_password("anything", "") is False

    def test_empty_password_returns_false(self):
        from auth import hash_password, verify_password
        h = hash_password("strongpassword123")
        assert verify_password("", h) is False
