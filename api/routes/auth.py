"""Canonical ``/auth/*`` endpoints for session-based authentication.

Endpoints:
- POST /auth/register   — create a new user account
- POST /auth/login      — authenticate and set session cookies
- POST /auth/refresh    — rotate refresh token, issue new access token
- POST /auth/logout     — revoke current session, clear cookies
- POST /auth/logout-all — revoke all sessions for the current user
- GET  /auth/me         — return current user profile
- GET  /auth/csrf       — issue a new CSRF token (sets cookie + returns value)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import (
    clear_cookie,
    generate_csrf_token,
    hash_password,
    set_cookie,
    validate_csrf_token,
    verify_and_update_password,
)
from auth.schemas import LoginIn, LoginOut, RegisterIn, RegisterOut, UserOut
from database import get_db
from deps import get_current_session, get_current_user, require_csrf, require_preauth_csrf
from models import RefreshSession, User, UserRole
from session import (
    issue_access_token,
    issue_refresh_session,
    revoke_all_user_sessions,
    revoke_session,
    rotate_refresh_token,
    is_session_active,
)
from settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _user_to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


def _set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    session_id: uuid.UUID,
) -> None:
    """Set access, refresh, and session-bound CSRF cookies on *response*."""
    cfg = get_settings()
    set_cookie(
        response,
        cfg.access_cookie_name,
        access_token,
        max_age=cfg.access_token_ttl_minutes * 60,
    )
    set_cookie(
        response,
        cfg.refresh_cookie_name,
        refresh_token,
        max_age=cfg.refresh_token_ttl_days * 86400,
    )
    csrf = generate_csrf_token(session_id)
    set_cookie(
        response,
        cfg.csrf_cookie_name,
        csrf,
        max_age=cfg.csrf_token_ttl_seconds,
        http_only=False,
    )


def _clear_auth_cookies(response: Response) -> None:
    cfg = get_settings()
    clear_cookie(response, cfg.access_cookie_name)
    clear_cookie(response, cfg.refresh_cookie_name)
    clear_cookie(response, cfg.csrf_cookie_name)


# ── Register ─────────────────────────────────────────────────────────────────


@router.post(
    "/register",
    response_model=RegisterOut,
    status_code=201,
    summary="Register a new user account",
)
def register(
    request: Request,
    body: RegisterIn = Body(...),
    _csrf: None = Depends(require_preauth_csrf),
    db: Session = Depends(get_db),
) -> RegisterOut:
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=400, detail="Username already registered")

    if body.email and db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=400, detail="Email already registered")

    try:
        hashed = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hashed,
        role=UserRole.user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return RegisterOut(user=_user_to_out(user))


# ── Login ────────────────────────────────────────────────────────────────────


@router.post(
    "/login",
    response_model=LoginOut,
    summary="Authenticate and set session cookies",
)
def login(
    request: Request,
    response: Response,
    body: LoginIn = Body(...),
    _csrf: None = Depends(require_preauth_csrf),
    db: Session = Depends(get_db),
) -> LoginOut:
    user = db.scalar(select(User).where(User.username == body.username))
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    success, new_hash = verify_and_update_password(body.password, user.hashed_password)
    if not success:
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    if new_hash is not None:
        user.hashed_password = new_hash
        db.commit()

    user_agent = request.headers.get("User-Agent", "")
    raw_refresh, session_id = issue_refresh_session(
        db, user_id=user.id, user_agent=user_agent
    )
    access = issue_access_token(user.id, session_id, user.role.value)

    _set_auth_cookies(response, access, raw_refresh, session_id)
    return LoginOut(user=_user_to_out(user))


# ── Refresh ──────────────────────────────────────────────────────────────────


@router.post(
    "/refresh",
    summary="Rotate refresh token and issue new access token",
)
def refresh(
    request: Request,
    response: Response,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    cfg = get_settings()
    raw_refresh: str | None = request.cookies.get(cfg.refresh_cookie_name)
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="No refresh token")

    result = rotate_refresh_token(
        db, raw_refresh, user_agent=request.headers.get("User-Agent", "")
    )
    if result is None:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    new_raw, new_session_id = result

    # Look up user to get role for the new access token
    session = db.scalar(select(RefreshSession).where(RefreshSession.id == new_session_id))
    if not session:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Session not found")

    user = db.scalar(select(User).where(User.id == session.user_id))
    if not user or not user.is_active:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="User not found or disabled")

    access = issue_access_token(user.id, new_session_id, user.role.value)
    _set_auth_cookies(response, access, new_raw, new_session_id)
    return {"detail": "Token refreshed"}


# ── Logout ───────────────────────────────────────────────────────────────────


@router.post(
    "/logout",
    summary="Revoke current session and clear cookies",
)
def logout(
    request: Request,
    response: Response,
    session: RefreshSession = Depends(get_current_session),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    cfg = get_settings()

    # Revoke the validated session (already confirmed active by dependency)
    revoke_session(db, session.id)

    # Also revoke the refresh session directly if present
    raw_refresh: str | None = request.cookies.get(cfg.refresh_cookie_name)
    if raw_refresh:
        from auth import hash_refresh_token

        token_hash = hash_refresh_token(raw_refresh)
        rs = db.scalar(
            select(RefreshSession).where(RefreshSession.token_hash == token_hash)
        )
        if rs:
            revoke_session(db, rs.id)

    _clear_auth_cookies(response)
    return {"detail": "Logged out"}


# ── Logout all ───────────────────────────────────────────────────────────────


@router.post(
    "/logout-all",
    summary="Revoke all sessions for the current user",
)
def logout_all(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    count = revoke_all_user_sessions(db, user.id)
    _clear_auth_cookies(response)
    return {"detail": "All sessions revoked", "revoked_count": count}


# ── Me ───────────────────────────────────────────────────────────────────────


@router.get(
    "/me",
    response_model=UserOut,
    summary="Get current user profile from session cookie",
)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_to_out(user)


# ── CSRF ─────────────────────────────────────────────────────────────────────


@router.get(
    "/csrf",
    summary="Issue a new CSRF token",
)
def csrf(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    """Set a new CSRF cookie and return the token value.

    If the user has an active session, the token is bound to that session.
    Otherwise, a pre-authentication token is issued (for login / register).
    """
    cfg = get_settings()

    # Try to resolve the current session
    from auth import decode_access_token, _PREAUTH_SESSION_ID

    token: str | None = request.cookies.get(cfg.access_cookie_name)
    session_id = _PREAUTH_SESSION_ID

    if token:
        payload = decode_access_token(token)
        if payload:
            session_id_str = payload.get("sid")
            if session_id_str:
                try:
                    session_id = uuid.UUID(session_id_str)
                    # Verify session is still active
                    from sqlalchemy import select as sa_select
                    session = db.scalar(
                        sa_select(RefreshSession).where(RefreshSession.id == session_id)
                    )
                    if session is None or session.revoked_at is not None:
                        session_id = _PREAUTH_SESSION_ID
                except (ValueError, Exception):
                    session_id = _PREAUTH_SESSION_ID

    csrf_value = generate_csrf_token(session_id)
    set_cookie(
        response,
        cfg.csrf_cookie_name,
        csrf_value,
        max_age=cfg.csrf_token_ttl_seconds,
        http_only=False,
    )
    return {"csrf_token": csrf_value}
