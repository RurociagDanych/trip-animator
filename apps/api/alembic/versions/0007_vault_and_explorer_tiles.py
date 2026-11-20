"""Add Activity Vault, Explorer tiles, sync state, gears, and stages.vault_activity_id."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_vault_and_explorer_tiles"
down_revision: str | Sequence[str] | None = "0006_password_reset_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_VARIANT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

_ACTIVITY_TYPE_CHECK = (
    "activity_type IN ("
    "'cycling', 'gravel_mtb', 'hiking', 'trail_run', 'running', 'walking', "
    "'skiing', 'kayak', 'swimming', 'climbing', 'motorcycle', 'car', 'train', 'camp'"
    ")"
)


def upgrade() -> None:
    op.create_table(
        "vault_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("source_provider", sa.String(length=16), nullable=False),
        sa.Column("external_activity_id", sa.String(length=128), nullable=False),
        sa.Column("strava_activity_id", sa.BigInteger(), nullable=True),
        sa.Column("source_stage_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("raw_sport_type", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "activity_type",
            sa.String(length=32),
            server_default="cycling",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at_local", sa.DateTime(timezone=False), nullable=True),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=True),
        sa.Column("utc_offset_s", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("distance_m", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("elevation_gain_m", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("elevation_loss_m", sa.Float(), nullable=True),
        sa.Column("elev_high_m", sa.Float(), nullable=True),
        sa.Column("elev_low_m", sa.Float(), nullable=True),
        sa.Column("moving_seconds", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("elapsed_seconds", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("avg_speed_mps", sa.Float(), nullable=True),
        sa.Column("max_speed_mps", sa.Float(), nullable=True),
        sa.Column("avg_hr_bpm", sa.Float(), nullable=True),
        sa.Column("max_hr_bpm", sa.Float(), nullable=True),
        sa.Column("avg_watts", sa.Float(), nullable=True),
        sa.Column("weighted_watts", sa.Float(), nullable=True),
        sa.Column("kilojoules", sa.Float(), nullable=True),
        sa.Column("avg_cadence", sa.Float(), nullable=True),
        sa.Column("suffer_score", sa.Float(), nullable=True),
        sa.Column("gear_id", sa.String(length=64), nullable=True),
        sa.Column("gear_name", sa.String(length=120), nullable=True),
        sa.Column("is_commute", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_indoor_virtual", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_private", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("has_gps", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("start_lat", sa.Float(), nullable=True),
        sa.Column("start_lon", sa.Float(), nullable=True),
        sa.Column("end_lat", sa.Float(), nullable=True),
        sa.Column("end_lon", sa.Float(), nullable=True),
        sa.Column("min_lat", sa.Float(), nullable=True),
        sa.Column("max_lat", sa.Float(), nullable=True),
        sa.Column("min_lon", sa.Float(), nullable=True),
        sa.Column("max_lon", sa.Float(), nullable=True),
        sa.Column("summary_polyline", sa.Text(), nullable=True),
        sa.Column(
            "tile_precision",
            sa.String(length=16),
            server_default="none",
            nullable=False,
        ),
        sa.Column("tile_count_z14", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "new_tiles_z14_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("primary_country", sa.String(length=64), nullable=True),
        sa.Column("bronze_object_key", sa.String(length=512), nullable=True),
        sa.Column("raw_metadata_json", JSON_VARIANT, nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
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
            "source_provider IN ('strava', 'upload', 'stage')",
            name="ck_vault_activities_source_provider",
        ),
        sa.CheckConstraint(_ACTIVITY_TYPE_CHECK, name="ck_vault_activities_activity_type"),
        sa.CheckConstraint(
            "tile_precision IN ('none', 'summary_polyline', 'full_stream')",
            name="ck_vault_activities_tile_precision",
        ),
        sa.ForeignKeyConstraint(["source_stage_id"], ["stages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "source_provider",
            "external_activity_id",
            name="uq_vault_activities_user_provider_ext",
        ),
    )
    op.create_index(
        "ix_vault_activities_user_started",
        "vault_activities",
        ["user_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_vault_activities_user_local_date",
        "vault_activities",
        ["user_id", "local_date"],
        unique=False,
    )
    op.create_index(
        "ix_vault_activities_user_type_date",
        "vault_activities",
        ["user_id", "activity_type", "local_date"],
        unique=False,
    )
    op.create_index(
        "ix_vault_activities_user_strava",
        "vault_activities",
        ["user_id", "strava_activity_id"],
        unique=False,
    )
    op.create_index(
        "ix_vault_activities_user_bbox",
        "vault_activities",
        ["user_id", "min_lat", "max_lat", "min_lon", "max_lon"],
        unique=False,
    )

    op.create_table(
        "vault_activity_tiles",
        sa.Column("vault_activity_id", sa.Uuid(), nullable=False),
        sa.Column("zoom", sa.SmallInteger(), nullable=False),
        sa.Column("tile_x", sa.Integer(), nullable=False),
        sa.Column("tile_y", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("tile_key", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("zoom IN (14, 17)", name="ck_vault_activity_tiles_zoom"),
        sa.CheckConstraint("tile_x >= 0 AND tile_y >= 0", name="ck_vault_activity_tiles_xy_nonneg"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vault_activity_id"], ["vault_activities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("vault_activity_id", "zoom", "tile_x", "tile_y"),
    )
    op.create_index(
        "ix_vault_activity_tiles_user_zoom_xy",
        "vault_activity_tiles",
        ["user_id", "zoom", "tile_x", "tile_y", "vault_activity_id"],
        unique=False,
    )
    op.create_index(
        "ix_vault_activity_tiles_user_key",
        "vault_activity_tiles",
        ["user_id", "tile_key"],
        unique=False,
    )

    op.create_table(
        "vault_sync_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=16), server_default="strava", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="idle", nullable=False),
        sa.Column("phase", sa.String(length=32), server_default="idle", nullable=False),
        sa.Column("mode", sa.String(length=24), server_default="incremental", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfill_cursor_before_epoch", sa.BigInteger(), nullable=True),
        sa.Column("incremental_after_epoch", sa.BigInteger(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limit_short_used", sa.Integer(), nullable=True),
        sa.Column("rate_limit_short_limit", sa.Integer(), nullable=True),
        sa.Column("rate_limit_daily_used", sa.Integer(), nullable=True),
        sa.Column("rate_limit_daily_limit", sa.Integer(), nullable=True),
        sa.Column("current_page", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_scanned", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_upserted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "total_skipped_existing",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "total_skipped_no_gps",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("total_failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "tiles_computed_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("estimated_total_activities", sa.Integer(), nullable=True),
        sa.Column("vault_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'queued', 'running', 'rate_limited', 'failed', 'completed')",
            name="ck_vault_sync_states_status",
        ),
        sa.CheckConstraint(
            "phase IN ("
            "'idle', 'discovering_pages', 'importing_activities', "
            "'computing_tiles', 'rate_limit_paused', 'completed', 'failed'"
            ")",
            name="ck_vault_sync_states_phase",
        ),
        sa.CheckConstraint(
            "mode IN ('full_backfill', 'incremental')",
            name="ck_vault_sync_states_mode",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_vault_sync_states_user_provider"),
    )
    op.create_index(
        "ix_vault_sync_states_status_retry",
        "vault_sync_states",
        ["status", "next_retry_at", "lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "vault_gears",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("gear_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("gear_type", sa.String(length=24), server_default="other", nullable=False),
        sa.Column("distance_m", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "provider", "gear_id"),
    )

    with op.batch_alter_table("stages") as batch_op:
        batch_op.add_column(sa.Column("vault_activity_id", sa.Uuid(), nullable=True))
        batch_op.create_index(
            op.f("ix_stages_vault_activity_id"),
            ["vault_activity_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_stages_vault_activity_id_vault_activities",
            "vault_activities",
            ["vault_activity_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_vault_activities_name_tsv "
            "ON vault_activities USING GIN (to_tsvector('simple', name))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_vault_activities_name_tsv")

    with op.batch_alter_table("stages") as batch_op:
        batch_op.drop_constraint(
            "fk_stages_vault_activity_id_vault_activities",
            type_="foreignkey",
        )
        batch_op.drop_index(op.f("ix_stages_vault_activity_id"))
        batch_op.drop_column("vault_activity_id")

    op.drop_table("vault_gears")

    op.drop_index("ix_vault_sync_states_status_retry", table_name="vault_sync_states")
    op.drop_table("vault_sync_states")

    op.drop_index("ix_vault_activity_tiles_user_key", table_name="vault_activity_tiles")
    op.drop_index("ix_vault_activity_tiles_user_zoom_xy", table_name="vault_activity_tiles")
    op.drop_table("vault_activity_tiles")

    op.drop_index("ix_vault_activities_user_bbox", table_name="vault_activities")
    op.drop_index("ix_vault_activities_user_strava", table_name="vault_activities")
    op.drop_index("ix_vault_activities_user_type_date", table_name="vault_activities")
    op.drop_index("ix_vault_activities_user_local_date", table_name="vault_activities")
    op.drop_index("ix_vault_activities_user_started", table_name="vault_activities")
    op.drop_table("vault_activities")
