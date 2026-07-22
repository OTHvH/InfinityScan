"""add Phase 3 storage, source, and import job schema

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CHAPTER_IMPORT_STATUSES = ("importing", "ready", "failed", "quarantined")
PAGE_INTEGRITY_STATUSES = ("pending", "verified", "missing", "mismatch", "quarantined")
IMPORT_JOB_STATUSES = (
    "pending",
    "scanning",
    "uploading",
    "verifying",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
)
IMPORT_JOB_ITEM_STATUSES = (
    "pending",
    "uploading",
    "verifying",
    "succeeded",
    "skipped",
    "failed",
    "quarantined",
)


def _enum_type(name: str, values: tuple[str, ...]) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.execute(
        "CREATE TYPE chapter_import_status_enum AS ENUM "
        "('importing', 'ready', 'failed', 'quarantined')"
    )
    op.execute(
        "CREATE TYPE page_integrity_status_enum AS ENUM "
        "('pending', 'verified', 'missing', 'mismatch', 'quarantined')"
    )
    op.execute(
        "CREATE TYPE import_job_status_enum AS ENUM "
        "('pending', 'scanning', 'uploading', 'verifying', 'succeeded', "
        "'partial', 'failed', 'cancelled')"
    )
    op.execute(
        "CREATE TYPE import_job_item_status_enum AS ENUM "
        "('pending', 'uploading', 'verifying', 'succeeded', 'skipped', "
        "'failed', 'quarantined')"
    )

    # Refuse lossy or out-of-range conversions instead of silently changing
    # existing chapter identities during the Float -> Numeric migration.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM chapters
                WHERE number::text IN ('NaN', 'Infinity', '-Infinity')
                   OR number > 99999999.9999
                   OR number < -99999999.9999
                   OR number::numeric <> round(number::numeric, 4)
            ) THEN
                RAISE EXCEPTION
                    'chapters.number contains a value that cannot be safely converted to NUMERIC(12,4)';
            END IF;
        END $$;
        """
    )
    op.alter_column(
        "chapters",
        "number",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 4),
        existing_nullable=False,
        postgresql_using="number::numeric(12,4)",
    )
    op.add_column(
        "chapters",
        sa.Column(
            "import_status",
            _enum_type("chapter_import_status_enum", CHAPTER_IMPORT_STATUSES),
            nullable=False,
            server_default="importing",
        ),
    )
    op.add_column("chapters", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chapters", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("pages", sa.Column("sha256", sa.String(64), nullable=True))
    op.add_column("pages", sa.Column("mime_type", sa.String(127), nullable=True))
    op.add_column("pages", sa.Column("file_extension", sa.String(16), nullable=True))
    op.add_column(
        "pages",
        sa.Column(
            "integrity_status",
            _enum_type("page_integrity_status_enum", PAGE_INTEGRITY_STATUSES),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column("pages", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pages", sa.Column("storage_etag", sa.String(255), nullable=True))
    op.add_column("pages", sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_pages_verified_requirements",
        "pages",
        "integrity_status <> 'verified' OR "
        "(sha256 IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL)",
    )

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("adapter_type", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("base_url", sa.String(1024), nullable=True),
        # This JSON is configuration only. Provider credentials belong in the
        # deployment secret store and must never be placed in this column.
        sa.Column("configuration", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("key", name="uq_sources_key"),
    )

    op.create_table(
        "source_series",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "series_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("series.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_series_id", sa.String(255), nullable=False),
        sa.Column("external_url", sa.String(2048), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_hash", sa.String(64), nullable=True),
        sa.UniqueConstraint(
            "source_id",
            "external_series_id",
            name="uq_source_series_external_id",
        ),
    )
    op.create_index("ix_source_series_source_id", "source_series", ["source_id"])
    op.create_index("ix_source_series_series_id", "source_series", ["series_id"])

    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            _enum_type("import_job_status_enum", IMPORT_JOB_STATUSES),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("series_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chapter_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_import_jobs_idempotency_key"),
    )
    op.create_index("ix_import_jobs_source_id", "import_jobs", ["source_id"])
    op.create_index("ix_import_jobs_requested_by_user_id", "import_jobs", ["requested_by_user_id"])
    op.create_index("ix_import_jobs_status", "import_jobs", ["status"])
    op.create_index("ix_import_jobs_created_at", "import_jobs", ["created_at"])
    op.create_index("ix_import_jobs_started_at", "import_jobs", ["started_at"])
    op.create_index("ix_import_jobs_finished_at", "import_jobs", ["finished_at"])

    op.create_table(
        "import_job_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("import_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_reference", sa.String(2048), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column(
            "status",
            _enum_type("import_job_item_status_enum", IMPORT_JOB_ITEM_STATUSES),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_import_job_items_job_id", "import_job_items", ["job_id"])
    op.create_index("ix_import_job_items_status", "import_job_items", ["status"])

    # Existing rows have no trustworthy hashes or dimensions. They are marked
    # pending at page level, while chapters are ready only when their current
    # page set is structurally complete and has non-empty object keys.
    op.execute(
        """
        UPDATE chapters AS c
        SET import_status = CASE
            WHEN c.page_count = (
                SELECT count(*)
                FROM pages AS p
                WHERE p.chapter_id = c.id
            )
            AND (
                c.page_count = 0
                OR (
                    (SELECT min(p.page_number) FROM pages AS p WHERE p.chapter_id = c.id) = 1
                    AND (SELECT max(p.page_number) FROM pages AS p WHERE p.chapter_id = c.id) = c.page_count
                )
            )
            AND NOT EXISTS (
                SELECT 1
                FROM pages AS p
                WHERE p.chapter_id = c.id
                  AND btrim(p.object_key) = ''
            )
            THEN 'ready'::chapter_import_status_enum
            ELSE 'failed'::chapter_import_status_enum
        END
        """
    )


def downgrade() -> None:
    op.drop_index("ix_import_job_items_status", table_name="import_job_items")
    op.drop_index("ix_import_job_items_job_id", table_name="import_job_items")
    op.drop_table("import_job_items")

    op.drop_index("ix_import_jobs_finished_at", table_name="import_jobs")
    op.drop_index("ix_import_jobs_started_at", table_name="import_jobs")
    op.drop_index("ix_import_jobs_created_at", table_name="import_jobs")
    op.drop_index("ix_import_jobs_status", table_name="import_jobs")
    op.drop_index("ix_import_jobs_requested_by_user_id", table_name="import_jobs")
    op.drop_index("ix_import_jobs_source_id", table_name="import_jobs")
    op.drop_table("import_jobs")

    op.drop_index("ix_source_series_series_id", table_name="source_series")
    op.drop_index("ix_source_series_source_id", table_name="source_series")
    op.drop_table("source_series")
    op.drop_table("sources")

    op.drop_constraint("ck_pages_verified_requirements", "pages", type_="check")
    op.drop_column("pages", "imported_at")
    op.drop_column("pages", "storage_etag")
    op.drop_column("pages", "verified_at")
    op.drop_column("pages", "integrity_status")
    op.drop_column("pages", "file_extension")
    op.drop_column("pages", "mime_type")
    op.drop_column("pages", "sha256")

    op.drop_column("chapters", "source_updated_at")
    op.drop_column("chapters", "verified_at")
    op.drop_column("chapters", "import_status")
    op.alter_column(
        "chapters",
        "number",
        existing_type=sa.Numeric(12, 4),
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="number::double precision",
    )

    op.execute("DROP TYPE import_job_item_status_enum")
    op.execute("DROP TYPE import_job_status_enum")
    op.execute("DROP TYPE page_integrity_status_enum")
    op.execute("DROP TYPE chapter_import_status_enum")
