"""Canonical local imported-content reader route."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from reader.cursor import ReaderDirection
from reader.schemas import ReaderChunksOut
from reader.service import get_reader_chunks


router = APIRouter(prefix="/reader", tags=["local-reader"])


@router.get("/{series_slug}/chunks", response_model=ReaderChunksOut)
def reader_chunks(
    series_slug: str,
    start_chapter_id: Annotated[UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    direction: Annotated[ReaderDirection, Query()] = ReaderDirection.next,
    limit: Annotated[int, Query(ge=1, le=5)] = 2,
    db: Session = Depends(get_db),
) -> ReaderChunksOut:
    return get_reader_chunks(
        db,
        series_slug=series_slug,
        start_chapter_id=start_chapter_id,
        cursor=cursor,
        direction=direction,
        limit=limit,
    )
