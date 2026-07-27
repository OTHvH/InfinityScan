"""Task 5 tests for bounded refresh-session maintenance."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy import text

from auth import hash_password
from models import RefreshSession, User, UserRole
from settings import Settings
from tools.sessions import cleanup_sessions, inspect_sessions, revoke_expired


@pytest.fixture(autouse=True)
def _enable_sqlite_foreign_keys(db):
    """Keep this module's valid-data cases independent of prior corruption tests."""
    if db.get_bind().dialect.name == "sqlite":
        db.commit()
        db.execute(text("PRAGMA foreign_keys=ON"))
        db.commit()


def _user(db, username: str = "session-tool-user") -> User:
    user = User(
        username=username,
        hashed_password=hash_password("strongpassword123"),
        role=UserRole.user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _session(
    db,
    user: User,
    *,
    created_at: datetime,
    expires_at: datetime,
    revoked_at: datetime | None = None,
    family_id: uuid.UUID | None = None,
) -> RefreshSession:
    row = RefreshSession(
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        created_at=created_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_status_reports_disjoint_lifecycle_counts(db):
    now = datetime.now(timezone.utc)
    user = _user(db)
    _session(db, user, created_at=now - timedelta(hours=1), expires_at=now + timedelta(days=1))
    _session(db, user, created_at=now - timedelta(days=3), expires_at=now - timedelta(days=1))
    _session(
        db,
        user,
        created_at=now - timedelta(days=3),
        expires_at=now + timedelta(days=1),
        revoked_at=now - timedelta(days=2),
    )

    report = inspect_sessions(
        db,
        settings=Settings(session_retention_days=1, session_maintenance_batch_size=2),
        checked_at=now,
    )

    assert (report.inspected, report.active, report.expired, report.revoked) == (3, 1, 1, 1)
    assert report.active + report.expired + report.revoked == report.inspected
    # The expired row is exactly at the strict retention boundary and waits.
    assert report.eligible_for_cleanup == 1


def test_revoke_expired_is_bounded_idempotent_and_dry_run_safe(db):
    now = datetime.now(timezone.utc)
    user = _user(db, "revoke-expired-user")
    rows = [
        _session(
            db,
            user,
            created_at=now - timedelta(days=2),
            expires_at=now - timedelta(hours=1),
        )
        for _ in range(3)
    ]
    cfg = Settings(session_retention_days=30, session_maintenance_batch_size=2)

    dry = revoke_expired(db, settings=cfg, dry_run=True, checked_at=now)
    assert dry.revoked_now == 0
    assert dry.expired == 3
    assert all(row.revoked_at is None for row in db.scalars(select(RefreshSession)).all())

    result = revoke_expired(db, settings=cfg, checked_at=now)
    assert result.revoked_now == 3
    assert result.batches == 2
    again = revoke_expired(db, settings=cfg, checked_at=now + timedelta(seconds=1))
    assert again.revoked_now == 0
    assert len(rows) == 3


def test_cleanup_dry_run_preserves_records_and_apply_is_bounded(db):
    now = datetime.now(timezone.utc)
    user = _user(db, "cleanup-user")
    [
        _session(
            db,
            user,
            created_at=now - timedelta(days=10),
            expires_at=now - timedelta(days=9),
            revoked_at=now - timedelta(days=8),
        )
        for _ in range(3)
    ]
    active = _session(
        db,
        user,
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
    )
    cfg = Settings(session_retention_days=2, session_maintenance_batch_size=2)

    dry = cleanup_sessions(db, settings=cfg, dry_run=True, checked_at=now)
    assert dry.eligible_for_cleanup == 3
    assert dry.removed == 0
    assert db.query(RefreshSession).count() == 4

    result = cleanup_sessions(db, settings=cfg, checked_at=now)
    assert result.removed == 3
    assert result.batches == 2
    assert db.get(RefreshSession, active.id) is not None
    assert db.query(RefreshSession).count() == 1


def test_cleanup_retains_referenced_target_until_predecessor_is_removed(db):
    now = datetime.now(timezone.utc)
    user = _user(db, "chain-user")
    family = uuid.uuid4()
    predecessor = _session(
        db,
        user,
        created_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=9),
        revoked_at=now - timedelta(days=8),
        family_id=family,
    )
    successor = _session(
        db,
        user,
        created_at=now - timedelta(days=9),
        expires_at=now + timedelta(days=10),
        family_id=family,
    )
    predecessor.replaced_by_session_id = successor.id
    predecessor.last_used_at = predecessor.revoked_at
    predecessor_id = predecessor.id
    successor_id = successor.id
    db.commit()

    result = cleanup_sessions(
        db,
        settings=Settings(session_retention_days=2, session_maintenance_batch_size=10),
        checked_at=now,
    )

    assert result.removed == 1
    assert db.get(RefreshSession, predecessor_id) is None
    assert db.get(RefreshSession, successor_id) is not None


def test_status_detects_replacement_cycle_and_blocks_mutation(db):
    now = datetime.now(timezone.utc)
    user = _user(db, "cycle-user")
    first = _session(
        db,
        user,
        created_at=now - timedelta(days=4),
        expires_at=now - timedelta(days=3),
        revoked_at=now - timedelta(days=2),
    )
    second = _session(
        db,
        user,
        created_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
        revoked_at=now - timedelta(days=1),
    )
    first.replaced_by_session_id = second.id
    second.replaced_by_session_id = first.id
    db.commit()

    cfg = Settings(session_retention_days=1, session_maintenance_batch_size=2)
    report = inspect_sessions(db, settings=cfg, checked_at=now)
    result = cleanup_sessions(db, settings=cfg, checked_at=now)

    assert report.replacement_cycles == 1
    assert report.sessions_in_cycles == 2
    assert result.blocked is True
    assert db.query(RefreshSession).count() == 2


def test_status_detects_cross_family_replacement(db):
    now = datetime.now(timezone.utc)
    user = _user(db, "cross-family-user")
    first = _session(
        db,
        user,
        created_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
        revoked_at=now - timedelta(days=1),
    )
    second = _session(
        db,
        user,
        created_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
        revoked_at=now,
    )
    first.replaced_by_session_id = second.id
    db.commit()
    second.family_id = uuid.uuid4()
    db.commit()

    report = inspect_sessions(db, settings=Settings(session_retention_days=1), checked_at=now)
    assert report.impossible_replacements == 1
    assert report.blocked is True


def test_status_detects_missing_user_and_blocks_cleanup(db):
    now = datetime.now(timezone.utc)
    db.commit()
    db.execute(text("PRAGMA foreign_keys=OFF"))
    db.commit()
    orphan_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO refresh_sessions "
            "(id, user_id, family_id, token_hash, created_at, expires_at) "
            "VALUES (:id, :user_id, :family_id, :token_hash, :created_at, :expires_at)"
        ),
        {
            "id": str(orphan_id),
            "user_id": str(uuid.uuid4()),
            "family_id": str(uuid.uuid4()),
            "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,
            "created_at": now - timedelta(days=4),
            "expires_at": now - timedelta(days=3),
        },
    )
    db.commit()
    db.execute(text("PRAGMA foreign_keys=ON"))
    db.commit()

    report = inspect_sessions(db, settings=Settings(session_retention_days=1), checked_at=now)
    result = cleanup_sessions(db, settings=Settings(session_retention_days=1), checked_at=now)

    assert report.missing_users == 1
    assert report.blocked is True
    assert result.removed == 0
    assert db.execute(
        text("SELECT count(*) FROM refresh_sessions WHERE id = :id"), {"id": str(orphan_id)}
    ).scalar() == 1
