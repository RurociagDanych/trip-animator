"""Telemetry analytics: climbs/splits/dwells, DuckDB ASOF+spatial photo sync, and color binning."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from trip_telemetry.gpx_parser import haversine_distance_m
from trip_telemetry.ref_data import resolve_ref_data_dir
from trip_telemetry.schemas import (
    ClimbCategory,
    ClimbHighlight,
    ColorByMode,
    ColorSegment,
    DwellStop,
    MetricSplit,
    PeakEffort,
    StageEventsBundle,
    StageGeoContext,
    TransitionIcon,
)

EVENTS_SIDECAR_VERSION = 1


def _connect_duckdb_with_spatial() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    try:
        con.execute("LOAD spatial;")
    except duckdb.Error:
        con.execute("INSTALL spatial; LOAD spatial;")
    return con


def _validate_trim_bounds(trim_start_s: float | None, trim_end_s: float | None) -> None:
    if trim_start_s is not None and (not math.isfinite(trim_start_s) or trim_start_s < 0.0):
        raise ValueError("trim_start_s must be finite and >= 0")
    if trim_end_s is not None and (not math.isfinite(trim_end_s) or trim_end_s < 0.0):
        raise ValueError("trim_end_s must be finite and >= 0")
    if trim_start_s is not None and trim_end_s is not None and trim_start_s >= trim_end_s:
        raise ValueError("trim_start_s must be strictly less than trim_end_s")


def _load_active_silver_table(
    con: duckdb.DuckDBPyConnection,
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    table_name: str = "active_pts",
) -> None:
    _validate_trim_bounds(trim_start_s, trim_end_s)
    rel = con.read_parquet(str(parquet_path))
    cols = set(rel.columns)
    con.register("_raw_stage_parquet", rel)

    if trim_start_s is None and trim_end_s is None:
        where_clause = "WHERE NOT COALESCE(is_trimmed, FALSE)" if "is_trimmed" in cols else ""
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {table_name} AS "  # noqa: S608
            f"SELECT * FROM _raw_stage_parquet {where_clause} ORDER BY seq ASC"
        )
        return

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE {table_name} AS
        WITH bounds AS (
            SELECT MIN(ts_utc) AS min_ts FROM _raw_stage_parquet
        )
        SELECT p.*
        FROM _raw_stage_parquet AS p
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
    )


def _categorize_fiets(fiets_score: float) -> ClimbCategory:
    if fiets_score >= 600.0:
        return "HC"
    if fiets_score >= 300.0:
        return "Cat 1"
    if fiets_score >= 150.0:
        return "Cat 2"
    if fiets_score >= 75.0:
        return "Cat 3"
    return "Cat 4"


def _match_nearest_pass(
    con: duckdb.DuckDBPyConnection,
    passes_path: Path,
    lat: float,
    lon: float,
    max_dist_m: float = 2500.0,
) -> str | None:
    rows = con.execute(
        """
        SELECT name, elev_m, lat, lon
        FROM read_parquet(?)
        WHERE ABS(lat - ?) <= 0.05 AND ABS(lon - ?) <= 0.08
        """,
        [str(passes_path), lat, lon],
    ).fetchall()
    best_name: str | None = None
    best_dist = max_dist_m
    for name, _elev, p_lat, p_lon in rows:
        d = haversine_distance_m(lat, lon, float(p_lat), float(p_lon))
        if d <= best_dist:
            best_dist = d
            best_name = str(name)
    return best_name


def _detect_climbs(
    con: duckdb.DuckDBPyConnection,
    *,
    stage_id: str,
    passes_path: Path,
    lat_arr: np.ndarray,
    lon_arr: np.ndarray,
    elev_arr: np.ndarray,
    dist_arr: np.ndarray,
    grade_arr: np.ndarray,
    seg_arr: np.ndarray,
) -> list[ClimbHighlight]:
    n = len(lat_arr)
    if n < 3 or np.all(np.isnan(elev_arr)):
        return []

    base_dist = float(dist_arr[0])
    total_dist = max(float(dist_arr[-1]) - base_dist, 1.0)

    # Identify candidate climb spans where grade >= 3.0% (bridging short flat dips < 300m)
    spans: list[tuple[int, int]] = []
    in_climb = False
    climb_start = 0
    last_climb_idx = 0

    for i in range(1, n):
        if seg_arr[i] != seg_arr[i - 1]:
            if in_climb:
                spans.append((climb_start, last_climb_idx))
                in_climb = False
            continue

        e_prev = elev_arr[i - 1]
        e_cur = elev_arr[i]
        d_step = dist_arr[i] - dist_arr[i - 1]
        if math.isnan(e_prev) or math.isnan(e_cur):
            continue

        local_grade = (
            float(grade_arr[i])
            if not math.isnan(grade_arr[i])
            else (((e_cur - e_prev) / d_step * 100.0) if d_step >= 1.0 else 0.0)
        )
        is_up = local_grade >= 3.0 or (
            e_cur - e_prev > 0.0 and d_step >= 5.0 and local_grade >= 2.5
        )

        if not in_climb:
            if is_up:
                in_climb = True
                climb_start = max(0, i - 1)
                last_climb_idx = i
        else:
            if is_up:
                last_climb_idx = i
            else:
                dip_dist = dist_arr[i] - dist_arr[last_climb_idx]
                dip_drop = float(elev_arr[last_climb_idx] - e_cur)
                if dip_dist > 300.0 or dip_drop > 15.0:
                    spans.append((climb_start, last_climb_idx))
                    in_climb = False

    if in_climb:
        spans.append((climb_start, last_climb_idx))

    climbs: list[ClimbHighlight] = []
    for s_idx, e_idx in spans:
        if e_idx <= s_idx:
            continue
        start_d = float(dist_arr[s_idx] - base_dist)
        end_d = float(dist_arr[e_idx] - base_dist)
        climb_dist = end_d - start_d
        if climb_dist < 500.0:
            continue

        seg_elev = elev_arr[s_idx : e_idx + 1]
        valid_mask = ~np.isnan(seg_elev)
        if np.sum(valid_mask) < 2:
            continue

        diffs = np.diff(seg_elev[valid_mask])
        elev_gain = float(np.sum(diffs[diffs > 0.0]))
        if elev_gain < 30.0:
            continue

        summit_rel_idx = int(np.nanargmax(seg_elev))
        summit_abs_idx = s_idx + summit_rel_idx
        summit_elev = float(elev_arr[summit_abs_idx])
        summit_lat = float(lat_arr[summit_abs_idx])
        summit_lon = float(lon_arr[summit_abs_idx])
        summit_d = float(dist_arr[summit_abs_idx] - base_dist)

        avg_grade = (elev_gain / climb_dist) * 100.0
        sub_grades = grade_arr[s_idx : e_idx + 1]
        finite_grades = sub_grades[~np.isnan(sub_grades)]
        max_grade = float(np.max(finite_grades)) if len(finite_grades) > 0 else avg_grade

        fiets = (climb_dist / 1000.0) * (avg_grade**2)
        category = _categorize_fiets(fiets)
        matched_pass = _match_nearest_pass(con, passes_path, summit_lat, summit_lon)
        climb_num = len(climbs) + 1
        name = matched_pass or f"Climb {climb_num} ({round(summit_elev)}m)"

        climbs.append(
            ClimbHighlight(
                id=f"{stage_id}:climb:{climb_num}",
                stage_id=stage_id,
                name=name,
                category=category,
                start_dist_m=round(start_d, 1),
                end_dist_m=round(end_d, 1),
                start_progress=round(min(max(start_d / total_dist, 0.0), 1.0), 5),
                end_progress=round(min(max(end_d / total_dist, 0.0), 1.0), 5),
                summit_progress=round(min(max(summit_d / total_dist, 0.0), 1.0), 5),
                summit_lat=round(summit_lat, 6),
                summit_lon=round(summit_lon, 6),
                summit_elev_m=round(summit_elev, 1),
                distance_m=round(climb_dist, 1),
                elevation_gain_m=round(elev_gain, 1),
                avg_grade_pct=round(avg_grade, 2),
                max_grade_pct=round(max_grade, 2),
                fiets_score=round(fiets, 2),
            )
        )

    return climbs


def _detect_splits(
    con: duckdb.DuckDBPyConnection,
    *,
    stage_id: str,
    split_distance_m: float = 1000.0,
) -> list[MetricSplit]:
    rows = con.execute(
        """
        WITH base AS (
            SELECT
                seq,
                segment_idx,
                ts_utc,
                elev_m,
                cum_dist_m - FIRST_VALUE(cum_dist_m) OVER (ORDER BY seq) AS rel_dist_m,
                speed_mps,
                grade_pct,
                hr_bpm,
                watts,
                LAG(segment_idx) OVER (ORDER BY seq) AS prev_seg,
                LAG(elev_m) OVER (ORDER BY seq) AS prev_elev_m,
                LAG(ts_utc) OVER (ORDER BY seq) AS prev_ts_utc
            FROM active_pts
        ),
        bucketed AS (
            SELECT
                FLOOR(GREATEST(rel_dist_m, 0.0) / ?)::INTEGER + 1 AS split_index,
                rel_dist_m,
                speed_mps,
                grade_pct,
                hr_bpm,
                watts,
                CASE
                    WHEN prev_seg = segment_idx AND elev_m IS NOT NULL AND prev_elev_m IS NOT NULL
                    THEN elev_m - prev_elev_m
                    ELSE 0.0
                END AS d_elev,
                CASE
                    WHEN prev_seg = segment_idx AND ts_utc IS NOT NULL AND prev_ts_utc IS NOT NULL
                    THEN GREATEST(EPOCH(ts_utc) - EPOCH(prev_ts_utc), 0.0)
                    ELSE 0.0
                END AS d_sec
            FROM base
        )
        SELECT
            split_index,
            MIN(rel_dist_m)::DOUBLE AS start_dist_m,
            MAX(rel_dist_m)::DOUBLE AS end_dist_m,
            SUM(d_sec)::DOUBLE AS duration_s,
            SUM(CASE WHEN d_elev > 0 THEN d_elev ELSE 0.0 END)::DOUBLE AS elev_gain_m,
            SUM(CASE WHEN d_elev < 0 THEN ABS(d_elev) ELSE 0.0 END)::DOUBLE AS elev_loss_m,
            AVG(speed_mps)::DOUBLE AS avg_speed_mps,
            AVG(grade_pct)::DOUBLE AS avg_grade_pct,
            AVG(hr_bpm)::DOUBLE AS avg_hr_bpm,
            AVG(watts)::DOUBLE AS avg_watts
        FROM bucketed
        GROUP BY split_index
        ORDER BY split_index ASC
        """,
        [split_distance_m],
    ).fetchall()

    splits: list[MetricSplit] = []
    for row in rows:
        start_d = float(row[1] or 0.0)
        end_d = float(row[2] or 0.0)
        dist_m = max(end_d - start_d, 0.0)
        dur_s = float(row[3] or 0.0)
        avg_spd = (dist_m / dur_s) if dur_s > 0.0 else float(row[6] or 0.0)
        splits.append(
            MetricSplit(
                split_index=int(row[0]),
                stage_id=stage_id,
                start_dist_m=round(start_d, 1),
                end_dist_m=round(end_d, 1),
                distance_m=round(dist_m, 1),
                duration_s=round(dur_s, 1),
                elev_gain_m=round(float(row[4] or 0.0), 1),
                elev_loss_m=round(float(row[5] or 0.0), 1),
                avg_speed_mps=round(avg_spd, 3),
                avg_grade_pct=round(float(row[7]), 2) if row[7] is not None else None,
                avg_hr_bpm=round(float(row[8]), 1) if row[8] is not None else None,
                avg_watts=round(float(row[9]), 1) if row[9] is not None else None,
            )
        )
    return splits


def _suggest_dwell_icon(started_at: datetime | None, duration_s: float) -> TransitionIcon:
    if duration_s >= 4.0 * 3600.0:
        return "tent"
    if duration_s >= 1800.0 and started_at is not None:
        hour = started_at.hour
        if 11 <= hour <= 14 or 18 <= hour <= 21:
            return "meal"
    return "coffee"


def _detect_dwell_stops(
    *,
    stage_id: str,
    lat_arr: np.ndarray,
    lon_arr: np.ndarray,
    dist_arr: np.ndarray,
    speed_arr: np.ndarray,
    moving_arr: np.ndarray,
    ts_us_arr: np.ndarray,
    min_duration_s: float = 180.0,
    max_radius_m: float = 30.0,
) -> list[DwellStop]:
    n = len(lat_arr)
    if n < 2 or np.all(ts_us_arr < 0):
        return []

    base_dist = float(dist_arr[0])
    total_dist = max(float(dist_arr[-1]) - base_dist, 1.0)
    stops: list[DwellStop] = []

    cluster_indices: list[int] = []

    def _flush_cluster(indices: list[int]) -> None:
        if len(indices) < 2:
            return
        t0_us = int(ts_us_arr[indices[0]])
        t1_us = int(ts_us_arr[indices[-1]])
        if t0_us < 0 or t1_us < 0:
            return
        dur_s = (t1_us - t0_us) / 1_000_000.0
        if dur_s < min_duration_s:
            return
        c_lat = float(np.mean(lat_arr[indices]))
        c_lon = float(np.mean(lon_arr[indices]))
        start_d = float(dist_arr[indices[0]] - base_dist)
        started_dt = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=t0_us)
        ended_dt = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=t1_us)
        stop_num = len(stops) + 1
        stops.append(
            DwellStop(
                id=f"{stage_id}:dwell:{stop_num}",
                stage_id=stage_id,
                lat=round(c_lat, 6),
                lon=round(c_lon, 6),
                start_dist_m=round(start_d, 1),
                progress=round(min(max(start_d / total_dist, 0.0), 1.0), 5),
                started_at_utc=started_dt,
                ended_at_utc=ended_dt,
                duration_s=round(dur_s, 1),
                suggested_icon=_suggest_dwell_icon(started_dt, dur_s),
            )
        )

    anchor_lat = 0.0
    anchor_lon = 0.0
    for i in range(n):
        is_stationary = (not bool(moving_arr[i])) or float(speed_arr[i]) < 0.5
        if not is_stationary or int(ts_us_arr[i]) < 0:
            if cluster_indices:
                _flush_cluster(cluster_indices)
                cluster_indices = []
            continue

        if not cluster_indices:
            cluster_indices = [i]
            anchor_lat = float(lat_arr[i])
            anchor_lon = float(lon_arr[i])
        else:
            d_anchor = haversine_distance_m(
                anchor_lat, anchor_lon, float(lat_arr[i]), float(lon_arr[i])
            )
            if d_anchor <= max_radius_m:
                cluster_indices.append(i)
            else:
                _flush_cluster(cluster_indices)
                cluster_indices = [i]
                anchor_lat = float(lat_arr[i])
                anchor_lon = float(lon_arr[i])

    if cluster_indices:
        _flush_cluster(cluster_indices)

    return stops


def _detect_peak_efforts(
    *,
    stage_id: str,
    dist_arr: np.ndarray,
    ts_us_arr: np.ndarray,
    watts_arr: np.ndarray,
    speed_arr: np.ndarray,
    hr_arr: np.ndarray,
    windows_s: tuple[int, ...] = (60, 300, 1200),
) -> list[PeakEffort]:
    n = len(dist_arr)
    if n < 2 or np.any(ts_us_arr < 0):
        return []

    t_sec = (ts_us_arr - ts_us_arr[0]) / 1_000_000.0
    total_dur = float(t_sec[-1])
    base_dist = float(dist_arr[0])
    efforts: list[PeakEffort] = []

    metrics: list[tuple[Literal_Metric, np.ndarray]] = [
        ("watts", watts_arr),
        ("speed_mps", speed_arr),
        ("hr_bpm", hr_arr),
    ]

    for win in windows_s:
        if total_dur < float(win):
            continue
        right_idx = 0
        pairs: list[tuple[int, int]] = []
        for left_idx in range(n):
            target_t = t_sec[left_idx] + float(win)
            while right_idx < n and t_sec[right_idx] < target_t:
                right_idx += 1
            if right_idx >= n:
                break
            pairs.append((left_idx, right_idx))
        if not pairs:
            continue

        for metric_name, arr in metrics:
            if np.all(np.isnan(arr)):
                continue
            valid = np.where(np.isnan(arr), 0.0, arr)
            counts = np.where(np.isnan(arr), 0, 1)
            csum = np.concatenate([[0.0], np.cumsum(valid)])
            ccnt = np.concatenate([[0], np.cumsum(counts)])

            best_val = -1.0
            best_pair: tuple[int, int] | None = None
            for l_idx, r_idx in pairs:
                cnt = int(ccnt[r_idx + 1] - ccnt[l_idx])
                if cnt <= 0:
                    continue
                avg_v = float(csum[r_idx + 1] - csum[l_idx]) / cnt
                if avg_v > best_val:
                    best_val = avg_v
                    best_pair = (l_idx, r_idx)

            if best_pair is not None and best_val > 0.0:
                efforts.append(
                    PeakEffort(
                        window_s=win,
                        metric=metric_name,  # type: ignore[arg-type]
                        value=round(best_val, 2),
                        stage_id=stage_id,
                        start_dist_m=round(float(dist_arr[best_pair[0]] - base_dist), 1),
                        end_dist_m=round(float(dist_arr[best_pair[1]] - base_dist), 1),
                    )
                )

    return efforts


Literal_Metric = str


def _resolve_stage_geo_context(
    con: duckdb.DuckDBPyConnection,
    *,
    stage_id: str,
    ref_dir: Path,
) -> StageGeoContext:
    countries_path = ref_dir / "countries.parquet"
    regions_path = ref_dir / "admin_regions.parquet"
    passes_path = ref_dir / "mountain_passes.parquet"

    # Sample up to ~200 evenly spaced points along stage for fast spatial lookup
    sample_rows = con.execute(
        """
        WITH numbered AS (
            SELECT lat, lon, ROW_NUMBER() OVER (ORDER BY seq) AS rn, COUNT(*) OVER () AS total_n
            FROM active_pts
        )
        SELECT lat, lon
        FROM numbered
        WHERE total_n <= 200 OR rn % GREATEST((total_n / 200)::INTEGER, 1) = 1
        """
    ).fetchall()
    if not sample_rows:
        return StageGeoContext(stage_id=stage_id)

    con.execute("CREATE OR REPLACE TEMP TABLE _geo_samples (lat DOUBLE, lon DOUBLE)")
    con.executemany("INSERT INTO _geo_samples VALUES (?, ?)", sample_rows)

    countries = [
        str(r[0])
        for r in con.execute(
            """
            SELECT DISTINCT c.name
            FROM read_parquet(?) AS c
            JOIN _geo_samples AS s
              ON s.lat BETWEEN c.min_lat AND c.max_lat
             AND s.lon BETWEEN c.min_lon AND c.max_lon
             AND ST_Intersects(c.geometry, ST_Point(s.lon, s.lat))
            ORDER BY c.name ASC
            """,
            [str(countries_path)],
        ).fetchall()
    ]

    regions = [
        str(r[0])
        for r in con.execute(
            """
            SELECT DISTINCT r.name
            FROM read_parquet(?) AS r
            JOIN _geo_samples AS s
              ON s.lat BETWEEN r.min_lat AND r.max_lat
             AND s.lon BETWEEN r.min_lon AND r.max_lon
             AND ST_Intersects(r.geometry, ST_Point(s.lon, s.lat))
            ORDER BY r.name ASC
            """,
            [str(regions_path)],
        ).fetchall()
    ]

    candidate_passes = con.execute(
        """
        SELECT DISTINCT p.name, p.lat, p.lon
        FROM read_parquet(?) AS p
        JOIN _geo_samples AS s
          ON ABS(p.lat - s.lat) <= 0.04 AND ABS(p.lon - s.lon) <= 0.06
        """,
        [str(passes_path)],
    ).fetchall()
    passes_near: list[str] = []
    for p_name, p_lat, p_lon in candidate_passes:
        if any(
            haversine_distance_m(float(s_lat), float(s_lon), float(p_lat), float(p_lon)) <= 2000.0
            for s_lat, s_lon in sample_rows
        ):
            passes_near.append(str(p_name))

    return StageGeoContext(
        stage_id=stage_id,
        countries=countries,
        regions=regions,
        passes=sorted(dict.fromkeys(passes_near)),
    )


def extract_stage_highlights(
    parquet_path: Path,
    *,
    stage_id: str,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    ref_dir: Path | None = None,
    split_distance_m: float = 1000.0,
) -> StageEventsBundle:
    """Compute climbs, metric splits, dwell stops, peak efforts, and geo context for a stage."""
    resolved_ref = resolve_ref_data_dir(ref_dir, ensure_exists=True)
    con = _connect_duckdb_with_spatial()
    try:
        _load_active_silver_table(
            con,
            parquet_path,
            trim_start_s=trim_start_s,
            trim_end_s=trim_end_s,
        )
        np_cols = con.execute(
            """
            SELECT
                lat::DOUBLE AS lat,
                lon::DOUBLE AS lon,
                elev_m::DOUBLE AS elev_m,
                cum_dist_m::DOUBLE AS cum_dist_m,
                speed_mps::DOUBLE AS speed_mps,
                grade_pct::DOUBLE AS grade_pct,
                hr_bpm::DOUBLE AS hr_bpm,
                watts::DOUBLE AS watts,
                segment_idx::INTEGER AS segment_idx,
                COALESCE(is_moving, TRUE)::BOOLEAN AS is_moving,
                COALESCE(EPOCH_US(ts_utc), -1)::BIGINT AS ts_us
            FROM active_pts
            ORDER BY seq ASC
            """
        ).fetchnumpy()

        lat_arr = np.asarray(np_cols["lat"], dtype=np.float64)
        lon_arr = np.asarray(np_cols["lon"], dtype=np.float64)
        elev_arr = np.asarray(np_cols["elev_m"], dtype=np.float64)
        dist_arr = np.asarray(np_cols["cum_dist_m"], dtype=np.float64)
        speed_arr = np.asarray(np_cols["speed_mps"], dtype=np.float64)
        grade_arr = np.asarray(np_cols["grade_pct"], dtype=np.float64)
        hr_arr = np.asarray(np_cols["hr_bpm"], dtype=np.float64)
        watts_arr = np.asarray(np_cols["watts"], dtype=np.float64)
        seg_arr = np.asarray(np_cols["segment_idx"], dtype=np.int32)
        moving_arr = np.asarray(np_cols["is_moving"], dtype=bool)
        ts_us_arr = np.asarray(np_cols["ts_us"], dtype=np.int64)

        if len(lat_arr) == 0:
            return StageEventsBundle(
                stage_id=stage_id,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s,
                geo_context=StageGeoContext(stage_id=stage_id),
            )

        climbs = _detect_climbs(
            con,
            stage_id=stage_id,
            passes_path=resolved_ref / "mountain_passes.parquet",
            lat_arr=lat_arr,
            lon_arr=lon_arr,
            elev_arr=elev_arr,
            dist_arr=dist_arr,
            grade_arr=grade_arr,
            seg_arr=seg_arr,
        )
        splits = _detect_splits(con, stage_id=stage_id, split_distance_m=split_distance_m)
        dwells = _detect_dwell_stops(
            stage_id=stage_id,
            lat_arr=lat_arr,
            lon_arr=lon_arr,
            dist_arr=dist_arr,
            speed_arr=speed_arr,
            moving_arr=moving_arr,
            ts_us_arr=ts_us_arr,
        )
        peaks = _detect_peak_efforts(
            stage_id=stage_id,
            dist_arr=dist_arr,
            ts_us_arr=ts_us_arr,
            watts_arr=watts_arr,
            speed_arr=speed_arr,
            hr_arr=hr_arr,
        )
        geo_context = _resolve_stage_geo_context(con, stage_id=stage_id, ref_dir=resolved_ref)
    finally:
        con.close()

    return StageEventsBundle(
        stage_id=stage_id,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        climbs=climbs,
        splits=splits,
        dwell_stops=dwells,
        peak_efforts=peaks,
        geo_context=geo_context,
    )


def serialize_stage_events_parquet_bytes(bundle: StageEventsBundle) -> bytes:
    """Serialize a StageEventsBundle to ZSTD-compressed `_events.parquet` bytes."""
    import io

    rows_type: list[str] = ["bundle"]
    rows_id: list[str] = [bundle.stage_id]
    rows_payload: list[str] = [bundle.model_dump_json()]

    for climb in bundle.climbs:
        rows_type.append("climb")
        rows_id.append(climb.id)
        rows_payload.append(climb.model_dump_json())
    for split in bundle.splits:
        rows_type.append("split")
        rows_id.append(f"{bundle.stage_id}:split:{split.split_index}")
        rows_payload.append(split.model_dump_json())
    for dwell in bundle.dwell_stops:
        rows_type.append("dwell")
        rows_id.append(dwell.id)
        rows_payload.append(dwell.model_dump_json())
    for peak in bundle.peak_efforts:
        rows_type.append("peak")
        rows_id.append(f"{bundle.stage_id}:peak:{peak.metric}:{peak.window_s}")
        rows_payload.append(peak.model_dump_json())

    table = pa.table(
        {
            "stage_id": pa.array([bundle.stage_id] * len(rows_type), type=pa.string()),
            "event_type": pa.array(rows_type, type=pa.string()),
            "event_id": pa.array(rows_id, type=pa.string()),
            "payload_json": pa.array(rows_payload, type=pa.string()),
        }
    )
    meta = {
        b"events_version": str(EVENTS_SIDECAR_VERSION).encode("utf-8"),
        b"trim_start_s": json.dumps(bundle.trim_start_s).encode("utf-8"),
        b"trim_end_s": json.dumps(bundle.trim_end_s).encode("utf-8"),
    }
    table = table.replace_schema_metadata(meta)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def build_stage_events_parquet(
    parquet_path: Path,
    output_path: Path,
    *,
    stage_id: str,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
    ref_dir: Path | None = None,
) -> StageEventsBundle:
    """Compute stage analytics and write `_events.parquet` sidecar to disk."""
    bundle = extract_stage_highlights(
        parquet_path,
        stage_id=stage_id,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
        ref_dir=ref_dir,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(serialize_stage_events_parquet_bytes(bundle))
    return bundle


def _trim_match(a: Any, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-6)
    except (TypeError, ValueError):
        return False


def parse_stage_events_parquet_bytes(
    raw_bytes: bytes,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> StageEventsBundle | None:
    """Read and validate a `_events.parquet` sidecar payload against expected trim bounds."""
    import io

    try:
        table = pq.read_table(io.BytesIO(raw_bytes))
        meta = table.schema.metadata or {}
        if meta.get(b"events_version") != str(EVENTS_SIDECAR_VERSION).encode("utf-8"):
            return None
        cached_start = (
            json.loads(meta[b"trim_start_s"].decode("utf-8")) if b"trim_start_s" in meta else None
        )
        cached_end = (
            json.loads(meta[b"trim_end_s"].decode("utf-8")) if b"trim_end_s" in meta else None
        )
        if not _trim_match(cached_start, trim_start_s) or not _trim_match(cached_end, trim_end_s):
            return None
        types = table.column("event_type").to_pylist()
        payloads = table.column("payload_json").to_pylist()
        for etype, payload in zip(types, payloads, strict=True):
            if etype == "bundle":
                return StageEventsBundle.model_validate_json(payload)
    except Exception:
        return None
    return None


def read_stage_events_parquet(
    events_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> StageEventsBundle | None:
    """Read and validate a `_events.parquet` sidecar file from disk."""
    if not events_path.is_file():
        return None
    return parse_stage_events_parquet_bytes(
        events_path.read_bytes(),
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )


@dataclass(frozen=True, slots=True)
class StageTrackInput:
    """Ordered stage Parquet reference for multi-stage photo alignment / pinning."""

    stage_id: str
    stage_order: int
    parquet_path: Path
    trim_start_s: float | None = None
    trim_end_s: float | None = None


class PhotoAlignmentInput(BaseModel):
    """Photo metadata passed into alignment and clock-skew solvers."""

    id: str
    taken_at_utc: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    exif_offset_s: int = 0
    camera_make_model: str | None = None
    match_method: str = "unmatched"
    stage_id: str | None = None
    matched_lat: float | None = None
    matched_lon: float | None = None
    matched_progress: float | None = None


class PhotoAlignmentResult(BaseModel):
    """Computed alignment output for one project photo."""

    photo_id: str
    stage_id: str | None = None
    matched_lat: float | None = None
    matched_lon: float | None = None
    matched_progress: float | None = None
    match_method: str = "unmatched"
    error_m: float | None = None
    time_diff_s: float | None = None


class ClockSkewResult(BaseModel):
    """Solved integer-hour camera timezone/clock offset minimizing spatial error."""

    offset_seconds: int = 0
    matched_count: int = 0
    median_error_m: float | None = None


class PinSnapResult(BaseModel):
    """Result of snapping a manual map coordinate or timeline progress to the route."""

    stage_id: str
    lat: float
    lon: float
    matched_lat: float
    matched_lon: float
    matched_progress: float = Field(ge=0.0, le=1.0)
    distance_m: float = 0.0


_UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _normalize_ts_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return _UNIX_EPOCH_UTC + timedelta(microseconds=value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return None


def _build_combined_track_table(
    con: duckdb.DuckDBPyConnection,
    stages: Sequence[StageTrackInput],
) -> float:
    ordered = sorted(stages, key=lambda s: s.stage_order)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE trip_pts (
            global_idx BIGINT,
            stage_id VARCHAR,
            stage_order INTEGER,
            segment_idx INTEGER,
            ts_utc TIMESTAMPTZ,
            lat DOUBLE,
            lon DOUBLE,
            global_dist_m DOUBLE
        )
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stage_lines (
            stage_id VARCHAR,
            stage_order INTEGER,
            start_dist_m DOUBLE,
            stage_dist_m DOUBLE,
            geom GEOMETRY
        )
        """
    )

    global_offset_m = 0.0
    global_idx = 0

    for st_idx, st in enumerate(ordered):
        _load_active_silver_table(
            con,
            st.parquet_path,
            trim_start_s=st.trim_start_s,
            trim_end_s=st.trim_end_s,
            table_name="_one_stage",
        )
        stats = con.execute(
            """
            SELECT
                COUNT(*)::BIGINT,
                COALESCE(FIRST(cum_dist_m ORDER BY seq ASC), 0.0)::DOUBLE,
                COALESCE(LAST(cum_dist_m ORDER BY seq ASC), 0.0)::DOUBLE
            FROM _one_stage
            """
        ).fetchone()
        if not stats or int(stats[0]) == 0:
            continue

        row_count = int(stats[0])
        base_d = float(stats[1])
        end_d = float(stats[2])
        stage_dist = max(end_d - base_d, 0.0)

        con.execute(
            """
            INSERT INTO trip_pts
            SELECT
                (? + ROW_NUMBER() OVER (ORDER BY seq ASC) - 1)::BIGINT AS global_idx,
                ?::VARCHAR AS stage_id,
                ?::INTEGER AS stage_order,
                COALESCE(segment_idx, 0)::INTEGER AS segment_idx,
                CAST(ts_utc AS TIMESTAMPTZ) AS ts_utc,
                lat::DOUBLE AS lat,
                lon::DOUBLE AS lon,
                (? + GREATEST(COALESCE(cum_dist_m, ?)::DOUBLE - ?, 0.0))::DOUBLE AS global_dist_m
            FROM _one_stage
            ORDER BY seq ASC
            """,
            [global_idx, st.stage_id, st_idx, global_offset_m, base_d, base_d],
        )

        if row_count == 1:
            con.execute(
                """
                INSERT INTO stage_lines
                SELECT
                    ?::VARCHAR,
                    ?::INTEGER,
                    ?::DOUBLE,
                    ?::DOUBLE,
                    ST_MakeLine(
                        ST_Point(lon::DOUBLE, lat::DOUBLE),
                        ST_Point(lon::DOUBLE, lat::DOUBLE)
                    )
                FROM _one_stage
                LIMIT 1
                """,
                [st.stage_id, st_idx, global_offset_m, stage_dist],
            )
        else:
            con.execute(
                """
                INSERT INTO stage_lines
                SELECT
                    ?::VARCHAR,
                    ?::INTEGER,
                    ?::DOUBLE,
                    ?::DOUBLE,
                    ST_MakeLine(LIST(ST_Point(lon::DOUBLE, lat::DOUBLE) ORDER BY seq ASC))
                FROM _one_stage
                """,
                [st.stage_id, st_idx, global_offset_m, stage_dist],
            )

        global_idx += row_count
        global_offset_m += stage_dist

    return global_offset_m


def align_photos_to_stages(
    photos: Sequence[PhotoAlignmentInput],
    stages: Sequence[StageTrackInput],
    *,
    max_time_gap_s: float = 300.0,
    max_spatial_dist_m: float = 500.0,
) -> list[PhotoAlignmentResult]:
    """Align photos against full-res active Silver Parquet via DuckDB ASOF JOIN + spatial."""
    if not photos:
        return []
    if not stages:
        return [
            PhotoAlignmentResult(
                photo_id=p.id,
                stage_id=p.stage_id if p.match_method == "manual" else None,
                matched_lat=p.matched_lat if p.match_method == "manual" else None,
                matched_lon=p.matched_lon if p.match_method == "manual" else None,
                matched_progress=p.matched_progress if p.match_method == "manual" else None,
                match_method="manual" if p.match_method == "manual" else "unmatched",
            )
            for p in photos
        ]

    con = _connect_duckdb_with_spatial()
    try:
        total_dist_m = _build_combined_track_table(con, stages)
        total_pts_row = con.execute("SELECT COUNT(*) FROM trip_pts").fetchone()
        total_pts = int(total_pts_row[0]) if total_pts_row else 0
        if total_pts == 0:
            return [
                PhotoAlignmentResult(
                    photo_id=p.id,
                    stage_id=p.stage_id if p.match_method == "manual" else None,
                    matched_lat=p.matched_lat if p.match_method == "manual" else None,
                    matched_lon=p.matched_lon if p.match_method == "manual" else None,
                    matched_progress=p.matched_progress if p.match_method == "manual" else None,
                    match_method="manual" if p.match_method == "manual" else "unmatched",
                )
                for p in photos
            ]

        results_by_id: dict[str, PhotoAlignmentResult] = {}
        ts_candidates: list[tuple[str, datetime, float | None, float | None]] = []
        gps_only_candidates: list[PhotoAlignmentInput] = []

        for p in photos:
            if p.match_method == "manual":
                results_by_id[p.id] = PhotoAlignmentResult(
                    photo_id=p.id,
                    stage_id=p.stage_id,
                    matched_lat=p.matched_lat,
                    matched_lon=p.matched_lon,
                    matched_progress=p.matched_progress,
                    match_method="manual",
                )
                continue

            if p.taken_at_utc is not None:
                base_ts = (
                    p.taken_at_utc.replace(tzinfo=UTC)
                    if p.taken_at_utc.tzinfo is None
                    else p.taken_at_utc.astimezone(UTC)
                )
                adj_ts = base_ts + timedelta(seconds=int(p.exif_offset_s))
                ts_candidates.append((p.id, adj_ts, p.lat, p.lon))
            elif p.lat is not None and p.lon is not None:
                gps_only_candidates.append(p)
            else:
                results_by_id[p.id] = PhotoAlignmentResult(photo_id=p.id, match_method="unmatched")

        if ts_candidates:
            con.execute(
                """
                CREATE OR REPLACE TEMP TABLE _photo_ts (
                    photo_id VARCHAR,
                    target_ts TIMESTAMPTZ,
                    p_lat DOUBLE,
                    p_lon DOUBLE
                )
                """
            )
            con.executemany("INSERT INTO _photo_ts VALUES (?, ?, ?, ?)", ts_candidates)
            con.execute(
                """
                CREATE OR REPLACE TEMP TABLE _ts_track AS
                SELECT * FROM trip_pts WHERE ts_utc IS NOT NULL ORDER BY ts_utc ASC
                """
            )
            asof_rows = con.execute(
                """
                WITH prev_match AS (
                    SELECT
                        ph.photo_id,
                        ph.target_ts,
                        ph.p_lat,
                        ph.p_lon,
                        t.stage_id,
                        t.lat,
                        t.lon,
                        t.global_dist_m,
                        t.global_idx,
                        ABS(EPOCH(ph.target_ts) - EPOCH(t.ts_utc)) AS diff_s
                    FROM _photo_ts AS ph
                    ASOF LEFT JOIN _ts_track AS t
                      ON ph.target_ts >= t.ts_utc
                ),
                next_match AS (
                    SELECT
                        ph.photo_id,
                        ph.target_ts,
                        t.stage_id,
                        t.lat,
                        t.lon,
                        t.global_dist_m,
                        t.global_idx,
                        ABS(EPOCH(t.ts_utc) - EPOCH(ph.target_ts)) AS diff_s
                    FROM _photo_ts AS ph
                    ASOF LEFT JOIN _ts_track AS t
                      ON ph.target_ts <= t.ts_utc
                )
                SELECT
                    p.photo_id,
                    p.p_lat,
                    p.p_lon,
                    CASE
                        WHEN n.diff_s IS NOT NULL AND (p.diff_s IS NULL OR n.diff_s < p.diff_s)
                        THEN n.stage_id ELSE p.stage_id
                    END AS stage_id,
                    CASE
                        WHEN n.diff_s IS NOT NULL AND (p.diff_s IS NULL OR n.diff_s < p.diff_s)
                        THEN n.lat ELSE p.lat
                    END AS m_lat,
                    CASE
                        WHEN n.diff_s IS NOT NULL AND (p.diff_s IS NULL OR n.diff_s < p.diff_s)
                        THEN n.lon ELSE p.lon
                    END AS m_lon,
                    CASE
                        WHEN n.diff_s IS NOT NULL AND (p.diff_s IS NULL OR n.diff_s < p.diff_s)
                        THEN n.global_dist_m ELSE p.global_dist_m
                    END AS m_dist,
                    CASE
                        WHEN n.diff_s IS NOT NULL AND (p.diff_s IS NULL OR n.diff_s < p.diff_s)
                        THEN n.global_idx ELSE p.global_idx
                    END AS m_idx,
                    CASE
                        WHEN n.diff_s IS NOT NULL AND (p.diff_s IS NULL OR n.diff_s < p.diff_s)
                        THEN n.diff_s ELSE p.diff_s
                    END AS best_diff_s
                FROM prev_match AS p
                JOIN next_match AS n ON p.photo_id = n.photo_id
                """
            ).fetchall()

            photo_lookup = {p.id: p for p in photos}
            for row in asof_rows:
                pid, p_lat, p_lon, st_id, m_lat, m_lon, m_dist, m_idx, diff_s = row
                if st_id is not None and diff_s is not None and float(diff_s) <= max_time_gap_s:
                    prog = (
                        float(m_dist) / total_dist_m
                        if total_dist_m > 0.0
                        else (float(m_idx) / max(total_pts - 1, 1))
                    )
                    err_m = (
                        haversine_distance_m(float(p_lat), float(p_lon), float(m_lat), float(m_lon))
                        if (p_lat is not None and p_lon is not None)
                        else None
                    )
                    results_by_id[str(pid)] = PhotoAlignmentResult(
                        photo_id=str(pid),
                        stage_id=str(st_id),
                        matched_lat=round(float(m_lat), 6),
                        matched_lon=round(float(m_lon), 6),
                        matched_progress=round(min(max(prog, 0.0), 1.0), 6),
                        match_method="timestamp",
                        error_m=round(err_m, 2) if err_m is not None else None,
                        time_diff_s=round(float(diff_s), 2),
                    )
                else:
                    orig = photo_lookup[str(pid)]
                    if orig.lat is not None and orig.lon is not None:
                        gps_only_candidates.append(orig)
                    else:
                        results_by_id[str(pid)] = PhotoAlignmentResult(
                            photo_id=str(pid), match_method="unmatched"
                        )

        if gps_only_candidates:
            for p in gps_only_candidates:
                assert p.lat is not None and p.lon is not None
                snap_rows = con.execute(
                    """
                    SELECT
                        stage_id,
                        start_dist_m,
                        stage_dist_m,
                        ST_LineLocatePoint(geom, ST_Point(?, ?)) AS frac,
                        ST_Y(
                            ST_LineInterpolatePoint(
                                geom, ST_LineLocatePoint(geom, ST_Point(?, ?))
                            )
                        ) AS snap_lat,
                        ST_X(
                            ST_LineInterpolatePoint(
                                geom, ST_LineLocatePoint(geom, ST_Point(?, ?))
                            )
                        ) AS snap_lon
                    FROM stage_lines
                    """,
                    [p.lon, p.lat, p.lon, p.lat, p.lon, p.lat],
                ).fetchall()
                best_row: tuple[str, float, float, float] | None = None
                best_d = max_spatial_dist_m
                for st_id, start_d, st_dist, frac, s_lat, s_lon in snap_rows:
                    d_m = haversine_distance_m(p.lat, p.lon, float(s_lat), float(s_lon))
                    if d_m <= best_d:
                        best_d = d_m
                        g_dist = float(start_d) + float(frac) * float(st_dist)
                        prog = g_dist / total_dist_m if total_dist_m > 0.0 else 0.0
                        best_row = (str(st_id), float(s_lat), float(s_lon), prog)

                if best_row is not None:
                    results_by_id[p.id] = PhotoAlignmentResult(
                        photo_id=p.id,
                        stage_id=best_row[0],
                        matched_lat=round(best_row[1], 6),
                        matched_lon=round(best_row[2], 6),
                        matched_progress=round(min(max(best_row[3], 0.0), 1.0), 6),
                        match_method="gps",
                        error_m=round(best_d, 2),
                    )
                else:
                    results_by_id[p.id] = PhotoAlignmentResult(
                        photo_id=p.id, match_method="unmatched"
                    )

        return [results_by_id[p.id] for p in photos]
    finally:
        con.close()


def solve_camera_clock_skew(
    photos: Sequence[PhotoAlignmentInput],
    stages: Sequence[StageTrackInput],
    *,
    camera_make_model: str | None = None,
    max_time_gap_s: float = 600.0,
) -> ClockSkewResult:
    """Solve median integer-hour camera clock offset [-12h..+14h] minimizing spatial error."""
    candidates = [
        p
        for p in photos
        if p.taken_at_utc is not None
        and p.lat is not None
        and p.lon is not None
        and (camera_make_model is None or p.camera_make_model == camera_make_model)
    ]
    if not candidates or not stages:
        return ClockSkewResult(offset_seconds=0, matched_count=0, median_error_m=None)

    best_offset = 0
    best_matched = 0
    best_median_err: float | None = None

    for delta_h in range(-12, 15):
        offset_s = delta_h * 3600
        shifted = [
            p.model_copy(update={"exif_offset_s": offset_s, "match_method": "unmatched"})
            for p in candidates
        ]
        aligned = align_photos_to_stages(
            shifted,
            stages,
            max_time_gap_s=max_time_gap_s,
            max_spatial_dist_m=0.0,  # evaluate timestamp match error only
        )
        errors = [
            r.error_m for r in aligned if r.match_method == "timestamp" and r.error_m is not None
        ]
        if not errors:
            continue
        med_err = float(statistics.median(errors))
        matched_cnt = len(errors)

        better_count = matched_cnt > best_matched
        same_count_better_err = matched_cnt == best_matched and (
            best_median_err is None or med_err < best_median_err - 1e-3
        )
        tie_break_smaller_shift = (
            matched_cnt == best_matched
            and best_median_err is not None
            and abs(med_err - best_median_err) <= 1e-3
            and abs(offset_s) < abs(best_offset)
        )
        if better_count or same_count_better_err or tie_break_smaller_shift:
            best_offset = offset_s
            best_matched = matched_cnt
            best_median_err = med_err

    return ClockSkewResult(
        offset_seconds=best_offset,
        matched_count=best_matched,
        median_error_m=round(best_median_err, 2) if best_median_err is not None else None,
    )


def snap_photo_pin(
    stages: Sequence[StageTrackInput],
    *,
    lat: float | None = None,
    lon: float | None = None,
    progress: float | None = None,
) -> PinSnapResult:
    """Snap a map coordinate or timeline progress [0..1] onto full-res active Silver tracks."""
    if (lat is None) != (lon is None):
        raise ValueError("Provide both lat and lon, or progress")
    if lat is None and progress is None:
        raise ValueError("Either (lat, lon) or progress is required")
    if progress is not None and not (0.0 <= progress <= 1.0):
        raise ValueError("progress must be between 0.0 and 1.0")
    if not stages:
        raise ValueError("Cannot snap pin without stages")

    con = _connect_duckdb_with_spatial()
    try:
        total_dist_m = _build_combined_track_table(con, stages)
        lines = con.execute(
            """
            SELECT stage_id, start_dist_m, stage_dist_m, geom
            FROM stage_lines
            ORDER BY stage_order ASC
            """
        ).fetchall()
        if not lines:
            raise ValueError("Stages contain zero active telemetry points")

        if progress is not None:
            target_dist = float(progress) * total_dist_m
            chosen = lines[-1]
            for row in lines:
                if target_dist <= float(row[1]) + float(row[2]) + 1e-6:
                    chosen = row
                    break
            st_id, start_d, st_dist, _ = chosen
            local_frac = (
                min(max((target_dist - float(start_d)) / float(st_dist), 0.0), 1.0)
                if float(st_dist) > 0.0
                else 0.0
            )
            pt_row = con.execute(
                """
                SELECT
                    ST_Y(ST_LineInterpolatePoint(geom, ?)) AS s_lat,
                    ST_X(ST_LineInterpolatePoint(geom, ?)) AS s_lon
                FROM stage_lines
                WHERE stage_id = ?
                """,
                [local_frac, local_frac, str(st_id)],
            ).fetchone()
            assert pt_row is not None
            s_lat, s_lon = round(float(pt_row[0]), 6), round(float(pt_row[1]), 6)
            return PinSnapResult(
                stage_id=str(st_id),
                lat=s_lat,
                lon=s_lon,
                matched_lat=s_lat,
                matched_lon=s_lon,
                matched_progress=round(float(progress), 6),
                distance_m=0.0,
            )

        assert lat is not None and lon is not None
        candidates = con.execute(
            """
            SELECT
                stage_id,
                start_dist_m,
                stage_dist_m,
                ST_LineLocatePoint(geom, ST_Point(?, ?)) AS frac,
                ST_Y(
                    ST_LineInterpolatePoint(
                        geom, ST_LineLocatePoint(geom, ST_Point(?, ?))
                    )
                ) AS snap_lat,
                ST_X(
                    ST_LineInterpolatePoint(
                        geom, ST_LineLocatePoint(geom, ST_Point(?, ?))
                    )
                ) AS snap_lon
            FROM stage_lines
            """,
            [lon, lat, lon, lat, lon, lat],
        ).fetchall()
        best: PinSnapResult | None = None
        for st_id, start_d, st_dist, frac, s_lat, s_lon in candidates:
            d_m = haversine_distance_m(lat, lon, float(s_lat), float(s_lon))
            g_dist = float(start_d) + float(frac) * float(st_dist)
            prog = (g_dist / total_dist_m) if total_dist_m > 0.0 else 0.0
            if best is None or d_m < best.distance_m:
                best = PinSnapResult(
                    stage_id=str(st_id),
                    lat=round(float(lat), 6),
                    lon=round(float(lon), 6),
                    matched_lat=round(float(s_lat), 6),
                    matched_lon=round(float(s_lon), 6),
                    matched_progress=round(min(max(prog, 0.0), 1.0), 6),
                    distance_m=round(d_m, 2),
                )
        assert best is not None
        return best
    finally:
        con.close()


_COLOR_SCALES: dict[ColorByMode, list[tuple[float, str, str]]] = {
    "grade": [
        (0.0, "#38BDF8", "<0%"),
        (3.0, "#22C55E", "0-3%"),
        (6.0, "#EAB308", "3-6%"),
        (9.0, "#F97316", "6-9%"),
        (12.0, "#EF4444", "9-12%"),
        (float("inf"), "#A855F7", "12%+"),
    ],
    "speed": [
        (3.0, "#3B82F6", "<11 km/h"),
        (5.5, "#14B8A6", "11-20 km/h"),
        (8.0, "#22C55E", "20-29 km/h"),
        (11.0, "#F59E0B", "29-40 km/h"),
        (float("inf"), "#EF4444", "40+ km/h"),
    ],
    "hr": [
        (120.0, "#38BDF8", "Z1 <120"),
        (140.0, "#22C55E", "Z2 120-140"),
        (160.0, "#EAB308", "Z3 140-160"),
        (175.0, "#F97316", "Z4 160-175"),
        (float("inf"), "#EF4444", "Z5 175+"),
    ],
    "watts": [
        (150.0, "#38BDF8", "<150W"),
        (220.0, "#22C55E", "150-220W"),
        (290.0, "#EAB308", "220-290W"),
        (360.0, "#F97316", "290-360W"),
        (float("inf"), "#EF4444", "360W+"),
    ],
}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.lstrip("#")
    if len(cleaned) != 6:
        return (249, 115, 22)
    return (int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16))


def compute_color_legend_stops(color_by: ColorByMode) -> list[dict[str, Any]]:
    """Return quantized legend stops for a `color_by` mode."""
    scale = _COLOR_SCALES.get(color_by)
    if not scale:
        return []
    stops: list[dict[str, Any]] = []
    prev = float("-inf")
    for upper, hex_color, label in scale:
        stops.append(
            {
                "color": hex_color,
                "rgb": _hex_to_rgb(hex_color),
                "label": label,
                "min_val": None if math.isinf(prev) else prev,
                "max_val": None if math.isinf(upper) else upper,
            }
        )
        prev = upper
    return stops


def _extract_metric_value(point: Mapping[str, Any], color_by: ColorByMode) -> float | None:
    field_map = {
        "grade": "grade_pct",
        "speed": "speed_mps",
        "hr": "hr_bpm",
        "watts": "watts",
    }
    key = field_map.get(color_by)
    if key is None:
        return None
    raw = point.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if math.isfinite(val) else None


def _classify_bin(
    val: float | None,
    color_by: ColorByMode,
    fallback_color: str,
) -> tuple[str, str, float, float]:
    scale = _COLOR_SCALES.get(color_by)
    if val is None or not scale:
        return fallback_color, "default", 0.0, 0.0
    prev = 0.0
    for upper, hex_color, label in scale:
        if val < upper or math.isinf(upper):
            return hex_color, label, prev, (val if math.isinf(upper) else upper)
        prev = upper
    return fallback_color, "default", 0.0, 0.0


def bin_track_color_segments(
    points: Sequence[Mapping[str, Any]],
    color_by: ColorByMode,
    fallback_color: str = "#F97316",
) -> list[ColorSegment]:
    """Quantize track points into contiguous overlapping RGB sub-segments for Deck.gl."""
    n = len(points)
    if n == 0:
        return []
    if color_by == "stage":
        return [
            ColorSegment(
                start_idx=0,
                end_idx=n - 1,
                color=fallback_color,
                rgb=_hex_to_rgb(fallback_color),
                bin_label="stage",
                min_val=0.0,
                max_val=0.0,
            )
        ]

    bins = [
        _classify_bin(_extract_metric_value(pt, color_by), color_by, fallback_color)
        for pt in points
    ]
    segments: list[ColorSegment] = []
    cur_start = 0
    cur_color, cur_label, cur_min, cur_max = bins[0]

    for idx in range(1, n):
        c, lbl, lo, hi = bins[idx]
        if c != cur_color:
            segments.append(
                ColorSegment(
                    start_idx=cur_start,
                    end_idx=idx,
                    color=cur_color,
                    rgb=_hex_to_rgb(cur_color),
                    bin_label=cur_label,
                    min_val=cur_min,
                    max_val=cur_max,
                )
            )
            cur_start = idx
            cur_color, cur_label, cur_min, cur_max = c, lbl, lo, hi
        else:
            cur_min = min(cur_min, lo)
            cur_max = max(cur_max, hi)

    segments.append(
        ColorSegment(
            start_idx=cur_start,
            end_idx=n - 1,
            color=cur_color,
            rgb=_hex_to_rgb(cur_color),
            bin_label=cur_label,
            min_val=cur_min,
            max_val=cur_max,
        )
    )
    return segments
