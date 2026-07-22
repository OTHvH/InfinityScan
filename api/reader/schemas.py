"""Strict response schemas for local imported-content reader chunks."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReaderSeriesOut(_Strict):
    id: UUID
    slug: str
    title: str


class ReaderPageOut(_Strict):
    id: UUID
    page_number: int
    media_path: str = Field(pattern=r"^/media/pages/[^?]+$")
    width: int | None
    height: int | None
    aspect_ratio: float | None


class ReaderChapterOut(_Strict):
    id: UUID
    number: Decimal
    title: str | None
    page_count: int
    previous_chapter_id: UUID | None
    next_chapter_id: UUID | None
    pages: list[ReaderPageOut]

    @field_serializer("number")
    def serialize_number(self, value: Decimal) -> str:
        normalized = value.normalize()
        return "0" if normalized == 0 else format(normalized, "f")


class ReaderChunksOut(_Strict):
    series: ReaderSeriesOut
    chapters: list[ReaderChapterOut]
    next_cursor: str | None
    previous_cursor: str | None
    has_more_next: bool
    has_more_previous: bool
