"""
InfinityScan API
================
User state (bookmarks, reading progress) is persisted in the local PostgreSQL database.

User identity
-------------
Authentication is cookie-based.  On login the server sets:
  • ``is_access``  — short-lived httpOnly JWT cookie
  • ``is_refresh`` — long-lived httpOnly opaque cookie (rotated on each use)
  • ``is_csrf``    — non-httpOnly cookie read by the frontend and sent back as
                     the ``X-CSRF-Token`` header on state-changing requests.

All identity derives from the session cookies.  No identity headers are accepted.

Environment variables
---------------------
DATABASE_URL       postgresql+psycopg://user:pass@host:5432/db
SECRET_KEY         HMAC signing key for JWTs (auto-generated if omitted)
CORS_ORIGINS       comma-separated list of allowed origins (default: http://localhost:3000)
"""

import sys
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from limiter import limiter

from settings import get_settings
from database import get_db
from storage import ObjectStorage, StorageError, create_object_storage
from providers.base import ProviderError, ProviderSecurityError, ProviderTimeout
from providers.copymanga import CopyMangaAdapter
from providers.local import LocalContentAdapter
from models import (
    Bookmark,
    Chapter,
    ChapterImportStatus,
    ContentType,
    Page,
    PageIntegrityStatus,
    ReadingMode,
    ReadingProgress,
    RefreshSession,
    Series,
    SeriesStatus,
    User,
    UserRole,
)
from deps import get_current_user, require_admin, require_csrf
from schemas import (
    BookmarkIn,
    BookmarkOut,
    ChapterPagesOut,
    LocalChapterOut,
    LocalSeriesDetailOut,
    LocalSeriesOut,
    PageMeta,
    ProgressIn,
    ProgressOut,
    ProviderChapterItemOut,
    ProviderChapterListOut,
    ProviderSeriesListOut,
    ProviderSeriesOut,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_cfg = get_settings()

S3_PUBLIC_URL: str = _cfg.s3_public_url
_media_storage: ObjectStorage | None = create_object_storage(_cfg)

# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------

_copymanga = CopyMangaAdapter(
    _cfg.copymanga_api,
    token=_cfg.copymanga_token,
    enabled=_cfg.copymanga_enabled,
    timeout=_cfg.copymanga_timeout,
    max_response_bytes=_cfg.copymanga_max_response_bytes,
    max_redirects=_cfg.copymanga_max_redirects,
    cache_ttl=_cfg.copymanga_cache_ttl,
)

_local_root = _cfg.local_content_root
_local = LocalContentAdapter(
    _local_root if _local_root else __import__("pathlib").Path("/nonexistent"),
    enabled=_cfg.local_content_enabled and bool(_local_root),
)

# ---------------------------------------------------------------------------
# Database (imported from database.py)
# ---------------------------------------------------------------------------
# SessionLocal is now accessed via database module

# ---------------------------------------------------------------------------
# HTTP client lifecycle
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: Any):
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    )
    yield
    await _copymanga.close()
    await _http_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="InfinityScan API", version="0.2.0", lifespan=lifespan)

# Rate limiter
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


app.add_middleware(SlowAPIMiddleware)

# Explicit CORS — no wildcard methods/headers, credentials allowed
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cfg.cors_origins,
    allow_methods=_cfg.cors_allow_methods,
    allow_headers=_cfg.cors_allow_headers,
    allow_credentials=True,
)

# Trusted hosts
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_cfg.trusted_hosts)

# Custom security middleware
from middleware import OriginValidationMiddleware, RequestBodyLimitMiddleware, NoCacheAuthMiddleware

app.add_middleware(NoCacheAuthMiddleware)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(OriginValidationMiddleware)


# ---------------------------------------------------------------------------
# Mount canonical /auth/* router
# ---------------------------------------------------------------------------

from routes.auth import router as auth_router
from routes.reader import router as reader_router

app.include_router(auth_router)
app.include_router(reader_router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_absolute_cover(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http"):
        return url
    return f"{S3_PUBLIC_URL}/{url.lstrip('/')}" if S3_PUBLIC_URL else url


def _local_cover_url(series_id: uuid.UUID, object_key: str | None) -> str | None:
    if not object_key:
        return None
    return f"/media/covers/{series_id}"


def _parse_media_uuid(raw_id: str, resource: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{resource} not found")


def _mark_page_missing(db: Session, page: Page) -> None:
    """Persist a missing-object signal without exposing storage internals."""
    page.integrity_status = PageIntegrityStatus.missing
    page.verified_at = None
    try:
        db.commit()
    except Exception:
        db.rollback()


def _presigned_redirect(
    db: Session,
    *,
    object_key: str,
    page: Page | None = None,
) -> RedirectResponse:
    """Return a short-lived private-storage redirect or a generic 404."""
    if _media_storage is None:
        raise HTTPException(status_code=404, detail="Media not found")

    try:
        metadata = _media_storage.head_object(object_key)
    except StorageError:
        raise HTTPException(status_code=404, detail="Media not found")

    if metadata is None:
        if page is not None:
            _mark_page_missing(db, page)
        raise HTTPException(status_code=404, detail="Media not found")

    try:
        url = _media_storage.generate_presigned_get(
            object_key,
            expires_in=_cfg.s3_presign_ttl_seconds,
        )
    except (StorageError, ValueError):
        raise HTTPException(status_code=404, detail="Media not found")
    return RedirectResponse(url=url, status_code=307)


# ---------------------------------------------------------------------------
# Routes — health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/library/{slug}", response_model=LocalSeriesDetailOut, summary="Get local series by slug")
def get_local_series(
    slug: str,
    db: Session = Depends(get_db),
) -> LocalSeriesDetailOut:
    series = db.scalar(select(Series).where(Series.slug == slug))
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    chapters = list(db.scalars(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.import_status == ChapterImportStatus.ready,
        ).order_by(Chapter.number)
    ).all())

    return LocalSeriesDetailOut(
        id=str(series.id),
        slug=series.slug,
        title=series.title,
        synopsis=series.synopsis,
        cover_url=_local_cover_url(series.id, series.cover_object_key),
        content_type=series.content_type.value if hasattr(series.content_type, "value") else series.content_type,
        status=series.status.value if hasattr(series.status, "value") else series.status,
        year=series.year,
        is_nsfw=series.is_nsfw,
        chapters=[
            LocalChapterOut(
                id=str(c.id),
                number=c.number,
                title=c.title,
                page_count=c.page_count,
                published_at=c.published_at.isoformat() if c.published_at else None,
            )
            for c in chapters
        ],
    )


@app.get(
    "/library/{slug}/chapter/{number}",
    response_model=ChapterPagesOut,
    summary="Get local chapter pages by number (deprecated)",
    description=(
        "Deprecated compatibility route. New clients must use "
        "GET /reader/{series_slug}/chunks with UUID chapter identities and opaque cursors."
    ),
    deprecated=True,
)
def get_local_chapter_pages(
    slug: str,
    number: Decimal,
    db: Session = Depends(get_db),
) -> ChapterPagesOut:
    series = db.scalar(select(Series).where(Series.slug == slug))
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number == number,
            Chapter.import_status == ChapterImportStatus.ready,
        )
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    pages = list(db.scalars(
        select(Page).where(
            Page.chapter_id == chapter.id,
            Page.integrity_status == PageIntegrityStatus.verified,
        ).order_by(Page.page_number)
    ).all())

    prev_chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number < number,
            Chapter.import_status == ChapterImportStatus.ready,
        ).order_by(Chapter.number.desc()).limit(1)
    )
    next_chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number > number,
            Chapter.import_status == ChapterImportStatus.ready,
        ).order_by(Chapter.number).limit(1)
    )

    return ChapterPagesOut(
        chapter_uuid=str(chapter.id),
        chapter_name=chapter.title or f"Chapter {chapter.number}",
        comic_path_word=slug,
        pages=[
            PageMeta(
                page_number=p.page_number,
                url=f"/media/pages/{p.id}",
            )
            for p in pages
        ],
        prev_chapter_uuid=str(prev_chapter.id) if prev_chapter else None,
        next_chapter_uuid=str(next_chapter.id) if next_chapter else None,
    )


@app.get("/media/pages/{page_id}", summary="Redirect to a private page object")
def get_media_page(
    page_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    page_uuid = _parse_media_uuid(page_id, "Page")
    page = db.scalar(
        select(Page)
        .join(Page.chapter)
        .where(
            Page.id == page_uuid,
            Chapter.import_status == ChapterImportStatus.ready,
            Page.integrity_status == PageIntegrityStatus.verified,
        )
    )
    if page is None or not page.object_key:
        raise HTTPException(status_code=404, detail="Page not found")
    return _presigned_redirect(db, object_key=page.object_key, page=page)


@app.get("/media/covers/{series_id}", summary="Redirect to a private cover object")
def get_media_cover(
    series_id: str,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    series_uuid = _parse_media_uuid(series_id, "Series")
    series = db.scalar(select(Series).where(Series.id == series_uuid))
    if series is None or not series.cover_object_key:
        raise HTTPException(status_code=404, detail="Cover not found")

    has_ready_chapter = db.scalar(
        select(Chapter.id)
        .where(
            Chapter.series_id == series.id,
            Chapter.import_status == ChapterImportStatus.ready,
        )
        .limit(1)
    )
    if has_ready_chapter is None:
        raise HTTPException(status_code=404, detail="Cover not found")

    return _presigned_redirect(db, object_key=series.cover_object_key)


# ---------------------------------------------------------------------------
# Routes — external provider content (adapter-backed)
# ---------------------------------------------------------------------------


@app.get("/series", summary="Search provider series or list local library")
async def search_series(
    q: str | None = Query(
        None,
        min_length=1,
        description="Search query — omit to list the local library",
    ),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Any:
    if q:
        if not _copymanga.enabled:
            raise HTTPException(status_code=404, detail="Search provider is not enabled")
        try:
            results = await _copymanga.search_series(q, limit=limit, offset=offset)
        except ProviderTimeout:
            raise HTTPException(status_code=504, detail="Search provider timed out")
        except ProviderSecurityError as exc:
            raise HTTPException(status_code=422, detail=f"Search provider URL rejected: {exc}")
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=f"Search provider error: {exc}")
        return ProviderSeriesListOut(
            total=len(results) + offset,
            limit=limit,
            offset=offset,
            list=[
                ProviderSeriesOut(
                    external_id=s.external_id,
                    title=s.title,
                    description=s.description,
                    cover_url=_ensure_absolute_cover(s.cover_url),
                    status=s.status,
                    content_type=s.content_type,
                    year=s.year,
                    tags=list(s.tags),
                    authors=list(s.authors),
                )
                for s in results
            ],
        )

    db: Session = next(get_db())
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
                cover_url=_local_cover_url(s.id, s.cover_object_key),
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


@app.get("/series/{path_word}", summary="Series detail from provider")
async def get_series(path_word: str = Path(...)) -> ProviderSeriesOut:
    if not _copymanga.enabled:
        raise HTTPException(status_code=404, detail="Provider is not enabled")
    try:
        series = await _copymanga.get_series(path_word)
    except ProviderTimeout:
        raise HTTPException(status_code=504, detail="Provider timed out")
    except ProviderSecurityError as exc:
        raise HTTPException(status_code=422, detail=f"Provider URL rejected: {exc}")
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"Provider error: {exc}")
    return ProviderSeriesOut(
        external_id=series.external_id,
        title=series.title,
        description=series.description,
        cover_url=_ensure_absolute_cover(series.cover_url),
        status=series.status,
        content_type=series.content_type,
        year=series.year,
        tags=list(series.tags),
        authors=list(series.authors),
    )


@app.get(
    "/series/{path_word}/chapters",
    summary="Paginated chapter list from provider",
)
async def list_chapters(
    path_word: str = Path(...),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ProviderChapterListOut:
    if not _copymanga.enabled:
        raise HTTPException(status_code=404, detail="Provider is not enabled")
    try:
        chapters = await _copymanga.list_chapters(path_word, limit=limit, offset=offset)
    except ProviderTimeout:
        raise HTTPException(status_code=504, detail="Provider timed out")
    except ProviderSecurityError as exc:
        raise HTTPException(status_code=422, detail=f"Provider URL rejected: {exc}")
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"Provider error: {exc}")
    return ProviderChapterListOut(
        total=len(chapters) + offset,
        limit=limit,
        offset=offset,
        list=[
            ProviderChapterItemOut(
                external_id=ch.external_id,
                number=ch.number,
                title=ch.title,
                volume=ch.volume,
                language=ch.language,
                page_count=ch.page_count,
                published_at=ch.published_at,
            )
            for ch in chapters
        ],
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
    if not _copymanga.enabled:
        raise HTTPException(status_code=404, detail="Provider is not enabled")
    try:
        page_refs = await _copymanga.get_chapter_pages(path_word, chapter_uuid)
    except ProviderTimeout:
        raise HTTPException(status_code=504, detail="Provider timed out")
    except ProviderSecurityError as exc:
        raise HTTPException(status_code=422, detail=f"Provider URL rejected: {exc}")
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=f"Provider error: {exc}")
    return ChapterPagesOut(
        chapter_uuid=chapter_uuid,
        chapter_name=chapter_uuid,
        comic_path_word=path_word,
        pages=[
            PageMeta(page_number=ref.page_number, url=ref.url)
            for ref in page_refs
        ],
        prev_chapter_uuid=None,
        next_chapter_uuid=None,
    )


# ---------------------------------------------------------------------------
# Routes — bookmarks (local DB, user-isolated)
# ---------------------------------------------------------------------------


@app.get("/bookmarks", response_model=list[BookmarkOut], summary="List user bookmarks")
def list_bookmarks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BookmarkOut]:
    stmt = (
        select(Bookmark)
        .options(joinedload(Bookmark.series))
        .where(Bookmark.user_id == user.id)
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
@limiter.limit(_cfg.bookmark_write_rate_limit)
def add_bookmark(
    request: Request,
    body: BookmarkIn = Body(...),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> BookmarkOut:
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
        db.flush()

    existing = db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.series_id == series_row.id,
        )
    )
    if existing:
        return BookmarkOut(
            id=existing.id,
            series_path_word=series_row.slug,
            series_name=series_row.title,
        )

    bm = Bookmark(user_id=user.id, series_id=series_row.id)
    db.add(bm)
    db.commit()
    db.refresh(bm)
    return BookmarkOut(
        id=bm.id,
        series_path_word=series_row.slug,
        series_name=series_row.title,
    )


@app.delete("/bookmarks/{path_word}", status_code=204, summary="Remove bookmark")
@limiter.limit(_cfg.bookmark_write_rate_limit)
def remove_bookmark(
    request: Request,
    path_word: str = Path(...),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    from fastapi.responses import Response

    series_row = db.scalar(select(Series).where(Series.slug == path_word))
    if series_row is None:
        return Response(status_code=204)

    bm = db.scalar(
        select(Bookmark).where(
            Bookmark.user_id == user.id,
            Bookmark.series_id == series_row.id,
        )
    )
    if bm:
        db.delete(bm)
        db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Routes — reading progress (local DB, user-isolated)
# ---------------------------------------------------------------------------


def _find_or_create_chapter(db: Session, chapter_uuid: str, path_word: str) -> Chapter:
    row = db.scalar(select(Chapter).where(Chapter.id == uuid.UUID(chapter_uuid)))
    if row:
        return row
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
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProgressOut:
    try:
        ch_id = uuid.UUID(chapter_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="chapter_uuid must be a valid UUID")
    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user.id,
            ReadingProgress.chapter_id == ch_id,
        )
    )
    if row is None:
        return ProgressOut(
            chapter_uuid=chapter_uuid,
            last_page=None,
            scroll_position=None,
            completed=False,
            updated_at=None,
        )
    return ProgressOut(
        chapter_uuid=chapter_uuid,
        last_page=row.last_page,
        scroll_position=row.scroll_position,
        completed=row.completed,
        updated_at=row.updated_at,
    )


@app.post(
    "/progress/{path_word}/{chapter_uuid}",
    response_model=ProgressOut,
    summary="Upsert reading progress for a chapter",
)
@limiter.limit(_cfg.progress_write_rate_limit)
def upsert_progress(
    request: Request,
    body: ProgressIn = Body(...),
    path_word: str = Path(...),
    chapter_uuid: str = Path(...),
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ProgressOut:
    try:
        ch_id = uuid.UUID(chapter_uuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="chapter_uuid must be a valid UUID")

    _find_or_create_chapter(db, chapter_uuid, path_word)

    row = db.scalar(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user.id,
            ReadingProgress.chapter_id == ch_id,
        )
    )
    if row is None:
        row = ReadingProgress(
            user_id=user.id,
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
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# Routes — admin (require_admin)
# ---------------------------------------------------------------------------


@app.get("/admin/import-jobs", summary="List import jobs (admin only)")
def admin_list_import_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    from tools.integrity import list_import_jobs
    return list_import_jobs(db, limit=limit, offset=offset)


@app.get("/admin/import-jobs/{job_id}", summary="Get import job detail (admin only)")
def admin_get_import_job(
    job_id: str = Path(...),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    from tools.integrity import get_import_job
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID")
    job = get_import_job(db, jid)
    if job is None:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


@app.get("/admin/integrity/summary", summary="Run integrity checks (admin only)")
def admin_integrity_summary(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    from tools.integrity import run_checks
    from storage import create_object_storage
    storage = create_object_storage()
    summary = run_checks(db, storage)
    return summary.to_dict()
