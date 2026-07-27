"""Validated audit-event recording, pagination, and retention helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from models import AuditEvent, AuditEventOutcome
from settings import get_settings

logger = logging.getLogger(__name__)
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_TYPES = frozenset({
    "auth.register", "auth.login", "auth.refresh", "auth.refresh_reuse_detected",
    "auth.logout", "auth.logout_all", "auth.csrf_issued", "admin.authorization_denied",
    "rate_limit.denied", "origin.denied", "import.completed", "import.failed",
    "import.recovered", "integrity.repaired", "audit.pruned",
})
METADATA_KEYS = frozenset({
    "reason_code", "status", "item_count", "page_count", "session_count",
    "retention_days", "removed_count", "dry_run", "batch_size", "error_code",
})
MAX_METADATA_KEYS = 16
MAX_METADATA_BYTES = 2048
MAX_METADATA_STRING = 128


class AuditValidationError(ValueError):
    """Raised when an event or its metadata is not approved for persistence."""


def validate_request_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value if REQUEST_ID_RE.fullmatch(value) else None


def _validate_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict) or len(metadata) > MAX_METADATA_KEYS:
        raise AuditValidationError("audit metadata must be a small object")
    if any(key not in METADATA_KEYS for key in metadata):
        raise AuditValidationError("audit metadata contains an unapproved field")
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, bool) or isinstance(value, int) and not isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, str) and len(value) <= MAX_METADATA_STRING:
            clean[key] = value
        else:
            raise AuditValidationError("audit metadata contains an invalid value")
    encoded = json.dumps(clean, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise AuditValidationError("audit metadata is too large")
    return clean


def record_event(
    db: Session,
    *,
    event_type: str,
    outcome: AuditEventOutcome,
    request_id: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Validate and stage one event. The caller owns the transaction."""
    if event_type not in EVENT_TYPES:
        raise AuditValidationError("unknown audit event type")
    try:
        outcome = AuditEventOutcome(outcome)
    except ValueError as exc:
        raise AuditValidationError("invalid audit outcome") from exc
    explicit_request_id = request_id is not None
    if request_id is None:
        # Imported lazily to keep the request-boundary middleware independent.
        from middleware import request_id_context

        request_id = request_id_context.get()
    request_id = validate_request_id(request_id)
    if explicit_request_id and request_id is None:
        raise AuditValidationError("invalid request ID")
    if subject_type is None and subject_id is not None or subject_type is not None and subject_id is None:
        raise AuditValidationError("subject fields must be supplied together")
    if subject_type is not None and (not subject_type or len(subject_type) > 64):
        raise AuditValidationError("invalid subject type")
    if subject_id is not None and len(subject_id) > 255:
        raise AuditValidationError("subject ID is too long")
    event = AuditEvent(
        request_id=request_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        outcome=outcome,
        subject_type=subject_type,
        subject_id=subject_id,
        event_metadata=_validate_metadata(metadata),
    )
    db.add(event)
    db.flush()
    return event


def record_event_safe(db: Session, **kwargs: Any) -> AuditEvent | None:
    """Best-effort boundary that never logs event payloads or secret values."""
    try:
        with db.begin_nested():
            return record_event(db, **kwargs)
    except Exception:
        logger.error("audit persistence failed event_type=%s", kwargs.get("event_type", "unknown"))
        return None


def _cursor_signature(payload: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), payload.encode("ascii"), hashlib.sha256).hexdigest()


def encode_cursor(created_at: datetime, event_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.astimezone(timezone.utc).isoformat(), "id": str(event_id)},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{encoded}.{_cursor_signature(encoded)}"


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        encoded, signature = cursor.split(".", 1)
        if not hmac.compare_digest(signature, _cursor_signature(encoded)):
            raise ValueError
        padded = encoded + "=" * (-len(encoded) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        created_at = datetime.fromisoformat(data["created_at"])
        event_id = uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise AuditValidationError("invalid audit cursor") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at, event_id


def list_events(
    db: Session,
    *,
    limit: int = 50,
    cursor: str | None = None,
    event_type: str | None = None,
    outcome: AuditEventOutcome | None = None,
) -> tuple[list[AuditEvent], str | None]:
    if not 1 <= limit <= 200:
        raise AuditValidationError("audit page size must be between 1 and 200")
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
    if event_type is not None:
        if event_type not in EVENT_TYPES:
            raise AuditValidationError("unknown audit event type")
        query = query.where(AuditEvent.event_type == event_type)
    if outcome is not None:
        query = query.where(AuditEvent.outcome == outcome)
    if cursor:
        created_at, event_id = decode_cursor(cursor)
        query = query.where(or_(AuditEvent.created_at < created_at, (AuditEvent.created_at == created_at) & (AuditEvent.id < event_id)))
    rows = list(db.scalars(query).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        if last.created_at is not None:
            next_cursor = encode_cursor(last.created_at, last.id)
    return rows, next_cursor


def prune_events(db: Session, *, retention_days: int, batch_size: int, dry_run: bool = False) -> int:
    if retention_days < 1 or not 1 <= batch_size <= 1000:
        raise AuditValidationError("invalid audit retention bounds")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    ids = list(db.scalars(select(AuditEvent.id).where(AuditEvent.created_at < cutoff).order_by(AuditEvent.created_at, AuditEvent.id).limit(batch_size)).all())
    if dry_run:
        return len(ids)
    if ids:
        db.execute(delete(AuditEvent).where(AuditEvent.id.in_(ids)))
        db.commit()
    return len(ids)
