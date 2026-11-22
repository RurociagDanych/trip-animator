"""Deterministic render manifest construction from Silver Parquet via DuckDB + NumPy."""

from __future__ import annotations

import base64
import logging
import math
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, Field
from trip_telemetry import (
    DEFAULT_ACTIVITY_TYPE,
    LOD_TOLERANCES_DEG,
    ActivityType,
    ColorByMode,
    RenderSpecV2,
    TrackPoint,
    bin_track_color_segments,
    extract_stage_highlights,
    normalize_activity_type,
    simplify_track_points,
)
from trip_telemetry.schemas import TransitionIcon

from trip_worker.storage import ObjectStore, materialize_parquet_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhotoRecord:
    """Enabled project photo metadata loaded from PostgreSQL `project_photos`."""

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
    """Ordered stage metadata loaded from PostgreSQL `stages`."""

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
    """Run-length encoded frame capture span `[frame_idx]` repeated `repeat_count` times."""

    frame_idx: int
    repeat_count: int


class ManifestPoint(BaseModel):
    """Single telemetry point serialized into `window.__TRIP_RENDER_MANIFEST__`."""

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
    """Pre-binned quantized RGB track sub-segment for `color_by` ribbon rendering."""

    start_index: int
    end_index: int
    color_rgb: tuple[int, int, int]
    bucket_label: str = ""


class ManifestStage(BaseModel):
    """Stage track, styling, and telemetry KPIs in the render manifest."""

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
    """Categorized climb highlight serialized into `RenderManifest`."""

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
    """Photo stop serialized with either inline data URI or virtual `render-assets.local` URL."""

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


class RenderManifest(BaseModel):
    """Deterministic manifest injected via `window.__TRIP_RENDER_MANIFEST__`."""

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


def _validate_trim(trim_start_s: float | None, trim_end_s: float | None) -> None:
    if trim_start_s is not None and (not math.isfinite(trim_start_s) or trim_start_s < 0.0):
        raise ValueError("trim_start_s must be finite and >= 0")
    if trim_end_s is not None and (not math.isfinite(trim_end_s) or trim_end_s < 0.0):
        raise ValueError("trim_end_s must be finite and >= 0")
    if trim_start_s is not None and trim_end_s is not None and trim_start_s >= trim_end_s:
        raise ValueError("trim_start_s must be strictly less than trim_end_s")


_UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _format_ts_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        utc_dt = _UNIX_EPOCH_UTC + timedelta(microseconds=int(value))
        return utc_dt.isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        utc_dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return utc_dt.isoformat().replace("+00:00", "Z")
    return None


def _fill_stride_indices(
    n: int,
    must_keep: set[int],
    max_points: int,
) -> list[int]:
    """Preserve `must_keep` indices and fill remaining budget up to `max_points` evenly."""
    if max_points <= 0 or n <= 0:
        return []
    if max_points == 1:
        return [0]

    selected_set = set(must_keep)
    if len(selected_set) < max_points:
        pool = [i for i in range(n) if i not in selected_set]
        need = min(len(pool), max_points - len(selected_set))
        if need == 1:
            selected_set.add(pool[len(pool) // 2])
        elif need > 1:
            step = (len(pool) - 1) / (need - 1)
            selected_set.update(pool[round(i * step)] for i in range(need))

    selected = sorted(selected_set)
    if len(selected) > max_points:
        step = (len(selected) - 1) / (max_points - 1)
        selected = [selected[round(i * step)] for i in range(max_points)]
    return selected


def _downsample_manifest_points(
    points: list[ManifestPoint],
    max_points: int,
) -> list[ManifestPoint]:
    """Preserve segment boundaries, geometry, and telemetry density up to `max_points`."""
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


def _extract_numeric_array(
    raw_col: Any,
    *,
    dtype: Any = np.float64,
    fill_value: float = np.nan,
) -> tuple[npt.NDArray[Any], npt.NDArray[np.bool_]]:
    """Convert a DuckDB `.fetchnumpy()` column (including MaskedArray) to `(values, valid_mask)`."""
    if np.ma.isMaskedArray(raw_col):
        mask_raw = np.asarray(raw_col.mask, dtype=bool)
        if mask_raw.ndim == 0:
            mask = np.full(len(raw_col.data), bool(mask_raw), dtype=bool)
        else:
            mask = mask_raw
        data = np.asarray(raw_col.data)
        valid = ~mask
        if np.issubdtype(dtype, np.floating):
            out = np.where(valid, data, fill_value).astype(dtype, copy=False)
            valid = valid & np.isfinite(out)
            out = np.where(valid, out, fill_value)
            return out, valid
        out = np.where(valid, data, fill_value).astype(dtype, copy=False)
        return out, valid

    arr = np.asarray(raw_col)
    if arr.dtype == object:
        valid = np.array([v is not None for v in arr], dtype=bool)
        out = np.array(
            [v if v is not None else fill_value for v in arr],
            dtype=dtype,
        )
        if np.issubdtype(dtype, np.floating):
            valid = valid & np.isfinite(out)
            out = np.where(valid, out, fill_value)
        return out, valid

    out = arr.astype(dtype, copy=False)
    if np.issubdtype(dtype, np.floating):
        valid = np.isfinite(out)
        out = np.where(valid, out, fill_value)
        return out, valid
    return out, np.ones(len(out), dtype=bool)


def vectorized_lttb_indices(
    xs: npt.NDArray[np.float64],
    ys: npt.NDArray[np.float64],
    threshold: int,
    *,
    segment_idx: npt.NDArray[np.int32] | None = None,
) -> list[int]:
    """Vectorized NumPy LTTB downsampling preserving endpoints and segment transitions."""
    n = int(len(xs))
    if threshold <= 0 or n == 0:
        return []
    if n <= threshold:
        return list(range(n))
    if threshold == 1:
        return [0]
    if threshold == 2:
        return [0, n - 1]

    must_keep: set[int] = {0, n - 1}
    if segment_idx is not None and len(segment_idx) == n:
        changes = np.flatnonzero(segment_idx[1:] != segment_idx[:-1])
        for idx in changes:
            must_keep.add(int(idx))
            must_keep.add(int(idx + 1))

    if len(must_keep) >= threshold:
        return _fill_stride_indices(n, must_keep, threshold)

    lttb_target = max(3, threshold - max(0, len(must_keep) - 2))
    selected: list[int] = [0]
    if n > 2 and lttb_target > 2:
        bucket_size = (n - 2) / (lttb_target - 2)
        a_idx = 0
        for bucket in range(lttb_target - 2):
            range_start = int(math.floor(bucket * bucket_size)) + 1
            range_end = min(int(math.floor((bucket + 1) * bucket_size)) + 1, n - 1)
            if range_end <= range_start:
                range_end = min(range_start + 1, n - 1)

            next_start = int(math.floor((bucket + 1) * bucket_size)) + 1
            next_end = (
                n
                if bucket + 1 == lttb_target - 2
                else min(int(math.floor((bucket + 2) * bucket_size)) + 1, n)
            )
            if next_end <= next_start:
                next_start = min(next_start, n - 1)
                next_end = n

            avg_x = float(np.mean(xs[next_start:next_end]))
            avg_y = float(np.mean(ys[next_start:next_end]))
            ax = float(xs[a_idx])
            ay = float(ys[a_idx])

            areas = np.abs(
                (ax - avg_x) * (ys[range_start:range_end] - ay)
                - (ax - xs[range_start:range_end]) * (avg_y - ay)
            )
            best_idx = range_start + int(np.argmax(areas))
            selected.append(best_idx)
            a_idx = best_idx
    selected.append(n - 1)

    merged = sorted(must_keep | set(selected))
    if len(merged) <= threshold:
        return _fill_stride_indices(n, set(merged), threshold)

    # Too many merged indices due to segment boundaries: prioritize must_keep then stride LTTB
    extras = [i for i in selected if i not in must_keep]
    need = threshold - len(must_keep)
    if need <= 0:
        return _fill_stride_indices(n, must_keep, threshold)
    if len(extras) <= need:
        return sorted(must_keep | set(extras))
    step = (len(extras) - 1) / (need - 1) if need > 1 else 0.0
    chosen_extras = (
        {extras[round(i * step)] for i in range(need)} if need > 1 else {extras[len(extras) // 2]}
    )
    return sorted(must_keep | chosen_extras)


def query_stage_kpis_and_manifest_points(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = 12_000,
) -> tuple[dict[str, Any], list[ManifestPoint]]:
    """Single-pass DuckDB `.fetchnumpy()` + vectorized NumPy KPIs and LTTB downsampling."""
    _validate_trim(trim_start_s, trim_end_s)

    con = duckdb.connect(database=":memory:")
    try:
        rel = con.read_parquet(str(parquet_path))
        cols = set(rel.columns)
        con.register("raw_telemetry", rel)

        lon_unwrapped_expr = "lon_unwrapped::DOUBLE" if "lon_unwrapped" in cols else "lon::DOUBLE"
        elapsed_expr = "elapsed_s::DOUBLE" if "elapsed_s" in cols else "NULL::DOUBLE"
        is_moving_expr = (
            "COALESCE(is_moving, TRUE)::BOOLEAN" if "is_moving" in cols else "TRUE::BOOLEAN"
        )
        hr_expr = "hr_bpm::DOUBLE" if "hr_bpm" in cols else "NULL::DOUBLE"
        cadence_expr = "cadence::DOUBLE" if "cadence" in cols else "NULL::DOUBLE"
        watts_expr = "watts::DOUBLE" if "watts" in cols else "NULL::DOUBLE"

        if trim_start_s is None and trim_end_s is None:
            where_clause = "WHERE NOT COALESCE(is_trimmed, FALSE)" if "is_trimmed" in cols else ""
            arrays = con.execute(
                f"""
                SELECT
                    lat::DOUBLE AS lat,
                    lon::DOUBLE AS lon,
                    {lon_unwrapped_expr} AS lon_unwrapped,
                    elev_m::DOUBLE AS elev_m,
                    cum_dist_m::DOUBLE AS cum_dist_m,
                    speed_mps::DOUBLE AS speed_mps,
                    grade_pct::DOUBLE AS grade_pct,
                    {elapsed_expr} AS elapsed_s,
                    EPOCH_US(ts_utc)::BIGINT AS ts_us,
                    segment_idx::INTEGER AS segment_idx,
                    {is_moving_expr} AS is_moving,
                    {hr_expr} AS hr_bpm,
                    {cadence_expr} AS cadence,
                    {watts_expr} AS watts
                FROM raw_telemetry
                {where_clause}
                ORDER BY seq ASC
                """  # noqa: S608
            ).fetchnumpy()
        else:
            p_lon_unwrapped_expr = (
                "p.lon_unwrapped::DOUBLE" if "lon_unwrapped" in cols else "p.lon::DOUBLE"
            )
            p_elapsed_expr = "p.elapsed_s::DOUBLE" if "elapsed_s" in cols else "NULL::DOUBLE"
            p_is_moving_expr = (
                "COALESCE(p.is_moving, TRUE)::BOOLEAN" if "is_moving" in cols else "TRUE::BOOLEAN"
            )
            p_hr_expr = "p.hr_bpm::DOUBLE" if "hr_bpm" in cols else "NULL::DOUBLE"
            p_cadence_expr = "p.cadence::DOUBLE" if "cadence" in cols else "NULL::DOUBLE"
            p_watts_expr = "p.watts::DOUBLE" if "watts" in cols else "NULL::DOUBLE"
            arrays = con.execute(
                f"""
                WITH bounds AS (
                    SELECT MIN(ts_utc) AS min_ts
                    FROM raw_telemetry
                )
                SELECT
                    p.lat::DOUBLE AS lat,
                    p.lon::DOUBLE AS lon,
                    {p_lon_unwrapped_expr} AS lon_unwrapped,
                    p.elev_m::DOUBLE AS elev_m,
                    p.cum_dist_m::DOUBLE AS cum_dist_m,
                    p.speed_mps::DOUBLE AS speed_mps,
                    p.grade_pct::DOUBLE AS grade_pct,
                    {p_elapsed_expr} AS elapsed_s,
                    EPOCH_US(p.ts_utc)::BIGINT AS ts_us,
                    p.segment_idx::INTEGER AS segment_idx,
                    {p_is_moving_expr} AS is_moving,
                    {p_hr_expr} AS hr_bpm,
                    {p_cadence_expr} AS cadence,
                    {p_watts_expr} AS watts
                FROM raw_telemetry AS p
                CROSS JOIN bounds
                WHERE bounds.min_ts IS NOT NULL
                  AND p.ts_utc IS NOT NULL
                  AND (
                      CAST(? AS DOUBLE) IS NULL
                      OR (EPOCH(p.ts_utc) - EPOCH(bounds.min_ts)) >= CAST(? AS DOUBLE)
                  )
                  AND (
                      CAST(? AS DOUBLE) IS NULL
                      OR (EPOCH(p.ts_utc) - EPOCH(bounds.min_ts)) <= CAST(? AS DOUBLE)
                  )
                ORDER BY p.seq ASC
                """,  # noqa: S608
                [trim_start_s, trim_start_s, trim_end_s, trim_end_s],
            ).fetchnumpy()
    finally:
        con.close()

    lat, _ = _extract_numeric_array(arrays["lat"], dtype=np.float64, fill_value=0.0)
    n = int(len(lat))
    if n == 0:
        raise ValueError("Stage Silver Parquet contains zero active telemetry points")

    lon, _ = _extract_numeric_array(arrays["lon"], dtype=np.float64, fill_value=0.0)
    lon_unwrapped, _ = _extract_numeric_array(
        arrays["lon_unwrapped"], dtype=np.float64, fill_value=0.0
    )
    elev_m, elev_valid = _extract_numeric_array(
        arrays["elev_m"], dtype=np.float64, fill_value=np.nan
    )
    cum_dist, dist_valid = _extract_numeric_array(
        arrays["cum_dist_m"], dtype=np.float64, fill_value=0.0
    )
    speed_mps, speed_valid = _extract_numeric_array(
        arrays["speed_mps"], dtype=np.float64, fill_value=0.0
    )
    grade_pct, grade_valid = _extract_numeric_array(
        arrays["grade_pct"], dtype=np.float64, fill_value=np.nan
    )
    elapsed_s, elapsed_valid = _extract_numeric_array(
        arrays["elapsed_s"], dtype=np.float64, fill_value=np.nan
    )
    ts_us, ts_valid = _extract_numeric_array(arrays["ts_us"], dtype=np.int64, fill_value=0)
    segment_idx, _ = _extract_numeric_array(arrays["segment_idx"], dtype=np.int32, fill_value=0)
    is_moving, _ = _extract_numeric_array(arrays["is_moving"], dtype=bool, fill_value=1.0)
    hr_bpm, hr_valid = _extract_numeric_array(arrays["hr_bpm"], dtype=np.float64, fill_value=np.nan)
    cadence, cadence_valid = _extract_numeric_array(
        arrays["cadence"], dtype=np.float64, fill_value=np.nan
    )
    watts, watts_valid = _extract_numeric_array(
        arrays["watts"], dtype=np.float64, fill_value=np.nan
    )

    total_distance_m = (
        float(np.max(cum_dist[dist_valid]) - np.min(cum_dist[dist_valid]))
        if np.any(dist_valid)
        else 0.0
    )
    if n > 1:
        same_seg = segment_idx[1:] == segment_idx[:-1]
        valid_elev_step = same_seg & elev_valid[1:] & elev_valid[:-1]
        delta_elev = np.where(valid_elev_step, elev_m[1:] - elev_m[:-1], 0.0)
        ascent_m = float(np.sum(delta_elev[delta_elev > 0.0]))
        descent_m = float(np.sum(-delta_elev[delta_elev < 0.0]))

        valid_ts_step = same_seg & ts_valid[1:] & ts_valid[:-1]
        delta_sec = np.where(
            valid_ts_step,
            np.maximum((ts_us[1:] - ts_us[:-1]) / 1_000_000.0, 0.0),
            0.0,
        )
        moving_time_s = float(np.sum(np.where(is_moving[1:], delta_sec, 0.0)))
    else:
        ascent_m = 0.0
        descent_m = 0.0
        moving_time_s = 0.0

    if np.any(ts_valid):
        valid_ts_vals = ts_us[ts_valid]
        elapsed_time_s = max(
            float(np.max(valid_ts_vals) - np.min(valid_ts_vals)) / 1_000_000.0, 0.0
        )
    else:
        elapsed_time_s = 0.0

    kpis: dict[str, Any] = {
        "point_count": n,
        "distance_m": total_distance_m,
        "total_distance_m": total_distance_m,
        "ascent_m": round(ascent_m, 2),
        "elevation_gain_m": round(ascent_m, 2),
        "descent_m": round(descent_m, 2),
        "elevation_loss_m": round(descent_m, 2),
        "moving_time_s": round(moving_time_s, 2),
        "elapsed_time_s": round(elapsed_time_s, 2),
    }

    if n > max_points:
        lttb_xs = cum_dist if np.all(dist_valid) else np.arange(n, dtype=np.float64)
        if np.any(elev_valid):
            first_valid_elev = float(elev_m[elev_valid][0])
            lttb_ys = np.where(elev_valid, elev_m, first_valid_elev)
            # Forward-fill missing elevation values for LTTB area calculation
            if not np.all(elev_valid):
                last_v = first_valid_elev
                filled = np.empty(n, dtype=np.float64)
                for idx_e in range(n):
                    if elev_valid[idx_e]:
                        last_v = float(elev_m[idx_e])
                    filled[idx_e] = last_v
                lttb_ys = filled
        else:
            lttb_ys = np.where(speed_valid, speed_mps, 0.0)

        selected_indices = vectorized_lttb_indices(
            lttb_xs, lttb_ys, max_points, segment_idx=segment_idx
        )
    else:
        selected_indices = list(range(n))

    has_trim = trim_start_s is not None or trim_end_s is not None
    first_dist = float(cum_dist[0]) if (has_trim and dist_valid[0]) else 0.0
    first_elapsed = float(elapsed_s[0]) if (has_trim and elapsed_valid[0]) else 0.0

    points: list[ManifestPoint] = []
    for idx in selected_indices:
        cum_dist_val = max(float(cum_dist[idx]) - first_dist, 0.0) if dist_valid[idx] else 0.0
        elapsed_val = (
            round(max(float(elapsed_s[idx]) - first_elapsed, 0.0), 3)
            if elapsed_valid[idx]
            else None
        )
        points.append(
            ManifestPoint(
                lat=round(float(lat[idx]), 6),
                lon=round(float(lon[idx]), 6),
                lon_unwrapped=round(float(lon_unwrapped[idx]), 6),
                elev_m=round(float(elev_m[idx]), 2) if elev_valid[idx] else None,
                cum_dist_m=round(cum_dist_val, 2),
                speed_mps=round(float(speed_mps[idx]), 3) if speed_valid[idx] else 0.0,
                grade_pct=round(float(grade_pct[idx]), 2) if grade_valid[idx] else None,
                hr_bpm=int(round(float(hr_bpm[idx]))) if hr_valid[idx] else None,
                cadence=int(round(float(cadence[idx]))) if cadence_valid[idx] else None,
                watts=int(round(float(watts[idx]))) if watts_valid[idx] else None,
                elapsed_s=elapsed_val,
                ts_utc=_format_ts_iso(int(ts_us[idx])) if ts_valid[idx] else None,
                segment_idx=int(segment_idx[idx]),
            )
        )

    return kpis, points


def query_stage_manifest_points(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = 12_000,
) -> list[ManifestPoint]:
    """Query ordered, trim-filtered telemetry points from Silver Parquet via single-pass DuckDB."""
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
    """Pre-bin quantized RGB track sub-segments for `grade`, `speed`, `hr`, or `watts`."""
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
    """Collapse static intro/outro holds and steady `pause_on_photo` plateaus into FrameSpans."""
    total_frames = max(
        1, total_frames_override if total_frames_override is not None else manifest.total_frames
    )
    eff_start = max(0, frame_start)
    eff_end = total_frames if frame_end is None else min(total_frames, frame_end)
    if eff_start >= eff_end:
        return []

    spec = manifest.spec
    if spec.photo_card_style == "cinematic_stop":
        # Legacy plateau windows cannot describe the browser cinematic schedule.
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
    """Return the deterministic object key for a chunked job's pre-serialized RenderManifest."""
    return f"outputs/manifests/{job_id}.json"


def photo_asset_key(spec: RenderSpecV2, photo: PhotoRecord) -> str:
    """Return the object key to render: full variant for cinematic_stop, else the thumbnail."""
    if spec.photo_card_style == "cinematic_stop" and photo.object_key:
        return photo.object_key
    return photo.thumb_object_key


def build_render_manifest(
    *,
    job_id: uuid.UUID,
    project_id: uuid.UUID,
    project_title: str,
    spec_json: dict[str, object],
    stages: list[StageRecord],
    store: ObjectStore,
    photos: list[PhotoRecord] | None = None,
    max_points_per_stage: int = 12_000,
    max_total_manifest_points: int = 36_000,
    embed_photo_data_uris: bool = True,
    load_assets: bool = True,
    out_photo_assets: dict[str, bytes] | None = None,
) -> RenderManifest:
    """Build a deterministic RenderManifest from DB stage metadata and Silver Parquet blobs."""
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
            parquet_path = materialize_parquet_path(
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
                asset_key = photo_asset_key(spec, photo)
                try:
                    thumb_bytes = store.get_bytes(asset_key)
                except FileNotFoundError:
                    logger.warning(
                        "Photo asset %s missing for project %s; skipping",
                        asset_key,
                        project_id,
                    )
                    continue
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
    )


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
