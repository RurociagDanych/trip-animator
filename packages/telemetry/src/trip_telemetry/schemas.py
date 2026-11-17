"""Canonical telemetry, ingestion, analytics, and render specification schemas."""

import math
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CURRENT_ETL_VERSION = 2

SourceFormat = Literal["gpx", "fit", "tcx", "strava"]
ResolutionPreset = Literal[
    "1280x720",
    "1920x1080",
    "1080x1080",
    "1080x1350",
    "1080x1920",
]
MapStylePreset = Literal[
    "liberty",
    "positron",
    "bright",
    "dark",
    "topo-relief",
    "satellite-hybrid",
    "editorial-mono",
]
TimelineMode = Literal["distance", "recorded-time"]
CameraMode = Literal["fit-track", "follow-marker"]
CameraPreset = Literal["flat", "dynamic-3d", "drone-orbit"]
FollowZoomMode = Literal["adaptive", "fixed"]
VisibilityMode = Literal["full", "traversed", "trail"]
UnitsMode = Literal["metric", "imperial"]
TransitionIcon = Literal["none", "bed", "meal", "coffee", "tent", "transport"]
LodLevel = Literal["full", "medium", "low"]
ColorByMode = Literal["stage", "grade", "speed", "hr", "watts"]
DirectorPreset = Literal["none", "expedition-doc", "climb-spotlight", "social-reel"]
PhotoCardStyle = Literal["corner", "polaroid", "cinematic"]
ClimbCategory = Literal["HC", "Cat 1", "Cat 2", "Cat 3", "Cat 4"]
ActivityType = Literal[
    "cycling",
    "gravel_mtb",
    "hiking",
    "trail_run",
    "running",
    "walking",
    "skiing",
    "kayak",
    "swimming",
    "climbing",
    "motorcycle",
    "car",
    "train",
    "camp",
]
VALID_ACTIVITY_TYPES: tuple[ActivityType, ...] = (
    "cycling",
    "gravel_mtb",
    "hiking",
    "trail_run",
    "running",
    "walking",
    "skiing",
    "kayak",
    "swimming",
    "climbing",
    "motorcycle",
    "car",
    "train",
    "camp",
)
DEFAULT_ACTIVITY_TYPE: ActivityType = "cycling"
_VALID_ACTIVITY_TYPE_SET: frozenset[str] = frozenset(VALID_ACTIVITY_TYPES)


def is_activity_type(value: object) -> bool:
    """Return True if value is one of the 14 canonical ActivityType identifiers."""
    return isinstance(value, str) and value in _VALID_ACTIVITY_TYPE_SET


def normalize_activity_type(
    value: object,
    fallback: ActivityType = DEFAULT_ACTIVITY_TYPE,
) -> ActivityType:
    """Normalize an arbitrary string/value to a canonical ActivityType."""
    if isinstance(value, str) and value in _VALID_ACTIVITY_TYPE_SET:
        return value  # type: ignore[return-value]
    if isinstance(value, str) and value.strip():
        return infer_activity_type_from_strava(value, None, fallback)
    return fallback


_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CAMEL_BOUNDARY_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_BOUNDARY_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_GRAVEL_MTB_COMPACT: frozenset[str] = frozenset(
    {
        "gravelmtb",
        "gravelride",
        "mountainbikeride",
        "emountainbikeride",
        "cyclocross",
        "velomobile",
        "handcycle",
    }
)
_GRAVEL_MTB_TOKENS: frozenset[str] = frozenset(
    {
        "gravel",
        "mtb",
        "cyclocross",
        "singletrack",
        "bikepack",
        "bikepacking",
    }
)
_GRAVEL_MTB_UPGRADE_TOKENS: frozenset[str] = _GRAVEL_MTB_TOKENS | {"enduro"}
_GRAVEL_MTB_PHRASES: tuple[tuple[str, ...], ...] = (
    ("mountain", "bike"),
    ("mountain", "biking"),
)

_CYCLING_COMPACT: frozenset[str] = frozenset(
    {
        "cycling",
        "ride",
        "virtualride",
        "ebikeride",
        "roadbike",
        "roadcycling",
        "biking",
    }
)
_CYCLING_TOKENS: frozenset[str] = frozenset(
    {
        "ride",
        "riding",
        "cycle",
        "cycling",
        "bike",
        "biking",
        "bicycle",
        "ebike",
    }
)

_TRAIL_RUN_COMPACT: frozenset[str] = frozenset(
    {
        "trailrun",
        "trailrunning",
        "skyrun",
        "skyrunning",
        "fellrun",
        "fellrunning",
        "ultrarun",
        "ultrarunning",
    }
)
_TRAIL_RUN_TOKENS: frozenset[str] = frozenset(
    {
        "trailrun",
        "trailrunning",
        "skyrun",
        "skyrunning",
        "fellrun",
        "fellrunning",
        "ultrarun",
        "ultrarunning",
        "ultramarathon",
    }
)
_TRAIL_RUN_PHRASES: tuple[tuple[str, ...], ...] = (
    ("trail", "run"),
    ("trail", "running"),
    ("fell", "run"),
    ("fell", "running"),
    ("ultra", "run"),
    ("ultra", "running"),
    ("sky", "run"),
    ("sky", "running"),
)
_TRAIL_RUN_UPGRADE_TOKENS: frozenset[str] = frozenset(
    {
        "trail",
        "skyrun",
        "skyrunning",
        "fell",
        "ultra",
        "ultramarathon",
    }
)

_RUNNING_COMPACT: frozenset[str] = frozenset({"running", "run", "virtualrun"})
_RUNNING_TOKENS: frozenset[str] = frozenset(
    {
        "run",
        "running",
        "jog",
        "jogging",
        "marathon",
        "halfmarathon",
        "parkrun",
    }
)

_HIKING_COMPACT: frozenset[str] = frozenset(
    {"hiking", "hike", "snowshoe", "snowshoeing", "trek", "trekking", "backpacking"}
)
_HIKING_TOKENS: frozenset[str] = frozenset(
    {
        "hike",
        "hiking",
        "trek",
        "trekking",
        "backpack",
        "backpacking",
        "snowshoe",
        "snowshoeing",
        "ruck",
        "rucking",
    }
)

_WALKING_COMPACT: frozenset[str] = frozenset(
    {"walking", "walk", "wheelchair", "iceskate", "inlineskate", "skateboard"}
)
_WALKING_TOKENS: frozenset[str] = frozenset(
    {
        "walk",
        "walking",
        "stroll",
        "wheelchair",
        "iceskate",
        "inlineskate",
        "skateboard",
    }
)

_SKIING_COMPACT: frozenset[str] = frozenset(
    {
        "skiing",
        "ski",
        "alpineski",
        "backcountryski",
        "nordicski",
        "snowboard",
        "snowboarding",
        "rollerski",
        "skitour",
        "skitouring",
    }
)
_SKIING_TOKENS: frozenset[str] = frozenset(
    {
        "ski",
        "skiing",
        "snowboard",
        "snowboarding",
        "skimo",
        "telemark",
        "alpineski",
        "backcountryski",
        "nordicski",
        "rollerski",
        "skitour",
        "skitouring",
        "splitboard",
    }
)

_KAYAK_COMPACT: frozenset[str] = frozenset(
    {
        "kayak",
        "kayaking",
        "canoeing",
        "canoe",
        "standuppaddling",
        "rowing",
        "virtualrow",
        "sail",
        "sailing",
        "surfing",
        "windsurf",
        "windsurfing",
        "kitesurf",
        "kitesurfing",
    }
)
_KAYAK_TOKENS: frozenset[str] = frozenset(
    {
        "kayak",
        "kayaking",
        "canoe",
        "canoeing",
        "paddle",
        "paddling",
        "standuppaddling",
        "packraft",
        "packrafting",
        "raft",
        "rafting",
        "row",
        "rowing",
        "sup",
        "sail",
        "sailing",
        "surf",
        "surfing",
        "windsurf",
        "windsurfing",
        "kitesurf",
        "kitesurfing",
        "ferry",
        "boat",
        "boating",
    }
)

_SWIMMING_COMPACT: frozenset[str] = frozenset({"swimming", "swim", "openwaterswim"})
_SWIMMING_TOKENS: frozenset[str] = frozenset({"swim", "swimming", "openwaterswim"})

_CLIMBING_COMPACT: frozenset[str] = frozenset(
    {"climbing", "rockclimbing", "mountaineering", "bouldering", "alpinism"}
)
_CLIMBING_TOKENS: frozenset[str] = frozenset(
    {
        "climb",
        "climbing",
        "rockclimbing",
        "boulder",
        "bouldering",
        "mountaineer",
        "mountaineering",
        "alpinism",
        "alpinist",
        "summit",
        "ferrata",
    }
)

_MOTORCYCLE_COMPACT: frozenset[str] = frozenset({"motorcycle", "moto", "motorbike", "dirtbike"})
_MOTORCYCLE_TOKENS: frozenset[str] = frozenset(
    {
        "motorcycle",
        "motorcycling",
        "motorbike",
        "moto",
        "scooter",
        "dirtbike",
        "moped",
    }
)

_CAR_COMPACT: frozenset[str] = frozenset(
    {"car", "drive", "driving", "auto", "overland", "roadtrip"}
)
_CAR_TOKENS: frozenset[str] = frozenset(
    {
        "drive",
        "driving",
        "car",
        "overland",
        "overlanding",
        "4x4",
        "bus",
        "shuttle",
        "van",
        "roadtrip",
    }
)
_CAR_PHRASES: tuple[tuple[str, ...], ...] = (("road", "trip"),)

_TRAIN_COMPACT: frozenset[str] = frozenset({"train", "rail", "railway", "transit", "transport"})
_TRAIN_TOKENS: frozenset[str] = frozenset(
    {
        "train",
        "rail",
        "railway",
        "railroad",
        "metro",
        "subway",
        "tram",
        "tgv",
        "eurostar",
    }
)

_CAMP_COMPACT: frozenset[str] = frozenset({"camp", "camping", "tent", "bivouac", "bivy"})
_CAMP_TOKENS: frozenset[str] = frozenset(
    {
        "camp",
        "camping",
        "campsite",
        "bivouac",
        "bivy",
        "tent",
        "hut",
        "refuge",
        "rifugio",
    }
)


def _tokenize_activity_text(
    raw: str | None,
) -> tuple[str, str, tuple[str, ...], frozenset[str]]:
    """Split raw activity text into normalized snake_case, compact form, and word tokens."""
    if not raw:
        return "", "", (), frozenset()
    text = raw.strip()
    if not text:
        return "", "", (), frozenset()
    decamel = _CAMEL_BOUNDARY_2.sub(r"\1 \2", _CAMEL_BOUNDARY_1.sub(r"\1 \2", text)).lower()
    tokens = tuple(tok for tok in _NON_ALNUM_RE.split(decamel) if tok)
    normalized = "_".join(tokens)
    compact = "".join(tokens)
    return normalized, compact, tokens, frozenset(tokens)


def _has_any_phrase(
    tokens: tuple[str, ...],
    phrases: tuple[tuple[str, ...], ...],
) -> bool:
    for phrase in phrases:
        plen = len(phrase)
        if plen <= len(tokens) and any(
            tokens[i : i + plen] == phrase for i in range(len(tokens) - plen + 1)
        ):
            return True
    return False


def _classify_activity_tokens(
    normalized: str,
    compact: str,
    tokens: tuple[str, ...],
    token_set: frozenset[str],
    *,
    name_tokens: tuple[str, ...] = (),
    name_token_set: frozenset[str] = frozenset(),
    is_sport_type: bool = True,
) -> ActivityType | None:
    if not tokens:
        return None

    if normalized in _VALID_ACTIVITY_TYPE_SET:
        return normalized  # type: ignore[return-value]

    combined_tokens = token_set | name_token_set

    # Gravel / Mountain Bike
    if (
        (is_sport_type and compact in _GRAVEL_MTB_COMPACT)
        or bool(token_set & _GRAVEL_MTB_TOKENS)
        or _has_any_phrase(tokens, _GRAVEL_MTB_PHRASES)
    ):
        return "gravel_mtb"

    # Road / Virtual / E-Bike Cycling (with name-based upgrade to gravel_mtb)
    if (is_sport_type and compact in _CYCLING_COMPACT) or bool(token_set & _CYCLING_TOKENS):
        if (
            bool(combined_tokens & _GRAVEL_MTB_UPGRADE_TOKENS)
            or _has_any_phrase(tokens, _GRAVEL_MTB_PHRASES)
            or _has_any_phrase(name_tokens, _GRAVEL_MTB_PHRASES)
        ):
            return "gravel_mtb"
        return "cycling"

    # Trail Running
    if (
        (is_sport_type and compact in _TRAIL_RUN_COMPACT)
        or bool(token_set & _TRAIL_RUN_TOKENS)
        or _has_any_phrase(tokens, _TRAIL_RUN_PHRASES)
    ):
        return "trail_run"

    # Running (with name-based upgrade to trail_run)
    if (is_sport_type and compact in _RUNNING_COMPACT) or bool(token_set & _RUNNING_TOKENS):
        if bool(combined_tokens & _TRAIL_RUN_UPGRADE_TOKENS):
            return "trail_run"
        return "running"

    # Hiking / Snowshoeing / Trekking
    if (is_sport_type and compact in _HIKING_COMPACT) or bool(token_set & _HIKING_TOKENS):
        return "hiking"

    # Walking / Urban / Skating
    if (is_sport_type and compact in _WALKING_COMPACT) or bool(token_set & _WALKING_TOKENS):
        return "walking"

    # Skiing / Snowboarding / Ski Touring
    if (is_sport_type and compact in _SKIING_COMPACT) or bool(token_set & _SKIING_TOKENS):
        return "skiing"

    # Kayaking / Paddling / Rowing / Water craft
    if (is_sport_type and compact in _KAYAK_COMPACT) or bool(token_set & _KAYAK_TOKENS):
        return "kayak"

    # Swimming
    if (is_sport_type and compact in _SWIMMING_COMPACT) or bool(token_set & _SWIMMING_TOKENS):
        return "swimming"

    # Climbing / Mountaineering / Summit
    if (is_sport_type and compact in _CLIMBING_COMPACT) or bool(token_set & _CLIMBING_TOKENS):
        return "climbing"

    # Motorcycle
    if (is_sport_type and compact in _MOTORCYCLE_COMPACT) or bool(token_set & _MOTORCYCLE_TOKENS):
        return "motorcycle"

    # Car / Overland / Bus Transfer
    if (
        (is_sport_type and compact in _CAR_COMPACT)
        or bool(token_set & _CAR_TOKENS)
        or _has_any_phrase(tokens, _CAR_PHRASES)
    ):
        return "car"

    # Train / Rail Transfer
    if (is_sport_type and compact in _TRAIN_COMPACT) or bool(token_set & _TRAIN_TOKENS):
        return "train"

    # Camp / Bivouac / Overnight
    if (is_sport_type and compact in _CAMP_COMPACT) or bool(token_set & _CAMP_TOKENS):
        return "camp"

    return None


def infer_activity_type_from_strava(
    sport_type: str | None,
    activity_name: str | None = None,
    fallback: ActivityType = DEFAULT_ACTIVITY_TYPE,
) -> ActivityType:
    """Infer canonical ActivityType from Strava sport_type/type or GPX name/type hints."""
    sport_norm, sport_compact, sport_tokens, sport_token_set = _tokenize_activity_text(sport_type)
    name_norm, name_compact, name_tokens, name_token_set = _tokenize_activity_text(activity_name)

    if sport_tokens:
        matched = _classify_activity_tokens(
            sport_norm,
            sport_compact,
            sport_tokens,
            sport_token_set,
            name_tokens=name_tokens,
            name_token_set=name_token_set,
            is_sport_type=True,
        )
        if matched is not None:
            return matched

    if name_tokens:
        matched_from_name = _classify_activity_tokens(
            name_norm,
            name_compact,
            name_tokens,
            name_token_set,
            is_sport_type=False,
        )
        if matched_from_name is not None:
            return matched_from_name

    return fallback


StatisticMetric = Literal[
    "date",
    "time",
    "trip-day",
    "elapsed-time",
    "speed",
    "elevation",
    "distance-travelled",
    "distance-remaining",
    "grade",
    "hr",
    "power",
    "vam",
]
PanelPosition = Literal["top", "bottom", "left", "right"]


class TrackPoint(BaseModel):
    """Single sanitized GPS + telemetry observation."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    elev_m: float | None = None
    ts_utc: datetime | None = None
    hr_bpm: int | None = Field(default=None, ge=0, le=260)
    cadence: int | None = Field(default=None, ge=0, le=300)
    watts: int | None = Field(default=None, ge=0, le=3000)
    temp_c: float | None = Field(default=None, ge=-80.0, le=80.0)
    velocity_smooth: float | None = Field(default=None, ge=0.0)
    grade_smooth: float | None = Field(default=None, ge=-100.0, le=100.0)
    moving: bool | None = None
    segment_idx: int = Field(default=0, ge=0)


class IngestWarning(BaseModel):
    """Non-fatal ingestion diagnostic emitted during GPX/FIT/TCX/Strava normalization."""

    code: str
    message: str
    count: int = 1


class ParsedTrack(BaseModel):
    """Normalized track parsed from GPX, FIT, TCX, or Strava streams."""

    name: str = ""
    activity_type_hint: str = ""
    source_format: SourceFormat = "gpx"
    points: list[TrackPoint]
    warnings: list[IngestWarning] = Field(default_factory=list)
    point_count: int
    segment_count: int
    has_timestamps: bool
    has_elevation: bool = False
    is_distance_only: bool = False
    crosses_antimeridian: bool = False
    bounds: tuple[float, float, float, float] | None = None
    velocity_smooth: list[float | None] = Field(default_factory=list)
    grade_smooth: list[float | None] = Field(default_factory=list)
    moving: list[bool | None] = Field(default_factory=list)


class SilverTelemetrySummary(BaseModel):
    """Summary KPIs returned after Polars Silver ETL."""

    trip_id: str
    stage_id: str
    point_count: int
    distance_m: float
    elevation_gain_m: float
    elevation_loss_m: float
    moving_seconds: float
    elapsed_seconds: float
    has_timestamps: bool
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    crosses_antimeridian: bool = False
    is_trimmed: bool = False
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    avg_speed_mps: float = 0.0
    max_speed_mps: float = 0.0
    vam_m_per_h: float = 0.0
    etl_version: int = CURRENT_ETL_VERSION


def _default_metrics() -> list[StatisticMetric]:
    return [
        "trip-day",
        "distance-travelled",
        "speed",
        "elevation",
        "elapsed-time",
    ]


class StatisticsPanelConfig(BaseModel):
    """Telemetry HUD overlay settings matching and extending v1 parity."""

    enabled: bool = True
    position: PanelPosition = "top"
    time_zone: str = "UTC"
    text_size: int = Field(default=24, ge=12, le=72)
    scale: float = Field(default=1.0, ge=0.5, le=2.0)
    text_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    background_color: str = Field(default="#0F172A", pattern=r"^#[0-9A-Fa-f]{6}$")
    background_opacity: float = Field(default=0.75, ge=0.0, le=1.0)
    metrics: list[StatisticMetric] = Field(default_factory=_default_metrics)
    collision_avoidance: bool = True


class CameraKeyframe(BaseModel):
    """Single timeline-anchored camera/pacing cue for Director storyboarding."""

    progress: float = Field(ge=0.0, le=1.0)
    camera_mode: CameraMode = "follow-marker"
    camera_preset: CameraPreset = "dynamic-3d"
    pitch_deg: float = Field(default=45.0, ge=0.0, le=60.0)
    bearing_offset_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    zoom_offset: float = Field(default=0.0, ge=-3.0, le=3.0)
    speed_multiplier: float = Field(default=1.0, ge=0.25, le=4.0)
    label: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_finite_floats(self) -> "CameraKeyframe":
        for field_name in (
            "progress",
            "pitch_deg",
            "bearing_offset_deg",
            "zoom_offset",
            "speed_multiplier",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                raise ValueError(f"CameraKeyframe.{field_name} must be a finite number")
        return self


class RenderSpecV2(BaseModel):
    """Immutable versioned animation specification shared by UI, API, and Worker."""

    version: Literal[2] = 2
    resolution: ResolutionPreset = "1920x1080"
    fps: Literal[30, 60] = 30
    map_style: MapStylePreset = "liberty"
    duration_seconds: float = Field(default=20.0, ge=3.0, le=180.0)
    timeline_mode: TimelineMode = "distance"
    camera_mode: CameraMode = "follow-marker"
    follow_zoom_mode: FollowZoomMode = "adaptive"
    follow_zoom_offset: float = Field(default=0.0, ge=-3.0, le=3.0)
    target_screen_px_per_sec: float = Field(default=180.0, ge=80.0, le=500.0)
    terrain_3d: bool = False
    camera_pitch_deg: float = Field(default=0.0, ge=0.0, le=60.0)
    camera_preset: CameraPreset = "flat"
    camera_bearing_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    visibility_mode: VisibilityMode = "traversed"
    trail_duration_seconds: float = Field(default=4.0, ge=1.0, le=30.0)
    track_color: str = Field(default="#F97316", pattern=r"^#[0-9A-Fa-f]{6}$")
    marker_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    track_width_px: float = Field(default=5.0, ge=2.0, le=16.0)
    units: UnitsMode = "metric"
    intro_hold_seconds: float = Field(default=1.0, ge=0.0, le=3.0)
    outro_hold_seconds: float = Field(default=1.5, ge=0.0, le=3.0)
    show_overnight_stops: bool = True
    show_photos: bool = True
    photo_display_seconds: float = Field(default=3.0, ge=0.5, le=10.0)
    pause_on_photo: bool = False
    elevation_chart_overlay: bool = False
    stage_colors: dict[str, str] = Field(default_factory=dict)
    stage_transitions: dict[str, TransitionIcon] = Field(default_factory=dict)
    statistics_panel: StatisticsPanelConfig = Field(default_factory=StatisticsPanelConfig)
    is_preview: bool = False

    # v2.1 / B1-B8 + A1-A6 extensions (backward-compatible defaults)
    color_by: ColorByMode = "stage"
    terrain_exaggeration: float = Field(default=1.4, ge=0.5, le=3.0)
    elevation_curtain: bool = False
    camera_keyframes: list[CameraKeyframe] = Field(default_factory=list, max_length=40)
    director_preset: DirectorPreset = "none"
    show_chapter_cards: bool = False
    show_transfer_arcs: bool = True
    show_finale_poster: bool = False
    show_climb_callouts: bool = True
    photo_card_style: PhotoCardStyle = "corner"
    telemetry_smoothing: int = Field(default=5, ge=1, le=31)

    @model_validator(mode="after")
    def validate_color_values(self) -> "RenderSpecV2":
        for stage_id, color in self.stage_colors.items():
            if not isinstance(color, str) or not _HEX_COLOR_RE.fullmatch(color):
                raise ValueError(f"Invalid hex color for stage {stage_id}")
        for field_name in (
            "duration_seconds",
            "follow_zoom_offset",
            "target_screen_px_per_sec",
            "camera_pitch_deg",
            "camera_bearing_deg",
            "trail_duration_seconds",
            "track_width_px",
            "intro_hold_seconds",
            "outro_hold_seconds",
            "photo_display_seconds",
            "terrain_exaggeration",
        ):
            val = getattr(self, field_name)
            if not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                raise ValueError(f"RenderSpecV2.{field_name} must be a finite number")
        if self.camera_keyframes:
            self.camera_keyframes = sorted(self.camera_keyframes, key=lambda kf: kf.progress)
        return self


class ClimbHighlight(BaseModel):
    """Categorized climb detected along a stage track."""

    id: str
    stage_id: str
    name: str
    category: ClimbCategory
    start_dist_m: float
    end_dist_m: float
    start_progress: float = Field(ge=0.0, le=1.0)
    end_progress: float = Field(ge=0.0, le=1.0)
    summit_progress: float = Field(ge=0.0, le=1.0)
    summit_lat: float
    summit_lon: float
    summit_elev_m: float
    distance_m: float
    elevation_gain_m: float
    avg_grade_pct: float
    max_grade_pct: float
    fiets_score: float


class MetricSplit(BaseModel):
    """Distance split (e.g. per-km) summary along a stage."""

    split_index: int
    stage_id: str
    start_dist_m: float
    end_dist_m: float
    distance_m: float
    duration_s: float
    elev_gain_m: float
    elev_loss_m: float
    avg_speed_mps: float
    avg_grade_pct: float | None = None
    avg_hr_bpm: float | None = None
    avg_watts: float | None = None


class DwellStop(BaseModel):
    """Stationary dwell-stop cluster (>=180s within 30m) along a stage."""

    id: str
    stage_id: str
    lat: float
    lon: float
    start_dist_m: float
    progress: float = Field(ge=0.0, le=1.0)
    started_at_utc: datetime | None = None
    ended_at_utc: datetime | None = None
    duration_s: float
    suggested_icon: TransitionIcon = "coffee"


class PeakEffort(BaseModel):
    """Peak rolling window effort (60s / 300s / 1200s) for power, speed, or HR."""

    window_s: int
    metric: Literal["watts", "speed_mps", "hr_bpm"]
    value: float
    stage_id: str
    start_dist_m: float
    end_dist_m: float


class StageGeoContext(BaseModel):
    """Spatial geo-enrichment context resolved from reference GeoParquet tables."""

    stage_id: str
    countries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    passes: list[str] = Field(default_factory=list)


class StageEventsBundle(BaseModel):
    """Complete stage analytics bundle cached in `_events.parquet` sidecar."""

    stage_id: str
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    climbs: list[ClimbHighlight] = Field(default_factory=list)
    splits: list[MetricSplit] = Field(default_factory=list)
    dwell_stops: list[DwellStop] = Field(default_factory=list)
    peak_efforts: list[PeakEffort] = Field(default_factory=list)
    geo_context: StageGeoContext


class ColorSegment(BaseModel):
    """Quantized telemetry-colored polyline sub-segment for Deck.gl route ribbons."""

    start_idx: int
    end_idx: int
    color: str
    rgb: tuple[int, int, int]
    bin_label: str
    min_val: float
    max_val: float
