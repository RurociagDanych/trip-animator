"""Add ETL versioning, photo camera metadata, chunked renders, and webhook ingest queue.

Revision ID: 0004_etl_chunks_ingest
Revises: 0003_project_photos
Create Date: 2026-11-17 16:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_etl_chunks_ingest"
down_revision: str | Sequence[str] | None = "0003_project_photos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("stages") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_format",
                sa.String(length=16),
                nullable=False,
                server_default="gpx",
            )
        )
        batch_op.add_column(
            sa.Column(
                "etl_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(sa.Column("bronze_sha256", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint(
            "ck_stages_source_format",
            "source_format IN ('gpx', 'fit', 'tcx', 'strava')",
        )

    with op.batch_alter_table("project_photos") as batch_op:
        batch_op.add_column(
            sa.Column("camera_make_model", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "exif_offset_s",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

    with op.batch_alter_table("render_jobs") as batch_op:
        batch_op.add_column(sa.Column("content_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_chunked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("total_frames", sa.Integer(), nullable=True))
        batch_op.create_index(op.f("ix_render_jobs_content_hash"), ["content_hash"], unique=False)

    op.create_table(
        "render_job_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("render_job_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("start_frame", sa.Integer(), nullable=False),
        sa.Column("end_frame", sa.Integer(), nullable=False),
        sa.Column(
            "preroll_frames",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("chunk_object_key", sa.String(length=512), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_render_job_chunks_status",
        ),
        sa.ForeignKeyConstraint(["render_job_id"], ["render_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("render_job_id", "chunk_index", name="uq_render_job_chunks_job_idx"),
    )
    op.create_index(
        op.f("ix_render_job_chunks_render_job_id"),
        "render_job_chunks",
        ["render_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_render_job_chunks_status_lease",
        "render_job_chunks",
        ["status", "lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=24),
            nullable=False,
            server_default="strava",
        ),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("external_owner_id", sa.String(length=64), nullable=False),
        sa.Column("external_object_id", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", JSON_VARIANT, nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'ignored')",
            name="ck_ingest_jobs_status",
        ),
        sa.CheckConstraint(
            "event_type IN ('create', 'update', 'delete')",
            name="ck_ingest_jobs_event_type",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ingest_jobs_external_owner_id"),
        "ingest_jobs",
        ["external_owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ingest_jobs_external_object_id"),
        "ingest_jobs",
        ["external_object_id"],
        unique=False,
    )
    op.create_index(
        "ix_ingest_jobs_status_lease_created",
        "ingest_jobs",
        ["status", "lease_expires_at", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status_lease_created", table_name="ingest_jobs")
    op.drop_index(op.f("ix_ingest_jobs_external_object_id"), table_name="ingest_jobs")
    op.drop_index(op.f("ix_ingest_jobs_external_owner_id"), table_name="ingest_jobs")
    op.drop_table("ingest_jobs")

    op.drop_index("ix_render_job_chunks_status_lease", table_name="render_job_chunks")
    op.drop_index(op.f("ix_render_job_chunks_render_job_id"), table_name="render_job_chunks")
    op.drop_table("render_job_chunks")

    with op.batch_alter_table("render_jobs") as batch_op:
        batch_op.drop_index(op.f("ix_render_jobs_content_hash"))
        batch_op.drop_column("total_frames")
        batch_op.drop_column("is_chunked")
        batch_op.drop_column("content_hash")

    with op.batch_alter_table("project_photos") as batch_op:
        batch_op.drop_column("exif_offset_s")
        batch_op.drop_column("camera_make_model")

    with op.batch_alter_table("stages") as batch_op:
        batch_op.drop_constraint("ck_stages_source_format", type_="check")
        batch_op.drop_column("bronze_sha256")
        batch_op.drop_column("etl_version")
        batch_op.drop_column("source_format")
