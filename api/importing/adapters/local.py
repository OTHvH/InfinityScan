"""Local folder scanner adapter."""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Optional

from image_inspector import ImageInspector, ImageInspectionError

from .base import ManifestChapter, ManifestPage, ManifestRejectedFile, ManifestSeries

log = logging.getLogger("importing.local")

IMPORT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://infinityscan.local/import")

_IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".avif", ".ico"}
)

_CHAPTER_RE = re.compile(
    r"""
    (?:chapter|chap|ch|c)[\s._\-]*
    (\d+(?:[._]\d+)?)
    |
    (\d+(?:[._]\d+)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def natural_key(s: str) -> list[object]:
    parts = re.split(r"(\d+)", s)
    out: list[object] = []
    for p in parts:
        out.append(int(p) if p.isdigit() else p.casefold())
    return out


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def stable_series_id(slug: str) -> uuid.UUID:
    return uuid.uuid5(IMPORT_NAMESPACE, f"series:{slug}")


def stable_chapter_id(series_id: uuid.UUID, number: Decimal, language: str) -> uuid.UUID:
    number_key = format(number.normalize(), "f")
    return uuid.uuid5(series_id, f"chapter:{number_key}:{language}")


def extract_chapter_number(name: str) -> Decimal:
    m = _CHAPTER_RE.search(name)
    if m:
        raw = m.group(1) or m.group(2)
        if raw:
            return Decimal(raw.replace("_", "."))
    return Decimal("0")


class LocalAdapter:
    """Scans a local folder tree and produces manifest dataclasses."""

    def __init__(
        self,
        root: Path,
        language: str = "en",
        content_type: str = "manga",
        series_filter: Optional[str] = None,
        inspector: Optional[ImageInspector] = None,
    ) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"import root does not exist: {self.root}")
        self.language = language
        self.content_type = content_type
        self.series_filter = series_filter.casefold() if series_filter else None
        self.inspector = inspector or ImageInspector(self.root)

    def _list_images(self, folder: Path) -> list[Path]:
        imgs = [
            p
            for p in folder.iterdir()
            if p.is_file() and p.suffix.casefold().lstrip(".") in {
                "jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "svg", "avif", "ico",
            }
        ]
        imgs.sort(key=lambda p: natural_key(p.name))
        return imgs

    def _list_files(self, folder: Path) -> list[Path]:
        files = [path for path in folder.iterdir() if path.is_file() and not path.name.startswith(".")]
        files.sort(key=lambda path: natural_key(path.name))
        return files

    def _source_reference(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _list_subdirs(self, folder: Path) -> list[Path]:
        dirs = [p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]
        dirs.sort(key=lambda p: natural_key(p.name))
        return dirs

    def _inspect_page(self, image_path: Path) -> ManifestPage:
        inspection = self.inspector.inspect(image_path)
        return ManifestPage(
            page_number=0,
            source_path=str(inspection.source_path),
            sha256=inspection.sha256,
            mime_type=inspection.mime_type,
            file_extension=inspection.verified_extension,
            width=inspection.width,
            height=inspection.height,
            byte_size=inspection.byte_size,
        )

    def _scan_chapter(
        self, series_id: uuid.UUID, folder: Path, chapter_number: Decimal
    ) -> tuple[ManifestChapter, list[ManifestRejectedFile]]:
        chapter_id = stable_chapter_id(series_id, chapter_number, self.language)
        pages: list[ManifestPage] = []
        rejected: list[ManifestRejectedFile] = []
        for image_path in self._list_files(folder):
            if image_path.suffix.casefold() not in _IMAGE_EXTS:
                rejected.append(ManifestRejectedFile(
                    source_reference=self._source_reference(image_path),
                    status="skipped",
                    reason="file extension is not an image candidate",
                ))
                continue
            try:
                page = self._inspect_page(image_path)
            except ImageInspectionError as exc:
                rejected.append(ManifestRejectedFile(
                    source_reference=self._source_reference(image_path),
                    status="failed",
                    reason=str(exc),
                ))
                continue
            pages.append(
                ManifestPage(
                    page_number=len(pages) + 1,
                    source_path=page.source_path,
                    sha256=page.sha256,
                    mime_type=page.mime_type,
                    file_extension=page.file_extension,
                    width=page.width,
                    height=page.height,
                    byte_size=page.byte_size,
                )
            )
        title_clean = re.sub(
            r"(?:chapter|chap|ch|c)[\s._\-]*\d+(?:[._]\d+)?",
            "",
            folder.name,
            flags=re.IGNORECASE,
        ).strip(" -_.")
        return (
            ManifestChapter(
                chapter_id=chapter_id,
                folder_name=folder.name,
                number=chapter_number,
                title=title_clean or None,
                language=self.language,
                pages=tuple(pages),
            ),
            rejected,
        )

    def _scan_series(self, folder: Path) -> ManifestSeries | None:
        slug = slugify(folder.name)
        series_id = stable_series_id(slug)
        chapter_dirs = self._list_subdirs(folder)
        chapters: list[ManifestChapter] = []
        rejected_files: list[ManifestRejectedFile] = []
        cover_path: Path | None = None

        top_imgs = self._list_images(folder)
        if top_imgs:
            cover_path = top_imgs[0]

        for ch_dir in chapter_dirs:
            if not self._list_images(ch_dir):
                continue
            num = extract_chapter_number(ch_dir.name)
            chapter, rejected = self._scan_chapter(series_id, ch_dir, num)
            chapters.append(chapter)
            rejected_files.extend(rejected)

        if not chapters:
            if top_imgs:
                log.debug("  '%s': flat series (no chapter folders), treating as ch1", folder.name)
                chapter_id = stable_chapter_id(series_id, Decimal("1.0"), self.language)
                flat_pages: list[ManifestPage] = []
                for i, img in enumerate(top_imgs, start=1):
                    page = self._inspect_page(img)
                    flat_pages.append(
                        ManifestPage(
                            page_number=i,
                            source_path=page.source_path,
                            sha256=page.sha256,
                            mime_type=page.mime_type,
                            file_extension=page.file_extension,
                            width=page.width,
                            height=page.height,
                            byte_size=page.byte_size,
                        )
                    )
                chapters = [
                    ManifestChapter(
                        chapter_id=chapter_id,
                        folder_name=folder.name,
                        number=Decimal("1.0"),
                        title=None,
                        language=self.language,
                        pages=tuple(flat_pages),
                    )
                ]
            else:
                return None

        chapters.sort(key=lambda c: c.number)

        cover_key: str | None = None
        cover_sha256: str | None = None
        cover_mime_type: str | None = None
        cover_file_ext: str | None = None
        if cover_path:
            try:
                cover = self.inspector.inspect(cover_path)
                from storage import cover_object_key

                cover_key = cover_object_key(series_id, cover.sha256, cover.verified_extension)
                cover_sha256 = cover.sha256
                cover_mime_type = cover.mime_type
                cover_file_ext = cover.verified_extension
            except ImageInspectionError as exc:
                log.warning("  cover inspection failed for '%s': %s", folder.name, exc)

        return ManifestSeries(
            series_id=series_id,
            folder_name=folder.name,
            slug=slug,
            title=folder.name,
            content_type=self.content_type,
            chapters=tuple(chapters),
            cover_source_path=str(cover_path) if cover_path else None,
            cover_object_key=cover_key,
            cover_sha256=cover_sha256,
            cover_mime_type=cover_mime_type,
            cover_file_extension=cover_file_ext,
            rejected_files=tuple(rejected_files),
        )

    def scan(self) -> list[ManifestSeries]:
        series: list[ManifestSeries] = []
        top_dirs = self._list_subdirs(self.root)
        log.info("Scanning '%s' - found %d top-level directories", self.root, len(top_dirs))

        for folder in top_dirs:
            if self.series_filter and self.series_filter not in folder.name.casefold():
                continue
            log.info("  -> %s", folder.name)
            entry = self._scan_series(folder)
            if entry is None:
                log.warning("    Skipped (no images found)")
                continue
            total_pages = sum(len(ch.pages) for ch in entry.chapters)
            log.info(
                "    %d chapter(s), %d page(s)", len(entry.chapters), total_pages
            )
            series.append(entry)

        log.info("Scan complete: %d series", len(series))
        return series
