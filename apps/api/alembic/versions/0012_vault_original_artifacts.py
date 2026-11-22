"""Persist canonical original artifact pointers for vault activities."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_vault_original_artifacts"
down_revision: str | Sequence[str] | None = "0011_vault_activity_search_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("vault_activities", sa.Column("silver_object_key", sa.String(512), nullable=True))
    op.add_column("vault_activities", sa.Column("source_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("vault_activities", "source_sha256")
    op.drop_column("vault_activities", "silver_object_key")
