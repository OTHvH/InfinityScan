"""add Phase 5 row-level integrity constraints

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-26 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _abort_on_invalid_rows(table: str, predicate: str, invariant: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$
            DECLARE
                invalid_count bigint;
            BEGIN
                SELECT count(*) INTO invalid_count
                FROM {table}
                WHERE {predicate};

                IF invalid_count > 0 THEN
                    RAISE EXCEPTION
                        'Phase 5 integrity preflight failed: % row(s) violate {invariant}',
                        invalid_count;
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    # Abort before DDL rather than silently normalizing potentially corrupt data.
    preflight_checks = (
        ("chapters", "page_count < 0", "chapters.page_count >= 0"),
        ("pages", "page_number < 1", "pages.page_number >= 1"),
        ("pages", "file_size IS NOT NULL AND file_size <= 0", "positive pages.file_size"),
        ("pages", "width IS NOT NULL AND width <= 0", "positive pages.width"),
        ("pages", "height IS NOT NULL AND height <= 0", "positive pages.height"),
        (
            "pages",
            "sha256 IS NOT NULL AND sha256 !~ '^[0-9a-f]{64}$'",
            "lowercase hexadecimal pages.sha256",
        ),
        (
            "pages",
            "integrity_status = 'verified' AND ("
            "btrim(object_key) = '' OR sha256 IS NULL OR width IS NULL OR height IS NULL "
            "OR file_size IS NULL OR mime_type IS NULL OR btrim(mime_type) = '' "
            "OR file_extension IS NULL OR btrim(file_extension) = '' OR verified_at IS NULL)",
            "verified page metadata requirements",
        ),
        (
            "reading_progress",
            "last_page IS NOT NULL AND last_page < 0",
            "reading_progress.last_page >= 0",
        ),
        (
            "reading_progress",
            "scroll_position IS NOT NULL AND (scroll_position < 0 OR scroll_position > 1)",
            "reading_progress.scroll_position between 0 and 1",
        ),
        (
            "import_jobs",
            "series_count < 0 OR chapter_count < 0 OR page_count < 0 "
            "OR uploaded_count < 0 OR skipped_count < 0 OR failed_count < 0",
            "non-negative import job counters",
        ),
        (
            "refresh_sessions",
            "token_hash !~ '^[0-9a-f]{64}$'",
            "lowercase hexadecimal refresh session token_hash",
        ),
        (
            "refresh_sessions",
            "expires_at <= created_at",
            "refresh session expires_at after created_at",
        ),
        (
            "refresh_sessions",
            "last_used_at IS NOT NULL AND last_used_at < created_at",
            "refresh session last_used_at after created_at",
        ),
        (
            "refresh_sessions",
            "revoked_at IS NOT NULL AND revoked_at < created_at",
            "refresh session revoked_at after created_at",
        ),
        (
            "refresh_sessions",
            "replaced_by_session_id = id",
            "refresh session replacement differs from itself",
        ),
        (
            "source_series",
            "btrim(external_series_id) = ''",
            "non-empty source_series.external_series_id",
        ),
    )
    for table, predicate, invariant in preflight_checks:
        _abort_on_invalid_rows(table, predicate, invariant)

    op.create_check_constraint("ck_chapters_page_count_nonnegative", "chapters", "page_count >= 0")
    op.create_check_constraint("ck_pages_page_number_positive", "pages", "page_number >= 1")
    op.create_check_constraint(
        "ck_pages_file_size_positive", "pages", "file_size IS NULL OR file_size > 0"
    )
    op.create_check_constraint("ck_pages_width_positive", "pages", "width IS NULL OR width > 0")
    op.create_check_constraint("ck_pages_height_positive", "pages", "height IS NULL OR height > 0")
    op.create_check_constraint(
        "ck_pages_sha256_format", "pages", "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'"
    )
    op.drop_constraint("ck_pages_verified_requirements", "pages", type_="check")
    op.create_check_constraint(
        "ck_pages_verified_requirements",
        "pages",
        "integrity_status <> 'verified' OR ("
        "btrim(object_key) <> '' AND sha256 IS NOT NULL "
        "AND width IS NOT NULL AND height IS NOT NULL AND file_size IS NOT NULL "
        "AND mime_type IS NOT NULL AND btrim(mime_type) <> '' "
        "AND file_extension IS NOT NULL AND btrim(file_extension) <> '' "
        "AND verified_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_reading_progress_last_page_nonnegative",
        "reading_progress",
        "last_page IS NULL OR last_page >= 0",
    )
    op.create_check_constraint(
        "ck_reading_progress_scroll_position_range",
        "reading_progress",
        "scroll_position IS NULL OR (scroll_position >= 0 AND scroll_position <= 1)",
    )
    op.create_check_constraint(
        "ck_import_jobs_nonnegative_counts",
        "import_jobs",
        "series_count >= 0 AND chapter_count >= 0 AND page_count >= 0 "
        "AND uploaded_count >= 0 AND skipped_count >= 0 AND failed_count >= 0",
    )
    op.create_check_constraint(
        "ck_refresh_sessions_token_hash_format",
        "refresh_sessions",
        "token_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_refresh_sessions_expires_after_created",
        "refresh_sessions",
        "expires_at > created_at",
    )
    op.create_check_constraint(
        "ck_refresh_sessions_last_used_after_created",
        "refresh_sessions",
        "last_used_at IS NULL OR last_used_at >= created_at",
    )
    op.create_check_constraint(
        "ck_refresh_sessions_revoked_after_created",
        "refresh_sessions",
        "revoked_at IS NULL OR revoked_at >= created_at",
    )
    op.create_check_constraint(
        "ck_refresh_sessions_replacement_not_self",
        "refresh_sessions",
        "replaced_by_session_id IS NULL OR replaced_by_session_id <> id",
    )
    op.create_check_constraint(
        "ck_source_series_external_id_nonempty",
        "source_series",
        "btrim(external_series_id) <> ''",
    )
    op.create_index(
        "ix_chapters_reader_order",
        "chapters",
        ["series_id", "import_status", "number", "id"],
    )
    op.create_index(
        "ix_refresh_sessions_replaced_by_session_id",
        "refresh_sessions",
        ["replaced_by_session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_sessions_replaced_by_session_id", table_name="refresh_sessions")
    op.drop_index("ix_chapters_reader_order", table_name="chapters")
    op.drop_constraint("ck_source_series_external_id_nonempty", "source_series", type_="check")
    op.drop_constraint("ck_refresh_sessions_replacement_not_self", "refresh_sessions", type_="check")
    op.drop_constraint("ck_refresh_sessions_revoked_after_created", "refresh_sessions", type_="check")
    op.drop_constraint("ck_refresh_sessions_last_used_after_created", "refresh_sessions", type_="check")
    op.drop_constraint("ck_refresh_sessions_expires_after_created", "refresh_sessions", type_="check")
    op.drop_constraint("ck_refresh_sessions_token_hash_format", "refresh_sessions", type_="check")
    op.drop_constraint("ck_import_jobs_nonnegative_counts", "import_jobs", type_="check")
    op.drop_constraint("ck_reading_progress_scroll_position_range", "reading_progress", type_="check")
    op.drop_constraint("ck_reading_progress_last_page_nonnegative", "reading_progress", type_="check")
    op.drop_constraint("ck_pages_verified_requirements", "pages", type_="check")
    op.create_check_constraint(
        "ck_pages_verified_requirements",
        "pages",
        "integrity_status <> 'verified' OR "
        "(sha256 IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL)",
    )
    op.drop_constraint("ck_pages_sha256_format", "pages", type_="check")
    op.drop_constraint("ck_pages_height_positive", "pages", type_="check")
    op.drop_constraint("ck_pages_width_positive", "pages", type_="check")
    op.drop_constraint("ck_pages_file_size_positive", "pages", type_="check")
    op.drop_constraint("ck_pages_page_number_positive", "pages", type_="check")
    op.drop_constraint("ck_chapters_page_count_nonnegative", "chapters", type_="check")
