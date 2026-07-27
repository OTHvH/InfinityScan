#!/usr/bin/env python3
"""Seed and validate the synthetic data used by the local DR drill."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
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
    ImportJobStatus,
    Page,
    PageIntegrityStatus,
    ReadingMode,
    ReadingProgress,
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
    db = Session(engine, expire_on_commit=False)
    try:
        db.add_all([user, admin, source, series, chapter, page, job])
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
                "object_key": page_key,
                "object_sha256": upload.sha256,
                "object_size": upload.byte_size,
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
            "series": 1,
            "chapters": 1,
            "pages": 1,
            "bookmarks": 1,
            "reading_progress": 1,
            "source_series": 1,
            "import_jobs": 1,
            "audit_events": 1,
        }
        models = {
            "users": User,
            "series": Series,
            "chapters": Chapter,
            "pages": Page,
            "bookmarks": Bookmark,
            "reading_progress": ReadingProgress,
            "source_series": SourceSeries,
            "import_jobs": ImportJob,
            "audit_events": AuditEvent,
        }
        actual_counts = {name: _count(db, model) for name, model in models.items()}
        if actual_counts != expected_counts:
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
        if db.scalar(select(AuditEvent).where(AuditEvent.event_type == "import.completed")) is None:
            raise RuntimeError("restored audit event is missing")
    finally:
        db.close()
    print(json.dumps({"counts": actual_counts, "password_hash": "verified", "relationships": "verified"}))


def mutate_object(baseline_path: Path, *, restore: bool) -> None:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    storage = create_object_storage()
    if storage is None:
        raise RuntimeError("object storage is unavailable")
    if restore:
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
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.output)
    elif args.command == "validate":
        validate(args.baseline)
    else:
        mutate_object(args.baseline, restore=args.restore)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
