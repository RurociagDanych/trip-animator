from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_render_job_remote_backend"
down_revision: str | Sequence[str] | None = "0012_vault_original_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "render_jobs",
        sa.Column(
            "render_backend",
            sa.String(32),
            nullable=False,
            server_default="local",
        ),
    )
    op.add_column(
        "render_jobs",
        sa.Column("external_execution_id", sa.String(512), nullable=True),
    )
    op.add_column(
        "render_jobs",
        sa.Column("remote_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_render_jobs_render_backend", "render_jobs", ["render_backend"])


def downgrade() -> None:
    op.drop_index("ix_render_jobs_render_backend", table_name="render_jobs")
    op.drop_column("render_jobs", "remote_dispatched_at")
    op.drop_column("render_jobs", "external_execution_id")
    op.drop_column("render_jobs", "render_backend")
