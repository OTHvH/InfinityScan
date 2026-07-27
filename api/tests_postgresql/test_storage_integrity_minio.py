"""PostgreSQL + MinIO integration coverage for Phase 5 storage integrity."""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

from models import (  # noqa: E402
    Chapter,
    ChapterImportStatus,
    ContentType,
    Page,
    PageIntegrityStatus,
    Series,
)
from storage import (  # noqa: E402
    ObjectMetadata,
    ObjectPage,
    ObjectVerification,
    S3CompatibleStorage,
    S3StorageConfig,
    StorageError,
)
from storage.keys import cover_object_key, page_object_key  # noqa: E402
from tools.storage_integrity import run_storage_integrity  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")
RUN_MINIO = os.environ.get("RUN_MINIO_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not RUN_MINIO,
    reason="requires disposable PostgreSQL and RUN_MINIO_TESTS=1",
)


class TrackingStorage:
    def __init__(self, delegate: S3CompatibleStorage, delay: float = 0.0) -> None:
        self.delegate = delegate
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.verify_calls = 0
        self.list_calls = 0
        self.delete_calls = 0
        self._lock = threading.Lock()

    def head_object(self, key: str) -> ObjectMetadata | None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.delegate.head_object(key)
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
        return self.delegate.verify_object(key, expected_sha256, expected_size, metadata)

    def list_objects_page(
        self,
        *,
        prefix: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
    ) -> ObjectPage:
        self.list_calls += 1
        return self.delegate.list_objects_page(
            prefix=prefix,
            continuation_token=continuation_token,
            max_keys=max_keys,
        )

    def delete_object(self, key: str) -> bool:
        self.delete_calls += 1
        return self.delegate.delete_object(key)

    def object_exists(self, key: str) -> bool:
        return self.delegate.object_exists(key)

    def upload_file(self, key, file_path, mime_type):
        return self.delegate.upload_file(key, file_path, mime_type)

    def upload_bytes(self, key, data, mime_type):
        return self.delegate.upload_bytes(key, data, mime_type)

    def generate_presigned_get(self, key, expires_in=None):
        return self.delegate.generate_presigned_get(key, expires_in)

    def health_check(self) -> bool:
        return self.delegate.health_check()


@pytest.fixture()
def engine():
    result = create_engine(DATABASE_URL, pool_pre_ping=True)
    if result.dialect.name != "postgresql":
        pytest.fail("storage integrity integration requires PostgreSQL")
    with result.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")
    yield result
    result.dispose()


@pytest.fixture()
def minio_storage() -> S3CompatibleStorage:
    storage = S3CompatibleStorage(
        S3StorageConfig(
            endpoint_url=os.environ.get("MINIO_TEST_ENDPOINT_URL", "http://127.0.0.1:9000"),
            region=os.environ.get("S3_REGION", "auto"),
            bucket=os.environ.get("S3_BUCKET", "infinityscan-pages"),
            access_key_id=os.environ.get("S3_ACCESS_KEY_ID", "minioadmin"),
            secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY", "minioadminsecret"),
            force_path_style=True,
        )
    )
    if not storage.health_check():
        pytest.fail("MinIO bucket is unavailable")
    return storage


def test_quick_full_and_repair_against_postgresql_and_minio(engine, minio_storage):
    namespace = uuid.uuid4()
    uploaded_keys: list[str] = []
    now = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as db:
        series = Series(
            id=namespace,
            slug=f"phase5-minio-{namespace.hex}",
            title="Phase 5 MinIO",
            content_type=ContentType.manga,
        )
        chapter = Chapter(
            id=uuid.uuid4(),
            series=series,
            number=Decimal("1"),
            page_count=5,
            import_status=ChapterImportStatus.ready,
            verified_at=now,
        )
        cases = (
            (b"valid", None, None),
            (b"missing", None, None),
            (b"size", "size", None),
            (b"sha", "sha", None),
            (b"mime", "mime", None),
        )
        pages: list[Page] = []
        for number, (payload, mismatch, _) in enumerate(cases, start=1):
            actual_sha = hashlib.sha256(payload).hexdigest()
            expected_sha = "f" * 64 if mismatch == "sha" else actual_sha
            expected_mime = "image/png" if mismatch == "mime" else "image/jpeg"
            key = page_object_key(series.id, chapter.id, number, expected_sha, "jpg")
            page = Page(
                chapter=chapter,
                page_number=number,
                object_key=key,
                width=100,
                height=200,
                file_size=len(payload) + 1 if mismatch == "size" else len(payload),
                sha256=expected_sha,
                mime_type=expected_mime,
                file_extension="jpg",
                integrity_status=PageIntegrityStatus.verified,
                verified_at=now,
            )
            pages.append(page)
            if mismatch != "missing" and number != 2:
                minio_storage.upload_bytes(key, payload, "image/jpeg")
                uploaded_keys.append(key)
        orphan_payload = b"orphan"
        orphan_key = cover_object_key(
            uuid.uuid4(),
            hashlib.sha256(orphan_payload).hexdigest(),
            "jpg",
        )
        minio_storage.upload_bytes(orphan_key, orphan_payload, "image/jpeg")
        uploaded_keys.append(orphan_key)
        db.add(series)
        db.commit()

        tracking = TrackingStorage(minio_storage, delay=0.01)
        try:
            quick = run_storage_integrity(
                db,
                tracking,
                mode="quick",
                max_concurrency=2,
            )
            assert quick.objects_checked == 5
            assert quick.objects_verified == 1
            assert quick.missing == 1
            assert quick.metadata_mismatch == 3
            assert tracking.verify_calls == 0
            assert tracking.list_calls == 0
            assert 1 < tracking.max_active <= 2
            assert tracking.delete_calls == 0
            assert all(page.integrity_status == PageIntegrityStatus.verified for page in pages)

            full = run_storage_integrity(
                db,
                tracking,
                mode="full",
                content_sha256=True,
                max_concurrency=2,
                inventory_page_size=2,
            )
            assert full.hash_mismatch == 2
            assert full.orphaned == 1
            assert full.bytes_hashed > 0
            assert tracking.list_calls >= 3
            assert tracking.delete_calls == 0

            repaired = run_storage_integrity(
                db,
                tracking,
                mode="quick",
                max_concurrency=2,
                repair=True,
            )
            assert repaired.repairs_applied == 4
            db.expire_all()
            assert pages[0].integrity_status == PageIntegrityStatus.verified
            assert pages[1].integrity_status == PageIntegrityStatus.missing
            assert all(page.integrity_status == PageIntegrityStatus.mismatch for page in pages[2:])
            assert tracking.delete_calls == 0
        finally:
            for key in uploaded_keys:
                minio_storage.delete_object(key)


def test_minio_retryable_timeout_and_permanent_error_classification(minio_storage, monkeypatch):
    key = f"series/{uuid.uuid4()}/fault-test"
    minio_storage.upload_bytes(key, b"fault-test", "application/octet-stream")
    original_head = minio_storage._client.head_object
    attempts = 0

    def timeout_once(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ReadTimeoutError(endpoint_url="minio")
        return original_head(**kwargs)

    try:
        monkeypatch.setattr(minio_storage._client, "head_object", timeout_once)
        assert minio_storage.head_object(key) is not None
        assert attempts == 2

        permanent_attempts = 0

        def forbidden(**kwargs):
            nonlocal permanent_attempts
            permanent_attempts += 1
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "HeadObject",
            )

        monkeypatch.setattr(minio_storage._client, "head_object", forbidden)
        with pytest.raises(StorageError, match="S3 head_object failed"):
            minio_storage.head_object(key)
        assert permanent_attempts == 1
    finally:
        monkeypatch.setattr(minio_storage._client, "head_object", original_head)
        minio_storage.delete_object(key)
