"""Opt-in integration tests for the local MinIO Compose profile."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from storage import S3CompatibleStorage, S3StorageConfig


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MINIO_TESTS") != "1",
    reason="set RUN_MINIO_TESTS=1 after starting MinIO and minio-setup",
)


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
        pytest.fail("MinIO bucket is not reachable; run the explicit minio-setup service")
    return storage


def test_minio_round_trip_and_presigned_get(minio_storage: S3CompatibleStorage):
    key = f"integration/{uuid.uuid4().hex}.jpg"
    payload = b"minio integration image"
    try:
        uploaded = minio_storage.upload_bytes(key, payload, "image/jpeg")
        assert minio_storage.object_exists(key)
        assert uploaded.byte_size == len(payload)
        assert minio_storage.verify_object(key, uploaded.sha256, len(payload)).verified

        url = minio_storage.generate_presigned_get(key, expires_in=60)
        response = httpx.get(url, timeout=10)
        assert response.status_code == 200
        assert response.content == payload
    finally:
        minio_storage.delete_object(key)
    assert not minio_storage.object_exists(key)
