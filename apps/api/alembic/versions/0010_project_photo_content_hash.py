"""Store the content fingerprint used for project-scoped photo deduplication."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_project_photo_content_hash"
down_revision: str | Sequence[str] | None = "0009_library_views_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable content fingerprints for existing and future project photos."""
    op.add_column("project_photos", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_project_photos_project_id_content_hash",
        "project_photos",
        ["project_id", "content_hash"],
    )


def downgrade() -> None:
    """Remove the photo fingerprint index and column."""
    op.drop_index("ix_project_photos_project_id_content_hash", table_name="project_photos")
    op.drop_column("project_photos", "content_hash")
