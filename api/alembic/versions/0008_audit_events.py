"""add append-only audit events

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-27 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    outcome_enum = postgresql.ENUM(
        "success", "failure", "denied", name="audit_event_outcome_enum", create_type=False
    )
    op.execute(
        "CREATE TYPE audit_event_outcome_enum AS ENUM ('success', 'failure', 'denied')"
    )
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("outcome", outcome_enum, nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=True),
        sa.Column("subject_id", sa.String(255), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("btrim(event_type) <> ''", name="ck_audit_events_event_type_nonempty"),
        sa.CheckConstraint("jsonb_typeof(metadata) = 'object'", name="ck_audit_events_metadata_object"),
        sa.CheckConstraint(
            "(subject_type IS NULL) = (subject_id IS NULL)",
            name="ck_audit_events_subject_fields_paired",
        ),
    )
    op.create_index("ix_audit_events_created_at_id", "audit_events", ["created_at", "id"])
    op.create_index("ix_audit_events_actor_created_at_id", "audit_events", ["actor_user_id", "created_at", "id"])
    op.create_index("ix_audit_events_event_type_created_at_id", "audit_events", ["event_type", "created_at", "id"])
    op.create_index("ix_audit_events_outcome", "audit_events", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_outcome", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type_created_at_id", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_created_at_id", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.execute("DROP TYPE audit_event_outcome_enum")
