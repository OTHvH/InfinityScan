"""Task 7 audit and request-correlation regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from audit_events import AuditValidationError, prune_events, record_event
from models import AuditEvent, AuditEventOutcome


def test_request_id_is_generated_and_echoed(client):
    response = client.get("/health")
    request_id = response.headers.get("X-Request-ID")
    assert request_id
    assert len(request_id) <= 128


def test_safe_request_id_is_propagated(client):
    response = client.get("/health", headers={"X-Request-ID": "client.request-42"})
    assert response.headers["X-Request-ID"] == "client.request-42"


def test_oversized_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "x" * 129})
    assert response.headers["X-Request-ID"] != "x" * 129
    assert len(response.headers["X-Request-ID"]) <= 128


def test_login_event_has_request_id_and_no_secret(client, user_factory, db):
    user_factory(username="audit_user")
    csrf_response = client.get("/auth/csrf", headers={"X-Request-ID": "audit-login"})
    response = client.post(
        "/auth/login",
        json={"username": "audit_user", "password": "strongpassword123"},
        headers={"X-CSRF-Token": csrf_response.json()["csrf_token"], "X-Request-ID": "audit-login"},
    )
    assert response.status_code == 200
    event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "auth.login",
            AuditEvent.request_id == "audit-login",
        )
    )
    assert event is not None
    assert event.outcome == AuditEventOutcome.success
    assert "password" not in event.event_metadata
    assert "token" not in event.event_metadata


def test_non_admin_audit_read_is_denied_and_recorded(client, auth_client, db):
    auth_client()
    response = client.get("/admin/audit-events")
    assert response.status_code == 403
    event = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.event_type == "admin.authorization_denied")
        .order_by(AuditEvent.created_at.desc())
    )
    assert event is not None
    assert event.outcome == AuditEventOutcome.denied
    assert event.subject_id == "admin"


def test_metadata_allowlist_rejects_secret_like_fields(db):
    with pytest.raises(AuditValidationError):
        record_event(
            db,
            event_type="auth.login",
            outcome=AuditEventOutcome.success,
            metadata={"password": "must-not-be-stored"},
        )


def test_prune_is_bounded_and_supports_dry_run(db):
    old = AuditEvent(
        created_at=datetime.now(timezone.utc) - timedelta(days=10),
        event_type="auth.login",
        outcome=AuditEventOutcome.success,
        event_metadata={},
    )
    db.add(old)
    db.commit()

    assert prune_events(db, retention_days=1, batch_size=1, dry_run=True) == 1
    assert db.scalar(select(AuditEvent).where(AuditEvent.id == old.id)) is not None
    assert prune_events(db, retention_days=1, batch_size=1) == 1
    assert db.scalar(select(AuditEvent).where(AuditEvent.id == old.id)) is None
