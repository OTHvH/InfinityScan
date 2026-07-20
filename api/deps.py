"""Reusable FastAPI dependencies for authentication and authorization.

Layered dependency chain:

  ``get_current_session``
      Extracts the access JWT from the httpOnly cookie, decodes + validates
      (signature, issuer, audience, expiration), resolves the ``RefreshSession``
      from the ``sid`` claim, rejects revoked / expired sessions.

  ``get_current_user``
      Depends on ``get_current_session``.  Resolves the ``User`` from the ``sub``
      claim.  Rejects missing / inactive users.

  ``get_optional_user``
      Same as ``get_current_user`` but returns ``None`` instead of 401 when no
      cookie is present or the token is invalid.

  ``require_active_user``
      Explicit 403 gate for inactive accounts (identical behaviour to
      ``get_current_user`` for active users).

  ``require_role(*roles)``
      Factory returning a dependency that asserts the DB role is one of
      *allowed_roles*.  The DB is authoritative; the JWT ``role`` claim
      is **not** trusted for authorization.

  ``require_admin``
      Shorthand for ``require_role("admin")``.

  ``require_csrf``
      Validates the double-submit CSRF cookie pattern.

HTTP behaviour:
  - missing authentication    → 401
  - invalid / expired JWT     → 401
  - revoked / expired session → 401
  - missing / inactive user   → 401 / 403
  - wrong role                → 403

Design rules:
  - Identity comes **only** from the access-cookie JWT.
  - No Authorization header, no query params, no request body.
  - The same user is never queried twice within one request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import decode_access_token, validate_csrf
from database import get_db
from models import RefreshSession, User
from settings import get_settings


# ── Typed authentication context ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Immutable authentication context resolved from the request cookie.

    Carries the decoded JWT payload, the validated ``RefreshSession``,
    and the resolved ``User`` — all from a single DB round-trip for the
    user (the session is resolved in ``get_current_session``).
    """

    user: User
    session: RefreshSession
    payload: dict


# ── Layer 1: session validation ─────────────────────────────────────────────


def get_current_session(
    request: Request,
    db: Session = Depends(get_db),
) -> RefreshSession:
    """Extract and validate the access token from the httpOnly cookie.

    Validates:
      1. Cookie exists (only the configured access cookie is accepted).
      2. JWT signature, issuer, audience, expiration.
      3. ``sid`` claim references an active (non-revoked, non-expired)
         ``RefreshSession`` row.

    Raises 401 on any failure.  Never logs the token value.
    """
    cfg = get_settings()
    token: str | None = request.cookies.get(cfg.access_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    session_id_str: str | None = payload.get("sid")
    if not session_id_str:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        session_id = uuid.UUID(session_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid session ID in token")

    session = db.scalar(select(RefreshSession).where(RefreshSession.id == session_id))
    if session is None:
        raise HTTPException(status_code=401, detail="Session not found")

    if session.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Session revoked")

    from datetime import datetime, timezone

    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")

    return session


# ── Layer 2: user resolution ────────────────────────────────────────────────


def get_current_user(
    request: Request,
    session: RefreshSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the ``User`` from the JWT ``sub`` claim.

    Depends on ``get_current_session`` so the session has already been
    validated.  Rejects missing or inactive users.

    Also cross-checks that the ``sub`` claim matches ``session.user_id``
    — if they diverge the token is forged or stale and must be rejected.
    """
    cfg = get_settings()
    token: str | None = request.cookies.get(cfg.access_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id_str: str | None = payload.get("sub")
    if not user_id_str:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    if user_id != session.user_id:
        raise HTTPException(status_code=401, detail="Token subject does not match session")

    user = db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    return user


# ── Layer 3: optional authentication ────────────────────────────────────────


def get_optional_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    """Like ``get_current_user`` but returns ``None`` instead of 401.

    Useful for public endpoints that show extra data when logged in.
    Does **not** depend on ``get_current_session`` — it duplicates the
    minimal extraction logic so that a missing / invalid cookie silently
    resolves to ``None``.
    """
    cfg = get_settings()
    token: str | None = request.cookies.get(cfg.access_cookie_name)
    if not token:
        return None

    payload = decode_access_token(token)
    if payload is None:
        return None

    session_id_str: str | None = payload.get("sid")
    user_id_str: str | None = payload.get("sub")
    if not session_id_str or not user_id_str:
        return None

    try:
        session_id = uuid.UUID(session_id_str)
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        return None

    session = db.scalar(select(RefreshSession).where(RefreshSession.id == session_id))
    if session is None or session.revoked_at is not None:
        return None

    from datetime import datetime, timezone

    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        return None

    user = db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.is_active:
        return None

    return user


# ── Layer 4: explicit active-user gate ──────────────────────────────────────


def require_active_user(
    user: User = Depends(get_current_user),
) -> User:
    """Explicit 403 for inactive accounts.

    In practice ``get_current_user`` already rejects inactive users, but
    this dependency makes the intent explicit on endpoints that absolutely
    require an active account.
    """
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")
    return user


# ── Layer 5: role enforcement (DB is authoritative) ────────────────────────


def require_role(*allowed_roles: str):
    """Return a dependency that asserts the **database** role is allowed.

    The JWT ``role`` claim is **never** used for this check — only the
    ``role`` column on the ``users`` table is consulted.
    """

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _check


def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Shorthand for ``require_role("admin")``."""
    if user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


# ── CSRF double-submit validation ───────────────────────────────────────────


def require_csrf(
    request: Request,
    x_csrf_token: str | None = None,
) -> None:
    """Validate the CSRF double-submit cookie pattern.

    The header value is read from ``request.headers`` using the configured
    header name.  ``x_csrf_token`` is intentionally left as a plain
    parameter so that FastAPI does **not** inject it from query / body.
    """
    cfg = get_settings()
    csrf_cookie: str | None = request.cookies.get(cfg.csrf_cookie_name)
    header_val = x_csrf_token or request.headers.get(cfg.csrf_header_name)
    if not validate_csrf(csrf_cookie, header_val):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
