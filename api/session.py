"""Secure session service for cookie-based authentication.

Provides functions for:
- Access token creation (JWT with session ID, user ID, role, etc.)
- Refresh session creation with token rotation and reuse detection
- Session revocation (single, family-wide, user-wide)
- Access token validation (including session-revoked check)

All secrets (refresh tokens, raw JWTs) are NEVER logged.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from auth import (
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from models import AuditEventOutcome, RefreshSession, User
from settings import get_settings

logger = logging.getLogger(__name__)


# ── Access token with session claims ────────────────────────────────────────


def issue_access_token(
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
) -> str:
    """Create a short-lived access JWT carrying the user ID and session ID.

    The session ID (``sid``) is embedded in the token so that the API
    can validate that the session hasn't been revoked.
    """
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=cfg.access_token_ttl_minutes)
    payload = {
        "type": "access",
        "sub": str(user_id),
        "sid": str(session_id),
        "role": role,
        "iat": now,
        "nbf": now,
        "exp": expire,
        "jti": secrets.token_hex(16),
        "iss": cfg.jwt_issuer,
        "aud": cfg.jwt_audience,
    }
    return jwt.encode(payload, cfg.secret_key, algorithm=cfg.jwt_algorithm)


def validate_access_token(token: str) -> dict | None:
    """Decode and validate an access JWT.

    Checks issuer, audience, expiration, and signature.
    Does NOT check session revocation (callers must do that via DB lookup).
    """
    return decode_access_token(token)


# ── Refresh sessions ────────────────────────────────────────────────────────


def issue_refresh_session(
    db: Session,
    user_id: uuid.UUID,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
) -> tuple[str, uuid.UUID]:
    """Create a new refresh session.

    Returns ``(raw_token, session_id)``.
    The caller is responsible for setting the raw token in a cookie.
    """
    if family_id is None:
        family_id = uuid.uuid4()
    db.scalar(select(User).where(User.id == user_id).with_for_update())
    raw_token, session = _new_refresh_session(
        user_id=user_id,
        family_id=family_id,
        user_agent=user_agent,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    logger.debug("Refresh session %s created for user %s (family %s)", session.id, user_id, family_id)
    return raw_token, session.id


def _new_refresh_session(
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    user_agent: str | None,
) -> tuple[str, RefreshSession]:
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    raw_token = generate_refresh_token()
    return raw_token, RefreshSession(
        user_id=user_id,
        family_id=family_id,
        token_hash=hash_refresh_token(raw_token),
        created_at=now,
        expires_at=now + timedelta(days=cfg.refresh_token_ttl_days),
        user_agent=(user_agent or "")[:255],
    )


def rotate_refresh_token(
    db: Session,
    raw_token: str,
    user_agent: str | None = None,
    expected_session_id: uuid.UUID | None = None,
) -> tuple[str, uuid.UUID] | None:
    """Rotate an existing refresh token.

    1. Look up the session by token hash.
    2. If the session is already revoked → reuse detected → revoke entire family.
    3. If expired → revoke session → return None.
    4. Otherwise: revoke old, create replacement, link via ``replaced_by_session_id``.
    5. Returns ``(new_raw_token, new_session_id)`` or ``None`` on failure.
    """
    now = datetime.now(timezone.utc)
    token_hash = hash_refresh_token(raw_token)

    candidate = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == token_hash))
    if candidate is None:
        logger.warning("Refresh token not found (possible reuse)")
        return None

    # Every rotation/revocation path takes the user row first. This gives
    # logout-all, reuse handling, and simultaneous refreshes one lock order.
    user = db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    if user is None:
        db.rollback()
        return None
    session = db.scalar(
        select(RefreshSession)
        .where(RefreshSession.token_hash == token_hash)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if session is None:
        db.rollback()
        return None

    # ── Reuse detection ──────────────────────────────────────────────────────
    if session.revoked_at is not None:
        logger.warning(
            "Refresh token reuse detected — revoking family %s for user %s",
            session.family_id,
            session.user_id,
        )
        _revoke_family(db, session.user_id, session.family_id, commit=False)
        from audit_events import record_event_safe

        record_event_safe(
            db,
            event_type="auth.refresh_reuse_detected",
            outcome=AuditEventOutcome.denied,
            actor_user_id=session.user_id,
            subject_type="refresh_session",
            subject_id=str(session.id),
            metadata={"reason_code": "revoked_token_reuse"},
        )
        db.commit()
        return None

    if expected_session_id is not None and session.id != expected_session_id:
        db.rollback()
        return None

    # ── Expiry check ─────────────────────────────────────────────────────────
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        session.revoked_at = now
        db.commit()
        logger.debug("Refresh session %s expired", session.id)
        return None

    if not user.is_active:
        _revoke_family(db, session.user_id, session.family_id, commit=False)
        db.commit()
        return None

    # ── Create replacement ───────────────────────────────────────────────────
    new_raw, replacement = _new_refresh_session(
        user_id=session.user_id,
        family_id=session.family_id,
        user_agent=user_agent,
    )
    db.add(replacement)
    db.flush()

    # Revoke old session and link to replacement.
    session.revoked_at = now
    session.replaced_by_session_id = replacement.id
    session.last_used_at = now
    db.commit()

    logger.debug("Refresh session %s rotated to %s", session.id, replacement.id)
    return new_raw, replacement.id


def revoke_session(db: Session, session_id: uuid.UUID) -> None:
    """Soft-revoke a single refresh session."""
    now = datetime.now(timezone.utc)
    candidate = db.get(RefreshSession, session_id)
    if candidate is None:
        db.rollback()
        return
    db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
    db.execute(
        update(RefreshSession)
        .where(RefreshSession.id == session_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()
    logger.debug("Refresh session %s revoked", session_id)


def revoke_all_user_sessions(db: Session, user_id: uuid.UUID) -> int:
    """Revoke all active refresh sessions for a user.

    Returns the number of sessions revoked.
    """
    now = datetime.now(timezone.utc)
    db.scalar(select(User).where(User.id == user_id).with_for_update())
    result = db.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    db.commit()
    count = result.rowcount
    if count:
        logger.debug("Revoked %d refresh sessions for user %s", count, user_id)
    return count


def _revoke_family(
    db: Session,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    *,
    commit: bool = True,
) -> int:
    """Revoke all refresh sessions within a family (reuse detection)."""
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(RefreshSession)
        .where(
            RefreshSession.user_id == user_id,
            RefreshSession.family_id == family_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    if commit:
        db.commit()
    count = result.rowcount
    logger.warning(
        "Revoked %d refresh sessions in family %s (reuse detection)", count, family_id
    )
    return count


def get_session_by_id(db: Session, session_id: uuid.UUID) -> RefreshSession | None:
    """Fetch a refresh session by its primary key."""
    return db.scalar(select(RefreshSession).where(RefreshSession.id == session_id))


def is_session_active(db: Session, session_id: uuid.UUID) -> bool:
    """Check if a session exists and has not been revoked."""
    session = get_session_by_id(db, session_id)
    if session is None:
        return False
    if session.revoked_at is not None:
        return False
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires >= datetime.now(timezone.utc)
