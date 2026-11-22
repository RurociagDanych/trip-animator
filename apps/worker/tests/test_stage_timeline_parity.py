from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trip_telemetry import (
    ParsedTrack,
    TrackPoint,
    build_silver_dataframe,
    query_stage_timeline_points,
    write_silver_parquet,
)
from trip_worker import query_stage_kpis_and_manifest_points

T0 = datetime(2001, 6, 1, 8, 0, 0, tzinfo=UTC)


def _write_parquet(tmp_path: Path, count: int) -> Path:
    points = [
        TrackPoint(
            lat=0.1 + (i % 37) * 0.0007,
            lon=0.2 + i * 0.0009,
            elev_m=1000.0 + (i * 7 % 23),
            ts_utc=T0 + timedelta(seconds=i * 3),
            segment_idx=0 if i < count // 2 else 1,
        )
        for i in range(count)
    ]
    parsed = ParsedTrack(
        name="Parity Stage",
        points=points,
        point_count=len(points),
        segment_count=2,
        has_timestamps=True,
        has_elevation=True,
    )
    df, _ = build_silver_dataframe(parsed, trip_id="trip-1", stage_id="stage-1")
    path = tmp_path / "parity.parquet"
    write_silver_parquet(df, path)
    return path


def test_timeline_points_match_manifest_extraction_for_same_input(tmp_path: Path) -> None:
    path = _write_parquet(tmp_path, 400)

    for budget in (1000, 60):
        _, manifest_points = query_stage_kpis_and_manifest_points(path, max_points=budget)
        timeline_points = query_stage_timeline_points(path, max_points=budget)

        assert len(timeline_points) == len(manifest_points)
        for timeline_pt, manifest_pt in zip(timeline_points, manifest_points, strict=True):
            assert timeline_pt.lat == manifest_pt.lat
            assert timeline_pt.lon == manifest_pt.lon
            assert timeline_pt.cum_dist_m == manifest_pt.cum_dist_m
            assert timeline_pt.elapsed_s == manifest_pt.elapsed_s
            assert timeline_pt.ts_utc == manifest_pt.ts_utc
            assert timeline_pt.segment_idx == manifest_pt.segment_idx
