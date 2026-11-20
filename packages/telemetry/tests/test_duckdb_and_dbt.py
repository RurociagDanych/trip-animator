import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from trip_telemetry import (
    LOD_TOLERANCES_DEG,
    VALID_ACTIVITY_TYPES,
    ParsedTrack,
    RenderSpecV2,
    TrackPoint,
    build_lod_cache_from_parquet,
    build_lod_cache_payload,
    build_lod_geojson_feature,
    build_lod_geojson_feature_from_tuples,
    build_silver_dataframe,
    extract_lod_feature_from_cache,
    infer_activity_type_from_strava,
    parse_gpx_bytes,
    query_map_geometry,
    query_stage_export_points,
    query_stage_kpis,
    query_telemetry_profile,
    run_dbt_gold_build,
    write_silver_parquet,
)
from trip_telemetry.duckdb_queries import (
    _lttb_downsample_indices,
    _normalize_ts_utc,
    _resolve_dbt_executable,
    _resolve_dbt_project_dir,
    _validate_trim_args,
)


def _build_synthetic_climb_track(num_points: int = 60) -> ParsedTrack:
    base = datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)
    points: list[TrackPoint] = []
    for i in range(num_points):
        # Sharp spike at i == 30 so LTTB selection can be verified
        elev = 1000.0 + i * 2.0 + (250.0 if i == 30 else 0.0)
        points.append(
            TrackPoint(
                lat=0.120 + i * 0.0005,
                lon=0.340 + i * 0.0005,
                elev_m=elev,
                ts_utc=base + timedelta(seconds=i * 10),
                hr_bpm=135 + (i % 25),
                cadence=85,
                watts=220 + (i % 40),
                segment_idx=0 if i < 40 else 1,
            )
        )
    return ParsedTrack(
        name="Climb Test",
        points=points,
        point_count=len(points),
        segment_count=2,
        has_timestamps=True,
        has_elevation=True,
    )


def test_duckdb_kpis_lttb_and_lod_geometry(tmp_path: Path) -> None:
    track = _build_synthetic_climb_track(60)
    df, summary = build_silver_dataframe(
        track, trip_id="trip-olap", stage_id="stage-olap", clean_signals=False
    )
    pq_path = write_silver_parquet(df, tmp_path / "silver" / "stage-olap.parquet")

    kpis = query_stage_kpis(pq_path)
    assert kpis["point_count"] == 60
    assert kpis["distance_m"] == pytest.approx(summary.distance_m)
    assert kpis["ascent_m"] == pytest.approx(summary.elevation_gain_m)
    assert kpis["descent_m"] == pytest.approx(summary.elevation_loss_m)
    assert kpis["moving_time_s"] == pytest.approx(summary.moving_seconds)
    assert kpis["elapsed_time_s"] == pytest.approx(summary.elapsed_seconds)
    assert kpis["avg_speed_mps"] > 0.0
    assert kpis["max_speed_mps"] >= kpis["avg_speed_mps"]
    assert kpis["vam"] == pytest.approx(summary.vam_m_per_h, rel=1e-3)
    assert kpis["avg_hr_bpm"] is not None
    assert kpis["avg_watts"] is not None

    trimmed_kpis = query_stage_kpis(pq_path, trim_start_s=0.0, trim_end_s=250.0)
    assert trimmed_kpis["point_count"] == 26
    assert trimmed_kpis["elapsed_time_s"] == pytest.approx(250.0)
    assert trimmed_kpis["max_elev_m"] == pytest.approx(1050.0)

    lttb_points = query_telemetry_profile(pq_path, max_points=10)
    assert len(lttb_points) == 10
    assert lttb_points[0]["dist_m"] == pytest.approx(0.0)
    assert lttb_points[0]["ts_utc"] == datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)
    assert any(p["elev_m"] == pytest.approx(1310.0) for p in lttb_points)
    for idx in range(1, len(lttb_points)):
        assert lttb_points[idx]["dist_m"] >= lttb_points[idx - 1]["dist_m"]

    with pytest.raises(ValueError, match="max_points"):
        query_telemetry_profile(pq_path, max_points=1)

    geom_full = query_map_geometry(pq_path, lod="full")
    geom_low = query_map_geometry(pq_path, lod="low")
    assert geom_full["geometry"]["type"] == "MultiLineString"
    assert geom_full["properties"]["segment_count"] == 2
    assert geom_low["properties"]["segment_count"] == 2
    assert geom_low["properties"]["point_count"] < geom_full["properties"]["point_count"]

    with pytest.raises(ValueError, match="Unsupported LOD"):
        query_map_geometry(pq_path, lod="ultra")  # type: ignore[arg-type]


def test_duckdb_missing_and_corrupt_parquet_files(tmp_path: Path) -> None:
    missing_pq = tmp_path / "does_not_exist.parquet"
    corrupt_pq = tmp_path / "corrupt.parquet"
    corrupt_pq.write_bytes(b"NOT_A_VALID_PARQUET_PAYLOAD_0123456789")

    for bad_path in (missing_pq, corrupt_pq):
        with pytest.raises(duckdb.Error):
            query_stage_kpis(bad_path)
        with pytest.raises(duckdb.Error):
            query_telemetry_profile(bad_path, max_points=10)
        with pytest.raises(duckdb.Error):
            query_map_geometry(bad_path, lod="low")
        with pytest.raises(duckdb.Error):
            query_stage_export_points(bad_path)
        with pytest.raises(duckdb.Error):
            build_lod_cache_from_parquet(bad_path)


def test_duckdb_empty_tables_and_missing_columns(tmp_path: Path) -> None:
    track = _build_synthetic_climb_track(10)
    df, _ = build_silver_dataframe(track, trip_id="trip-empty", stage_id="stage-empty")

    empty_df = df.clear()
    empty_pq = tmp_path / "empty.parquet"
    empty_df.write_parquet(empty_pq)

    empty_kpis = query_stage_kpis(empty_pq)
    assert empty_kpis["point_count"] == 0
    assert empty_kpis["distance_m"] == 0.0
    assert empty_kpis["vam"] == 0.0
    assert empty_kpis["min_elev_m"] is None
    assert empty_kpis["avg_hr_bpm"] is None

    assert query_telemetry_profile(empty_pq, max_points=10) == []
    assert query_stage_export_points(empty_pq) == []
    with pytest.raises(ValueError, match="No telemetry points found"):
        query_map_geometry(empty_pq, lod="low")
    with pytest.raises(ValueError, match="No telemetry points found"):
        build_lod_cache_from_parquet(empty_pq)

    valid_pq = write_silver_parquet(df, tmp_path / "valid.parquet")
    with pytest.raises(ValueError, match="Trim window excludes all telemetry points"):
        query_stage_kpis(valid_pq, trim_start_s=5000.0, trim_end_s=6000.0)
    assert (
        query_telemetry_profile(valid_pq, max_points=10, trim_start_s=5000.0, trim_end_s=6000.0)
        == []
    )
    assert query_stage_export_points(valid_pq, trim_start_s=5000.0, trim_end_s=6000.0) == []
    with pytest.raises(ValueError, match="No telemetry points found"):
        query_map_geometry(valid_pq, lod="low", trim_start_s=5000.0, trim_end_s=6000.0)

    df_no_trim_col = df.drop("is_trimmed").with_columns(
        df.get_column("is_moving").clear().extend_constant(False, df.height)
    )
    no_trim_pq = write_silver_parquet(df_no_trim_col, tmp_path / "no_is_trimmed.parquet")
    kpis_no_moving = query_stage_kpis(no_trim_pq)
    assert kpis_no_moving["point_count"] == 10
    assert kpis_no_moving["moving_time_s"] == 0.0
    assert kpis_no_moving["elapsed_time_s"] > 0.0
    assert kpis_no_moving["vam"] > 0.0

    no_ele_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1"><trk><trkseg>
  <trkpt lat="0.1200" lon="0.3400"><time>2001-07-01T08:00:00Z</time></trkpt>
  <trkpt lat="0.1205" lon="0.3405"><time>2001-07-01T08:01:00Z</time></trkpt>
  <trkpt lat="0.1210" lon="0.3410"><time>2001-07-01T08:02:00Z</time></trkpt>
  <trkpt lat="0.1215" lon="0.3415"><time>2001-07-01T08:03:00Z</time></trkpt>
</trkseg></trk></gpx>"""
    df_no_ele, _ = build_silver_dataframe(
        parse_gpx_bytes(no_ele_gpx), trip_id="t-noele", stage_id="s-noele"
    )
    no_ele_pq = write_silver_parquet(df_no_ele, tmp_path / "no_ele.parquet")
    prof_no_ele = query_telemetry_profile(no_ele_pq, max_points=3)
    assert len(prof_no_ele) == 3
    assert all(p["elev_m"] is None for p in prof_no_ele)

    partial_ele_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1"><trk><trkseg>
  <trkpt lat="0.1200" lon="0.3400"><time>2001-07-01T08:00:00Z</time></trkpt>
  <trkpt lat="0.1205" lon="0.3405"><ele>1100.0</ele><time>2001-07-01T08:01:00Z</time></trkpt>
  <trkpt lat="0.1210" lon="0.3410"><time>2001-07-01T08:02:00Z</time></trkpt>
  <trkpt lat="0.1215" lon="0.3415"><ele>1150.0</ele><time>2001-07-01T08:03:00Z</time></trkpt>
</trkseg></trk></gpx>"""
    df_part_ele, _ = build_silver_dataframe(
        parse_gpx_bytes(partial_ele_gpx), trip_id="t-partele", stage_id="s-partele"
    )
    part_ele_pq = write_silver_parquet(df_part_ele, tmp_path / "part_ele.parquet")
    prof_part_ele = query_telemetry_profile(part_ele_pq, max_points=3)
    assert len(prof_part_ele) == 3


def test_duckdb_boundary_limits_offsets_and_helpers(tmp_path: Path) -> None:
    track = _build_synthetic_climb_track(8)
    df, _ = build_silver_dataframe(track, trip_id="t-bound", stage_id="s-bound")
    pq_path = write_silver_parquet(df, tmp_path / "bound.parquet")

    prof_2 = query_telemetry_profile(pq_path, max_points=2)
    assert len(prof_2) == 2
    assert prof_2[0]["dist_m"] == pytest.approx(0.0)

    prof_all = query_telemetry_profile(pq_path, max_points=8)
    assert len(prof_all) == 8
    prof_over = query_telemetry_profile(pq_path, max_points=500)
    assert len(prof_over) == 8

    prof_trimmed = query_telemetry_profile(
        pq_path, max_points=4, trim_start_s=20.0, trim_end_s=70.0
    )
    assert len(prof_trimmed) == 4
    assert prof_trimmed[0]["dist_m"] == pytest.approx(0.0)
    assert prof_trimmed[-1]["dist_m"] > 0.0

    assert _lttb_downsample_indices([0.0, 1.0, 2.0], [10.0, 20.0, 30.0], 5) == [0, 1, 2]
    assert _lttb_downsample_indices([0.0, 1.0, 2.0, 3.0], [1.0, 5.0, 2.0, 4.0], 2) == [0, 3]
    assert len(_lttb_downsample_indices([0.0, 1.0, 2.0, 3.0], [1.0, 5.0, 2.0, 4.0], 3)) == 3

    for bad_start, bad_end in (
        (-0.01, None),
        (float("nan"), None),
        (None, -1.0),
        (None, float("inf")),
        (15.0, 15.0),
        (20.0, 10.0),
    ):
        with pytest.raises(ValueError):
            _validate_trim_args(bad_start, bad_end)

    assert _normalize_ts_utc(None) is None
    assert _normalize_ts_utc(True) is None
    assert _normalize_ts_utc("not-a-timestamp") is None
    assert _normalize_ts_utc(1_000_000) == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert _normalize_ts_utc(datetime(2001, 7, 1, 8, 0, 0)) == datetime(
        2001, 7, 1, 8, 0, 0, tzinfo=UTC
    )
    assert _normalize_ts_utc(
        datetime(2001, 7, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    ) == datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)


def test_dbt_resolution_and_validation_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for invalid_glob in ("", "   ", "path'; DROP TABLE--", "a;b", "a--b", "a\x00b"):
        with pytest.raises(ValueError, match="Invalid silver_glob"):
            run_dbt_gold_build(invalid_glob, tmp_path / "gold")

    fake_dbt_dir = tmp_path / "fake_dbt"
    fake_dbt_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="dbt_project.yml not found"):
        _resolve_dbt_project_dir(fake_dbt_dir)

    (fake_dbt_dir / "dbt_project.yml").write_text("name: test\n", encoding="utf-8")
    assert _resolve_dbt_project_dir(fake_dbt_dir) == fake_dbt_dir.resolve()

    monkeypatch.setenv("TRIP_DBT_PROJECT_DIR", str(fake_dbt_dir))
    assert _resolve_dbt_project_dir(None) == fake_dbt_dir.resolve()
    monkeypatch.delenv("TRIP_DBT_PROJECT_DIR", raising=False)

    monkeypatch.setattr(sys, "executable", str(tmp_path / "no_venv" / "python"))
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/dbt")
    assert _resolve_dbt_executable() == "/usr/local/bin/dbt"

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="dbt executable not found"):
        _resolve_dbt_executable()
    monkeypatch.undo()

    def _mock_failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["dbt", "build"],
            returncode=2,
            stdout="dbt stdout error details",
            stderr="dbt stderr failure",
        )

    monkeypatch.setattr(subprocess, "run", _mock_failed_run)
    with pytest.raises(RuntimeError, match="dbt build failed"):
        run_dbt_gold_build(tmp_path / "*.parquet", tmp_path / "gold_fail")


def test_dbt_gold_build_on_live_silver_parquet(tmp_path: Path) -> None:
    # Constructed GPX stages use fictional coordinates and dates for pipeline coverage.
    gpx_day1 = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk><name>Fixture Stage A</name><trkseg>
    <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele><time>2001-07-01T08:00:00Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>130</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
    </trkpt>
    <trkpt lat="0.12075" lon="0.34075"><ele>1050</ele><time>2001-07-01T08:02:00Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>148</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
    </trkpt>
    <trkpt lat="0.1215" lon="0.3415"><ele>1080</ele><time>2001-07-01T08:04:00Z</time>
      <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>156</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    gpx_day2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Fixture Stage B</name><trkseg>
    <trkpt lat="0.1500" lon="0.3900"><ele>1200</ele><time>2001-07-02T09:00:00Z</time></trkpt>
    <trkpt lat="0.1510" lon="0.3910"><ele>1260</ele><time>2001-07-02T09:03:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""

    silver_dir = tmp_path / "silver"
    df1, _ = build_silver_dataframe(
        parse_gpx_bytes(gpx_day1), trip_id="trip-gold", stage_id="stage-1"
    )
    df2, _ = build_silver_dataframe(
        parse_gpx_bytes(gpx_day2), trip_id="trip-gold", stage_id="stage-2"
    )
    write_silver_parquet(df1, silver_dir / "stage-1.parquet")
    write_silver_parquet(df2, silver_dir / "stage-2.parquet")

    gold_dir = tmp_path / "gold"
    res = run_dbt_gold_build(silver_dir / "*.parquet", gold_dir)
    assert res["status"] == "success"
    duckdb_path = Path(res["duckdb_path"])
    assert duckdb_path.is_file()

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        stages = con.execute(
            """
            SELECT stage_id, point_count, elevation_gain_m, vam_m_per_h
            FROM mart_stage_summary
            ORDER BY stage_id
            """
        ).fetchall()
        assert len(stages) == 2
        assert stages[0][0] == "stage-1"
        assert stages[0][1] == 3
        assert stages[0][2] == pytest.approx(80.0)
        assert stages[0][3] > 0.0
        assert stages[1][0] == "stage-2"
        assert stages[1][2] == pytest.approx(60.0)

        daily = con.execute(
            """
            SELECT trip_id, stage_count, total_points, total_elevation_gain_m
            FROM mart_trip_daily_summary
            ORDER BY trip_date
            """
        ).fetchall()
        assert len(daily) == 2
        assert daily[0][0] == "trip-gold"
        assert daily[0][1] == 1
        assert daily[1][3] == pytest.approx(60.0)

        h3_rows = con.execute(
            """
            SELECT
                trip_id, stage_id, year, h3_index,
                center_lat, center_lon, point_count, stage_count
            FROM mart_spatial_coverage_h3
            ORDER BY stage_id, h3_index
            """
        ).fetchall()
        assert len(h3_rows) >= 2
        assert {r[1] for r in h3_rows} == {"stage-1", "stage-2"}
        assert all(r[2] == 2001 for r in h3_rows)
        assert all(r[4] is not None and r[5] is not None for r in h3_rows)
        assert sum(int(r[6]) for r in h3_rows) == 5
        assert all(int(r[7]) == 1 for r in h3_rows)
    finally:
        con.close()


def test_dbt_gold_climbs_splits_and_dwells_from_live_parquet(tmp_path: Path) -> None:
    base = datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)
    points: list[TrackPoint] = []
    for i in range(15):
        points.append(
            TrackPoint(
                lat=0.120 + i * 0.0015,
                lon=0.340,
                elev_m=1000.0 + i * 10.0,
                ts_utc=base + timedelta(seconds=i * 60),
                hr_bpm=140,
                watts=200,
                segment_idx=0,
            )
        )
    # Four stationary timestamps span the model's 180-second dwell threshold.
    points[-1].moving = False
    for i in range(1, 4):
        points.append(
            TrackPoint(
                lat=points[-1].lat,
                lon=points[-1].lon,
                elev_m=1140.0,
                ts_utc=base + timedelta(seconds=14 * 60 + i * 60),
                moving=False,
                segment_idx=0,
            )
        )
    track = ParsedTrack(
        name="Gold analytics integration",
        points=points,
        point_count=len(points),
        segment_count=1,
        has_timestamps=True,
        has_elevation=True,
    )
    frame, _ = build_silver_dataframe(track, trip_id="trip-analytics", stage_id="stage-analytics")
    silver_path = write_silver_parquet(frame, tmp_path / "silver" / "stage.parquet")
    result = run_dbt_gold_build(silver_path, tmp_path / "gold")
    assert result["status"] == "success"

    con = duckdb.connect(str(result["duckdb_path"]), read_only=True)
    try:
        climbs = con.execute(
            "SELECT stage_id, distance_m, elevation_gain_m FROM fct_climbs"
        ).fetchall()
        assert len(climbs) == 1
        assert climbs[0][0] == "stage-analytics"
        assert climbs[0][1] >= 500.0
        assert climbs[0][2] >= 30.0

        splits = con.execute(
            "SELECT split_index, distance_m FROM fct_splits ORDER BY split_index"
        ).fetchall()
        assert len(splits) >= 2
        assert splits[0][0] == 1
        assert splits[0][1] > 0.0
        assert splits[-1][0] >= 2

        dwells = con.execute("SELECT duration_s, suggested_icon FROM fct_dwell_stops").fetchall()
        assert len(dwells) == 1
        assert dwells[0][0] >= 180.0
        assert dwells[0][1] == "coffee"
    finally:
        con.close()


def test_lod_cache_tuple_parity_export_and_activity_inference(tmp_path: Path) -> None:
    track = _build_synthetic_climb_track(50)
    df, _ = build_silver_dataframe(track, trip_id="trip-cache", stage_id="stage-cache")
    pq_path = write_silver_parquet(df, tmp_path / "stage-cache.parquet")

    tuples = [(p.lat, p.lon, p.segment_idx) for p in track.points]
    feat_tp = build_lod_geojson_feature(
        track.points, lod="medium", tolerance_deg=LOD_TOLERANCES_DEG["medium"]
    )
    feat_tuple = build_lod_geojson_feature_from_tuples(
        tuples, lod="medium", tolerance_deg=LOD_TOLERANCES_DEG["medium"]
    )
    assert feat_tuple == feat_tp

    gz_from_pts = build_lod_cache_payload(track.points)
    gz_from_pq = build_lod_cache_from_parquet(pq_path)
    feat_cached = extract_lod_feature_from_cache(gz_from_pq, "medium")
    assert feat_cached == feat_tp
    assert extract_lod_feature_from_cache(gz_from_pts, "low") is not None
    assert extract_lod_feature_from_cache(gz_from_pq, "medium", trim_start_s=10.0) is None
    assert extract_lod_feature_from_cache(b"not-gzip", "medium") is None

    export_pts = query_stage_export_points(pq_path, trim_start_s=0.0, trim_end_s=100.0)
    assert len(export_pts) == 11
    assert export_pts[0][2] == pytest.approx(1000.0)

    default_spec = RenderSpecV2()
    assert default_spec.camera_preset == "flat"
    assert default_spec.camera_bearing_deg == 0.0
    preset_spec = RenderSpecV2(camera_preset="drone-orbit", camera_bearing_deg=-45.0)
    assert preset_spec.camera_preset == "drone-orbit"
    assert preset_spec.camera_bearing_deg == -45.0
    with pytest.raises(ValueError):
        RenderSpecV2(camera_bearing_deg=200.0)

    assert len(VALID_ACTIVITY_TYPES) == 14
    assert infer_activity_type_from_strava("Ride", "Morning Spin") == "cycling"
    assert infer_activity_type_from_strava("Ride", "Strade Bianche Gravel") == "gravel_mtb"
    assert infer_activity_type_from_strava("MountainBikeRide") == "gravel_mtb"
    assert infer_activity_type_from_strava("Hike") == "hiking"
    assert infer_activity_type_from_strava("TrailRun") == "trail_run"
    assert infer_activity_type_from_strava("Run", "Fictional Trail") == "trail_run"
    assert infer_activity_type_from_strava("Run", "City 10k") == "running"
    assert infer_activity_type_from_strava("Walk") == "walking"
    assert infer_activity_type_from_strava("BackcountrySki") == "skiing"
    assert infer_activity_type_from_strava("Kayaking") == "kayak"
    assert infer_activity_type_from_strava("Swim") == "swimming"
    assert infer_activity_type_from_strava("RockClimbing") == "climbing"
    assert infer_activity_type_from_strava("Motorcycle") == "motorcycle"
    assert infer_activity_type_from_strava("Drive") == "car"
    assert infer_activity_type_from_strava("Train") == "train"
    assert infer_activity_type_from_strava("Camp") == "camp"

    gpx_typed = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Constructed Typed Track</name><type>hiking</type><trkseg>
    <trkpt lat="0.120" lon="0.350"><ele>1500</ele></trkpt>
    <trkpt lat="0.1201" lon="0.3505"><ele>1550</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    parsed_typed = parse_gpx_bytes(gpx_typed)
    assert parsed_typed.activity_type_hint == "hiking"
    assert (
        infer_activity_type_from_strava(parsed_typed.activity_type_hint, parsed_typed.name)
        == "hiking"
    )


def test_infer_activity_type_word_boundary_and_sport_vs_name_handling() -> None:
    for stage_name in (
        "Pacific Crest Trail",
        "Morning Training",
        "Vancouver Island",
        "Super Stage",
        "Narrow Pass",
        "Constructed GPX Track",
    ):
        assert infer_activity_type_from_strava("", stage_name) == "cycling"
        assert infer_activity_type_from_strava(None, stage_name, fallback="hiking") == "hiking"

    assert infer_activity_type_from_strava("Ride", "Train Station Loop") == "cycling"
    assert infer_activity_type_from_strava("Ride", "Mountain Bike Singletrack") == "gravel_mtb"
    assert infer_activity_type_from_strava("Run", "Pacific Crest Trail") == "trail_run"

    assert infer_activity_type_from_strava("", "Morning Trail Run") == "trail_run"
    assert infer_activity_type_from_strava("", "Ridge Hike ridge_hike.gpx") == "hiking"
    assert infer_activity_type_from_strava("", "SUP Paddle Session") == "kayak"
    assert infer_activity_type_from_strava("", "Camper Van Road Trip") == "car"
    assert infer_activity_type_from_strava("", "Eurostar Rail Transfer") == "train"
    assert infer_activity_type_from_strava("", "Alpinism Summit Push") == "climbing"
    assert infer_activity_type_from_strava("Workout", "Evening Bouldering") == "climbing"


def test_render_spec_v2_validates_stage_colors_hex_regex() -> None:
    spec = RenderSpecV2(
        stage_colors={
            "stage-1": "#000000",
            "stage-2": "#ffffff",
            "stage-3": "#1a2B3c",
        }
    )
    assert spec.stage_colors["stage-3"] == "#1a2B3c"

    for invalid_color in (
        "#ZZZZZZ",
        "#<svg>!",
        "#12345G",
        "#12345_",
        "1234567",
        "#123",
        "#1234567",
        "#123456\n",
    ):
        with pytest.raises(ValueError, match="Invalid hex color for stage"):
            RenderSpecV2(stage_colors={"stage-bad": invalid_color})


def test_render_spec_v2_extended_bounds_and_backward_compatibility() -> None:
    legacy_payload = {
        "version": 2,
        "resolution": "1920x1080",
        "fps": 30,
        "map_style": "liberty",
        "duration_seconds": 45.0,
        "statistics_panel": {
            "enabled": True,
            "position": "top",
            "time_zone": "UTC",
            "text_size": 24,
            "scale": 1.0,
            "text_color": "#FFFFFF",
            "background_color": "#0F172A",
            "background_opacity": 0.75,
            "metrics": ["trip-day", "distance-travelled"],
        },
    }
    parsed = RenderSpecV2.model_validate(legacy_payload)
    assert parsed.duration_seconds == 45.0
    assert parsed.follow_zoom_mode == "adaptive"
    assert parsed.follow_zoom_offset == 0.0
    assert parsed.target_screen_px_per_sec == 180.0
    assert parsed.show_photos is True
    assert parsed.photo_display_seconds == 3.0
    assert parsed.pause_on_photo is False
    assert parsed.statistics_panel.collision_avoidance is True

    extended = RenderSpecV2(
        duration_seconds=180.0,
        follow_zoom_mode="fixed",
        follow_zoom_offset=-2.5,
        target_screen_px_per_sec=240.0,
        show_photos=False,
        photo_display_seconds=8.5,
        pause_on_photo=True,
    )
    assert extended.duration_seconds == 180.0
    assert extended.follow_zoom_mode == "fixed"
    assert extended.pause_on_photo is True

    for invalid_kwargs in (
        {"duration_seconds": 180.1},
        {"duration_seconds": 2.9},
        {"follow_zoom_mode": "invalid"},
        {"follow_zoom_offset": -3.1},
        {"follow_zoom_offset": 3.1},
        {"target_screen_px_per_sec": 79.0},
        {"target_screen_px_per_sec": 501.0},
        {"photo_display_seconds": 0.4},
        {"photo_display_seconds": 10.1},
    ):
        with pytest.raises(ValueError):
            RenderSpecV2.model_validate(invalid_kwargs)


def test_dbt_gold_incremental_stage_add_zero_row_edit_and_delete(tmp_path: Path) -> None:
    def _build_climb_and_dwell_track(
        *,
        base_dt: datetime,
        lat0: float,
        lon0: float,
        include_climb: bool,
        include_dwell: bool,
    ) -> ParsedTrack:
        pts: list[TrackPoint] = []
        for i in range(15):
            pts.append(
                TrackPoint(
                    lat=lat0 + i * 0.0015,
                    lon=lon0,
                    elev_m=(1000.0 + i * 10.0) if include_climb else 1000.0,
                    ts_utc=base_dt + timedelta(seconds=i * 60),
                    hr_bpm=140,
                    watts=200,
                    segment_idx=0,
                )
            )
        if include_dwell:
            pts[-1].moving = False
            for j in range(1, 4):
                pts.append(
                    TrackPoint(
                        lat=pts[-1].lat,
                        lon=pts[-1].lon,
                        elev_m=pts[-1].elev_m,
                        ts_utc=base_dt + timedelta(seconds=14 * 60 + j * 60),
                        moving=False,
                        segment_idx=0,
                    )
                )
        return ParsedTrack(
            name="Incremental Stage",
            points=pts,
            point_count=len(pts),
            segment_count=1,
            has_timestamps=True,
            has_elevation=True,
        )

    silver_dir = tmp_path / "silver"
    gold_dir = tmp_path / "gold"

    t1 = _build_climb_and_dwell_track(
        base_dt=datetime(2002, 7, 1, 8, 0, 0, tzinfo=UTC),
        lat0=0.120,
        lon0=0.340,
        include_climb=True,
        include_dwell=True,
    )
    t2 = _build_climb_and_dwell_track(
        base_dt=datetime(2002, 7, 2, 8, 0, 0, tzinfo=UTC),
        lat0=0.220,
        lon0=0.440,
        include_climb=True,
        include_dwell=False,
    )
    df1, _ = build_silver_dataframe(t1, trip_id="trip-inc", stage_id="stage-1")
    df2, _ = build_silver_dataframe(t2, trip_id="trip-inc", stage_id="stage-2")
    write_silver_parquet(df1, silver_dir / "stage-1.parquet")
    write_silver_parquet(df2, silver_dir / "stage-2.parquet")

    res1 = run_dbt_gold_build(silver_dir / "*.parquet", gold_dir)
    duckdb_path = Path(res1["duckdb_path"])

    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        inv_map_1 = dict(
            con.execute("SELECT stage_id, _dbt_invocation_id FROM mart_stage_summary").fetchall()
        )
        assert set(inv_map_1) == {"stage-1", "stage-2"}
        c1 = con.execute("SELECT COUNT(*) FROM fct_climbs WHERE stage_id = 'stage-1'").fetchone()
        d1 = con.execute(
            "SELECT COUNT(*) FROM fct_dwell_stops WHERE stage_id = 'stage-1'"
        ).fetchone()
        assert c1 is not None and c1[0] == 1
        assert d1 is not None and d1[0] == 1

    t3 = _build_climb_and_dwell_track(
        base_dt=datetime(2002, 7, 3, 8, 0, 0, tzinfo=UTC),
        lat0=0.320,
        lon0=0.540,
        include_climb=True,
        include_dwell=False,
    )
    df3, _ = build_silver_dataframe(t3, trip_id="trip-inc", stage_id="stage-3")
    write_silver_parquet(df3, silver_dir / "stage-3.parquet")

    run_dbt_gold_build(silver_dir / "*.parquet", gold_dir, full_refresh=False)
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        inv_map_2 = dict(
            con.execute("SELECT stage_id, _dbt_invocation_id FROM mart_stage_summary").fetchall()
        )
        assert set(inv_map_2) == {"stage-1", "stage-2", "stage-3"}
        assert inv_map_2["stage-1"] == inv_map_1["stage-1"]
        assert inv_map_2["stage-2"] == inv_map_1["stage-2"]
        assert inv_map_2["stage-3"] != inv_map_1["stage-1"]
        splits_cnt = con.execute("SELECT COUNT(DISTINCT stage_id) FROM fct_splits").fetchone()
        geo_cnt = con.execute(
            "SELECT COUNT(DISTINCT stage_id) FROM mart_stage_geo_context"
        ).fetchone()
        h3_cnt = con.execute(
            "SELECT COUNT(DISTINCT stage_id) FROM mart_spatial_coverage_h3"
        ).fetchone()
        assert splits_cnt is not None and splits_cnt[0] == 3
        assert geo_cnt is not None and geo_cnt[0] == 3
        assert h3_cnt is not None and h3_cnt[0] == 3

    t1_flat = _build_climb_and_dwell_track(
        base_dt=datetime(2002, 7, 1, 8, 0, 0, tzinfo=UTC),
        lat0=0.120,
        lon0=0.340,
        include_climb=False,
        include_dwell=False,
    )
    df1_flat, _ = build_silver_dataframe(t1_flat, trip_id="trip-inc", stage_id="stage-1")
    write_silver_parquet(df1_flat, silver_dir / "stage-1.parquet")

    run_dbt_gold_build(silver_dir / "*.parquet", gold_dir, full_refresh=False)
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        inv_map_3 = dict(
            con.execute("SELECT stage_id, _dbt_invocation_id FROM mart_stage_summary").fetchall()
        )
        assert inv_map_3["stage-1"] != inv_map_2["stage-1"]
        assert inv_map_3["stage-2"] == inv_map_2["stage-2"]
        assert inv_map_3["stage-3"] == inv_map_2["stage-3"]
        c1_after = con.execute(
            "SELECT COUNT(*) FROM fct_climbs WHERE stage_id = 'stage-1'"
        ).fetchone()
        d1_after = con.execute(
            "SELECT COUNT(*) FROM fct_dwell_stops WHERE stage_id = 'stage-1'"
        ).fetchone()
        climbs_stages = con.execute("SELECT COUNT(DISTINCT stage_id) FROM fct_climbs").fetchone()
        assert c1_after is not None and c1_after[0] == 0
        assert d1_after is not None and d1_after[0] == 0
        assert climbs_stages is not None and climbs_stages[0] == 2

    (silver_dir / "stage-2.parquet").unlink()
    run_dbt_gold_build(silver_dir / "*.parquet", gold_dir, full_refresh=False)
    with duckdb.connect(str(duckdb_path), read_only=True) as con:
        for table_name in (
            "mart_stage_summary",
            "fct_climbs",
            "fct_splits",
            "mart_stage_geo_context",
            "mart_spatial_coverage_h3",
        ):
            rows = con.execute(
                f"SELECT DISTINCT stage_id FROM {table_name}"  # noqa: S608
            ).fetchall()
            stage_ids = {r[0] for r in rows}
            assert "stage-2" not in stage_ids, f"stage-2 leaked in {table_name}"
            assert "stage-3" in stage_ids

        summary_totals = con.execute(
            "SELECT COUNT(*), SUM(point_count), SUM(total_distance_m) FROM mart_stage_summary"
        ).fetchone()
        daily_totals = con.execute(
            "SELECT SUM(stage_count), SUM(total_points), SUM(total_distance_m) "
            "FROM mart_trip_daily_summary"
        ).fetchone()
        career_totals = con.execute(
            "SELECT SUM(stage_count), SUM(total_points), SUM(total_distance_m) "
            "FROM mart_user_career_summary"
        ).fetchone()
        assert summary_totals is not None
        assert daily_totals is not None
        assert career_totals is not None
        assert summary_totals[0] == 2
        assert daily_totals[0] == 2
        assert career_totals[0] == 2
        assert daily_totals[1] == summary_totals[1]
        assert career_totals[1] == summary_totals[1]
        assert daily_totals[2] == pytest.approx(summary_totals[2])
        assert career_totals[2] == pytest.approx(summary_totals[2])
