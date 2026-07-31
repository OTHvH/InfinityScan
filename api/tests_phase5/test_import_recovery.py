"""Phase 5 crash simulation tests for import recovery."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from importing.adapters.local import LocalAdapter
from importing.service import ImportService, SeriesImportLock
from models import (
    Base,
    Chapter,
    ChapterImportStatus,
    ImportJob,
    ImportJobItem,
    ImportJobItemStatus,
    ImportJobStatus,
    Page,
)
from settings import Settings
from storage.base import DownloadResult, ObjectMetadata, ObjectVerification, UploadResult
from tools.import_recovery import recover_safe, scan_imports


DR_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dr_fixture.py"
DR_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "dr_fixture_for_tests", DR_FIXTURE_PATH
)
assert DR_FIXTURE_SPEC is not None and DR_FIXTURE_SPEC.loader is not None
dr_fixture = importlib.util.module_from_spec(DR_FIXTURE_SPEC)
DR_FIXTURE_SPEC.loader.exec_module(dr_fixture)


class SimulatedCrash(BaseException):
    pass


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.deleted: list[str] = []

    def head_object(self, key: str) -> ObjectMetadata | None:
        stored = self.objects.get(key)
        if stored is None:
            return None
        data, mime_type = stored
        return ObjectMetadata(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        )

    def upload_file(self, key: str, file_path: str | Path, mime_type: str) -> UploadResult:
        data = Path(file_path).read_bytes()
        self.objects[key] = (data, mime_type)
        return UploadResult(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        )

    def upload_bytes(self, key: str, data: bytes, mime_type: str) -> UploadResult:
        self.objects[key] = (data, mime_type)
        return UploadResult(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        )

    def download_file(
        self,
        key: str,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        max_bytes: int = 1024 * 1024 * 1024,
    ) -> DownloadResult:
        data, mime_type = self.objects[key]
        assert len(data) <= max_bytes
        Path(destination).write_bytes(data)
        return DownloadResult(
            key=key,
            byte_size=len(data),
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=hashlib.md5(data, usedforsecurity=False).hexdigest(),
        )

    def verify_object(
        self,
        key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        metadata: ObjectMetadata | None = None,
    ) -> ObjectVerification:
        stored = self.objects.get(key)
        if stored is None:
            return ObjectVerification(
                key=key,
                exists=False,
                verified=False,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                expected_size=expected_size,
                actual_size=None,
                reason="missing",
            )
        data, _ = stored
        actual_sha = hashlib.sha256(data).hexdigest()
        verified = (expected_sha256 is None or expected_sha256 == actual_sha) and (
            expected_size is None or expected_size == len(data)
        )
        return ObjectVerification(
            key=key,
            exists=True,
            verified=verified,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha,
            expected_size=expected_size,
            actual_size=len(data),
            reason=None if verified else "mismatch",
        )

    def delete_object(self, key: str) -> bool:
        self.deleted.append(key)
        self.objects.pop(key, None)
        return True

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def generate_presigned_get(self, key: str, expires_in: int | None = None) -> str:
        return "redacted"

    def list_objects_page(self, **_kwargs):
        raise NotImplementedError

    def health_check(self) -> bool:
        return True


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        import_pending_stale_seconds=1,
        import_scanning_stale_seconds=1,
        import_uploading_stale_seconds=1,
        import_verifying_stale_seconds=1,
        import_lease_seconds=30,
    )


@pytest.fixture()
def manifest(tmp_path: Path):
    chapter = tmp_path / "Recovery Series" / "Chapter 1"
    chapter.mkdir(parents=True)
    for number in (1, 2):
        image = Image.new("RGB", (8, 8), color=(number * 40, 80, 120))
        image.save(chapter / f"{number:03d}.jpg", format="JPEG")
    return LocalAdapter(tmp_path).scan()


def test_dr_fixture_creates_distinct_two_page_recovery_input(tmp_path: Path):
    root = tmp_path / "input"
    output = tmp_path / "input.json"

    dr_fixture.create_import_input(root, output)

    payload = json.loads(output.read_text(encoding="utf-8"))
    scanned = LocalAdapter(root).scan()
    pages = scanned[0].chapters[0].pages
    assert payload["page_count"] == 2
    assert [page.sha256 for page in pages] == [
        item["sha256"] for item in payload["pages"]
    ]
    assert pages[0].sha256 != pages[1].sha256
    assert not list(tmp_path.glob(".*.tmp-*"))

    with pytest.raises(RuntimeError, match="must be empty"):
        dr_fixture.create_import_input(root, output)


def _crash_at(name: str):
    def hook(checkpoint: str, _job_id) -> None:
        if checkpoint == name:
            raise SimulatedCrash(name)

    return hook


def _crashed_job(factory, storage, settings, manifest, checkpoint):
    session = factory()
    service = ImportService(
        session,
        storage=storage,
        settings=settings,
        checkpoint_hook=_crash_at(checkpoint),
    )
    with pytest.raises(SimulatedCrash):
        service.run(manifest)
    job = session.query(ImportJob).one()
    job_id = job.id
    session.close()
    return job_id


def test_crash_before_database_metadata_creation(sessions, settings, manifest):
    storage = MemoryStorage()
    session = sessions()
    service = ImportService(
        session,
        storage=storage,
        settings=settings,
        checkpoint_hook=_crash_at("during_validation"),
    )
    with pytest.raises(SimulatedCrash):
        service.run(manifest)
    assert session.query(ImportJob).count() == 0


@pytest.mark.parametrize(
    "checkpoint",
    [
        "after_job_creation",
        "after_first_upload",
        "after_all_uploads",
        "during_verification",
        "before_chapter_ready",
    ],
)
def test_process_restart_resumes_from_every_durable_stage(
    sessions, settings, manifest, checkpoint
):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, checkpoint)

    restarted = sessions()
    job = ImportService(restarted, storage=storage, settings=settings).resume(job_id)

    assert job.status == ImportJobStatus.succeeded
    assert restarted.query(Chapter).one().import_status == ChapterImportStatus.ready
    assert restarted.query(Page).count() == 2
    assert restarted.query(ImportJobItem).count() == 2


def test_crash_after_upload_reuses_existing_object(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "after_first_upload")
    object_count = len(storage.objects)

    session = sessions()
    job = ImportService(session, storage=storage, settings=settings).resume(job_id)

    assert job.status == ImportJobStatus.succeeded
    assert len(storage.objects) == object_count + 1
    assert session.query(ImportJobItem).filter(
        ImportJobItem.status == ImportJobItemStatus.skipped
    ).count() == 1


def test_missing_storage_object_is_reuploaded(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "before_chapter_ready")
    missing_key = next(iter(storage.objects))
    del storage.objects[missing_key]

    session = sessions()
    job = ImportService(session, storage=storage, settings=settings).resume(job_id)

    assert job.status == ImportJobStatus.succeeded
    assert missing_key in storage.objects
    assert session.query(Page).count() == 2


def test_changed_immutable_object_fails_without_publish_or_delete(
    sessions, settings, manifest
):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "before_chapter_ready")
    changed_key = next(iter(storage.objects))
    storage.objects[changed_key] = (b"changed", "image/jpeg")

    session = sessions()
    job = ImportService(session, storage=storage, settings=settings).resume(job_id)

    assert job.status == ImportJobStatus.failed
    assert session.query(Page).count() == 0
    assert session.query(Chapter).one().import_status == ChapterImportStatus.failed
    assert storage.deleted == []
    assert "/" not in (job.error_summary or "")


def test_recovery_repairs_counters(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "before_chapter_ready")
    session = sessions()
    job = session.get(ImportJob, job_id)
    job.series_count = 99
    job.chapter_count = 99
    job.page_count = 99
    job.uploaded_count = 99
    job.skipped_count = 99
    job.failed_count = 99
    session.commit()

    recovered = ImportService(session, storage=storage, settings=settings).resume(job_id)

    assert recovered.series_count == 1
    assert recovered.chapter_count == 1
    assert recovered.page_count == 2
    assert recovered.uploaded_count + recovered.skipped_count == 2
    assert recovered.failed_count == 0


def test_completed_job_is_not_recovered(sessions, settings, manifest):
    storage = MemoryStorage()
    session = sessions()
    completed = ImportService(session, storage=storage, settings=settings).run(manifest)

    result = recover_safe(
        session, storage=storage, settings=settings, job_id=completed.id
    )

    assert result.attempted == 0
    assert result.skipped_terminal == 1


def test_genuinely_active_job_is_not_recovered(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "after_job_creation")
    session = sessions()
    job = session.get(ImportJob, job_id)
    job.status = ImportJobStatus.uploading
    job.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=1)
    job.updated_at = job.heartbeat_at
    job.lease_owner_id = uuid.uuid4()
    job.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    session.commit()

    result = recover_safe(session, storage=storage, settings=settings, job_id=job_id)

    assert result.attempted == 0
    assert result.skipped_active == 1
    assert session.get(ImportJob, job_id).status == ImportJobStatus.uploading


def test_concurrent_recovery_attempt_is_serialized(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "after_job_creation")
    first = sessions()
    job = first.get(ImportJob, job_id)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    job.heartbeat_at = old
    job.updated_at = old
    job.lease_owner_id = None
    job.lease_expires_at = None
    first.commit()

    held = SeriesImportLock(first, f"job:{job_id}")
    assert held.acquire(blocking=False)
    try:
        second = sessions()
        result = recover_safe(second, storage=storage, settings=settings, job_id=job_id)
        assert result.attempted == 0
        assert result.skipped_active == 1
    finally:
        held.release()


def test_scan_detects_stale_upload_and_incomplete_finalize(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "after_first_upload")
    session = sessions()
    job = session.get(ImportJob, job_id)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    job.heartbeat_at = old
    job.updated_at = old
    job.lease_owner_id = None
    job.lease_expires_at = None
    session.commit()

    report = scan_imports(
        session,
        storage=storage,
        settings=settings,
        job_id=job_id,
        now=datetime.now(timezone.utc),
    )
    codes = {issue.code for issue in report.issues}

    assert "stuck_uploading" in codes
    assert "uploaded_before_checkpoint" in codes
    assert "abandoned_job_item" in codes


def test_error_evidence_never_contains_source_path(sessions, settings, manifest):
    storage = MemoryStorage()
    job_id = _crashed_job(sessions, storage, settings, manifest, "after_job_creation")
    source_path = manifest[0].chapters[0].pages[0].source_path
    Path(source_path).unlink()

    session = sessions()
    job = ImportService(session, storage=storage, settings=settings).resume(job_id)
    errors = [job.error_summary or ""] + [item.error or "" for item in job.items]

    assert job.status == ImportJobStatus.failed
    assert all(source_path not in error for error in errors)
