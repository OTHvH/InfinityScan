"""Bounded PostgreSQL-to-object-storage integrity verification."""
from __future__ import annotations

import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from models import Chapter, ImportJobItem, Page, PageIntegrityStatus, Series
from storage import ObjectMetadata, ObjectStorage, StorageError, page_object_key

IntegrityMode = Literal["quick", "full"]

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SHA256 = r"[0-9a-f]{64}"
_EXTENSION = r"[a-z0-9]{1,16}"
_PAGE_KEY = re.compile(rf"^series/{_UUID}/{_UUID}/[0-9]{{5}}-{_SHA256}\.{_EXTENSION}$")
_COVER_KEY = re.compile(rf"^series/{_UUID}/cover-{_SHA256}\.{_EXTENSION}$")


@dataclass(frozen=True)
class StorageIntegrityIssue:
    category: str
    severity: Literal["warning", "error", "critical"]
    entity_id: str
    description: str
    repairable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StorageIntegrityReport:
    mode: IntegrityMode
    content_sha256: bool
    issue_limit: int
    objects_checked: int = 0
    objects_verified: int = 0
    missing: int = 0
    metadata_mismatch: int = 0
    hash_mismatch: int = 0
    orphaned: int = 0
    invalid_keys: int = 0
    duplicate_references: int = 0
    stale_objects: int = 0
    errors: int = 0
    bytes_hashed: int = 0
    repairs_applied: int = 0
    elapsed_time: float = 0.0
    issues: list[StorageIntegrityIssue] = field(default_factory=list)
    issues_truncated: bool = False

    @property
    def complete(self) -> bool:
        return self.errors == 0

    def add_issue(self, issue: StorageIntegrityIssue) -> None:
        if len(self.issues) < self.issue_limit:
            self.issues.append(issue)
        else:
            self.issues_truncated = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "content_sha256": self.content_sha256,
            "complete": self.complete,
            "objects_checked": self.objects_checked,
            "objects_verified": self.objects_verified,
            "missing": self.missing,
            "metadata_mismatch": self.metadata_mismatch,
            "hash_mismatch": self.hash_mismatch,
            "orphaned": self.orphaned,
            "invalid_keys": self.invalid_keys,
            "duplicate_references": self.duplicate_references,
            "stale_objects": self.stale_objects,
            "errors": self.errors,
            "bytes_hashed": self.bytes_hashed,
            "repairs_applied": self.repairs_applied,
            "elapsed_time": self.elapsed_time,
            "issues_reported": len(self.issues),
            "issues_truncated": self.issues_truncated,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass
class StorageDeletionReport:
    dry_run: bool
    candidates: int = 0
    deleted: int = 0
    skipped_referenced: int = 0
    skipped_young: int = 0
    errors: int = 0
    elapsed_time: float = 0.0

    @property
    def complete(self) -> bool:
        return self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "complete": self.complete,
            "candidates": self.candidates,
            "deleted": self.deleted,
            "skipped_referenced": self.skipped_referenced,
            "skipped_young": self.skipped_young,
            "errors": self.errors,
            "elapsed_time": self.elapsed_time,
        }


@dataclass(frozen=True)
class _PageExpectation:
    page_id: uuid.UUID
    series_id: uuid.UUID
    chapter_id: uuid.UUID
    page_number: int
    object_key: str
    file_size: int | None
    sha256: str | None
    mime_type: str | None
    file_extension: str | None
    storage_etag: str | None
    integrity_status: PageIntegrityStatus


@dataclass(frozen=True)
class _PageObservation:
    expectation: _PageExpectation
    metadata: ObjectMetadata | None = None
    invalid_key: bool = False
    missing: bool = False
    metadata_fields: tuple[str, ...] = ()
    hash_mismatch: bool = False
    stale: bool = False
    bytes_hashed: int = 0
    error: bool = False


def _iter_page_expectations(db: Session, batch_size: int = 250) -> Iterator[_PageExpectation]:
    last_id: uuid.UUID | None = None
    while True:
        statement = (
            select(
                Page.id,
                Chapter.series_id,
                Page.chapter_id,
                Page.page_number,
                Page.object_key,
                Page.file_size,
                Page.sha256,
                Page.mime_type,
                Page.file_extension,
                Page.storage_etag,
                Page.integrity_status,
            )
            .join(Chapter, Page.chapter_id == Chapter.id)
            .where(Page.object_key != "")
            .order_by(Page.id)
            .limit(batch_size)
        )
        if last_id is not None:
            statement = statement.where(Page.id > last_id)
        rows = db.execute(statement).all()
        if not rows:
            return
        for row in rows:
            last_id = row.id
            yield _PageExpectation(
                page_id=row.id,
                series_id=row.series_id,
                chapter_id=row.chapter_id,
                page_number=row.page_number,
                object_key=row.object_key,
                file_size=row.file_size,
                sha256=row.sha256,
                mime_type=row.mime_type,
                file_extension=row.file_extension,
                storage_etag=row.storage_etag,
                integrity_status=row.integrity_status,
            )


def _is_valid_page_key(expectation: _PageExpectation) -> bool:
    if expectation.integrity_status != PageIntegrityStatus.verified:
        return True
    if not expectation.sha256 or not expectation.file_extension:
        return False
    try:
        expected = page_object_key(
            expectation.series_id,
            expectation.chapter_id,
            expectation.page_number,
            expectation.sha256,
            expectation.file_extension,
        )
    except ValueError:
        return False
    return expectation.object_key == expected


def is_valid_managed_key(key: str) -> bool:
    return bool(_PAGE_KEY.fullmatch(key) or _COVER_KEY.fullmatch(key))


def _inspect_page(
    expectation: _PageExpectation,
    storage: ObjectStorage,
    *,
    mode: IntegrityMode,
    content_sha256: bool,
) -> _PageObservation:
    invalid_key = not _is_valid_page_key(expectation)
    try:
        metadata = storage.head_object(expectation.object_key)
        if metadata is None:
            return _PageObservation(
                expectation=expectation,
                invalid_key=invalid_key,
                missing=True,
            )
        mismatch_fields: list[str] = []
        if expectation.file_size is not None and metadata.byte_size != expectation.file_size:
            mismatch_fields.append("file_size")
        if expectation.sha256 is not None and metadata.sha256 != expectation.sha256:
            mismatch_fields.append("sha256_metadata")
        if expectation.mime_type is not None and metadata.mime_type != expectation.mime_type:
            mismatch_fields.append("mime_type")
        stale = bool(
            mode == "full"
            and expectation.storage_etag
            and metadata.etag
            and expectation.storage_etag != metadata.etag
        )
        content_mismatch = False
        bytes_hashed = 0
        if mode == "full" and content_sha256:
            verification = storage.verify_object(
                expectation.object_key,
                expected_sha256=expectation.sha256,
                expected_size=expectation.file_size,
                metadata=metadata,
            )
            bytes_hashed = verification.actual_size or 0
            content_mismatch = not verification.verified
        return _PageObservation(
            expectation=expectation,
            metadata=metadata,
            invalid_key=invalid_key,
            metadata_fields=tuple(mismatch_fields),
            hash_mismatch=content_mismatch,
            stale=stale,
            bytes_hashed=bytes_hashed,
        )
    except (StorageError, ValueError):
        return _PageObservation(
            expectation=expectation,
            invalid_key=invalid_key,
            error=True,
        )


def _repair_observation(db: Session, observation: _PageObservation) -> int:
    if observation.error:
        return 0
    status: PageIntegrityStatus | None = None
    if observation.missing:
        status = PageIntegrityStatus.missing
    elif (
        observation.invalid_key
        or observation.metadata_fields
        or observation.hash_mismatch
        or observation.stale
    ):
        status = PageIntegrityStatus.mismatch
    if status is None:
        return 0
    expectation = observation.expectation
    conditions = [
        Page.id == expectation.page_id,
        Page.object_key == expectation.object_key,
        Page.sha256 == expectation.sha256,
        Page.file_size == expectation.file_size,
    ]
    result = db.execute(
        update(Page)
        .where(*conditions)
        .values(integrity_status=status, verified_at=None)
    )
    return int(result.rowcount or 0)


def _record_observation(
    report: StorageIntegrityReport,
    observation: _PageObservation,
) -> None:
    expectation = observation.expectation
    report.objects_checked += 1
    report.bytes_hashed += observation.bytes_hashed
    has_finding = False
    if observation.invalid_key:
        has_finding = True
        report.invalid_keys += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="invalid_object_key",
                severity="critical",
                entity_id=str(expectation.page_id),
                description="Verified page has an invalid immutable object key",
                repairable=True,
                details={"object_key": expectation.object_key},
            )
        )
    if observation.error:
        report.errors += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="storage_error",
                severity="error",
                entity_id=str(expectation.page_id),
                description="Object storage verification failed",
                details={"object_key": expectation.object_key},
            )
        )
        return
    if observation.missing:
        report.missing += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="missing_storage_object",
                severity="critical",
                entity_id=str(expectation.page_id),
                description="Referenced object is missing",
                repairable=True,
                details={"object_key": expectation.object_key},
            )
        )
        return
    if observation.metadata_fields:
        has_finding = True
        report.metadata_mismatch += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="storage_metadata_mismatch",
                severity="error",
                entity_id=str(expectation.page_id),
                description="Stored object metadata differs from PostgreSQL",
                repairable=True,
                details={
                    "object_key": expectation.object_key,
                    "fields": list(observation.metadata_fields),
                },
            )
        )
    if observation.hash_mismatch:
        has_finding = True
        report.hash_mismatch += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="sha256_mismatch",
                severity="critical",
                entity_id=str(expectation.page_id),
                description="Stored object content SHA-256 differs from PostgreSQL",
                repairable=True,
                details={"object_key": expectation.object_key},
            )
        )
    if observation.stale:
        has_finding = True
        report.stale_objects += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="stale_replaced_object",
                severity="error",
                entity_id=str(expectation.page_id),
                description="Stored object ETag differs from PostgreSQL",
                repairable=True,
                details={"object_key": expectation.object_key},
            )
        )
    if not has_finding:
        report.objects_verified += 1


def _consume_future(
    future: Future[_PageObservation],
    report: StorageIntegrityReport,
    db: Session,
    *,
    repair: bool,
) -> None:
    observation = future.result()
    _record_observation(report, observation)
    if repair:
        report.repairs_applied += _repair_observation(db, observation)


def _scan_referenced_pages(
    db: Session,
    storage: ObjectStorage,
    report: StorageIntegrityReport,
    *,
    max_concurrency: int,
    repair: bool,
) -> None:
    pending: set[Future[_PageObservation]] = set()
    with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="integrity") as executor:
        for expectation in _iter_page_expectations(db):
            pending.add(
                executor.submit(
                    _inspect_page,
                    expectation,
                    storage,
                    mode=report.mode,
                    content_sha256=report.content_sha256,
                )
            )
            if len(pending) >= max_concurrency:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    _consume_future(future, report, db, repair=repair)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                _consume_future(future, report, db, repair=repair)


def _reference_sets(db: Session, keys: tuple[str, ...]) -> tuple[set[str], set[str]]:
    if not keys:
        return set(), set()
    page_keys = set(db.scalars(select(Page.object_key).where(Page.object_key.in_(keys))).all())
    all_keys = set(page_keys)
    all_keys.update(
        db.scalars(select(Series.cover_object_key).where(Series.cover_object_key.in_(keys))).all()
    )
    all_keys.update(
        db.scalars(select(ImportJobItem.object_key).where(ImportJobItem.object_key.in_(keys))).all()
    )
    return page_keys, all_keys


def _scan_inventory(
    db: Session,
    storage: ObjectStorage,
    report: StorageIntegrityReport,
    *,
    page_size: int,
) -> None:
    token: str | None = None
    while True:
        try:
            page = storage.list_objects_page(
                prefix="series/",
                continuation_token=token,
                max_keys=page_size,
            )
        except StorageError:
            report.errors += 1
            report.add_issue(
                StorageIntegrityIssue(
                    category="storage_inventory_error",
                    severity="error",
                    entity_id="series/",
                    description="Object storage inventory could not be completed",
                )
            )
            return
        keys = tuple(item.key for item in page.objects)
        page_references, all_references = _reference_sets(db, keys)
        for item in page.objects:
            if item.key not in all_references:
                report.orphaned += 1
                report.add_issue(
                    StorageIntegrityIssue(
                        category="orphaned_storage_object",
                        severity="warning",
                        entity_id=item.key,
                        description="Storage object is not referenced by PostgreSQL",
                        details={"object_key": item.key},
                    )
                )
            if item.key not in page_references and not is_valid_managed_key(item.key):
                report.invalid_keys += 1
                report.add_issue(
                    StorageIntegrityIssue(
                        category="invalid_object_key",
                        severity="error",
                        entity_id=item.key,
                        description="Storage inventory contains an invalid managed key",
                        details={"object_key": item.key},
                    )
                )
        next_token = page.next_token
        if next_token is None:
            return
        if next_token == token:
            report.errors += 1
            report.add_issue(
                StorageIntegrityIssue(
                    category="storage_inventory_error",
                    severity="error",
                    entity_id="series/",
                    description="Object storage inventory pagination did not advance",
                )
            )
            return
        token = next_token


def _check_duplicate_references(db: Session, report: StorageIntegrityReport) -> None:
    duplicates = db.execute(
        select(Page.object_key, func.count(Page.id).label("count"))
        .where(Page.object_key != "")
        .group_by(Page.object_key)
        .having(func.count(Page.id) > 1)
    )
    for duplicate in duplicates:
        report.duplicate_references += 1
        report.add_issue(
            StorageIntegrityIssue(
                category="duplicate_content_address_reference",
                severity="error",
                entity_id=duplicate.object_key,
                description="Multiple page rows reference one content-addressed object",
                details={"object_key": duplicate.object_key, "references": duplicate.count},
            )
        )


def run_storage_integrity(
    db: Session,
    storage: ObjectStorage,
    *,
    mode: IntegrityMode = "quick",
    content_sha256: bool = False,
    max_concurrency: int = 8,
    inventory_page_size: int = 1000,
    issue_limit: int = 1000,
    repair: bool = False,
) -> StorageIntegrityReport:
    if mode not in ("quick", "full"):
        raise ValueError("integrity mode must be 'quick' or 'full'")
    if content_sha256 and mode != "full":
        raise ValueError("content SHA-256 verification requires full mode")
    if max_concurrency < 1 or max_concurrency > 32:
        raise ValueError("max_concurrency must be between 1 and 32")
    if inventory_page_size < 1 or inventory_page_size > 1000:
        raise ValueError("inventory_page_size must be between 1 and 1000")
    if issue_limit < 1:
        raise ValueError("issue_limit must be positive")

    started = time.monotonic()
    report = StorageIntegrityReport(
        mode=mode,
        content_sha256=content_sha256,
        issue_limit=issue_limit,
    )
    _scan_referenced_pages(
        db,
        storage,
        report,
        max_concurrency=max_concurrency,
        repair=repair,
    )
    if mode == "full":
        _check_duplicate_references(db, report)
        _scan_inventory(db, storage, report, page_size=inventory_page_size)
    if repair:
        db.commit()
    report.elapsed_time = round(time.monotonic() - started, 6)
    return report


def delete_unreferenced_objects(
    db: Session,
    storage: ObjectStorage,
    *,
    execute: bool,
    inventory_page_size: int = 1000,
    minimum_age_seconds: int = 86400,
) -> StorageDeletionReport:
    """Dry-run or explicitly delete old objects that remain unreferenced."""
    if inventory_page_size < 1 or inventory_page_size > 1000:
        raise ValueError("inventory_page_size must be between 1 and 1000")
    if minimum_age_seconds < 0:
        raise ValueError("minimum_age_seconds must not be negative")
    started = time.monotonic()
    report = StorageDeletionReport(dry_run=not execute)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=minimum_age_seconds)
    token: str | None = None
    while True:
        try:
            page = storage.list_objects_page(
                prefix="series/",
                continuation_token=token,
                max_keys=inventory_page_size,
            )
        except StorageError:
            report.errors += 1
            break
        keys = tuple(item.key for item in page.objects)
        _, references = _reference_sets(db, keys)
        for item in page.objects:
            if item.key in references:
                report.skipped_referenced += 1
                continue
            if item.last_modified is None or item.last_modified > cutoff:
                report.skipped_young += 1
                continue
            report.candidates += 1
            if not execute:
                continue
            _, current_references = _reference_sets(db, (item.key,))
            if item.key in current_references:
                report.skipped_referenced += 1
                continue
            try:
                current = storage.head_object(item.key)
                if current is None:
                    continue
                if (
                    current.byte_size != item.byte_size
                    or current.etag != item.etag
                    or current.last_modified != item.last_modified
                ):
                    report.errors += 1
                    continue
                if storage.delete_object(item.key):
                    report.deleted += 1
            except StorageError:
                report.errors += 1
        next_token = page.next_token
        if next_token is None:
            break
        if next_token == token:
            report.errors += 1
            break
        token = next_token
    report.elapsed_time = round(time.monotonic() - started, 6)
    return report
