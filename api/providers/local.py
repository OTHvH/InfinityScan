from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from providers.base import NormalizedChapter, NormalizedPageReference, NormalizedSeries, ProviderError

logger = logging.getLogger(__name__)


class LocalContentAdapter:
    """Adapter for operator-controlled local content.

    Wraps the existing importing adapter to present local filesystem content
    through the same ProviderAdapter interface. No network requests are made.
    """

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        base_url: str = "http://localhost:8000",
    ) -> None:
        self._root = root
        self._enabled = enabled
        self._base_url = base_url

    @property
    def key(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        return "Local Import"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def base_url(self) -> str:
        return self._base_url

    async def health_check(self) -> bool:
        return self._root.is_dir()

    async def search_series(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[NormalizedSeries]:
        if not self._root.is_dir():
            raise ProviderError("Local content root directory not found")
        results: list[NormalizedSeries] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            meta_file = entry / "series.json"
            if meta_file.exists():
                import json

                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            else:
                meta = {"title": entry.name}
            title = meta.get("title", entry.name)
            if query.lower() not in title.lower():
                continue
            results.append(
                NormalizedSeries(
                    external_id=entry.name,
                    title=title,
                    description=meta.get("synopsis", ""),
                    cover_url=meta.get("cover_url"),
                    status=meta.get("status"),
                    content_type=meta.get("content_type"),
                    year=meta.get("year"),
                )
            )
        return results[offset : offset + limit]

    async def get_series(self, external_id: str) -> NormalizedSeries:
        series_dir = self._root / external_id
        if not series_dir.is_dir():
            raise ProviderError(f"Local series not found: {external_id}")
        meta_file = series_dir / "series.json"
        if meta_file.exists():
            import json

            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        else:
            meta = {"title": external_id}
        return NormalizedSeries(
            external_id=external_id,
            title=meta.get("title", external_id),
            description=meta.get("synopsis", ""),
            cover_url=meta.get("cover_url"),
            status=meta.get("status"),
            content_type=meta.get("content_type"),
            year=meta.get("year"),
        )

    async def list_chapters(
        self, external_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[NormalizedChapter]:
        series_dir = self._root / external_id
        if not series_dir.is_dir():
            raise ProviderError(f"Local series not found: {external_id}")
        chapters: list[NormalizedChapter] = []
        for entry in sorted(series_dir.iterdir()):
            if not entry.is_dir():
                continue
            page_count = sum(1 for f in entry.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
            try:
                number = Decimal(entry.name)
            except Exception:
                number = Decimal("0")
            chapters.append(
                NormalizedChapter(
                    external_id=entry.name,
                    number=number,
                    page_count=page_count,
                )
            )
        return chapters[offset : offset + limit]

    async def get_chapter_pages(
        self, external_id: str, chapter_external_id: str
    ) -> list[NormalizedPageReference]:
        chapter_dir = self._root / external_id / chapter_external_id
        if not chapter_dir.is_dir():
            raise ProviderError(f"Local chapter not found: {external_id}/{chapter_external_id}")
        pages: list[NormalizedPageReference] = []
        for i, f in enumerate(sorted(chapter_dir.iterdir())):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                pages.append(
                    NormalizedPageReference(
                        page_number=i + 1,
                        url=f"file://{f.as_posix()}",
                    )
                )
        return pages
