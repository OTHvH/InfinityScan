from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """Raised when a provider request or parsing fails."""


class ProviderTimeout(ProviderError):
    """Raised when a provider request times out."""


@dataclass(frozen=True)
class NormalizedSeries:
    external_id: str
    title: str
    description: str = ""
    cover_url: str | None = None
    status: str | None = None
    content_type: str | None = None
    year: int | None = None
    tags: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NormalizedChapter:
    external_id: str
    number: Decimal
    title: str | None = None
    volume: str | None = None
    language: str = ""
    page_count: int = 0
    published_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class NormalizedPageReference:
    page_number: int
    url: str
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None


@runtime_checkable
class ProviderAdapter(Protocol):
    @property
    def key(self) -> str:
        """Unique identifier for this provider (e.g., 'copymanga')."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable provider name."""
        ...

    @property
    def enabled(self) -> bool:
        """Whether this adapter is currently active."""
        ...

    @property
    def base_url(self) -> str:
        """Allowlisted base URL for this provider's API."""
        ...

    async def health_check(self) -> bool:
        """Check provider availability. Returns True if reachable."""
        ...

    async def search_series(
        self, query: str, *, limit: int = 20, offset: int = 0
    ) -> list[NormalizedSeries]:
        """Search for series matching query."""
        ...

    async def get_series(self, external_id: str) -> NormalizedSeries:
        """Fetch a single series by provider-specific identifier."""
        ...

    async def list_chapters(
        self, external_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[NormalizedChapter]:
        """List chapters for a series, ordered by the provider."""
        ...

    async def get_chapter_pages(
        self, external_id: str, chapter_external_id: str
    ) -> list[NormalizedPageReference]:
        """Fetch ordered page references for a chapter."""
        ...
