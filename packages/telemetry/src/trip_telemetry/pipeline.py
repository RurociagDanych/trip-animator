"""Polars Bronze-to-Silver telemetry ETL, GeoParquet persistence, and multi-LOD geometries."""

from __future__ import annotations

import gzip
import json
import math
import struct
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trip_telemetry.gpx_parser import (
    compute_unwrapped_bounds,
    haversine_distance_m,
    unwrap_longitudes,
)
from trip_telemetry.schemas import (
    CURRENT_ETL_VERSION,
    IngestWarning,
    LodLevel,
    ParsedTrack,
    SilverTelemetrySummary,
    TrackPoint,
)

EARTH_RADIUS_M = 6_371_000.0
MOVING_SPEED_THRESHOLD_MPS = 0.5
TINY_TRACK_CLEANING_THRESHOLD = 15
TELEPORT_MAX_SPEED_MPS = 100.0
TELEPORT_MIN_STEP_M = 500.0
ELEVATION_HYSTERESIS_M = 3.0
LOD_TOLERANCES_DEG: dict[LodLevel, float] = {
    "full": 0.0,
    "medium": 0.0001,
    "low": 0.001,
}
LOD_TOLERANCES_M: dict[LodLevel, float] = {
    "full": 0.0,
    "medium": 8.0,
    "low": 80.0,
}


def _to_finite_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _to_bounded_int(val: Any, low: int, high: int) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num_f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num_f):
        return None
    num = int(round(num_f))
    if low <= num <= high:
        return num
    return None


def _parse_start_date(start_date: datetime | str | None) -> datetime | None:
    if start_date is None:
        return None
    if isinstance(start_date, datetime):
        if start_date.tzinfo is None:
            return start_date.replace(tzinfo=UTC)
        return start_date.astimezone(UTC)
    text = start_date.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _normalize_strava_streams(
    streams: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, list[Any]]:
    """Normalize Strava API stream payloads (keyed dict or list of stream objects)."""
    normalized: dict[str, list[Any]] = {}
    if isinstance(streams, Mapping):
        for key, value in streams.items():
            if isinstance(value, Mapping) and "data" in value and isinstance(value["data"], list):
                normalized[str(key)] = value["data"]
            elif isinstance(value, list):
                normalized[str(key)] = value
    else:
        for item in streams:
            if isinstance(item, Mapping) and "type" in item and isinstance(item.get("data"), list):
                normalized[str(item["type"])] = item["data"]
    return normalized


def _extract_strava_track_and_extras(
    streams: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    start_date: datetime | str | None = None,
    name: str = "",
) -> tuple[ParsedTrack, list[float | None], list[float | None], list[bool | None]]:
    """Parse Strava streams into a ParsedTrack plus aligned optional smoothed streams."""
    raw = _normalize_strava_streams(streams)
    latlng_data = raw.get("latlng")
    if not isinstance(latlng_data, list) or not latlng_data:
        raise ValueError("Strava streams missing non-empty 'latlng' stream")

    time_data = raw.get("time", [])
    altitude_data = raw.get("altitude", [])
    velocity_data = raw.get("velocity_smooth", [])
    hr_data = raw.get("heartrate", [])
    cadence_data = raw.get("cadence", [])
    watts_data = raw.get("watts", [])
    grade_data = raw.get("grade_smooth", [])
    moving_data = raw.get("moving", [])

    parsed_start = _parse_start_date(start_date)
    anchor_dt = parsed_start or datetime(1970, 1, 1, tzinfo=UTC)
    has_time_stream = isinstance(time_data, list) and len(time_data) > 0

    warnings: dict[str, IngestWarning] = {}

    def warn(code: str, message: str) -> None:
        if code in warnings:
            warnings[code].count += 1
        else:
            warnings[code] = IngestWarning(code=code, message=message, count=1)

    points: list[TrackPoint] = []
    vel_col: list[float | None] = []
    grade_col: list[float | None] = []
    moving_col: list[bool | None] = []

    segment_idx = 0
    in_gap = False
    prev_ts: datetime | None = None
    non_monotonic_ts = False

    for idx, coord in enumerate(latlng_data):
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            warn("INVALID_COORD", "Dropped Strava stream sample with invalid latlng")
            if points:
                in_gap = True
            continue
        lat = _to_finite_float(coord[0])
        lon = _to_finite_float(coord[1])
        if lat is None or lon is None or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            warn("INVALID_COORD", "Dropped Strava stream sample with out-of-range coordinates")
            if points:
                in_gap = True
            continue

        if in_gap:
            segment_idx += 1
            in_gap = False

        ts_utc: datetime | None = None
        if has_time_stream and idx < len(time_data):
            t_sec = _to_finite_float(time_data[idx])
            if t_sec is not None and t_sec >= 0.0:
                ts_utc = anchor_dt + timedelta(seconds=t_sec)
                if prev_ts is not None and ts_utc < prev_ts:
                    non_monotonic_ts = True
                prev_ts = ts_utc

        elev_m = _to_finite_float(altitude_data[idx]) if idx < len(altitude_data) else None
        hr_bpm = _to_bounded_int(hr_data[idx], 0, 260) if idx < len(hr_data) else None
        cadence = _to_bounded_int(cadence_data[idx], 0, 300) if idx < len(cadence_data) else None
        watts = _to_bounded_int(watts_data[idx], 0, 3000) if idx < len(watts_data) else None

        vel_raw = _to_finite_float(velocity_data[idx]) if idx < len(velocity_data) else None
        vel_smooth = vel_raw if (vel_raw is not None and vel_raw >= 0.0) else None

        grade_raw = _to_finite_float(grade_data[idx]) if idx < len(grade_data) else None
        grade_smooth = max(min(grade_raw, 100.0), -100.0) if grade_raw is not None else None

        mov_raw = moving_data[idx] if idx < len(moving_data) else None
        mov_val = bool(mov_raw) if isinstance(mov_raw, bool) else None

        points.append(
            TrackPoint(
                lat=lat,
                lon=lon,
                elev_m=elev_m,
                ts_utc=ts_utc,
                hr_bpm=hr_bpm,
                cadence=cadence,
                watts=watts,
                velocity_smooth=vel_smooth,
                grade_smooth=grade_smooth,
                moving=mov_val,
                segment_idx=segment_idx,
            )
        )
        vel_col.append(vel_smooth)
        grade_col.append(grade_smooth)
        moving_col.append(mov_val)

    if not points:
        raise ValueError("Strava streams contain zero valid latlng points")

    has_timestamps = all(pt.ts_utc is not None for pt in points)
    has_elevation = any(pt.elev_m is not None for pt in points)
    if not has_timestamps:
        warn(
            "MISSING_TIMESTAMPS",
            "Strava streams lack complete time array; distance timeline used",
        )
    if not has_elevation:
        warn("MISSING_ELEVATION", "Strava streams lack altitude data")
    if non_monotonic_ts:
        warn("NON_MONOTONIC_TIME", "Strava time stream is not strictly monotonic")

    bounds, crosses_antimeridian = compute_unwrapped_bounds(points)
    parsed = ParsedTrack(
        name=name[:200],
        points=points,
        warnings=list(warnings.values()),
        point_count=len(points),
        segment_count=len({pt.segment_idx for pt in points}),
        has_timestamps=has_timestamps,
        has_elevation=has_elevation,
        is_distance_only=not has_timestamps,
        crosses_antimeridian=crosses_antimeridian,
        bounds=bounds,
        velocity_smooth=vel_col,
        grade_smooth=grade_col,
        moving=moving_col,
    )
    return parsed, vel_col, grade_col, moving_col


def parse_strava_streams(
    streams: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    start_date: datetime | str | None = None,
    name: str = "",
) -> ParsedTrack:
    """Convert raw Strava activity streams into a canonical ParsedTrack."""
    parsed, _, _, _ = _extract_strava_track_and_extras(streams, start_date=start_date, name=name)
    return parsed


def _validate_trim_bounds(trim_start_s: float | None, trim_end_s: float | None) -> None:
    if trim_start_s is not None:
        if not math.isfinite(trim_start_s) or trim_start_s < 0.0:
            raise ValueError("trim_start_s must be a finite non-negative number")
    if trim_end_s is not None:
        if not math.isfinite(trim_end_s) or trim_end_s < 0.0:
            raise ValueError("trim_end_s must be a finite non-negative number")
    if trim_start_s is not None and trim_end_s is not None and trim_start_s >= trim_end_s:
        raise ValueError("trim_start_s must be strictly less than trim_end_s")


def _compute_summary_from_df(
    df: pl.DataFrame,
    *,
    trip_id: str,
    stage_id: str,
    has_timestamps: bool,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> SilverTelemetrySummary:
    """Compute SilverTelemetrySummary over active (~is_trimmed) rows."""
    active = df.filter(~pl.col("is_trimmed")) if "is_trimmed" in df.columns else df
    if active.height == 0:
        raise ValueError("Trim window excludes all telemetry points")

    # Exclude leading boundary delta entering the first active row when trimmed
    active_with_idx = active.with_row_index("_active_row")
    not_first_active = pl.col("_active_row") > 0
    same_seg_active = (pl.col("segment_idx") == pl.col("segment_idx").shift(1)).fill_null(False)
    valid_step = not_first_active & same_seg_active

    delta_elev = (
        pl.when(
            valid_step & pl.col("elev_m").is_not_null() & pl.col("elev_m").shift(1).is_not_null()
        )
        .then(pl.col("elev_m") - pl.col("elev_m").shift(1))
        .otherwise(0.0)
    )

    delta_sec = (
        pl.when(
            valid_step & pl.col("ts_utc").is_not_null() & pl.col("ts_utc").shift(1).is_not_null()
        )
        .then(
            (
                (pl.col("ts_utc") - pl.col("ts_utc").shift(1)).dt.total_microseconds() / 1_000_000.0
            ).clip(lower_bound=0.0)
        )
        .otherwise(0.0)
    )

    stats = active_with_idx.select(
        pl.len().alias("point_count"),
        (pl.col("cum_dist_m").max() - pl.col("cum_dist_m").min()).alias("distance_m"),
        pl.when(delta_elev > 0.0).then(delta_elev).otherwise(0.0).sum().alias("elev_gain"),
        pl.when(delta_elev < 0.0).then(delta_elev.abs()).otherwise(0.0).sum().alias("elev_loss"),
        pl.when(pl.col("is_moving")).then(delta_sec).otherwise(0.0).sum().alias("moving_sec"),
        pl.when(pl.col("ts_utc").is_not_null().all())
        .then(
            (
                (pl.col("ts_utc").max() - pl.col("ts_utc").min()).dt.total_microseconds()
                / 1_000_000.0
            ).clip(lower_bound=0.0)
        )
        .otherwise(0.0)
        .alias("elapsed_sec"),
        pl.col("lat").min().alias("min_lat"),
        pl.col("lat").max().alias("max_lat"),
        pl.col("speed_mps").max().alias("max_speed_mps"),
    ).to_dicts()[0]

    lons = active.get_column("lon").to_list()
    unwrapped_lons, crosses_antimeridian = unwrap_longitudes(lons)
    min_lon = float(min(unwrapped_lons))
    max_lon = float(max(unwrapped_lons))

    distance_m = float(stats["distance_m"] or 0.0)
    elev_gain = float(stats["elev_gain"] or 0.0)
    elev_loss = float(stats["elev_loss"] or 0.0)
    moving_sec = float(stats["moving_sec"] or 0.0)
    elapsed_sec = float(stats["elapsed_sec"] or 0.0)
    max_speed_mps = float(stats["max_speed_mps"] or 0.0)
    avg_speed_mps = (distance_m / elapsed_sec) if elapsed_sec > 0.0 else 0.0
    vam_m_per_h = (
        (elev_gain / moving_sec) * 3600.0
        if moving_sec > 0.0
        else ((elev_gain / elapsed_sec) * 3600.0 if elapsed_sec > 0.0 else 0.0)
    )

    return SilverTelemetrySummary(
        trip_id=trip_id,
        stage_id=stage_id,
        point_count=int(stats["point_count"]),
        distance_m=distance_m,
        elevation_gain_m=round(elev_gain, 2),
        elevation_loss_m=round(elev_loss, 2),
        moving_seconds=round(moving_sec, 2),
        elapsed_seconds=round(elapsed_sec, 2),
        has_timestamps=has_timestamps,
        min_lat=float(stats["min_lat"]),
        max_lat=float(stats["max_lat"]),
        min_lon=min_lon,
        max_lon=max_lon,
        crosses_antimeridian=crosses_antimeridian,
        is_trimmed=(trim_start_s is not None or trim_end_s is not None),
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        avg_speed_mps=round(avg_speed_mps, 4),
        max_speed_mps=round(max_speed_mps, 4),
        vam_m_per_h=round(vam_m_per_h, 2),
    )


def apply_non_destructive_trim(
    df: pl.DataFrame,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> tuple[pl.DataFrame, SilverTelemetrySummary]:
    """Mark rows outside [trim_start_s, trim_end_s] as is_trimmed=True without deleting rows."""
    _validate_trim_bounds(trim_start_s, trim_end_s)

    if df.height == 0:
        raise ValueError("Cannot trim an empty Silver DataFrame")

    trip_id = str(df.get_column("trip_id")[0])
    stage_id = str(df.get_column("stage_id")[0])
    has_timestamps = bool(df.get_column("ts_utc").is_not_null().all())

    if trim_start_s is None and trim_end_s is None:
        trimmed_df = df.with_columns(pl.lit(False).alias("is_trimmed"))
        summary = _compute_summary_from_df(
            trimmed_df,
            trip_id=trip_id,
            stage_id=stage_id,
            has_timestamps=has_timestamps,
        )
        return trimmed_df, summary

    if not has_timestamps:
        raise ValueError("Cannot apply time-based trim to a track without complete timestamps")

    if "elapsed_s" not in df.columns:
        elapsed_expr = (
            (pl.col("ts_utc") - pl.col("ts_utc").min()).dt.total_microseconds() / 1_000_000.0
        ).alias("elapsed_s")
        df = df.with_columns(elapsed_expr)

    out_of_window = pl.lit(False)
    if trim_start_s is not None:
        out_of_window = out_of_window | (pl.col("elapsed_s") < trim_start_s)
    if trim_end_s is not None:
        out_of_window = out_of_window | (pl.col("elapsed_s") > trim_end_s)

    trimmed_df = df.with_columns(out_of_window.alias("is_trimmed"))
    summary = _compute_summary_from_df(
        trimmed_df,
        trip_id=trip_id,
        stage_id=stage_id,
        has_timestamps=has_timestamps,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    return trimmed_df, summary


def _normalize_split_half(
    half: pl.DataFrame,
    *,
    trip_id: str,
    stage_id: str,
) -> tuple[pl.DataFrame, SilverTelemetrySummary]:
    """Re-normalize sequence, distance, elapsed time, and unwrapped longitudes for a split half."""
    n = half.height
    unwrapped_lons, _ = unwrap_longitudes(half.get_column("lon").to_list())
    base_cum_dist = float(half.get_column("cum_dist_m")[0])
    half_has_timestamps = bool(half.get_column("ts_utc").is_not_null().all())

    normalized = half.with_columns(
        pl.lit(trip_id).cast(pl.Utf8).alias("trip_id"),
        pl.lit(stage_id).cast(pl.Utf8).alias("stage_id"),
        pl.Series("seq", list(range(n)), dtype=pl.Int64),
        pl.Series("lon_unwrapped", unwrapped_lons, dtype=pl.Float64),
        pl.when(pl.int_range(0, n) == 0)
        .then(0.0)
        .otherwise(pl.col("delta_dist_m"))
        .cast(pl.Float64)
        .alias("delta_dist_m"),
        (pl.col("cum_dist_m") - base_cum_dist)
        .clip(lower_bound=0.0)
        .cast(pl.Float64)
        .alias("cum_dist_m"),
        pl.when(pl.col("ts_utc").is_not_null())
        .then(
            (
                (pl.col("ts_utc") - pl.col("ts_utc").min()).dt.total_microseconds() / 1_000_000.0
            ).clip(lower_bound=0.0)
        )
        .otherwise(None)
        .cast(pl.Float64)
        .alias("elapsed_s"),
        pl.lit(False).alias("is_trimmed"),
    )
    summary = _compute_summary_from_df(
        normalized,
        trip_id=trip_id,
        stage_id=stage_id,
        has_timestamps=half_has_timestamps,
    )
    return normalized, summary


def split_silver_dataframe(
    df: pl.DataFrame,
    *,
    trip_id: str,
    part1_stage_id: str,
    part2_stage_id: str,
    split_distance_m: float | None = None,
    split_elapsed_s: float | None = None,
) -> tuple[pl.DataFrame, SilverTelemetrySummary, pl.DataFrame, SilverTelemetrySummary]:
    """Split a Silver telemetry DataFrame into two re-normalized halves by distance or elapsed s."""
    if (split_distance_m is None) == (split_elapsed_s is None):
        raise ValueError("Provide exactly one of split_distance_m or split_elapsed_s")
    if split_distance_m is not None and (
        not math.isfinite(split_distance_m) or split_distance_m <= 0.0
    ):
        raise ValueError("split_distance_m must be a finite positive number")
    if split_elapsed_s is not None and (
        not math.isfinite(split_elapsed_s) or split_elapsed_s <= 0.0
    ):
        raise ValueError("split_elapsed_s must be a finite positive number")

    active = df.filter(~pl.col("is_trimmed")) if "is_trimmed" in df.columns else df
    if active.height < 4:
        raise ValueError("Stage must have at least 4 active points to split")

    has_timestamps = bool(active.get_column("ts_utc").is_not_null().all())

    if split_distance_m is not None:
        base_dist = float(active.get_column("cum_dist_m")[0])
        cum_dists = active.get_column("cum_dist_m").to_list()
        split_idx: int | None = None
        for idx, dist_val in enumerate(cum_dists):
            if float(dist_val) - base_dist > split_distance_m:
                split_idx = idx
                break
    else:
        assert split_elapsed_s is not None
        if not has_timestamps:
            raise ValueError("Cannot split by elapsed time on a stage without timestamps")
        timestamps = active.get_column("ts_utc").to_list()
        base_ts = timestamps[0]
        split_idx = None
        for idx, ts_val in enumerate(timestamps):
            if (ts_val - base_ts).total_seconds() > split_elapsed_s:
                split_idx = idx
                break

    if split_idx is None or split_idx < 2 or (active.height - split_idx) < 2:
        raise ValueError("Split point must leave at least 2 points in both halves")

    part1_raw = active.slice(0, split_idx)
    part2_raw = active.slice(split_idx)

    part1_df, part1_summary = _normalize_split_half(
        part1_raw,
        trip_id=trip_id,
        stage_id=part1_stage_id,
    )
    part2_df, part2_summary = _normalize_split_half(
        part2_raw,
        trip_id=trip_id,
        stage_id=part2_stage_id,
    )
    return part1_df, part1_summary, part2_df, part2_summary


def _is_teleport_jump(prev_pt: TrackPoint, cur_pt: TrackPoint) -> bool:
    if prev_pt.segment_idx != cur_pt.segment_idx:
        return False
    if prev_pt.ts_utc is None or cur_pt.ts_utc is None:
        return False
    dt_s = (cur_pt.ts_utc - prev_pt.ts_utc).total_seconds()
    if dt_s <= 0.0:
        return False
    step_m = haversine_distance_m(prev_pt.lat, prev_pt.lon, cur_pt.lat, cur_pt.lon)
    return step_m > TELEPORT_MIN_STEP_M and (step_m / dt_s) > TELEPORT_MAX_SPEED_MPS


def _reject_gps_teleports(
    pts: list[TrackPoint],
    vel_smooth: list[float | None],
    grade_smooth: list[float | None],
    strava_moving: list[bool | None],
) -> tuple[list[TrackPoint], list[float | None], list[float | None], list[bool | None]]:
    """Filter isolated GPS teleportation spikes (>100 m/s and >500 m) on tracks >=15 pts."""
    if len(pts) < TINY_TRACK_CLEANING_THRESHOLD:
        return pts, vel_smooth, grade_smooth, strava_moving

    start_idx = 0
    if (
        len(pts) >= 3
        and _is_teleport_jump(pts[0], pts[1])
        and not _is_teleport_jump(pts[1], pts[2])
    ):
        start_idx = 1

    kept_pts: list[TrackPoint] = [pts[start_idx]]
    kept_vel: list[float | None] = [vel_smooth[start_idx]]
    kept_grade: list[float | None] = [grade_smooth[start_idx]]
    kept_mov: list[bool | None] = [strava_moving[start_idx]]

    for idx in range(start_idx + 1, len(pts)):
        if _is_teleport_jump(kept_pts[-1], pts[idx]):
            continue
        kept_pts.append(pts[idx])
        kept_vel.append(vel_smooth[idx])
        kept_grade.append(grade_smooth[idx])
        kept_mov.append(strava_moving[idx])

    return kept_pts, kept_vel, kept_grade, kept_mov


def _smooth_elev_array(vals: np.ndarray) -> np.ndarray:
    """Apply 5-point rolling median, 7-point Savitzky-Golay, and 3m directional hysteresis."""
    n = len(vals)
    if n < TINY_TRACK_CLEANING_THRESHOLD:
        return vals

    # 1) 5-point centered rolling median (edge-clamped)
    padded_med = np.pad(vals, 2, mode="edge")
    windows_med = np.lib.stride_tricks.sliding_window_view(padded_med, 5)
    med = np.median(windows_med, axis=1)

    # 2) 7-point quadratic/cubic Savitzky-Golay smoothing (antisymmetric linear boundary pad)
    sg_coeffs = np.array([-2.0, 3.0, 6.0, 7.0, 6.0, 3.0, -2.0], dtype=np.float64) / 21.0
    left_pad = 2.0 * med[0] - med[3:0:-1]
    right_pad = 2.0 * med[-1] - med[-2:-5:-1]
    padded_sg = np.concatenate([left_pad, med, right_pad])
    windows_sg = np.lib.stride_tricks.sliding_window_view(padded_sg, 7)
    sg = windows_sg @ sg_coeffs

    # 3) 3m directional hysteresis deadband
    out = np.empty(n, dtype=np.float64)
    anchor = float(sg[0])
    direction = 0
    out[0] = round(anchor, 3)
    for idx in range(1, n):
        sample = float(sg[idx])
        if direction == 0:
            if sample >= anchor + ELEVATION_HYSTERESIS_M:
                direction = 1
                anchor = sample
            elif sample <= anchor - ELEVATION_HYSTERESIS_M:
                direction = -1
                anchor = sample
        elif direction == 1:
            if sample > anchor:
                anchor = sample
            elif sample <= anchor - ELEVATION_HYSTERESIS_M:
                direction = -1
                anchor = sample
        else:
            if sample < anchor:
                anchor = sample
            elif sample >= anchor + ELEVATION_HYSTERESIS_M:
                direction = 1
                anchor = sample
        out[idx] = round(anchor, 3)
    return out


def _clean_track_elevations(pts: list[TrackPoint]) -> list[float | None]:
    """Smooth elevations per segment (>=15 valid samples) while keeping tiny tracks exact."""
    raw_elevs: list[float | None] = [p.elev_m for p in pts]
    if len(pts) < TINY_TRACK_CLEANING_THRESHOLD:
        return raw_elevs

    cleaned: list[float | None] = list(raw_elevs)
    seg_indices: list[int] = []
    current_seg: int | None = None

    def _flush_segment(indices: list[int]) -> None:
        valid_pos = [
            pos
            for pos in indices
            if raw_elevs[pos] is not None and math.isfinite(float(raw_elevs[pos]))  # type: ignore[arg-type]
        ]
        if len(valid_pos) < TINY_TRACK_CLEANING_THRESHOLD:
            return
        arr = np.array([float(raw_elevs[pos]) for pos in valid_pos], dtype=np.float64)  # type: ignore[arg-type]
        smoothed = _smooth_elev_array(arr)
        for pos, val in zip(valid_pos, smoothed.tolist(), strict=True):
            cleaned[pos] = float(val)

    for idx, pt in enumerate(pts):
        if current_seg is None or pt.segment_idx != current_seg:
            if seg_indices:
                _flush_segment(seg_indices)
            current_seg = pt.segment_idx
            seg_indices = [idx]
        else:
            seg_indices.append(idx)
    if seg_indices:
        _flush_segment(seg_indices)

    return cleaned


def build_silver_dataframe(
    parsed: ParsedTrack | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    trip_id: str,
    stage_id: str,
    start_date: datetime | str | None = None,
    name: str = "",
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    clean_signals: bool = True,
) -> tuple[pl.DataFrame, SilverTelemetrySummary]:
    """Transform a ParsedTrack or raw Strava streams into canonical Silver telemetry rows."""
    _validate_trim_bounds(trim_start_s, trim_end_s)

    if isinstance(parsed, ParsedTrack):
        track = parsed
        n_pts = len(track.points)
        if len(track.velocity_smooth) == n_pts:
            vel_smooth: list[float | None] = [
                v if v is not None else p.velocity_smooth
                for v, p in zip(track.velocity_smooth, track.points, strict=True)
            ]
        else:
            vel_smooth = [p.velocity_smooth for p in track.points]

        if len(track.grade_smooth) == n_pts:
            grade_smooth: list[float | None] = [
                g if g is not None else p.grade_smooth
                for g, p in zip(track.grade_smooth, track.points, strict=True)
            ]
        else:
            grade_smooth = [p.grade_smooth for p in track.points]

        if len(track.moving) == n_pts:
            strava_moving: list[bool | None] = [
                m if m is not None else p.moving
                for m, p in zip(track.moving, track.points, strict=True)
            ]
        else:
            strava_moving = [p.moving for p in track.points]
    else:
        track, vel_smooth, grade_smooth, strava_moving = _extract_strava_track_and_extras(
            parsed,
            start_date=start_date,
            name=name,
        )

    pts = list(track.points)
    if not pts:
        raise ValueError("Cannot build Silver DataFrame from zero trackpoints")

    if clean_signals:
        pts, vel_smooth, grade_smooth, strava_moving = _reject_gps_teleports(
            pts, vel_smooth, grade_smooth, strava_moving
        )
        cleaned_elevs = _clean_track_elevations(pts)
    else:
        cleaned_elevs = [p.elev_m for p in pts]

    n = len(pts)
    raw_elevs = [p.elev_m for p in pts]

    base_df = pl.DataFrame(
        {
            "trip_id": pl.Series("trip_id", [trip_id] * n, dtype=pl.Utf8),
            "stage_id": pl.Series("stage_id", [stage_id] * n, dtype=pl.Utf8),
            "seq": pl.Series("seq", list(range(n)), dtype=pl.Int64),
            "ts_utc": pl.Series("ts_utc", [p.ts_utc for p in pts], dtype=pl.Datetime("us", "UTC")),
            "lat": pl.Series("lat", [p.lat for p in pts], dtype=pl.Float64),
            "lon": pl.Series("lon", [p.lon for p in pts], dtype=pl.Float64),
            "elev_raw_m": pl.Series("elev_raw_m", raw_elevs, dtype=pl.Float64),
            "elev_m": pl.Series("elev_m", cleaned_elevs, dtype=pl.Float64),
            "hr_bpm": pl.Series("hr_bpm", [p.hr_bpm for p in pts], dtype=pl.Int32),
            "cadence": pl.Series("cadence", [p.cadence for p in pts], dtype=pl.Int32),
            "watts": pl.Series("watts", [p.watts for p in pts], dtype=pl.Int32),
            "temp_c": pl.Series("temp_c", [p.temp_c for p in pts], dtype=pl.Float64),
            "segment_idx": pl.Series("segment_idx", [p.segment_idx for p in pts], dtype=pl.Int32),
            "_vel_smooth": pl.Series("_vel_smooth", vel_smooth, dtype=pl.Float64),
            "_grade_smooth": pl.Series("_grade_smooth", grade_smooth, dtype=pl.Float64),
            "_strava_moving": pl.Series("_strava_moving", strava_moving, dtype=pl.Boolean),
        }
    )

    same_seg = (pl.col("segment_idx") == pl.col("segment_idx").shift(1)).fill_null(False)
    dlon_raw = pl.col("lon") - pl.col("lon").shift(1)
    dlon_wrapped = (
        pl.when(dlon_raw > 180.0)
        .then(dlon_raw - 360.0)
        .when(dlon_raw < -180.0)
        .then(dlon_raw + 360.0)
        .otherwise(dlon_raw)
    )

    lat_rad = pl.col("lat").radians()
    prev_lat_rad = pl.col("lat").shift(1).radians()
    dphi = (pl.col("lat") - pl.col("lat").shift(1)).radians()
    dlambda = dlon_wrapped.radians()
    hav_a = (
        (dphi / 2.0).sin().pow(2)
        + prev_lat_rad.cos() * lat_rad.cos() * (dlambda / 2.0).sin().pow(2)
    ).clip(0.0, 1.0)
    hav_dist_m = 2.0 * EARTH_RADIUS_M * pl.arctan2(hav_a.sqrt(), (1.0 - hav_a).sqrt())

    step_df = base_df.with_columns(
        (pl.col("lon").first() + dlon_wrapped.fill_null(0.0).cum_sum()).alias("lon_unwrapped"),
        pl.when(same_seg).then(hav_dist_m.fill_null(0.0)).otherwise(0.0).alias("delta_dist_m"),
        pl.when(same_seg & pl.col("ts_utc").is_not_null() & pl.col("ts_utc").shift(1).is_not_null())
        .then(
            (
                (pl.col("ts_utc") - pl.col("ts_utc").shift(1)).dt.total_microseconds() / 1_000_000.0
            ).clip(lower_bound=0.0)
        )
        .otherwise(0.0)
        .alias("_delta_sec"),
        pl.when(same_seg & pl.col("elev_m").is_not_null() & pl.col("elev_m").shift(1).is_not_null())
        .then(pl.col("elev_m") - pl.col("elev_m").shift(1))
        .otherwise(None)
        .alias("_delta_elev_m"),
    )

    computed_speed = (
        pl.when(same_seg & (pl.col("_delta_sec") > 0.0))
        .then(pl.col("delta_dist_m") / pl.col("_delta_sec"))
        .otherwise(0.0)
    )
    computed_grade = (
        pl.when(same_seg & pl.col("_delta_elev_m").is_not_null() & (pl.col("delta_dist_m") >= 1.0))
        .then(((pl.col("_delta_elev_m") / pl.col("delta_dist_m")) * 100.0).clip(-100.0, 100.0))
        .otherwise(None)
    )

    enriched_df = (
        step_df.with_columns(
            pl.col("delta_dist_m").cum_sum().alias("cum_dist_m"),
            pl.coalesce([pl.col("_vel_smooth"), computed_speed]).alias("speed_mps"),
            pl.coalesce([pl.col("_grade_smooth"), computed_grade]).alias("grade_pct"),
            pl.when(pl.col("ts_utc").is_not_null())
            .then((pl.col("ts_utc") - pl.col("ts_utc").min()).dt.total_microseconds() / 1_000_000.0)
            .otherwise(None)
            .cast(pl.Float64)
            .alias("elapsed_s"),
        )
        .with_columns(
            pl.coalesce(
                [
                    pl.col("_strava_moving"),
                    pl.col("speed_mps") >= MOVING_SPEED_THRESHOLD_MPS,
                ]
            ).alias("is_moving"),
            pl.lit(False).alias("is_trimmed"),
        )
        .select(
            [
                "trip_id",
                "stage_id",
                "seq",
                "ts_utc",
                "lat",
                "lon",
                "lon_unwrapped",
                "elev_raw_m",
                "elev_m",
                "delta_dist_m",
                "cum_dist_m",
                "speed_mps",
                "grade_pct",
                "hr_bpm",
                "cadence",
                "watts",
                "temp_c",
                "segment_idx",
                "is_moving",
                "elapsed_s",
                "is_trimmed",
            ]
        )
    )

    if trim_start_s is not None or trim_end_s is not None:
        return apply_non_destructive_trim(
            enriched_df,
            trim_start_s=trim_start_s,
            trim_end_s=trim_end_s,
        )

    summary = _compute_summary_from_df(
        enriched_df,
        trip_id=trip_id,
        stage_id=stage_id,
        has_timestamps=track.has_timestamps,
    )
    return enriched_df, summary


def build_silver_from_strava(
    streams: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    trip_id: str,
    stage_id: str,
    start_date: datetime | str | None = None,
    name: str = "",
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> tuple[pl.DataFrame, SilverTelemetrySummary]:
    """Convenience wrapper to ingest Strava activity streams into Silver DataFrame + KPIs."""
    return build_silver_dataframe(
        streams,
        trip_id=trip_id,
        stage_id=stage_id,
        start_date=start_date,
        name=name,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )


_WKB_POINT_DTYPE = np.dtype(
    [("bo", "<u1"), ("wkb_type", "<u4"), ("lon", "<f8"), ("lat", "<f8")],
    align=False,
)


def _encode_wkb_point(lon: float, lat: float) -> bytes:
    """Encode a 2D WGS84 Point in little-endian Well-Known Binary (WKB)."""
    return struct.pack("<BIdd", 1, 1, lon, lat)


def _encode_wkb_points_arrow(lons: Sequence[float], lats: Sequence[float]) -> pa.Array:
    """Vectorized NumPy->PyArrow WKB Point encoder."""
    n = len(lons)
    if n == 0:
        return pa.array([], type=pa.binary())
    wkb_arr = np.empty(n, dtype=_WKB_POINT_DTYPE)
    wkb_arr["bo"] = 1
    wkb_arr["wkb_type"] = 1
    wkb_arr["lon"] = np.asarray(lons, dtype=np.float64)
    wkb_arr["lat"] = np.asarray(lats, dtype=np.float64)
    raw_buf = pa.py_buffer(np.ascontiguousarray(wkb_arr).view(np.uint8).data)
    fixed = pa.FixedSizeBinaryArray.from_buffers(pa.binary(21), n, [None, raw_buf])
    return fixed.cast(pa.binary())


def write_silver_parquet(df: pl.DataFrame, output_path: Path) -> Path:
    """Persist Silver telemetry DataFrame as ZSTD-compressed GeoParquet (WKB + geo metadata)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lons = df.get_column("lon").to_list()
    lats = df.get_column("lat").to_list()
    if "lon_unwrapped" in df.columns:
        bbox_lons = df.get_column("lon_unwrapped").to_list()
    else:
        bbox_lons, _ = unwrap_longitudes(lons)

    table = df.to_arrow()
    if "geometry" not in table.column_names:
        geom_array = _encode_wkb_points_arrow(lons, lats)
        table = table.append_column("geometry", geom_array)

    geo_meta = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Point"],
                "bbox": [
                    float(min(bbox_lons)) if bbox_lons else 0.0,
                    float(min(lats)) if lats else 0.0,
                    float(max(bbox_lons)) if bbox_lons else 0.0,
                    float(max(lats)) if lats else 0.0,
                ],
            }
        },
    }
    existing_meta = dict(table.schema.metadata or {})
    existing_meta[b"geo"] = json.dumps(geo_meta).encode("utf-8")
    existing_meta[b"etl_version"] = str(CURRENT_ETL_VERSION).encode("utf-8")
    table = table.replace_schema_metadata(existing_meta)

    pq.write_table(table, output_path, compression="zstd")
    return output_path


def _to_local_enu_coords(
    lats: Sequence[float],
    unwrapped_lons: Sequence[float],
) -> list[tuple[float, float]]:
    """Project (lat, unwrapped_lon) into isotropic local ENU meters (north_m, east_m)."""
    if not lats:
        return []
    lat0 = float(lats[0])
    lon0 = float(unwrapped_lons[0])
    cos_lat0 = math.cos(math.radians(lat0))
    deg_to_m = (math.pi / 180.0) * EARTH_RADIUS_M
    return [
        ((float(lat) - lat0) * deg_to_m, (float(u_lon) - lon0) * deg_to_m * cos_lat0)
        for lat, u_lon in zip(lats, unwrapped_lons, strict=True)
    ]


def simplify_track_points(points: list[TrackPoint], tolerance_deg: float) -> list[TrackPoint]:
    """Segment-preserving, antimeridian-safe Douglas-Peucker simplification (degrees)."""
    if len(points) <= 2 or tolerance_deg <= 0.0:
        return points

    segments: list[list[TrackPoint]] = []
    current: list[TrackPoint] = [points[0]]
    for pt in points[1:]:
        if pt.segment_idx != current[-1].segment_idx:
            segments.append(current)
            current = [pt]
        else:
            current.append(pt)
    segments.append(current)

    simplified: list[TrackPoint] = []
    for seg in segments:
        unwrapped_lons, _ = unwrap_longitudes([p.lon for p in seg])
        coords = [(p.lat, u_lon) for p, u_lon in zip(seg, unwrapped_lons, strict=True)]
        kept_indices = _rdp_segment_indices(coords, tolerance_deg)
        simplified.extend(seg[idx] for idx in kept_indices)
    return simplified


def simplify_track_points_m(points: Sequence[TrackPoint], tolerance_m: float) -> list[TrackPoint]:
    """Segment-preserving, antimeridian-safe local-ENU meter-space RDP simplification."""
    if len(points) <= 2 or tolerance_m <= 0.0:
        return list(points)

    segments: list[list[TrackPoint]] = []
    current: list[TrackPoint] = [points[0]]
    for pt in points[1:]:
        if pt.segment_idx != current[-1].segment_idx:
            segments.append(current)
            current = [pt]
        else:
            current.append(pt)
    segments.append(current)

    simplified: list[TrackPoint] = []
    for seg in segments:
        unwrapped_lons, _ = unwrap_longitudes([p.lon for p in seg])
        coords_m = _to_local_enu_coords([p.lat for p in seg], unwrapped_lons)
        kept_indices = _rdp_segment_indices(coords_m, tolerance_m)
        simplified.extend(seg[idx] for idx in kept_indices)
    return simplified


def _perp_distance_deg(
    pt_lat: float,
    pt_lon: float,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
) -> float:
    dx = end_lon - start_lon
    dy = end_lat - start_lat
    if dx == 0.0 and dy == 0.0:
        return math.hypot(pt_lon - start_lon, pt_lat - start_lat)
    num = abs(dy * pt_lon - dx * pt_lat + end_lon * start_lat - end_lat * start_lon)
    return num / math.hypot(dx, dy)


def _rdp_segment_indices(
    coords: Sequence[tuple[float, float]],
    tol: float,
) -> list[int]:
    """Return kept indices in `coords` using iterative Douglas-Peucker."""
    n = len(coords)
    if n <= 2 or tol <= 0.0:
        return list(range(n))
    keep = [False] * n
    keep[0] = True
    keep[n - 1] = True
    stack: list[tuple[int, int]] = [(0, n - 1)]
    while stack:
        start_idx, end_idx = stack.pop()
        if end_idx - start_idx <= 1:
            continue
        start_lat, start_lon = coords[start_idx]
        end_lat, end_lon = coords[end_idx]
        max_dist = -1.0
        max_idx = start_idx
        for idx in range(start_idx + 1, end_idx):
            pt_lat, pt_lon = coords[idx]
            d = _perp_distance_deg(pt_lat, pt_lon, start_lat, start_lon, end_lat, end_lon)
            if d > max_dist:
                max_dist = d
                max_idx = idx
        if max_dist > tol:
            keep[max_idx] = True
            stack.append((start_idx, max_idx))
            stack.append((max_idx, end_idx))
    return [idx for idx, flag in enumerate(keep) if flag]


def simplify_coord_tuples(
    points: Sequence[tuple[float, float, int]],
    tolerance_deg: float,
) -> list[tuple[float, float, int]]:
    """Fast tuple-based `(lat, lon, segment_idx)` RDP simplification in degrees."""
    if len(points) <= 2 or tolerance_deg <= 0.0:
        return list(points)

    segments: list[list[tuple[float, float, int]]] = []
    current: list[tuple[float, float, int]] = [points[0]]
    for pt in points[1:]:
        if pt[2] != current[-1][2]:
            segments.append(current)
            current = [pt]
        else:
            current.append(pt)
    segments.append(current)

    simplified: list[tuple[float, float, int]] = []
    for seg in segments:
        unwrapped_lons, _ = unwrap_longitudes([p[1] for p in seg])
        coords = [(p[0], u_lon) for p, u_lon in zip(seg, unwrapped_lons, strict=True)]
        kept_indices = _rdp_segment_indices(coords, tolerance_deg)
        simplified.extend(seg[idx] for idx in kept_indices)
    return simplified


def simplify_coord_tuples_m(
    points: Sequence[tuple[float, float, int]],
    tolerance_m: float,
) -> list[tuple[float, float, int]]:
    """Fast tuple-based `(lat, lon, segment_idx)` RDP simplification in local ENU meters."""
    if len(points) <= 2 or tolerance_m <= 0.0:
        return list(points)

    segments: list[list[tuple[float, float, int]]] = []
    current: list[tuple[float, float, int]] = [points[0]]
    for pt in points[1:]:
        if pt[2] != current[-1][2]:
            segments.append(current)
            current = [pt]
        else:
            current.append(pt)
    segments.append(current)

    simplified: list[tuple[float, float, int]] = []
    for seg in segments:
        unwrapped_lons, _ = unwrap_longitudes([p[1] for p in seg])
        coords_m = _to_local_enu_coords([p[0] for p in seg], unwrapped_lons)
        kept_indices = _rdp_segment_indices(coords_m, tolerance_m)
        simplified.extend(seg[idx] for idx in kept_indices)
    return simplified


def build_lod_geojson_feature_from_tuples(
    points: Sequence[tuple[float, float, int]],
    *,
    lod: LodLevel,
    tolerance_deg: float = 0.0,
    tolerance_m: float | None = None,
    unwrap_antimeridian: bool = True,
) -> dict[str, Any]:
    """Build a GeoJSON MultiLineString Feature directly from `(lat, lon, segment_idx)` tuples."""
    if not points:
        raise ValueError("Cannot build geometry from zero trackpoints")

    if tolerance_m is not None:
        simplified = simplify_coord_tuples_m(points, tolerance_m)
    else:
        simplified = simplify_coord_tuples(points, tolerance_deg)
    lats = [p[0] for p in simplified]
    lons = [p[1] for p in simplified]
    unwrapped_lons, crosses_antimeridian = unwrap_longitudes(lons)
    bounds = (min(unwrapped_lons), min(lats), max(unwrapped_lons), max(lats))
    use_unwrapped = unwrap_antimeridian and crosses_antimeridian

    lines: list[list[list[float]]] = []
    cur_seg: int | None = None
    cur_line: list[list[float]] = []

    for (lat_val, lon_raw, seg_idx), u_lon in zip(simplified, unwrapped_lons, strict=True):
        lon_val = u_lon if use_unwrapped else lon_raw
        coord = [round(lon_val, 6), round(lat_val, 6)]
        if cur_seg is None or seg_idx != cur_seg:
            if cur_line:
                lines.append(cur_line if len(cur_line) >= 2 else [cur_line[0], cur_line[0]])
            cur_seg = seg_idx
            cur_line = [coord]
        else:
            cur_line.append(coord)

    if cur_line:
        lines.append(cur_line if len(cur_line) >= 2 else [cur_line[0], cur_line[0]])

    return {
        "type": "Feature",
        "geometry": {
            "type": "MultiLineString",
            "coordinates": lines,
        },
        "properties": {
            "lod": lod,
            "point_count": len(simplified),
            "original_point_count": len(points),
            "segment_count": len(lines),
            "bbox": [
                round(bounds[0], 6),
                round(bounds[1], 6),
                round(bounds[2], 6),
                round(bounds[3], 6),
            ],
            "crosses_antimeridian": crosses_antimeridian,
        },
    }


def build_lod_geojson_feature(
    points: Sequence[TrackPoint],
    *,
    lod: LodLevel,
    tolerance_deg: float = 0.0,
    tolerance_m: float | None = None,
    unwrap_antimeridian: bool = True,
) -> dict[str, Any]:
    """Build a segment-preserving GeoJSON MultiLineString Feature for a given LOD."""
    if not points:
        raise ValueError("Cannot build geometry from zero trackpoints")
    tuples = [(p.lat, p.lon, p.segment_idx) for p in points]
    return build_lod_geojson_feature_from_tuples(
        tuples,
        lod=lod,
        tolerance_deg=tolerance_deg,
        tolerance_m=tolerance_m,
        unwrap_antimeridian=unwrap_antimeridian,
    )


def generate_lod_geometries_from_tuples(
    points: Sequence[tuple[float, float, int]],
    *,
    tolerances: Mapping[LodLevel, float] | None = None,
    tolerances_m: Mapping[LodLevel, float] | None = None,
    unwrap_antimeridian: bool = True,
) -> dict[LodLevel, dict[str, Any]]:
    """Generate multi-LOD (full, medium, low) GeoJSON features from `(lat, lon, segment_idx)`."""
    if tolerances is not None:
        return {
            lod: build_lod_geojson_feature_from_tuples(
                points,
                lod=lod,
                tolerance_deg=tolerances[lod],
                unwrap_antimeridian=unwrap_antimeridian,
            )
            for lod in ("full", "medium", "low")
        }
    tol_m_map = tolerances_m or LOD_TOLERANCES_M
    return {
        lod: build_lod_geojson_feature_from_tuples(
            points,
            lod=lod,
            tolerance_m=tol_m_map[lod],
            unwrap_antimeridian=unwrap_antimeridian,
        )
        for lod in ("full", "medium", "low")
    }


def generate_lod_geometries(
    points: Sequence[TrackPoint],
    *,
    tolerances: Mapping[LodLevel, float] | None = None,
    tolerances_m: Mapping[LodLevel, float] | None = None,
    unwrap_antimeridian: bool = True,
) -> dict[LodLevel, dict[str, Any]]:
    """Generate multi-LOD (full, medium, low) GeoJSON features preserving segment boundaries."""
    tuples = [(p.lat, p.lon, p.segment_idx) for p in points]
    return generate_lod_geometries_from_tuples(
        tuples,
        tolerances=tolerances,
        tolerances_m=tolerances_m,
        unwrap_antimeridian=unwrap_antimeridian,
    )


LOD_CACHE_SCHEMA_VERSION = 1
CHART_CACHE_SCHEMA_VERSION = 1


def _trim_values_match(cached_val: Any, expected_val: float | None) -> bool:
    if cached_val is None and expected_val is None:
        return True
    if cached_val is None or expected_val is None:
        return False
    try:
        return math.isclose(float(cached_val), float(expected_val), rel_tol=0.0, abs_tol=1e-6)
    except (TypeError, ValueError):
        return False


def build_lod_cache_payload(
    points: Sequence[tuple[float, float, int]] | Sequence[TrackPoint],
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    unwrap_antimeridian: bool = True,
) -> bytes:
    """Serialize all 3 LOD GeoJSON features into a gzip-compressed `.lod.json.gz` envelope."""
    if not points:
        raise ValueError("Cannot build LOD cache from zero trackpoints")
    first = points[0]
    if isinstance(first, TrackPoint):
        tuples = [(p.lat, p.lon, p.segment_idx) for p in points]  # type: ignore[union-attr]
    else:
        tuples = list(points)  # type: ignore[arg-type]

    lods = generate_lod_geometries_from_tuples(
        tuples,
        tolerances_m=LOD_TOLERANCES_M,
        unwrap_antimeridian=unwrap_antimeridian,
    )
    envelope = {
        "v": LOD_CACHE_SCHEMA_VERSION,
        "trim_start_s": trim_start_s,
        "trim_end_s": trim_end_s,
        "lods": lods,
    }
    raw_json = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw_json, compresslevel=6)


def parse_lod_cache_payload(
    raw_gz_bytes: bytes,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Decompress and validate a `.lod.json.gz` envelope against expected trim bounds."""
    try:
        data = json.loads(gzip.decompress(raw_gz_bytes).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("v") != LOD_CACHE_SCHEMA_VERSION:
        return None
    if not _trim_values_match(data.get("trim_start_s"), trim_start_s):
        return None
    if not _trim_values_match(data.get("trim_end_s"), trim_end_s):
        return None
    lods = data.get("lods") or data.get("lodes")
    if not isinstance(lods, dict):
        return None
    return lods


def extract_lod_feature_from_cache(
    raw_gz_bytes: bytes,
    lod: LodLevel,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> dict[str, Any] | None:
    """Return the cached GeoJSON Feature for `lod` if valid and trim-matching, else None."""
    lods = parse_lod_cache_payload(
        raw_gz_bytes,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    if lods is None:
        return None
    feature = lods.get(lod)
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        return None
    props = feature.get("properties")
    geom = feature.get("geometry")
    if not isinstance(props, dict) or not isinstance(geom, dict):
        return None
    return {
        "type": "Feature",
        "geometry": dict(geom),
        "properties": dict(props),
    }


def build_chart_cache_payload(
    profile: Sequence[Mapping[str, Any]],
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = 800,
) -> bytes:
    """Serialize precomputed LTTB telemetry chart series into `.chart.json.gz` bytes."""
    serializable_points: list[dict[str, Any]] = []
    for pt in profile:
        ts_val = pt.get("ts_utc")
        ts_str = ts_val.isoformat() if isinstance(ts_val, datetime) else ts_val
        serializable_points.append(
            {
                "dist_m": float(pt.get("dist_m", 0.0)),
                "cum_dist_m": float(pt.get("cum_dist_m", pt.get("dist_m", 0.0))),
                "elev_m": float(pt["elev_m"]) if pt.get("elev_m") is not None else None,
                "speed_mps": float(pt.get("speed_mps", 0.0)),
                "grade_pct": float(pt["grade_pct"]) if pt.get("grade_pct") is not None else None,
                "hr_bpm": float(pt["hr_bpm"]) if pt.get("hr_bpm") is not None else None,
                "watts": float(pt["watts"]) if pt.get("watts") is not None else None,
                "ts_utc": ts_str,
            }
        )
    envelope = {
        "v": CHART_CACHE_SCHEMA_VERSION,
        "max_points": max_points,
        "trim_start_s": trim_start_s,
        "trim_end_s": trim_end_s,
        "points": serializable_points,
    }
    raw_json = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw_json, compresslevel=6)


def parse_chart_cache_payload(
    raw_gz_bytes: bytes,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> list[dict[str, Any]] | None:
    """Decompress and validate a `.chart.json.gz` envelope against expected trim bounds."""
    try:
        data = json.loads(gzip.decompress(raw_gz_bytes).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("v") != CHART_CACHE_SCHEMA_VERSION:
        return None
    if not _trim_values_match(data.get("trim_start_s"), trim_start_s):
        return None
    if not _trim_values_match(data.get("trim_end_s"), trim_end_s):
        return None
    pts = data.get("points")
    if not isinstance(pts, list):
        return None
    return [dict(item) for item in pts if isinstance(item, dict)]
