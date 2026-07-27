"""Manifest construction, hashing, and serialization."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from .adapters.base import (
    ManifestChapter,
    ManifestPage,
    ManifestRejectedFile,
    ManifestSeries,
)


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
            "rejected_files": [
                {
                    "source_reference": rejected.source_reference,
                    "status": rejected.status,
                    "reason": rejected.reason,
                }
                for rejected in s.rejected_files
            ],
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


def manifest_to_recovery_payload(
    manifest: list[ManifestSeries], *, prune: bool = False
) -> dict[str, Any]:
    """Serialize a manifest with relative per-file source references."""
    source_paths = [
        path
        for series in manifest
        for path in (
            [series.cover_source_path] if series.cover_source_path else []
        )
        + [page.source_path for chapter in series.chapters for page in chapter.pages]
    ]
    if source_paths:
        root = Path(os.path.commonpath(source_paths))
        if root.is_file() or all(Path(path) == root for path in source_paths):
            root = root.parent
        root = root.resolve()
    else:
        root = Path(".").resolve()

    data = manifest_to_dict(manifest)
    data.pop("generated_at", None)
    for series in data["series"]:
        if series["cover_source_path"]:
            series["cover_source_path"] = _relative_reference(
                Path(series["cover_source_path"]), root
            )
        for chapter in series["chapters"]:
            for page in chapter["pages"]:
                page["source_path"] = _relative_reference(Path(page["source_path"]), root)
        for rejected in series["rejected_files"]:
            reference = Path(rejected["source_reference"])
            if reference.is_absolute():
                try:
                    rejected["source_reference"] = _relative_reference(reference, root)
                except ValueError:
                    rejected["source_reference"] = reference.name
            elif ".." in reference.parts:
                rejected["source_reference"] = reference.name
            else:
                rejected["source_reference"] = reference.as_posix()
            rejected["reason"] = "source file rejected during scan"
    payload = {
        "version": 1,
        "source_root": str(root),
        "prune": bool(prune),
        "manifest_hash": hash_manifest(manifest),
        "manifest": data,
    }
    payload["payload_hash"] = _recovery_payload_hash(payload)
    return payload


def manifest_from_recovery_payload(payload: dict[str, Any]) -> tuple[list[ManifestSeries], bool]:
    """Validate and deserialize a persisted recovery payload."""
    if payload.get("version") != 1 or not isinstance(payload.get("manifest"), dict):
        raise ValueError("unsupported recovery payload")
    if payload.get("payload_hash") != _recovery_payload_hash(payload):
        raise ValueError("recovery payload integrity check failed")
    root_value = payload.get("source_root")
    if not isinstance(root_value, str) or not root_value:
        raise ValueError("recovery payload has no source root")
    root = Path(root_value).resolve()
    raw_series = payload["manifest"].get("series")
    if not isinstance(raw_series, list):
        raise ValueError("recovery payload has no series")

    series_result: list[ManifestSeries] = []
    for raw_s in raw_series:
        chapters: list[ManifestChapter] = []
        for raw_c in raw_s["chapters"]:
            pages = tuple(
                ManifestPage(
                    page_number=int(raw_p["page_number"]),
                    source_path=str(_resolve_reference(root, raw_p["source_path"])),
                    sha256=str(raw_p["sha256"]),
                    mime_type=str(raw_p["mime_type"]),
                    file_extension=str(raw_p["file_extension"]),
                    width=int(raw_p["width"]),
                    height=int(raw_p["height"]),
                    byte_size=int(raw_p["byte_size"]),
                )
                for raw_p in raw_c["pages"]
            )
            chapters.append(
                ManifestChapter(
                    chapter_id=uuid.UUID(str(raw_c["chapter_id"])),
                    folder_name=str(raw_c["folder_name"]),
                    number=Decimal(str(raw_c["number"])),
                    title=raw_c.get("title"),
                    language=str(raw_c["language"]),
                    pages=pages,
                )
            )
        cover_ref = raw_s.get("cover_source_path")
        rejected = tuple(
            ManifestRejectedFile(
                source_reference=str(raw_r["source_reference"]),
                status=raw_r["status"],
                reason=str(raw_r["reason"]),
            )
            for raw_r in raw_s.get("rejected_files", [])
        )
        series_result.append(
            ManifestSeries(
                series_id=uuid.UUID(str(raw_s["series_id"])),
                folder_name=str(raw_s["folder_name"]),
                slug=str(raw_s["slug"]),
                title=str(raw_s["title"]),
                content_type=str(raw_s["content_type"]),
                chapters=tuple(chapters),
                cover_source_path=(
                    str(_resolve_reference(root, cover_ref)) if cover_ref else None
                ),
                cover_object_key=raw_s.get("cover_object_key"),
                cover_sha256=raw_s.get("cover_sha256"),
                cover_mime_type=raw_s.get("cover_mime_type"),
                cover_file_extension=raw_s.get("cover_file_extension"),
                rejected_files=rejected,
            )
        )
    return series_result, bool(payload.get("prune", False))


def recovery_payload_with_prune(payload: dict[str, Any], *, prune: bool) -> dict[str, Any]:
    """Return an integrity-protected payload with an updated prune option."""
    updated = dict(payload)
    updated["prune"] = bool(prune)
    updated["payload_hash"] = _recovery_payload_hash(updated)
    return updated


def _relative_reference(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("source file is outside the recovery root") from exc
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe source reference")
    return relative.as_posix()


def _resolve_reference(root: Path, reference: object) -> Path:
    if not isinstance(reference, str) or not reference:
        raise ValueError("invalid source reference")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe source reference")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("source reference escapes recovery root") from exc
    return resolved


def _recovery_payload_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "payload_hash"}
    encoded = json.dumps(
        content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
