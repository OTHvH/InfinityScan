"""Manifest construction, hashing, and serialization."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .adapters.base import ManifestChapter, ManifestPage, ManifestSeries


def manifest_to_dict(manifest: list[ManifestSeries]) -> dict[str, Any]:
    """Convert a list of ManifestSeries into a deterministic dict."""

    def _page_dict(p: ManifestPage) -> dict[str, Any]:
        return {
            "page_number": p.page_number,
            "source_path": p.source_path,
            "sha256": p.sha256,
            "mime_type": p.mime_type,
            "file_extension": p.file_extension,
            "width": p.width,
            "height": p.height,
            "byte_size": p.byte_size,
        }

    def _chapter_dict(c: ManifestChapter) -> dict[str, Any]:
        return {
            "chapter_id": str(c.chapter_id),
            "folder_name": c.folder_name,
            "number": str(c.number),
            "title": c.title,
            "language": c.language,
            "page_count": len(c.pages),
            "pages": [_page_dict(p) for p in c.pages],
        }

    def _series_dict(s: ManifestSeries) -> dict[str, Any]:
        return {
            "series_id": str(s.series_id),
            "folder_name": s.folder_name,
            "slug": s.slug,
            "title": s.title,
            "content_type": s.content_type,
            "cover_source_path": s.cover_source_path,
            "cover_object_key": s.cover_object_key,
            "cover_sha256": s.cover_sha256,
            "cover_mime_type": s.cover_mime_type,
            "cover_file_extension": s.cover_file_extension,
            "chapter_count": len(s.chapters),
            "chapters": [_chapter_dict(c) for c in s.chapters],
        }

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "series_count": len(manifest),
        "series": [_series_dict(s) for s in manifest],
    }


def manifest_to_json(manifest: list[ManifestSeries], indent: int | None = 2) -> str:
    """Serialize manifest to deterministic JSON."""
    return json.dumps(manifest_to_dict(manifest), indent=indent, ensure_ascii=False, sort_keys=False)


def hash_manifest(manifest: list[ManifestSeries]) -> str:
    """Compute a deterministic SHA-256 digest of the manifest."""
    data = manifest_to_dict(manifest)
    data.pop("generated_at", None)
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
