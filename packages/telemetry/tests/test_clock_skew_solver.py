from __future__ import annotations

import random
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from trip_telemetry import (
    PhotoAlignmentInput,
    StageTrackInput,
    align_photos_to_stages,
    build_silver_dataframe,
    solve_camera_clock_skew,
    write_silver_parquet,
)
from trip_telemetry.analytics import ClockSkewResult
from trip_telemetry.schemas import ParsedTrack, TrackPoint

TRUE_OFFSET_S = 7200
_DAY0 = datetime(2001, 7, 20, 8, 0, 0, tzinfo=UTC)


def _write_stages(
    tmp_path: Path, *, stage_count: int, points_per_stage: int, step_s: float = 1.0
) -> tuple[list[StageTrackInput], list[list[TrackPoint]]]:
    stages: list[StageTrackInput] = []
    all_points: list[list[TrackPoint]] = []
    for k in range(stage_count):
        start = _DAY0 + timedelta(days=k)
        pts = [
            TrackPoint(
                lat=0.10 + k * 0.05 + i * 1e-5,
                lon=0.30 + k * 0.05 + i * 1.3e-5,
                elev_m=100.0 + (i % 50),
                ts_utc=start + timedelta(seconds=i * step_s),
            )
            for i in range(points_per_stage)
        ]
        df, _ = build_silver_dataframe(
            ParsedTrack(
                points=pts,
                point_count=len(pts),
                segment_count=1,
                has_timestamps=True,
                has_elevation=True,
            ),
            trip_id="t-skew",
            stage_id=f"st-{k}",
        )
        path = write_silver_parquet(df, tmp_path / f"st{k}.parquet")
        stages.append(StageTrackInput(stage_id=f"st-{k}", stage_order=k, parquet_path=path))
        all_points.append(pts)
    return stages, all_points


def _make_photos(
    all_points: list[list[TrackPoint]],
    *,
    count: int,
    gps_every: int = 1,
    seed: int = 7,
) -> list[PhotoAlignmentInput]:
    rng = random.Random(seed)  # noqa: S311
    photos: list[PhotoAlignmentInput] = []
    for n in range(count):
        pts = rng.choice(all_points)
        pt = pts[rng.randrange(len(pts))]
        assert pt.ts_utc is not None
        has_gps = n % gps_every == 0
        photos.append(
            PhotoAlignmentInput(
                id=f"p{n}",
                taken_at_utc=pt.ts_utc - timedelta(seconds=TRUE_OFFSET_S),
                lat=pt.lat + rng.uniform(-2e-5, 2e-5) if has_gps else None,
                lon=pt.lon + rng.uniform(-2e-5, 2e-5) if has_gps else None,
            )
        )
    return photos


def _reference_solve(
    photos: list[PhotoAlignmentInput],
    stages: list[StageTrackInput],
    *,
    max_time_gap_s: float = 600.0,
) -> ClockSkewResult:
    candidates = [p for p in photos if p.taken_at_utc and p.lat is not None and p.lon is not None]
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
            shifted, stages, max_time_gap_s=max_time_gap_s, max_spatial_dist_m=0.0
        )
        errors = [
            r.error_m for r in aligned if r.match_method == "timestamp" and r.error_m is not None
        ]
        if not errors:
            continue
        med_err = float(statistics.median(errors))
        matched_cnt = len(errors)
        better = matched_cnt > best_matched or (
            matched_cnt == best_matched
            and (best_median_err is None or med_err < best_median_err - 1e-3)
        )
        tie = (
            matched_cnt == best_matched
            and best_median_err is not None
            and abs(med_err - best_median_err) <= 1e-3
            and abs(offset_s) < abs(best_offset)
        )
        if better or tie:
            best_offset, best_matched, best_median_err = offset_s, matched_cnt, med_err
    return ClockSkewResult(
        offset_seconds=best_offset,
        matched_count=best_matched,
        median_error_m=round(best_median_err, 2) if best_median_err is not None else None,
    )


def test_solver_matches_reference_implementation(tmp_path: Path) -> None:
    stages, pts = _write_stages(tmp_path, stage_count=3, points_per_stage=2000, step_s=2.0)
    photos = _make_photos(pts, count=40, gps_every=4)
    photos.append(
        PhotoAlignmentInput(id="far", taken_at_utc=_DAY0 + timedelta(days=30), lat=1.0, lon=1.0)
    )

    expected = _reference_solve(photos, stages)
    actual = solve_camera_clock_skew(photos, stages)

    assert actual.offset_seconds == expected.offset_seconds
    assert actual.matched_count == expected.matched_count
    assert actual.median_error_m == pytest.approx(expected.median_error_m, abs=0.011)


def test_solver_matches_reference_with_narrow_time_gap(tmp_path: Path) -> None:
    stages, pts = _write_stages(tmp_path, stage_count=2, points_per_stage=500, step_s=30.0)
    photos = _make_photos(pts, count=25, seed=3)

    expected = _reference_solve(photos, stages, max_time_gap_s=10.0)
    actual = solve_camera_clock_skew(photos, stages, max_time_gap_s=10.0)

    assert actual.offset_seconds == expected.offset_seconds
    assert actual.matched_count == expected.matched_count


def test_solver_recovers_true_offset_and_ignores_photos_without_gps(tmp_path: Path) -> None:
    stages, pts = _write_stages(tmp_path, stage_count=3, points_per_stage=2000)
    photos = _make_photos(pts, count=30, gps_every=3)
    photos.append(PhotoAlignmentInput(id="no-ts", lat=0.1, lon=0.3))
    photos.append(PhotoAlignmentInput(id="no-anything"))

    result = solve_camera_clock_skew(photos, stages)

    assert result.offset_seconds == TRUE_OFFSET_S
    assert result.matched_count == 10
    assert result.median_error_m is not None and result.median_error_m < 5.0


def test_solver_returns_zero_when_no_photo_has_gps(tmp_path: Path) -> None:
    stages, pts = _write_stages(tmp_path, stage_count=1, points_per_stage=200)
    photos = _make_photos(pts, count=5, gps_every=100)[1:]

    result = solve_camera_clock_skew(photos, stages)

    assert result == ClockSkewResult(offset_seconds=0, matched_count=0, median_error_m=None)


def test_solver_filters_by_camera(tmp_path: Path) -> None:
    stages, pts = _write_stages(tmp_path, stage_count=1, points_per_stage=500)
    photos = [
        p.model_copy(update={"camera_make_model": "A" if i % 2 else "B"})
        for i, p in enumerate(_make_photos(pts, count=10))
    ]

    result = solve_camera_clock_skew(photos, stages, camera_make_model="A")

    assert result.offset_seconds == TRUE_OFFSET_S
    assert result.matched_count == 5


def test_solver_is_fast_on_large_project(tmp_path: Path) -> None:
    stages, pts = _write_stages(tmp_path, stage_count=20, points_per_stage=25000)
    photos = _make_photos(pts, count=100, gps_every=2)

    started = time.perf_counter()
    result = solve_camera_clock_skew(photos, stages)
    elapsed = time.perf_counter() - started

    assert result.offset_seconds == TRUE_OFFSET_S
    assert result.matched_count == 50
    assert elapsed < 5.0, f"solver took {elapsed:.2f}s"
