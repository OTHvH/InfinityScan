"""Core authentication primitives.

• Password hashing (Argon2id via pwdlib, legacy bcrypt fallback)
• JWT access-token creation / verification
• Refresh-token random-secret generation + SHA-256 hashing
• Cookie helpers (set / clear)
• Signed session-bound CSRF token generation / validation

No secrets, passwords, hashes, or tokens are ever written to logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid

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


def decode_access_token(token: str) -> dict | None:
    """Decode and validate a JWT. Returns the payload dict or None."""
    cfg = get_settings()
    try:
        payload = jwt.decode(
            token,
            cfg.secret_key,
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            audience=cfg.jwt_audience,
            options={
                "require": [
                    "type",
                    "sub",
                    "sid",
                    "role",
                    "iat",
                    "nbf",
                    "exp",
                    "jti",
                    "iss",
                    "aud",
                ]
            },
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload


def decode_refresh_binding_token(token: str) -> dict | None:
    """Validate an access token for refresh binding, allowing only expired ``exp``."""
    cfg = get_settings()
    try:
        payload = jwt.decode(
            token,
            cfg.secret_key,
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            audience=cfg.jwt_audience,
            options={
                "verify_exp": False,
                "require": [
                    "type",
                    "sub",
                    "sid",
                    "role",
                    "iat",
                    "nbf",
                    "exp",
                    "jti",
                    "iss",
                    "aud",
                ],
            },
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload


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


# ── CSRF (signed, session-bound) ──────────────────────────────────────────
#
# The CSRF token is an HMAC-signed, base64url-encoded value that binds the
# token to a specific refresh-session ID.  Format:
#
#   base64url(session_id_hex + "." + timestamp_str + "." + hmac_hex)
#
# The HMAC is computed over ``session_id_hex.timestamp_str`` using the
# dedicated ``CSRF_SECRET_KEY``.  This prevents:
#   - Cross-session token reuse
#   - Token forgery without the CSRF secret
#   - Replay attacks beyond the configured TTL
#
# For pre-authentication endpoints (login, register) a deterministic
# "pre-auth" session ID is derived from the CSRF secret so that the
# same validation logic applies before a session exists.

_PREAUTH_SESSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _csrf_hmac(key: str, message: str) -> str:
    """Compute HMAC-SHA256 and return the hex digest."""
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def generate_csrf_token(session_id: uuid.UUID | None = None) -> str:
    """Generate a signed CSRF token bound to *session_id*.

    If *session_id* is ``None`` the pre-auth session ID is used (for
    login / register flows before a session exists).
    """
    cfg = get_settings()
    sid = session_id or _PREAUTH_SESSION_ID
    sid_hex = sid.hex
    # Nanosecond precision guarantees a new token on immediate reissuance.
    ts_str = str(time.time_ns())
    sig = _csrf_hmac(cfg.csrf_secret_key, f"{sid_hex}.{ts_str}")
    payload = f"{sid_hex}.{ts_str}.{sig}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def validate_csrf_token(
    token: str,
    session_id: uuid.UUID | None = None,
) -> bool:
    """Validate a signed CSRF token.

    Checks:
      1. Token can be decoded.
      2. HMAC signature is valid.
      3. Session ID matches the expected value.
      4. Token has not expired (``csrf_token_ttl_seconds``).

    Returns ``True`` only if all checks pass.
    """
    cfg = get_settings()
    expected_sid = session_id or _PREAUTH_SESSION_ID

    try:
        # Re-add padding if needed
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        parts = decoded.split(".")
        if len(parts) != 3:
            return False
        sid_hex, ts_str, sig = parts
    except Exception:
        return False

    # Verify session ID
    try:
        sid = uuid.UUID(hex=sid_hex)
    except ValueError:
        return False
    if sid != expected_sid:
        return False

    # Verify HMAC
    expected_sig = _csrf_hmac(cfg.csrf_secret_key, f"{sid_hex}.{ts_str}")
    if not hmac.compare_digest(sig, expected_sig):
        return False

    # Verify expiry
    try:
        issued_at = int(ts_str)
    except ValueError:
        return False
    now = time.time_ns()
    if issued_at > now or now - issued_at > cfg.csrf_token_ttl_seconds * 1_000_000_000:
        return False

    return True


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
    path: str | None = None,
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
        path=path or cfg.cookie_path,
    )


def clear_cookie(
    response: Response,
    name: str,
    *,
    domain: str | None = None,
    path: str | None = None,
) -> None:
    cfg = get_settings()
    response.delete_cookie(
        key=name,
        domain=domain if domain is not None else cfg.cookie_domain,
        path=path or cfg.cookie_path,
    )
