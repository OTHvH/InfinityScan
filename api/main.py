"""
InfinityScan API
================
Content is sourced from the CopyManga public API (https://github.com/fumiama/copymanga).
User state (bookmarks, reading progress) is persisted in the local PostgreSQL database.

User identity
-------------
Pass a stable UUID as the ``X-User-ID`` request header.  If omitted a per-request
anonymous UUID is used (state endpoints will therefore not be retrievable later).

Environment variables
---------------------
DATABASE_URL       postgresql+psycopg://user:pass@host:5432/db
COPYMANGA_API      https://api.copymanga.tv  (default)
COPYMANGA_TOKEN    optional bearer token for CopyManga (raises rate-limits)
S3_PUBLIC_URL      https://cdn.example.com  (prefix used when building cover URLs)
CORS_ORIGINS       comma-separated list of allowed origins (default: http://localhost:3000)
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from models import (
    Bookmark,
    Chapter,
    ContentType,
    Page,
    ReadingMode,
    ReadingProgress,
    Series,
    SeriesStatus,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
COPYMANGA_API: str = os.environ.get("COPYMANGA_API", "https://api.copymanga.tv").rstrip("/")
COPYMANGA_TOKEN: str = os.environ.get("COPYMANGA_TOKEN", "")
S3_PUBLIC_URL: str = os.environ.get("S3_PUBLIC_URL", "").rstrip("/")
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

_COPYMANGA_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (compatible; InfinityScan/1.0)",
    "Accept": "application/json",
}
if COPYMANGA_TOKEN:
    _COPYMANGA_HEADERS["Authorization"] = f"Token {COPYMANGA_TOKEN}"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

engine = create_engine(DATABASE_URL, pool_pre_ping=True) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False) if engine else None


def get_db() -> Session:  # type: ignore[return]
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    db: Session = SessionLocal()
    try:
        yield db  # type: ignore[misc]
    finally:
        db.close()


def get_user_id(x_user_id: Optional[str] = Header(None)) -> uuid.UUID:
    """Resolve caller UUID from ``X-User-ID`` header (falls back to a random UUID)."""
    if x_user_id:
        try:
            return uuid.UUID(x_user_id)
        except (ValueError, AttributeError):
            pass  # Fall back to anonymous UUID for invalid headers
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# HTTP client lifecycle
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: Any):
    global _http_client
    _http_client = httpx.AsyncClient(
        headers=_COPYMANGA_HEADERS,
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    )
    yield
    await _http_client.aclose()


async def copymanga(path: str, **params: Any) -> Any:
    """GET from CopyManga API and return the parsed ``results`` payload."""
    if _http_client is None:
        raise HTTPException(status_code=503, detail="HTTP client not initialised")
    url = f"{COPYMANGA_API}/api/v3/{path.lstrip('/')}"
    r = await _http_client.get(url, params={k: v for k, v in params.items() if v is not None})
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found on CopyManga")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"CopyManga returned HTTP {r.status_code}")
    body = r.json()
    if body.get("code") not in (100, 200, None):
        raise HTTPException(status_code=502, detail=f"CopyManga error: {body.get('message')}")
    return body.get("results") or body.get("data") or body


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="InfinityScan API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------


class ComicOut(BaseModel):
    path_word: str
    name: str
    alias: str | None
    cover: str | None
    status: dict | None
    author: list[dict] | None
    theme: list[dict] | None
    brief: str | None
    last_chapter: dict | None


class LocalSeriesOut(BaseModel):
    """Local-library series row serialised for the browser."""

    id: str
    slug: str
    title: str
    synopsis: str | None
    cover_url: str | None  # fully-resolved CDN / S3 URL
    content_type: str
    status: str
    year: int | None
    is_nsfw: bool


class ChapterItem(BaseModel):
    uuid: str
    name: str
    index: int
    count: int  # page count


class ChapterListOut(BaseModel):
    total: int
    limit: int
    offset: int
    list: list[ChapterItem]


class PageMeta(BaseModel):
    page_number: int
    url: str  # direct CDN / proxy URL


class ChapterPagesOut(BaseModel):
    chapter_uuid: str
    chapter_name: str
    comic_path_word: str
    pages: list[PageMeta]
    prev_chapter_uuid: str | None
    next_chapter_uuid: str | None


class ReaderPayload(BaseModel):
    """Continuous reader response — starting chapter + look-ahead chunks."""

    start_chapter_uuid: str
    chapters: list[ChapterPagesOut]


class BookmarkIn(BaseModel):
    series_path_word: str
    series_name: str


class BookmarkOut(BaseModel):
    id: uuid.UUID
    series_path_word: str
    series_name: str


class ProgressIn(BaseModel):
    chapter_uuid: str
    last_page: Optional[int] = None
    scroll_position: Optional[float] = None
    completed: bool = False


class ProgressOut(BaseModel):
    chapter_uuid: str
    last_page: Optional[int]
    scroll_position: Optional[float]
    completed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_absolute_cover(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http"):
        return url
    return f"{S3_PUBLIC_URL}/{url.lstrip('/')}" if S3_PUBLIC_URL else url


def _page_url(raw_url: str) -> str:
    """Return the page image URL, optionally rewriting to our proxy."""
    return raw_url  # extend here to add signed URLs or proxy rewriting


def _extract_chapters(raw_list: list[dict]) -> list[ChapterItem]:
    out: list[ChapterItem] = []
    for i, ch in enumerate(raw_list):
        out.append(
            ChapterItem(
                uuid=ch.get("uuid") or ch.get("id") or "",
                name=ch.get("name") or ch.get("title") or f"Chapter {i + 1}",
                index=ch.get("index", i),
                count=ch.get("count", 0),
            )
        )
    return out


async def _fetch_chapter_pages(path_word: str, chapter_uuid: str) -> ChapterPagesOut:
    data = await copymanga(f"comic/{path_word}/chapter/{chapter_uuid}")
    chapter_info: dict = data.get("chapter") or data
    pages_raw: list[dict] = data.get("contents") or []
    pages = [
        PageMeta(
            page_number=i + 1,
            url=_page_url(p.get("url") or p.get("img") or ""),
        )
        for i, p in enumerate(pages_raw)
    ]
    return ChapterPagesOut(
        chapter_uuid=chapter_uuid,
        chapter_name=chapter_info.get("name") or chapter_info.get("title") or chapter_uuid,
        comic_path_word=path_word,
        pages=pages,
        prev_chapter_uuid=chapter_info.get("prev_id") or chapter_info.get("prev") or None,
        next_chapter_uuid=chapter_info.get("next_id") or chapter_info.get("next") or None,
    )


# ---------------------------------------------------------------------------
# Routes — health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes — series / content (CopyManga-backed)
# ---------------------------------------------------------------------------


@app.get("/series", summary="List local library or search series on CopyManga")
async def search_series(
    q: Optional[str] = Query(
        None,
        min_length=1,
        description="Search query — omit to list the local library",
    ),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Any:
    """
    - **With** ``q``: proxy CopyManga search and return raw comic summaries.
    - **Without** ``q``: return series rows from the local PostgreSQL database
      (fields match :class:`LocalSeriesOut`).
    """
    if q:
        results = await copymanga("search/comic", q=q, limit=limit, offset=offset, platform=1)
        comics = results.get("list") or []
        for c in comics:
            c["cover"] = _ensure_absolute_cover(c.get("cover"))
        return {
            "total": results.get("total", len(comics)),
            "limit": limit,
            "offset": offset,
            "list": comics,
        }

    # Local library listing
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    db: Session = SessionLocal()
    try:
        total: int = db.scalar(select(func.count()).select_from(Series)) or 0
        rows = list(
            db.scalars(select(Series).order_by(Series.title).offset(offset).limit(limit)).all()
        )
    finally:
        db.close()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "list": [
            LocalSeriesOut(
                id=str(s.id),
                slug=s.slug,
                title=s.title,
                synopsis=s.synopsis,
                cover_url=_ensure_absolute_cover(s.cover_object_key),
                content_type=s.content_type.value
                if hasattr(s.content_type, "value")
                else s.content_type,
                status=s.status.value if hasattr(s.status, "value") else s.status,
                year=s.year,
                is_nsfw=s.is_nsfw,
            )
            for s in rows
        ],
    }


@app.get("/series/{path_word}", response_model=ComicOut, summary="Series detail")
async def get_series(path_word: str = Path(..., description="CopyManga path_word / slug")) -> ComicOut:
    data = await copymanga(f"comic/{path_word}")
    comic: dict = data.get("comic") or data
    return ComicOut(
        path_word=comic.get("path_word") or path_word,
        name=comic.get("name") or "",
        alias=comic.get("alias") or None,
        cover=_ensure_absolute_cover(comic.get("cover")),
        status=comic.get("status"),
        author=comic.get("author"),
        theme=comic.get("theme"),
        brief=comic.get("brief") or None,
        last_chapter=comic.get("last_chapter"),
    )


@app.get(
    "/series/{path_word}/chapters",
    response_model=ChapterListOut,
    summary="Paginated chapter list",
)
async def list_chapters(
    path_word: str = Path(...),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ChapterListOut:
    data = await copymanga(
        f"comic/{path_word}/group/default/chapters", limit=limit, offset=offset
    )
    raw_list: list[dict] = data.get("list") or []
    return ChapterListOut(
        total=data.get("total", len(raw_list)),
        limit=limit,
        offset=offset,
        list=_extract_chapters(raw_list),
    )


@app.get(
    "/series/{path_word}/chapter/{chapter_uuid}",
    response_model=ChapterPagesOut,
    summary="Chapter page metadata and image URLs",
)
async def get_chapter_pages(
    path_word: str = Path(...),
    chapter_uuid: str = Path(...),
) -> ChapterPagesOut:
    return await _fetch_chapter_pages(path_word, chapter_uuid)


@app.get(
    "/reader/{path_word}/chapter/{chapter_uuid}",
    response_model=ReaderPayload,
    summary="Continuous reader payload — starting chapter and look-ahead chunks",
)
async def get_reader_payload(
    path_word: str = Path(...),
    chapter_uuid: str = Path(...),
    look_ahead: int = Query(
        2, ge=0, le=5, alias="look_ahead",
        description="How many subsequent chapters to pre-fetch (0–5)",
    ),
) -> ReaderPayload:
    """
    Returns the requested chapter's pages **plus** up to ``look_ahead`` next
    chapters fetched sequentially from CopyManga (each chapter's UUID is
    discovered from the previous chapter's ``next_id`` pointer).
    """
    # Fetch starting chapter first to discover next pointers.
    start = await _fetch_chapter_pages(path_word, chapter_uuid)
    chunks: list[ChapterPagesOut] = [start]

    current = start
    for _ in range(look_ahead):
        nxt = current.next_chapter_uuid
        if not nxt:
            break
        current = await _fetch_chapter_pages(path_word, nxt)
        chunks.append(current)

    return ReaderPayload(start_chapter_uuid=chapter_uuid, chapters=chunks)


# ---------------------------------------------------------------------------
# Routes — bookmarks (local DB)
# ---------------------------------------------------------------------------


@app.get("/bookmarks", response_model=list[BookmarkOut], summary="List user bookmarks")
def list_bookmarks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: uuid.UUID = Depends(get_user_id),
    db: Session = Depends(get_db),
) -> list[BookmarkOut]:
    stmt = (
        select(Bookmark)
        .options(joinedload(Bookmark.series))
        .where(Bookmark.user_id == user_id)
        .order_by(Bookmark.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    bookmarks = list(db.scalars(stmt).all())
    return [
        BookmarkOut(
            id=bm.id,
            series_path_word=bm.series.slug if bm.series else "",
            series_name=bm.series.title if bm.series else "",
        )
        for bm in bookmarks
    ]


@app.post("/bookmarks", response_model=BookmarkOut, status_code=201, summary="Add bookmark")
def add_bookmark(
    body: BookmarkIn,
    user_id: uuid.UUID = Depends(get_user_id),
    db: Session = Depends(get_db),
) -> BookmarkOut:
    # Look up or create a local Series placeholder keyed by slug.
    series_row = db.scalar(select(Series).where(Series.slug == body.series_path_word))
    if series_row is None:
        series_row = Series(
            slug=body.series_path_word,
            title=body.series_name,
            content_type=ContentType.manga,
            default_reading_mode=ReadingMode.paged,
            status=SeriesStatus.ongoing,
        )
        db.add(series_row)
        db.flush()  # assign series_row.id without a full commit

    # Upsert — if bookmark exists return it.
    existing = db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user_id,
            Bookmark.series_id == series_row.id,
        )
    )
    if existing:
        return BookmarkOut(
            id=existing.id,
            series_path_word=series_row.slug,
            series_name=series_row.title,
        )

    bm = Bookmark(user_id=user_id, series_id=series_row.id)
    db.add(bm)
    db.commit()
    db.refresh(bm)
    return BookmarkOut(
        id=bm.id,
        series_path_word=series_row.slug,
        series_name=series_row.title,
    )


@app.delete("/bookmarks/{path_word}", status_code=204, summary="Remove bookmark")
def remove_bookmark(
    path_word: str = Path(...),
    user_id: uuid.UUID = Depends(get_user_id),
    db: Session = Depends(get_db),
):
    series_row = db.scalar(select(Series).where(Series.slug == path_word))
    if series_row is None:
        from fastapi.responses import Response
        return Response(status_code=204)

    bm = db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user_id,
            Bookmark.series_id == series_row.id,
        )
    )
    if bm:
        db.delete(bm)
        db.commit()
    from fastapi.responses import Response
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Routes — reading progress (local DB keyed by chapter UUID stored in chapter slug)
# ---------------------------------------------------------------------------


def _find_or_create_chapter(db: Session, chapter_uuid: str, path_word: str) -> Chapter:
    """Return existing Chapter row or create a thin placeholder keyed by UUID."""
    row = db.scalar(select(Chapter).where(Chapter.id == uuid.UUID(chapter_uuid)))
    if row:
        return row
    # Ensure Series placeholder exists.
    series_row = db.scalar(select(Series).where(Series.slug == path_word))
    if series_row is None:
        series_row = Series(
            slug=path_word,
            title=path_word,
            content_type=ContentType.manga,
            default_reading_mode=ReadingMode.continuous,
            status=SeriesStatus.ongoing,
        )
        db.add(series_row)
        db.flush()
    ch = Chapter(
        id=uuid.UUID(chapter_uuid),
        series_id=series_row.id,
        number=0.0,
        language="en",
    )
    db.add(ch)
    db.flush()
    return ch


@app.get(
    "/progress/{path_word}/{chapter_uuid}",
    response_model=ProgressOut,
    summary="Get reading progress for a chapter",
)
def get_progress(
    path_word: str = Path(...),
    chapter_uuid: str = Path(...),
    user_id: uuid.UUID = Depends(get_user_id),
    db: Session = Depends(get_db),
) -> ProgressOut:
    try:
        ch_id = uuid.UUID(chapter_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="chapter_uuid must be a valid UUID")
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user_id,
            ReadingProgress.chapter_id == ch_id,
        )
    )
    if row is None:
        return ProgressOut(
            chapter_uuid=chapter_uuid,
            last_page=None,
            scroll_position=None,
            completed=False,
        )
    return ProgressOut(
        chapter_uuid=chapter_uuid,
        last_page=row.last_page,
        scroll_position=row.scroll_position,
        completed=row.completed,
    )


@app.post(
    "/progress/{path_word}/{chapter_uuid}",
    response_model=ProgressOut,
    summary="Upsert reading progress for a chapter",
)
def upsert_progress(
    body: ProgressIn,
    path_word: str = Path(...),
    chapter_uuid: str = Path(...),
    user_id: uuid.UUID = Depends(get_user_id),
    db: Session = Depends(get_db),
) -> ProgressOut:
    try:
        ch_id = uuid.UUID(chapter_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="chapter_uuid must be a valid UUID")

    _find_or_create_chapter(db, chapter_uuid, path_word)

    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user_id,
            ReadingProgress.chapter_id == ch_id,
        )
    )
    if row is None:
        row = ReadingProgress(
            user_id=user_id,
            chapter_id=ch_id,
            last_page=body.last_page,
            scroll_position=body.scroll_position,
            completed=body.completed,
        )
        db.add(row)
    else:
        if body.last_page is not None:
            row.last_page = body.last_page
        if body.scroll_position is not None:
            row.scroll_position = body.scroll_position
        row.completed = body.completed

    db.commit()
    db.refresh(row)
    return ProgressOut(
        chapter_uuid=chapter_uuid,
        last_page=row.last_page,
        scroll_position=row.scroll_position,
        completed=row.completed,
    )
