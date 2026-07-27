"""add durable import recovery state

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-27 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("import_jobs", sa.Column("resume_payload", postgresql.JSONB(), nullable=True))
    op.add_column(
        "import_jobs",
        sa.Column(
            "checkpoint",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("import_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column(
        "import_jobs",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("import_jobs", sa.Column("lease_owner_id", postgresql.UUID(as_uuid=True)))
    op.add_column("import_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "import_jobs", sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "import_jobs",
        sa.Column("recovery_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_import_jobs_recovery_numbers_nonnegative",
        "import_jobs",
        "fencing_token >= 0 AND recovery_attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_import_jobs_lease_fields_paired",
        "import_jobs",
        "(lease_owner_id IS NULL) = (lease_expires_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_import_jobs_recovery_json_objects",
        "import_jobs",
        "(resume_payload IS NULL OR jsonb_typeof(resume_payload) = 'object') "
        "AND jsonb_typeof(checkpoint) = 'object'",
    )
    op.create_index("ix_import_jobs_heartbeat_at", "import_jobs", ["heartbeat_at"])
    op.create_index("ix_import_jobs_lease_expires_at", "import_jobs", ["lease_expires_at"])
    op.create_index(
        "ix_import_jobs_recovery_candidates",
        "import_jobs",
        ["status", "heartbeat_at", "lease_expires_at"],
        postgresql_where=sa.text("status IN ('pending', 'scanning', 'uploading', 'verifying')"),
    )

    op.add_column("import_job_items", sa.Column("item_key", sa.String(255)))
    op.add_column("import_job_items", sa.Column("item_kind", sa.String(32)))
    op.add_column("import_job_items", sa.Column("series_id", postgresql.UUID(as_uuid=True)))
    op.add_column("import_job_items", sa.Column("chapter_id", postgresql.UUID(as_uuid=True)))
    op.add_column("import_job_items", sa.Column("page_number", sa.Integer()))
    op.add_column("import_job_items", sa.Column("byte_size", sa.BigInteger()))
    op.add_column("import_job_items", sa.Column("mime_type", sa.String(127)))
    op.add_column("import_job_items", sa.Column("file_extension", sa.String(16)))
    op.add_column("import_job_items", sa.Column("storage_etag", sa.String(255)))
    op.add_column(
        "import_job_items",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("import_job_items", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint(
        "uq_import_job_items_job_item_key", "import_job_items", ["job_id", "item_key"]
    )
    op.create_check_constraint(
        "ck_import_job_items_attempt_count_nonnegative",
        "import_job_items",
        "attempt_count >= 0",
    )
    op.create_index("ix_import_job_items_series_id", "import_job_items", ["series_id"])
    op.create_index("ix_import_job_items_chapter_id", "import_job_items", ["chapter_id"])


def downgrade() -> None:
    op.drop_index("ix_import_job_items_chapter_id", table_name="import_job_items")
    op.drop_index("ix_import_job_items_series_id", table_name="import_job_items")
    op.drop_constraint(
        "ck_import_job_items_attempt_count_nonnegative", "import_job_items", type_="check"
    )
    op.drop_constraint(
        "uq_import_job_items_job_item_key", "import_job_items", type_="unique"
    )
    for column in (
        "verified_at",
        "attempt_count",
        "storage_etag",
        "file_extension",
        "mime_type",
        "byte_size",
        "page_number",
        "chapter_id",
        "series_id",
        "item_kind",
        "item_key",
    ):
        op.drop_column("import_job_items", column)

    op.drop_index("ix_import_jobs_recovery_candidates", table_name="import_jobs")
    op.drop_index("ix_import_jobs_lease_expires_at", table_name="import_jobs")
    op.drop_index("ix_import_jobs_heartbeat_at", table_name="import_jobs")
    op.drop_constraint("ck_import_jobs_recovery_json_objects", "import_jobs", type_="check")
    op.drop_constraint("ck_import_jobs_lease_fields_paired", "import_jobs", type_="check")
    op.drop_constraint(
        "ck_import_jobs_recovery_numbers_nonnegative", "import_jobs", type_="check"
    )
    for column in (
        "recovery_attempt_count",
        "fencing_token",
        "lease_expires_at",
        "lease_owner_id",
        "updated_at",
        "heartbeat_at",
        "checkpoint",
        "resume_payload",
    ):
        op.drop_column("import_jobs", column)
