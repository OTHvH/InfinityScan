"""Canonical immutable object-key construction."""

from __future__ import annotations

import re
import uuid


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,16}$")


def _validate_hash(sha256: str) -> None:
    if not _SHA256_RE.fullmatch(sha256):
        raise ValueError("sha256 must be a lowercase 64-character hexadecimal digest")


def _validate_extension(extension: str) -> str:
    normalized = extension.lower().lstrip(".")
    if not _EXTENSION_RE.fullmatch(normalized):
        raise ValueError("verified extension is invalid")
    return normalized


def page_object_key(
    series_id: uuid.UUID,
    chapter_id: uuid.UUID,
    page_number: int,
    sha256: str,
    verified_extension: str,
) -> str:
    """Build a content-addressed page key with a stable five-digit page number."""
    if page_number < 1 or page_number > 99999:
        raise ValueError("page_number must be between 1 and 99999")
    _validate_hash(sha256)
    extension = _validate_extension(verified_extension)
    return f"series/{series_id}/{chapter_id}/{page_number:05d}-{sha256}.{extension}"


def cover_object_key(
    series_id: uuid.UUID,
    sha256: str,
    verified_extension: str,
) -> str:
    """Build a content-addressed series cover key."""
    _validate_hash(sha256)
    extension = _validate_extension(verified_extension)
    return f"series/{series_id}/cover-{sha256}.{extension}"
