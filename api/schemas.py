"""Strict Pydantic schemas for request validation and response serialisation.

Every schema uses ``model_config = ConfigDict(extra="forbid")`` so that
unexpected fields are rejected at the boundary.  Tokens are never returned
in JSON — the ``TokenResponse`` only carries user metadata.

Note: Auth schemas (RegisterIn, LoginIn, UserOut, RegisterOut, LoginOut)
are now in auth/schemas.py.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_serializer


# ── Shared base ──────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Content schemas ─────────────────────────────────────────────────────────


class ProviderSeriesOut(_Strict):
    external_id: str
    title: str
    description: str = ""
    cover_url: str | None = None
    status: str | None = None
    content_type: str | None = None
    year: int | None = None
    tags: list[str] = []
    authors: list[str] = []


class ProviderSeriesListOut(_Strict):
    total: int
    limit: int
    offset: int
    list: list[ProviderSeriesOut]


class ProviderChapterItemOut(_Strict):
    external_id: str
    number: Decimal
    title: str | None = None
    volume: str | None = None
    language: str = ""
    page_count: int = 0
    published_at: str | None = None

    @field_serializer("number")
    def serialize_number(self, value: Decimal) -> str:
        return format(value.normalize(), "f")


class ProviderChapterListOut(_Strict):
    total: int
    limit: int
    offset: int
    list: list[ProviderChapterItemOut]


class ComicOut(_Strict):
    path_word: str
    name: str
    alias: str | None
    cover: str | None
    status: dict | None
    author: list[dict] | None
    theme: list[dict] | None
    brief: str | None
    last_chapter: dict | None


class LocalSeriesOut(_Strict):
    id: str
    slug: str
    title: str
    synopsis: str | None
    cover_url: str | None
    content_type: str
    status: str
    year: int | None
    is_nsfw: bool


class LocalChapterOut(_Strict):
    id: str
    number: Decimal
    title: str | None
    page_count: int
    published_at: str | None

    @field_serializer("number")
    def serialize_number(self, value: Decimal) -> str:
        return format(value.normalize(), "f")


class LocalSeriesDetailOut(LocalSeriesOut):
    chapters: list[LocalChapterOut] = []


class ChapterItem(_Strict):
    uuid: str
    name: str
    index: int
    count: int


class ChapterListOut(_Strict):
    total: int
    limit: int
    offset: int
    list: list[ChapterItem]


class PageMeta(_Strict):
    page_number: int
    url: str


class ChapterPagesOut(_Strict):
    chapter_uuid: str
    chapter_name: str
    comic_path_word: str
    pages: list[PageMeta]
    prev_chapter_uuid: str | None
    next_chapter_uuid: str | None


class BookmarkIn(_Strict):
    series_path_word: str
    series_name: str


class BookmarkOut(_Strict):
    id: UUID
    series_path_word: str
    series_name: str


class ProgressIn(_Strict):
    chapter_uuid: str
    last_page: Optional[int] = Field(default=None, ge=0)
    scroll_position: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    completed: bool = False


class ProgressOut(_Strict):
    chapter_uuid: str
    last_page: Optional[int]
    scroll_position: Optional[float]
    completed: bool
    updated_at: Optional[datetime] = None
