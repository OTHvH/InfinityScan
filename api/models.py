"""SQLAlchemy ORM models for InfinityScan."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    JSON,
    Integer,
    BigInteger,
    Numeric,
    SmallInteger,
    String,
    Table,
    Column,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ContentType(str, enum.Enum):
    manga = "manga"
    manhua = "manhua"
    manhwa = "manhwa"


class ReadingMode(str, enum.Enum):
    paged = "paged"
    continuous = "continuous"


class SeriesStatus(str, enum.Enum):
    ongoing = "ongoing"
    completed = "completed"
    hiatus = "hiatus"
    cancelled = "cancelled"


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class ChapterImportStatus(str, enum.Enum):
    importing = "importing"
    ready = "ready"
    failed = "failed"
    quarantined = "quarantined"


class PageIntegrityStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    missing = "missing"
    mismatch = "mismatch"
    quarantined = "quarantined"


class ImportJobStatus(str, enum.Enum):
    pending = "pending"
    scanning = "scanning"
    uploading = "uploading"
    verifying = "verifying"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class ImportJobItemStatus(str, enum.Enum):
    pending = "pending"
    uploading = "uploading"
    verifying = "verifying"
    succeeded = "succeeded"
    skipped = "skipped"
    failed = "failed"
    quarantined = "quarantined"


# ---------------------------------------------------------------------------
# Association tables (many-to-many)
# ---------------------------------------------------------------------------

series_authors = Table(
    "series_authors",
    Base.metadata,
    Column("series_id", Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("author_id", Uuid(as_uuid=True), ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True),
)

series_artists = Table(
    "series_artists",
    Base.metadata,
    Column("series_id", Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("artist_id", Uuid(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True),
)

series_tags = Table(
    "series_tags",
    Base.metadata,
    Column("series_id", Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Uuid(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------------------
# Lookup / taxonomy tables
# ---------------------------------------------------------------------------

class Author(Base):
    __tablename__ = "authors"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[list[Series]] = relationship("Series", secondary=series_authors, back_populates="authors")


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[list[Series]] = relationship("Series", secondary=series_artists, back_populates="artists")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    series: Mapped[list[Series]] = relationship("Series", secondary=series_tags, back_populates="tags")


# ---------------------------------------------------------------------------
# User accounts (defined before bookmarks / reading_progress so FK targets exist)
# ---------------------------------------------------------------------------

class User(Base):
    """User accounts for authentication."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role_enum"), nullable=False, default=UserRole.user
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    bookmarks: Mapped[list[Bookmark]] = relationship("Bookmark", back_populates="user", cascade="all, delete-orphan")
    reading_progress: Mapped[list[ReadingProgress]] = relationship(
        "ReadingProgress", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_sessions: Mapped[list[RefreshSession]] = relationship(
        "RefreshSession", back_populates="user", cascade="all, delete-orphan"
    )
    import_jobs: Mapped[list[ImportJob]] = relationship("ImportJob", back_populates="requested_by_user")


# ---------------------------------------------------------------------------
# Core content tables
# ---------------------------------------------------------------------------

class Series(Base):
    """A manga / manhua / manhwa series."""

    __tablename__ = "series"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    alternative_titles: Mapped[str | None] = mapped_column(Text)
    synopsis: Mapped[str | None] = mapped_column(Text)
    cover_object_key: Mapped[str | None] = mapped_column(String(1024))
    content_type: Mapped[ContentType] = mapped_column(
        Enum(ContentType, name="content_type_enum"), nullable=False
    )
    default_reading_mode: Mapped[ReadingMode] = mapped_column(
        Enum(ReadingMode, name="reading_mode_enum"),
        nullable=False,
        default=ReadingMode.paged,
    )
    status: Mapped[SeriesStatus] = mapped_column(
        Enum(SeriesStatus, name="series_status_enum"),
        nullable=False,
        default=SeriesStatus.ongoing,
    )
    year: Mapped[int | None] = mapped_column(SmallInteger)
    is_nsfw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chapters: Mapped[list[Chapter]] = relationship(
        "Chapter", back_populates="series", cascade="all, delete-orphan", order_by="Chapter.number"
    )
    authors: Mapped[list[Author]] = relationship("Author", secondary=series_authors, back_populates="series")
    artists: Mapped[list[Artist]] = relationship("Artist", secondary=series_artists, back_populates="series")
    tags: Mapped[list[Tag]] = relationship("Tag", secondary=series_tags, back_populates="series")
    bookmarks: Mapped[list[Bookmark]] = relationship(
        "Bookmark", back_populates="series", cascade="all, delete-orphan"
    )
    source_links: Mapped[list[SourceSeries]] = relationship(
        "SourceSeries", back_populates="series", cascade="all, delete-orphan"
    )


class Chapter(Base):
    """A chapter belonging to a series."""

    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("series_id", "number", "language", name="uq_chapter_series_number_lang"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    volume: Mapped[int | None] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    scanlation_group: Mapped[str | None] = mapped_column(String(255))
    reading_mode: Mapped[ReadingMode | None] = mapped_column(
        Enum(ReadingMode, name="reading_mode_enum", create_constraint=False)
    )
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    import_status: Mapped[ChapterImportStatus] = mapped_column(
        Enum(ChapterImportStatus, name="chapter_import_status_enum"),
        nullable=False,
        default=ChapterImportStatus.importing,
        server_default=ChapterImportStatus.importing.value,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[Series] = relationship("Series", back_populates="chapters")
    pages: Mapped[list[Page]] = relationship(
        "Page", back_populates="chapter", cascade="all, delete-orphan", order_by="Page.page_number"
    )
    reading_progress: Mapped[list[ReadingProgress]] = relationship(
        "ReadingProgress", back_populates="chapter", cascade="all, delete-orphan"
    )


class Page(Base):
    """A single page image within a chapter."""

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("chapter_id", "page_number", name="uq_page_chapter_number"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(127))
    file_extension: Mapped[str | None] = mapped_column(String(16))
    integrity_status: Mapped[PageIntegrityStatus] = mapped_column(
        Enum(PageIntegrityStatus, name="page_integrity_status_enum"),
        nullable=False,
        default=PageIntegrityStatus.pending,
        server_default=PageIntegrityStatus.pending.value,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    storage_etag: Mapped[str | None] = mapped_column(String(255))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chapter: Mapped[Chapter] = relationship("Chapter", back_populates="pages")


class Source(Base):
    """A configured content source; configuration must contain no secrets."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    base_url: Mapped[str | None] = mapped_column(String(1024))
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    series_links: Mapped[list[SourceSeries]] = relationship(
        "SourceSeries", back_populates="source", cascade="all, delete-orphan"
    )
    import_jobs: Mapped[list[ImportJob]] = relationship("ImportJob", back_populates="source")


class SourceSeries(Base):
    """Maps a local series to its identity at an external source."""

    __tablename__ = "source_series"
    __table_args__ = (
        UniqueConstraint("source_id", "external_series_id", name="uq_source_series_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_series_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_url: Mapped[str | None] = mapped_column(String(2048))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_hash: Mapped[str | None] = mapped_column(String(64))

    source: Mapped[Source] = relationship("Source", back_populates="series_links")
    series: Mapped[Series] = relationship("Series", back_populates="source_links")


class ImportJob(Base):
    """Tracks one import or synchronization run."""

    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[ImportJobStatus] = mapped_column(
        Enum(ImportJobStatus, name="import_job_status_enum"),
        nullable=False,
        default=ImportJobStatus.pending,
        server_default=ImportJobStatus.pending.value,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    series_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    chapter_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    source: Mapped[Source | None] = relationship("Source", back_populates="import_jobs")
    requested_by_user: Mapped[User | None] = relationship("User", back_populates="import_jobs")
    items: Mapped[list[ImportJobItem]] = relationship(
        "ImportJobItem", back_populates="job", cascade="all, delete-orphan"
    )


class ImportJobItem(Base):
    """Per-file state for an import job."""

    __tablename__ = "import_job_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_reference: Mapped[str] = mapped_column(String(2048), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ImportJobItemStatus] = mapped_column(
        Enum(ImportJobItemStatus, name="import_job_item_status_enum"),
        nullable=False,
        default=ImportJobItemStatus.pending,
        server_default=ImportJobItemStatus.pending.value,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    job: Mapped[ImportJob] = relationship("ImportJob", back_populates="items")


# ---------------------------------------------------------------------------
# User activity tables
# ---------------------------------------------------------------------------

class RefreshSession(Base):
    """Persistent refresh-session record for token-rotation tracking.

    Only the SHA-256 digest of the token is stored.  The raw token is
    sent to the client inside an httpOnly cookie and never persisted.
    """

    __tablename__ = "refresh_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    replaced_by_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("refresh_sessions.id", ondelete="SET NULL"),
    )
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship("User", back_populates="refresh_sessions")


class Bookmark(Base):
    """A user's bookmark on a series (library entry)."""

    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "series_id", name="uq_bookmark_user_series"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[Series] = relationship("Series", back_populates="bookmarks")
    user: Mapped[User] = relationship("User", back_populates="bookmarks")


class ReadingProgress(Base):
    """Tracks how far a user has read within a chapter.

    For *paged* mode: ``last_page`` is the last page number viewed.
    For *continuous* mode: ``scroll_position`` stores the fractional scroll
    offset (0.0 = top, 1.0 = bottom) so the reader can resume at the exact
    scroll position.
    """

    __tablename__ = "reading_progress"
    __table_args__ = (UniqueConstraint("user_id", "chapter_id", name="uq_progress_user_chapter"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_page: Mapped[int | None] = mapped_column(Integer)
    scroll_position: Mapped[float | None] = mapped_column()
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chapter: Mapped[Chapter] = relationship("Chapter", back_populates="reading_progress")
    user: Mapped[User] = relationship("User", back_populates="reading_progress")
