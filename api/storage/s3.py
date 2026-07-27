"""S3-compatible private object storage implementation."""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from settings import Settings

from .base import (
    ObjectMetadata,
    ObjectPage,
    ObjectVerification,
    StorageConfigurationError,
    StorageError,
    UploadResult,
)


OBJECT_CACHE_CONTROL = "public, max-age=31536000, immutable"
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_RETRYABLE_STATUS_CODES = {408, 429}


@dataclass(frozen=True)
class S3StorageConfig:
    endpoint_url: str | None
    region: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    presign_ttl_seconds: int = 300
    force_path_style: bool = False
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    max_retries: int = 4

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3StorageConfig":
        if not settings.object_storage_enabled:
            raise StorageConfigurationError("Object storage is disabled")
        return cls(
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            bucket=settings.s3_bucket,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            presign_ttl_seconds=settings.s3_presign_ttl_seconds,
            force_path_style=settings.s3_force_path_style,
            connect_timeout=settings.s3_connect_timeout,
            read_timeout=settings.s3_read_timeout,
            max_retries=settings.s3_max_retries,
        )

    def validate(self) -> None:
        required = {
            "region": self.region,
            "bucket": self.bucket,
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise StorageConfigurationError(
                "Object storage configuration is incomplete: " + ", ".join(missing)
            )
        if self.presign_ttl_seconds < 1 or self.presign_ttl_seconds > 3600:
            raise StorageConfigurationError("Presigned URL lifetime must be between 1 and 3600 seconds")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise StorageConfigurationError("S3 timeouts must be positive")
        if self.max_retries < 0 or self.max_retries > 10:
            raise StorageConfigurationError("S3 max retries must be between 0 and 10")


class S3CompatibleStorage:
    """Private S3-compatible storage with bounded, selective retries."""

    def __init__(
        self,
        config: S3StorageConfig,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        max_retries: int | None = None,
        retry_base_delay: float = 0.25,
        retry_max_delay: float = 4.0,
    ) -> None:
        config.validate()
        configured_retries = config.max_retries if max_retries is None else max_retries
        if configured_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.config = config
        self.bucket = config.bucket
        self._sleep = sleep
        self._random = random_value
        self._max_retries = configured_retries
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        if client is None:
            addressing_style = "path" if config.force_path_style else "auto"
            client_config = Config(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
                connect_timeout=config.connect_timeout,
                read_timeout=config.read_timeout,
                retries={"max_attempts": 0, "mode": "standard"},
            )
            region = config.region or None
            self._client = boto3.client(
                "s3",
                endpoint_url=config.endpoint_url or None,
                region_name=region,
                aws_access_key_id=config.access_key_id,
                aws_secret_access_key=config.secret_access_key,
                config=client_config,
            )
        else:
            self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3CompatibleStorage":
        return cls(S3StorageConfig.from_settings(settings))

    @staticmethod
    def _validate_key(key: str) -> None:
        if not key or key.startswith("/") or "\x00" in key:
            raise ValueError("Object key must be a non-empty relative key")

    @staticmethod
    def _validate_mime_type(mime_type: str) -> None:
        if not mime_type or "\r" in mime_type or "\n" in mime_type:
            raise ValueError("MIME type must be a non-empty header-safe value")

    @staticmethod
    def _error_status(exc: ClientError) -> int | None:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return int(status) if status is not None else None

    @classmethod
    def _is_missing(cls, exc: ClientError) -> bool:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "NoSuchBucket":
            return False
        return code in _MISSING_CODES or cls._error_status(exc) == 404

    @classmethod
    def _is_retryable(cls, exc: Exception) -> bool:
        if isinstance(exc, ClientError):
            status = cls._error_status(exc)
            return status in _RETRYABLE_STATUS_CODES or (status is not None and status >= 500)
        return isinstance(
            exc,
            (
                EndpointConnectionError,
                ConnectionClosedError,
                ConnectTimeoutError,
                ReadTimeoutError,
            ),
        )

    def _call(self, operation: str, key: str | None, fn: Callable[[], Any]) -> Any:
        for attempt in range(self._max_retries + 1):
            try:
                return fn()
            except (BotoCoreError, ClientError) as exc:
                if isinstance(exc, ClientError) and self._is_missing(exc):
                    raise
                if not self._is_retryable(exc) or attempt == self._max_retries:
                    # Do not include endpoint URLs, credentials, or signed URLs
                    # in storage errors.
                    object_suffix = f" for object {key}" if key else ""
                    raise StorageError(f"S3 {operation} failed{object_suffix}") from exc
                exponential = min(
                    self._retry_max_delay,
                    self._retry_base_delay * (2**attempt),
                )
                self._sleep(min(self._retry_max_delay, exponential + self._random() * self._retry_base_delay))

    @staticmethod
    def _metadata_from_response(key: str, response: dict[str, Any]) -> ObjectMetadata:
        metadata = {str(k).lower(): str(v) for k, v in response.get("Metadata", {}).items()}
        etag = response.get("ETag")
        return ObjectMetadata(
            key=key,
            byte_size=int(response.get("ContentLength", metadata.get("byte_size", 0))),
            mime_type=response.get("ContentType") or metadata.get("mime_type"),
            sha256=metadata.get("sha256"),
            etag=str(etag).strip('"') if etag else None,
            last_modified=response.get("LastModified"),
        )

    @staticmethod
    def _hash_file(file_path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        byte_size = 0
        with file_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_size += len(chunk)
        return digest.hexdigest(), byte_size

    @staticmethod
    def _hash_bytes(data: bytes) -> tuple[str, int]:
        return hashlib.sha256(data).hexdigest(), len(data)

    @staticmethod
    def _extra_args(sha256: str, byte_size: int, mime_type: str) -> dict[str, Any]:
        return {
            "ContentType": mime_type,
            "CacheControl": OBJECT_CACHE_CONTROL,
            "Metadata": {
                "sha256": sha256,
                "byte_size": str(byte_size),
                "mime_type": mime_type,
            },
        }

    def head_object(self, key: str) -> ObjectMetadata | None:
        self._validate_key(key)
        try:
            response = self._call(
                "head_object",
                key,
                lambda: self._client.head_object(Bucket=self.bucket, Key=key),
            )
        except ClientError as exc:
            if self._is_missing(exc):
                return None
            raise
        return self._metadata_from_response(key, response)

    def object_exists(self, key: str) -> bool:
        return self.head_object(key) is not None

    def upload_file(self, key: str, file_path: str | Path, mime_type: str) -> UploadResult:
        self._validate_key(key)
        self._validate_mime_type(mime_type)
        path = Path(file_path)
        sha256, byte_size = self._hash_file(path)
        extra_args = self._extra_args(sha256, byte_size, mime_type)
        self._call(
            "upload_file",
            key,
            lambda: self._client.upload_file(
                str(path), self.bucket, key, ExtraArgs=extra_args
            ),
        )
        metadata = self.head_object(key)
        return UploadResult(
            key=key,
            byte_size=byte_size,
            mime_type=mime_type,
            sha256=sha256,
            etag=metadata.etag if metadata else None,
        )

    def upload_bytes(self, key: str, data: bytes, mime_type: str) -> UploadResult:
        self._validate_key(key)
        self._validate_mime_type(mime_type)
        sha256, byte_size = self._hash_bytes(data)
        response = self._call(
            "upload_bytes",
            key,
            lambda: self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                **self._extra_args(sha256, byte_size, mime_type),
            ),
        )
        etag = response.get("ETag") if isinstance(response, dict) else None
        return UploadResult(
            key=key,
            byte_size=byte_size,
            mime_type=mime_type,
            sha256=sha256,
            etag=str(etag).strip('"') if etag else None,
        )

    def delete_object(self, key: str) -> bool:
        self._validate_key(key)
        try:
            self._call(
                "delete_object",
                key,
                lambda: self._client.delete_object(Bucket=self.bucket, Key=key),
            )
            return True
        except ClientError as exc:
            if self._is_missing(exc):
                return True
            raise

    def generate_presigned_get(self, key: str, expires_in: int | None = None) -> str:
        self._validate_key(key)
        ttl = self.config.presign_ttl_seconds if expires_in is None else expires_in
        if ttl < 1 or ttl > 3600:
            raise ValueError("Presigned URL lifetime must be between 1 and 3600 seconds")
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    def verify_object(
        self,
        key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        metadata: ObjectMetadata | None = None,
    ) -> ObjectVerification:
        self._validate_key(key)
        object_metadata = metadata if metadata is not None else self.head_object(key)
        if object_metadata is None:
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

        def download_and_hash() -> tuple[str, int]:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            digest = hashlib.sha256()
            byte_size = 0
            try:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    digest.update(chunk)
                    byte_size += len(chunk)
            finally:
                body.close()
            return digest.hexdigest(), byte_size

        actual_sha256, byte_size = self._call("get_object", key, download_and_hash)
        required_sha256 = expected_sha256 or object_metadata.sha256
        required_size = expected_size if expected_size is not None else object_metadata.byte_size
        verified = (
            required_sha256 is not None
            and actual_sha256 == required_sha256
            and required_size == byte_size
        )
        reason = None if verified else "checksum_or_size_mismatch"
        return ObjectVerification(
            key=key,
            exists=True,
            verified=verified,
            expected_sha256=required_sha256,
            actual_sha256=actual_sha256,
            expected_size=required_size,
            actual_size=byte_size,
            reason=reason,
        )

    def list_objects_page(
        self,
        *,
        prefix: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
    ) -> ObjectPage:
        if max_keys < 1 or max_keys > 1000:
            raise ValueError("max_keys must be between 1 and 1000")
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Prefix": prefix,
            "MaxKeys": max_keys,
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = self._call(
            "list_objects_v2",
            None,
            lambda: self._client.list_objects_v2(**kwargs),
        )
        objects = tuple(
            ObjectMetadata(
                key=str(entry["Key"]),
                byte_size=int(entry.get("Size", 0)),
                mime_type=None,
                sha256=None,
                etag=str(entry.get("ETag", "")).strip('"') or None,
                last_modified=entry.get("LastModified"),
            )
            for entry in response.get("Contents", ())
        )
        truncated = bool(response.get("IsTruncated"))
        next_token = response.get("NextContinuationToken") if truncated else None
        if truncated and (next_token is None or not str(next_token)):
            raise StorageError("S3 list_objects_v2 returned an invalid continuation token")
        return ObjectPage(objects=objects, next_token=str(next_token) if next_token is not None else None)

    def health_check(self) -> bool:
        try:
            self._call(
                "head_bucket",
                None,
                lambda: self._client.head_bucket(Bucket=self.bucket),
            )
            return True
        except (ClientError, StorageError):
            return False
