"""Tests for the transactional import service."""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from models import (
    Chapter,
    ChapterImportStatus,
    ImportJobItem,
    ImportJobItemStatus,
    ImportJobStatus,
    Page,
    PageIntegrityStatus,
    Series,
)
from storage.base import (
    DownloadResult,
    ObjectMetadata,
    ObjectVerification,
    StorageError,
    UploadResult,
)

from importing.adapters.base import ManifestChapter, ManifestPage, ManifestSeries
from importing.adapters.local import (
    LocalAdapter,
    extract_chapter_number,
    natural_key,
    slugify,
    stable_chapter_id,
    stable_series_id,
)
from importing.manifest import hash_manifest, manifest_to_dict, manifest_to_json
from importing.service import ImportService, SeriesImportLock, _safe_rejection_reason
from importing.validation import validate_manifest, validate_manifest_strict


# ---------------------------------------------------------------------------
# Fake storage for tests
# ---------------------------------------------------------------------------


class FakeStorage:
    """In-memory object storage for unit tests."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def head_object(self, key: str) -> ObjectMetadata | None:
        if key not in self.objects:
            return None
        data, mime = self.objects[key]
        return ObjectMetadata(
            key=key,
            byte_size=len(data),
            mime_type=mime,
            sha256=hashlib.sha256(data).hexdigest(),
            etag="fake-etag",
        )

    def upload_file(self, key: str, file_path: str | Path, mime_type: str) -> UploadResult:
        data = Path(file_path).read_bytes()
        self.objects[key] = (data, mime_type)
        sha256 = hashlib.sha256(data).hexdigest()
        return UploadResult(key=key, byte_size=len(data), mime_type=mime_type, sha256=sha256, etag="fake-etag")

    def upload_bytes(self, key: str, data: bytes, mime_type: str) -> UploadResult:
        self.objects[key] = (data, mime_type)
        sha256 = hashlib.sha256(data).hexdigest()
        return UploadResult(key=key, byte_size=len(data), mime_type=mime_type, sha256=sha256, etag="fake-etag")

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
            etag="fake-etag",
        )

    def delete_object(self, key: str) -> bool:
        self.objects.pop(key, None)
        return True

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def generate_presigned_get(self, key: str, expires_in: int | None = None) -> str:
        return f"fake://{key}"

    def verify_object(
        self,
        key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> ObjectVerification:
        if key not in self.objects:
            return ObjectVerification(
                key=key, exists=False, verified=False,
                expected_sha256=expected_sha256, actual_sha256=None,
                expected_size=expected_size, actual_size=None, reason="missing",
            )
        data, _ = self.objects[key]
        actual_sha256 = hashlib.sha256(data).hexdigest()
        actual_size = len(data)
        verified = (
            (expected_sha256 is None or actual_sha256 == expected_sha256)
            and (expected_size is None or actual_size == expected_size)
        )
        return ObjectVerification(
            key=key, exists=True, verified=verified,
            expected_sha256=expected_sha256 or actual_sha256,
            actual_sha256=actual_sha256,
            expected_size=expected_size or actual_size,
            actual_size=actual_size,
            reason=None if verified else "checksum_or_size_mismatch",
        )

    def health_check(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_image_file(tmp_path: Path, name: str = "page.jpg", content: bytes | None = None) -> Path:
    """Create a valid minimal JPEG file that Pillow can open."""
    path = tmp_path / name
    if content is not None:
        path.write_bytes(content)
        return path
    img = Image.new("RGB", (8, 8), color=(128, 128, 128))
    img.save(path, format="JPEG")
    return path


def _make_series_dir(
    tmp_path: Path,
    series_name: str = "Test Series",
    chapters: dict[str, int] | None = None,
) -> Path:
    """Create a directory structure: root/series_name/chapter_N/page.jpg"""
    if chapters is None:
        chapters = {"Chapter 001": 3, "Chapter 002": 2}
    series_dir = tmp_path / series_name
    series_dir.mkdir(parents=True, exist_ok=True)
    for ch_name, page_count in chapters.items():
        ch_dir = series_dir / ch_name
        ch_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, page_count + 1):
            _make_image_file(ch_dir, f"{i:03d}.jpg")
    return series_dir


# ---------------------------------------------------------------------------
# Tests: adapter helpers
# ---------------------------------------------------------------------------


class TestAdapterHelpers:
    def test_slugify_basic(self):
        assert slugify("Dragon Ball") == "dragon-ball"
        assert slugify("  spaces  ") == "spaces"
        assert slugify("Special@Chars!") == "specialchars"

    def test_extract_chapter_number(self):
        assert extract_chapter_number("Chapter 001") == Decimal("1")
        assert extract_chapter_number("ch1.5") == Decimal("1.5")
        assert extract_chapter_number("no_number") == Decimal("0")

    def test_natural_key_sorting(self):
        items = ["ch10", "ch2", "ch1"]
        items.sort(key=natural_key)
        assert items == ["ch1", "ch2", "ch10"]

    def test_stable_series_id_deterministic(self):
        id1 = stable_series_id("dragon-ball")
        id2 = stable_series_id("dragon-ball")
        assert id1 == id2

    def test_stable_chapter_id_deterministic(self):
        sid = stable_series_id("dragon-ball")
        id1 = stable_chapter_id(sid, Decimal("1"), "en")
        id2 = stable_chapter_id(sid, Decimal("1"), "en")
        assert id1 == id2

    def test_stable_chapter_id_differs_by_number(self):
        sid = stable_series_id("dragon-ball")
        id1 = stable_chapter_id(sid, Decimal("1"), "en")
        id2 = stable_chapter_id(sid, Decimal("2"), "en")
        assert id1 != id2

    def test_stable_chapter_id_differs_by_language(self):
        sid = stable_series_id("dragon-ball")
        id1 = stable_chapter_id(sid, Decimal("1"), "en")
        id2 = stable_chapter_id(sid, Decimal("1"), "ja")
        assert id1 != id2


# ---------------------------------------------------------------------------
# Tests: local adapter scanning
# ---------------------------------------------------------------------------


class TestLocalAdapter:
    def test_scan_single_series(self, tmp_path: Path):
        _make_series_dir(tmp_path, "My Manga", {"Ch1": 2, "Ch2": 3})
        adapter = LocalAdapter(tmp_path)
        manifest = adapter.scan()

        assert len(manifest) == 1
        s = manifest[0]
        assert s.slug == "my-manga"
        assert len(s.chapters) == 2
        total_pages = sum(len(c.pages) for c in s.chapters)
        assert total_pages == 5

    def test_scan_with_series_filter(self, tmp_path: Path):
        _make_series_dir(tmp_path, "Alpha", {"Ch1": 1})
        _make_series_dir(tmp_path, "Beta", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path, series_filter="Alpha")
        manifest = adapter.scan()
        assert len(manifest) == 1
        assert manifest[0].slug == "alpha"

    def test_scan_empty_dir(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        adapter = LocalAdapter(empty)
        manifest = adapter.scan()
        assert manifest == []

    def test_scan_flat_series(self, tmp_path: Path):
        series_dir = tmp_path / "Flat Series"
        series_dir.mkdir()
        _make_image_file(series_dir, "001.jpg")
        _make_image_file(series_dir, "002.jpg")
        adapter = LocalAdapter(tmp_path)
        manifest = adapter.scan()
        assert len(manifest) == 1
        assert len(manifest[0].chapters) == 1
        assert manifest[0].chapters[0].number == Decimal("1.0")

    def test_pages_have_stable_object_keys(self, tmp_path: Path):
        _make_series_dir(tmp_path, "Key Test", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path / "Key Test")
        manifest = adapter.scan()
        s = manifest[0]
        ch = s.chapters[0]
        page = ch.pages[0]
        from storage import page_object_key

        expected_key = page_object_key(
            s.series_id, ch.chapter_id, 1, page.sha256, page.file_extension
        )
        assert expected_key.startswith(f"series/{s.series_id}/")


# ---------------------------------------------------------------------------
# Tests: manifest hashing and serialization
# ---------------------------------------------------------------------------


class TestManifest:
    def _sample_manifest(self) -> list[ManifestSeries]:
        sid = stable_series_id("test")
        cid = stable_chapter_id(sid, Decimal("1"), "en")
        return [
            ManifestSeries(
                series_id=sid,
                folder_name="test",
                slug="test",
                title="Test",
                content_type="manga",
                chapters=(
                    ManifestChapter(
                        chapter_id=cid,
                        folder_name="Chapter 1",
                        number=Decimal("1"),
                        title=None,
                        language="en",
                        pages=(
                            ManifestPage(
                                page_number=1,
                                source_path="/tmp/p1.jpg",
                                sha256="a" * 64,
                                mime_type="image/jpeg",
                                file_extension="jpg",
                                width=100,
                                height=100,
                                byte_size=1024,
                            ),
                        ),
                    ),
                ),
            )
        ]

    def test_hash_manifest_deterministic(self):
        m = self._sample_manifest()
        h1 = hash_manifest(m)
        h2 = hash_manifest(m)
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_manifest_excludes_generation_time(self):
        m = self._sample_manifest()
        with patch(
            "importing.manifest.time.strftime",
            side_effect=["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
        ):
            assert hash_manifest(m) == hash_manifest(m)

    def test_hash_manifest_differs_for_different_content(self):
        m1 = self._sample_manifest()
        m2 = self._sample_manifest()
        m2_list = [ManifestSeries(
            series_id=m2[0].series_id,
            folder_name=m2[0].folder_name,
            slug=m2[0].slug,
            title="Different Title",
            content_type=m2[0].content_type,
            chapters=m2[0].chapters,
        )]
        assert hash_manifest(m1) != hash_manifest(m2_list)

    def test_manifest_to_dict_is_serializable(self):
        m = self._sample_manifest()
        d = manifest_to_dict(m)
        assert isinstance(d, dict)
        assert d["series_count"] == 1

    def test_manifest_to_json_is_valid_json(self):
        m = self._sample_manifest()
        j = manifest_to_json(m)
        parsed = __import__("json").loads(j)
        assert parsed["series_count"] == 1


# ---------------------------------------------------------------------------
# Tests: validation
# ---------------------------------------------------------------------------


class TestValidation:
    def _valid_manifest(self) -> list[ManifestSeries]:
        sid = stable_series_id("valid")
        cid = stable_chapter_id(sid, Decimal("1"), "en")
        return [
            ManifestSeries(
                series_id=sid,
                folder_name="valid",
                slug="valid",
                title="Valid",
                content_type="manga",
                chapters=(
                    ManifestChapter(
                        chapter_id=cid,
                        folder_name="Ch1",
                        number=Decimal("1"),
                        title=None,
                        language="en",
                        pages=(
                            ManifestPage(
                                page_number=1,
                                source_path="/tmp/p.jpg",
                                sha256="a" * 64,
                                mime_type="image/jpeg",
                                file_extension="jpg",
                                width=100,
                                height=100,
                                byte_size=100,
                            ),
                        ),
                    ),
                ),
            )
        ]

    def test_valid_manifest_passes(self):
        errors = validate_manifest(self._valid_manifest())
        assert errors == []

    def test_empty_manifest_fails(self):
        errors = validate_manifest([])
        assert len(errors) == 1

    def test_missing_pages_fails(self):
        m = self._valid_manifest()
        m_list = [ManifestSeries(
            series_id=m[0].series_id,
            folder_name=m[0].folder_name,
            slug=m[0].slug,
            title=m[0].title,
            content_type=m[0].content_type,
            chapters=(
                ManifestChapter(
                    chapter_id=m[0].chapters[0].chapter_id,
                    folder_name="Ch1",
                    number=Decimal("1"),
                    title=None,
                    language="en",
                    pages=(),
                ),
            ),
        )]
        errors = validate_manifest(m_list)
        assert any("no pages" in str(e) for e in errors)

    def test_invalid_content_type_fails(self):
        m = self._valid_manifest()
        m_list = [ManifestSeries(
            series_id=m[0].series_id,
            folder_name=m[0].folder_name,
            slug=m[0].slug,
            title=m[0].title,
            content_type="invalid",
            chapters=m[0].chapters,
        )]
        errors = validate_manifest(m_list)
        assert any("content_type" in str(e) for e in errors)

    def test_strict_validation_raises(self):
        with pytest.raises(ValueError, match="manifest validation failed"):
            validate_manifest_strict([])


# ---------------------------------------------------------------------------
# Tests: SeriesImportLock
# ---------------------------------------------------------------------------


class TestSeriesImportLock:
    def test_lock_acquire_release(self, db):
        lock = SeriesImportLock(db, "test-series")
        lock.acquire()
        lock.release()

    def test_context_manager(self, db):
        with SeriesImportLock(db, "test-series"):
            pass


# ---------------------------------------------------------------------------
# Tests: ImportService - upserts
# ---------------------------------------------------------------------------


class TestImportServiceUpserts:
    def _make_manifest(self, tmp_path: Path) -> list[ManifestSeries]:
        _make_series_dir(tmp_path, "Upsert Test", {"Ch1": 1, "Ch2": 2})
        adapter = LocalAdapter(tmp_path)
        return adapter.scan()

    def test_upsert_series_creates_new(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        service = ImportService(db, storage=FakeStorage(), dry_run=True)
        series = service._upsert_series(manifest[0])
        db.commit()
        assert series.id == manifest[0].series_id
        assert series.slug == "upsert-test"

    def test_upsert_series_updates_existing(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        service = ImportService(db, storage=FakeStorage(), dry_run=True)
        service._upsert_series(manifest[0])
        db.commit()

        updated_manifest = [ManifestSeries(
            series_id=manifest[0].series_id,
            folder_name=manifest[0].folder_name,
            slug=manifest[0].slug,
            title="Updated Title",
            content_type=manifest[0].content_type,
            chapters=manifest[0].chapters,
        )]
        service._upsert_series(updated_manifest[0])
        db.commit()

        series = db.get(Series, manifest[0].series_id)
        assert series.title == "Updated Title"

    def test_upsert_chapter_creates_new(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        service = ImportService(db, storage=FakeStorage(), dry_run=True)
        series = service._upsert_series(manifest[0])
        chapter = service._upsert_chapter(
            series.id, manifest[0].chapters[0], status=ChapterImportStatus.importing
        )
        db.commit()
        assert chapter.id == manifest[0].chapters[0].chapter_id
        assert chapter.import_status == ChapterImportStatus.importing

    def test_upsert_chapter_updates_existing(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        service = ImportService(db, storage=FakeStorage(), dry_run=True)
        series = service._upsert_series(manifest[0])
        service._upsert_chapter(series.id, manifest[0].chapters[0])
        db.commit()

        chapter = service._upsert_chapter(
            series.id, manifest[0].chapters[0], status=ChapterImportStatus.ready
        )
        db.commit()
        assert chapter.import_status == ChapterImportStatus.ready


# ---------------------------------------------------------------------------
# Tests: ImportService - dry-run import
# ---------------------------------------------------------------------------


class TestImportServiceDryRun:
    def _make_manifest(self, tmp_path: Path) -> list[ManifestSeries]:
        _make_series_dir(tmp_path, "Dry Run", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path)
        return adapter.scan()

    def test_dry_run_does_not_write_to_db(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        service = ImportService(db, storage=FakeStorage(), dry_run=True)
        job = service.run(manifest)

        assert job.status == ImportJobStatus.pending
        assert db.query(Series).count() == 0
        assert db.query(Chapter).count() == 0
        assert db.query(Page).count() == 0


# ---------------------------------------------------------------------------
# Tests: ImportService - full import with FakeStorage
# ---------------------------------------------------------------------------


class TestImportServiceFullImport:
    def _make_manifest(self, tmp_path: Path) -> list[ManifestSeries]:
        _make_series_dir(tmp_path, "Full Import", {"Ch1": 2, "Ch2": 1})
        adapter = LocalAdapter(tmp_path)
        return adapter.scan()

    def test_full_import_creates_records(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        storage = FakeStorage()
        service = ImportService(db, storage=storage, dry_run=False)
        job = service.run(manifest)

        assert job.status == ImportJobStatus.succeeded
        assert db.query(Series).count() == 1
        assert db.query(Chapter).count() == 2
        assert db.query(Page).count() == 3

        series = db.query(Series).first()
        assert series.slug == "full-import"

        for ch in db.query(Chapter).all():
            assert ch.import_status == ChapterImportStatus.ready
            assert ch.verified_at is not None

        for page in db.query(Page).all():
            assert page.integrity_status == PageIntegrityStatus.verified
            assert page.verified_at is not None

        assert job.uploaded_count >= 3
        assert job.failed_count == 0
        assert job.series_count == 1
        assert job.chapter_count == 2
        assert job.page_count == 3

    def test_full_import_uploads_to_storage(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        storage = FakeStorage()
        service = ImportService(db, storage=storage, dry_run=False)
        service.run(manifest)

        assert len(storage.objects) >= 3
        for key in storage.objects:
            assert key.startswith("series/")

    def test_rejection_evidence_keeps_only_public_reason(self, db, tmp_path: Path):
        _make_series_dir(tmp_path, "Rejected Evidence", {"Ch1": 1})
        chapter = tmp_path / "Rejected Evidence" / "Ch1"
        (chapter / "002.txt").write_text("not an image", encoding="utf-8")
        service = ImportService(db, storage=FakeStorage(), dry_run=False)

        job = service.run(LocalAdapter(tmp_path).scan())

        item = db.query(ImportJobItem).filter(ImportJobItem.job_id == job.id).filter(
            ImportJobItem.item_kind == "rejected"
        ).one()
        assert item.status == ImportJobItemStatus.skipped
        assert item.error == "file extension is not an image candidate"
        assert str(tmp_path) not in item.error
        assert "secret" not in item.error.lower()
        assert _safe_rejection_reason(f"secret={tmp_path}/private") == "source file rejected during scan"

    def test_idempotent_import_skips_existing_objects(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        storage = FakeStorage()
        service = ImportService(db, storage=storage, dry_run=False)
        job1 = service.run(manifest)
        objects_after_first = len(storage.objects)

        job2 = service.run(manifest)
        assert job2.id == job1.id
        assert len(storage.objects) == objects_after_first

    def test_import_with_prune_removes_stale_pages(self, db, tmp_path: Path):
        manifest_v1 = self._make_manifest(tmp_path)
        storage = FakeStorage()
        service = ImportService(db, storage=storage, dry_run=False)
        service.run(manifest_v1)
        assert db.query(Page).count() == 3

        new_dir = tmp_path / "Full Import" / "Ch1"
        _make_image_file(new_dir, "003.jpg")

        adapter = LocalAdapter(tmp_path)
        manifest_v2 = adapter.scan()
        service2 = ImportService(db, storage=storage, dry_run=False)
        service2.run(manifest_v2, prune=True)

        ch1 = db.query(Chapter).filter(Chapter.number == Decimal("1")).first()
        assert ch1 is not None
        pages_for_ch1 = db.query(Page).filter(Page.chapter_id == ch1.id).count()
        assert pages_for_ch1 == 3

    def test_failed_page_marks_chapter_failed(self, db, tmp_path: Path):
        manifest = self._make_manifest(tmp_path)
        failing_storage = MagicMock(spec=FakeStorage)
        failing_storage.head_object.return_value = None

        def raise_upload(key, file_path, mime_type):
            raise StorageError("upload failed")

        failing_storage.upload_file.side_effect = raise_upload
        failing_storage.verify_object.return_value = ObjectVerification(
            key="fake", exists=False, verified=False,
            expected_sha256=None, actual_sha256=None,
            expected_size=None, actual_size=None, reason="missing",
        )

        service = ImportService(db, storage=failing_storage, dry_run=False)
        job = service.run(manifest)

        assert job.status in (ImportJobStatus.failed, ImportJobStatus.partial)
        failed_chapters = (
            db.query(Chapter)
            .filter(Chapter.import_status == ChapterImportStatus.failed)
            .count()
        )
        assert failed_chapters > 0


# ---------------------------------------------------------------------------
# Tests: ImportService - resume and status
# ---------------------------------------------------------------------------


class TestImportServiceResumeStatus:
    def test_status_returns_job(self, db, tmp_path: Path):
        _make_series_dir(tmp_path, "Status Test", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path)
        manifest = adapter.scan()
        service = ImportService(db, storage=FakeStorage(), dry_run=False)
        job = service.run(manifest)

        retrieved = service.status(job.id)
        assert retrieved.id == job.id
        assert retrieved.status == ImportJobStatus.succeeded

    def test_status_raises_for_missing_job(self, db):
        service = ImportService(db)
        with pytest.raises(ValueError, match="not found"):
            service.status(uuid.uuid4())

    def test_resume_returns_completed_job(self, db, tmp_path: Path):
        _make_series_dir(tmp_path, "Resume Test", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path)
        manifest = adapter.scan()
        service = ImportService(db, storage=FakeStorage(), dry_run=False)
        job = service.run(manifest)

        resumed = service.resume(job.id)
        assert resumed.status == ImportJobStatus.succeeded

    def test_resume_raises_for_missing_job(self, db):
        service = ImportService(db)
        with pytest.raises(ValueError, match="not found"):
            service.resume(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tests: reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_reconcile_reports_stale(self, db, tmp_path: Path):
        _make_series_dir(tmp_path, "Reconcile", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path)
        manifest = adapter.scan()
        service = ImportService(db, storage=FakeStorage(), dry_run=False)
        service.run(manifest)

        report = service.reconcile(prune=False)
        assert report["total_series"] == 1
        assert report["total_chapters"] == 1
        assert report["pruned_pages"] == 0

    def test_reconcile_prune_removes_stale(self, db, tmp_path: Path):
        _make_series_dir(tmp_path, "Reconcile Prune", {"Ch1": 1})
        adapter = LocalAdapter(tmp_path)
        manifest = adapter.scan()
        storage = FakeStorage()
        service = ImportService(db, storage=storage, dry_run=False)
        service.run(manifest)

        report = service.reconcile(prune=True)
        assert isinstance(report["pruned_pages"], int)
