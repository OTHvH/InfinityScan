"""Adapter protocol and manifest dataclasses."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ManifestPage:
    """A single page within a chapter manifest."""

    page_number: int
    source_path: str
    sha256: str
    mime_type: str
    file_extension: str
    width: int
    height: int
    byte_size: int


@dataclass(frozen=True)
class ManifestChapter:
    """A single chapter within a series manifest."""

    chapter_id: uuid.UUID
    folder_name: str
    number: Decimal
    title: str | None
    language: str
    pages: tuple[ManifestPage, ...] = ()


@dataclass(frozen=True)
class ManifestSeries:
    """A series manifest ready for validation and import."""

    series_id: uuid.UUID
    folder_name: str
    slug: str
    title: str
    content_type: str
    chapters: tuple[ManifestChapter, ...] = ()
    cover_source_path: str | None = None
    cover_object_key: str | None = None
    cover_sha256: str | None = None
    cover_mime_type: str | None = None
    cover_file_extension: str | None = None


@runtime_checkable
class ImportAdapter(Protocol):
    """Protocol for adapters that scan a source and produce manifests."""

    def scan(self) -> list[ManifestSeries]:
        """Scan the source and return a list of series manifests."""
        ...
