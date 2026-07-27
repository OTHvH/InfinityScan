"""Database and storage integrity verification.

Usage:
    python -m tools.integrity check [--mode quick|full] [--content-sha256] [--json]
    python -m tools.integrity repair-safe [--mode quick|full] [--json]
    python -m tools.integrity delete-unreferenced (--dry-run | --execute) [--json]

Checks detect orphaned rows, missing storage objects, hash mismatches,
abandoned jobs, and other consistency violations.  repair-safe corrects
only non-destructive issues.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from database import _get_engine, _get_session_local
from models import (
    AuditEventOutcome,
    Bookmark,
    Chapter,
    ChapterImportStatus,
    ImportJob,
    ImportJobItem,
    ImportJobItemStatus,
    ImportJobStatus,
    Page,
    PageIntegrityStatus,
    ReadingProgress,
    Series,
    Source,
    SourceSeries,
    User,
)
from audit_events import record_event_safe
from settings import get_settings
from storage import ObjectStorage, create_object_storage
from storage.base import StorageError
from tools.storage_integrity import (
    StorageIntegrityReport,
    delete_unreferenced_objects,
    run_storage_integrity,
)

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
log = logging.getLogger("tools.integrity")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class IntegrityIssue:
    category: str
    severity: str
    entity_type: str
    entity_id: str
    description: str
    repairable: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntegritySummary:
    total_issues: int
    errors: int
    warnings: int
    criticals: int
    repairable: int
    issues: list[IntegrityIssue]
    checked_at: str = ""
    mode: str = "quick"
    complete: bool = True
    objects_checked: int = 0
    objects_verified: int = 0
    missing: int = 0
    metadata_mismatch: int = 0
    hash_mismatch: int = 0
    orphaned: int = 0
    invalid_keys: int = 0
    duplicate_references: int = 0
    stale_objects: int = 0
    storage_errors: int = 0
    bytes_hashed: int = 0
    repairs_applied: int = 0
    elapsed_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
        d["integrity_errors"] = d["errors"]
        d["errors"] = d["storage_errors"]
        return d


# ─── Individual checks ────────────────────────────────────────────────────────


def check_orphaned_pages(db: Session) -> list[IntegrityIssue]:
    """Pages whose chapter no longer exists."""
    orphaned = db.execute(
        select(Page.id, Page.chapter_id, Page.page_number)
        .join(Chapter, Page.chapter_id == Chapter.id, isouter=True)
        .where(Chapter.id.is_(None))
    ).all()
    return [
        IntegrityIssue(
            category="orphaned_page",
            severity=SEVERITY_ERROR,
            entity_type="page",
            entity_id=str(row.id),
            description=f"Page {row.page_number} references missing chapter {row.chapter_id}",
            repairable=False,
            details={"chapter_id": str(row.chapter_id), "page_number": row.page_number},
        )
        for row in orphaned
    ]


def check_orphaned_chapters(db: Session) -> list[IntegrityIssue]:
    """Chapters whose series no longer exists."""
    orphaned = db.execute(
        select(Chapter.id, Chapter.series_id, Chapter.number)
        .join(Series, Chapter.series_id == Series.id, isouter=True)
        .where(Series.id.is_(None))
    ).all()
    return [
        IntegrityIssue(
            category="orphaned_chapter",
            severity=SEVERITY_ERROR,
            entity_type="chapter",
            entity_id=str(row.id),
            description=f"Chapter {row.number} references missing series {row.series_id}",
            repairable=False,
            details={"series_id": str(row.series_id), "number": str(row.number)},
        )
        for row in orphaned
    ]


def check_duplicate_page_positions(db: Session) -> list[IntegrityIssue]:
    """Multiple pages sharing the same page_number within a chapter."""
    dupes = db.execute(
        select(
            Page.chapter_id,
            Page.page_number,
            func.count(Page.id).label("cnt"),
        )
        .group_by(Page.chapter_id, Page.page_number)
        .having(func.count(Page.id) > 1)
    ).all()
    issues: list[IntegrityIssue] = []
    for row in dupes:
        page_ids = [
            str(p.id)
            for p in db.scalars(
                select(Page.id).where(
                    Page.chapter_id == row.chapter_id,
                    Page.page_number == row.page_number,
                )
            ).all()
        ]
        issues.append(
            IntegrityIssue(
                category="duplicate_page_position",
                severity=SEVERITY_ERROR,
                entity_type="page",
                entity_id=page_ids[0] if page_ids else "unknown",
                description=(
                    f"Chapter {row.chapter_id} has {row.cnt} pages at position {row.page_number}"
                ),
                repairable=False,
                details={
                    "chapter_id": str(row.chapter_id),
                    "page_number": row.page_number,
                    "count": row.cnt,
                    "page_ids": page_ids,
                },
            )
        )
    return issues


def check_page_count_mismatch(db: Session) -> list[IntegrityIssue]:
    """Chapter.page_count does not match actual page rows."""
    mismatches = db.execute(
        select(
            Chapter.id,
            Chapter.page_count,
            Chapter.series_id,
            func.count(Page.id).label("actual_count"),
        )
        .outerjoin(Page, Chapter.id == Page.chapter_id)
        .group_by(Chapter.id, Chapter.page_count, Chapter.series_id)
        .having(Chapter.page_count != func.count(Page.id))
    ).all()
    return [
        IntegrityIssue(
            category="page_count_mismatch",
            severity=SEVERITY_WARNING,
            entity_type="chapter",
            entity_id=str(row.id),
            description=(
                f"Chapter reports page_count={row.page_count} "
                f"but has {row.actual_count} page rows"
            ),
            repairable=True,
            details={
                "expected": row.page_count,
                "actual": row.actual_count,
                "series_id": str(row.series_id),
            },
        )
        for row in mismatches
    ]


def _verified_page_metadata_errors(page: Page) -> list[str]:
    errors: list[str] = []
    if not page.object_key or not page.object_key.strip():
        errors.append("object_key")
    if not page.sha256 or not SHA256_PATTERN.fullmatch(page.sha256):
        errors.append("sha256")
    if page.width is None or page.width <= 0:
        errors.append("width")
    if page.height is None or page.height <= 0:
        errors.append("height")
    if page.file_size is None or page.file_size <= 0:
        errors.append("file_size")
    if not page.mime_type or not page.mime_type.strip():
        errors.append("mime_type")
    if not page.file_extension or not page.file_extension.strip():
        errors.append("file_extension")
    if page.verified_at is None:
        errors.append("verified_at")
    return errors


def check_ready_chapters_without_usable_pages(db: Session) -> list[IntegrityIssue]:
    """Ready chapters must contain at least one usable verified page."""
    chapters = db.scalars(
        select(Chapter).where(Chapter.import_status == ChapterImportStatus.ready)
    ).all()
    issues: list[IntegrityIssue] = []
    for chapter in chapters:
        pages = db.scalars(select(Page).where(Page.chapter_id == chapter.id)).all()
        usable_count = sum(
            1
            for page in pages
            if page.integrity_status == PageIntegrityStatus.verified
            and not _verified_page_metadata_errors(page)
        )
        if usable_count == 0:
            issues.append(
                IntegrityIssue(
                    category="ready_chapter_without_usable_pages",
                    severity=SEVERITY_CRITICAL,
                    entity_type="chapter",
                    entity_id=str(chapter.id),
                    description="Ready chapter has no usable verified pages",
                    repairable=False,
                    details={
                        "series_id": str(chapter.series_id),
                        "page_count": chapter.page_count,
                        "page_rows": len(pages),
                    },
                )
            )
    return issues


def check_verified_pages_missing_metadata(db: Session) -> list[IntegrityIssue]:
    """Verified pages missing metadata required by imports and media delivery."""
    pages = db.scalars(
        select(Page).where(Page.integrity_status == PageIntegrityStatus.verified)
    ).all()
    issues: list[IntegrityIssue] = []
    for page in pages:
        invalid_fields = _verified_page_metadata_errors(page)
        if invalid_fields:
            issues.append(
                IntegrityIssue(
                    category="verified_page_missing_metadata",
                    severity=SEVERITY_CRITICAL,
                    entity_type="page",
                    entity_id=str(page.id),
                    description="Verified page has missing or invalid required metadata",
                    repairable=False,
                    details={
                        "chapter_id": str(page.chapter_id),
                        "invalid_fields": invalid_fields,
                    },
                )
            )
    return issues


def check_duplicate_logical_content_references(db: Session) -> list[IntegrityIssue]:
    """The same non-empty page object key must not identify multiple page rows."""
    duplicates = db.execute(
        select(Page.object_key, func.count(Page.id).label("count"))
        .where(Page.object_key != "")
        .group_by(Page.object_key)
        .having(func.count(Page.id) > 1)
    ).all()
    issues: list[IntegrityIssue] = []
    for duplicate in duplicates:
        page_ids = [
            str(page_id)
            for page_id in db.scalars(
                select(Page.id).where(Page.object_key == duplicate.object_key)
            ).all()
        ]
        issues.append(
            IntegrityIssue(
                category="duplicate_logical_content_reference",
                severity=SEVERITY_ERROR,
                entity_type="page",
                entity_id=page_ids[0],
                description="Multiple page rows reference the same object key",
                repairable=False,
                details={"object_key": duplicate.object_key, "page_ids": page_ids},
            )
        )
    return issues


def check_broken_source_mappings(db: Session) -> list[IntegrityIssue]:
    """Source mappings with missing parents or unusable external identities."""
    rows = db.execute(
        select(
            SourceSeries.id,
            SourceSeries.source_id,
            SourceSeries.series_id,
            SourceSeries.external_series_id,
            Source.id.label("existing_source_id"),
            Series.id.label("existing_series_id"),
        )
        .outerjoin(Source, SourceSeries.source_id == Source.id)
        .outerjoin(Series, SourceSeries.series_id == Series.id)
    ).all()
    issues: list[IntegrityIssue] = []
    for row in rows:
        invalid_fields: list[str] = []
        if row.existing_source_id is None:
            invalid_fields.append("source_id")
        if row.existing_series_id is None:
            invalid_fields.append("series_id")
        if not row.external_series_id or not row.external_series_id.strip():
            invalid_fields.append("external_series_id")
        if invalid_fields:
            issues.append(
                IntegrityIssue(
                    category="broken_source_mapping",
                    severity=SEVERITY_ERROR,
                    entity_type="source_series",
                    entity_id=str(row.id),
                    description="Source mapping has missing or invalid relationships",
                    repairable=False,
                    details={"invalid_fields": invalid_fields},
                )
            )
    return issues


def check_invalid_import_job_states(db: Session) -> list[IntegrityIssue]:
    """Import jobs and items whose counters, timestamps, or state data disagree."""
    terminal = {
        ImportJobStatus.succeeded,
        ImportJobStatus.partial,
        ImportJobStatus.failed,
        ImportJobStatus.cancelled,
    }
    issues: list[IntegrityIssue] = []
    for job in db.scalars(select(ImportJob)).all():
        invalid_fields: list[str] = []
        counters = (
            job.series_count,
            job.chapter_count,
            job.page_count,
            job.uploaded_count,
            job.skipped_count,
            job.failed_count,
        )
        if any(value < 0 for value in counters):
            invalid_fields.append("counters")
        if job.status in terminal and job.finished_at is None:
            invalid_fields.append("finished_at")
        if job.status not in terminal and job.finished_at is not None:
            invalid_fields.append("status")
        if job.started_at and job.created_at and job.started_at < job.created_at:
            invalid_fields.append("started_at")
        reference_time = job.started_at or job.created_at
        if job.finished_at and reference_time and job.finished_at < reference_time:
            invalid_fields.append("finished_at_order")
        if job.manifest_hash and not SHA256_PATTERN.fullmatch(job.manifest_hash):
            invalid_fields.append("manifest_hash")
        if invalid_fields:
            issues.append(
                IntegrityIssue(
                    category="invalid_import_job_state",
                    severity=SEVERITY_ERROR,
                    entity_type="import_job",
                    entity_id=str(job.id),
                    description="Import job has inconsistent state or accounting data",
                    repairable=False,
                    details={"status": job.status.value, "invalid_fields": invalid_fields},
                )
            )

    for item in db.scalars(select(ImportJobItem)).all():
        invalid_fields = []
        if item.status == ImportJobItemStatus.succeeded:
            if not item.object_key or not item.object_key.strip():
                invalid_fields.append("object_key")
            if not item.sha256 or not SHA256_PATTERN.fullmatch(item.sha256):
                invalid_fields.append("sha256")
        if item.status == ImportJobItemStatus.failed and not item.error:
            invalid_fields.append("error")
        if invalid_fields:
            issues.append(
                IntegrityIssue(
                    category="invalid_import_job_item_state",
                    severity=SEVERITY_ERROR,
                    entity_type="import_job_item",
                    entity_id=str(item.id),
                    description="Import job item has incomplete terminal state data",
                    repairable=False,
                    details={"status": item.status.value, "invalid_fields": invalid_fields},
                )
            )
    return issues


def check_progress_beyond_chapter_page_count(db: Session) -> list[IntegrityIssue]:
    """Reading progress must not point beyond its chapter's declared page count."""
    rows = db.execute(
        select(
            ReadingProgress.id,
            ReadingProgress.user_id,
            ReadingProgress.chapter_id,
            ReadingProgress.last_page,
            Chapter.page_count,
        )
        .join(Chapter, ReadingProgress.chapter_id == Chapter.id)
        .where(
            ReadingProgress.last_page.isnot(None),
            ReadingProgress.last_page > Chapter.page_count,
        )
    ).all()
    return [
        IntegrityIssue(
            category="progress_beyond_chapter_page_count",
            severity=SEVERITY_WARNING,
            entity_type="reading_progress",
            entity_id=str(row.id),
            description="Reading progress points beyond the chapter page count",
            repairable=False,
            details={
                "user_id": str(row.user_id),
                "chapter_id": str(row.chapter_id),
                "last_page": row.last_page,
                "page_count": row.page_count,
            },
        )
        for row in rows
    ]


def check_orphaned_relationships(db: Session) -> list[IntegrityIssue]:
    """Relationship rows whose parents are missing despite declared foreign keys."""
    issues: list[IntegrityIssue] = []
    bookmark_rows = db.execute(
        select(Bookmark.id, User.id.label("user_exists"), Series.id.label("series_exists"))
        .outerjoin(User, Bookmark.user_id == User.id)
        .outerjoin(Series, Bookmark.series_id == Series.id)
        .where((User.id.is_(None)) | (Series.id.is_(None)))
    ).all()
    progress_rows = db.execute(
        select(ReadingProgress.id, User.id.label("user_exists"), Chapter.id.label("chapter_exists"))
        .outerjoin(User, ReadingProgress.user_id == User.id)
        .outerjoin(Chapter, ReadingProgress.chapter_id == Chapter.id)
        .where((User.id.is_(None)) | (Chapter.id.is_(None)))
    ).all()
    item_rows = db.execute(
        select(ImportJobItem.id)
        .outerjoin(ImportJob, ImportJobItem.job_id == ImportJob.id)
        .where(ImportJob.id.is_(None))
    ).all()
    for entity_type, rows in (
        ("bookmark", bookmark_rows),
        ("reading_progress", progress_rows),
        ("import_job_item", item_rows),
    ):
        issues.extend(
            IntegrityIssue(
                category="orphaned_relationship",
                severity=SEVERITY_ERROR,
                entity_type=entity_type,
                entity_id=str(row.id),
                description=f"{entity_type} references a missing parent row",
                repairable=False,
            )
            for row in rows
        )
    return issues


def check_missing_storage_objects(
    db: Session, storage: ObjectStorage | None
) -> list[IntegrityIssue]:
    """Pages whose object_key does not exist in storage."""
    if storage is None:
        return []
    rows = db.execute(
        select(Page.id, Page.object_key, Page.chapter_id).where(
            Page.object_key.isnot(None),
            Page.object_key != "",
        )
    ).all()
    issues: list[IntegrityIssue] = []
    for row in rows:
        if not row.object_key:
            continue
        try:
            meta = storage.head_object(row.object_key)
        except StorageError:
            issues.append(
                IntegrityIssue(
                    category="storage_unavailable",
                    severity=SEVERITY_ERROR,
                    entity_type="page",
                    entity_id=str(row.id),
                    description="Storage object existence could not be verified",
                    repairable=False,
                    details={"chapter_id": str(row.chapter_id)},
                )
            )
            continue
        if meta is None:
            issues.append(
                IntegrityIssue(
                    category="missing_storage_object",
                    severity=SEVERITY_CRITICAL,
                    entity_type="page",
                    entity_id=str(row.id),
                    description=f"Object not found: {row.object_key}",
                    repairable=True,
                    details={"object_key": row.object_key, "chapter_id": str(row.chapter_id)},
                )
            )
    return issues


def check_storage_size_mismatch(
    db: Session, storage: ObjectStorage | None
) -> list[IntegrityIssue]:
    """Page.file_size does not match actual storage object size."""
    if storage is None:
        return []
    rows = db.execute(
        select(Page.id, Page.object_key, Page.file_size).where(
            Page.object_key.isnot(None),
            Page.file_size.isnot(None),
        )
    ).all()
    issues: list[IntegrityIssue] = []
    for row in rows:
        if not row.object_key:
            continue
        try:
            meta = storage.head_object(row.object_key)
        except StorageError:
            continue
        if meta is None:
            continue
        if meta.byte_size != row.file_size:
            issues.append(
                IntegrityIssue(
                    category="storage_size_mismatch",
                    severity=SEVERITY_ERROR,
                    entity_type="page",
                    entity_id=str(row.id),
                    description=(
                        f"Page file_size={row.file_size} but storage has {meta.byte_size}"
                    ),
                    repairable=False,
                    details={
                        "object_key": row.object_key,
                        "db_size": row.file_size,
                        "storage_size": meta.byte_size,
                    },
                )
            )
    return issues


def check_sha256_mismatch(
    db: Session, storage: ObjectStorage | None
) -> list[IntegrityIssue]:
    """Page.sha256 does not match the actual hash of the stored object."""
    if storage is None:
        return []
    rows = db.execute(
        select(Page.id, Page.object_key, Page.sha256).where(
            Page.sha256.isnot(None),
            Page.object_key.isnot(None),
            Page.object_key != "",
        )
    ).all()
    issues: list[IntegrityIssue] = []
    for row in rows:
        if not row.object_key or not row.sha256:
            continue
        try:
            verification = storage.verify_object(row.object_key, expected_sha256=row.sha256)
        except StorageError:
            continue
        if not verification.verified:
            issues.append(
                IntegrityIssue(
                    category="sha256_mismatch",
                    severity=SEVERITY_CRITICAL,
                    entity_type="page",
                    entity_id=str(row.id),
                    description=f"SHA-256 mismatch for {row.object_key}",
                    repairable=False,
                    details={
                        "object_key": row.object_key,
                        "expected_sha256": row.sha256,
                        "actual_sha256": verification.actual_sha256,
                    },
                )
            )
    return issues


def check_invalid_image_dimensions(db: Session) -> list[IntegrityIssue]:
    """Pages with zero, negative, or implausible image dimensions."""
    rows = db.execute(
        select(Page.id, Page.width, Page.height, Page.chapter_id).where(
            (Page.width.isnot(None) | Page.height.isnot(None))
        )
    ).all()
    issues: list[IntegrityIssue] = []
    for row in rows:
        bad = False
        reason = ""
        if row.width is not None and row.width <= 0:
            bad = True
            reason = f"width={row.width}"
        if row.height is not None and row.height <= 0:
            bad = True
            reason = f"{reason}, " if reason else ""
            reason += f"height={row.height}"
        if bad:
            issues.append(
                IntegrityIssue(
                    category="invalid_image_dimensions",
                    severity=SEVERITY_WARNING,
                    entity_type="page",
                    entity_id=str(row.id),
                    description=f"Invalid dimensions: {reason}",
                    repairable=False,
                    details={"width": row.width, "height": row.height, "chapter_id": str(row.chapter_id)},
                )
            )
    return issues


def check_importing_chapters_exposed(db: Session) -> list[IntegrityIssue]:
    """Importing chapters with verified pages require review but are not published."""
    rows = db.execute(
        select(Chapter.id, Chapter.import_status, Chapter.series_id)
        .where(Chapter.import_status == ChapterImportStatus.importing)
    ).all()
    issues: list[IntegrityIssue] = []
    for ch in rows:
        verified_count = db.scalar(
            select(func.count(Page.id)).where(
                Page.chapter_id == ch.id,
                Page.integrity_status == PageIntegrityStatus.verified,
            )
        ) or 0
        if verified_count > 0:
            issues.append(
                IntegrityIssue(
                    category="importing_chapter_exposed",
                    severity=SEVERITY_WARNING,
                    entity_type="chapter",
                    entity_id=str(ch.id),
                    description=f"Importing chapter has {verified_count} intermediate verified pages",
                    repairable=False,
                    details={
                        "series_id": str(ch.series_id),
                        "verified_page_count": verified_count,
                    },
                )
            )
    return issues


def check_abandoned_import_jobs(db: Session) -> list[IntegrityIssue]:
    """Import jobs stuck in non-terminal status with no recent activity."""
    terminal = {
        ImportJobStatus.succeeded,
        ImportJobStatus.partial,
        ImportJobStatus.failed,
        ImportJobStatus.cancelled,
    }
    active_statuses = [s for s in ImportJobStatus if s not in terminal]
    jobs = db.scalars(
        select(ImportJob).where(ImportJob.status.in_(active_statuses))
    ).all()
    issues: list[IntegrityIssue] = []
    now = datetime.now(timezone.utc)
    for job in jobs:
        if job.started_at is None and job.created_at:
            age_hours = (now - job.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        elif job.started_at:
            age_hours = (now - job.started_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        else:
            age_hours = 0
        if age_hours > 24:
            issues.append(
                IntegrityIssue(
                    category="abandoned_import_job",
                    severity=SEVERITY_WARNING,
                    entity_type="import_job",
                    entity_id=str(job.id),
                    description=f"Import job stuck in '{job.status.value}' for {age_hours:.0f}h",
                    repairable=True,
                    details={
                        "status": job.status.value,
                        "age_hours": round(age_hours, 1),
                        "idempotency_key": job.idempotency_key,
                    },
                )
            )
    return issues


def check_unreferenced_storage_objects(
    db: Session, storage: ObjectStorage | None
) -> list[IntegrityIssue]:
    """Objects in storage that are not referenced by any page or cover."""
    if storage is None:
        return []
    referenced_keys = set()
    page_keys = db.scalars(select(Page.object_key).where(Page.object_key.isnot(None))).all()
    for k in page_keys:
        if k:
            referenced_keys.add(k)
    cover_keys = db.scalars(
        select(Series.cover_object_key).where(Series.cover_object_key.isnot(None))
    ).all()
    for k in cover_keys:
        if k:
            referenced_keys.add(k)
    job_keys = db.scalars(
        select(ImportJobItem.object_key).where(ImportJobItem.object_key.isnot(None))
    ).all()
    for k in job_keys:
        if k:
            referenced_keys.add(k)
    if not referenced_keys:
        return []
    issues: list[IntegrityIssue] = []
    if not hasattr(storage, "list_objects"):
        return issues
    for prefix in ("series/",):
        try:
            listing = storage.list_objects(prefix=prefix, max_keys=10000)
        except (StorageError, AttributeError):
            continue
        for obj_key in listing:
            if obj_key not in referenced_keys:
                issues.append(
                    IntegrityIssue(
                        category="unreferenced_storage_object",
                        severity=SEVERITY_WARNING,
                        entity_type="storage_object",
                        entity_id=obj_key,
                        description=f"Object not referenced: {obj_key}",
                        repairable=False,
                        details={"object_key": obj_key},
                    )
                )
    return issues


def check_stale_replaced_objects(
    db: Session, storage: ObjectStorage | None
) -> list[IntegrityIssue]:
    """Objects in storage with older etag than what the DB records."""
    if storage is None:
        return []
    rows = db.execute(
        select(Page.id, Page.object_key, Page.storage_etag).where(
            Page.storage_etag.isnot(None),
            Page.object_key.isnot(None),
            Page.object_key != "",
        )
    ).all()
    issues: list[IntegrityIssue] = []
    for row in rows:
        if not row.object_key or not row.storage_etag:
            continue
        try:
            meta = storage.head_object(row.object_key)
        except StorageError:
            continue
        if meta is None:
            continue
        if meta.etag and meta.etag != row.storage_etag:
            issues.append(
                IntegrityIssue(
                    category="stale_replaced_object",
                    severity=SEVERITY_WARNING,
                    entity_type="page",
                    entity_id=str(row.id),
                    description=f"Object etag changed: {row.storage_etag} -> {meta.etag}",
                    repairable=False,
                    details={
                        "object_key": row.object_key,
                        "db_etag": row.storage_etag,
                        "storage_etag": meta.etag,
                    },
                )
            )
    return issues


# ─── Aggregate check runner ───────────────────────────────────────────────────

ALL_CHECKS = [
    check_orphaned_pages,
    check_orphaned_chapters,
    check_duplicate_page_positions,
    check_page_count_mismatch,
    check_ready_chapters_without_usable_pages,
    check_verified_pages_missing_metadata,
    check_broken_source_mappings,
    check_invalid_import_job_states,
    check_progress_beyond_chapter_page_count,
    check_orphaned_relationships,
    check_invalid_image_dimensions,
    check_abandoned_import_jobs,
]


def _build_summary(
    issues: list[IntegrityIssue],
    storage_report: StorageIntegrityReport | None = None,
) -> IntegritySummary:
    report = storage_report or StorageIntegrityReport(
        mode="quick",
        content_sha256=False,
        issue_limit=1,
    )
    return IntegritySummary(
        total_issues=len(issues),
        errors=sum(1 for issue in issues if issue.severity == SEVERITY_ERROR),
        warnings=sum(1 for issue in issues if issue.severity == SEVERITY_WARNING),
        criticals=sum(1 for issue in issues if issue.severity == SEVERITY_CRITICAL),
        repairable=sum(1 for issue in issues if issue.repairable),
        issues=issues,
        checked_at=datetime.now(timezone.utc).isoformat(),
        mode=report.mode,
        complete=report.complete,
        objects_checked=report.objects_checked,
        objects_verified=report.objects_verified,
        missing=report.missing,
        metadata_mismatch=report.metadata_mismatch,
        hash_mismatch=report.hash_mismatch,
        orphaned=report.orphaned,
        invalid_keys=report.invalid_keys,
        duplicate_references=report.duplicate_references,
        stale_objects=report.stale_objects,
        storage_errors=report.errors,
        bytes_hashed=report.bytes_hashed,
        repairs_applied=report.repairs_applied,
        elapsed_time=report.elapsed_time,
    )


def _storage_issues(report: StorageIntegrityReport) -> list[IntegrityIssue]:
    return [
        IntegrityIssue(
            category=issue.category,
            severity=issue.severity,
            entity_type="storage_object",
            entity_id=issue.entity_id,
            description=issue.description,
            repairable=issue.repairable,
            details=issue.details,
        )
        for issue in report.issues
    ]


def run_checks(
    db: Session,
    storage: ObjectStorage | None = None,
    *,
    mode: str = "quick",
    content_sha256: bool = False,
    max_concurrency: int = 8,
    inventory_page_size: int = 1000,
    issue_limit: int = 1000,
) -> IntegritySummary:
    """Run all integrity checks and return a summary."""
    issues: list[IntegrityIssue] = []
    for check_fn in ALL_CHECKS:
        issues.extend(check_fn(db))
    storage_report = None
    if storage is not None:
        storage_report = run_storage_integrity(
            db,
            storage,
            mode=mode,
            content_sha256=content_sha256,
            max_concurrency=max_concurrency,
            inventory_page_size=inventory_page_size,
            issue_limit=issue_limit,
        )
        issues.extend(_storage_issues(storage_report))
    return _build_summary(issues, storage_report)


# ─── Safe repair operations ──────────────────────────────────────────────────


def repair_safe(
    db: Session,
    storage: ObjectStorage | None = None,
    *,
    mode: str = "quick",
    content_sha256: bool = False,
    max_concurrency: int = 8,
    inventory_page_size: int = 1000,
    issue_limit: int = 1000,
) -> IntegritySummary:
    """Run checks, apply non-destructive repairs, then re-check.

    Repairs performed:
      - Recalculate page_count on chapters with mismatch
      - Mark conclusive missing/mismatch integrity status on pages

    Never deletes objects, metadata, or hashes.
    Never publishes partial chapters.
    """
    issues: list[IntegrityIssue] = []
    repaired_count = 0

    # 1. Recalculate page_count
    mismatch_chapters = db.execute(
        select(
            Chapter.id,
            Chapter.page_count,
            func.count(Page.id).label("actual_count"),
        )
        .outerjoin(Page, Chapter.id == Page.chapter_id)
        .group_by(Chapter.id, Chapter.page_count)
        .having(Chapter.page_count != func.count(Page.id))
    ).all()
    for row in mismatch_chapters:
        chapter = db.get(Chapter, row.id)
        if chapter is not None:
            chapter.page_count = row.actual_count
        else:
            db.execute(
                text("UPDATE chapters SET page_count = :cnt WHERE id = :id"),
                {"cnt": row.actual_count, "id": str(row.id)},
            )
        repaired_count += 1

    db.commit()
    event = record_event_safe(
        db,
        event_type="integrity.repaired",
        outcome=AuditEventOutcome.success,
        subject_type="integrity_run",
        subject_id=mode,
        metadata={"item_count": repaired_count, "status": "completed"},
    )
    if event is not None:
        db.commit()
    storage_report = None
    if storage is not None:
        storage_report = run_storage_integrity(
            db,
            storage,
            mode=mode,
            content_sha256=content_sha256,
            max_concurrency=max_concurrency,
            inventory_page_size=inventory_page_size,
            issue_limit=issue_limit,
            repair=True,
        )
        issues.extend(_storage_issues(storage_report))
    db.expire_all()
    relational_issues: list[IntegrityIssue] = []
    for check_fn in ALL_CHECKS:
        relational_issues.extend(check_fn(db))
    issues.extend(relational_issues)
    if storage_report is not None:
        storage_report.repairs_applied += repaired_count
    else:
        storage_report = StorageIntegrityReport(
            mode="quick",
            content_sha256=False,
            issue_limit=issue_limit,
            repairs_applied=repaired_count,
        )
    return _build_summary(issues, storage_report)


# ─── Admin API helpers ────────────────────────────────────────────────────────


def list_import_jobs(
    db: Session,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return paginated import jobs with summary stats."""
    total = db.scalar(select(func.count()).select_from(ImportJob)) or 0
    jobs = db.scalars(
        select(ImportJob)
        .order_by(ImportJob.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "jobs": [
            {
                "id": str(j.id),
                "status": j.status.value,
                "source_id": str(j.source_id) if j.source_id else None,
                "idempotency_key": j.idempotency_key,
                "series_count": j.series_count,
                "chapter_count": j.chapter_count,
                "page_count": j.page_count,
                "uploaded_count": j.uploaded_count,
                "failed_count": j.failed_count,
                "error_summary": j.error_summary,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            }
            for j in jobs
        ],
    }


def get_import_job(db: Session, job_id: uuid.UUID) -> dict[str, Any] | None:
    """Return a single import job with its items."""
    job = db.scalar(select(ImportJob).where(ImportJob.id == job_id))
    if job is None:
        return None
    items = db.scalars(
        select(ImportJobItem)
        .where(ImportJobItem.job_id == job.id)
        .order_by(ImportJobItem.created_at)
    ).all()
    return {
        "id": str(job.id),
        "status": job.status.value,
        "source_id": str(job.source_id) if job.source_id else None,
        "requested_by_user_id": str(job.requested_by_user_id) if job.requested_by_user_id else None,
        "idempotency_key": job.idempotency_key,
        "manifest_hash": job.manifest_hash,
        "series_count": job.series_count,
        "chapter_count": job.chapter_count,
        "page_count": job.page_count,
        "uploaded_count": job.uploaded_count,
        "skipped_count": job.skipped_count,
        "failed_count": job.failed_count,
        "error_summary": job.error_summary,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "items": [
            {
                "id": str(item.id),
                "source_reference": item.source_reference,
                "object_key": item.object_key,
                "sha256": item.sha256,
                "status": item.status.value,
                "error": item.error,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in items
        ],
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────


def _build_session() -> Session:
    engine = _get_engine()
    if engine is None:
        log.error("DATABASE_URL is not configured")
        sys.exit(1)
    factory = _get_session_local()
    if factory is None:
        log.error("Session factory unavailable")
        sys.exit(1)
    return factory()


def cmd_check(args: argparse.Namespace) -> None:
    """Run all integrity checks and report results."""
    db = _build_session()
    storage = create_object_storage()
    try:
        cfg = get_settings()
        summary = run_checks(
            db,
            storage,
            mode=args.mode,
            content_sha256=args.content_sha256,
            max_concurrency=args.concurrency or cfg.integrity_max_concurrency,
            inventory_page_size=cfg.integrity_inventory_page_size,
            issue_limit=cfg.integrity_issue_limit,
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, default=str))
    else:
        if summary.total_issues == 0:
            print("All checks passed. No issues found.")
        else:
            print(f"\n{'='*60}")
            print(f"  Integrity Report — {summary.checked_at}")
            print(f"{'='*60}")
            print(f"  Total issues:  {summary.total_issues}")
            print(f"  Critical:      {summary.criticals}")
            print(f"  Errors:        {summary.errors}")
            print(f"  Warnings:      {summary.warnings}")
            print(f"  Repairable:    {summary.repairable}")
            print(f"  Mode:          {summary.mode}")
            print(f"  Objects:       {summary.objects_checked} checked, {summary.objects_verified} verified")
            print(f"  Missing:       {summary.missing}")
            print(f"  Metadata:      {summary.metadata_mismatch} mismatch")
            print(f"  Hash:          {summary.hash_mismatch} mismatch")
            print(f"  Orphaned:      {summary.orphaned}")
            print(f"  Invalid keys:  {summary.invalid_keys}")
            print(f"  Storage errors:{summary.storage_errors}")
            print(f"  Elapsed:       {summary.elapsed_time:.3f}s")
            print(f"{'='*60}\n")
            for issue in summary.issues:
                tag = {"error": "ERR", "warning": "WRN", "critical": "CRI"}.get(issue.severity, "???")
                repair = " [repairable]" if issue.repairable else ""
                print(f"  [{tag}] {issue.category}: {issue.description}{repair}")
            print()

    if not summary.complete:
        sys.exit(2)
    sys.exit(1 if summary.criticals > 0 or summary.errors > 0 else 0)


def cmd_repair_safe(args: argparse.Namespace) -> None:
    """Apply non-destructive repairs, then re-check."""
    db = _build_session()
    storage = create_object_storage()
    try:
        cfg = get_settings()
        summary = repair_safe(
            db,
            storage,
            mode=args.mode,
            content_sha256=args.content_sha256,
            max_concurrency=args.concurrency or cfg.integrity_max_concurrency,
            inventory_page_size=cfg.integrity_inventory_page_size,
            issue_limit=cfg.integrity_issue_limit,
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, default=str))
    else:
        print(f"\n{'='*60}")
        print(f"  Repair-Safe Complete — {summary.checked_at}")
        print(f"{'='*60}")
        print(f"  Remaining issues: {summary.total_issues}")
        print(f"  Critical:         {summary.criticals}")
        print(f"  Errors:           {summary.errors}")
        print(f"  Warnings:         {summary.warnings}")
        print(f"{'='*60}\n")
        if summary.total_issues > 0:
            for issue in summary.issues:
                tag = {"error": "ERR", "warning": "WRN", "critical": "CRI"}.get(issue.severity, "???")
                print(f"  [{tag}] {issue.category}: {issue.description}")
        else:
            print("  All issues resolved.")
        print()

    if not summary.complete:
        sys.exit(2)
    sys.exit(1 if summary.criticals > 0 or summary.errors > 0 else 0)


def cmd_delete_unreferenced(args: argparse.Namespace) -> None:
    """Run the explicit, separately safeguarded orphan deletion operation."""
    cfg = get_settings()
    storage = create_object_storage()
    if storage is None:
        print("Object storage is not configured", file=sys.stderr)
        sys.exit(2)
    if args.execute and args.confirm_bucket != cfg.s3_bucket:
        print("--confirm-bucket must exactly match the configured bucket", file=sys.stderr)
        sys.exit(2)
    db = _build_session()
    try:
        report = delete_unreferenced_objects(
            db,
            storage,
            execute=args.execute,
            inventory_page_size=cfg.integrity_inventory_page_size,
            minimum_age_seconds=cfg.integrity_delete_min_age_seconds,
        )
    finally:
        db.close()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        action = "Deletion" if args.execute else "Dry-run"
        print(f"{action} complete: {report.candidates} candidates, {report.deleted} deleted")
    sys.exit(0 if report.complete else 2)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tools.integrity",
        description="Database and storage integrity verification",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Run all integrity checks")
    check_p.add_argument("--json", action="store_true", help="Output JSON")
    check_p.add_argument("--mode", choices=("quick", "full"), default="quick")
    check_p.add_argument("--content-sha256", action="store_true", help="Stream and hash object bytes in full mode")
    check_p.add_argument("--concurrency", type=int, help="Maximum concurrent object requests")

    repair_p = sub.add_parser("repair-safe", help="Apply safe repairs and re-check")
    repair_p.add_argument("--json", action="store_true", help="Output JSON")
    repair_p.add_argument("--mode", choices=("quick", "full"), default="quick")
    repair_p.add_argument("--content-sha256", action="store_true", help="Stream and hash object bytes in full mode")
    repair_p.add_argument("--concurrency", type=int, help="Maximum concurrent object requests")

    delete_p = sub.add_parser(
        "delete-unreferenced",
        help="Explicitly dry-run or delete old unreferenced managed objects",
    )
    delete_mode = delete_p.add_mutually_exclusive_group(required=True)
    delete_mode.add_argument("--dry-run", action="store_true")
    delete_mode.add_argument("--execute", action="store_true")
    delete_p.add_argument("--confirm-bucket")
    delete_p.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()
    if getattr(args, "content_sha256", False) and args.mode != "full":
        parser.error("--content-sha256 requires --mode full")
    if getattr(args, "concurrency", None) is not None and not 1 <= args.concurrency <= 32:
        parser.error("--concurrency must be between 1 and 32")

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format=LOG_FORMAT, level=level, stream=sys.stderr)

    if args.command == "check":
        cmd_check(args)
    elif args.command == "repair-safe":
        cmd_repair_safe(args)
    elif args.command == "delete-unreferenced":
        cmd_delete_unreferenced(args)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
