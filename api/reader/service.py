"""Query service for bounded, cursor-based local reader chunks."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from models import Chapter, ChapterImportStatus, Page, PageIntegrityStatus, Series

from .cursor import ReaderDirection, decode_cursor, encode_cursor
from .schemas import ReaderChapterOut, ReaderChunksOut, ReaderPageOut, ReaderSeriesOut


def _position_filter(
    chapter_model: type[Chapter],
    boundary_number: Decimal,
    boundary_id: uuid.UUID,
    direction: ReaderDirection,
):
    if direction is ReaderDirection.next:
        return or_(
            chapter_model.number > boundary_number,
            and_(chapter_model.number == boundary_number, chapter_model.id > boundary_id),
        )
    return or_(
        chapter_model.number < boundary_number,
        and_(chapter_model.number == boundary_number, chapter_model.id < boundary_id),
    )


def _ordered_chapters_query(series_id: uuid.UUID):
    return select(Chapter).where(
        Chapter.series_id == series_id,
        Chapter.import_status == ChapterImportStatus.ready,
    )


def _page_output(page: Page) -> ReaderPageOut:
    aspect_ratio = None
    if page.width and page.height and page.height > 0:
        aspect_ratio = round(page.width / page.height, 7)
    return ReaderPageOut(
        id=page.id,
        page_number=page.page_number,
        media_path=f"/media/pages/{page.id}",
        width=page.width,
        height=page.height,
        aspect_ratio=aspect_ratio,
    )


def get_reader_chunks(
    db: Session,
    *,
    series_slug: str,
    start_chapter_id: uuid.UUID | None,
    cursor: str | None,
    direction: ReaderDirection,
    limit: int,
) -> ReaderChunksOut:
    series = db.scalar(select(Series).where(Series.slug == series_slug))
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")

    if start_chapter_id is not None and cursor is not None:
        raise HTTPException(status_code=400, detail="start_chapter_id and cursor are mutually exclusive")
    if start_chapter_id is None and cursor is None:
        raise HTTPException(status_code=400, detail="start_chapter_id or cursor is required")

    if start_chapter_id is not None:
        start = db.scalar(
            _ordered_chapters_query(series.id).where(Chapter.id == start_chapter_id)
        )
        if start is None:
            raise HTTPException(status_code=404, detail="Start chapter not found in series")
        # The explicit keyset predicate handles Decimal ordering and duplicate numbers.
        chapters_query = _ordered_chapters_query(series.id).where(
            _position_filter(Chapter, start.number, start.id, direction)
        )
        remaining_limit = max(0, limit - 1)
        if direction is ReaderDirection.previous:
            chapters_query = chapters_query.order_by(Chapter.number.desc(), Chapter.id.desc())
        else:
            chapters_query = chapters_query.order_by(Chapter.number, Chapter.id)
        chapters = list(db.scalars(chapters_query.limit(remaining_limit)).all())
        if direction is ReaderDirection.previous:
            chapters.reverse()
            chapters.append(start)
        else:
            chapters.insert(0, start)
    else:
        try:
            boundary_number, boundary_id = decode_cursor(cursor or "", series.id, direction)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        chapters_query = _ordered_chapters_query(series.id).where(
            _position_filter(Chapter, boundary_number, boundary_id, direction)
        )
        if direction is ReaderDirection.previous:
            chapters_query = chapters_query.order_by(Chapter.number.desc(), Chapter.id.desc())
        else:
            chapters_query = chapters_query.order_by(Chapter.number, Chapter.id)
        chapters = list(db.scalars(chapters_query.limit(limit)).all())
        if direction is ReaderDirection.previous:
            chapters.reverse()

    if not chapters:
        return ReaderChunksOut(
            series=ReaderSeriesOut(id=series.id, slug=series.slug, title=series.title),
            chapters=[],
            next_cursor=None,
            previous_cursor=None,
            has_more_next=False,
            has_more_previous=False,
        )

    chapter_ids = [chapter.id for chapter in chapters]
    previous_chapter = aliased(Chapter)
    next_chapter = aliased(Chapter)
    previous_id = (
        select(previous_chapter.id)
        .where(
            previous_chapter.series_id == series.id,
            previous_chapter.import_status == ChapterImportStatus.ready,
            _position_filter(previous_chapter, Chapter.number, Chapter.id, ReaderDirection.previous),
        )
        .order_by(previous_chapter.number.desc(), previous_chapter.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    next_id = (
        select(next_chapter.id)
        .where(
            next_chapter.series_id == series.id,
            next_chapter.import_status == ChapterImportStatus.ready,
            _position_filter(next_chapter, Chapter.number, Chapter.id, ReaderDirection.next),
        )
        .order_by(next_chapter.number, next_chapter.id)
        .limit(1)
        .scalar_subquery()
    )
    chapter_rows = db.execute(
        select(Chapter, previous_id.label("previous_id"), next_id.label("next_id"))
        .where(Chapter.id.in_(chapter_ids))
    ).all()
    chapter_details = {
        chapter.id: (chapter, previous, following)
        for chapter, previous, following in chapter_rows
    }

    pages = list(
        db.scalars(
            select(Page)
            .where(
                Page.chapter_id.in_(chapter_ids),
                Page.integrity_status == PageIntegrityStatus.verified,
            )
            .order_by(Page.chapter_id, Page.page_number)
        ).all()
    )
    pages_by_chapter: dict[uuid.UUID, list[ReaderPageOut]] = {chapter_id: [] for chapter_id in chapter_ids}
    for page in pages:
        pages_by_chapter[page.chapter_id].append(_page_output(page))

    output_chapters = []
    for chapter in chapters:
        row, previous, following = chapter_details[chapter.id]
        chapter_pages = pages_by_chapter[chapter.id]
        output_chapters.append(
            ReaderChapterOut(
                id=row.id,
                number=row.number,
                title=row.title,
                page_count=len(chapter_pages),
                previous_chapter_id=previous,
                next_chapter_id=following,
                pages=chapter_pages,
            )
        )

    first = output_chapters[0]
    last = output_chapters[-1]
    has_more_previous = first.previous_chapter_id is not None
    has_more_next = last.next_chapter_id is not None
    return ReaderChunksOut(
        series=ReaderSeriesOut(id=series.id, slug=series.slug, title=series.title),
        chapters=output_chapters,
        next_cursor=(
            encode_cursor(series.id, last.number, last.id, ReaderDirection.next)
            if has_more_next
            else None
        ),
        previous_cursor=(
            encode_cursor(series.id, first.number, first.id, ReaderDirection.previous)
            if has_more_previous
            else None
        ),
        has_more_next=has_more_next,
        has_more_previous=has_more_previous,
    )
