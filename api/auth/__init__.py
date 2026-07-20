"""Core authentication primitives.

• Password hashing (Argon2id via pwdlib, legacy bcrypt fallback)
• JWT access-token creation / verification
• Refresh-token random-secret generation + SHA-256 hashing
• Cookie helpers (set / clear)
• CSRF token generation / validation

No secrets, passwords, hashes, or tokens are ever written to logs.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from starlette.responses import Response

from settings import get_settings

# ── Password hashing ────────────────────────────────────────────────────────
#
# Primary:  Argon2id  (via pwdlib)
# Fallback: bcrypt    (legacy — for existing DB rows only)
#
# On a successful bcrypt verify-and-update we rehash to Argon2id and return
# the new hash so the caller can persist it.

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

_password_hasher = PasswordHash([Argon2Hasher(), BcryptHasher()])


def hash_password(password: str) -> str:
    """Hash a *new* password with Argon2id.

    Raises ``ValueError`` for passwords outside the configured length bounds.
    """
    cfg = get_settings()
    if len(password) < cfg.min_password_length:
        raise ValueError(
            f"Password must be at least {cfg.min_password_length} characters"
        )
    if len(password) > cfg.max_password_length:
        raise ValueError(
            f"Password must not exceed {cfg.max_password_length} characters"
        )
    return _password_hasher.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify *plain* against *hashed* (Argon2id or legacy bcrypt).

    Never logs the password or hash.  Returns ``False`` on any error.
    """
    try:
        return _password_hasher.verify(plain, hashed)
    except Exception:
        return False


def verify_and_update_password(plain: str, hashed: str) -> tuple[bool, str | None]:
    """Verify and, if the hash is outdated (e.g. bcrypt), rehash to Argon2id.

    Returns ``(success, new_hash_or_None)``.
    * ``new_hash_or_None`` is non-``None`` only when the caller should update
      the stored hash.
    """
    try:
        return _password_hasher.verify_and_update(plain, hashed)
    except Exception:
        return False, None


# ── JWT access tokens ───────────────────────────────────────────────────────


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    """Create a short-lived JWT carrying the user id and role."""
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=cfg.access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": expire,
        "jti": secrets.token_hex(16),
        "iss": cfg.jwt_issuer,
        "aud": cfg.jwt_audience,
    }
    return jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    """Decode and validate a JWT. Returns the payload dict or None."""
    cfg = get_settings()
    try:
        return jwt.decode(
            token,
            cfg.secret_key,
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            audience=cfg.jwt_audience,
        )
    except jwt.PyJWTError:
        return None


# ── Refresh tokens ──────────────────────────────────────────────────────────
#
# A refresh token is a high-entropy random string.  Only the *hash* (SHA-256)
# is stored in PostgreSQL.  The plaintext is sent to the client inside an
# httpOnly cookie.


def generate_refresh_token() -> str:
    """Return a new high-entropy refresh token."""
    return secrets.token_urlsafe(64)


def hash_refresh_token(token: str) -> str:
    """Return a deterministic SHA-256 hex digest of *token*."""
    return hashlib.sha256(token.encode()).hexdigest()


# ── CSRF ────────────────────────────────────────────────────────────────────
#
# We use the double-submit cookie pattern:
#   1. Server generates a random value and sets it in a *non-httpOnly* cookie.
#   2. The client reads the cookie and sends it back in a request header.
#   3. Server compares the cookie value with the header value via hmac.compare_digest.

def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def validate_csrf(cookie_value: str | None, header_value: str | None) -> bool:
    """Constant-time comparison of CSRF cookie vs header."""
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


# ── Cookie helpers ──────────────────────────────────────────────────────────


def set_cookie(
    response: Response,
    name: str,
    value: str,
    max_age: int,
    *,
    http_only: bool = True,
    secure: bool | None = None,
    samesite: str | None = None,
    domain: str | None = None,
    path: str = "/",
) -> None:
    cfg = get_settings()
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=http_only,
        secure=secure if secure is not None else cfg.cookie_secure,
        samesite=samesite or cfg.cookie_same_site,
        domain=domain if domain is not None else cfg.cookie_domain,
        path=path,
    )


def clear_cookie(
    response: Response,
    name: str,
    *,
    domain: str | None = None,
    path: str = "/",
) -> None:
    cfg = get_settings()
    response.delete_cookie(
        key=name,
        domain=domain if domain is not None else cfg.cookie_domain,
        path=path,
    )
