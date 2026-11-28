from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from trip_telemetry.analytics import bin_track_color_segments, extract_stage_highlights
from trip_telemetry.pipeline import LOD_TOLERANCES_DEG, simplify_track_points
from trip_telemetry.schemas import (
    DEFAULT_ACTIVITY_TYPE,
    ActivityType,
    ColorByMode,
    RenderSpecV2,
    TrackPoint,
    TransitionIcon,
    normalize_activity_type,
)
from trip_telemetry.stage_timeline import (
    fill_stride_indices as _fill_stride_indices,
)
from trip_telemetry.stage_timeline import (
    format_ts_iso as _format_ts_iso,
)
from trip_telemetry.stage_timeline import (
    query_stage_telemetry_rows,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class ByteStore(Protocol):
    def get_bytes(self, key: str) -> bytes: ...


@dataclass(frozen=True)
class PhotoRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    thumb_object_key: str
    width: int
    height: int
    caption: str = ""
    taken_at_utc: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    matched_lat: float | None = None
    matched_lon: float | None = None
    matched_progress: float | None = None
    match_method: str = "unmatched"
    object_key: str = ""


@dataclass(frozen=True)
class StageRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    stage_order: int
    day_index: int
    trim_start_s: float | None
    trim_end_s: float | None
    silver_object_key: str
    distance_m: float
    elevation_gain_m: float
    moving_seconds: float
    elapsed_seconds: float
    has_timestamps: bool
    activity_type: ActivityType = DEFAULT_ACTIVITY_TYPE


@dataclass(frozen=True)
class FrameSpan:
    frame_idx: int
    repeat_count: int


class ManifestPoint(BaseModel):
    lat: float
    lon: float
    lon_unwrapped: float
    elev_m: float | None = None
    cum_dist_m: float
    speed_mps: float
    grade_pct: float | None = None
    hr_bpm: int | None = None
    cadence: int | None = None
    watts: int | None = None
    elapsed_s: float | None = None
    ts_utc: str | None = None
    segment_idx: int = 0


class ManifestColorSegment(BaseModel):
    start_index: int
    end_index: int
    color_rgb: tuple[int, int, int]
    bucket_label: str = ""


class ManifestStage(BaseModel):
    id: str
    name: str
    day_index: int
    stage_order: int
    activity_type: ActivityType = DEFAULT_ACTIVITY_TYPE
    color: str
    transition: TransitionIcon = "none"
    distance_m: float
    elevation_gain_m: float
    moving_seconds: float
    elapsed_seconds: float
    has_timestamps: bool
    points: list[ManifestPoint]
    color_segments: list[ManifestColorSegment] = Field(default_factory=list)


class ManifestClimb(BaseModel):
    id: str
    stage_id: str
    name: str
    category: Literal["HC", "Cat 1", "Cat 2", "Cat 3", "Cat 4"]
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


class ManifestPhoto(BaseModel):
    id: str
    caption: str = ""
    width: int
    height: int
    taken_at_utc: str | None = None
    lat: float | None = None
    lon: float | None = None
    matched_lat: float | None = None
    matched_lon: float | None = None
    matched_progress: float | None = None
    match_method: str = "unmatched"
    data_uri: str = ""
    asset_url: str | None = None


MARKED_TRAILS_PIN_KEY = "marked_trails_pin"


# Same build id layout as the publisher: date plus an optional same-day suffix from 2.
MARKED_TRAILS_BUILD_ID_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}(-[2-9]|-[1-9]\d+)?")
MARKED_TRAILS_DATA_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}([T ][0-9:.+\-Zz]*)?")
MARKED_TRAILS_DATA_DATE_MAX_LENGTH = 40


def marked_trails_pmtiles_key(build_id: str) -> str:
    """Object key of a build's PMTiles file in the trail store."""
    return f"builds/{build_id}/marked-trails.pmtiles"


class MarkedTrailsPin(BaseModel):
    """Marked Trails build chosen when a render was enqueued.

    `base_url` is the store base as configured: an http(s) URL, or a root-relative path that
    the render page resolves against its own origin. The build id, the key and the data date
    are checked because the page builds a request URL from them.
    """

    build_id: str
    pmtiles_key: str
    osm_data_date: str | None = None
    base_url: str

    @field_validator("build_id")
    @classmethod
    def _check_build_id(cls, value: str) -> str:
        if not MARKED_TRAILS_BUILD_ID_PATTERN.fullmatch(value):
            raise ValueError("build_id is not a Marked Trails build id")
        return value

    @field_validator("osm_data_date")
    @classmethod
    def _check_data_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > MARKED_TRAILS_DATA_DATE_MAX_LENGTH or not (
            MARKED_TRAILS_DATA_DATE_PATTERN.fullmatch(value)
        ):
            raise ValueError("osm_data_date must be an ISO date or timestamp")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        if value.startswith("/") and not value.startswith("//") and "\\" not in value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL or a root-relative path")
        return value

    @model_validator(mode="after")
    def _check_key(self) -> MarkedTrailsPin:
        if self.pmtiles_key != marked_trails_pmtiles_key(self.build_id):
            raise ValueError("pmtiles_key does not match build_id")
        return self


def marked_trails_pin_from_spec_json(
    spec_json: dict[str, object], *, trail_types_enabled: bool
) -> MarkedTrailsPin | None:
    """Read the pin the API stored next to the render spec, or None when absent or invalid.

    A malformed pin is dropped so that missing trail data never fails a render.
    """
    raw = spec_json.get(MARKED_TRAILS_PIN_KEY)
    if not trail_types_enabled or not isinstance(raw, dict):
        return None
    try:
        return MarkedTrailsPin.model_validate(raw)
    except ValidationError:
        logger.warning("Ignoring malformed %s in render spec", MARKED_TRAILS_PIN_KEY)
        return None


class RenderManifest(BaseModel):
    job_id: str
    project_id: str
    project_title: str
    spec: RenderSpecV2
    width: int
    height: int
    fps: int
    duration_s: float
    total_frames: int
    is_preview: bool
    stages: list[ManifestStage]
    photos: list[ManifestPhoto] = Field(default_factory=list)
    climbs: list[ManifestClimb] = Field(default_factory=list)
    marked_trails: MarkedTrailsPin | None = None


@dataclass(frozen=True)
class RenderBundle:
    manifest: RenderManifest
    photo_assets: dict[str, bytes]


def sign_render_callback(secret: str, timestamp: int, raw_body: bytes) -> str:
    msg = f"{timestamp}.".encode("ascii") + raw_body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_render_callback_signature(
    secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
    raw_body: bytes,
    *,
    now_ts: int | None = None,
    max_skew_s: int = 300,
) -> bool:
    if not secret or not timestamp_header or not signature_header:
        return False
    ts_clean = timestamp_header.strip()
    if not ts_clean.isascii() or not ts_clean.isdigit():
        return False
    ts = int(ts_clean)
    current = int(time.time()) if now_ts is None else now_ts
    if abs(current - ts) > max_skew_s:
        return False
    expected = sign_render_callback(secret, ts, raw_body)
    return hmac.compare_digest(signature_header.strip(), expected)


def _materialize_parquet_path(
    store: ByteStore,
    silver_object_key: str,
    tmp_dir: Path,
    stage_id: str,
) -> Path:
    resolver = getattr(store, "resolve_existing_path", None)
    if callable(resolver):
        resolved = resolver(silver_object_key)
        if isinstance(resolved, Path):
            return resolved
    target = tmp_dir / f"{stage_id}.parquet"
    target.write_bytes(store.get_bytes(silver_object_key))
    return target


def _downsample_manifest_points(
    points: list[ManifestPoint],
    max_points: int,
) -> list[ManifestPoint]:
    if max_points <= 0 or not points:
        return []
    if len(points) <= max_points:
        return points
    if max_points == 1:
        return [points[0]]

    track_pts = [
        TrackPoint(
            lat=p.lat,
            lon=p.lon,
            elev_m=p.elev_m,
            segment_idx=p.segment_idx,
        )
        for p in points
    ]

    candidate_pts: list[ManifestPoint] = points
    for lod_name in ("medium", "low"):
        simplified = simplify_track_points(track_pts, LOD_TOLERANCES_DEG[lod_name])
        matched_indices: list[int] = []
        src_idx = 0
        for spt in simplified:
            while src_idx < len(points):
                cur = points[src_idx]
                src_idx += 1
                if cur.segment_idx == spt.segment_idx and cur.lat == spt.lat and cur.lon == spt.lon:
                    matched_indices.append(src_idx - 1)
                    break

        if not matched_indices:
            continue

        if len(matched_indices) <= max_points:
            if len(candidate_pts) < len(points) and len(matched_indices) < max(2, max_points // 2):
                break
            must_keep = set(matched_indices) | {0, len(points) - 1}
            for i in range(1, len(points)):
                if points[i].segment_idx != points[i - 1].segment_idx:
                    must_keep.add(i - 1)
                    must_keep.add(i)
            indices = _fill_stride_indices(len(points), must_keep, max_points)
            return [points[i] for i in indices]

        candidate_pts = [points[i] for i in matched_indices]

    n = len(candidate_pts)
    must_keep = {0, n - 1}
    for i in range(1, n):
        if candidate_pts[i].segment_idx != candidate_pts[i - 1].segment_idx:
            must_keep.add(i - 1)
            must_keep.add(i)

    indices = _fill_stride_indices(n, must_keep, max_points)
    return [candidate_pts[i] for i in indices]


def query_stage_kpis_and_manifest_points(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = 12_000,
) -> tuple[dict[str, Any], list[ManifestPoint]]:
    kpis, rows = query_stage_telemetry_rows(
        parquet_path,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        max_points=max_points,
    )
    points = [
        ManifestPoint(
            lat=row.lat,
            lon=row.lon,
            lon_unwrapped=row.lon_unwrapped,
            elev_m=row.elev_m,
            cum_dist_m=row.cum_dist_m,
            speed_mps=row.speed_mps,
            grade_pct=row.grade_pct,
            hr_bpm=row.hr_bpm,
            cadence=row.cadence,
            watts=row.watts,
            elapsed_s=row.elapsed_s,
            ts_utc=row.ts_utc,
            segment_idx=row.segment_idx,
        )
        for row in rows
    ]
    return kpis, points


def query_stage_manifest_points(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = 12_000,
) -> list[ManifestPoint]:
    _, points = query_stage_kpis_and_manifest_points(
        parquet_path,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        max_points=max_points,
    )
    return points


def build_color_segments(
    points: Sequence[ManifestPoint],
    color_by: str,
    fallback_hex: str,
) -> list[ManifestColorSegment]:
    if color_by not in {"grade", "speed", "hr", "watts"} or len(points) < 2:
        return []

    mode: ColorByMode = color_by  # type: ignore[assignment]
    raw_dicts = [
        {
            "grade_pct": p.grade_pct,
            "speed_mps": p.speed_mps,
            "hr_bpm": p.hr_bpm,
            "watts": p.watts,
        }
        for p in points
    ]
    binned = bin_track_color_segments(raw_dicts, mode, fallback_color=fallback_hex)
    return [
        ManifestColorSegment(
            start_index=seg.start_idx,
            end_index=seg.end_idx,
            color_rgb=seg.rgb,
            bucket_label=seg.bin_label,
        )
        for seg in binned
    ]


def compute_frame_render_spans(
    manifest: RenderManifest,
    frame_start: int = 0,
    frame_end: int | None = None,
    *,
    total_frames_override: int | None = None,
) -> list[FrameSpan]:
    total_frames = max(
        1, total_frames_override if total_frames_override is not None else manifest.total_frames
    )
    eff_start = max(0, frame_start)
    eff_end = total_frames if frame_end is None else min(total_frames, frame_end)
    if eff_start >= eff_end:
        return []

    spec = manifest.spec
    if spec.photo_card_style == "cinematic_stop":
        return [FrameSpan(frame_idx=index, repeat_count=1) for index in range(eff_start, eff_end)]
    duration_s = float(manifest.duration_s)
    hold_scale = (
        min(1.0, duration_s / spec.duration_seconds)
        if (manifest.is_preview and spec.duration_seconds > 0)
        else 1.0
    )
    intro_hold = min(spec.intro_hold_seconds * hold_scale, duration_s * 0.35)
    outro_hold = min(spec.outro_hold_seconds * hold_scale, duration_s * 0.35)
    active_duration = max(0.05, duration_s - intro_hold - outro_hold)

    camera_preset = str(getattr(spec, "camera_preset", "flat"))
    photo_card_style = str(getattr(spec, "photo_card_style", "corner"))
    show_finale_poster = bool(getattr(spec, "show_finale_poster", False))
    show_chapter_cards = bool(getattr(spec, "show_chapter_cards", False))

    stops = (
        sorted(
            (p for p in manifest.photos if p.matched_progress is not None),
            key=lambda p: (float(p.matched_progress or 0.0), p.taken_at_utc or "", p.id),
        )
        if spec.show_photos
        else []
    )
    desired_display_s = max(0.2, float(spec.photo_display_seconds or 3.0) * hold_scale)

    pause_windows: list[tuple[int, float, float]] = []
    if (
        spec.pause_on_photo
        and stops
        and photo_card_style != "cinematic"
        and camera_preset != "drone-orbit"
    ):
        max_total_pause_s = active_duration * 0.65
        raw_total_pause_s = len(stops) * desired_display_s
        pause_scale = (
            max_total_pause_s / raw_total_pause_s if raw_total_pause_s > max_total_pause_s else 1.0
        )
        per_pause_s = desired_display_s * pause_scale
        total_pause_s = len(stops) * per_pause_s
        travel_duration_s = max(0.05, active_duration - total_pause_s)
        fade = min(0.22, max(0.2, per_pause_s) * 0.25)

        cursor_time = 0.0
        prev_progress = 0.0
        for idx, stop in enumerate(stops):
            target_progress = max(0.0, min(1.0, float(stop.matched_progress or 0.0)))
            seg_travel_s = max(0.0, target_progress - prev_progress) * travel_duration_s
            cursor_time += seg_travel_s
            plateau_start = intro_hold + cursor_time + fade
            plateau_end = intro_hold + cursor_time + per_pause_s - fade
            if plateau_end > plateau_start:
                pause_windows.append((idx, plateau_start, plateau_end))
            cursor_time += per_pause_s
            prev_progress = target_progress

    non_paused_photo_windows: list[tuple[float, float]] = []
    if not spec.pause_on_photo and stops:
        for stop in stops:
            prog = max(0.0, min(1.0, float(stop.matched_progress or 0.0)))
            trigger_t = intro_hold + prog * active_duration
            non_paused_photo_windows.append((trigger_t, trigger_t + desired_display_s))

    def _has_non_paused_photo(t: float) -> bool:
        return any(w_start <= t <= w_end for w_start, w_end in non_paused_photo_windows)

    def _state_key(frame_idx: int) -> tuple[Any, ...]:
        t = (
            0.0
            if (total_frames <= 1 or duration_s <= 0.0)
            else (frame_idx / (total_frames - 1)) * duration_s
        )
        if (
            intro_hold > 0.0
            and t < intro_hold
            and not show_chapter_cards
            and not _has_non_paused_photo(t)
        ):
            return ("intro",)
        if (
            outro_hold > 0.0
            and t >= duration_s - outro_hold
            and not show_finale_poster
            and not _has_non_paused_photo(t)
        ):
            return ("outro",)
        for pause_idx, p_start, p_end in pause_windows:
            if p_start <= t <= p_end:
                return ("photo_pause", pause_idx)
        return ("frame", frame_idx)

    spans: list[FrameSpan] = []
    cur_key = _state_key(eff_start)
    cur_start = eff_start
    cur_count = 1
    for f_idx in range(eff_start + 1, eff_end):
        key = _state_key(f_idx)
        if key == cur_key and key[0] != "frame":
            cur_count += 1
        else:
            spans.append(FrameSpan(frame_idx=cur_start, repeat_count=cur_count))
            cur_key = key
            cur_start = f_idx
            cur_count = 1
    spans.append(FrameSpan(frame_idx=cur_start, repeat_count=cur_count))
    return spans


def chunk_manifest_key(job_id: uuid.UUID | str) -> str:
    return f"outputs/manifests/{job_id}.json"


def photo_asset_key(spec: RenderSpecV2, photo: PhotoRecord) -> str:
    if spec.photo_card_style == "cinematic_stop" and photo.object_key:
        return photo.object_key
    return photo.thumb_object_key


def load_photo_asset(store: ByteStore, spec: RenderSpecV2, photo: PhotoRecord) -> bytes:
    asset_key = photo_asset_key(spec, photo)
    try:
        return store.get_bytes(asset_key)
    except FileNotFoundError:
        if asset_key != photo.thumb_object_key:
            try:
                data = store.get_bytes(photo.thumb_object_key)
            except FileNotFoundError:
                pass
            else:
                logger.warning(
                    "Photo rendition %s missing; using thumbnail %s",
                    asset_key,
                    photo.thumb_object_key,
                )
                return data
    raise ValueError(
        f"Selected photo {photo.id} has no available render asset "
        f"(tried {asset_key} and {photo.thumb_object_key})"
    )


def build_render_manifest(
    *,
    job_id: uuid.UUID,
    project_id: uuid.UUID,
    project_title: str,
    spec_json: dict[str, object],
    stages: list[StageRecord],
    store: ByteStore,
    photos: list[PhotoRecord] | None = None,
    max_points_per_stage: int = 12_000,
    max_total_manifest_points: int = 36_000,
    embed_photo_data_uris: bool = True,
    load_assets: bool = True,
    out_photo_assets: dict[str, bytes] | None = None,
) -> RenderManifest:
    if not stages:
        raise ValueError("Cannot build render manifest for a project with zero stages")

    spec = RenderSpecV2.model_validate(spec_json)
    width_str, height_str = spec.resolution.split("x", 1)
    width = int(width_str)
    height = int(height_str)

    is_preview = spec.is_preview
    fps = 15 if is_preview else spec.fps
    duration_s = (
        min(float(spec.duration_seconds), 5.0) if is_preview else float(spec.duration_seconds)
    )
    total_frames = max(1, round(duration_s * fps))

    ordered_stages = sorted(stages, key=lambda s: s.stage_order)
    per_stage_budget = min(
        max_points_per_stage,
        max(2, max_total_manifest_points // len(ordered_stages)),
    )
    manifest_stages: list[ManifestStage] = []
    stage_climbs_raw: list[tuple[str, float, float, list[dict[str, Any]]]] = []
    show_climb_callouts = bool(getattr(spec, "show_climb_callouts", True))

    with tempfile.TemporaryDirectory(prefix="trip_manifest_") as tmpdir:
        tmp_root = Path(tmpdir)
        for stage in ordered_stages:
            parquet_path = _materialize_parquet_path(
                store,
                stage.silver_object_key,
                tmp_root,
                str(stage.id),
            )
            kpis, points = query_stage_kpis_and_manifest_points(
                parquet_path,
                trim_start_s=stage.trim_start_s,
                trim_end_s=stage.trim_end_s,
                max_points=per_stage_budget,
            )
            stage_id_str = str(stage.id)
            color = spec.stage_colors.get(stage_id_str, spec.track_color)
            transition: TransitionIcon = spec.stage_transitions.get(stage_id_str, "none")

            manifest_stages.append(
                ManifestStage(
                    id=stage_id_str,
                    name=stage.name,
                    day_index=stage.day_index,
                    stage_order=stage.stage_order,
                    activity_type=normalize_activity_type(
                        stage.activity_type, DEFAULT_ACTIVITY_TYPE
                    ),
                    color=color,
                    transition=transition,
                    distance_m=float(kpis["total_distance_m"]),
                    elevation_gain_m=float(kpis["elevation_gain_m"]),
                    moving_seconds=float(kpis["moving_time_s"]),
                    elapsed_seconds=float(kpis["elapsed_time_s"]),
                    has_timestamps=stage.has_timestamps,
                    points=points,
                )
            )
            if show_climb_callouts:
                climbs_for_stage = _detect_stage_climbs_safe(
                    parquet_path,
                    stage_id=stage_id_str,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )
                if climbs_for_stage:
                    stage_climbs_raw.append(
                        (
                            stage_id_str,
                            float(kpis["total_distance_m"]),
                            float(kpis["elapsed_time_s"]),
                            climbs_for_stage,
                        )
                    )

    total_pts = sum(len(s.points) for s in manifest_stages)
    if total_pts > max_total_manifest_points and manifest_stages:
        min_floor = 2 if max_total_manifest_points >= 2 * len(manifest_stages) else 1
        for ms in manifest_stages:
            share = max(min_floor, int(max_total_manifest_points * (len(ms.points) / total_pts)))
            if len(ms.points) > share:
                ms.points = _downsample_manifest_points(ms.points, max_points=share)
        excess = sum(len(s.points) for s in manifest_stages) - max_total_manifest_points
        if excess > 0:
            for ms in sorted(manifest_stages, key=lambda s: len(s.points), reverse=True):
                if excess <= 0:
                    break
                reducible = len(ms.points) - min_floor
                if reducible > 0:
                    cut = min(excess, reducible)
                    ms.points = _downsample_manifest_points(
                        ms.points, max_points=len(ms.points) - cut
                    )
                    excess -= cut

    color_by = str(getattr(spec, "color_by", "stage"))
    if color_by != "stage":
        for ms in manifest_stages:
            ms.color_segments = build_color_segments(ms.points, color_by, ms.color)

    manifest_climbs: list[ManifestClimb] = []
    if show_climb_callouts and stage_climbs_raw:
        total_trip_dist = sum(max(ms.distance_m, 0.0) for ms in manifest_stages)
        stage_dist_offsets: dict[str, tuple[float, float]] = {}
        cursor_dist = 0.0
        for ms in manifest_stages:
            s_dist = max(ms.distance_m, 0.0)
            stage_dist_offsets[ms.id] = (cursor_dist, s_dist)
            cursor_dist += s_dist

        for stage_id_str, stage_dist, _stage_elapsed, raw_climbs in stage_climbs_raw:
            offset_m, _ = stage_dist_offsets.get(stage_id_str, (0.0, stage_dist))
            denom = total_trip_dist if total_trip_dist > 0.0 else 1.0
            for c in raw_climbs:
                s_prog = max(0.0, min(1.0, (offset_m + float(c["start_dist_m"])) / denom))
                e_prog = max(0.0, min(1.0, (offset_m + float(c["end_dist_m"])) / denom))
                sum_dist = (
                    float(c["summit_progress"]) * stage_dist
                    if "summit_progress" in c
                    else float(c.get("summit_dist_m", c["end_dist_m"]))
                )
                sum_prog = max(0.0, min(1.0, (offset_m + sum_dist) / denom))
                manifest_climbs.append(
                    ManifestClimb(
                        id=str(c["id"]),
                        stage_id=stage_id_str,
                        name=str(c["name"]),
                        category=c["category"],
                        start_dist_m=float(c["start_dist_m"]),
                        end_dist_m=float(c["end_dist_m"]),
                        start_progress=round(s_prog, 6),
                        end_progress=round(e_prog, 6),
                        summit_progress=round(sum_prog, 6),
                        summit_lat=float(c["summit_lat"]),
                        summit_lon=float(c["summit_lon"]),
                        summit_elev_m=float(c["summit_elev_m"]),
                        distance_m=float(c["distance_m"]),
                        elevation_gain_m=float(c["elevation_gain_m"]),
                        avg_grade_pct=float(c["avg_grade_pct"]),
                        max_grade_pct=float(c["max_grade_pct"]),
                        fiets_score=float(c["fiets_score"]),
                    )
                )

    manifest_photos: list[ManifestPhoto] = []
    if spec.show_photos and photos:
        for photo in photos:
            photo_id_str = str(photo.id)
            if load_assets:
                thumb_bytes = load_photo_asset(store, spec, photo)
                if out_photo_assets is not None:
                    out_photo_assets[photo_id_str] = thumb_bytes
                if embed_photo_data_uris:
                    b64_payload = base64.b64encode(thumb_bytes).decode("ascii")
                    data_uri = f"data:image/webp;base64,{b64_payload}"
                    asset_url = None
                else:
                    data_uri = ""
                    asset_url = f"https://render-assets.local/{photo_id_str}"
            else:
                data_uri = ""
                asset_url = (
                    None if embed_photo_data_uris else f"https://render-assets.local/{photo_id_str}"
                )
            manifest_photos.append(
                ManifestPhoto(
                    id=photo_id_str,
                    caption=photo.caption,
                    width=photo.width,
                    height=photo.height,
                    taken_at_utc=_format_ts_iso(photo.taken_at_utc),
                    lat=photo.lat,
                    lon=photo.lon,
                    matched_lat=photo.matched_lat,
                    matched_lon=photo.matched_lon,
                    matched_progress=photo.matched_progress,
                    match_method=photo.match_method,
                    data_uri=data_uri,
                    asset_url=asset_url,
                )
            )

    return RenderManifest(
        job_id=str(job_id),
        project_id=str(project_id),
        project_title=project_title,
        spec=spec,
        width=width,
        height=height,
        fps=fps,
        duration_s=duration_s,
        total_frames=total_frames,
        is_preview=is_preview,
        stages=manifest_stages,
        photos=manifest_photos,
        climbs=manifest_climbs,
        marked_trails=marked_trails_pin_from_spec_json(
            spec_json, trail_types_enabled=bool(spec.marked_trail_types)
        ),
    )


def build_render_bundle(
    *,
    job_id: uuid.UUID,
    project_id: uuid.UUID,
    project_title: str,
    spec_json: dict[str, object],
    stages: list[StageRecord],
    store: ByteStore,
    photos: list[PhotoRecord] | None = None,
    max_points_per_stage: int = 12_000,
    max_total_manifest_points: int = 36_000,
) -> RenderBundle:
    photo_assets: dict[str, bytes] = {}
    manifest = build_render_manifest(
        job_id=job_id,
        project_id=project_id,
        project_title=project_title,
        spec_json=spec_json,
        stages=stages,
        store=store,
        photos=photos,
        max_points_per_stage=max_points_per_stage,
        max_total_manifest_points=max_total_manifest_points,
        embed_photo_data_uris=False,
        load_assets=True,
        out_photo_assets=photo_assets,
    )
    return RenderBundle(manifest=manifest, photo_assets=photo_assets)


def _detect_stage_climbs_safe(
    parquet_path: Path,
    *,
    stage_id: str,
    trim_start_s: float | None,
    trim_end_s: float | None,
) -> list[dict[str, Any]]:
    try:
        highlights = extract_stage_highlights(
            parquet_path,
            stage_id=stage_id,
            trim_start_s=trim_start_s,
            trim_end_s=trim_end_s,
        )
    except Exception:
        logger.debug("Climb extraction skipped for stage %s", stage_id, exc_info=True)
        return []
    return [c.model_dump(mode="python") for c in highlights.climbs]
