"""Reusable FastAPI dependencies for authentication and authorization.

• ``get_db``         — yields a SQLAlchemy session
• ``get_current_user`` — reads the access-token cookie, decodes JWT, validates
                         session hasn't been revoked, returns User
• ``require_role``   — factory that returns a dependency enforcing a specific role
• ``require_csrf``   — validates the CSRF double-submit cookie pattern
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import decode_access_token, validate_csrf
from database import get_db
from models import User, RefreshSession
from settings import get_settings


# ── Current user from cookie ────────────────────────────────────────────────


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Extract and validate the access token from the httpOnly cookie.

    Validates:
    1. Cookie exists
    2. JWT signature, issuer, audience, expiration
    3. ``sid`` claim references an active (non-revoked, non-expired) session
    4. User exists and is active

    Raises 401 on any failure.  Never logs the token value.
    """
    cfg = get_settings()
    token: str | None = request.cookies.get(cfg.access_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id_str: str | None = payload.get("sub")
    session_id_str: str | None = payload.get("sid")
    if not user_id_str:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    # Validate session exists and is active (not revoked, not expired)
    if session_id_str:
        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError:
            raise HTTPException(status_code=401, detail="Invalid session ID in token")

        from session import is_session_active

        if not is_session_active(db, session_id):
            raise HTTPException(status_code=401, detail="Session revoked or expired")

    user = db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    return user


# ── Role enforcement ────────────────────────────────────────────────────────


def require_role(*allowed_roles: str):
    """Return a dependency that asserts the current user has one of *allowed_roles*."""

    def _check(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in allowed_roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _check


# ── CSRF double-submit validation ───────────────────────────────────────────


def require_csrf(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias=None)] = None,
) -> None:
    """Validate the CSRF double-submit cookie pattern.

    The header name is read from config (``x-csrf-token`` by default).
    """
    cfg = get_settings()
    csrf_cookie: str | None = request.cookies.get(cfg.csrf_cookie_name)
    header_val = x_csrf_token or request.headers.get(cfg.csrf_header_name)
    if not validate_csrf(csrf_cookie, header_val):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
