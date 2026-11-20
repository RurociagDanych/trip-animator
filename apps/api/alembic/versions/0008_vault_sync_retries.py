"""Persist the retry budget for resumable Vault synchronization.

Revision ID: 0008_vault_sync_retries
Revises: 0007_vault_and_explorer_tiles
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_vault_sync_retries"
down_revision: str | Sequence[str] | None = "0007_vault_and_explorer_tiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "vault_sync_states",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("vault_sync_states", "retry_count")
