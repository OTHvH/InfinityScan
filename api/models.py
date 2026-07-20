"""SQLAlchemy ORM models for InfinityScan."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
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
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    refresh_sessions: Mapped[list[RefreshSession]] = relationship(
        "RefreshSession", back_populates="user", cascade="all, delete-orphan"
    )


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


class Chapter(Base):
    """A chapter belonging to a series."""

    __tablename__ = "chapters"
    __table_args__ = (UniqueConstraint("series_id", "number", "language", name="uq_chapter_series_number_lang"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[float] = mapped_column(nullable=False)
    volume: Mapped[int | None] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    scanlation_group: Mapped[str | None] = mapped_column(String(255))
    reading_mode: Mapped[ReadingMode | None] = mapped_column(
        Enum(ReadingMode, name="reading_mode_enum", create_constraint=False)
    )
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    file_size: Mapped[int | None] = mapped_column(Integer)

    chapter: Mapped[Chapter] = relationship("Chapter", back_populates="pages")


# ---------------------------------------------------------------------------
# User activity tables
# ---------------------------------------------------------------------------

class RefreshToken(Base):
    """A hashed refresh token tied to a specific user session.

    Only the SHA-256 digest of the token is stored.  The raw token is
    sent to the client inside an httpOnly cookie and never persisted.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship("User", back_populates="refresh_tokens")


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
