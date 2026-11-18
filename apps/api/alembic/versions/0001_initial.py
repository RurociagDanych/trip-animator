"""Initial OLTP database schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15 19:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sessions_token_hash"), "sessions", ["token_hash"], unique=True)
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier_enc", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_oauth_states_session_id"), "oauth_states", ["session_id"], unique=False
    )
    op.create_index(op.f("ix_oauth_states_state_hash"), "oauth_states", ["state_hash"], unique=True)
    op.create_index(op.f("ix_oauth_states_user_id"), "oauth_states", ["user_id"], unique=False)

    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_athlete_id", sa.String(length=64), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.String(length=256), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),
    )
    op.create_index(
        op.f("ix_oauth_connections_external_athlete_id"),
        "oauth_connections",
        ["external_athlete_id"],
        unique=False,
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("settings_json", JSON_VARIANT, nullable=False),
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
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_projects_owner_user_id"), "projects", ["owner_user_id"], unique=False)

    op.create_table(
        "stages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("external_source_id", sa.String(length=128), nullable=True),
        sa.Column("strava_activity_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("day_index", sa.Integer(), nullable=False),
        sa.Column("trim_start_s", sa.Float(), nullable=True),
        sa.Column("trim_end_s", sa.Float(), nullable=True),
        sa.Column("bronze_object_key", sa.String(length=512), nullable=False),
        sa.Column("silver_object_key", sa.String(length=512), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("point_count", sa.Integer(), nullable=False),
        sa.Column("distance_m", sa.Float(), nullable=False),
        sa.Column("elevation_gain_m", sa.Float(), nullable=False),
        sa.Column("elevation_loss_m", sa.Float(), nullable=False),
        sa.Column("moving_seconds", sa.Float(), nullable=False),
        sa.Column("elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("has_timestamps", sa.Boolean(), nullable=False),
        sa.Column("min_lat", sa.Float(), nullable=True),
        sa.Column("max_lat", sa.Float(), nullable=True),
        sa.Column("min_lon", sa.Float(), nullable=True),
        sa.Column("max_lon", sa.Float(), nullable=True),
        sa.Column("warnings_json", JSON_VARIANT, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("source_type IN ('gpx', 'strava')", name="ck_stages_source_type"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stages_project_id"), "stages", ["project_id"], unique=False)
    op.create_index(
        op.f("ix_stages_strava_activity_id"), "stages", ["strava_activity_id"], unique=False
    )

    op.create_table(
        "render_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("idempotency_hash", sa.String(length=64), nullable=False),
        sa.Column("spec_json", JSON_VARIANT, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("progress_pct", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("output_object_key", sa.String(length=512), nullable=True),
        sa.Column("thumbnail_key", sa.String(length=512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "project_id",
            "idempotency_key",
            name="uq_render_jobs_idempotency",
        ),
    )
    op.create_index(op.f("ix_render_jobs_project_id"), "render_jobs", ["project_id"], unique=False)
    op.create_index(op.f("ix_render_jobs_status"), "render_jobs", ["status"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", JSON_VARIANT, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_events_user_id"), "audit_events", ["user_id"], unique=False)

    op.create_table(
        "rate_limits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bucket_key", sa.String(length=255), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rate_limits_bucket_key"), "rate_limits", ["bucket_key"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_rate_limits_bucket_key"), table_name="rate_limits")
    op.drop_table("rate_limits")
    op.drop_index(op.f("ix_audit_events_user_id"), table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index(op.f("ix_render_jobs_status"), table_name="render_jobs")
    op.drop_index(op.f("ix_render_jobs_project_id"), table_name="render_jobs")
    op.drop_table("render_jobs")
    op.drop_index(op.f("ix_stages_strava_activity_id"), table_name="stages")
    op.drop_index(op.f("ix_stages_project_id"), table_name="stages")
    op.drop_table("stages")
    op.drop_index(op.f("ix_projects_owner_user_id"), table_name="projects")
    op.drop_table("projects")
    op.drop_index(op.f("ix_oauth_connections_external_athlete_id"), table_name="oauth_connections")
    op.drop_table("oauth_connections")
    op.drop_index(op.f("ix_oauth_states_user_id"), table_name="oauth_states")
    op.drop_index(op.f("ix_oauth_states_state_hash"), table_name="oauth_states")
    op.drop_index(op.f("ix_oauth_states_session_id"), table_name="oauth_states")
    op.drop_table("oauth_states")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_index(op.f("ix_sessions_token_hash"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
