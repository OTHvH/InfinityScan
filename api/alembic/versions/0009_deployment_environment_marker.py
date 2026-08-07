"""Add an explicit deployment-environment marker.

Revision ID: 0009_deployment_environment_marker
Revises: 0008_audit_events
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_environment",
        sa.Column("environment", sa.String(length=32), primary_key=True),
        sa.Column("fixture_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("deployment_environment")
