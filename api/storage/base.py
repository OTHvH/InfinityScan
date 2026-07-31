"""Typed object-storage contract and value types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    """Raised when an object-storage operation cannot be completed."""


class StorageConfigurationError(StorageError):
    """Raised when object storage is enabled but incorrectly configured."""


@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    byte_size: int
    mime_type: str | None
    sha256: str | None
    etag: str | None
    last_modified: datetime | None = None


@dataclass(frozen=True)
class ObjectPage:
    objects: tuple[ObjectMetadata, ...]
    next_token: str | None


@dataclass(frozen=True)
class UploadResult:
    key: str
    byte_size: int
    mime_type: str
    sha256: str
    etag: str | None


@dataclass(frozen=True)
class DownloadResult:
    key: str
    byte_size: int
    mime_type: str | None
    sha256: str
    etag: str | None


@dataclass(frozen=True)
class ObjectVerification:
    key: str
    exists: bool
    verified: bool
    expected_sha256: str | None
    actual_sha256: str | None
    expected_size: int | None
    actual_size: int | None
    reason: str | None = None


@runtime_checkable
class ObjectStorage(Protocol):
    def head_object(self, key: str) -> ObjectMetadata | None:
        """Return metadata for an object, or ``None`` when it is missing."""

    def upload_file(self, key: str, file_path: str | Path, mime_type: str) -> UploadResult:
        """Upload a local file with verified MIME metadata."""

    def upload_bytes(self, key: str, data: bytes, mime_type: str) -> UploadResult:
        """Upload bytes with verified MIME metadata."""

    def download_file(
        self,
        key: str,
        destination: str | Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        max_bytes: int = 1024 * 1024 * 1024,
    ) -> DownloadResult:
        """Atomically stream an object to a file with a strict size bound."""

    def delete_object(self, key: str) -> bool:
        """Delete an object. Missing objects are treated as already deleted."""

    def object_exists(self, key: str) -> bool:
        """Return whether an object exists."""

    def generate_presigned_get(self, key: str, expires_in: int | None = None) -> str:
        """Generate a short-lived GET URL without logging or exposing it."""

    def verify_object(
        self,
        key: str,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        metadata: ObjectMetadata | None = None,
    ) -> ObjectVerification:
        """Stream and verify object bytes against expected metadata."""

    def list_objects_page(
        self,
        *,
        prefix: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
    ) -> ObjectPage:
        """Return one bounded object-inventory page and an opaque next token."""

    def health_check(self) -> bool:
        """Check that the configured bucket is reachable."""
