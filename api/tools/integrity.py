"""Database and storage integrity verification.

Usage:
    python -m tools.integrity check [--json]
    python -m tools.integrity repair-safe [--json]

Checks detect orphaned rows, missing storage objects, hash mismatches,
abandoned jobs, and other consistency violations.  repair-safe corrects
only non-destructive issues.
"""

from __future__ import annotations

import argparse
import json
import logging
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
    Chapter,
    ChapterImportStatus,
    ImportJob,
    ImportJobItem,
    ImportJobStatus,
    Page,
    PageIntegrityStatus,
    Series,
)
from storage import ObjectStorage, create_object_storage
from storage.base import StorageError

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
log = logging.getLogger("tools.integrity")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
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
            meta = None
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
    """Chapters still in 'importing' status that have verified pages (data leak risk)."""
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
                    severity=SEVERITY_CRITICAL,
                    entity_type="chapter",
                    entity_id=str(ch.id),
                    description=(
                        f"Chapter with status 'importing' has {verified_count} verified pages"
                    ),
                    repairable=True,
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
    check_missing_storage_objects,
    check_storage_size_mismatch,
    check_sha256_mismatch,
    check_invalid_image_dimensions,
    check_importing_chapters_exposed,
    check_abandoned_import_jobs,
    check_unreferenced_storage_objects,
    check_stale_replaced_objects,
]


def run_checks(
    db: Session, storage: ObjectStorage | None = None
) -> IntegritySummary:
    """Run all integrity checks and return a summary."""
    issues: list[IntegrityIssue] = []
    for check_fn in ALL_CHECKS:
        if check_fn in (check_missing_storage_objects, check_storage_size_mismatch, check_sha256_mismatch, check_unreferenced_storage_objects, check_stale_replaced_objects):
            issues.extend(check_fn(db, storage))
        else:
            issues.extend(check_fn(db))
    errors = sum(1 for i in issues if i.severity == SEVERITY_ERROR)
    warnings = sum(1 for i in issues if i.severity == SEVERITY_WARNING)
    criticals = sum(1 for i in issues if i.severity == SEVERITY_CRITICAL)
    return IntegritySummary(
        total_issues=len(issues),
        errors=errors,
        warnings=warnings,
        criticals=criticals,
        repairable=sum(1 for i in issues if i.repairable),
        issues=issues,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


# ─── Safe repair operations ──────────────────────────────────────────────────


def repair_safe(
    db: Session, storage: ObjectStorage | None = None
) -> IntegritySummary:
    """Run checks, apply non-destructive repairs, then re-check.

    Repairs performed:
      - Recalculate page_count on chapters with mismatch
      - Mark missing/mismatch integrity status on pages
      - Mark abandoned import jobs as failed
      - Quarantine chapters with importing status but verified pages

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
        issues.append(
            IntegrityIssue(
                category="page_count_mismatch",
                severity=SEVERITY_WARNING,
                entity_type="chapter",
                entity_id=str(row.id),
                description=f"Recalculated page_count: {row.page_count} -> {row.actual_count}",
                repairable=True,
                details={"old": row.page_count, "new": row.actual_count},
            )
        )

    # 2. Mark missing storage objects
    if storage is not None:
        pages = db.scalars(
            select(Page).where(
                Page.object_key.isnot(None),
                Page.object_key != "",
                Page.integrity_status != PageIntegrityStatus.missing,
            )
        ).all()
        for page in pages:
            try:
                meta = storage.head_object(page.object_key)
            except StorageError:
                meta = None
            if meta is None:
                page.integrity_status = PageIntegrityStatus.missing
                page.verified_at = None
                repaired_count += 1
                issues.append(
                    IntegrityIssue(
                        category="missing_storage_object",
                        severity=SEVERITY_CRITICAL,
                        entity_type="page",
                        entity_id=str(page.id),
                        description=f"Marked missing: {page.object_key}",
                        repairable=True,
                        details={"object_key": page.object_key},
                    )
                )

    # 3. Mark abandoned import jobs as failed
    terminal = {
        ImportJobStatus.succeeded,
        ImportJobStatus.failed,
        ImportJobStatus.cancelled,
    }
    active_statuses = [s for s in ImportJobStatus if s not in terminal]
    active_jobs = db.scalars(
        select(ImportJob).where(ImportJob.status.in_(active_statuses))
    ).all()
    now = datetime.now(timezone.utc)
    for job in active_jobs:
        ref_time = job.started_at or job.created_at
        if ref_time is None:
            continue
        age_hours = (now - ref_time.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if age_hours > 24:
            job.status = ImportJobStatus.failed
            job.finished_at = now
            job.error_summary = job.error_summary or "Marked failed by integrity repair (abandoned >24h)"
            repaired_count += 1
            issues.append(
                IntegrityIssue(
                    category="abandoned_import_job",
                    severity=SEVERITY_WARNING,
                    entity_type="import_job",
                    entity_id=str(job.id),
                    description=f"Marked failed (was '{job.status.value}' for {age_hours:.0f}h)",
                    repairable=True,
                    details={"job_id": str(job.id), "age_hours": round(age_hours, 1)},
                )
            )

    # 4. Quarantine importing chapters with verified pages
    importing_chapters = db.scalars(
        select(Chapter).where(Chapter.import_status == ChapterImportStatus.importing)
    ).all()
    for ch in importing_chapters:
        verified_count = db.scalar(
            select(func.count(Page.id)).where(
                Page.chapter_id == ch.id,
                Page.integrity_status == PageIntegrityStatus.verified,
            )
        ) or 0
        if verified_count > 0:
            ch.import_status = ChapterImportStatus.quarantined
            repaired_count += 1
            issues.append(
                IntegrityIssue(
                    category="importing_chapter_exposed",
                    severity=SEVERITY_CRITICAL,
                    entity_type="chapter",
                    entity_id=str(ch.id),
                    description=f"Quarantined (had {verified_count} verified pages)",
                    repairable=True,
                    details={"series_id": str(ch.series_id), "verified_page_count": verified_count},
                )
            )

    db.commit()
    db.expire_all()

    # Re-check to see remaining issues
    return run_checks(db, storage)


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
        summary = run_checks(db, storage)
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
            print(f"{'='*60}\n")
            for issue in summary.issues:
                tag = {"error": "ERR", "warning": "WRN", "critical": "CRI"}.get(issue.severity, "???")
                repair = " [repairable]" if issue.repairable else ""
                print(f"  [{tag}] {issue.category}: {issue.description}{repair}")
            print()

    sys.exit(1 if summary.criticals > 0 or summary.errors > 0 else 0)


def cmd_repair_safe(args: argparse.Namespace) -> None:
    """Apply non-destructive repairs, then re-check."""
    db = _build_session()
    storage = create_object_storage()
    try:
        summary = repair_safe(db, storage)
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

    sys.exit(1 if summary.criticals > 0 or summary.errors > 0 else 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m tools.integrity",
        description="Database and storage integrity verification",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Run all integrity checks")
    check_p.add_argument("--json", action="store_true", help="Output JSON")

    repair_p = sub.add_parser("repair-safe", help="Apply safe repairs and re-check")
    repair_p.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(format=LOG_FORMAT, level=level, stream=sys.stderr)

    if args.command == "check":
        cmd_check(args)
    elif args.command == "repair-safe":
        cmd_repair_safe(args)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
