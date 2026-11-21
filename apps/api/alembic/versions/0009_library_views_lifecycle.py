"""Persist private library views and reversible catalog lifecycle state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_library_views_lifecycle"
down_revision = "0008_vault_sync_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add owner-scoped saved rules and catalog visibility state."""
    op.add_column(
        "vault_activities", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "vault_activities", sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "library_views",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("rules", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_library_views_user_id", "library_views", ["user_id"])


def downgrade() -> None:
    """Remove saved views and catalog visibility state without touching projects."""
    op.drop_table("library_views")
    op.drop_column("vault_activities", "disconnected_at")
    op.drop_column("vault_activities", "hidden_at")
