"""
InfinityScan API
================
Content is sourced from the CopyManga public API (https://github.com/fumiama/copymanga).
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
COPYMANGA_API      https://api.copymanga.tv  (default)
COPYMANGA_TOKEN    optional bearer token for CopyManga (raises rate-limits)
S3_PUBLIC_URL      https://cdn.example.com  (prefix used when building cover URLs)
CORS_ORIGINS       comma-separated list of allowed origins (default: http://localhost:3000)
"""

import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from settings import get_settings
from database import get_db
from models import (
    Bookmark,
    Chapter,
    ContentType,
    Page,
    ReadingMode,
    ReadingProgress,
    RefreshSession,
    RefreshToken,
    Series,
    SeriesStatus,
    User,
    UserRole,
)
from auth import (
    create_access_token,
    decode_access_token,
    generate_csrf_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    set_cookie,
    clear_cookie,
    verify_and_update_password,
)
from auth.schemas import (
    LoginIn,
    LoginOut,
    RegisterIn,
    RegisterOut,
    UserOut,
)
from session import issue_access_token, issue_refresh_session, rotate_refresh_token
from deps import get_current_user, require_csrf, require_role
from schemas import (
    BookmarkIn,
    BookmarkOut,
    ChapterItem,
    ChapterListOut,
    ChapterPagesOut,
    ComicOut,
    LocalChapterOut,
    LocalSeriesDetailOut,
    LocalSeriesOut,
    PageMeta,
    ProgressIn,
    ProgressOut,
    ReaderPayload,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_cfg = get_settings()

COPYMANGA_API: str = _cfg.copymanga_api
S3_PUBLIC_URL: str = _cfg.s3_public_url

_COPYMANGA_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (compatible; InfinityScan/1.0)",
    "Accept": "application/json",
}
if _cfg.copymanga_token:
    _COPYMANGA_HEADERS["Authorization"] = f"Token {_cfg.copymanga_token}"

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

# Rate limiter
limiter = Limiter(key_func=get_remote_address)
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

app.include_router(auth_router)


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
    return raw_url


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


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str, session_id: uuid.UUID) -> None:
    """Set access, refresh, and session-bound CSRF cookies on *response*."""
    cfg = get_settings()
    set_cookie(response, cfg.access_cookie_name, access_token,
               max_age=cfg.access_token_ttl_minutes * 60)
    set_cookie(response, cfg.refresh_cookie_name, refresh_token,
               max_age=cfg.refresh_token_ttl_days * 86400)
    csrf = generate_csrf_token(session_id)
    set_cookie(response, cfg.csrf_cookie_name, csrf,
               max_age=cfg.csrf_token_ttl_seconds,
               http_only=False)


def _clear_auth_cookies(response: Response) -> None:
    cfg = get_settings()
    clear_cookie(response, cfg.access_cookie_name)
    clear_cookie(response, cfg.refresh_cookie_name)
    clear_cookie(response, cfg.csrf_cookie_name)


def _user_to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


def _issue_refresh_token(db: Session, user: User) -> str:
    """Generate, hash, and store a refresh token.  Returns the raw token."""
    raw = generate_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_token_ttl_days),
    ))
    db.commit()
    return raw


def _revoke_all_refresh_tokens(db: Session, user: User) -> None:
    from sqlalchemy import update
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked == False)  # noqa: E712
        .values(revoked=True)
    )
    db.commit()


# ---------------------------------------------------------------------------
# Routes — health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routes — deprecated legacy authentication (use /auth/* instead)
# ---------------------------------------------------------------------------


@app.post(
    "/register",
    response_model=RegisterOut,
    summary="[DEPRECATED] Use POST /auth/register instead",
    include_in_schema=True,
)
@limiter.limit(_cfg.register_rate_limit)
def register(
    request: Request,
    body: RegisterIn = Body(...),
    db: Session = Depends(get_db),
) -> RegisterOut:
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=400, detail="Username already registered")

    if body.email and db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=400, detail="Email already registered")

    try:
        hashed = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hashed,
        role=UserRole.user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return RegisterOut(user=_user_to_out(user))


@app.post(
    "/token",
    response_model=LoginOut,
    summary="[DEPRECATED] Use POST /auth/login instead",
    include_in_schema=True,
)
@limiter.limit(_cfg.login_rate_limit)
def login(
    request: Request,
    response: Response,
    body: LoginIn = Body(...),
    db: Session = Depends(get_db),
) -> LoginOut:
    user = db.scalar(select(User).where(User.username == body.username))
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    success, new_hash = verify_and_update_password(body.password, user.hashed_password)
    if not success:
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    if new_hash is not None:
        user.hashed_password = new_hash
        db.commit()

    user_agent = request.headers.get("User-Agent", "")
    raw_refresh, session_id = issue_refresh_session(db, user_id=user.id, user_agent=user_agent)
    access = issue_access_token(user.id, session_id, user.role.value)

    _set_auth_cookies(response, access, raw_refresh, session_id)
    return LoginOut(user=_user_to_out(user))


@app.post(
    "/logout",
    summary="[DEPRECATED] Use POST /auth/logout instead",
    include_in_schema=True,
)
@limiter.limit(_cfg.logout_rate_limit)
def logout(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    raw_refresh: str | None = request.cookies.get(_cfg.refresh_cookie_name)
    if raw_refresh:
        token_hash = hash_refresh_token(raw_refresh)
        session = db.scalar(
            select(RefreshSession).where(RefreshSession.token_hash == token_hash)
        )
        if session and session.revoked_at is None:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()

    _clear_auth_cookies(response)
    return {"detail": "Logged out"}


@app.post(
    "/refresh",
    summary="[DEPRECATED] Use POST /auth/refresh instead",
    include_in_schema=True,
)
@limiter.limit(_cfg.refresh_rate_limit)
def refresh_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    raw_refresh: str | None = request.cookies.get(_cfg.refresh_cookie_name)
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="No refresh token")

    user_agent = request.headers.get("User-Agent", "")
    result = rotate_refresh_token(db, raw_refresh, user_agent=user_agent)

    if result is None:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    new_raw, new_session_id = result
    token_hash = hash_refresh_token(raw_refresh)
    old_session = db.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == token_hash)
    )
    user = db.scalar(select(User).where(User.id == old_session.user_id))
    if not user or not user.is_active:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="User not found or disabled")

    access = issue_access_token(user.id, new_session_id, user.role.value)
    _set_auth_cookies(response, access, new_raw, new_session_id)
    return {"detail": "Token refreshed"}


@app.get(
    "/me",
    response_model=UserOut,
    summary="[DEPRECATED] Use GET /auth/me instead",
    include_in_schema=True,
)
def get_me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_to_out(user)


# ---------------------------------------------------------------------------
# Routes — series / content (CopyManga-backed)
# ---------------------------------------------------------------------------


@app.get("/series", summary="List local library or search series on CopyManga")
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

    from database import get_db
    db_gen = get_db()
    db: Session = next(db_gen)
    try:
        total: int = db.scalar(select(func.count()).select_from(Series)) or 0
        rows = list(
            db.scalars(select(Series).order_by(Series.title).offset(offset).limit(limit)).all()
        )
    finally:
        try:
            next(db_gen)
        except StopIteration:
            pass

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


@app.get("/library/{slug}", response_model=LocalSeriesDetailOut, summary="Get local series by slug")
def get_local_series(
    slug: str,
    db: Session = Depends(get_db),
) -> LocalSeriesDetailOut:
    series = db.scalar(select(Series).where(Series.slug == slug))
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    chapters = list(db.scalars(
        select(Chapter).where(Chapter.series_id == series.id).order_by(Chapter.number)
    ).all())

    return LocalSeriesDetailOut(
        id=str(series.id),
        slug=series.slug,
        title=series.title,
        synopsis=series.synopsis,
        cover_url=_ensure_absolute_cover(series.cover_object_key),
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
    summary="Get local chapter pages",
)
def get_local_chapter_pages(
    slug: str,
    number: float,
    db: Session = Depends(get_db),
) -> ChapterPagesOut:
    API_BASE = os.environ.get("NEXT_PUBLIC_API_URL", "http://localhost:8000")

    series = db.scalar(select(Series).where(Series.slug == slug))
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")

    chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number == number,
        )
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")

    pages = list(db.scalars(
        select(Page).where(Page.chapter_id == chapter.id).order_by(Page.page_number)
    ).all())

    prev_chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number < number,
        ).order_by(Chapter.number.desc()).limit(1)
    )
    next_chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number > number,
        ).order_by(Chapter.number).limit(1)
    )

    return ChapterPagesOut(
        chapter_uuid=str(chapter.id),
        chapter_name=chapter.title or f"Chapter {chapter.number}",
        comic_path_word=slug,
        pages=[
            PageMeta(
                page_number=p.page_number,
                url=f"{API_BASE}/library/{slug}/page/{p.id}",
            )
            for p in pages
        ],
        prev_chapter_uuid=str(prev_chapter.id) if prev_chapter else None,
        next_chapter_uuid=str(next_chapter.id) if next_chapter else None,
    )


@app.get(
    "/library/{slug}/page/{page_id}",
    summary="Get local page image",
)
def get_local_page(
    slug: str,
    page_id: str,
    db: Session = Depends(get_db),
):
    from pathlib import Path
    from fastapi.responses import FileResponse

    try:
        page_uuid = uuid.UUID(page_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid page ID")

    page = db.scalar(select(Page).where(Page.id == page_uuid))
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    manga_base = Path(os.environ.get("MANGA_LOCAL_PATH", ""))
    if not manga_base:
        raise HTTPException(status_code=500, detail="MANGA_LOCAL_PATH not configured")
    object_key = page.object_key

    parts = object_key.split("/")
    if len(parts) >= 3:
        file_path = manga_base / parts[0] / parts[1] / parts[2]
    else:
        file_path = manga_base / object_key

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found")

    content_type = "image/jpeg"
    if file_path.suffix.lower() == ".png":
        content_type = "image/png"
    elif file_path.suffix.lower() == ".webp":
        content_type = "image/webp"

    return FileResponse(file_path, media_type=content_type)


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
    )
