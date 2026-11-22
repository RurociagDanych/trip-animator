from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from trip_telemetry import (
    ParsedTrack,
    TrackPoint,
    build_silver_dataframe,
    query_stage_timeline_points,
    write_silver_parquet,
)

T0 = datetime(2001, 6, 1, 8, 0, 0, tzinfo=UTC)


def _write_track(
    tmp_path: Path,
    points: list[TrackPoint],
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> Path:
    parsed = ParsedTrack(
        name="Timeline Stage",
        points=points,
        point_count=len(points),
        segment_count=len({p.segment_idx for p in points}),
        has_timestamps=True,
        has_elevation=True,
    )
    df, _ = build_silver_dataframe(
        parsed,
        trip_id="trip-1",
        stage_id="stage-1",
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    path = tmp_path / "stage.parquet"
    write_silver_parquet(df, path)
    return path


def _track_points() -> list[TrackPoint]:
    points: list[TrackPoint] = []
    for i in range(10):
        points.append(
            TrackPoint(
                lat=0.1 + i * 0.001,
                lon=0.2 + i * 0.001,
                elev_m=1000.0 + i,
                ts_utc=T0 + timedelta(seconds=60 * i),
                segment_idx=0 if i < 6 else 1,
            )
        )
    return points


def test_timeline_points_carry_real_timestamps_and_segment_index(tmp_path: Path) -> None:
    path = _write_track(tmp_path, _track_points())

    points = query_stage_timeline_points(path, max_points=100)

    assert len(points) == 10
    assert points[0].ts_utc == "2001-06-01T08:00:00Z"
    assert points[5].ts_utc == "2001-06-01T08:05:00Z"
    assert points[9].ts_utc == "2001-06-01T08:09:00Z"
    assert [p.segment_idx for p in points] == [0] * 6 + [1] * 4
    assert points[0].cum_dist_m == 0.0
    assert points[-1].cum_dist_m > points[0].cum_dist_m
    assert all(
        later.cum_dist_m >= earlier.cum_dist_m
        for earlier, later in zip(points, points[1:], strict=False)
    )


def test_timeline_points_are_relative_to_trim_start(tmp_path: Path) -> None:
    path = _write_track(tmp_path, _track_points(), trim_start_s=120.0, trim_end_s=480.0)

    points = query_stage_timeline_points(path, trim_start_s=120.0, trim_end_s=480.0, max_points=100)

    assert points[0].ts_utc == "2001-06-01T08:02:00Z"
    assert points[0].cum_dist_m == 0.0
    assert points[0].elapsed_s == 0.0
    assert points[-1].ts_utc == "2001-06-01T08:08:00Z"
    assert points[-1].elapsed_s == pytest.approx(360.0)


def test_timeline_points_respect_budget_and_keep_endpoints(tmp_path: Path) -> None:
    path = _write_track(tmp_path, _track_points())

    points = query_stage_timeline_points(path, max_points=4)

    assert len(points) <= 4
    assert points[0].ts_utc == "2001-06-01T08:00:00Z"
    assert points[-1].ts_utc == "2001-06-01T08:09:00Z"


def test_timeline_points_reject_invalid_trim(tmp_path: Path) -> None:
    path = _write_track(tmp_path, _track_points())

    with pytest.raises(ValueError, match="strictly less"):
        query_stage_timeline_points(path, trim_start_s=20.0, trim_end_s=10.0)
