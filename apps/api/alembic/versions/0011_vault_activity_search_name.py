"""Add normalized activity names for accent-insensitive library search."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from trip_api.activity_search import normalize_activity_search

revision: str = "0011_vault_activity_search_name"
down_revision: str | Sequence[str] | None = "0010_project_photo_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add and backfill folded names without limiting Unicode expansion."""
    op.add_column(
        "vault_activities",
        sa.Column("search_name", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    select_batch = sa.text(
        "SELECT id, name FROM vault_activities "
        "WHERE search_name IS NULL ORDER BY id LIMIT :batch_size"
    )
    update_name = sa.text("UPDATE vault_activities SET search_name = :search_name WHERE id = :id")
    while rows := connection.execute(select_batch, {"batch_size": 500}).mappings().all():
        connection.execute(
            update_name,
            [
                {"id": row["id"], "search_name": normalize_activity_search(row["name"])}
                for row in rows
            ],
        )


def downgrade() -> None:
    """Remove the derived search-name column."""
    op.drop_column("vault_activities", "search_name")
