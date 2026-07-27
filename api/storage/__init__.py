"""Object-storage backends."""

from __future__ import annotations

from settings import Settings, get_settings

from .base import (
    ObjectMetadata,
    ObjectPage,
    ObjectStorage,
    ObjectVerification,
    StorageConfigurationError,
    StorageError,
    UploadResult,
)
from .s3 import S3CompatibleStorage, S3StorageConfig
from .keys import cover_object_key, page_object_key


def create_object_storage(settings: Settings | None = None) -> ObjectStorage | None:
    """Create configured storage, or ``None`` when object storage is disabled."""
    config = settings or get_settings()
    if not config.object_storage_enabled:
        return None
    return S3CompatibleStorage.from_settings(config)


__all__ = [
    "ObjectMetadata",
    "ObjectPage",
    "ObjectStorage",
    "ObjectVerification",
    "S3CompatibleStorage",
    "S3StorageConfig",
    "StorageConfigurationError",
    "StorageError",
    "UploadResult",
    "create_object_storage",
    "cover_object_key",
    "page_object_key",
]
