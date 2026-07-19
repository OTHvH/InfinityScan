"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-10 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Enum types
    # ------------------------------------------------------------------
    content_type_enum = postgresql.ENUM(
        "manga", "manhua", "manhwa", name="content_type_enum", create_type=False
    )
    reading_mode_enum = postgresql.ENUM(
        "paged", "continuous", name="reading_mode_enum", create_type=False
    )
    series_status_enum = postgresql.ENUM(
        "ongoing", "completed", "hiatus", "cancelled", name="series_status_enum", create_type=False
    )
    user_role_enum = postgresql.ENUM(
        "admin", "user", name="user_role_enum", create_type=False
    )

    op.execute("CREATE TYPE content_type_enum AS ENUM ('manga', 'manhua', 'manhwa')")
    op.execute("CREATE TYPE reading_mode_enum AS ENUM ('paged', 'continuous')")
    op.execute("CREATE TYPE series_status_enum AS ENUM ('ongoing', 'completed', 'hiatus', 'cancelled')")
    op.execute("CREATE TYPE user_role_enum AS ENUM ('admin', 'user')")

    # ------------------------------------------------------------------
    # authors
    # ------------------------------------------------------------------
    op.create_table(
        "authors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_authors_name"),
    )

    # ------------------------------------------------------------------
    # artists
    # ------------------------------------------------------------------
    op.create_table(
        "artists",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_artists_name"),
    )

    # ------------------------------------------------------------------
    # tags
    # ------------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.UniqueConstraint("name", name="uq_tags_name"),
        sa.UniqueConstraint("slug", name="uq_tags_slug"),
    )

    # ------------------------------------------------------------------
    # series
    # ------------------------------------------------------------------
    op.create_table(
        "series",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("alternative_titles", sa.Text(), nullable=True),
        sa.Column("synopsis", sa.Text(), nullable=True),
        sa.Column("cover_object_key", sa.String(1024), nullable=True),
        sa.Column("content_type", content_type_enum, nullable=False),
        sa.Column("default_reading_mode", reading_mode_enum, nullable=False, server_default="paged"),
        sa.Column("status", series_status_enum, nullable=False, server_default="ongoing"),
        sa.Column("year", sa.SmallInteger(), nullable=True),
        sa.Column("is_nsfw", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_series_slug"),
    )
    op.create_index("ix_series_slug", "series", ["slug"])

    # ------------------------------------------------------------------
    # series_authors (M2M)
    # ------------------------------------------------------------------
    op.create_table(
        "series_authors",
        sa.Column("series_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True),
    )

    # ------------------------------------------------------------------
    # series_artists (M2M)
    # ------------------------------------------------------------------
    op.create_table(
        "series_artists",
        sa.Column("series_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("artist_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True),
    )

    # ------------------------------------------------------------------
    # series_tags (M2M)
    # ------------------------------------------------------------------
    op.create_table(
        "series_tags",
        sa.Column("series_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    )

    # ------------------------------------------------------------------
    # chapters
    # ------------------------------------------------------------------
    op.create_table(
        "chapters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("series_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Float(), nullable=False),
        sa.Column("volume", sa.SmallInteger(), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("scanlation_group", sa.String(255), nullable=True),
        sa.Column("reading_mode", reading_mode_enum, nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("series_id", "number", "language", name="uq_chapter_series_number_lang"),
    )
    op.create_index("ix_chapters_series_id", "chapters", ["series_id"])

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    op.create_table(
        "pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("chapter_id", "page_number", name="uq_page_chapter_number"),
    )
    op.create_index("ix_pages_chapter_id", "pages", ["chapter_id"])

    # ------------------------------------------------------------------
    # users (must exist before bookmarks / reading_progress)
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", user_role_enum, nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------------
    # bookmarks
    # ------------------------------------------------------------------
    op.create_table(
        "bookmarks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("series_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "series_id", name="uq_bookmark_user_series"),
    )
    op.create_index("ix_bookmarks_user_id", "bookmarks", ["user_id"])
    op.create_index("ix_bookmarks_series_id", "bookmarks", ["series_id"])

    # ------------------------------------------------------------------
    # reading_progress
    # ------------------------------------------------------------------
    op.create_table(
        "reading_progress",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("last_page", sa.Integer(), nullable=True),
        sa.Column("scroll_position", sa.Float(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "chapter_id", name="uq_progress_user_chapter"),
    )
    op.create_index("ix_reading_progress_user_id", "reading_progress", ["user_id"])
    op.create_index("ix_reading_progress_chapter_id", "reading_progress", ["chapter_id"])


def downgrade() -> None:
    op.drop_table("reading_progress")
    op.drop_table("bookmarks")
    op.drop_table("users")
    op.drop_table("pages")
    op.drop_table("chapters")
    op.drop_table("series_tags")
    op.drop_table("series_artists")
    op.drop_table("series_authors")
    op.drop_table("series")
    op.drop_table("tags")
    op.drop_table("artists")
    op.drop_table("authors")

    op.execute("DROP TYPE user_role_enum")
    op.execute("DROP TYPE series_status_enum")
    op.execute("DROP TYPE reading_mode_enum")
    op.execute("DROP TYPE content_type_enum")
