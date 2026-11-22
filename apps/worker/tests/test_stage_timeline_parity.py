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
            lat=0.1 + (i % 37) * 0.00002,
            lon=0.2 + i * 0.00003,
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


def _iso_to_ms(ts_iso: str) -> int:
    return round(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000)


def test_timeline_points_match_manifest_extraction_for_same_input(tmp_path: Path) -> None:
    path = _write_parquet(tmp_path, 400)

    for budget in (1000, 60):
        _, manifest_points = query_stage_kpis_and_manifest_points(path, max_points=budget)
        timeline_points = query_stage_timeline_points(path, max_points=budget)

        # The timeline only exposes distance and time; compare that subset. Steps stay
        # below the gap thresholds, so gap retention adds nothing and selection matches.
        assert len(timeline_points) == len(manifest_points)
        for timeline_pt, manifest_pt in zip(timeline_points, manifest_points, strict=True):
            assert manifest_pt.ts_utc is not None
            assert timeline_pt.cum_dist_m == round(manifest_pt.cum_dist_m, 1)
            assert timeline_pt.ts_ms == _iso_to_ms(manifest_pt.ts_utc)


def test_timeline_points_are_a_subset_of_full_manifest_points(
    tmp_path: Path,
) -> None:
    path = _write_parquet(tmp_path, 400)
    _, full = query_stage_kpis_and_manifest_points(path, max_points=100_000)
    full_pairs = {(round(p.cum_dist_m, 1), _iso_to_ms(p.ts_utc)) for p in full if p.ts_utc}

    timeline_points = query_stage_timeline_points(path, max_points=60)

    assert {(p.cum_dist_m, p.ts_ms) for p in timeline_points} <= full_pairs
