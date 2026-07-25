"""Transactional import service with SQLAlchemy ORM."""

from __future__ import annotations

import logging
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from database import get_settings
from models import (
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
from settings import Settings
from storage import ObjectStorage, StorageError, create_object_storage, page_object_key

from .adapters.base import ManifestChapter, ManifestPage, ManifestSeries
from .manifest import hash_manifest
from .validation import validate_manifest_strict

log = logging.getLogger("importing.service")


class SeriesImportLock:
    """Advisory lock for a single series using PostgreSQL or threading fallback."""

    _PG_LOCK_ACQUIRED: dict[int, bool] = {}

    def __init__(self, session: Session, series_slug: str) -> None:
        self._session = session
        self._lock_key = zlib.adler32(series_slug.encode("utf-8")) & 0x7FFFFFFF
        self._is_pg = session.bind.dialect.name == "postgresql"

    def acquire(self) -> None:
        if self._is_pg:
            self._session.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": self._lock_key}
            )
            self._PG_LOCK_ACQUIRED[self._lock_key] = True
        else:
            import threading

            if not hasattr(SeriesImportLock, "_thread_locks"):
                SeriesImportLock._thread_locks = {}
            if self._lock_key not in SeriesImportLock._thread_locks:
                SeriesImportLock._thread_locks[self._lock_key] = threading.Lock()
            SeriesImportLock._thread_locks[self._lock_key].acquire()

    def release(self) -> None:
        if self._is_pg:
            self._session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key}
            )
            self._PG_LOCK_ACQUIRED.pop(self._lock_key, None)
        else:

            lock = getattr(SeriesImportLock, "_thread_locks", {}).get(self._lock_key)
            if lock is not None:
                try:
                    lock.release()
                except RuntimeError:
                    pass

    def __enter__(self) -> "SeriesImportLock":
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ImportService:
    """Manages the lifecycle of import jobs with transactional guarantees.

    Each durable state change (job creation, chapter status transitions, page
    inserts) runs in its own short transaction.  Network I/O (uploads,
    HEAD verification) happens outside transactions.
    """

    def __init__(
        self,
        session: Session,
        storage: ObjectStorage | None = None,
        *,
        dry_run: bool = False,
    ) -> None:
        self._session = session
        self._storage = storage
        self._dry_run = dry_run

    @classmethod
    def from_settings(
        cls,
        session: Session,
        *,
        dry_run: bool = False,
        settings: Settings | None = None,
    ) -> "ImportService":
        cfg = settings or get_settings()
        storage = create_object_storage(cfg)
        return cls(session, storage=storage, dry_run=dry_run)

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
            existing.verified_at = None
            self._session.flush()
            return existing

        ch = Chapter(
            id=chapter.chapter_id,
            series_id=series_id,
            number=chapter.number,
            title=chapter.title,
            language=chapter.language,
            page_count=len(chapter.pages),
            import_status=status,
        )
        self._session.add(ch)
        self._session.flush()
        return ch

    def _upsert_page(
        self,
        chapter_id: uuid.UUID,
        page: ManifestPage,
        *,
        object_key: str,
        status: PageIntegrityStatus = PageIntegrityStatus.pending,
    ) -> Page:
        existing = (
            self._session.query(Page)
            .filter(Page.chapter_id == chapter_id, Page.page_number == page.page_number)
            .first()
        )
        if existing:
            existing.object_key = object_key
            existing.width = page.width
            existing.height = page.height
            existing.file_size = page.byte_size
            existing.sha256 = page.sha256
            existing.mime_type = page.mime_type
            existing.file_extension = page.file_extension
            existing.integrity_status = status
            existing.verified_at = None if status != PageIntegrityStatus.verified else _now()
            existing.imported_at = _now()
            self._session.flush()
            return existing

        pg = Page(
            chapter_id=chapter_id,
            page_number=page.page_number,
            object_key=object_key,
            width=page.width,
            height=page.height,
            file_size=page.byte_size,
            sha256=page.sha256,
            mime_type=page.mime_type,
            file_extension=page.file_extension,
            integrity_status=status,
            verified_at=_now() if status == PageIntegrityStatus.verified else None,
            imported_at=_now(),
        )
        self._session.add(pg)
        self._session.flush()
        return pg

    def _upload_page(self, page: ManifestPage, series_id: uuid.UUID, chapter_id: uuid.UUID) -> str:
        """Upload a page and return the object key. Raises on failure."""
        object_key = page_object_key(
            series_id, chapter_id, page.page_number, page.sha256, page.file_extension
        )
        if self._dry_run:
            log.info("    DRY-RUN upload: %s", object_key)
            return object_key
        if self._storage is None:
            raise StorageError("object storage is not configured")

        source_path = Path(page.source_path)
        existing_meta = self._storage.head_object(object_key)
        if existing_meta is not None:
            verification = self._storage.verify_object(
                object_key, expected_sha256=page.sha256, expected_size=page.byte_size
            )
            if verification.verified:
                log.debug("    SKIP (exists) %s", object_key)
                return object_key
            raise StorageError(f"existing object failed verification for key {object_key}")

        self._storage.upload_file(object_key, source_path, page.mime_type)
        verification = self._storage.verify_object(
            object_key, expected_sha256=page.sha256, expected_size=page.byte_size
        )
        if not verification.verified:
            raise StorageError(f"uploaded object failed verification for key {object_key}")
        log.debug("    UPLOADED %s", object_key)
        return object_key

    def _upload_cover(self, manifest: ManifestSeries) -> str | None:
        """Upload the cover image and return the object key."""
        if not manifest.cover_source_path or not manifest.cover_object_key:
            return None
        if self._dry_run:
            log.info("    DRY-RUN cover upload: %s", manifest.cover_object_key)
            return manifest.cover_object_key
        if self._storage is None:
            return manifest.cover_object_key

        source_path = Path(manifest.cover_source_path)
        existing_meta = self._storage.head_object(manifest.cover_object_key)
        if existing_meta is not None:
            verification = self._storage.verify_object(
                manifest.cover_object_key,
                expected_sha256=manifest.cover_sha256,
            )
            if verification.verified:
                return manifest.cover_object_key
            raise StorageError(f"cover object failed verification for key {manifest.cover_object_key}")

        self._storage.upload_file(
            manifest.cover_object_key, source_path, manifest.cover_mime_type or "application/octet-stream"
        )
        verification = self._storage.verify_object(
            manifest.cover_object_key, expected_sha256=manifest.cover_sha256
        )
        if not verification.verified:
            raise StorageError(f"uploaded cover failed verification for key {manifest.cover_object_key}")
        return manifest.cover_object_key

    def _finalize_chapter(
        self,
        chapter_id: uuid.UUID,
        *,
        status: ChapterImportStatus,
    ) -> None:
        ch = self._session.get(Chapter, chapter_id)
        if ch is None:
            return
        ch.import_status = status
        if status == ChapterImportStatus.ready:
            ch.verified_at = _now()
        self._session.flush()

    def _finalize_job(
        self,
        job: ImportJob,
        manifest: list[ManifestSeries],
    ) -> None:
        chapter_ids = [chapter.chapter_id for series in manifest for chapter in series.chapters]
        total_chapters = self._session.query(Chapter).filter(Chapter.id.in_(chapter_ids)).count()

        succeeded = self._session.query(Chapter).filter(
            Chapter.id.in_(chapter_ids),
            Chapter.import_status == ChapterImportStatus.ready,
        ).count()

        uploaded = (
            self._session.query(func.count(ImportJobItem.id))
            .filter(
                ImportJobItem.job_id == job.id,
                ImportJobItem.status == ImportJobItemStatus.succeeded,
            )
            .scalar()
            or 0
        )

        skipped = (
            self._session.query(func.count(ImportJobItem.id))
            .filter(
                ImportJobItem.job_id == job.id,
                ImportJobItem.status == ImportJobItemStatus.skipped,
            )
            .scalar()
            or 0
        )

        failed = (
            self._session.query(func.count(ImportJobItem.id))
            .filter(
                ImportJobItem.job_id == job.id,
                ImportJobItem.status == ImportJobItemStatus.failed,
            )
            .scalar()
            or 0
        )

        total_pages = (
            self._session.query(func.count(Page.id))
            .filter(Page.chapter_id.in_(chapter_ids))
            .scalar()
            or 0
        )

        job.series_count = len(manifest)
        job.chapter_count = total_chapters
        job.uploaded_count = uploaded
        job.skipped_count = skipped
        job.failed_count = failed
        job.page_count = total_pages

        if failed == 0:
            job.status = ImportJobStatus.succeeded
        elif succeeded > 0:
            job.status = ImportJobStatus.partial
        else:
            job.status = ImportJobStatus.failed

        job.finished_at = _now()
        self._session.flush()

    def _prune_stale_pages(
        self,
        chapter_id: uuid.UUID,
        current_page_keys: set[str],
    ) -> list[str]:
        """Remove stale pages from a chapter. Returns pruned object keys."""
        stale = (
            self._session.query(Page)
            .filter(Page.chapter_id == chapter_id, ~Page.object_key.in_(current_page_keys))
            .all()
        )
        pruned_keys: list[str] = []
        for page in stale:
            pruned_keys.append(page.object_key)
            self._session.delete(page)
        if pruned_keys:
            self._session.flush()
        return pruned_keys

    def _delete_pruned_objects(self, keys: list[str]) -> None:
        """Delete objects from storage after DB commit. Best-effort."""
        if self._dry_run or self._storage is None or not keys:
            return
        for key in keys:
            try:
                self._storage.delete_object(key)
                log.info("    DELETED stale object: %s", key)
            except StorageError as exc:
                log.warning("    failed to delete stale object %s: %s", key, exc)

    def run(
        self,
        manifest: list[ManifestSeries],
        *,
        source_key: str = "local",
        source_display_name: str = "Local Import",
        prune: bool = False,
    ) -> ImportJob:
        """Execute the full import pipeline. Returns the ImportJob record."""
        validate_manifest_strict(manifest)
        manifest_hash = hash_manifest(manifest)
        idempotency_key = f"local:{manifest_hash}"

        first_series_slug = manifest[0].slug if manifest else "unknown"

        with SeriesImportLock(self._session, first_series_slug):
            existing_job = (
                self._session.query(ImportJob)
                .filter(ImportJob.idempotency_key == idempotency_key)
                .first()
            )

            if existing_job is not None:
                if existing_job.status in (ImportJobStatus.succeeded, ImportJobStatus.partial):
                    log.info("manifest already imported (job %s, status %s)", existing_job.id, existing_job.status.value)
                    return existing_job
                if existing_job.status in (ImportJobStatus.pending, ImportJobStatus.scanning, ImportJobStatus.uploading):
                    return self._resume_job(existing_job, manifest, prune=prune)

            if not self._dry_run:
                source = (
                    self._session.query(Source)
                    .filter(Source.key == source_key)
                    .first()
                )
                if source is None:
                    source = Source(
                        key=source_key,
                        display_name=source_display_name,
                        adapter_type="local",
                    )
                    self._session.add(source)
                    self._session.flush()

                job = ImportJob(
                    source_id=source.id,
                    status=ImportJobStatus.pending,
                    idempotency_key=idempotency_key,
                    manifest_hash=manifest_hash,
                )
                self._session.add(job)
                self._session.flush()
            else:
                job = ImportJob(
                    id=uuid.uuid4(),
                    status=ImportJobStatus.pending,
                    idempotency_key=idempotency_key,
                    manifest_hash=manifest_hash,
                )

        return self._execute_job(job, manifest, prune=prune)

    def _resume_job(
        self,
        job: ImportJob,
        manifest: list[ManifestSeries],
        *,
        prune: bool = False,
    ) -> ImportJob:
        log.info("resuming existing job %s (status %s)", job.id, job.status.value)
        if not self._dry_run:
            job.status = ImportJobStatus.scanning
            job.started_at = job.started_at or _now()
            self._session.flush()
        return self._execute_job(job, manifest, prune=prune)

    def _execute_job(
        self,
        job: ImportJob,
        manifest: list[ManifestSeries],
        *,
        prune: bool = False,
    ) -> ImportJob:
        if not self._dry_run:
            job.status = ImportJobStatus.scanning
            job.started_at = job.started_at or _now()
            self._session.flush()

        all_pruned_keys: list[str] = []

        for series_manifest in manifest:
            try:
                self._process_series(job, series_manifest, prune=prune, all_pruned_keys=all_pruned_keys)
            except Exception as exc:
                log.error("series '%s' failed: %s", series_manifest.slug, exc)
                if not self._dry_run:
                    job.error_summary = f"series '{series_manifest.slug}': {exc}"
                    self._session.flush()

        if not self._dry_run:
            self._finalize_job(job, manifest)
            self._session.commit()
            self._delete_pruned_objects(all_pruned_keys)

        log.info(
            "import %s: %d uploaded, %d skipped, %d failed",
            job.id,
            job.uploaded_count,
            job.skipped_count,
            job.failed_count,
        )
        return job

    def _process_series(
        self,
        job: ImportJob,
        series_manifest: ManifestSeries,
        *,
        prune: bool,
        all_pruned_keys: list[str],
    ) -> None:
        if self._dry_run:
            log.info("  DRY-RUN series '%s': %d chapters", series_manifest.slug, len(series_manifest.chapters))
            return

        series = self._upsert_series(series_manifest)

        cover_key = self._upload_cover(series_manifest)
        if cover_key:
            series.cover_object_key = cover_key
            self._session.flush()

        for rejected in series_manifest.rejected_files:
            item = ImportJobItem(
                job_id=job.id,
                source_reference=rejected.source_reference,
                object_key="",
                status=(
                    ImportJobItemStatus.skipped
                    if rejected.status == "skipped"
                    else ImportJobItemStatus.failed
                ),
                error=rejected.reason[:2000],
            )
            self._session.add(item)
        if series_manifest.rejected_files:
            self._session.flush()

        for chapter_manifest in series_manifest.chapters:
            try:
                self._process_chapter(
                    job,
                    series.id,
                    chapter_manifest,
                    prune=prune,
                    all_pruned_keys=all_pruned_keys,
                )
            except Exception as exc:
                log.error(
                    "  chapter '%s' failed: %s", chapter_manifest.folder_name, exc
                )
                self._finalize_chapter(chapter_manifest.chapter_id, status=ChapterImportStatus.failed)
                item = ImportJobItem(
                    job_id=job.id,
                    source_reference=chapter_manifest.folder_name,
                    object_key="",
                    status=ImportJobItemStatus.failed,
                    error=str(exc)[:2000],
                )
                self._session.add(item)
                self._session.flush()

    def _process_chapter(
        self,
        job: ImportJob,
        series_id: uuid.UUID,
        chapter_manifest: ManifestChapter,
        *,
        prune: bool,
        all_pruned_keys: list[str],
    ) -> None:
        self._upsert_chapter(series_id, chapter_manifest, status=ChapterImportStatus.importing)

        uploaded = 0
        skipped = 0
        failed = 0

        for page_manifest in chapter_manifest.pages:
            try:
                object_key = self._upload_page(page_manifest, series_id, chapter_manifest.chapter_id)
                self._upsert_page(
                    chapter_manifest.chapter_id,
                    page_manifest,
                    object_key=object_key,
                    status=PageIntegrityStatus.verified,
                )
                if not self._dry_run:
                    item = ImportJobItem(
                        job_id=job.id,
                        source_reference=page_manifest.source_path,
                        object_key=object_key,
                        sha256=page_manifest.sha256,
                        status=ImportJobItemStatus.succeeded,
                    )
                    self._session.add(item)
                    self._session.flush()
                uploaded += 1
            except Exception as exc:
                failed += 1
                log.warning(
                    "    page %d failed: %s", page_manifest.page_number, exc
                )
                if not self._dry_run:
                    item = ImportJobItem(
                        job_id=job.id,
                        source_reference=page_manifest.source_path,
                        object_key="",
                        sha256=page_manifest.sha256,
                        status=ImportJobItemStatus.failed,
                        error=str(exc)[:2000],
                    )
                    self._session.add(item)
                    self._session.flush()

        current_page_keys = {
            page_object_key(
                series_id,
                chapter_manifest.chapter_id,
                p.page_number,
                p.sha256,
                p.file_extension,
            )
            for p in chapter_manifest.pages
        }

        if prune and not self._dry_run:
            pruned = self._prune_stale_pages(chapter_manifest.chapter_id, current_page_keys)
            all_pruned_keys.extend(pruned)

        if failed == 0:
            self._finalize_chapter(chapter_manifest.chapter_id, status=ChapterImportStatus.ready)
        else:
            self._finalize_chapter(chapter_manifest.chapter_id, status=ChapterImportStatus.failed)

        log.info(
            "  chapter '%s': %d uploaded, %d skipped, %d failed",
            chapter_manifest.folder_name,
            uploaded,
            skipped,
            failed,
        )

    def resume(self, job_id: uuid.UUID, *, prune: bool = False) -> ImportJob:
        """Resume a pending/importing job by its ID."""
        job = self._session.get(ImportJob, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} not found")

        if job.status not in (
            ImportJobStatus.pending,
            ImportJobStatus.scanning,
            ImportJobStatus.uploading,
            ImportJobStatus.verifying,
        ):
            log.info("job %s is already in terminal state %s", job_id, job.status.value)
            return job

        if not job.manifest_hash:
            raise ValueError(f"job {job_id} has no manifest_hash; cannot resume")

        existing_job = (
            self._session.query(ImportJob)
            .filter(
                ImportJob.idempotency_key == job.idempotency_key,
                ImportJob.id != job_id,
                ImportJob.status.in_([ImportJobStatus.succeeded, ImportJobStatus.partial]),
            )
            .first()
        )
        if existing_job is not None:
            log.info(
                "a newer job %s with the same idempotency key already completed; skipping resume",
                existing_job.id,
            )
            return existing_job

        log.warning(
            "cannot resume job %s without original manifest; job will remain in current state",
            job_id,
        )
        return job

    def status(self, job_id: uuid.UUID) -> ImportJob:
        """Return the current state of an import job."""
        job = self._session.get(ImportJob, job_id)
        if job is None:
            raise ValueError(f"import job {job_id} not found")
        return job

    def reconcile(
        self,
        *,
        prune: bool = False,
        source_key: str = "local",
    ) -> dict[str, object]:
        """Reconcile current DB state against storage.

        Reports removed pages. With prune=True, removes stale page references
        and deletes unreferenced objects after commit.
        """
        from .reconciliation import reconcile_database

        return reconcile_database(
            self._session,
            storage=self._storage,
            dry_run=self._dry_run,
            prune=prune,
            source_key=source_key,
        )
