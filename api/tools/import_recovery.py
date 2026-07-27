"""Inspect and safely resume stale durable import jobs.

Usage:
    python -m tools.import_recovery scan
    python -m tools.import_recovery scan --json
    python -m tools.import_recovery recover-safe
    python -m tools.import_recovery recover-safe --job JOB_ID
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.orm import Session

from importing.service import ImportService, SeriesImportLock
from importing.state import ACTIVE_JOB_STATES, TERMINAL_JOB_STATES
from models import (
    Chapter,
    ChapterImportStatus,
    ImportJob,
    ImportJobItem,
    ImportJobItemStatus,
    ImportJobStatus,
    Page,
)
from settings import Settings, get_settings
from storage import ObjectStorage, StorageError, create_object_storage


@dataclass(frozen=True)
class RecoveryIssue:
    code: str
    severity: Literal["warning", "error"]
    detail: str
    job_id: str | None = None
    item_id: str | None = None
    chapter_id: str | None = None
    active: bool = False
    recoverable: bool = False


@dataclass
class RecoveryReport:
    checked_at: str
    jobs_checked: int = 0
    active_jobs: int = 0
    stale_jobs: int = 0
    issues: list[RecoveryIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at,
            "jobs_checked": self.jobs_checked,
            "active_jobs": self.active_jobs,
            "stale_jobs": self.stale_jobs,
            "issue_count": len(self.issues),
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass
class RecoveryResult:
    attempted: int = 0
    recovered: int = 0
    failed_safely: int = 0
    skipped_active: int = 0
    skipped_not_stale: int = 0
    skipped_terminal: int = 0
    jobs: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _threshold(settings: Settings, status: ImportJobStatus) -> timedelta:
    seconds = {
        ImportJobStatus.pending: settings.import_pending_stale_seconds,
        ImportJobStatus.scanning: settings.import_scanning_stale_seconds,
        ImportJobStatus.uploading: settings.import_uploading_stale_seconds,
        ImportJobStatus.verifying: settings.import_verifying_stale_seconds,
    }[status]
    return timedelta(seconds=seconds)


def _activity(job: ImportJob) -> datetime:
    candidates = (
        _utc(job.heartbeat_at),
        _utc(job.updated_at),
        _utc(job.started_at),
        _utc(job.created_at),
    )
    return next(value for value in candidates if value is not None)


def _process_lock_active(db: Session, job_id: uuid.UUID) -> bool:
    lock = SeriesImportLock(db, f"job:{job_id}")
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
    return not acquired


def _job_liveness(
    db: Session, job: ImportJob, settings: Settings, now: datetime
) -> tuple[bool, bool]:
    """Return (active, stale); age alone is never treated as abandonment."""
    process_active = _process_lock_active(db, job.id)
    lease_expires = _utc(job.lease_expires_at)
    lease_active = (
        job.lease_owner_id is not None
        and lease_expires is not None
        and lease_expires > now
    )
    stale_activity = _activity(job) <= now - _threshold(settings, job.status)
    active = process_active or lease_active
    return active, stale_activity and not active


def scan_imports(
    db: Session,
    *,
    storage: ObjectStorage | None,
    settings: Settings,
    job_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> RecoveryReport:
    now = _utc(now) or datetime.now(timezone.utc)
    report = RecoveryReport(checked_at=now.isoformat())
    query = db.query(ImportJob).order_by(ImportJob.created_at, ImportJob.id)
    if job_id is not None:
        query = query.filter(ImportJob.id == job_id)
    jobs = query.all()
    report.jobs_checked = len(jobs)
    stale_job_ids: set[uuid.UUID] = set()

    for job in jobs:
        active = False
        stale = False
        if job.status in ACTIVE_JOB_STATES:
            active, stale = _job_liveness(db, job, settings, now)
            report.active_jobs += int(active)
            report.stale_jobs += int(stale)
            if stale:
                stale_job_ids.add(job.id)
                report.issues.append(
                    RecoveryIssue(
                        code=f"stuck_{job.status.value}",
                        severity="error",
                        detail=f"job has no recent activity in {job.status.value}",
                        job_id=str(job.id),
                        recoverable=True,
                    )
                )
            elif active and _activity(job) <= now - _threshold(settings, job.status):
                report.issues.append(
                    RecoveryIssue(
                        code="heartbeat_overdue_active",
                        severity="warning",
                        detail="heartbeat is overdue but the worker still owns a lock or lease",
                        job_id=str(job.id),
                        active=True,
                    )
                )

        items = list(job.items)
        if job.status in TERMINAL_JOB_STATES or stale:
            expected = _expected_counters(job, items)
            actual = {
                "series_count": job.series_count,
                "chapter_count": job.chapter_count,
                "page_count": job.page_count,
                "uploaded_count": job.uploaded_count,
                "skipped_count": job.skipped_count,
                "failed_count": job.failed_count,
            }
            if expected is not None and expected != actual:
                report.issues.append(
                    RecoveryIssue(
                        code="inconsistent_counters",
                        severity="error",
                        detail="persisted counters do not match the manifest and item journal",
                        job_id=str(job.id),
                        recoverable=stale,
                    )
                )

        for item in items:
            if item.status in (
                ImportJobItemStatus.pending,
                ImportJobItemStatus.uploading,
                ImportJobItemStatus.verifying,
            ) and (job.status in TERMINAL_JOB_STATES or stale):
                report.issues.append(
                    RecoveryIssue(
                        code="abandoned_job_item",
                        severity="error",
                        detail=f"item remains in {item.status.value}",
                        job_id=str(job.id),
                        item_id=str(item.id),
                        recoverable=stale,
                    )
                )
            if storage is None or not item.object_key or item.item_kind not in {"page", "cover"}:
                continue
            if item.status not in (
                ImportJobItemStatus.uploading,
                ImportJobItemStatus.verifying,
                ImportJobItemStatus.succeeded,
                ImportJobItemStatus.skipped,
            ):
                continue
            try:
                verification = storage.verify_object(
                    item.object_key,
                    expected_sha256=item.sha256,
                    expected_size=item.byte_size,
                )
            except StorageError:
                continue
            if not verification.exists:
                report.issues.append(
                    RecoveryIssue(
                        code="missing_storage_object",
                        severity="error",
                        detail="an import item references a missing storage object",
                        job_id=str(job.id),
                        item_id=str(item.id),
                        recoverable=stale,
                    )
                )
            elif not verification.verified:
                report.issues.append(
                    RecoveryIssue(
                        code="changed_immutable_object",
                        severity="error",
                        detail="an immutable storage object no longer matches its journal",
                        job_id=str(job.id),
                        item_id=str(item.id),
                        recoverable=stale,
                    )
                )
            elif item.status in (
                ImportJobItemStatus.uploading,
                ImportJobItemStatus.verifying,
            ):
                report.issues.append(
                    RecoveryIssue(
                        code="uploaded_before_checkpoint",
                        severity="warning",
                        detail="storage upload completed before its database checkpoint",
                        job_id=str(job.id),
                        item_id=str(item.id),
                        recoverable=stale,
                    )
                )
            elif item.item_kind == "page" and not _item_is_published(db, item):
                report.issues.append(
                    RecoveryIssue(
                        code="database_finalize_incomplete",
                        severity="error",
                        detail="a verified object has not been published in database metadata",
                        job_id=str(job.id),
                        item_id=str(item.id),
                        recoverable=stale,
                    )
                )

    page_query = db.query(Page)
    if job_id is not None:
        chapter_ids = {
            item.chapter_id
            for job in jobs
            for item in job.items
            if item.chapter_id is not None
        }
        page_query = page_query.filter(Page.chapter_id.in_(chapter_ids)) if chapter_ids else page_query.filter(False)
    if storage is not None:
        for page in page_query.all():
            try:
                verification = storage.verify_object(
                    page.object_key,
                    expected_sha256=page.sha256,
                    expected_size=page.file_size,
                )
            except StorageError:
                continue
            if not verification.exists:
                report.issues.append(
                    RecoveryIssue(
                        code="database_row_without_object",
                        severity="error",
                        detail="published page metadata references a missing object",
                        chapter_id=str(page.chapter_id),
                    )
                )
            elif not verification.verified:
                report.issues.append(
                    RecoveryIssue(
                        code="published_immutable_object_changed",
                        severity="error",
                        detail="published page bytes no longer match immutable metadata",
                        chapter_id=str(page.chapter_id),
                    )
                )

    importing_query = db.query(Chapter).filter(
        Chapter.import_status == ChapterImportStatus.importing
    )
    for chapter in importing_query.all():
        related = (
            db.query(ImportJobItem)
            .filter(ImportJobItem.chapter_id == chapter.id)
            .order_by(ImportJobItem.created_at.desc())
            .first()
        )
        related_job_id = related.job_id if related is not None else None
        if job_id is not None and related_job_id != job_id:
            continue
        report.issues.append(
            RecoveryIssue(
                code="chapter_left_importing",
                severity="error",
                detail="a chapter remains in the non-public importing state",
                job_id=str(related_job_id) if related_job_id else None,
                chapter_id=str(chapter.id),
                recoverable=related_job_id in stale_job_ids,
            )
        )
    return report


def _expected_counters(job: ImportJob, items: list[ImportJobItem]) -> dict[str, int] | None:
    payload = job.resume_payload
    if not isinstance(payload, dict):
        return None
    try:
        series = payload["manifest"]["series"]
        return {
            "series_count": len(series),
            "chapter_count": sum(len(row["chapters"]) for row in series),
            "page_count": sum(
                len(chapter["pages"]) for row in series for chapter in row["chapters"]
            ),
            "uploaded_count": sum(
                item.status == ImportJobItemStatus.succeeded for item in items
            ),
            "skipped_count": sum(
                item.status == ImportJobItemStatus.skipped for item in items
            ),
            "failed_count": sum(
                item.status in (ImportJobItemStatus.failed, ImportJobItemStatus.quarantined)
                for item in items
            ),
        }
    except (KeyError, TypeError):
        return None


def _item_is_published(db: Session, item: ImportJobItem) -> bool:
    if item.chapter_id is None or item.page_number is None:
        return False
    return (
        db.query(Page.id)
        .join(Chapter, Page.chapter_id == Chapter.id)
        .filter(
            Page.chapter_id == item.chapter_id,
            Page.page_number == item.page_number,
            Page.object_key == item.object_key,
            Chapter.import_status == ChapterImportStatus.ready,
        )
        .first()
        is not None
    )


def recover_safe(
    db: Session,
    *,
    storage: ObjectStorage | None,
    settings: Settings,
    job_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> RecoveryResult:
    """Resume stale jobs without deleting objects or publishing incomplete chapters."""
    now = _utc(now) or datetime.now(timezone.utc)
    result = RecoveryResult()
    query = db.query(ImportJob).order_by(ImportJob.created_at, ImportJob.id)
    if job_id is not None:
        query = query.filter(ImportJob.id == job_id)
    else:
        query = query.filter(ImportJob.status.in_(ACTIVE_JOB_STATES)).limit(
            settings.import_recovery_batch_size
        )
    for job in query.all():
        if job.status in TERMINAL_JOB_STATES:
            result.skipped_terminal += 1
            result.jobs.append({"job_id": str(job.id), "outcome": "terminal"})
            continue
        active, stale = _job_liveness(db, job, settings, now)
        if active:
            result.skipped_active += 1
            result.jobs.append({"job_id": str(job.id), "outcome": "active"})
            continue
        if not stale:
            result.skipped_not_stale += 1
            result.jobs.append({"job_id": str(job.id), "outcome": "not_stale"})
            continue
        result.attempted += 1
        service = ImportService(db, storage=storage, settings=settings)
        try:
            recovered = service.resume(job.id, recovery=True)
        except Exception:
            db.rollback()
            recovered = service.fail_unrecoverable(
                job.id, "safe recovery could not reconstruct the import"
            )
        if recovered.status in (ImportJobStatus.succeeded, ImportJobStatus.partial):
            result.recovered += 1
            outcome = recovered.status.value
        elif recovered.status == ImportJobStatus.failed:
            result.failed_safely += 1
            outcome = "failed_safely"
        else:
            result.skipped_active += 1
            outcome = "claimed_elsewhere"
        result.jobs.append({"job_id": str(job.id), "outcome": outcome})
    return result


def _open_session() -> tuple[Session, Settings, ObjectStorage | None]:
    from database import _get_engine
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    engine = _get_engine()
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory(), settings, create_object_storage(settings)


def _print_scan(report: RecoveryReport) -> None:
    print(
        f"Checked {report.jobs_checked} jobs: {report.stale_jobs} stale, "
        f"{report.active_jobs} active, {len(report.issues)} issues"
    )
    for issue in report.issues:
        identity = f" job={issue.job_id}" if issue.job_id else ""
        print(f"{issue.severity.upper():7} {issue.code}{identity}: {issue.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crash-safe import recovery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="Inspect import recovery state")
    scan_parser.add_argument("--json", action="store_true", help="Emit JSON")
    scan_parser.add_argument("--job", type=uuid.UUID, help="Inspect one job")
    recover_parser = subparsers.add_parser(
        "recover-safe", help="Safely resume stale import jobs"
    )
    recover_parser.add_argument("--job", type=uuid.UUID, help="Recover one stale job")
    recover_parser.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        db, settings, storage = _open_session()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.command == "scan":
            report = scan_imports(
                db, storage=storage, settings=settings, job_id=args.job
            )
            if args.json:
                print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            else:
                _print_scan(report)
        else:
            result = recover_safe(
                db, storage=storage, settings=settings, job_id=args.job
            )
            if args.json:
                print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            else:
                print(
                    f"Attempted {result.attempted}: {result.recovered} recovered, "
                    f"{result.failed_safely} failed safely, "
                    f"{result.skipped_active} active skipped"
                )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
