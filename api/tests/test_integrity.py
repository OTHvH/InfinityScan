from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal


from conftest import _do_login
from models import (
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
    Series,
    SeriesStatus,
)
from sqlalchemy import func, select, text
from storage.base import ObjectMetadata, ObjectVerification
from tools.integrity import (
    IntegritySummary,
    check_abandoned_import_jobs,
    check_duplicate_page_positions,
    check_importing_chapters_exposed,
    check_invalid_image_dimensions,
    check_missing_storage_objects,
    check_orphaned_chapters,
    check_orphaned_pages,
    check_page_count_mismatch,
    check_sha256_mismatch,
    check_stale_replaced_objects,
    check_storage_size_mismatch,
    get_import_job,
    list_import_jobs,
    repair_safe,
    run_checks,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _create_series(db, slug: str = "test-series") -> Series:
    s = Series(
        slug=slug,
        title=f"Test {slug}",
        content_type=ContentType.manga,
        default_reading_mode=ReadingMode.paged,
        status=SeriesStatus.ongoing,
    )
    db.add(s)
    db.flush()
    return s


def _create_chapter(db, series: Series, number: float = 1.0, status: ChapterImportStatus = ChapterImportStatus.ready) -> Chapter:
    ch = Chapter(
        series_id=series.id,
        number=Decimal(str(number)),
        page_count=0,
        import_status=status,
    )
    db.add(ch)
    db.flush()
    return ch


def _create_page(db, chapter: Chapter, page_number: int = 1, **kwargs) -> Page:
    defaults = {
        "object_key": f"series/{chapter.series_id}/{chapter.id}/{page_number:05d}.jpg",
        "page_number": page_number,
        "integrity_status": PageIntegrityStatus.pending,
        "sha256": "a" * 64,
        "width": 800,
        "height": 1200,
        "file_size": 50000,
    }
    defaults.update(kwargs)
    p = Page(chapter_id=chapter.id, **defaults)
    db.add(p)
    db.flush()
    return p


def _create_import_job(
    db,
    status: ImportJobStatus = ImportJobStatus.pending,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
) -> ImportJob:
    job = ImportJob(
        idempotency_key=str(uuid.uuid4()),
        status=status,
        series_count=0,
        chapter_count=0,
        page_count=0,
        uploaded_count=0,
        failed_count=0,
    )
    if created_at:
        job.created_at = created_at
    if started_at:
        job.started_at = started_at
    db.add(job)
    db.flush()
    return job


class FakeStorage:
    """In-memory fake ObjectStorage for testing."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}

    def head_object(self, key: str) -> ObjectMetadata | None:
        if key not in self.objects:
            return None
        return ObjectMetadata(
            key=key,
            byte_size=len(self.objects[key]),
            mime_type="image/jpeg",
            sha256="a" * 64,
            etag=self.etags.get(key),
        )

    def upload_file(self, key, file_path, mime_type):
        pass

    def upload_bytes(self, key, data, mime_type):
        pass

    def delete_object(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def generate_presigned_get(self, key, expires_in=None):
        return f"https://fake/{key}"

    def verify_object(self, key, expected_sha256=None, expected_size=None) -> ObjectVerification:
        if key not in self.objects:
            return ObjectVerification(
                key=key, exists=False, verified=False,
                expected_sha256=expected_sha256, actual_sha256=None,
                expected_size=expected_size, actual_size=None,
            )
        data = self.objects[key]
        import hashlib
        actual_sha = hashlib.sha256(data).hexdigest()
        return ObjectVerification(
            key=key,
            exists=True,
            verified=(expected_sha256 is None or expected_sha256 == actual_sha)
                     and (expected_size is None or expected_size == len(data)),
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha,
            expected_size=expected_size,
            actual_size=len(data),
        )

    def health_check(self) -> bool:
        return True

    def list_objects(self, prefix: str = "", max_keys: int = 1000) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)][:max_keys]


# ═══════════════════════════════════════════════════════════════════════════════
# Check Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestOrphanedPages:
    def test_no_orphaned_pages(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch)
        issues = check_orphaned_pages(db)
        assert issues == []

    def test_orphaned_page_detected(self, db):
        orphan_id = str(uuid.uuid4())
        page_id = str(uuid.uuid4())
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(
            text(
                "INSERT INTO pages (id, chapter_id, page_number, object_key, "
                "integrity_status) VALUES (:id, :cid, :pn, :ok, :ist)"
            ),
            {"id": page_id, "cid": orphan_id, "pn": 1, "ok": "fake/key.jpg", "ist": "pending"},
        )
        db.flush()
        db.execute(text("PRAGMA foreign_keys=ON"))
        issues = check_orphaned_pages(db)
        assert len(issues) == 1
        assert issues[0].category == "orphaned_page"
        assert issues[0].repairable is False

    def test_multiple_orphaned_pages(self, db):
        orphan_id = str(uuid.uuid4())
        db.execute(text("PRAGMA foreign_keys=OFF"))
        for i in range(3):
            page_id = str(uuid.uuid4())
            db.execute(
                text(
                    "INSERT INTO pages (id, chapter_id, page_number, object_key, "
                    "integrity_status) VALUES (:id, :cid, :pn, :ok, :ist)"
                ),
                {"id": page_id, "cid": orphan_id, "pn": i, "ok": f"fake/{i}.jpg", "ist": "pending"},
            )
        db.flush()
        db.execute(text("PRAGMA foreign_keys=ON"))
        issues = check_orphaned_pages(db)
        assert len(issues) == 3
        db.flush()
        issues = check_orphaned_pages(db)
        assert len(issues) == 3


class TestOrphanedChapters:
    def test_no_orphaned_chapters(self, db):
        s = _create_series(db)
        _create_chapter(db, s)
        issues = check_orphaned_chapters(db)
        assert issues == []

    def test_orphaned_chapter_detected(self, db):
        orphan_id = str(uuid.uuid4())
        ch_id = str(uuid.uuid4())
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(
            text(
            "INSERT INTO chapters (id, series_id, number, import_status, page_count, language) "
            "VALUES (:id, :sid, :num, :ist, :pc, :lang)"
        ),
        {"id": ch_id, "sid": orphan_id, "num": 1.0, "ist": "ready", "pc": 0, "lang": "en"},
        )
        db.flush()
        db.execute(text("PRAGMA foreign_keys=ON"))
        issues = check_orphaned_chapters(db)
        assert len(issues) == 1
        assert issues[0].category == "orphaned_chapter"
        assert issues[0].repairable is False


class TestDuplicatePagePositions:
    def test_no_duplicates(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, page_number=1)
        _create_page(db, ch, page_number=2)
        issues = check_duplicate_page_positions(db)
        assert issues == []

    def test_duplicate_detected(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, page_number=1)

        result = db.execute(
            select(
                Page.chapter_id,
                Page.page_number,
                func.count(Page.id).label("cnt"),
            )
            .group_by(Page.chapter_id, Page.page_number)
            .having(func.count(Page.id) > 1)
        ).all()
        assert result == []

    def test_check_runs_on_realistic_data(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, page_number=1)
        _create_page(db, ch, page_number=2)
        issues = check_duplicate_page_positions(db)
        assert issues == []

    def test_duplicates_in_different_chapters(self, db):
        s = _create_series(db)
        ch1 = _create_chapter(db, s, number=1)
        ch2 = _create_chapter(db, s, number=2)
        _create_page(db, ch1, page_number=1)
        _create_page(db, ch2, page_number=1)
        issues = check_duplicate_page_positions(db)
        assert issues == []


class TestPageCountMismatch:
    def test_no_mismatch(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        ch.page_count = 2
        _create_page(db, ch, page_number=1)
        _create_page(db, ch, page_number=2)
        db.flush()
        issues = check_page_count_mismatch(db)
        assert issues == []

    def test_mismatch_detected(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        ch.page_count = 5
        _create_page(db, ch, page_number=1)
        db.flush()
        issues = check_page_count_mismatch(db)
        assert len(issues) == 1
        assert issues[0].category == "page_count_mismatch"
        assert issues[0].repairable is True
        assert issues[0].details["expected"] == 5
        assert issues[0].details["actual"] == 1

    def test_chapter_with_zero_pages(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        ch.page_count = 3
        db.flush()
        issues = check_page_count_mismatch(db)
        assert len(issues) == 1
        assert issues[0].details["actual"] == 0


class TestMissingStorageObjects:
    def test_no_missing(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch)
        storage.objects[p.object_key] = b"\xff" * 100
        issues = check_missing_storage_objects(db, storage)
        assert issues == []

    def test_missing_detected(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch)
        issues = check_missing_storage_objects(db, storage)
        assert len(issues) == 1
        assert issues[0].category == "missing_storage_object"
        assert issues[0].severity == "critical"
        assert issues[0].repairable is True

    def test_no_storage_returns_empty(self, db):
        issues = check_missing_storage_objects(db, None)
        assert issues == []

    def test_pages_with_no_key_skipped(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch)
        pages = db.scalars(select(Page).where(Page.chapter_id == ch.id)).all()
        assert pages[0].object_key is not None


class TestStorageSizeMismatch:
    def test_no_mismatch(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch, file_size=100)
        storage.objects[p.object_key] = b"\x00" * 100
        issues = check_storage_size_mismatch(db, storage)
        assert issues == []

    def test_mismatch_detected(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch, file_size=100)
        storage.objects[p.object_key] = b"\x00" * 200
        issues = check_storage_size_mismatch(db, storage)
        assert len(issues) == 1
        assert issues[0].category == "storage_size_mismatch"
        assert issues[0].repairable is False

    def test_no_storage_returns_empty(self, db):
        issues = check_storage_size_mismatch(db, None)
        assert issues == []


class TestSHA256Mismatch:
    def test_no_mismatch(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch)
        import hashlib
        data = b"test content"
        storage.objects[p.object_key] = data
        actual_hash = hashlib.sha256(data).hexdigest()
        p.sha256 = actual_hash
        db.flush()
        issues = check_sha256_mismatch(db, storage)
        assert issues == []

    def test_mismatch_detected(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch, sha256="b" * 64)
        storage.objects[p.object_key] = b"wrong content"
        issues = check_sha256_mismatch(db, storage)
        assert len(issues) == 1
        assert issues[0].category == "sha256_mismatch"
        assert issues[0].severity == "critical"
        assert issues[0].repairable is False


class TestInvalidImageDimensions:
    def test_valid_dimensions(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, width=800, height=1200)
        issues = check_invalid_image_dimensions(db)
        assert issues == []

    def test_zero_width(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, width=0, height=1200)
        issues = check_invalid_image_dimensions(db)
        assert len(issues) == 1
        assert issues[0].category == "invalid_image_dimensions"
        assert issues[0].repairable is False

    def test_negative_height(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, width=800, height=-1)
        issues = check_invalid_image_dimensions(db)
        assert len(issues) == 1

    def test_none_dimensions_ok(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch, width=None, height=None)
        issues = check_invalid_image_dimensions(db)
        assert issues == []


class TestImportingChaptersExposed:
    def test_no_exposure(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s, status=ChapterImportStatus.ready)
        _create_page(db, ch, integrity_status=PageIntegrityStatus.verified)
        issues = check_importing_chapters_exposed(db)
        assert issues == []

    def test_exposure_detected(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s, status=ChapterImportStatus.importing)
        _create_page(db, ch, integrity_status=PageIntegrityStatus.verified)
        issues = check_importing_chapters_exposed(db)
        assert len(issues) == 1
        assert issues[0].category == "importing_chapter_exposed"
        assert issues[0].severity == "critical"
        assert issues[0].repairable is True

    def test_importing_with_pending_pages_no_issue(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s, status=ChapterImportStatus.importing)
        _create_page(db, ch, integrity_status=PageIntegrityStatus.pending)
        issues = check_importing_chapters_exposed(db)
        assert issues == []


class TestAbandonedImportJobs:
    def test_no_abandoned(self, db):
        _create_import_job(db, status=ImportJobStatus.succeeded)
        issues = check_abandoned_import_jobs(db)
        assert issues == []

    def test_abandoned_job_detected(self, db):
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        _create_import_job(
            db,
            status=ImportJobStatus.uploading,
            started_at=old_time,
        )
        issues = check_abandoned_import_jobs(db)
        assert len(issues) == 1
        assert issues[0].category == "abandoned_import_job"
        assert issues[0].repairable is True
        assert issues[0].details["age_hours"] > 24

    def test_recent_job_not_abandoned(self, db):
        recent_time = datetime.now(timezone.utc) - timedelta(hours=1)
        _create_import_job(
            db,
            status=ImportJobStatus.uploading,
            started_at=recent_time,
        )
        issues = check_abandoned_import_jobs(db)
        assert issues == []


class TestStaleReplacedObjects:
    def test_no_stale(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch, storage_etag="abc123")
        storage.objects[p.object_key] = b"\x00" * 100
        storage.etags[p.object_key] = "abc123"
        issues = check_stale_replaced_objects(db, storage)
        assert issues == []

    def test_stale_detected(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch, storage_etag="old_etag")
        storage.objects[p.object_key] = b"\x00" * 100
        storage.etags[p.object_key] = "new_etag"
        issues = check_stale_replaced_objects(db, storage)
        assert len(issues) == 1
        assert issues[0].category == "stale_replaced_object"
        assert issues[0].repairable is False


# ═══════════════════════════════════════════════════════════════════════════════
# Aggregate Check & Repair Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunChecks:
    def test_clean_database(self, db):
        import hashlib
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        ch.page_count = 1
        data = b"\x00" * 100
        actual_hash = hashlib.sha256(data).hexdigest()
        p = _create_page(db, ch, file_size=100, sha256=actual_hash)
        storage.objects[p.object_key] = data
        summary = run_checks(db, storage)
        assert summary.total_issues == 0
        assert summary.criticals == 0
        assert summary.errors == 0

    def test_multiple_issues(self, db):
        storage = FakeStorage()
        orphan_id = str(uuid.uuid4())
        page_id = str(uuid.uuid4())
        db.execute(text("PRAGMA foreign_keys=OFF"))
        db.execute(
            text(
                "INSERT INTO pages (id, chapter_id, page_number, object_key, "
                "integrity_status) VALUES (:id, :cid, :pn, :ok, :ist)"
            ),
            {"id": page_id, "cid": orphan_id, "pn": 1, "ok": "missing.jpg", "ist": "pending"},
        )
        db.flush()
        db.execute(text("PRAGMA foreign_keys=ON"))
        summary = run_checks(db, storage)
        assert summary.total_issues >= 1
        assert isinstance(summary, IntegritySummary)

    def test_summary_to_dict(self, db):
        summary = run_checks(db, None)
        d = summary.to_dict()
        assert "total_issues" in d
        assert "issues" in d
        assert isinstance(d["issues"], list)


class TestRepairSafe:
    def test_repair_page_count_mismatch(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        ch.page_count = 10
        _create_page(db, ch, page_number=1)
        _create_page(db, ch, page_number=2)
        db.flush()
        summary = repair_safe(db, None)
        ch_refresh = db.get(Chapter, ch.id)
        assert ch_refresh.page_count == 2
        assert isinstance(summary, IntegritySummary)

    def test_repair_marks_missing_objects(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        _create_page(db, ch)
        db.flush()
        repair_safe(db, storage)
        pages = db.scalars(select(Page).where(Page.chapter_id == ch.id)).all()
        assert all(p.integrity_status == PageIntegrityStatus.missing for p in pages)

    def test_repair_marks_abandoned_jobs(self, db):
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        job = _create_import_job(
            db,
            status=ImportJobStatus.uploading,
            started_at=old_time,
        )
        db.flush()
        repair_safe(db, None)
        job_refresh = db.get(ImportJob, job.id)
        assert job_refresh.status == ImportJobStatus.failed
        assert job_refresh.finished_at is not None

    def test_repair_quarantines_exposed_chapters(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s, status=ChapterImportStatus.importing)
        _create_page(db, ch, integrity_status=PageIntegrityStatus.verified)
        db.flush()
        repair_safe(db, None)
        ch_refresh = db.get(Chapter, ch.id)
        assert ch_refresh.import_status == ChapterImportStatus.quarantined

    def test_repair_does_not_delete_objects(self, db):
        storage = FakeStorage()
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch)
        storage.objects[p.object_key] = b"\x00" * 100
        db.flush()
        repair_safe(db, storage)
        assert p.object_key in storage.objects

    def test_repair_does_not_delete_metadata(self, db):
        s = _create_series(db)
        ch = _create_chapter(db, s)
        p = _create_page(db, ch, sha256="a" * 64)
        db.flush()
        repair_safe(db, None)
        p_refresh = db.get(Page, p.id)
        assert p_refresh.sha256 == "a" * 64

    def test_repair_returns_summary(self, db):
        summary = repair_safe(db, None)
        assert isinstance(summary, IntegritySummary)
        assert summary.checked_at is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Admin API Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestListImportJobs:
    def test_empty_list(self, db):
        result = list_import_jobs(db)
        assert result["total"] == 0
        assert result["jobs"] == []

    def test_returns_jobs(self, db):
        job = _create_import_job(db)
        result = list_import_jobs(db)
        assert result["total"] == 1
        assert result["jobs"][0]["id"] == str(job.id)
        assert result["jobs"][0]["status"] == "pending"

    def test_pagination(self, db):
        for _ in range(5):
            _create_import_job(db)
        result = list_import_jobs(db, limit=2, offset=0)
        assert result["total"] == 5
        assert len(result["jobs"]) == 2
        result2 = list_import_jobs(db, limit=2, offset=2)
        assert len(result2["jobs"]) == 2


class TestGetImportJob:
    def test_not_found(self, db):
        result = get_import_job(db, uuid.uuid4())
        assert result is None

    def test_returns_job(self, db):
        job = _create_import_job(db)
        result = get_import_job(db, job.id)
        assert result is not None
        assert result["id"] == str(job.id)
        assert result["items"] == []

    def test_includes_items(self, db):
        job = _create_import_job(db)
        item = ImportJobItem(
            job_id=job.id,
            source_reference="test-source",
            object_key="test/key.jpg",
            status=ImportJobItemStatus.succeeded,
        )
        db.add(item)
        db.flush()
        result = get_import_job(db, job.id)
        assert len(result["items"]) == 1
        assert result["items"][0]["source_reference"] == "test-source"


# ═══════════════════════════════════════════════════════════════════════════════
# Admin Endpoint Tests (integration)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminEndpoints:
    def test_import_jobs_requires_admin(self, client, auth_client):
        auth_client()
        resp = client.get("/admin/import-jobs")
        assert resp.status_code == 403

    def test_import_job_detail_requires_admin(self, client, auth_client):
        auth_client()
        resp = client.get(f"/admin/import-jobs/{uuid.uuid4()}")
        assert resp.status_code == 403

    def test_integrity_summary_requires_admin(self, client, auth_client):
        auth_client()
        resp = client.get("/admin/integrity/summary")
        assert resp.status_code == 403

    def test_import_jobs_as_admin(self, client, auth_client, user_factory, db):
        user_factory(username="admin_user", password="adminpass123", role="admin")
        resp = _do_login(client, "admin_user", "adminpass123")
        assert resp.status_code == 200
        resp = client.get("/admin/import-jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "jobs" in data

    def test_import_job_detail_as_admin(self, client, user_factory, db):
        user_factory(username="admin_user", password="adminpass123", role="admin")
        _do_login(client, "admin_user", "adminpass123")
        job = _create_import_job(db)
        resp = client.get(f"/admin/import-jobs/{job.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(job.id)

    def test_import_job_detail_not_found(self, client, user_factory, db):
        user_factory(username="admin_user", password="adminpass123", role="admin")
        _do_login(client, "admin_user", "adminpass123")
        resp = client.get(f"/admin/import-jobs/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_import_job_detail_invalid_id(self, client, user_factory, db):
        user_factory(username="admin_user", password="adminpass123", role="admin")
        _do_login(client, "admin_user", "adminpass123")
        resp = client.get("/admin/import-jobs/not-a-uuid")
        assert resp.status_code == 400

    def test_integrity_summary_as_admin(self, client, user_factory, db):
        user_factory(username="admin_user", password="adminpass123", role="admin")
        _do_login(client, "admin_user", "adminpass123")
        resp = client.get("/admin/integrity/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_issues" in data
        assert "issues" in data

    def test_unauthenticated_gets_401(self, client):
        resp = client.get("/admin/import-jobs")
        assert resp.status_code == 401


# ─── Import from select for repair tests ─────────────────────────────────────, text
