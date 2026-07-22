"""Pre-upload manifest validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .adapters.base import ManifestSeries

_VALID_CONTENT_TYPES = {"manga", "manhua", "manhwa"}
_OBJECT_KEY_RE = re.compile(r"^series/[0-9a-f-]+/[0-9a-f-]+/\d{5}-[0-9a-f]{64}\.[a-z0-9]+$")
_COVER_KEY_RE = re.compile(r"^series/[0-9a-f-]+/cover-[0-9a-f]{64}\.[a-z0-9]+$")


@dataclass(frozen=True)
class ValidationError:
    """A single validation error with context."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def validate_manifest(manifest: list[ManifestSeries]) -> list[ValidationError]:
    """Validate every manifest entry. Returns a list of errors (empty = valid)."""
    errors: list[ValidationError] = []

    if not manifest:
        errors.append(ValidationError("manifest", "manifest contains no series"))
        return errors

    seen_slugs: set[str] = set()
    seen_chapter_ids: set[str] = set()

    for si, series in enumerate(manifest):
        sp = f"series[{si}] '{series.slug}'"

        if not series.slug:
            errors.append(ValidationError(sp, "slug is empty"))
        elif series.slug in seen_slugs:
            errors.append(ValidationError(sp, f"duplicate slug '{series.slug}'"))
        seen_slugs.add(series.slug)

        if not series.title:
            errors.append(ValidationError(sp, "title is empty"))

        if series.content_type not in _VALID_CONTENT_TYPES:
            errors.append(ValidationError(sp, f"invalid content_type '{series.content_type}'"))

        if not series.chapters:
            errors.append(ValidationError(sp, "no chapters"))
            continue

        for ci, chapter in enumerate(series.chapters):
            cp = f"{sp}/chapter[{ci}] '{chapter.folder_name}'"

            ch_id_str = str(chapter.chapter_id)
            if ch_id_str in seen_chapter_ids:
                errors.append(ValidationError(cp, f"duplicate chapter_id {ch_id_str}"))
            seen_chapter_ids.add(ch_id_str)

            if chapter.number < Decimal("0"):
                errors.append(ValidationError(cp, "chapter number is negative"))

            if not chapter.language:
                errors.append(ValidationError(cp, "language is empty"))

            if not chapter.pages:
                errors.append(ValidationError(cp, "no pages"))
                continue

            page_numbers = [p.page_number for p in chapter.pages]
            if sorted(page_numbers) != list(range(1, len(chapter.pages) + 1)):
                errors.append(ValidationError(cp, "page numbers are not 1-based sequential"))

            if len(chapter.pages) != len(set(page_numbers)):
                errors.append(ValidationError(cp, "duplicate page numbers"))

            for pi, page in enumerate(chapter.pages):
                pp = f"{cp}/page[{pi}]"

                if not page.sha256 or len(page.sha256) != 64:
                    errors.append(ValidationError(pp, "invalid sha256"))

                if page.width < 1 or page.height < 1:
                    errors.append(ValidationError(pp, "invalid dimensions"))

                if page.byte_size < 1:
                    errors.append(ValidationError(pp, "byte_size must be positive"))

    return errors


def validate_manifest_strict(manifest: list[ManifestSeries]) -> None:
    """Raise ValueError if manifest has any validation errors."""
    errors = validate_manifest(manifest)
    if errors:
        summary = "; ".join(str(e) for e in errors[:10])
        suffix = f" (and {len(errors) - 10} more)" if len(errors) > 10 else ""
        raise ValueError(f"manifest validation failed: {summary}{suffix}")
