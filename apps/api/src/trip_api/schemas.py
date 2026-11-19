"""Pydantic v2 API request/response schemas and domain validation helpers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, StringConstraints, model_validator
from trip_telemetry.schemas import ActivityType, RenderSpecV2

ProjectTitleStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
]
StageNameStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
DisplayNameStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]
StageFormat = Literal["gpx", "fit", "tcx", "strava"]
PhotoSourceType = Literal["upload", "google_photos"]
PhotoMatchMethod = Literal["timestamp", "gps", "manual", "unmatched"]
PalettePreset = Literal["alpine-dawn", "topo-emerald", "stage-by-stage"]
ClimbCategory = Literal["HC", "Cat 1", "Cat 2", "Cat 3", "Cat 4"]


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: DisplayNameStr
    password: str = Field(min_length=12, max_length=256)
    invite_code: str = Field(min_length=1, max_length=256)


class RegisterResponse(BaseModel):
    status: Literal["verification_required"] = "verification_required"
    email: str
    message: str


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ResendVerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResendVerificationResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    message: str


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ForgotPasswordResponse(BaseModel):
    status: Literal["accepted"] = "accepted"
    message: str


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ResetPasswordResponse(BaseModel):
    ok: bool = True
    message: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ChangePasswordResponse(BaseModel):
    ok: bool = True
    revoked_sessions_count: int = 0


class AuthSessionOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    is_current: bool


class LogoutAllResponse(BaseModel):
    ok: bool = True
    revoked_sessions_count: int = 0


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    strava_connected: bool = False


class SessionInfoOut(BaseModel):
    user: UserOut
    csrf_token: str
    dev_auth_enabled: bool


class StageOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_type: Literal["gpx", "strava"]
    source_format: StageFormat = "gpx"
    etl_version: int = 1
    activity_type: ActivityType = "cycling"
    external_source_id: str | None = None
    strava_activity_id: int | None = None
    name: str
    stage_order: int
    day_index: int
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    point_count: int
    distance_m: float
    elevation_gain_m: float
    elevation_loss_m: float
    moving_seconds: float
    elapsed_seconds: float
    has_timestamps: bool
    min_lat: float | None = None
    max_lat: float | None = None
    min_lon: float | None = None
    max_lon: float | None = None
    warnings: list[dict[str, object]] = Field(default_factory=list)
    created_at: datetime


class StageUpdateRequest(BaseModel):
    name: StageNameStr | None = None
    day_index: int | None = Field(default=None, ge=1, le=365)
    activity_type: ActivityType | None = None
    trim_start_s: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    trim_end_s: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_trim_bounds(self) -> StageUpdateRequest:
        if (
            self.trim_start_s is not None
            and self.trim_end_s is not None
            and self.trim_end_s <= self.trim_start_s
        ):
            raise ValueError("trim_end_s must be greater than trim_start_s")
        return self


class StageReorderRequest(BaseModel):
    stage_ids: list[uuid.UUID] = Field(min_length=1)
    day_indices: dict[uuid.UUID, int] | None = None


class StageAutoOrganizeRequest(BaseModel):
    overnight_gap_hours: float = Field(default=6.0, gt=0.0, le=168.0, allow_inf_nan=False)
    palette_preset: PalettePreset = "alpine-dawn"
    assign_overnight_icons: bool = True
    timezone: str | None = Field(default=None, max_length=64)


class ProjectCreateRequest(BaseModel):
    title: ProjectTitleStr
    settings: RenderSpecV2 = Field(default_factory=RenderSpecV2)


class ProjectUpdateRequest(BaseModel):
    revision: int = Field(ge=1)
    title: ProjectTitleStr | None = None
    settings: RenderSpecV2 | None = None


class ProjectOut(BaseModel):
    id: uuid.UUID
    title: str
    revision: int
    stage_count: int
    total_distance_m: float
    settings: dict[str, object]
    share_enabled: bool = False
    share_slug: str | None = None
    share_created_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    stages: list[StageOut]


class ShareUpdateRequest(BaseModel):
    rotate: bool = False


class ShareSettingsOut(BaseModel):
    share_enabled: bool
    share_slug: str | None = None
    share_created_at: datetime | None = None


class PublicStageOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_type: Literal["gpx", "strava"]
    source_format: StageFormat = "gpx"
    activity_type: ActivityType = "cycling"
    name: str
    stage_order: int
    day_index: int
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    point_count: int
    distance_m: float
    elevation_gain_m: float
    elevation_loss_m: float
    moving_seconds: float
    elapsed_seconds: float
    has_timestamps: bool
    min_lat: float | None = None
    max_lat: float | None = None
    min_lon: float | None = None
    max_lon: float | None = None
    created_at: datetime


class ProjectPhotoOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    stage_id: uuid.UUID | None = None
    source_type: PhotoSourceType = "upload"
    width: int
    height: int
    byte_size: int
    caption: str = ""
    taken_at_utc: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    matched_lat: float | None = None
    matched_lon: float | None = None
    matched_progress: float | None = None
    match_method: PhotoMatchMethod = "unmatched"
    camera_make_model: str | None = None
    exif_offset_s: int = 0
    enabled: bool = True
    created_at: datetime


class PublicPhotoOut(BaseModel):
    id: uuid.UUID
    width: int
    height: int
    caption: str = ""
    taken_at_utc: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    matched_lat: float | None = None
    matched_lon: float | None = None
    matched_progress: float | None = None
    match_method: PhotoMatchMethod = "unmatched"
    enabled: bool = True


class ProjectPhotoUpdateRequest(BaseModel):
    caption: Annotated[str, StringConstraints(strip_whitespace=True, max_length=280)] | None = None
    taken_at_utc: datetime | None = None
    lat: float | None = Field(default=None, ge=-90.0, le=90.0, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0, allow_inf_nan=False)
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_coordinates_pair(self) -> ProjectPhotoUpdateRequest:
        has_lat = "lat" in self.model_fields_set
        has_lon = "lon" in self.model_fields_set
        if has_lat != has_lon:
            raise ValueError("lat and lon must be updated together")
        if has_lat and (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must both be null or both be numbers")
        return self


class PhotoShiftClockRequest(BaseModel):
    offset_seconds: int | None = Field(default=None, ge=-86400, le=86400)
    photo_ids: list[uuid.UUID] | None = None
    dry_run: bool = False
    camera_make_model: str | None = Field(default=None, max_length=128)


class PhotoShiftClockOut(BaseModel):
    applied_offset_seconds: int
    dry_run: bool
    matched_before: int
    matched_after: int
    newly_matched_count: int
    photos: list[ProjectPhotoOut]


class PhotoPinRequest(BaseModel):
    lat: float | None = Field(default=None, ge=-90.0, le=90.0, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0, allow_inf_nan=False)
    progress: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_pin_target(self) -> PhotoPinRequest:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be provided together")
        if self.lat is None and self.progress is None:
            raise ValueError("Either lat/lon or progress must be provided")
        return self


class PublicProjectOut(BaseModel):
    id: uuid.UUID
    title: str
    stage_count: int
    total_distance_m: float
    settings: dict[str, object]
    stages: list[PublicStageOut]
    photos: list[PublicPhotoOut] = Field(default_factory=list)
    has_rendered_video: bool = False
    latest_render_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class AuditEventOut(BaseModel):
    id: int
    project_id: uuid.UUID | None = None
    event_type: str
    metadata: dict[str, object]
    created_at: datetime


class TelemetryChartPoint(BaseModel):
    cum_dist_m: float
    elev_m: float | None = None
    speed_mps: float
    grade_pct: float | None = None
    hr_bpm: float | None = None
    watts: float | None = None


class TelemetryChartOut(BaseModel):
    project_id: uuid.UUID
    total_distance_m: float
    points: list[TelemetryChartPoint]


class TelemetryGeometryOut(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    project_id: uuid.UUID
    features: list[dict[str, object]]


class StravaConnectOut(BaseModel):
    authorize_url: str
    state: str


class StravaCallbackOut(BaseModel):
    connected: bool
    athlete_id: str
    scope: str


class StravaDisconnectOut(BaseModel):
    connected: bool


class StravaActivityOut(BaseModel):
    id: int
    name: str
    sport_type: str
    start_date: str
    distance_m: float
    elevation_gain_m: float
    moving_time_s: int
    has_gps: bool
    summary_polyline: str | None = None


class StravaImportRequest(BaseModel):
    activity_id: int = Field(gt=0)
    day_index: int = Field(default=1, ge=1, le=365)
    name: StageNameStr | None = None
    activity_type: ActivityType | None = None


class StravaBatchImportItem(BaseModel):
    activity_id: int = Field(gt=0)
    day_index: int = Field(default=1, ge=1, le=365)
    name: StageNameStr | None = None
    custom_name: StageNameStr | None = None
    activity_type: ActivityType | None = None


class StravaBatchImportRequest(BaseModel):
    items: list[StravaBatchImportItem] = Field(min_length=1, max_length=25)


class StravaBatchImportFailure(BaseModel):
    activity_id: int
    detail: str


class StravaBatchImportOut(BaseModel):
    imported: list[StageOut]
    failed: list[StravaBatchImportFailure]


class StravaImportAllRequest(BaseModel):
    project_id: uuid.UUID | None = None
    max_activities: int = Field(default=200, ge=1, le=500)
    after: str | None = None
    before: str | None = None
    gps_only: bool = True


class StravaImportAllOut(BaseModel):
    queued_count: int
    skipped_existing_count: int
    skipped_no_gps_count: int
    total_scanned: int
    project_id: uuid.UUID | None = None
    project_title: str | None = None
    queued_activity_ids: list[int] = Field(default_factory=list)


class StravaImportStatusOut(BaseModel):
    connected: bool
    athlete_id: str | None = None
    scope: str | None = None
    pending_jobs: int = 0
    queued_jobs: int = 0
    running_jobs: int = 0
    succeeded_jobs: int = 0
    failed_jobs: int = 0
    ignored_jobs: int = 0
    imported_stages_count: int = 0
    last_error: str | None = None


class RenderJobOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    progress_pct: int
    attempt_count: int
    spec: dict[str, object]
    idempotency_key: str
    output_object_key: str | None = None
    thumbnail_key: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ClimbHighlightOut(BaseModel):
    id: str
    stage_id: uuid.UUID
    name: str
    category: ClimbCategory
    start_dist_m: float
    end_dist_m: float
    start_progress: float
    end_progress: float
    summit_progress: float
    summit_lat: float
    summit_lon: float
    summit_elev_m: float
    distance_m: float
    elevation_gain_m: float
    avg_grade_pct: float
    max_grade_pct: float
    fiets_score: float


class MetricSplitOut(BaseModel):
    stage_id: uuid.UUID
    split_index: int
    split_km: int
    distance_m: float
    elapsed_seconds: float
    moving_seconds: float
    elevation_gain_m: float
    elevation_loss_m: float
    avg_speed_mps: float
    avg_grade_pct: float | None = None
    avg_hr_bpm: float | None = None
    avg_watts: float | None = None


class DwellStopOut(BaseModel):
    id: str
    stage_id: uuid.UUID
    lat: float
    lon: float
    cum_dist_m: float
    progress: float
    duration_seconds: float
    started_at: datetime | None = None
    ended_at: datetime | None = None
    inferred_kind: str = "stop"


class PeakEffortOut(BaseModel):
    stage_id: uuid.UUID
    metric: str
    window_label: str
    window_seconds: int
    value: float
    start_dist_m: float
    end_dist_m: float


class StageGeoContextOut(BaseModel):
    stage_id: uuid.UUID
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    passes: list[str] = Field(default_factory=list)
    primary_country: str | None = None


class ProjectHighlightsOut(BaseModel):
    project_id: uuid.UUID
    climbs: list[ClimbHighlightOut] = Field(default_factory=list)
    splits: list[MetricSplitOut] = Field(default_factory=list)
    dwell_stops: list[DwellStopOut] = Field(default_factory=list)
    peak_efforts: list[PeakEffortOut] = Field(default_factory=list)
    geo_context: list[StageGeoContextOut] = Field(default_factory=list)


class CareerBucketOut(BaseModel):
    year: int
    project_count: int
    stage_count: int
    distance_m: float
    elevation_gain_m: float
    moving_seconds: float


class ActivityBreakdownOut(BaseModel):
    activity_type: str
    stage_count: int
    distance_m: float
    elevation_gain_m: float
    moving_seconds: float


class CareerAnalyticsOut(BaseModel):
    total_projects: int
    total_stages: int
    total_distance_m: float
    total_elevation_gain_m: float
    total_moving_seconds: float
    countries_visited: list[str] = Field(default_factory=list)
    by_year: list[CareerBucketOut] = Field(default_factory=list)
    by_activity: list[ActivityBreakdownOut] = Field(default_factory=list)


class AtlasTripOut(BaseModel):
    project_id: uuid.UUID
    title: str
    year: int | None = None
    primary_activity: str
    distance_m: float
    elevation_gain_m: float
    stage_count: int
    bbox: list[float] | None = None
    polylines: list[list[list[float]]] = Field(default_factory=list)


class HeatmapCellOut(BaseModel):
    h3_index: str
    lat: float
    lon: float
    point_count: int
    stage_count: int


class AnalyticsHeatmapOut(BaseModel):
    trips: list[AtlasTripOut] = Field(default_factory=list)
    hex_cells: list[HeatmapCellOut] = Field(default_factory=list)


def validate_stage_maps_against_project(spec: RenderSpecV2, valid_stage_ids: set[str]) -> None:
    """Reject stage_colors or stage_transitions referencing foreign or missing stage UUIDs."""
    bad_colors = set(spec.stage_colors.keys()) - valid_stage_ids
    bad_transitions = set(spec.stage_transitions.keys()) - valid_stage_ids
    if bad_colors or bad_transitions:
        raise HTTPException(
            status_code=422,
            detail="stage_colors or stage_transitions contain stage IDs not in this project",
        )
