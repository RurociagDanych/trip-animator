from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
    validates,
)
from trip_telemetry import VALID_ACTIVITY_TYPES

from trip_api.activity_search import normalize_activity_search

if TYPE_CHECKING:
    from trip_api.config import Settings

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    code_verifier_enc: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthConnection(Base):
    __tablename__ = "oauth_connections"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_oauth_user_provider"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_athlete_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    settings_json: Mapped[dict[str, object]] = mapped_column(
        JSONVariant, default=dict, nullable=False
    )
    share_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    share_slug: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    share_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


_ACTIVITY_TYPE_CHECK_SQL = (
    "activity_type IN (" + ", ".join(f"'{t}'" for t in VALID_ACTIVITY_TYPES) + ")"
)


class Stage(Base):
    __tablename__ = "stages"
    __table_args__ = (
        CheckConstraint("source_type IN ('gpx', 'strava')", name="ck_stages_source_type"),
        CheckConstraint(
            "source_format IN ('gpx', 'fit', 'tcx', 'strava')",
            name="ck_stages_source_format",
        ),
        CheckConstraint(_ACTIVITY_TYPE_CHECK_SQL, name="ck_stages_activity_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_format: Mapped[str] = mapped_column(
        String(16), default="gpx", server_default="gpx", nullable=False
    )
    etl_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    bronze_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activity_type: Mapped[str] = mapped_column(
        String(32), default="cycling", server_default="cycling", nullable=False
    )
    external_source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strava_activity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    vault_activity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "vault_activities.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_stages_vault_activity_id_vault_activities",
        ),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trim_start_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    trim_end_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    bronze_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    silver_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_m: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_gain_m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    elevation_loss_m: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    moving_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    has_timestamps: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    min_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    warnings_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONVariant, default=list, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectPhoto(Base):
    __tablename__ = "project_photos"
    __table_args__ = (
        Index("ix_project_photos_project_id_content_hash", "project_id", "content_hash"),
        CheckConstraint(
            "source_type IN ('upload', 'google_photos')",
            name="ck_project_photos_source_type",
        ),
        CheckConstraint(
            "match_method IN ('timestamp', 'gps', 'manual', 'unmatched')",
            name="ck_project_photos_match_method",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("stages.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(24), default="upload", nullable=False)
    external_media_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumb_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), default="image/webp", nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    caption: Mapped[str] = mapped_column(String(280), default="", nullable=False)
    taken_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_method: Mapped[str] = mapped_column(String(24), default="unmatched", nullable=False)
    camera_make_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exif_offset_s: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RenderJob(Base):
    __tablename__ = "render_jobs"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "project_id",
            "idempotency_key",
            name="uq_render_jobs_idempotency",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_json: Mapped[dict[str, object]] = mapped_column(JSONVariant, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    output_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    thumbnail_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_chunked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    render_backend: Mapped[str] = mapped_column(
        String(32), default="local", server_default="local", nullable=False, index=True
    )
    external_execution_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    remote_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credits_charged: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RenderJobChunk(Base):
    __tablename__ = "render_job_chunks"
    __table_args__ = (
        UniqueConstraint("render_job_id", "chunk_index", name="uq_render_job_chunks_job_idx"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_render_job_chunks_status",
        ),
        Index("ix_render_job_chunks_status_lease", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    render_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("render_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    end_frame: Mapped[int] = mapped_column(Integer, nullable=False)
    preroll_frames: Mapped[int] = mapped_column(
        Integer, default=15, server_default=text("15"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default="queued", server_default="queued", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    chunk_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'ignored')",
            name="ck_ingest_jobs_status",
        ),
        CheckConstraint(
            "event_type IN ('create', 'update', 'delete')",
            name="ck_ingest_jobs_event_type",
        ),
        Index("ix_ingest_jobs_status_lease_created", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(
        String(24), default="strava", server_default="strava", nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    external_owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_object_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSONVariant, default=dict, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default="queued", server_default="queued", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSONVariant, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RateLimitRecord(Base):
    __tablename__ = "rate_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VaultActivity(Base):
    __tablename__ = "vault_activities"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "source_provider",
            "external_activity_id",
            name="uq_vault_activities_user_provider_ext",
        ),
        CheckConstraint(
            "source_provider IN ('strava', 'upload', 'stage')",
            name="ck_vault_activities_source_provider",
        ),
        CheckConstraint(_ACTIVITY_TYPE_CHECK_SQL, name="ck_vault_activities_activity_type"),
        CheckConstraint(
            "tile_precision IN ('none', 'summary_polyline', 'full_stream')",
            name="ck_vault_activities_tile_precision",
        ),
        Index("ix_vault_activities_user_started", "user_id", "started_at"),
        Index("ix_vault_activities_user_local_date", "user_id", "local_date"),
        Index("ix_vault_activities_user_type_date", "user_id", "activity_type", "local_date"),
        Index("ix_vault_activities_user_strava", "user_id", "strava_activity_id"),
        Index(
            "ix_vault_activities_user_bbox",
            "user_id",
            "min_lat",
            "max_lat",
            "min_lon",
            "max_lon",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_provider: Mapped[str] = mapped_column(String(16), nullable=False)
    external_activity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strava_activity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stages.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    search_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_sport_type: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    activity_type: Mapped[str] = mapped_column(
        String(32), default="cycling", server_default="cycling", nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at_local: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    utc_offset_s: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    distance_m: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0"), nullable=False
    )
    elevation_gain_m: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0"), nullable=False
    )
    elevation_loss_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    elev_high_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    elev_low_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    moving_seconds: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0"), nullable=False
    )
    elapsed_seconds: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0"), nullable=False
    )
    avg_speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_hr_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_hr_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    weighted_watts: Mapped[float | None] = mapped_column(Float, nullable=True)
    kilojoules: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_cadence: Mapped[float | None] = mapped_column(Float, nullable=True)
    suffer_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    gear_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gear_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_commute: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    is_indoor_virtual: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    is_private: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    has_gps: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    start_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_polyline: Mapped[str | None] = mapped_column(Text, nullable=True)

    tile_precision: Mapped[str] = mapped_column(
        String(16), default="none", server_default="none", nullable=False
    )
    tile_count_z14: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    new_tiles_z14_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    primary_country: Mapped[str | None] = mapped_column(String(64), nullable=True)

    bronze_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    silver_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSONVariant, default=dict, nullable=False
    )

    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @validates("name")
    def _set_search_name(self, _key: str, value: str) -> str:
        self.search_name = normalize_activity_search(value)
        return value


class VaultActivityTile(Base):
    __tablename__ = "vault_activity_tiles"
    __table_args__ = (
        CheckConstraint("zoom IN (14, 17)", name="ck_vault_activity_tiles_zoom"),
        CheckConstraint("tile_x >= 0 AND tile_y >= 0", name="ck_vault_activity_tiles_xy_nonneg"),
        Index(
            "ix_vault_activity_tiles_user_zoom_xy",
            "user_id",
            "zoom",
            "tile_x",
            "tile_y",
            "vault_activity_id",
        ),
        Index("ix_vault_activity_tiles_user_key", "user_id", "tile_key"),
    )

    vault_activity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("vault_activities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    zoom: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    tile_x: Mapped[int] = mapped_column(Integer, primary_key=True)
    tile_y: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tile_key: Mapped[int] = mapped_column(BigInteger, nullable=False)


class VaultSyncState(Base):
    __tablename__ = "vault_sync_states"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_vault_sync_states_user_provider"),
        CheckConstraint(
            "status IN ('idle', 'queued', 'running', 'rate_limited', 'failed', 'completed')",
            name="ck_vault_sync_states_status",
        ),
        CheckConstraint(
            "phase IN ("
            "'idle', 'discovering_pages', 'importing_activities', "
            "'computing_tiles', 'rate_limit_paused', 'completed', 'failed'"
            ")",
            name="ck_vault_sync_states_phase",
        ),
        CheckConstraint(
            "mode IN ('full_backfill', 'incremental')",
            name="ck_vault_sync_states_mode",
        ),
        Index("ix_vault_sync_states_status_retry", "status", "next_retry_at", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(16), default="strava", server_default="strava", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default="idle", server_default="idle", nullable=False
    )
    phase: Mapped[str] = mapped_column(
        String(32), default="idle", server_default="idle", nullable=False
    )
    mode: Mapped[str] = mapped_column(
        String(24), default="incremental", server_default="incremental", nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backfill_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    backfill_cursor_before_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    incremental_after_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rate_limit_short_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_short_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_daily_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_page: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    total_scanned: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    total_upserted: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    total_skipped_existing: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    total_skipped_no_gps: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    total_failed: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    tiles_computed_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    estimated_total_activities: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vault_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VaultGear(Base):
    __tablename__ = "vault_gears"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(16), primary_key=True, default="strava")
    gear_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    gear_type: Mapped[str] = mapped_column(
        String(24), default="other", server_default="other", nullable=False
    )
    distance_m: Mapped[float] = mapped_column(
        Float, default=0.0, server_default=text("0"), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CreditVoucher(Base):
    __tablename__ = "credit_vouchers"
    __table_args__ = (
        CheckConstraint("credits_amount > 0", name="ck_credit_vouchers_credits_positive"),
        CheckConstraint("max_redemptions >= 1", name="ck_credit_vouchers_max_redemptions"),
        CheckConstraint(
            "redemption_count >= 0 AND redemption_count <= max_redemptions",
            name="ck_credit_vouchers_redemption_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    code_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    credits_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    max_redemptions: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1"), nullable=False
    )
    redemption_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(280), default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CreditVoucherRedemption(Base):
    __tablename__ = "credit_voucher_redemptions"
    __table_args__ = (
        UniqueConstraint(
            "voucher_id",
            "user_id",
            name="uq_credit_voucher_redemptions_voucher_user",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    voucher_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("credit_vouchers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger_entries"
    __table_args__ = (
        CheckConstraint("delta != 0", name="ck_credit_ledger_entries_delta_nonzero"),
        CheckConstraint(
            "entry_type IN ('voucher_grant', 'render_debit', 'render_refund', 'admin_adjustment')",
            name="ck_credit_ledger_entries_entry_type",
        ),
        Index("ix_credit_ledger_entries_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    render_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("render_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    render_job: Mapped[RenderJob | None] = relationship("RenderJob", foreign_keys=[render_job_id])
    voucher_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("credit_vouchers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    note: Mapped[str] = mapped_column(String(280), default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def create_db_engine(database_url: str, *, settings: Settings | None = None) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, Any] = {
        "pool_pre_ping": not is_sqlite,
        "future": True,
        "connect_args": connect_args,
    }
    if is_sqlite:
        connect_args["check_same_thread"] = False
    else:
        engine_kwargs["pool_size"] = settings.db_pool_size if settings is not None else 10
        engine_kwargs["max_overflow"] = settings.db_max_overflow if settings is not None else 20
        engine_kwargs["pool_timeout"] = settings.db_pool_timeout_s if settings is not None else 30
        engine_kwargs["pool_recycle"] = settings.db_pool_recycle_s if settings is not None else 1800
    return create_engine(database_url, **engine_kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
