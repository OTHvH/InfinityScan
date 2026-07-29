#!/usr/bin/env python3
"""Seed and validate the synthetic data used by the local DR drill."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth import hash_password, verify_password
from database import _get_engine
from models import (
    AuditEventOutcome,
    AuditEvent,
    Bookmark,
    Chapter,
    ChapterImportStatus,
    ContentType,
    ImportJob,
    ImportJobItem,
    ImportJobItemStatus,
    ImportJobStatus,
    Page,
    PageIntegrityStatus,
    ReadingMode,
    ReadingProgress,
    RefreshSession,
    Series,
    SeriesStatus,
    Source,
    SourceSeries,
    User,
    UserRole,
)
from storage import create_object_storage, page_object_key
from audit_events import record_event


USER_PASSWORD = os.environ.get("DR_USER_PASSWORD", "Drill-only-password-42")
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _engine():
    engine = _get_engine()
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured")
    return engine


def _count(db: Session, model) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def seed(output: Path) -> None:
    engine = _engine()
    storage = create_object_storage()
    if storage is None or not storage.health_check():
        raise RuntimeError("disposable object storage is unavailable")
    now = datetime.now(timezone.utc)
    user = User(
        id=uuid.uuid4(),
        username="drill_user",
        email="drill-user@example.invalid",
        hashed_password=hash_password(USER_PASSWORD),
        role=UserRole.user,
        is_active=True,
    )
    admin = User(
        id=uuid.uuid4(),
        username="drill_admin",
        email="drill-admin@example.invalid",
        hashed_password=hash_password("Drill-admin-password-42"),
        role=UserRole.admin,
        is_active=True,
    )
    source = Source(
        id=uuid.uuid4(),
        key="drill-source",
        display_name="Disaster Recovery Synthetic Source",
        adapter_type="local",
        enabled=True,
        configuration={},
    )
    series = Series(
        id=uuid.uuid4(),
        slug="drill-series",
        title="Disaster Recovery Synthetic Series",
        content_type=ContentType.manga,
        default_reading_mode=ReadingMode.paged,
        status=SeriesStatus.ongoing,
        is_nsfw=False,
    )
    chapter = Chapter(
        id=uuid.uuid4(),
        series=series,
        number=Decimal("1"),
        title="Recovery Chapter",
        language="en",
        page_count=1,
        import_status=ChapterImportStatus.ready,
        verified_at=now,
        published_at=now,
    )
    page_digest = hashlib.sha256(PNG_BYTES).hexdigest()
    page_key = page_object_key(series.id, chapter.id, 1, page_digest, "png")
    upload = storage.upload_bytes(page_key, PNG_BYTES, "image/png")
    page = Page(
        id=uuid.uuid4(),
        chapter=chapter,
        page_number=1,
        object_key=page_key,
        width=1,
        height=1,
        file_size=upload.byte_size,
        sha256=upload.sha256,
        mime_type=upload.mime_type,
        file_extension="png",
        integrity_status=PageIntegrityStatus.verified,
        verified_at=now,
        imported_at=now,
        storage_etag=upload.etag,
    )
    job = ImportJob(
        id=uuid.uuid4(),
        source=source,
        requested_by_user=user,
        status=ImportJobStatus.succeeded,
        idempotency_key="drill-recovery-job",
        checkpoint={"version": 1, "phase": "succeeded"},
        series_count=1,
        chapter_count=1,
        page_count=1,
        uploaded_count=1,
        skipped_count=0,
        failed_count=0,
        created_at=now,
        finished_at=now,
        updated_at=now,
    )
    job_item = ImportJobItem(
        job=job,
        source_reference="drill-series/chapter-1/page-1.png",
        item_key="page:drill-series:1:1",
        item_kind="page",
        series_id=series.id,
        chapter_id=chapter.id,
        page_number=1,
        object_key=page_key,
        sha256=upload.sha256,
        byte_size=upload.byte_size,
        mime_type=upload.mime_type,
        file_extension="png",
        storage_etag=upload.etag,
        attempt_count=1,
        verified_at=now,
        status=ImportJobItemStatus.succeeded,
        created_at=now,
        updated_at=now,
    )
    refresh_session = RefreshSession(
        user=user,
        family_id=uuid.uuid4(),
        token_hash=hashlib.sha256(b"drill-refresh-token").hexdigest(),
        created_at=now,
        expires_at=now + timedelta(days=365),
        user_agent="InfinityScan DR drill",
    )
    db = Session(engine, expire_on_commit=False)
    try:
        db.add_all([user, admin, source, series, chapter, page, job, job_item, refresh_session])
        db.flush()
        db.add(SourceSeries(source=source, series=series, external_series_id="drill-external-series"))
        db.add(Bookmark(user=user, series=series))
        db.add(
            ReadingProgress(
                user=user,
                chapter=chapter,
                last_page=1,
                scroll_position=0.5,
                completed=False,
                updated_at=now,
            )
        )
        record_event(
            db,
            event_type="import.completed",
            outcome=AuditEventOutcome.success,
            actor_user_id=user.id,
            subject_type="import_job",
            subject_id=str(job.id),
            metadata={"status": "succeeded", "item_count": 1, "page_count": 1},
        )
        db.commit()
        count_models = {
            "users": User,
            "refresh_sessions": RefreshSession,
            "series": Series,
            "chapters": Chapter,
            "pages": Page,
            "bookmarks": Bookmark,
            "reading_progress": ReadingProgress,
            "sources": Source,
            "source_series": SourceSeries,
            "import_jobs": ImportJob,
            "import_job_items": ImportJobItem,
            "audit_events": AuditEvent,
        }
        counts = {name: _count(db, model) for name, model in count_models.items()}
    finally:
        db.close()
    output.write_text(
        json.dumps(
            {
                "user_id": str(user.id),
                "admin_id": str(admin.id),
                "series_id": str(series.id),
                "chapter_id": str(chapter.id),
                "page_id": str(page.id),
                "job_id": str(job.id),
                "refresh_session_id": str(refresh_session.id),
                "import_job_item_id": str(job_item.id),
                "object_key": page_key,
                "object_sha256": upload.sha256,
                "object_size": upload.byte_size,
                "orphan_key": "series/drill-orphan/object.png",
                "counts": counts,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def validate(baseline_path: Path) -> None:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    db = Session(_engine())
    try:
        expected_counts = {
            "users": 2,
            "refresh_sessions": 1,
            "series": 1,
            "chapters": 1,
            "pages": 1,
            "bookmarks": 1,
            "reading_progress": 1,
            "sources": 1,
            "source_series": 1,
            "import_jobs": 1,
            "import_job_items": 1,
            "audit_events": 1,
        }
        models = {
            "users": User,
            "refresh_sessions": RefreshSession,
            "series": Series,
            "chapters": Chapter,
            "pages": Page,
            "bookmarks": Bookmark,
            "reading_progress": ReadingProgress,
            "sources": Source,
            "source_series": SourceSeries,
            "import_jobs": ImportJob,
            "import_job_items": ImportJobItem,
            "audit_events": AuditEvent,
        }
        actual_counts = {name: _count(db, model) for name, model in models.items()}
        if actual_counts != expected_counts or baseline.get("counts") != expected_counts:
            raise RuntimeError(f"restored row counts differ: {actual_counts}")
        user = db.get(User, uuid.UUID(baseline["user_id"]))
        if user is None or not verify_password(USER_PASSWORD, user.hashed_password):
            raise RuntimeError("restored user password hash did not verify")
        page = db.get(Page, uuid.UUID(baseline["page_id"]))
        chapter = db.get(Chapter, uuid.UUID(baseline["chapter_id"]))
        if page is None or chapter is None or page.chapter_id != chapter.id:
            raise RuntimeError("restored chapter/page relationship is invalid")
        if db.scalar(select(Bookmark).where(Bookmark.user_id == user.id, Bookmark.series_id == baseline["series_id"])) is None:
            raise RuntimeError("restored bookmark ownership is invalid")
        if db.scalar(select(ReadingProgress).where(ReadingProgress.user_id == user.id, ReadingProgress.chapter_id == chapter.id)) is None:
            raise RuntimeError("restored reading progress is missing")
        session = db.get(RefreshSession, uuid.UUID(baseline["refresh_session_id"]))
        if session is None or session.user_id != user.id or len(session.token_hash) != 64:
            raise RuntimeError("restored refresh session is invalid")
        item = db.get(ImportJobItem, uuid.UUID(baseline["import_job_item_id"]))
        if item is None or item.job_id != uuid.UUID(baseline["job_id"]) or item.chapter_id != chapter.id:
            raise RuntimeError("restored import job/page relationship is invalid")
        if db.scalar(select(AuditEvent).where(AuditEvent.event_type == "import.completed")) is None:
            raise RuntimeError("restored audit event is missing")
    finally:
        db.close()
    print(json.dumps({"counts": actual_counts, "password_hash": "verified", "relationships": "verified"}))


def inventory(output: Path) -> None:
    storage = create_object_storage()
    if storage is None or not storage.health_check():
        raise RuntimeError("object storage is unavailable")
    objects = []
    token = None
    while True:
        page = storage.list_objects_page(prefix="series/", continuation_token=token, max_keys=1000)
        objects.extend(
            {
                "key": item.key,
                "byte_size": item.byte_size,
                "mime_type": item.mime_type,
                "sha256": item.sha256,
                "etag": item.etag,
            }
            for item in page.objects
        )
        if not page.next_token:
            break
        token = page.next_token
    output.write_text(
        json.dumps({"object_count": len(objects), "objects": objects}, indent=2) + "\n",
        encoding="utf-8",
    )


def mutate_object(
    baseline_path: Path,
    *,
    restore: bool,
    orphan: bool,
    cleanup_orphan: bool,
) -> None:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    storage = create_object_storage()
    if storage is None:
        raise RuntimeError("object storage is unavailable")
    if orphan or cleanup_orphan:
        orphan_key = baseline["orphan_key"]
        if orphan:
            storage.upload_bytes(orphan_key, PNG_BYTES, "image/png")
        elif not storage.delete_object(orphan_key):
            raise RuntimeError("synthetic orphan object could not be deleted")
    elif restore:
        upload = storage.upload_bytes(baseline["object_key"], PNG_BYTES, "image/png")
        if upload.sha256 != baseline["object_sha256"]:
            raise RuntimeError("restored object checksum differs")
    elif not storage.delete_object(baseline["object_key"]):
        raise RuntimeError("synthetic object could not be deleted")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    seed_parser = sub.add_parser("seed")
    seed_parser.add_argument("--output", type=Path, required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--baseline", type=Path, required=True)
    object_parser = sub.add_parser("object")
    object_parser.add_argument("--baseline", type=Path, required=True)
    object_parser.add_argument("--restore", action="store_true")
    object_parser.add_argument("--orphan", action="store_true")
    object_parser.add_argument("--cleanup-orphan", action="store_true")
    inventory_parser = sub.add_parser("inventory")
    inventory_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.output)
    elif args.command == "validate":
        validate(args.baseline)
    elif args.command == "inventory":
        inventory(args.output)
    else:
        mutate_object(
            args.baseline,
            restore=args.restore,
            orphan=args.orphan,
            cleanup_orphan=args.cleanup_orphan,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
