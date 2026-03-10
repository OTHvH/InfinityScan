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
    User,
    UserRole,
)

# Auth imports
from datetime import datetime, timedelta
import jwt
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from slowapi import Limiter
from slowapi.util import get_remote_address

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/token")

# JWT configuration
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable must be set in production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

# Rate limiter for auth endpoints
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


def create_access_token(user_id: uuid.UUID) -> str:
    """Create a JWT access token with expiration."""
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    to_encode = {"sub": str(user_id), "exp": expire}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> uuid.UUID | None:
    """Verify a JWT token and return the user ID if valid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            return None
        return uuid.UUID(user_id)
    except (jwt.PyJWTError, ValueError, AttributeError):
        return None

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


class LocalChapterOut(BaseModel):
    """Local-library chapter row."""

    id: str
    number: float
    title: str | None
    page_count: int
    published_at: str | None


class LocalSeriesDetailOut(LocalSeriesOut):
    """Local-library series with chapters."""

    chapters: list[LocalChapterOut] = []


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


# ---------------------------------------------------------------------------# Auth schemas# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    email: Optional[str]
    role: str
    is_active: bool


class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserOut


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


# ---------------------------------------------------------------------------# Routes — authentication# ---------------------------------------------------------------------------


@app.post("/register", response_model=UserOut, summary="Register new user")
@limiter.limit("5/minute")
def register(
    user_data: UserCreate,
    db: Session = Depends(get_db),
) -> UserOut:
    # Validate password
    if len(user_data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    
    # Check if username exists
    existing = db.scalar(select(User).where(User.username == user_data.username))
    if existing:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    # Check if email exists
    if user_data.email:
        existing_email = db.scalar(select(User).where(User.email == user_data.email))
        if existing_email:
            raise HTTPException(status_code=400, detail="Email already registered")
    
    # Create user
    hashed_password = pwd_context.hash(user_data.password)
    user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hashed_password,
        role=UserRole.user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
    )


@app.post("/token", response_model=Token, summary="Login and get token")
@limiter.limit("10/minute")
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    user = db.scalar(select(User).where(User.username == form_data.username))
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")
    
    # Generate JWT token with expiration
    access_token = create_access_token(user.id)
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=UserOut(
            id=str(user.id),
            username=user.username,
            email=user.email,
            role=user.role.value,
            is_active=user.is_active,
        ),
    )


@app.get("/me", response_model=UserOut, summary="Get current user")
def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> UserOut:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    token = authorization.replace("Bearer ", "")
    
    # Verify JWT token
    user_id = verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    # Query using string representation (UUIDs are stored as strings in SQLite)
    user = db.scalar(select(User).where(User.id == str(user_id)))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return UserOut(
        id=str(user.id),
        username=user.username,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
    )


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


@app.get("/library/{slug}", response_model=LocalSeriesDetailOut, summary="Get local series by slug")
def get_local_series(
    slug: str,
    db: Session = Depends(get_db),
) -> LocalSeriesDetailOut:
    """Get a local series by slug with its chapters."""
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
    """Get pages for a local chapter by series slug and chapter number."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    
    API_BASE = os.environ.get("NEXT_PUBLIC_API_URL", "http://localhost:8000")
    
    series = db.scalar(select(Series).where(Series.slug == slug))
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    
    chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number == number
        )
    )
    if not chapter:
        raise HTTPException(status_code=404, detail="Chapter not found")
    
    pages = list(db.scalars(
        select(Page).where(Page.chapter_id == chapter.id).order_by(Page.page_number)
    ).all())
    
    # Get prev/next chapters
    prev_chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number < number
        ).order_by(Chapter.number.desc()).limit(1)
    )
    next_chapter = db.scalar(
        select(Chapter).where(
            Chapter.series_id == series.id,
            Chapter.number > number
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
    """Redirect to local page image."""
    from pathlib import Path
    from fastapi.responses import FileResponse
    
    try:
        page_uuid = uuid.UUID(page_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid page ID")
    
    page = db.scalar(select(Page).where(Page.id == page_uuid))
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    
    # For local files, we serve from the filesystem
    # The object_key contains the path relative to the manga folder
    # Configure via MANGA_LOCAL_PATH environment variable
    manga_base = Path(os.environ.get("MANGA_LOCAL_PATH", ""))
    if not manga_base:
        raise HTTPException(status_code=500, detail="MANGA_LOCAL_PATH not configured")
    object_key = page.object_key
    
    # Parse the object key to get the actual file path
    # Format: "the-regressor-can-make-them-all/{chapter_num}/{filename}"
    parts = object_key.split("/")
    if len(parts) >= 3:
        # The filename contains the full path structure
        file_path = manga_base / parts[0] / parts[1] / parts[2]
    else:
        # Try to find the file directly
        file_path = manga_base / object_key
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Image file not found")
    
    # Determine content type
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
