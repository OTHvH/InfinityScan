"""Quick/full object-storage integrity scanner tests."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from models import (
    Chapter,
    ChapterImportStatus,
    ContentType,
    ImportJob,
    ImportJobItem,
    ImportJobItemStatus,
    Page,
    PageIntegrityStatus,
    Series,
)
from storage import DownloadResult, ObjectMetadata, ObjectPage, ObjectVerification, StorageError
from storage.keys import cover_object_key, page_object_key
from tools.storage_integrity import delete_unreferenced_objects, run_storage_integrity


class InstrumentedStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, ObjectMetadata] = {}
        self.errors: set[str] = set()
        self.delay = 0.0
        self.active = 0
        self.max_active = 0
        self.head_calls = 0
        self.verify_calls = 0
        self.list_calls = 0
        self.delete_calls: list[str] = []
        self._lock = threading.Lock()

    def put(
        self,
        key: str,
        data: bytes,
        *,
        sha256: str | None = None,
        mime_type: str | None = "image/jpeg",
        byte_size: int | None = None,
        etag: str = "etag",
        last_modified: datetime | None = None,
    ) -> None:
        self.objects[key] = data
        self.metadata[key] = ObjectMetadata(
            key=key,
            byte_size=len(data) if byte_size is None else byte_size,
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
            etag=etag,
            last_modified=last_modified or datetime.now(timezone.utc) - timedelta(days=2),
        )

    def head_object(self, key: str) -> ObjectMetadata | None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.head_calls += 1
        try:
            if self.delay:
                time.sleep(self.delay)
            if key in self.errors:
                raise StorageError("unavailable")
            return self.metadata.get(key)
        finally:
            with self._lock:
                self.active -= 1

    def verify_object(
        self,
        key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        metadata: ObjectMetadata | None = None,
    ) -> ObjectVerification:
        self.verify_calls += 1
        data = self.objects[key]
        actual_sha256 = hashlib.sha256(data).hexdigest()
        actual_size = len(data)
        verified = actual_sha256 == expected_sha256 and actual_size == expected_size
        return ObjectVerification(
            key=key,
            exists=True,
            verified=verified,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
            expected_size=expected_size,
            actual_size=actual_size,
            reason=None if verified else "checksum_or_size_mismatch",
        )

    def list_objects_page(
        self,
        *,
        prefix: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
    ) -> ObjectPage:
        self.list_calls += 1
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        offset = int(continuation_token or 0)
        selected = keys[offset:offset + max_keys]
        next_offset = offset + len(selected)
        return ObjectPage(
            objects=tuple(self.metadata[key] for key in selected),
            next_token=str(next_offset) if next_offset < len(keys) else None,
        )

    def delete_object(self, key: str) -> bool:
        self.delete_calls.append(key)
        self.objects.pop(key, None)
        self.metadata.pop(key, None)
        return True

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def upload_file(self, key, file_path, mime_type):
        raise AssertionError("not used")

    def upload_bytes(self, key, data, mime_type):
        raise AssertionError("not used")

    def download_file(
        self,
        key,
        destination,
        *,
        expected_sha256=None,
        expected_size=None,
        max_bytes=1024 * 1024 * 1024,
    ):
        data = self.objects[key]
        assert len(data) <= max_bytes
        from pathlib import Path

        Path(destination).write_bytes(data)
        return DownloadResult(
            key=key,
            byte_size=len(data),
            mime_type=self.metadata[key].mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
            etag=self.metadata[key].etag,
        )

    def generate_presigned_get(self, key, expires_in=None):
        raise AssertionError("not used")

    def health_check(self) -> bool:
        return True


def _content_graph(db):
    series = Series(
        id=uuid.uuid4(),
        slug=f"storage-integrity-{uuid.uuid4().hex}",
        title="Storage Integrity",
        content_type=ContentType.manga,
    )
    chapter = Chapter(
        id=uuid.uuid4(),
        series=series,
        number=Decimal("1"),
        page_count=0,
        import_status=ChapterImportStatus.ready,
    )
    db.add(series)
    db.flush()
    return series, chapter


def _add_page(db, chapter: Chapter, data: bytes, page_number: int) -> Page:
    digest = hashlib.sha256(data).hexdigest()
    key = page_object_key(
        chapter.series_id,
        chapter.id,
        page_number,
        digest,
        "jpg",
    )
    page = Page(
        chapter=chapter,
        page_number=page_number,
        object_key=key,
        width=100,
        height=200,
        file_size=len(data),
        sha256=digest,
        mime_type="image/jpeg",
        file_extension="jpg",
        integrity_status=PageIntegrityStatus.verified,
        verified_at=datetime.now(timezone.utc),
        storage_etag="etag",
    )
    chapter.page_count += 1
    db.add(page)
    db.flush()
    return page


def test_quick_checks_metadata_without_downloading_or_listing(db):
    _, chapter = _content_graph(db)
    page = _add_page(db, chapter, b"valid", 1)
    storage = InstrumentedStorage()
    storage.put(page.object_key, b"valid")

    report = run_storage_integrity(db, storage, mode="quick", max_concurrency=2)

    assert report.objects_checked == 1
    assert report.objects_verified == 1
    assert storage.verify_calls == 0
    assert storage.list_calls == 0
    assert report.to_dict()["complete"] is True
    automated = json.loads(json.dumps(report.to_dict()))
    assert {
        "objects_checked",
        "objects_verified",
        "missing",
        "metadata_mismatch",
        "hash_mismatch",
        "orphaned",
        "invalid_keys",
        "errors",
        "elapsed_time",
    } <= automated.keys()


def test_quick_rejects_noncanonical_verified_page_key(db):
    _, chapter = _content_graph(db)
    page = _add_page(db, chapter, b"valid", 1)
    page.object_key = "series/not-canonical.jpg"
    storage = InstrumentedStorage()
    storage.put(page.object_key, b"valid")
    db.flush()

    report = run_storage_integrity(db, storage, mode="quick")

    assert report.invalid_keys == 1
    assert report.objects_verified == 0


def test_quick_classifies_missing_size_sha_mime_and_permanent_errors(db):
    _, chapter = _content_graph(db)
    pages = [_add_page(db, chapter, f"page-{index}".encode(), index) for index in range(1, 6)]
    storage = InstrumentedStorage()
    storage.put(pages[1].object_key, b"page-2", byte_size=999)
    storage.put(pages[2].object_key, b"page-3", sha256="f" * 64)
    storage.put(pages[3].object_key, b"page-4", mime_type="image/png")
    storage.put(pages[4].object_key, b"page-5")
    storage.errors.add(pages[4].object_key)

    report = run_storage_integrity(db, storage, mode="quick", max_concurrency=3)

    assert report.objects_checked == 5
    assert report.missing == 1
    assert report.metadata_mismatch == 3
    assert report.errors == 1
    assert report.complete is False


def test_full_streams_content_hash_and_detects_changed_bytes(db):
    _, chapter = _content_graph(db)
    page = _add_page(db, chapter, b"right", 1)
    storage = InstrumentedStorage()
    storage.put(page.object_key, b"wrong", sha256=page.sha256)

    report = run_storage_integrity(
        db,
        storage,
        mode="full",
        content_sha256=True,
        inventory_page_size=1,
    )

    assert storage.verify_calls == 1
    assert report.hash_mismatch == 1
    assert report.bytes_hashed == len(b"wrong")


def test_full_inventory_is_paginated_and_reports_orphans_and_invalid_keys(db):
    series, chapter = _content_graph(db)
    page = _add_page(db, chapter, b"referenced", 1)
    storage = InstrumentedStorage()
    storage.put(page.object_key, b"referenced")
    for index in range(3):
        payload = f"orphan-{index}".encode()
        storage.put(
            cover_object_key(uuid.uuid4(), hashlib.sha256(payload).hexdigest(), "jpg"),
            payload,
        )
    storage.put("series/not-a-valid-key", b"invalid")
    series.cover_object_key = None
    db.flush()

    report = run_storage_integrity(
        db,
        storage,
        mode="full",
        inventory_page_size=2,
    )

    assert storage.list_calls == 3
    assert report.orphaned == 4
    assert report.invalid_keys == 1


def test_full_detects_duplicate_references_and_stale_etag(db):
    _, chapter = _content_graph(db)
    first = _add_page(db, chapter, b"same", 1)
    second = _add_page(db, chapter, b"same", 2)
    second.object_key = first.object_key
    storage = InstrumentedStorage()
    storage.put(first.object_key, b"same", etag="replacement")
    db.flush()

    report = run_storage_integrity(db, storage, mode="full")

    assert report.duplicate_references == 1
    assert report.stale_objects == 2


def test_scanner_concurrency_and_issue_memory_are_bounded(db):
    _, chapter = _content_graph(db)
    storage = InstrumentedStorage()
    for index in range(1, 13):
        data = f"page-{index}".encode()
        page = _add_page(db, chapter, data, index)
        storage.put(page.object_key, data)
    storage.delay = 0.01

    report = run_storage_integrity(
        db,
        storage,
        mode="quick",
        max_concurrency=3,
        issue_limit=2,
    )

    assert 1 < storage.max_active <= 3
    assert report.objects_checked == 12
    assert len(report.issues) <= 2


def test_check_is_read_only_and_repair_safe_only_changes_conclusive_statuses(db):
    _, chapter = _content_graph(db)
    missing = _add_page(db, chapter, b"missing", 1)
    mismatch = _add_page(db, chapter, b"mismatch", 2)
    unavailable = _add_page(db, chapter, b"unavailable", 3)
    storage = InstrumentedStorage()
    storage.put(mismatch.object_key, b"mismatch", byte_size=999)
    storage.put(unavailable.object_key, b"unavailable")
    storage.errors.add(unavailable.object_key)

    run_storage_integrity(db, storage, mode="quick", repair=False)
    db.refresh(missing)
    db.refresh(mismatch)
    assert missing.integrity_status == PageIntegrityStatus.verified
    assert mismatch.integrity_status == PageIntegrityStatus.verified
    assert storage.delete_calls == []

    report = run_storage_integrity(db, storage, mode="quick", repair=True)
    db.refresh(missing)
    db.refresh(mismatch)
    db.refresh(unavailable)
    assert missing.integrity_status == PageIntegrityStatus.missing
    assert mismatch.integrity_status == PageIntegrityStatus.mismatch
    assert unavailable.integrity_status == PageIntegrityStatus.verified
    assert report.repairs_applied == 2
    assert storage.delete_calls == []


def test_orphan_deletion_requires_explicit_execute_and_rechecks_references(db):
    series, chapter = _content_graph(db)
    referenced = _add_page(db, chapter, b"referenced", 1)
    orphan_data = b"orphan"
    orphan_key = cover_object_key(uuid.uuid4(), hashlib.sha256(orphan_data).hexdigest(), "jpg")
    storage = InstrumentedStorage()
    storage.put(referenced.object_key, b"referenced")
    storage.put(orphan_key, orphan_data)
    cover_data = b"cover"
    series.cover_object_key = cover_object_key(
        series.id,
        hashlib.sha256(cover_data).hexdigest(),
        "jpg",
    )
    storage.put(series.cover_object_key, cover_data)
    item_data = b"job-item"
    item_key = cover_object_key(
        uuid.uuid4(),
        hashlib.sha256(item_data).hexdigest(),
        "jpg",
    )
    job = ImportJob(idempotency_key=uuid.uuid4().hex)
    job.items.append(
        ImportJobItem(
            source_reference="page.jpg",
            object_key=item_key,
            status=ImportJobItemStatus.pending,
        )
    )
    db.add(job)
    storage.put(item_key, item_data)
    db.flush()

    dry_run = delete_unreferenced_objects(
        db,
        storage,
        execute=False,
        inventory_page_size=1,
        minimum_age_seconds=0,
    )
    assert dry_run.candidates == 1
    assert dry_run.deleted == 0
    assert storage.delete_calls == []

    executed = delete_unreferenced_objects(
        db,
        storage,
        execute=True,
        inventory_page_size=1,
        minimum_age_seconds=0,
    )
    assert executed.deleted == 1
    assert storage.object_exists(referenced.object_key)
    assert storage.object_exists(series.cover_object_key)
    assert storage.object_exists(item_key)
    assert not storage.object_exists(orphan_key)
