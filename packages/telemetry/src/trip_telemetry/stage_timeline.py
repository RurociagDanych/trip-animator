"""Stage Silver telemetry extraction shared by render manifests and the editor timeline."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import numpy.typing as npt

# Per-stage budget for the editor timeline (distance to timestamp samples).
# The web app interpolates linearly between kept points, so the placement error of a
# photo is bounded by the spacing between neighbours. 2_500 points keep the mean spacing
# at 80 m on a 200 km stage (about 100 m at 250 km) and the JSON near 120 KB. Raw steps
# longer than the gap thresholds below are always kept on both sides so a pause or a
# recording hole is never interpolated across. The render manifest keeps its own, larger
# budget and is unaffected.
STAGE_TIMELINE_MAX_POINTS = 2_500

# A raw step covering more than this distance or time is treated as a gap whose two
# endpoints must survive downsampling.
TIMELINE_GAP_DIST_M = 100.0
TIMELINE_GAP_TIME_S = 300.0


@dataclass(frozen=True)
class StageTelemetryRow:
    """One LTTB-selected Silver telemetry sample with trim-relative distance and time."""

    lat: float
    lon: float
    lon_unwrapped: float
    elev_m: float | None
    cum_dist_m: float
    speed_mps: float
    grade_pct: float | None
    hr_bpm: int | None
    cadence: int | None
    watts: int | None
    elapsed_s: float | None
    ts_utc: str | None
    segment_idx: int


@dataclass(frozen=True)
class StageTimelinePoint:
    """Downsampled stage sample: trim-relative distance and real UTC epoch milliseconds."""

    cum_dist_m: float
    ts_ms: int


def validate_trim(trim_start_s: float | None, trim_end_s: float | None) -> None:
    """Reject non-finite, negative or inverted trim bounds in seconds from stage start."""
    if trim_start_s is not None and (not math.isfinite(trim_start_s) or trim_start_s < 0.0):
        raise ValueError("trim_start_s must be finite and >= 0")
    if trim_end_s is not None and (not math.isfinite(trim_end_s) or trim_end_s < 0.0):
        raise ValueError("trim_end_s must be finite and >= 0")
    if trim_start_s is not None and trim_end_s is not None and trim_start_s >= trim_end_s:
        raise ValueError("trim_start_s must be strictly less than trim_end_s")


_UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def format_ts_iso(value: Any) -> str | None:
    """Format a UTC epoch-microsecond integer or datetime as ISO 8601 with a Z suffix."""
    if value is None:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        utc_dt = _UNIX_EPOCH_UTC + timedelta(microseconds=int(value))
        return utc_dt.isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        utc_dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return utc_dt.isoformat().replace("+00:00", "Z")
    return None


def iso_to_epoch_ms(ts_iso: str) -> int:
    """Convert an ISO 8601 UTC string produced by `format_ts_iso` to epoch milliseconds."""
    parsed = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    delta = parsed - _UNIX_EPOCH_UTC
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def fill_stride_indices(
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


def extract_numeric_array(
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
    keep_indices: Iterable[int] | None = None,
) -> list[int]:
    """Vectorized NumPy LTTB downsampling preserving endpoints and segment transitions.

    `keep_indices` are additional indices that must survive selection (budget permitting).
    """
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
    if keep_indices is not None:
        must_keep.update(int(i) for i in keep_indices if 0 <= int(i) < n)
    if segment_idx is not None and len(segment_idx) == n:
        changes = np.flatnonzero(segment_idx[1:] != segment_idx[:-1])
        for idx in changes:
            must_keep.add(int(idx))
            must_keep.add(int(idx + 1))

    if len(must_keep) >= threshold:
        return fill_stride_indices(n, must_keep, threshold)

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
        return fill_stride_indices(n, set(merged), threshold)

    # Too many merged indices due to segment boundaries: prioritize must_keep then stride LTTB
    extras = [i for i in selected if i not in must_keep]
    need = threshold - len(must_keep)
    if need <= 0:
        return fill_stride_indices(n, must_keep, threshold)
    if len(extras) <= need:
        return sorted(must_keep | set(extras))
    step = (len(extras) - 1) / (need - 1) if need > 1 else 0.0
    chosen_extras = (
        {extras[round(i * step)] for i in range(need)} if need > 1 else {extras[len(extras) // 2]}
    )
    return sorted(must_keep | chosen_extras)


def timeline_gap_indices(
    cum_dist: npt.NDArray[np.float64],
    dist_valid: npt.NDArray[np.bool_],
    ts_us: npt.NDArray[np.int64],
    ts_valid: npt.NDArray[np.bool_],
    max_steps: int,
) -> list[int]:
    """Return both endpoints of the largest raw steps exceeding the timeline gap thresholds.

    At most `max_steps` steps are kept, ranked by how far they exceed the thresholds.
    """
    if len(cum_dist) < 2 or max_steps <= 0:
        return []
    both_dist = dist_valid[1:] & dist_valid[:-1]
    both_ts = ts_valid[1:] & ts_valid[:-1]
    d_dist = np.where(both_dist, cum_dist[1:] - cum_dist[:-1], 0.0)
    d_time = np.where(both_ts, (ts_us[1:] - ts_us[:-1]) / 1_000_000.0, 0.0)
    score = np.maximum(d_dist / TIMELINE_GAP_DIST_M, d_time / TIMELINE_GAP_TIME_S)
    steps = np.flatnonzero(score > 1.0)
    if len(steps) > max_steps:
        steps = steps[np.argsort(score[steps])[-max_steps:]]
    return sorted({int(i) for i in steps} | {int(i) + 1 for i in steps})


def query_stage_telemetry_rows(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = 12_000,
    keep_gaps: bool = False,
) -> tuple[dict[str, Any], list[StageTelemetryRow]]:
    """Single-pass DuckDB `.fetchnumpy()` + vectorized NumPy KPIs and LTTB downsampling.

    With `keep_gaps`, both endpoints of long raw steps (see `timeline_gap_indices`) are
    kept. The default leaves selection identical to the render manifest.
    """
    validate_trim(trim_start_s, trim_end_s)

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

    lat, _ = extract_numeric_array(arrays["lat"], dtype=np.float64, fill_value=0.0)
    n = int(len(lat))
    if n == 0:
        raise ValueError("Stage Silver Parquet contains zero active telemetry points")

    lon, _ = extract_numeric_array(arrays["lon"], dtype=np.float64, fill_value=0.0)
    lon_unwrapped, _ = extract_numeric_array(
        arrays["lon_unwrapped"], dtype=np.float64, fill_value=0.0
    )
    elev_m, elev_valid = extract_numeric_array(
        arrays["elev_m"], dtype=np.float64, fill_value=np.nan
    )
    cum_dist, dist_valid = extract_numeric_array(
        arrays["cum_dist_m"], dtype=np.float64, fill_value=0.0
    )
    speed_mps, speed_valid = extract_numeric_array(
        arrays["speed_mps"], dtype=np.float64, fill_value=0.0
    )
    grade_pct, grade_valid = extract_numeric_array(
        arrays["grade_pct"], dtype=np.float64, fill_value=np.nan
    )
    elapsed_s, elapsed_valid = extract_numeric_array(
        arrays["elapsed_s"], dtype=np.float64, fill_value=np.nan
    )
    ts_us, ts_valid = extract_numeric_array(arrays["ts_us"], dtype=np.int64, fill_value=0)
    segment_idx, _ = extract_numeric_array(arrays["segment_idx"], dtype=np.int32, fill_value=0)
    is_moving, _ = extract_numeric_array(arrays["is_moving"], dtype=bool, fill_value=1.0)
    hr_bpm, hr_valid = extract_numeric_array(arrays["hr_bpm"], dtype=np.float64, fill_value=np.nan)
    cadence, cadence_valid = extract_numeric_array(
        arrays["cadence"], dtype=np.float64, fill_value=np.nan
    )
    watts, watts_valid = extract_numeric_array(arrays["watts"], dtype=np.float64, fill_value=np.nan)

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

        gap_indices = (
            timeline_gap_indices(cum_dist, dist_valid, ts_us, ts_valid, max_points // 4)
            if keep_gaps
            else None
        )
        selected_indices = vectorized_lttb_indices(
            lttb_xs, lttb_ys, max_points, segment_idx=segment_idx, keep_indices=gap_indices
        )
    else:
        selected_indices = list(range(n))

    has_trim = trim_start_s is not None or trim_end_s is not None
    first_dist = float(cum_dist[0]) if (has_trim and dist_valid[0]) else 0.0
    first_elapsed = float(elapsed_s[0]) if (has_trim and elapsed_valid[0]) else 0.0

    rows: list[StageTelemetryRow] = []
    for idx in selected_indices:
        cum_dist_val = max(float(cum_dist[idx]) - first_dist, 0.0) if dist_valid[idx] else 0.0
        elapsed_val = (
            round(max(float(elapsed_s[idx]) - first_elapsed, 0.0), 3)
            if elapsed_valid[idx]
            else None
        )
        rows.append(
            StageTelemetryRow(
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
                ts_utc=format_ts_iso(int(ts_us[idx])) if ts_valid[idx] else None,
                segment_idx=int(segment_idx[idx]),
            )
        )

    return kpis, rows


def query_stage_timeline_points(
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    max_points: int = STAGE_TIMELINE_MAX_POINTS,
) -> list[StageTimelinePoint]:
    """Return trim-relative points with real UTC epoch milliseconds from Silver Parquet.

    Distance origin and trim handling match the render manifest; selection additionally
    keeps both sides of long raw gaps. Points without a timestamp are dropped, and
    distances are rounded to 0.1 m. Raises ValueError for an invalid trim or a stage
    without active telemetry points.
    """
    _, rows = query_stage_telemetry_rows(
        parquet_path,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        max_points=max_points,
        keep_gaps=True,
    )
    return [
        StageTimelinePoint(
            cum_dist_m=round(row.cum_dist_m, 1),
            ts_ms=iso_to_epoch_ms(row.ts_utc),
        )
        for row in rows
        if row.ts_utc is not None
    ]
