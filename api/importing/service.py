"""Crash-safe import service backed by a durable per-object work journal."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import (
    AuditEventOutcome,
    Chapter,
    ChapterImportStatus,
    ImportJob,
    ImportJobItem,
    ImportJobItemStatus,
    ImportJobStatus,
    Page,
    PageIntegrityStatus,
    Series,
    Source,
)
from audit_events import record_event_safe
from settings import Settings, get_settings
from storage import ObjectStorage, StorageError, create_object_storage, page_object_key

from .adapters.base import ManifestChapter, ManifestPage, ManifestSeries
from .manifest import (
    hash_manifest,
    manifest_from_recovery_payload,
    manifest_to_recovery_payload,
    recovery_payload_with_prune,
)
from .state import TERMINAL_JOB_STATES, require_item_transition, require_job_transition
from .validation import validate_manifest_strict

log = logging.getLogger("importing.service")

_PUBLIC_REJECTION_REASONS = frozenset({
    "file extension is not an image candidate",
    "image resolves outside the approved import root",
    "image exceeds the maximum byte size",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_rejection_reason(reason: str) -> str:
    """Persist only scanner messages that cannot expose source details."""
    if reason in _PUBLIC_REJECTION_REASONS:
        return reason
    return "source file rejected during scan"


def _lock_key(identity: str) -> int:
    raw = hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(raw, byteorder="big", signed=True)


class SeriesImportLock:
    """A dedicated-connection advisory lock, with a process-local test fallback."""

    _thread_locks: dict[int, threading.Lock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(self, session: Session, series_slug: str) -> None:
        self._session = session
        self._lock_key = _lock_key(series_slug)
        self._is_pg = session.get_bind().dialect.name == "postgresql"
        self._connection = None
        self._thread_lock: threading.Lock | None = None
        self.acquired = False

    def acquire(self, *, blocking: bool = True) -> bool:
        if self.acquired:
            return True
        if self._is_pg:
            self._connection = self._session.get_bind().connect()
            function = "pg_advisory_lock" if blocking else "pg_try_advisory_lock"
            result = self._connection.execute(
                text(f"SELECT {function}(:key)"), {"key": self._lock_key}
            ).scalar()
            self.acquired = blocking or bool(result)
            if not self.acquired:
                self._connection.close()
                self._connection = None
            return self.acquired

        with self._thread_locks_guard:
            lock = self._thread_locks.setdefault(self._lock_key, threading.Lock())
        self._thread_lock = lock
        self.acquired = lock.acquire(blocking=blocking)
        return self.acquired

    def release(self) -> None:
        if not self.acquired:
            return
        if self._is_pg:
            assert self._connection is not None
            try:
                self._connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key}
                )
            finally:
                self._connection.close()
                self._connection = None
        elif self._thread_lock is not None:
            self._thread_lock.release()
            self._thread_lock = None
        self.acquired = False

    def __enter__(self) -> "SeriesImportLock":
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class ImportService:
    """Run and resume one persisted import state machine.

    Job/item checkpoints are committed before and after object-storage I/O.
    Live chapter rows are changed only after every desired page is verified.
    """

    def __init__(
        self,
        session: Session,
        storage: ObjectStorage | None = None,
        *,
        dry_run: bool = False,
        settings: Settings | None = None,
        checkpoint_hook: Callable[[str, uuid.UUID | None], None] | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._dry_run = dry_run
        self._settings = settings or get_settings()
        self._checkpoint_hook = checkpoint_hook
        self._worker_id = uuid.uuid4()
        self._fencing_token = 0
        self._first_upload_seen = False
        self._recovering = False

    @classmethod
    def from_settings(
        cls,
        session: Session,
        *,
        dry_run: bool = False,
        settings: Settings | None = None,
        checkpoint_hook: Callable[[str, uuid.UUID | None], None] | None = None,
    ) -> "ImportService":
        cfg = settings or get_settings()
        return cls(
            session,
            storage=create_object_storage(cfg),
            dry_run=dry_run,
            settings=cfg,
            checkpoint_hook=checkpoint_hook,
        )

    def _checkpoint(self, name: str, job_id: uuid.UUID | None = None) -> None:
        if self._checkpoint_hook is not None:
            self._checkpoint_hook(name, job_id)

    def _upsert_series(self, manifest: ManifestSeries) -> Series:
        existing = self._session.query(Series).filter(Series.slug == manifest.slug).first()
        if existing:
            existing.title = manifest.title
            if manifest.cover_object_key and not existing.cover_object_key:
                existing.cover_object_key = manifest.cover_object_key
            existing.updated_at = _now()
            self._session.flush()
            return existing
        series = Series(
            id=manifest.series_id,
            slug=manifest.slug,
            title=manifest.title,
            content_type=manifest.content_type,
            cover_object_key=manifest.cover_object_key,
        )
        self._session.add(series)
        self._session.flush()
        return series

    def _upsert_chapter(
        self,
        series_id: uuid.UUID,
        chapter: ManifestChapter,
        *,
        status: ChapterImportStatus = ChapterImportStatus.importing,
    ) -> Chapter:
        existing = (
            self._session.query(Chapter)
            .filter(
                Chapter.series_id == series_id,
                Chapter.number == chapter.number,
                Chapter.language == chapter.language,
            )
            .first()
        )
        if existing:
            existing.title = chapter.title
            existing.page_count = len(chapter.pages)
            existing.import_status = status
            existing.verified_at = _now() if status == ChapterImportStatus.ready else None
            self._session.flush()
            return existing
        row = Chapter(
            id=chapter.chapter_id,
            series_id=series_id,
            number=chapter.number,
            title=chapter.title,
            language=chapter.language,
            page_count=len(chapter.pages),
            import_status=status,
            verified_at=_now() if status == ChapterImportStatus.ready else None,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def _upsert_page(
        self,
        chapter_id: uuid.UUID,
        page: ManifestPage,
        *,
        object_key: str,
        status: PageIntegrityStatus = PageIntegrityStatus.pending,
        storage_etag: str | None = None,
    ) -> Page:
        existing = (
            self._session.query(Page)
            .filter(Page.chapter_id == chapter_id, Page.page_number == page.page_number)
            .first()
        )
        values = {
            "object_key": object_key,
            "width": page.width,
            "height": page.height,
            "file_size": page.byte_size,
            "sha256": page.sha256,
            "mime_type": page.mime_type,
            "file_extension": page.file_extension,
            "integrity_status": status,
            "verified_at": _now() if status == PageIntegrityStatus.verified else None,
            "imported_at": _now(),
            "storage_etag": storage_etag,
        }
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            self._session.flush()
            return existing
        row = Page(chapter_id=chapter_id, page_number=page.page_number, **values)
        self._session.add(row)
        self._session.flush()
        return row

    def run(
        self,
        manifest: list[ManifestSeries],
        *,
        source_key: str = "local",
        source_display_name: str = "Local Import",
        prune: bool = False,
    ) -> ImportJob:
        """Persist and execute a manifest using the resumable state machine."""
        self._checkpoint("during_validation")
        validate_manifest_strict(manifest)
        manifest_hash = hash_manifest(manifest)
        idempotency_key = f"{source_key}:{manifest_hash}"
        if self._dry_run:
            return ImportJob(
                id=uuid.uuid4(),
                status=ImportJobStatus.pending,
                idempotency_key=idempotency_key,
                manifest_hash=manifest_hash,
            )

        identities = sorted(f"series:{series.slug}" for series in manifest)
        with ExitStack() as stack:
            for identity in identities:
                stack.enter_context(SeriesImportLock(self._session, identity))
            existing = (
                self._session.query(ImportJob)
                .filter(ImportJob.idempotency_key == idempotency_key)
                .first()
            )
            if existing is not None:
                if existing.status in TERMINAL_JOB_STATES:
                    return existing
                if existing.resume_payload is None:
                    self._initialize_existing_job(existing, manifest, prune=prune)
                job_id = existing.id
            else:
                job_id = self._reserve_job(
                    manifest,
                    manifest_hash=manifest_hash,
                    idempotency_key=idempotency_key,
                    source_key=source_key,
                    source_display_name=source_display_name,
                    prune=prune,
                )
        self._checkpoint("after_job_creation", job_id)
        return self._drive_job(job_id, explicit=False)

    def _reserve_job(
        self,
        manifest: list[ManifestSeries],
        *,
        manifest_hash: str,
        idempotency_key: str,
        source_key: str,
        source_display_name: str,
        prune: bool,
    ) -> uuid.UUID:
        source = self._session.query(Source).filter(Source.key == source_key).first()
        if source is None:
            source = Source(
                key=source_key,
                display_name=source_display_name,
                adapter_type="local",
            )
            self._session.add(source)
            self._session.flush()
        payload = manifest_to_recovery_payload(manifest, prune=prune)
        job = ImportJob(
            source_id=source.id,
            status=ImportJobStatus.pending,
            idempotency_key=idempotency_key,
            manifest_hash=manifest_hash,
            resume_payload=payload,
            checkpoint={"version": 1, "phase": "pending"},
            series_count=len(manifest),
            chapter_count=sum(len(series.chapters) for series in manifest),
            page_count=sum(
                len(chapter.pages) for series in manifest for chapter in series.chapters
            ),
            heartbeat_at=_now(),
        )
        self._session.add(job)
        self._session.flush()
        self._create_work_items(job, manifest, payload)
        self._session.commit()
        return job.id

    def _initialize_existing_job(
        self, job: ImportJob, manifest: list[ManifestSeries], *, prune: bool
    ) -> None:
        payload = manifest_to_recovery_payload(manifest, prune=prune)
        job.resume_payload = payload
        job.heartbeat_at = _now()
        job.updated_at = _now()
        job.series_count = len(manifest)
        job.chapter_count = sum(len(series.chapters) for series in manifest)
        job.page_count = sum(
            len(chapter.pages) for series in manifest for chapter in series.chapters
        )
        self._create_work_items(job, manifest, payload)
        self._session.commit()

    def _create_work_items(
        self,
        job: ImportJob,
        manifest: list[ManifestSeries],
        payload: dict,
    ) -> None:
        raw_series = payload["manifest"]["series"]
        existing_keys = {
            key
            for (key,) in self._session.query(ImportJobItem.item_key)
            .filter(ImportJobItem.job_id == job.id)
            .all()
            if key is not None
        }
        for series, raw_s in zip(manifest, raw_series, strict=True):
            if series.cover_object_key and series.cover_sha256 and series.cover_source_path:
                item_key = f"cover:{series.series_id}"
                if item_key not in existing_keys:
                    self._session.add(
                        ImportJobItem(
                            job_id=job.id,
                            item_key=item_key,
                            item_kind="cover",
                            series_id=series.series_id,
                            source_reference=raw_s["cover_source_path"],
                            object_key=series.cover_object_key,
                            sha256=series.cover_sha256,
                            mime_type=series.cover_mime_type,
                            file_extension=series.cover_file_extension,
                            status=ImportJobItemStatus.pending,
                        )
                    )
            for index, rejected in enumerate(series.rejected_files):
                item_key = f"rejected:{series.series_id}:{index}"
                if item_key not in existing_keys:
                    self._session.add(
                        ImportJobItem(
                            job_id=job.id,
                            item_key=item_key,
                            item_kind="rejected",
                            series_id=series.series_id,
                            source_reference=Path(rejected.source_reference).name,
                            object_key="",
                            status=(
                                ImportJobItemStatus.skipped
                                if rejected.status == "skipped"
                                else ImportJobItemStatus.failed
                            ),
                            error=_safe_rejection_reason(rejected.reason),
                        )
                    )
            for chapter, raw_c in zip(series.chapters, raw_s["chapters"], strict=True):
                for page, raw_p in zip(chapter.pages, raw_c["pages"], strict=True):
                    item_key = f"page:{chapter.chapter_id}:{page.page_number}"
                    if item_key in existing_keys:
                        continue
                    self._session.add(
                        ImportJobItem(
                            job_id=job.id,
                            item_key=item_key,
                            item_kind="page",
                            series_id=series.series_id,
                            chapter_id=chapter.chapter_id,
                            page_number=page.page_number,
                            source_reference=raw_p["source_path"],
                            object_key=page_object_key(
                                series.series_id,
                                chapter.chapter_id,
                                page.page_number,
                                page.sha256,
                                page.file_extension,
                            ),
                            sha256=page.sha256,
                            byte_size=page.byte_size,
                            mime_type=page.mime_type,
                            file_extension=page.file_extension,
                            status=ImportJobItemStatus.pending,
                        )
                    )
        self._session.flush()

    def resume(
        self, job_id: uuid.UUID, *, prune: bool = False, recovery: bool = False
    ) -> ImportJob:
        """Resume an active job from its persisted object journal."""
        job = self._session.get(ImportJob, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} not found")
        if job.status in TERMINAL_JOB_STATES:
            return job
        if job.resume_payload is None:
            return self.fail_unrecoverable(job_id, "recovery manifest is unavailable")
        if prune and not bool(job.resume_payload.get("prune")):
            job.resume_payload = recovery_payload_with_prune(job.resume_payload, prune=True)
            self._session.commit()
        self._recovering = recovery
        return self._drive_job(job_id, explicit=not recovery)

    def _drive_job(self, job_id: uuid.UUID, *, explicit: bool) -> ImportJob:
        execution_lock = SeriesImportLock(self._session, f"job:{job_id}")
        if not execution_lock.acquire(blocking=False):
            self._session.expire_all()
            return self._session.get(ImportJob, job_id)
        try:
            job = self._session.get(ImportJob, job_id)
            if job is None:
                raise ValueError(f"import job {job_id} not found")
            if job.status in TERMINAL_JOB_STATES:
                return job
            try:
                manifest, prune = manifest_from_recovery_payload(job.resume_payload or {})
                validate_manifest_strict(manifest)
                if (job.resume_payload or {}).get("manifest_hash") != job.manifest_hash:
                    return self.fail_unrecoverable(job_id, "recovery manifest failed validation")
            except (KeyError, TypeError, ValueError):
                return self.fail_unrecoverable(job_id, "recovery manifest failed validation")

            identities = sorted(f"series:{series.slug}" for series in manifest)
            with ExitStack() as stack:
                for identity in identities:
                    stack.enter_context(SeriesImportLock(self._session, identity))
                job = self._claim_job(job_id, explicit=explicit)
                source_root = Path((job.resume_payload or {})["source_root"]).resolve()
                items = (
                    self._session.query(ImportJobItem)
                    .filter(ImportJobItem.job_id == job_id)
                    .order_by(ImportJobItem.created_at, ImportJobItem.id)
                    .all()
                )
                for item in items:
                    if item.item_kind not in {"page", "cover"}:
                        continue
                    source_path = self._resolve_item_source(source_root, item.source_reference)
                    self._process_object_item(job_id, item.id, source_path)
                self._checkpoint("after_all_uploads", job_id)
                self._set_job_status(job_id, ImportJobStatus.verifying, "verifying")

                published = 0
                pruned_keys: list[str] = []
                manifest_by_series = {series.series_id: series for series in manifest}
                for series in manifest_by_series.values():
                    for chapter in series.chapters:
                        if self._publish_chapter(job_id, series, chapter, prune, pruned_keys):
                            published += 1
                job = self._finalize_job(job_id, manifest, published)
                if prune and not self._recovering:
                    self._delete_pruned_objects(pruned_keys)
                return job
        finally:
            execution_lock.release()

    def _claim_job(self, job_id: uuid.UUID, *, explicit: bool) -> ImportJob:
        job = (
            self._session.query(ImportJob)
            .filter(ImportJob.id == job_id)
            .with_for_update()
            .one()
        )
        if job.status in TERMINAL_JOB_STATES:
            return job
        now = _now()
        job.lease_owner_id = self._worker_id
        job.lease_expires_at = now + timedelta(seconds=self._settings.import_lease_seconds)
        job.heartbeat_at = now
        job.updated_at = now
        job.fencing_token += 1
        job.recovery_attempt_count += int(self._recovering or explicit)
        self._fencing_token = job.fencing_token
        if job.status == ImportJobStatus.pending:
            require_job_transition(job.status, ImportJobStatus.scanning)
            job.status = ImportJobStatus.scanning
        job.started_at = job.started_at or now
        job.checkpoint = {"version": 1, "phase": job.status.value}
        self._session.commit()
        return job

    def _set_job_status(
        self, job_id: uuid.UUID, status: ImportJobStatus, phase: str
    ) -> ImportJob:
        job = self._owned_job(job_id)
        require_job_transition(job.status, status)
        job.status = status
        job.checkpoint = {"version": 1, "phase": phase}
        self._renew_lease(job)
        self._session.commit()
        return job

    def _owned_job(self, job_id: uuid.UUID) -> ImportJob:
        job = (
            self._session.query(ImportJob)
            .filter(
                ImportJob.id == job_id,
                ImportJob.lease_owner_id == self._worker_id,
                ImportJob.fencing_token == self._fencing_token,
            )
            .one_or_none()
        )
        if job is None:
            raise RuntimeError("import recovery lease was lost")
        return job

    def _renew_lease(self, job: ImportJob) -> None:
        now = _now()
        job.heartbeat_at = now
        job.updated_at = now
        job.lease_expires_at = now + timedelta(seconds=self._settings.import_lease_seconds)

    def _transition_item(
        self,
        job_id: uuid.UUID,
        item_id: uuid.UUID,
        status: ImportJobItemStatus,
        *,
        error: str | None = None,
        storage_etag: str | None = None,
    ) -> ImportJobItem:
        self._owned_job(job_id)
        item = self._session.get(ImportJobItem, item_id)
        if item is None:
            raise RuntimeError("import item disappeared")
        require_item_transition(item.status, status)
        if status == ImportJobItemStatus.uploading:
            item.attempt_count += 1
        item.status = status
        item.error = error
        item.updated_at = _now()
        if status in (ImportJobItemStatus.succeeded, ImportJobItemStatus.skipped):
            item.verified_at = _now()
            item.storage_etag = storage_etag
        job = self._owned_job(job_id)
        self._renew_lease(job)
        if status == ImportJobItemStatus.uploading and job.status != ImportJobStatus.uploading:
            require_job_transition(job.status, ImportJobStatus.uploading)
            job.status = ImportJobStatus.uploading
        job.checkpoint = {"version": 1, "phase": job.status.value, "item": item.item_key}
        self._session.commit()
        return item

    def _process_object_item(
        self, job_id: uuid.UUID, item_id: uuid.UUID, source_path: Path
    ) -> None:
        item = self._session.get(ImportJobItem, item_id)
        if item is None or item.status in (
            ImportJobItemStatus.failed,
            ImportJobItemStatus.quarantined,
        ):
            return
        if self._storage is None:
            self._transition_item(
                job_id,
                item_id,
                ImportJobItemStatus.failed,
                error="object storage is not configured",
            )
            return

        if item.status in (ImportJobItemStatus.succeeded, ImportJobItemStatus.skipped):
            try:
                verification = self._storage.verify_object(
                    item.object_key,
                    expected_sha256=item.sha256,
                    expected_size=item.byte_size,
                )
            except StorageError:
                verification = None
            if verification is not None and verification.verified:
                self._heartbeat(job_id)
                return
            if verification is not None and verification.exists:
                self._transition_item(
                    job_id,
                    item_id,
                    ImportJobItemStatus.quarantined,
                    error="immutable storage object changed",
                )
                return

        item = self._transition_item(job_id, item_id, ImportJobItemStatus.uploading)
        self._checkpoint("after_item_claim", job_id)
        reused = False
        try:
            metadata = self._storage.head_object(item.object_key)
            if metadata is not None:
                verification = self._storage.verify_object(
                    item.object_key,
                    expected_sha256=item.sha256,
                    expected_size=item.byte_size,
                )
                if not verification.verified:
                    self._transition_item(
                        job_id,
                        item_id,
                        ImportJobItemStatus.quarantined,
                        error="immutable storage object changed",
                    )
                    return
                reused = True
            else:
                source_hash, source_size = self._hash_file(source_path)
                if source_hash != item.sha256 or (
                    item.byte_size is not None and source_size != item.byte_size
                ):
                    self._transition_item(
                        job_id,
                        item_id,
                        ImportJobItemStatus.failed,
                        error="source content changed since scan",
                    )
                    return
                result = self._storage.upload_file(
                    item.object_key,
                    source_path,
                    item.mime_type or "application/octet-stream",
                )
                if result.sha256 != item.sha256 or (
                    item.byte_size is not None and result.byte_size != item.byte_size
                ):
                    self._transition_item(
                        job_id,
                        item_id,
                        ImportJobItemStatus.quarantined,
                        error="uploaded object metadata changed",
                    )
                    return
                if not self._first_upload_seen:
                    self._first_upload_seen = True
                    self._checkpoint("after_first_upload", job_id)
        except (OSError, StorageError):
            self._transition_item(
                job_id,
                item_id,
                ImportJobItemStatus.failed,
                error="source or storage operation failed safely",
            )
            return

        self._transition_item(job_id, item_id, ImportJobItemStatus.verifying)
        self._checkpoint("during_verification", job_id)
        try:
            verification = self._storage.verify_object(
                item.object_key,
                expected_sha256=item.sha256,
                expected_size=item.byte_size,
            )
            if not verification.verified:
                self._transition_item(
                    job_id,
                    item_id,
                    ImportJobItemStatus.quarantined,
                    error="object verification failed",
                )
                return
            metadata = self._storage.head_object(item.object_key)
        except StorageError:
            self._transition_item(
                job_id,
                item_id,
                ImportJobItemStatus.failed,
                error="object verification failed",
            )
            return
        self._transition_item(
            job_id,
            item_id,
            ImportJobItemStatus.skipped if reused else ImportJobItemStatus.succeeded,
            storage_etag=metadata.etag if metadata else None,
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _resolve_item_source(root: Path, reference: str) -> Path:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe persisted source reference")
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("unsafe persisted source reference") from exc
        return resolved

    def _heartbeat(self, job_id: uuid.UUID) -> None:
        job = self._owned_job(job_id)
        self._renew_lease(job)
        self._session.commit()

    def _publish_chapter(
        self,
        job_id: uuid.UUID,
        series_manifest: ManifestSeries,
        chapter_manifest: ManifestChapter,
        prune: bool,
        pruned_keys: list[str],
    ) -> bool:
        page_items = {
            item.page_number: item
            for item in self._session.query(ImportJobItem)
            .filter(
                ImportJobItem.job_id == job_id,
                ImportJobItem.chapter_id == chapter_manifest.chapter_id,
                ImportJobItem.item_kind == "page",
            )
            .all()
        }
        for page in chapter_manifest.pages:
            item = page_items.get(page.page_number)
            if item is None or item.status not in (
                ImportJobItemStatus.succeeded,
                ImportJobItemStatus.skipped,
            ):
                self._record_failed_chapter(series_manifest, chapter_manifest)
                return False
            if self._storage is None:
                return False
            try:
                verification = self._storage.verify_object(
                    item.object_key,
                    expected_sha256=page.sha256,
                    expected_size=page.byte_size,
                )
            except StorageError:
                verification = None
            if verification is None or not verification.verified:
                target = (
                    ImportJobItemStatus.quarantined
                    if verification is not None and verification.exists
                    else ImportJobItemStatus.failed
                )
                self._transition_item(
                    job_id,
                    item.id,
                    target,
                    error=(
                        "immutable storage object changed"
                        if target == ImportJobItemStatus.quarantined
                        else "storage object is missing"
                    ),
                )
                self._record_failed_chapter(series_manifest, chapter_manifest)
                return False

        self._checkpoint("before_chapter_ready", job_id)
        self._owned_job(job_id)
        existing_series = (
            self._session.query(Series)
            .filter(Series.slug == series_manifest.slug)
            .with_for_update()
            .first()
        )
        if existing_series is None:
            existing_series = Series(
                id=series_manifest.series_id,
                slug=series_manifest.slug,
                title=series_manifest.title,
                content_type=series_manifest.content_type,
            )
            self._session.add(existing_series)
            self._session.flush()
        else:
            existing_series.title = series_manifest.title

        cover_item = (
            self._session.query(ImportJobItem)
            .filter(
                ImportJobItem.job_id == job_id,
                ImportJobItem.item_key == f"cover:{series_manifest.series_id}",
                ImportJobItem.status.in_(
                    [ImportJobItemStatus.succeeded, ImportJobItemStatus.skipped]
                ),
            )
            .first()
        )
        if cover_item is not None:
            existing_series.cover_object_key = cover_item.object_key

        chapter = (
            self._session.query(Chapter)
            .filter(
                Chapter.series_id == existing_series.id,
                Chapter.number == chapter_manifest.number,
                Chapter.language == chapter_manifest.language,
            )
            .with_for_update()
            .first()
        )
        if chapter is None:
            chapter = Chapter(
                id=chapter_manifest.chapter_id,
                series_id=existing_series.id,
                number=chapter_manifest.number,
                language=chapter_manifest.language,
                title=chapter_manifest.title,
                page_count=len(chapter_manifest.pages),
                import_status=ChapterImportStatus.ready,
                verified_at=_now(),
            )
            self._session.add(chapter)
            self._session.flush()
        desired_keys: set[str] = set()
        for page in chapter_manifest.pages:
            item = page_items[page.page_number]
            desired_keys.add(item.object_key)
            self._upsert_page(
                chapter.id,
                page,
                object_key=item.object_key,
                status=PageIntegrityStatus.verified,
                storage_etag=item.storage_etag,
            )
        if prune:
            stale = (
                self._session.query(Page)
                .filter(Page.chapter_id == chapter.id, ~Page.object_key.in_(desired_keys))
                .all()
            )
            for page in stale:
                pruned_keys.append(page.object_key)
                self._session.delete(page)
        chapter.title = chapter_manifest.title
        chapter.page_count = len(chapter_manifest.pages)
        chapter.import_status = ChapterImportStatus.ready
        chapter.verified_at = _now()
        job = self._owned_job(job_id)
        self._renew_lease(job)
        self._session.commit()
        self._checkpoint("after_chapter_ready", job_id)
        return True

    def _record_failed_chapter(
        self, series_manifest: ManifestSeries, chapter_manifest: ManifestChapter
    ) -> None:
        chapter = self._session.get(Chapter, chapter_manifest.chapter_id)
        if chapter is not None:
            if chapter.import_status == ChapterImportStatus.importing:
                chapter.import_status = ChapterImportStatus.failed
                chapter.verified_at = None
                self._session.commit()
            return
        series = self._session.get(Series, series_manifest.series_id)
        if series is None:
            series = Series(
                id=series_manifest.series_id,
                slug=series_manifest.slug,
                title=series_manifest.title,
                content_type=series_manifest.content_type,
            )
            self._session.add(series)
            self._session.flush()
        self._session.add(
            Chapter(
                id=chapter_manifest.chapter_id,
                series_id=series.id,
                number=chapter_manifest.number,
                language=chapter_manifest.language,
                title=chapter_manifest.title,
                page_count=len(chapter_manifest.pages),
                import_status=ChapterImportStatus.failed,
            )
        )
        self._session.commit()

    def _finalize_job(
        self,
        job_id: uuid.UUID,
        manifest: list[ManifestSeries],
        published_chapters: int,
    ) -> ImportJob:
        job = self._owned_job(job_id)
        items = self._session.query(ImportJobItem).filter(ImportJobItem.job_id == job_id)
        uploaded = items.filter(ImportJobItem.status == ImportJobItemStatus.succeeded).count()
        skipped = items.filter(ImportJobItem.status == ImportJobItemStatus.skipped).count()
        failed = items.filter(
            ImportJobItem.status.in_(
                [ImportJobItemStatus.failed, ImportJobItemStatus.quarantined]
            )
        ).count()
        expected_chapters = sum(len(series.chapters) for series in manifest)
        job.series_count = len(manifest)
        job.chapter_count = expected_chapters
        job.page_count = sum(
            len(chapter.pages) for series in manifest for chapter in series.chapters
        )
        job.uploaded_count = uploaded
        job.skipped_count = skipped
        job.failed_count = failed
        if failed == 0 and published_chapters == expected_chapters:
            target = ImportJobStatus.succeeded
        elif published_chapters > 0:
            target = ImportJobStatus.partial
        else:
            target = ImportJobStatus.failed
        require_job_transition(job.status, target)
        job.status = target
        job.finished_at = _now()
        job.checkpoint = {"version": 1, "phase": target.value}
        job.lease_owner_id = None
        job.lease_expires_at = None
        job.updated_at = _now()
        if target != ImportJobStatus.succeeded and not job.error_summary:
            job.error_summary = "one or more import items could not be recovered safely"
        self._session.commit()
        event = record_event_safe(
            self._session,
            event_type="import.recovered" if self._recovering else "import.completed",
            outcome=AuditEventOutcome.success if target == ImportJobStatus.succeeded else AuditEventOutcome.failure,
            actor_user_id=job.requested_by_user_id,
            subject_type="import_job",
            subject_id=str(job.id),
            metadata={
                "status": target.value,
                "item_count": uploaded + skipped + failed,
                "page_count": job.page_count,
            },
        )
        if event is not None:
            self._session.commit()
        self._checkpoint("after_job_finalize", job_id)
        return job

    def fail_unrecoverable(self, job_id: uuid.UUID, reason: str) -> ImportJob:
        """Fail an active job with a caller-supplied, already-safe reason."""
        lock = SeriesImportLock(self._session, f"job:{job_id}")
        if not lock.acquire(blocking=False):
            return self._session.get(ImportJob, job_id)
        try:
            job = (
                self._session.query(ImportJob)
                .filter(ImportJob.id == job_id)
                .with_for_update()
                .one()
            )
            if job.status in TERMINAL_JOB_STATES:
                return job
            require_job_transition(job.status, ImportJobStatus.failed)
            job.status = ImportJobStatus.failed
            job.finished_at = _now()
            job.heartbeat_at = _now()
            job.updated_at = _now()
            job.lease_owner_id = None
            job.lease_expires_at = None
            if not job.error_summary:
                job.error_summary = self._safe_failure_reason(reason)
            job.checkpoint = {"version": 1, "phase": "failed"}
            self._session.commit()
            event = record_event_safe(
                self._session,
                event_type="import.failed",
                outcome=AuditEventOutcome.failure,
                actor_user_id=job.requested_by_user_id,
                subject_type="import_job",
                subject_id=str(job.id),
                metadata={"reason_code": "unrecoverable"},
            )
            if event is not None:
                self._session.commit()
            return job
        finally:
            lock.release()

    @staticmethod
    def _safe_failure_reason(reason: str) -> str:
        allowed = {
            "recovery manifest is unavailable",
            "recovery manifest failed validation",
            "safe recovery could not reconstruct the import",
        }
        return reason if reason in allowed else "safe import recovery failed"

    def _delete_pruned_objects(self, keys: list[str]) -> None:
        if self._dry_run or self._storage is None:
            return
        for key in keys:
            referenced = self._session.query(Page.id).filter(Page.object_key == key).first()
            if referenced is not None:
                continue
            try:
                self._storage.delete_object(key)
            except StorageError:
                log.warning("deferred stale-object deletion failed")

    def status(self, job_id: uuid.UUID) -> ImportJob:
        job = self._session.get(ImportJob, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} not found")
        return job

    def reconcile(
        self, *, prune: bool = False, source_key: str = "local"
    ) -> dict[str, object]:
        from .reconciliation import reconcile_database

        return reconcile_database(
            self._session,
            storage=self._storage,
            dry_run=self._dry_run,
            prune=prune,
            source_key=source_key,
        )

    # Kept for Phase 3 callers that exercise individual storage operations.
    def _upload_page(
        self, page: ManifestPage, series_id: uuid.UUID, chapter_id: uuid.UUID
    ) -> str:
        key = page_object_key(
            series_id, chapter_id, page.page_number, page.sha256, page.file_extension
        )
        if self._dry_run:
            return key
        if self._storage is None:
            raise StorageError("object storage is not configured")
        existing = self._storage.head_object(key)
        if existing is None:
            source_hash, source_size = self._hash_file(Path(page.source_path))
            if source_hash != page.sha256 or source_size != page.byte_size:
                raise StorageError("source content changed since scan")
            self._storage.upload_file(key, page.source_path, page.mime_type)
        verification = self._storage.verify_object(
            key, expected_sha256=page.sha256, expected_size=page.byte_size
        )
        if not verification.verified:
            raise StorageError("object verification failed")
        return key
