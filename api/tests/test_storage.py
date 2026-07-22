"""Unit tests for the typed object-storage contract and S3 backend."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from storage import ObjectStorage, S3CompatibleStorage, S3StorageConfig, StorageError


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "operation",
    )


class FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.objects: dict[str, tuple[bytes, dict[str, Any]]] = {}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("put_object", kwargs))
        body = kwargs["Body"]
        data = body if isinstance(body, bytes) else body.read()
        self.objects[kwargs["Key"]] = (data, kwargs)
        return {"ETag": '"fake-etag"'}

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, Any]) -> None:
        self.calls.append(("upload_file", {"Filename": filename, "Bucket": bucket, "Key": key, "ExtraArgs": ExtraArgs}))
        data = Path(filename).read_bytes()
        self.objects[key] = (data, {"Metadata": ExtraArgs["Metadata"], "ContentType": ExtraArgs["ContentType"]})

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.calls.append(("head_object", {"Bucket": Bucket, "Key": Key}))
        if Key not in self.objects:
            raise _client_error("404", 404)
        data, args = self.objects[Key]
        return {
            "ContentLength": len(data),
            "ContentType": args.get("ContentType"),
            "Metadata": args.get("Metadata", {}),
            "ETag": '"fake-etag"',
        }

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.calls.append(("get_object", {"Bucket": Bucket, "Key": Key}))
        data, _ = self.objects[Key]
        return {"Body": io.BytesIO(data)}

    def delete_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.calls.append(("delete_object", {"Bucket": Bucket, "Key": Key}))
        self.objects.pop(Key, None)
        return {}

    def generate_presigned_url(self, operation: str, Params: dict[str, str], ExpiresIn: int) -> str:
        self.calls.append(("generate_presigned_url", {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn}))
        return "https://signed.example.test/object"

    def head_bucket(self, Bucket: str) -> dict[str, Any]:
        self.calls.append(("head_bucket", {"Bucket": Bucket}))
        return {}


def _config() -> S3StorageConfig:
    return S3StorageConfig(
        endpoint_url="http://minio:9000",
        region="auto",
        bucket="test-bucket",
        access_key_id="access",
        secret_access_key="secret",
    )


class FakeStorage:
    """Small in-memory implementation used to exercise the typed contract."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def head_object(self, key: str):
        data = self.objects.get(key)
        return None if data is None else {"key": key, "byte_size": len(data)}

    def upload_file(self, key: str, file_path: str | Path, mime_type: str):
        data = Path(file_path).read_bytes()
        self.objects[key] = data
        return {"key": key, "byte_size": len(data), "mime_type": mime_type}

    def upload_bytes(self, key: str, data: bytes, mime_type: str):
        self.objects[key] = data
        return {"key": key, "byte_size": len(data), "mime_type": mime_type}

    def delete_object(self, key: str) -> bool:
        self.objects.pop(key, None)
        return True

    def object_exists(self, key: str) -> bool:
        return key in self.objects

    def generate_presigned_get(self, key: str, expires_in: int | None = None) -> str:
        return f"fake://{key}?expires={expires_in}"

    def verify_object(self, key: str, expected_sha256: str | None = None, expected_size: int | None = None):
        data = self.objects.get(key)
        actual = hashlib.sha256(data).hexdigest() if data is not None else None
        return {"key": key, "exists": data is not None, "verified": actual == expected_sha256}

    def health_check(self) -> bool:
        return True


def test_fake_storage_implements_typed_interface(tmp_path: Path):
    storage = FakeStorage()
    assert isinstance(storage, ObjectStorage)
    source = tmp_path / "page.jpg"
    source.write_bytes(b"page")
    storage.upload_file("pages/1.jpg", source, "image/jpeg")
    assert storage.object_exists("pages/1.jpg")
    storage.delete_object("pages/1.jpg")
    assert not storage.object_exists("pages/1.jpg")


def test_upload_bytes_sends_private_cacheable_metadata():
    client = FakeS3Client()
    storage = S3CompatibleStorage(_config(), client=client)
    result = storage.upload_bytes("pages/test.jpg", b"image", "image/jpeg")

    operation, kwargs = client.calls[0]
    assert operation == "put_object"
    assert kwargs["CacheControl"] == "public, max-age=31536000, immutable"
    assert "ACL" not in kwargs
    assert kwargs["Metadata"] == {
        "sha256": hashlib.sha256(b"image").hexdigest(),
        "byte_size": "5",
        "mime_type": "image/jpeg",
    }
    assert result.etag == "fake-etag"


def test_upload_file_hashes_file_and_sends_metadata(tmp_path: Path):
    client = FakeS3Client()
    storage = S3CompatibleStorage(_config(), client=client)
    source = tmp_path / "page.jpg"
    source.write_bytes(b"file image")

    result = storage.upload_file("pages/file.jpg", source, "image/jpeg")
    assert result.sha256 == hashlib.sha256(b"file image").hexdigest()
    assert result.byte_size == len(b"file image")
    assert client.objects["pages/file.jpg"][1]["Metadata"]["byte_size"] == str(len(b"file image"))


def test_verify_object_hashes_downloaded_bytes():
    client = FakeS3Client()
    storage = S3CompatibleStorage(_config(), client=client)
    payload = b"verified image"
    storage.upload_bytes("pages/verified.jpg", payload, "image/jpeg")

    result = storage.verify_object("pages/verified.jpg")
    assert result.verified is True
    assert result.actual_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.actual_size == len(payload)


def test_missing_objects_are_not_errors_for_exists_and_delete():
    client = FakeS3Client()
    storage = S3CompatibleStorage(_config(), client=client)
    assert storage.object_exists("pages/missing.jpg") is False
    assert storage.delete_object("pages/missing.jpg") is True


def test_transient_errors_use_bounded_exponential_backoff():
    client = FakeS3Client()
    attempts = 0
    sleeps: list[float] = []

    def flaky_put(**kwargs: Any) -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise EndpointConnectionError(endpoint_url="http://minio:9000")
        return {"ETag": '"ok"'}

    client.put_object = flaky_put  # type: ignore[method-assign]
    storage = S3CompatibleStorage(
        _config(), client=client, sleep=sleeps.append, random_value=lambda: 0.0
    )
    storage.upload_bytes("pages/retry.jpg", b"retry", "image/jpeg")
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


def test_permanent_4xx_is_not_retried():
    client = FakeS3Client()
    attempts = 0

    def forbidden_put(**kwargs: Any) -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        raise _client_error("AccessDenied", 403)

    client.put_object = forbidden_put  # type: ignore[method-assign]
    storage = S3CompatibleStorage(_config(), client=client, sleep=lambda _: pytest.fail("unexpected retry"))
    with pytest.raises(StorageError):
        storage.upload_bytes("pages/forbidden.jpg", b"nope", "image/jpeg")
    assert attempts == 1


def test_presigned_url_uses_short_configured_lifetime():
    client = FakeS3Client()
    storage = S3CompatibleStorage(_config(), client=client)
    assert storage.generate_presigned_get("pages/test.jpg", expires_in=120).startswith("https://")
    operation, kwargs = client.calls[-1]
    assert operation == "generate_presigned_url"
    assert kwargs["ExpiresIn"] == 120
