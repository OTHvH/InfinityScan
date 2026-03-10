"""SQLAlchemy ORM models for InfinityScan."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
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
    manga = "manga"      # right-to-left, Japanese origin
    manhua = "manhua"    # left-to-right, Chinese origin
    manhwa = "manhwa"    # left-to-right, Korean origin


class ReadingMode(str, enum.Enum):
    paged = "paged"           # one page at a time
    continuous = "continuous"  # vertical scroll (webtoon / manhua style)


class SeriesStatus(str, enum.Enum):
    ongoing = "ongoing"
    completed = "completed"
    hiatus = "hiatus"
    cancelled = "cancelled"


# ---------------------------------------------------------------------------
# Association tables (many-to-many)
# ---------------------------------------------------------------------------

from sqlalchemy import Table, Column  # noqa: E402 – kept together for clarity

series_authors = Table(
    "series_authors",
    Base.metadata,
    Column("series_id", UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("author_id", UUID(as_uuid=True), ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True),
)

series_artists = Table(
    "series_artists",
    Base.metadata,
    Column("series_id", UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("artist_id", UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True),
)

series_tags = Table(
    "series_tags",
    Base.metadata,
    Column("series_id", UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------------------
# Lookup / taxonomy tables
# ---------------------------------------------------------------------------

class Author(Base):
    __tablename__ = "authors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[list[Series]] = relationship("Series", secondary=series_authors, back_populates="authors")


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    bio: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[list[Series]] = relationship("Series", secondary=series_artists, back_populates="artists")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    series: Mapped[list[Series]] = relationship("Series", secondary=series_tags, back_populates="tags")


# ---------------------------------------------------------------------------
# Core content tables
# ---------------------------------------------------------------------------

class Series(Base):
    """A manga / manhua / manhwa series."""

    __tablename__ = "series"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    alternative_titles: Mapped[str | None] = mapped_column(Text)  # JSON array stored as text
    synopsis: Mapped[str | None] = mapped_column(Text)
    cover_object_key: Mapped[str | None] = mapped_column(String(1024))  # S3 object key
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[float] = mapped_column(nullable=False)  # e.g. 1.0, 1.5 for half-chapters
    volume: Mapped[int | None] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")  # BCP-47
    scanlation_group: Mapped[str | None] = mapped_column(String(255))
    reading_mode: Mapped[ReadingMode | None] = mapped_column(
        Enum(ReadingMode, name="reading_mode_enum"), create_constraint=False
    )  # overrides series default when set
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

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-based
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)  # S3-compatible object key
    width: Mapped[int | None] = mapped_column(Integer)   # pixels
    height: Mapped[int | None] = mapped_column(Integer)  # pixels
    file_size: Mapped[int | None] = mapped_column(BigInteger)  # bytes

    chapter: Mapped[Chapter] = relationship("Chapter", back_populates="pages")


# ---------------------------------------------------------------------------
# User activity tables
# ---------------------------------------------------------------------------

class Bookmark(Base):
    """A user's bookmark on a series (library entry)."""

    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("user_id", "series_id", name="uq_bookmark_user_series"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    series: Mapped[Series] = relationship("Series", back_populates="bookmarks")


class ReadingProgress(Base):
    """Tracks how far a user has read within a chapter.

    For *paged* mode: ``last_page`` is the last page number viewed.
    For *continuous* mode: ``scroll_position`` stores the fractional scroll
    offset (0.0 = top, 1.0 = bottom) so the reader can resume at the exact
    scroll position.
    """

    __tablename__ = "reading_progress"
    __table_args__ = (UniqueConstraint("user_id", "chapter_id", name="uq_progress_user_chapter"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    chapter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    last_page: Mapped[int | None] = mapped_column(Integer)          # paged mode
    scroll_position: Mapped[float | None] = mapped_column()         # continuous mode (0.0–1.0)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chapter: Mapped[Chapter] = relationship("Chapter", back_populates="reading_progress")
