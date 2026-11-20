"""DuckDB OLAP queries over Silver Parquet, LTTB downsampling, LOD geometry, and dbt Gold build."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from trip_telemetry.pipeline import (
    LOD_TOLERANCES_DEG,
    LOD_TOLERANCES_M,
    build_chart_cache_payload,
    build_lod_cache_payload,
    build_lod_geojson_feature_from_tuples,
)
from trip_telemetry.ref_data import resolve_ref_data_dir
from trip_telemetry.schemas import LodLevel


def _validate_trim_args(trim_start_s: float | None, trim_end_s: float | None) -> None:
    if trim_start_s is not None:
        if not math.isfinite(trim_start_s) or trim_start_s < 0.0:
            raise ValueError("trim_start_s must be a finite non-negative number")
    if trim_end_s is not None:
        if not math.isfinite(trim_end_s) or trim_end_s < 0.0:
            raise ValueError("trim_end_s must be a finite non-negative number")
    if trim_start_s is not None and trim_end_s is not None and trim_start_s >= trim_end_s:
        raise ValueError("trim_start_s must be strictly less than trim_end_s")


def _prepare_filtered_table(
    con: duckdb.DuckDBPyConnection,
    parquet_path: Path,
    trim_start_s: float | None,
    trim_end_s: float | None,
) -> None:
    _validate_trim_args(trim_start_s, trim_end_s)
    rel = con.read_parquet(str(parquet_path))
    cols = set(rel.columns)
    con.register("raw_telemetry", rel)

    if trim_start_s is None and trim_end_s is None:
        if "is_trimmed" in cols:
            con.execute(
                """
                CREATE OR REPLACE TEMP TABLE filtered AS
                SELECT *
                FROM raw_telemetry
                WHERE NOT COALESCE(is_trimmed, FALSE)
                """
            )
        else:
            con.execute(
                """
                CREATE OR REPLACE TEMP TABLE filtered AS
                SELECT *
                FROM raw_telemetry
                """
            )
        return

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE filtered AS
        WITH bounds AS (
            SELECT MIN(ts_utc) AS min_ts
            FROM raw_telemetry
        )
        SELECT p.*
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
        """,
        [trim_start_s, trim_start_s, trim_end_s, trim_end_s],
    )


_UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _normalize_ts_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return _UNIX_EPOCH_UTC + timedelta(microseconds=value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return None


def query_stage_kpis(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> dict[str, Any]:
    """Compute stage-level distance, ascent/descent, speed, VAM, and HR/power via DuckDB."""
    con = duckdb.connect(database=":memory:")
    try:
        _prepare_filtered_table(con, parquet_path, trim_start_s, trim_end_s)
        row = con.execute(
            """
            WITH ordered AS (
                SELECT
                    seq,
                    ts_utc,
                    elev_m,
                    cum_dist_m,
                    speed_mps,
                    grade_pct,
                    hr_bpm,
                    watts,
                    segment_idx,
                    is_moving,
                    ROW_NUMBER() OVER (ORDER BY seq) AS rn,
                    LAG(segment_idx) OVER (ORDER BY seq) AS prev_seg,
                    LAG(elev_m) OVER (ORDER BY seq) AS prev_elev_m,
                    LAG(ts_utc) OVER (ORDER BY seq) AS prev_ts_utc
                FROM filtered
            ),
            deltas AS (
                SELECT
                    *,
                    CASE
                        WHEN rn > 1 AND segment_idx = prev_seg
                             AND elev_m IS NOT NULL AND prev_elev_m IS NOT NULL
                        THEN elev_m - prev_elev_m
                        ELSE 0.0
                    END AS delta_elev_m,
                    CASE
                        WHEN rn > 1 AND segment_idx = prev_seg
                             AND ts_utc IS NOT NULL AND prev_ts_utc IS NOT NULL
                        THEN GREATEST(EPOCH(ts_utc) - EPOCH(prev_ts_utc), 0.0)
                        ELSE 0.0
                    END AS delta_sec
                FROM ordered
            )
            SELECT
                COUNT(*)::BIGINT AS point_count,
                COALESCE(MAX(cum_dist_m) - MIN(cum_dist_m), 0.0)::DOUBLE AS total_distance_m,
                COALESCE(
                    SUM(CASE WHEN delta_elev_m > 0 THEN delta_elev_m ELSE 0.0 END),
                    0.0
                )::DOUBLE AS ascent_m,
                COALESCE(
                    SUM(CASE WHEN delta_elev_m < 0 THEN ABS(delta_elev_m) ELSE 0.0 END),
                    0.0
                )::DOUBLE AS descent_m,
                COALESCE(
                    SUM(CASE WHEN is_moving THEN delta_sec ELSE 0.0 END),
                    0.0
                )::DOUBLE AS moving_time_s,
                COALESCE(
                    EPOCH(MAX(ts_utc)) - EPOCH(MIN(ts_utc)),
                    0.0
                )::DOUBLE AS elapsed_time_s,
                COALESCE(AVG(NULLIF(speed_mps, 0.0)), 0.0)::DOUBLE AS avg_moving_speed_fallback,
                COALESCE(AVG(speed_mps), 0.0)::DOUBLE AS avg_raw_speed_fallback,
                COALESCE(MAX(speed_mps), 0.0)::DOUBLE AS max_speed_mps,
                MIN(elev_m)::DOUBLE AS min_elev_m,
                MAX(elev_m)::DOUBLE AS max_elev_m,
                MAX(grade_pct)::DOUBLE AS max_grade_pct,
                AVG(hr_bpm)::DOUBLE AS avg_hr_bpm,
                AVG(watts)::DOUBLE AS avg_watts
            FROM deltas
            """
        ).fetchone()
    finally:
        con.close()

    assert row is not None
    point_count = int(row[0])
    if point_count == 0 and (trim_start_s is not None or trim_end_s is not None):
        raise ValueError("Trim window excludes all telemetry points")

    distance_m = float(row[1])
    ascent_m = float(row[2])
    descent_m = float(row[3])
    moving_time_s = float(row[4])
    elapsed_time_s = max(float(row[5]), 0.0)
    avg_moving_fallback = float(row[6])
    avg_raw_fallback = float(row[7])
    max_speed_mps = float(row[8])

    avg_speed_mps = (distance_m / elapsed_time_s) if elapsed_time_s > 0.0 else avg_raw_fallback
    avg_moving_speed_mps = (
        (distance_m / moving_time_s) if moving_time_s > 0.0 else avg_moving_fallback
    )
    if moving_time_s > 0.0:
        vam = (ascent_m / moving_time_s) * 3600.0
    elif elapsed_time_s > 0.0:
        vam = (ascent_m / elapsed_time_s) * 3600.0
    else:
        vam = 0.0

    return {
        "point_count": point_count,
        "distance_m": distance_m,
        "total_distance_m": distance_m,
        "ascent_m": round(ascent_m, 2),
        "elevation_gain_m": round(ascent_m, 2),
        "descent_m": round(descent_m, 2),
        "elevation_loss_m": round(descent_m, 2),
        "moving_time_s": round(moving_time_s, 2),
        "elapsed_time_s": round(elapsed_time_s, 2),
        "avg_speed_mps": round(avg_speed_mps, 4),
        "avg_moving_speed_mps": round(avg_moving_speed_mps, 4),
        "max_speed_mps": round(max_speed_mps, 4),
        "vam": round(vam, 2),
        "min_elev_m": float(row[9]) if row[9] is not None else None,
        "max_elev_m": float(row[10]) if row[10] is not None else None,
        "max_grade_pct": float(row[11]) if row[11] is not None else None,
        "avg_hr_bpm": float(row[12]) if row[12] is not None else None,
        "avg_watts": float(row[13]) if row[13] is not None else None,
    }


def _lttb_downsample_indices(xs: list[float], ys: list[float], threshold: int) -> list[int]:
    n = len(xs)
    if n <= threshold:
        return list(range(n))
    if threshold <= 2:
        return [0, n - 1]

    selected = [0]
    bucket_size = (n - 2) / (threshold - 2)
    a_idx = 0

    for bucket in range(threshold - 2):
        range_start = int(math.floor(bucket * bucket_size)) + 1
        range_end = min(int(math.floor((bucket + 1) * bucket_size)) + 1, n - 1)
        if range_end <= range_start:
            range_end = min(range_start + 1, n - 1)

        next_start = int(math.floor((bucket + 1) * bucket_size)) + 1
        if bucket + 1 == threshold - 2:
            next_end = n
        else:
            next_end = min(int(math.floor((bucket + 2) * bucket_size)) + 1, n)
        if next_end <= next_start:
            next_start = min(next_start, n - 1)
            next_end = n

        next_count = next_end - next_start
        avg_x = sum(xs[next_start:next_end]) / next_count
        avg_y = sum(ys[next_start:next_end]) / next_count

        ax = xs[a_idx]
        ay = ys[a_idx]
        max_area = -1.0
        best_idx = range_start

        for idx in range(range_start, range_end):
            area = abs((ax - avg_x) * (ys[idx] - ay) - (ax - xs[idx]) * (avg_y - ay))
            if area > max_area:
                max_area = area
                best_idx = idx

        selected.append(best_idx)
        a_idx = best_idx

    selected.append(n - 1)
    return selected


def query_telemetry_profile(
    parquet_path: Path,
    max_points: int = 800,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> list[dict[str, Any]]:
    """LTTB-downsample Silver telemetry via DuckDB for synchronized UI elevation/speed charts."""
    if max_points < 2:
        raise ValueError("max_points must be >= 2")

    con = duckdb.connect(database=":memory:")
    try:
        _prepare_filtered_table(con, parquet_path, trim_start_s, trim_end_s)
        rows = con.execute(
            """
            SELECT
                cum_dist_m::DOUBLE AS cum_dist_m,
                elev_m::DOUBLE AS elev_m,
                speed_mps::DOUBLE AS speed_mps,
                grade_pct::DOUBLE AS grade_pct,
                hr_bpm::DOUBLE AS hr_bpm,
                watts::DOUBLE AS watts,
                EPOCH_US(ts_utc)::BIGINT AS ts_us
            FROM filtered
            ORDER BY seq ASC
            """
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return []

    xs = [float(r[0]) if r[0] is not None else float(i) for i, r in enumerate(rows)]
    has_elev = any(r[1] is not None for r in rows)
    if has_elev:
        ys: list[float] = []
        last_y = next(float(r[1]) for r in rows if r[1] is not None)
        for r in rows:
            if r[1] is not None:
                last_y = float(r[1])
            ys.append(last_y)
    else:
        ys = [float(r[2]) if r[2] is not None else 0.0 for r in rows]

    indices = _lttb_downsample_indices(xs, ys, max_points)

    first_dist = rows[0][0]
    dist_offset = (
        float(first_dist) if (trim_start_s is not None and first_dist is not None) else 0.0
    )
    result: list[dict[str, Any]] = []
    for idx in indices:
        r = rows[idx]
        dist_val = max(float(r[0]) - dist_offset, 0.0) if r[0] is not None else 0.0
        result.append(
            {
                "dist_m": dist_val,
                "cum_dist_m": dist_val,
                "elev_m": float(r[1]) if r[1] is not None else None,
                "speed_mps": float(r[2]) if r[2] is not None else 0.0,
                "grade_pct": float(r[3]) if r[3] is not None else None,
                "hr_bpm": float(r[4]) if r[4] is not None else None,
                "watts": float(r[5]) if r[5] is not None else None,
                "ts_utc": _normalize_ts_utc(r[6]),
            }
        )
    return result


def _read_geometry_tuples(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> list[tuple[float, float, int]]:
    con = duckdb.connect(database=":memory:")
    try:
        _prepare_filtered_table(con, parquet_path, trim_start_s, trim_end_s)
        rows = con.execute(
            """
            SELECT
                lat::DOUBLE AS lat,
                lon::DOUBLE AS lon,
                segment_idx::INTEGER AS segment_idx
            FROM filtered
            ORDER BY seq ASC
            """
        ).fetchall()
    finally:
        con.close()

    if not rows:
        raise ValueError("No telemetry points found for map geometry query")

    return [(float(r[0]), float(r[1]), int(r[2]) if r[2] is not None else 0) for r in rows]


def query_map_geometry(
    parquet_path: Path,
    lod: LodLevel = "medium",
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    unwrap_antimeridian: bool = True,
) -> dict[str, Any]:
    """Query Silver Parquet via DuckDB and return segment-preserving GeoJSON at requested LOD."""
    if lod not in LOD_TOLERANCES_DEG:
        expected = tuple(LOD_TOLERANCES_DEG)
        raise ValueError(f"Unsupported LOD level {lod!r}; expected one of {expected}")

    tuples = _read_geometry_tuples(
        parquet_path,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    return build_lod_geojson_feature_from_tuples(
        tuples,
        lod=lod,
        tolerance_m=LOD_TOLERANCES_M[lod],
        unwrap_antimeridian=unwrap_antimeridian,
    )


def build_lod_cache_from_parquet(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    unwrap_antimeridian: bool = True,
) -> bytes:
    """Extract coordinates from Silver Parquet via DuckDB and build `.lod.json.gz` cache bytes."""
    tuples = _read_geometry_tuples(
        parquet_path,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    return build_lod_cache_payload(
        tuples,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        unwrap_antimeridian=unwrap_antimeridian,
    )


def build_chart_cache_from_parquet(
    parquet_path: Path,
    *,
    max_points: int = 800,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> bytes:
    """Query LTTB telemetry profile via DuckDB and build `.chart.json.gz` cache bytes."""
    profile = query_telemetry_profile(
        parquet_path,
        max_points=max_points,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    return build_chart_cache_payload(
        profile,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        max_points=max_points,
    )


def _select_manifest_indices_numpy(
    lat_arr: np.ndarray,
    elev_arr: np.ndarray,
    dist_arr: np.ndarray,
    seg_arr: np.ndarray,
    max_points: int,
) -> np.ndarray:
    n = len(lat_arr)
    if max_points <= 0 or n == 0:
        return np.empty(0, dtype=np.int64)
    if n <= max_points:
        return np.arange(n, dtype=np.int64)
    if max_points == 1:
        return np.array([0], dtype=np.int64)

    must_keep: set[int] = {0, n - 1}
    seg_breaks = np.flatnonzero(seg_arr[1:] != seg_arr[:-1])
    for br in seg_breaks.tolist():
        must_keep.add(br)
        must_keep.add(br + 1)

    if len(must_keep) >= max_points:
        kept = sorted(must_keep)
        step = (len(kept) - 1) / (max_points - 1)
        return np.array([kept[round(i * step)] for i in range(max_points)], dtype=np.int64)

    xs = np.where(np.isnan(dist_arr), np.arange(n, dtype=np.float64), dist_arr)
    ys = np.where(np.isnan(elev_arr), lat_arr * 111_000.0, elev_arr)
    lttb_idx = _lttb_downsample_indices(xs.tolist(), ys.tolist(), max_points)
    combined = sorted(must_keep | set(lttb_idx))
    if len(combined) > max_points:
        step = (len(combined) - 1) / (max_points - 1)
        combined = [combined[round(i * step)] for i in range(max_points)]
    return np.array(combined, dtype=np.int64)


def query_stage_kpis_and_manifest_points(
    parquet_path: Path,
    *,
    max_points: int = 12_000,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Single-pass DuckDB `.fetchnumpy()` query returning `(kpis, downsampled_points)`."""
    con = duckdb.connect(database=":memory:")
    try:
        _prepare_filtered_table(con, parquet_path, trim_start_s, trim_end_s)
        kpi_row = con.execute(
            """
            WITH ordered AS (
                SELECT
                    seq,
                    ts_utc,
                    elev_m,
                    cum_dist_m,
                    speed_mps,
                    grade_pct,
                    hr_bpm,
                    watts,
                    segment_idx,
                    is_moving,
                    ROW_NUMBER() OVER (ORDER BY seq) AS rn,
                    LAG(segment_idx) OVER (ORDER BY seq) AS prev_seg,
                    LAG(elev_m) OVER (ORDER BY seq) AS prev_elev_m,
                    LAG(ts_utc) OVER (ORDER BY seq) AS prev_ts_utc
                FROM filtered
            ),
            deltas AS (
                SELECT
                    *,
                    CASE
                        WHEN rn > 1 AND segment_idx = prev_seg
                             AND elev_m IS NOT NULL AND prev_elev_m IS NOT NULL
                        THEN elev_m - prev_elev_m
                        ELSE 0.0
                    END AS delta_elev_m,
                    CASE
                        WHEN rn > 1 AND segment_idx = prev_seg
                             AND ts_utc IS NOT NULL AND prev_ts_utc IS NOT NULL
                        THEN GREATEST(EPOCH(ts_utc) - EPOCH(prev_ts_utc), 0.0)
                        ELSE 0.0
                    END AS delta_sec
                FROM ordered
            )
            SELECT
                COUNT(*)::BIGINT AS point_count,
                COALESCE(MAX(cum_dist_m) - MIN(cum_dist_m), 0.0)::DOUBLE AS total_distance_m,
                COALESCE(
                    SUM(CASE WHEN delta_elev_m > 0 THEN delta_elev_m ELSE 0.0 END),
                    0.0,
                )::DOUBLE AS ascent_m,
                COALESCE(
                    SUM(CASE WHEN delta_elev_m < 0 THEN ABS(delta_elev_m) ELSE 0.0 END),
                    0.0,
                )::DOUBLE AS descent_m,
                COALESCE(
                    SUM(CASE WHEN is_moving THEN delta_sec ELSE 0.0 END),
                    0.0,
                )::DOUBLE AS moving_time_s,
                COALESCE(EPOCH(MAX(ts_utc)) - EPOCH(MIN(ts_utc)), 0.0)::DOUBLE AS elapsed_time_s,
                COALESCE(AVG(NULLIF(speed_mps, 0.0)), 0.0)::DOUBLE AS avg_moving_speed_fallback,
                COALESCE(AVG(speed_mps), 0.0)::DOUBLE AS avg_raw_speed_fallback,
                COALESCE(MAX(speed_mps), 0.0)::DOUBLE AS max_speed_mps,
                MIN(elev_m)::DOUBLE AS min_elev_m,
                MAX(elev_m)::DOUBLE AS max_elev_m,
                MAX(grade_pct)::DOUBLE AS max_grade_pct,
                AVG(hr_bpm)::DOUBLE AS avg_hr_bpm,
                AVG(watts)::DOUBLE AS avg_watts
            FROM deltas
            """
        ).fetchone()
        np_cols = con.execute(
            """
            SELECT
                lat::DOUBLE AS lat,
                lon::DOUBLE AS lon,
                COALESCE(lon_unwrapped, lon)::DOUBLE AS lon_unwrapped,
                elev_m::DOUBLE AS elev_m,
                cum_dist_m::DOUBLE AS cum_dist_m,
                COALESCE(speed_mps, 0.0)::DOUBLE AS speed_mps,
                grade_pct::DOUBLE AS grade_pct,
                hr_bpm::DOUBLE AS hr_bpm,
                watts::DOUBLE AS watts,
                elapsed_s::DOUBLE AS elapsed_s,
                COALESCE(EPOCH_US(ts_utc), -1)::BIGINT AS ts_us,
                COALESCE(segment_idx, 0)::INTEGER AS segment_idx
            FROM filtered
            ORDER BY seq ASC
            """
        ).fetchnumpy()
    finally:
        con.close()

    assert kpi_row is not None
    point_count = int(kpi_row[0])
    if point_count == 0 and (trim_start_s is not None or trim_end_s is not None):
        raise ValueError("Trim window excludes all telemetry points")

    distance_m = float(kpi_row[1])
    ascent_m = float(kpi_row[2])
    descent_m = float(kpi_row[3])
    moving_time_s = float(kpi_row[4])
    elapsed_time_s = max(float(kpi_row[5]), 0.0)
    avg_moving_fallback = float(kpi_row[6])
    avg_raw_fallback = float(kpi_row[7])
    max_speed_mps = float(kpi_row[8])
    avg_speed_mps = (distance_m / elapsed_time_s) if elapsed_time_s > 0.0 else avg_raw_fallback
    avg_moving_speed_mps = (
        (distance_m / moving_time_s) if moving_time_s > 0.0 else avg_moving_fallback
    )
    vam = (
        (ascent_m / moving_time_s) * 3600.0
        if moving_time_s > 0.0
        else ((ascent_m / elapsed_time_s) * 3600.0 if elapsed_time_s > 0.0 else 0.0)
    )

    kpis: dict[str, Any] = {
        "point_count": point_count,
        "distance_m": distance_m,
        "total_distance_m": distance_m,
        "ascent_m": round(ascent_m, 2),
        "elevation_gain_m": round(ascent_m, 2),
        "descent_m": round(descent_m, 2),
        "elevation_loss_m": round(descent_m, 2),
        "moving_time_s": round(moving_time_s, 2),
        "elapsed_time_s": round(elapsed_time_s, 2),
        "avg_speed_mps": round(avg_speed_mps, 4),
        "avg_moving_speed_mps": round(avg_moving_speed_mps, 4),
        "max_speed_mps": round(max_speed_mps, 4),
        "vam": round(vam, 2),
        "min_elev_m": float(kpi_row[9]) if kpi_row[9] is not None else None,
        "max_elev_m": float(kpi_row[10]) if kpi_row[10] is not None else None,
        "max_grade_pct": float(kpi_row[11]) if kpi_row[11] is not None else None,
        "avg_hr_bpm": float(kpi_row[12]) if kpi_row[12] is not None else None,
        "avg_watts": float(kpi_row[13]) if kpi_row[13] is not None else None,
    }

    lat_arr = np.asarray(np_cols["lat"], dtype=np.float64)
    if len(lat_arr) == 0:
        return kpis, []

    lon_arr = np.asarray(np_cols["lon"], dtype=np.float64)
    lon_u_arr = np.asarray(np_cols["lon_unwrapped"], dtype=np.float64)
    elev_arr = np.asarray(np_cols["elev_m"], dtype=np.float64)
    dist_arr = np.asarray(np_cols["cum_dist_m"], dtype=np.float64)
    speed_arr = np.asarray(np_cols["speed_mps"], dtype=np.float64)
    grade_arr = np.asarray(np_cols["grade_pct"], dtype=np.float64)
    hr_arr = np.asarray(np_cols["hr_bpm"], dtype=np.float64)
    watts_arr = np.asarray(np_cols["watts"], dtype=np.float64)
    elapsed_arr = np.asarray(np_cols["elapsed_s"], dtype=np.float64)
    ts_us_arr = np.asarray(np_cols["ts_us"], dtype=np.int64)
    seg_arr = np.asarray(np_cols["segment_idx"], dtype=np.int32)

    indices = _select_manifest_indices_numpy(lat_arr, elev_arr, dist_arr, seg_arr, max_points)
    base_dist = float(dist_arr[0]) if not math.isnan(float(dist_arr[0])) else 0.0
    base_elapsed = (
        float(elapsed_arr[0])
        if (trim_start_s is not None and not math.isnan(float(elapsed_arr[0])))
        else 0.0
    )

    points: list[dict[str, Any]] = []
    for idx in indices.tolist():
        elev_v = float(elev_arr[idx])
        grade_v = float(grade_arr[idx])
        hr_v = float(hr_arr[idx])
        watts_v = float(watts_arr[idx])
        elapsed_v = float(elapsed_arr[idx])
        ts_us_v = int(ts_us_arr[idx])
        ts_iso = (
            (_UNIX_EPOCH_UTC + timedelta(microseconds=ts_us_v)).isoformat().replace("+00:00", "Z")
            if ts_us_v >= 0
            else None
        )
        points.append(
            {
                "lat": float(lat_arr[idx]),
                "lon": float(lon_arr[idx]),
                "lon_unwrapped": float(lon_u_arr[idx]),
                "elev_m": None if math.isnan(elev_v) else elev_v,
                "cum_dist_m": max(float(dist_arr[idx]) - base_dist, 0.0),
                "speed_mps": float(speed_arr[idx]),
                "grade_pct": None if math.isnan(grade_v) else grade_v,
                "hr_bpm": None if math.isnan(hr_v) else int(round(hr_v)),
                "watts": None if math.isnan(watts_v) else int(round(watts_v)),
                "elapsed_s": None if math.isnan(elapsed_v) else max(elapsed_v - base_elapsed, 0.0),
                "ts_utc": ts_iso,
                "segment_idx": int(seg_arr[idx]),
            }
        )

    return kpis, points


def query_stage_export_points(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> list[tuple[float, float, float | None, datetime | None, int]]:
    """Query trimmed `(lat, lon, elev_m, ts_utc, segment_idx)` tuples for GPX/GeoJSON export."""
    con = duckdb.connect(database=":memory:")
    try:
        _prepare_filtered_table(con, parquet_path, trim_start_s, trim_end_s)
        rows = con.execute(
            """
            SELECT
                lat::DOUBLE AS lat,
                lon::DOUBLE AS lon,
                elev_m::DOUBLE AS elev_m,
                EPOCH_US(ts_utc)::BIGINT AS ts_us,
                segment_idx::INTEGER AS segment_idx
            FROM filtered
            ORDER BY seq ASC
            """
        ).fetchall()
    finally:
        con.close()

    return [
        (
            float(r[0]),
            float(r[1]),
            float(r[2]) if r[2] is not None else None,
            _normalize_ts_utc(r[3]),
            int(r[4]) if r[4] is not None else 0,
        )
        for r in rows
    ]


def _resolve_dbt_project_dir(dbt_project_dir: Path | None) -> Path:
    if dbt_project_dir is not None:
        resolved = Path(dbt_project_dir).resolve()
        if (resolved / "dbt_project.yml").is_file():
            return resolved
        raise FileNotFoundError(f"dbt_project.yml not found in {resolved}")

    env_dir = os.environ.get("TRIP_DBT_PROJECT_DIR")
    if env_dir:
        candidate = Path(env_dir).resolve()
        if (candidate / "dbt_project.yml").is_file():
            return candidate

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "dbt"
        if (candidate / "dbt_project.yml").is_file():
            return candidate

    cwd_candidate = Path.cwd() / "dbt"
    if (cwd_candidate / "dbt_project.yml").is_file():
        return cwd_candidate

    raise FileNotFoundError("Could not locate dbt/dbt_project.yml")


def _resolve_dbt_executable() -> str:
    venv_dbt = Path(sys.executable).parent / "dbt"
    if venv_dbt.is_file():
        return str(venv_dbt)
    found = shutil.which("dbt")
    if found:
        return found
    raise FileNotFoundError("dbt executable not found in active environment or PATH")


def run_dbt_gold_build(
    silver_glob: str | Path,
    gold_dir: str | Path,
    *,
    dbt_project_dir: Path | None = None,
    ref_data_dir: Path | None = None,
    full_refresh: bool = False,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Execute `dbt build` (run + test) against live Silver Parquet files into gold_dir."""
    silver_glob_str = str(silver_glob).strip()
    if not silver_glob_str or any(tok in silver_glob_str for tok in ("'", ";", "--", "\x00")):
        raise ValueError("Invalid silver_glob path pattern")

    project_dir = _resolve_dbt_project_dir(dbt_project_dir)
    dbt_exe = _resolve_dbt_executable()

    gold_path = Path(gold_dir).resolve()
    gold_path.mkdir(parents=True, exist_ok=True)
    duckdb_path = gold_path / "trip_analytics.duckdb"
    target_dir = gold_path / ".dbt_target"
    log_dir = gold_path / ".dbt_logs"

    resolved_ref = resolve_ref_data_dir(ref_data_dir, ensure_exists=True)
    ref_dir_str = str(resolved_ref)
    if any(tok in ref_dir_str for tok in ("'", ";", "--", "\x00")):
        raise ValueError("Invalid ref_data_dir path")

    env = os.environ.copy()
    env["DBT_DUCKDB_PATH"] = str(duckdb_path)

    vars_payload = json.dumps(
        {
            "silver_parquet_glob": silver_glob_str,
            "ref_data_dir": ref_dir_str,
        }
    )
    cmd = [
        dbt_exe,
        "build",
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
        "--target-path",
        str(target_dir),
        "--log-path",
        str(log_dir),
        "--vars",
        vars_payload,
    ]
    if full_refresh:
        cmd.append("--full-refresh")
    proc = subprocess.run(  # noqa: S603
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"dbt build failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}".strip()
        )

    return {
        "status": "success",
        "duckdb_path": str(duckdb_path),
        "silver_glob": silver_glob_str,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
    }
