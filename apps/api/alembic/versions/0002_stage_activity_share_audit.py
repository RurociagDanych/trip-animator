"""Add stage activity_type, project public sharing, and audit_events project_id.

Revision ID: 0002_stage_activity_share_audit
Revises: 0001_initial
Create Date: 2026-11-15 22:50:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_stage_activity_share_audit"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVITY_TYPE_CHECK = (
    "activity_type IN ("
    "'cycling', 'gravel_mtb', 'hiking', 'trail_run', 'running', 'walking', "
    "'skiing', 'kayak', 'swimming', 'climbing', 'motorcycle', 'car', 'train', 'camp'"
    ")"
)


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "share_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("share_slug", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("share_created_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(op.f("ix_projects_share_slug"), ["share_slug"], unique=True)

    with op.batch_alter_table("stages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "activity_type",
                sa.String(length=32),
                nullable=False,
                server_default="cycling",
            )
        )
        batch_op.create_check_constraint("ck_stages_activity_type", _ACTIVITY_TYPE_CHECK)

    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
        batch_op.create_index(op.f("ix_audit_events_project_id"), ["project_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_audit_events_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.drop_constraint("fk_audit_events_project_id_projects", type_="foreignkey")
        batch_op.drop_index(op.f("ix_audit_events_project_id"))
        batch_op.drop_column("project_id")

    with op.batch_alter_table("stages") as batch_op:
        batch_op.drop_constraint("ck_stages_activity_type", type_="check")
        batch_op.drop_column("activity_type")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index(op.f("ix_projects_share_slug"))
        batch_op.drop_column("share_created_at")
        batch_op.drop_column("share_slug")
        batch_op.drop_column("share_enabled")
