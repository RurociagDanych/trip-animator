"""Add project_photos table for geotagged and timestamped route photo stops."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_project_photos"
down_revision: str | Sequence[str] | None = "0002_stage_activity_share_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_photos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("stage_id", sa.Uuid(), nullable=True),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("external_media_id", sa.String(length=256), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("thumb_object_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("caption", sa.String(length=280), nullable=False),
        sa.Column("taken_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("matched_lat", sa.Float(), nullable=True),
        sa.Column("matched_lon", sa.Float(), nullable=True),
        sa.Column("matched_progress", sa.Float(), nullable=True),
        sa.Column("match_method", sa.String(length=24), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_type IN ('upload', 'google_photos')",
            name="ck_project_photos_source_type",
        ),
        sa.CheckConstraint(
            "match_method IN ('timestamp', 'gps', 'manual', 'unmatched')",
            name="ck_project_photos_match_method",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_id"], ["stages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_project_photos_project_id"), "project_photos", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_project_photos_taken_at_utc"), "project_photos", ["taken_at_utc"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_project_photos_taken_at_utc"), table_name="project_photos")
    op.drop_index(op.f("ix_project_photos_project_id"), table_name="project_photos")
    op.drop_table("project_photos")
