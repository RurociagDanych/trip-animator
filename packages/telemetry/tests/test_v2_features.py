"""Tests for B1/B3/B4/B7/B8 multi-format parsers, signal cleaning, analytics, DEM, and schemas."""

from __future__ import annotations

# Constructed fictional activity fixtures; no recorded trip data is used.
import errno
import struct
import sys
import tempfile
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from trip_telemetry import (
    CURRENT_ETL_VERSION,
    CameraKeyframe,
    PhotoAlignmentInput,
    RenderSpecV2,
    StageCameraOverride,
    StageFocusMode,
    StageTrackInput,
    TerrariumDemProvider,
    TrackParseError,
    TrackPoint,
    align_photos_to_stages,
    backfill_track_elevation,
    bin_track_color_segments,
    build_chart_cache_from_parquet,
    build_silver_dataframe,
    build_stage_events_parquet,
    compute_color_legend_stops,
    detect_track_format,
    extract_stage_highlights,
    parse_chart_cache_payload,
    parse_fit_bytes,
    parse_tcx_bytes,
    parse_track_bytes,
    parse_track_with_timeout,
    query_stage_kpis,
    query_stage_kpis_and_manifest_points,
    read_stage_events_parquet,
    resolve_ref_data_dir,
    simplify_coord_tuples_m,
    snap_photo_pin,
    solve_camera_clock_skew,
    split_silver_dataframe,
    write_silver_parquet,
)
from trip_telemetry.schemas import ParsedTrack


def _fit_crc(data: bytes) -> int:
    crc_table = (
        0x0000,
        0xCC01,
        0xD801,
        0x1400,
        0xF001,
        0x3C00,
        0x2800,
        0xE401,
        0xA001,
        0x6C00,
        0x7800,
        0xB401,
        0x5000,
        0x9C01,
        0x8801,
        0x4400,
    )
    crc = 0
    for byte in data:
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[byte & 0xF]
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[(byte >> 4) & 0xF]
    return crc


def _build_minimal_fit_bytes(points: list[tuple[float, float, float, datetime, int]]) -> bytes:
    fit_epoch = datetime(1989, 12, 31, 0, 0, 0, tzinfo=UTC)
    deg_to_semi = float(1 << 31) / 180.0
    body = bytearray()

    # Local msg 0: file_id (global 0) -> type=0(enum,1B), manufacturer=1(uint16,2B)
    body += bytes([0x40, 0x00, 0x00, 0, 0, 2, 0, 1, 0, 1, 2, 132])
    body += bytes([0x00]) + struct.pack("<BH", 4, 1)

    # Local msg 1: sport (global 12) -> sport=0(enum,1B), sub_sport=1(enum,1B)
    body += bytes([0x41, 0x00, 0x00, 12, 0, 2, 0, 1, 0, 1, 1, 0])
    body += bytes([0x01]) + struct.pack("<BB", 2, 46)

    body += bytes(
        [
            0x42,
            0x00,
            0x00,
            20,
            0,
            5,
            253,
            4,
            134,
            0,
            4,
            133,
            1,
            4,
            133,
            2,
            2,
            132,
            3,
            1,
            2,
        ]
    )
    for lat, lon, elev_m, ts, hr in points:
        ts_sec = int((ts - fit_epoch).total_seconds())
        semi_lat = int(round(lat * deg_to_semi))
        semi_lon = int(round(lon * deg_to_semi))
        raw_alt = int(round((elev_m + 500.0) * 5.0))
        body += bytes([0x02]) + struct.pack("<IiiHB", ts_sec, semi_lat, semi_lon, raw_alt, hr)

    header_wo_crc = struct.pack("<BBHI4s", 14, 0x20, 2100, len(body), b".FIT")
    header_crc = _fit_crc(header_wo_crc)
    header = header_wo_crc + struct.pack("<H", header_crc)
    file_crc = _fit_crc(header + bytes(body))
    return header + bytes(body) + struct.pack("<H", file_crc)


def _encode_rgb_png(rgb_rows: list[list[tuple[int, int, int]]]) -> bytes:
    height = len(rgb_rows)
    width = len(rgb_rows[0])
    raw = bytearray()
    for row in rgb_rows:
        raw.append(0)
        for r, g, b in row:
            raw.extend((r, g, b))

    def _chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw))
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def test_render_spec_v2_new_fields_and_keyframe_validation() -> None:
    spec = RenderSpecV2(
        map_style="topo-relief",
        color_by="grade",
        terrain_exaggeration=2.2,
        elevation_curtain=True,
        director_preset="climb-spotlight",
        show_chapter_cards=True,
        show_transfer_arcs=False,
        show_finale_poster=True,
        show_climb_callouts=True,
        photo_card_style="polaroid",
        telemetry_smoothing=7,
        camera_keyframes=[
            CameraKeyframe(progress=0.8, pitch_deg=50.0, speed_multiplier=2.0, label="Summit"),
            CameraKeyframe(progress=0.2, pitch_deg=35.0, speed_multiplier=1.0, label="Start"),
        ],
    )
    assert [kf.progress for kf in spec.camera_keyframes] == [0.2, 0.8]
    assert spec.color_by == "grade"
    assert spec.statistics_panel.enabled is True

    spec_hud = RenderSpecV2(
        statistics_panel={"metrics": ["grade", "hr", "power", "vam"]}  # type: ignore[arg-type]
    )
    assert spec_hud.statistics_panel.metrics == ["grade", "hr", "power", "vam"]

    with pytest.raises(ValueError):
        CameraKeyframe(progress=0.5, pitch_deg=float("nan"))
    with pytest.raises(ValueError):
        RenderSpecV2(terrain_exaggeration=float("inf"))


def test_stage_camera_override_and_climb_callout_duration_validation() -> None:
    street_mode: StageFocusMode = "street"
    street = StageCameraOverride(focus_mode=street_mode)
    assert street.target_zoom == pytest.approx(15.8)
    assert street.speed_multiplier == pytest.approx(0.2)
    assert street.transition_margin == pytest.approx(0.02)

    street_tuned = StageCameraOverride(
        focus_mode="street",
        target_zoom=16.4,
        speed_multiplier=0.55,
    )
    assert street_tuned.target_zoom == pytest.approx(16.4)
    assert street_tuned.speed_multiplier == pytest.approx(0.55)

    close = StageCameraOverride(focus_mode="close")
    assert close.target_zoom == pytest.approx(13.8)
    assert close.speed_multiplier == pytest.approx(0.7)

    custom = StageCameraOverride(
        focus_mode="custom",
        target_zoom=14.5,
        speed_multiplier=0.6,
        transition_margin=0.05,
    )
    assert custom.focus_mode == "custom"
    assert custom.target_zoom == pytest.approx(14.5)
    assert custom.speed_multiplier == pytest.approx(0.6)
    assert custom.transition_margin == pytest.approx(0.05)

    min_speed = StageCameraOverride(focus_mode="custom", speed_multiplier=0.05)
    assert min_speed.speed_multiplier == pytest.approx(0.05)
    max_speed = StageCameraOverride(focus_mode="custom", speed_multiplier=3.0)
    assert max_speed.speed_multiplier == pytest.approx(3.0)

    auto = StageCameraOverride(focus_mode="auto", target_zoom=16.0, speed_multiplier=0.5)
    assert auto.target_zoom is None
    assert auto.speed_multiplier == pytest.approx(1.0)

    for bad_zoom in (2.9, 18.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            StageCameraOverride(focus_mode="custom", target_zoom=bad_zoom)
    for bad_speed in (0.04, 3.01, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            StageCameraOverride(focus_mode="custom", speed_multiplier=bad_speed)
    for bad_margin in (0.004, 0.26, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            StageCameraOverride(focus_mode="custom", transition_margin=bad_margin)

    default_spec = RenderSpecV2()
    assert default_spec.stage_camera_overrides == {}
    assert default_spec.climb_callout_duration_s == pytest.approx(4.0)

    configured = RenderSpecV2(
        climb_callout_duration_s=0.0,
        stage_camera_overrides={"stage-1": street, "stage-2": custom},
    )
    assert configured.climb_callout_duration_s == 0.0
    assert configured.stage_camera_overrides["stage-1"].target_zoom == pytest.approx(15.8)
    assert configured.stage_camera_overrides["stage-2"].speed_multiplier == pytest.approx(0.6)

    max_callout = RenderSpecV2(climb_callout_duration_s=30.0)
    assert max_callout.climb_callout_duration_s == pytest.approx(30.0)

    for bad_duration in (-0.1, 30.1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            RenderSpecV2(climb_callout_duration_s=bad_duration)


def test_fit_tcx_and_multitrack_gpx_parsers() -> None:
    t0 = datetime(2001, 7, 10, 8, 0, 0, tzinfo=UTC)
    fit_bytes = _build_minimal_fit_bytes(
        [
            (0.1200, 0.3400, 1000.0, t0, 130),
            (0.1205, 0.3405, 1020.0, t0 + timedelta(seconds=30), 142),
            (0.1210, 0.3410, 1045.0, t0 + timedelta(seconds=60), 155),
        ]
    )
    assert detect_track_format(fit_bytes) == "fit"
    parsed_fit = parse_fit_bytes(fit_bytes)
    assert parsed_fit.source_format == "fit"
    assert parsed_fit.point_count == 3
    assert parsed_fit.has_timestamps is True
    assert parsed_fit.points[0].hr_bpm == 130
    assert parsed_fit.points[2].elev_m == pytest.approx(1045.0, abs=0.3)
    assert "cycling" in parsed_fit.activity_type_hint

    parsed_fit_sub = parse_track_with_timeout(fit_bytes, timeout_seconds=10.0)
    assert parsed_fit_sub.point_count == 3

    with pytest.raises(TrackParseError):
        parse_fit_bytes(fit_bytes[:20] + b"\xff\xff")

    tcx_bytes = b"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
  xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
  <Activities>
    <Activity Sport="Biking">
      <Id>2001-07-11T07:00:00Z</Id>
      <Notes>Constructed TCX Track</Notes>
      <Lap StartTime="2001-07-11T07:00:00Z">
        <Track>
          <Trackpoint>
            <Time>2001-07-11T07:00:00Z</Time>
            <Position><LatitudeDegrees>0.1200</LatitudeDegrees><LongitudeDegrees>0.3400</LongitudeDegrees></Position>
            <AltitudeMeters>1800.0</AltitudeMeters>
            <HeartRateBpm><Value>140</Value></HeartRateBpm>
            <Cadence>78</Cadence>
            <Extensions><ns3:TPX><ns3:Speed>4.2</ns3:Speed><ns3:Watts>260</ns3:Watts></ns3:TPX></Extensions>
          </Trackpoint>
          <Trackpoint>
            <Time>2001-07-11T07:01:00Z</Time>
            <Position><LatitudeDegrees>0.1205</LatitudeDegrees><LongitudeDegrees>0.3405</LongitudeDegrees></Position>
            <AltitudeMeters>1815.0</AltitudeMeters>
            <HeartRateBpm><Value>152</Value></HeartRateBpm>
            <Cadence>82</Cadence>
            <Extensions><ns3:TPX><ns3:Speed>4.5</ns3:Speed><ns3:Watts>280</ns3:Watts></ns3:TPX></Extensions>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>"""
    assert detect_track_format(tcx_bytes) == "tcx"
    parsed_tcx = parse_tcx_bytes(tcx_bytes)
    assert parsed_tcx.source_format == "tcx"
    assert parsed_tcx.name == "Constructed TCX Track"
    assert parsed_tcx.activity_type_hint == "Biking"
    assert parsed_tcx.point_count == 2
    assert parsed_tcx.points[0].watts == 260
    assert parsed_tcx.points[1].velocity_smooth == pytest.approx(4.5)

    multi_trk_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Leg 1</name><trkseg>
    <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele></trkpt>
    <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele></trkpt>
  </trkseg></trk>
  <trk><name>Leg 2</name><trkseg>
    <trkpt lat="0.1250" lon="0.3450"><ele>1050</ele></trkpt>
    <trkpt lat="0.1255" lon="0.3455"><ele>1060</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    stitched = parse_track_bytes(multi_trk_gpx, stitch_tracks=True)
    assert stitched.point_count == 4
    assert stitched.segment_count == 2
    assert any(w.code == "STITCHED_TRACKS" for w in stitched.warnings)


def test_signal_cleaning_tiny_track_invariant_teleports_and_enu_rdp(tmp_path: Path) -> None:
    t0 = datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)

    tiny_pts = [
        TrackPoint(lat=0.1200, lon=0.3400, elev_m=1000.0, ts_utc=t0),
        TrackPoint(lat=0.12025, lon=0.5405, elev_m=1001.5, ts_utc=t0 + timedelta(seconds=10)),
        TrackPoint(lat=0.1205, lon=0.3405, elev_m=1000.0, ts_utc=t0 + timedelta(seconds=20)),
        TrackPoint(lat=0.12075, lon=0.5415, elev_m=1085.0, ts_utc=t0 + timedelta(seconds=30)),
    ]
    tiny_track = ParsedTrack(
        points=tiny_pts, point_count=4, segment_count=1, has_timestamps=True, has_elevation=True
    )
    tiny_df, tiny_sum = build_silver_dataframe(tiny_track, trip_id="t", stage_id="s-tiny")
    assert tiny_sum.elevation_gain_m == pytest.approx(86.5)
    assert tiny_sum.etl_version == CURRENT_ETL_VERSION
    assert "elev_raw_m" in tiny_df.columns

    noisy_pts: list[TrackPoint] = []
    for i in range(30):
        jitter = 1.2 if (i % 2 == 0) else -1.2
        elev = 1000.0 + jitter + (200.0 if i == 15 else 0.0)
        lat = 0.120 + i * 0.0005
        lon = 0.340 + i * 0.0005
        if i == 10:
            # GPS teleport 50 km away in 10 seconds (>100 m/s and >500m)
            lat = 0.200
            lon = 8.5
        noisy_pts.append(
            TrackPoint(
                lat=lat,
                lon=lon,
                elev_m=elev,
                ts_utc=t0 + timedelta(seconds=i * 10),
            )
        )
    noisy_track = ParsedTrack(
        points=noisy_pts, point_count=30, segment_count=1, has_timestamps=True, has_elevation=True
    )
    clean_df, clean_sum = build_silver_dataframe(noisy_track, trip_id="t", stage_id="s-clean")
    assert clean_sum.point_count == 29
    assert clean_sum.elevation_gain_m < 5.0
    assert max(clean_df.get_column("elev_raw_m").to_list()) == pytest.approx(1198.8)

    pq_path = write_silver_parquet(clean_df, tmp_path / "clean.parquet")
    kpis, manifest_pts = query_stage_kpis_and_manifest_points(pq_path, max_points=12)
    direct_kpis = query_stage_kpis(pq_path)
    assert kpis["point_count"] == direct_kpis["point_count"]
    assert kpis["distance_m"] == pytest.approx(direct_kpis["distance_m"])
    assert len(manifest_pts) == 12

    chart_gz = build_chart_cache_from_parquet(pq_path, max_points=10)
    parsed_chart = parse_chart_cache_payload(chart_gz)
    assert parsed_chart is not None
    assert len(parsed_chart) == 10
    assert parse_chart_cache_payload(chart_gz, trim_start_s=5.0) is None

    # Local ENU RDP is isotropic at high latitude (70N)
    # 100m lateral excursion at 70N should be preserved with 8m tol and simplified with 200m tol
    high_lat = [(70.0, 20.0, 0), (70.0009, 20.005, 0), (70.0, 20.01, 0)]
    assert len(simplify_coord_tuples_m(high_lat, 0.390)) == 3
    assert len(simplify_coord_tuples_m(high_lat, 200.0)) == 2


def test_analytics_climbs_splits_dwells_peaks_geo_and_color_bins(tmp_path: Path) -> None:
    ref_dir = resolve_ref_data_dir()
    assert (ref_dir / "countries.parquet").is_file()
    assert (ref_dir / "admin_regions.parquet").is_file()
    assert (ref_dir / "mountain_passes.parquet").is_file()
    total_ref_bytes = sum(p.stat().st_size for p in ref_dir.glob("*.parquet"))
    assert total_ref_bytes < 2 * 1024 * 1024

    # Reference-data lookup contract: intentionally use the real Passo Giau location.
    t0 = datetime(2001, 7, 15, 12, 0, 0, tzinfo=UTC)
    pts: list[TrackPoint] = []
    # Synthetic straight-line ascent to a public reference landmark, not a recorded activity.
    # 40 points climbing ~3.6 km at ~7% grade up to Passo Giau
    for i in range(40):
        frac = i / 39.0
        pts.append(
            TrackPoint(
                lat=46.4550 + frac * (46.4828 - 46.4550),
                lon=12.0350 + frac * (12.0542 - 12.0350),
                elev_m=1980.0 + frac * 256.0,
                ts_utc=t0 + timedelta(seconds=i * 30),
                hr_bpm=155,
                watts=290,
            )
        )
    for j in range(1, 11):
        pts.append(
            TrackPoint(
                lat=46.4828,
                lon=12.0542,
                elev_m=2236.0,
                ts_utc=t0 + timedelta(seconds=39 * 30 + j * 30),
                hr_bpm=105,
                watts=0,
                velocity_smooth=0.0,
                moving=False,
            )
        )

    track = ParsedTrack(
        points=pts, point_count=len(pts), segment_count=1, has_timestamps=True, has_elevation=True
    )
    df, _ = build_silver_dataframe(track, trip_id="trip-giau", stage_id="stage-giau")
    pq_path = write_silver_parquet(df, tmp_path / "giau.parquet")

    bundle = extract_stage_highlights(pq_path, stage_id="stage-giau", ref_dir=ref_dir)
    assert len(bundle.climbs) >= 1
    assert "Passo Giau" in bundle.climbs[0].name
    assert len(bundle.splits) >= 3
    assert len(bundle.dwell_stops) == 1
    assert bundle.dwell_stops[0].duration_s >= 180.0
    assert len(bundle.peak_efforts) >= 1
    assert "Italy" in bundle.geo_context.countries
    assert "Veneto" in bundle.geo_context.regions
    assert "Passo Giau" in bundle.geo_context.passes

    events_path = tmp_path / "giau_events.parquet"
    build_stage_events_parquet(pq_path, events_path, stage_id="stage-giau", ref_dir=ref_dir)
    loaded = read_stage_events_parquet(events_path)
    assert loaded is not None and loaded.climbs[0].name == bundle.climbs[0].name
    assert read_stage_events_parquet(events_path, trim_start_s=10.0) is None

    sample_dicts = [
        {"grade_pct": -1.0, "speed_mps": 10.0, "hr_bpm": 115, "watts": 120},
        {"grade_pct": 1.0, "speed_mps": 6.0, "hr_bpm": 130, "watts": 180},
        {"grade_pct": 7.5, "speed_mps": 4.0, "hr_bpm": 165, "watts": 310},
        {"grade_pct": 8.0, "speed_mps": 3.5, "hr_bpm": 170, "watts": 320},
    ]
    segs = bin_track_color_segments(sample_dicts, "grade")
    assert len(segs) == 3
    assert len(compute_color_legend_stops("grade")) == 6


def test_photo_alignment_clock_skew_and_pin_snap(tmp_path: Path) -> None:
    # Fictional equatorial track/photo coordinates isolate alignment behavior.
    t0 = datetime(2001, 7, 20, 8, 0, 0, tzinfo=UTC)
    pts = [
        TrackPoint(
            lat=0.1200 + i * 0.001,
            lon=0.3400 + i * 0.001,
            elev_m=1000.0 + i * 10.0,
            ts_utc=t0 + timedelta(minutes=i * 5),
        )
        for i in range(10)
    ]
    df, _ = build_silver_dataframe(
        ParsedTrack(
            points=pts, point_count=10, segment_count=1, has_timestamps=True, has_elevation=True
        ),
        trip_id="t-photo",
        stage_id="st-1",
    )
    pq_path = write_silver_parquet(df, tmp_path / "st1.parquet")
    stages = [StageTrackInput(stage_id="st-1", stage_order=0, parquet_path=pq_path)]

    cam_photos = [
        PhotoAlignmentInput(
            id="p1",
            taken_at_utc=t0 + timedelta(minutes=15, hours=2),
            lat=0.123,
            lon=0.343,
            camera_make_model="Fujifilm X-T5",
        ),
        PhotoAlignmentInput(
            id="p2",
            taken_at_utc=t0 + timedelta(minutes=30, hours=2),
            camera_make_model="Fujifilm X-T5",
        ),
        PhotoAlignmentInput(
            id="p3-manual",
            match_method="manual",
            stage_id="st-1",
            matched_lat=0.1225,
            matched_lon=0.3425,
            matched_progress=0.5,
        ),
    ]
    skew = solve_camera_clock_skew(cam_photos, stages, camera_make_model="Fujifilm X-T5")
    assert skew.offset_seconds == -7200
    assert skew.matched_count == 1
    assert skew.median_error_m is not None and skew.median_error_m < 5.0

    shifted_photos = [
        p.model_copy(update={"exif_offset_s": skew.offset_seconds})
        if p.match_method != "manual"
        else p
        for p in cam_photos
    ]
    aligned = align_photos_to_stages(shifted_photos, stages)
    assert aligned[0].match_method == "timestamp"
    assert aligned[0].matched_lat == pytest.approx(0.123, abs=1e-4)
    assert aligned[1].match_method == "timestamp"
    assert aligned[1].matched_progress == pytest.approx(2.0 / 3.0, abs=0.05)
    assert aligned[2].match_method == "manual"

    pin_coord = snap_photo_pin(stages, lat=0.1245, lon=0.3445)
    assert pin_coord.stage_id == "st-1"
    assert pin_coord.matched_progress == pytest.approx(0.5, abs=0.02)

    pin_prog = snap_photo_pin(stages, progress=0.5)
    assert pin_prog.matched_lat == pytest.approx(0.1245, abs=1e-3)


def test_dem_terrarium_backfill_and_offline_fallback() -> None:
    no_ele = ParsedTrack(
        points=[
            TrackPoint(lat=0.120, lon=0.340),
            TrackPoint(lat=0.1205, lon=0.3405),
        ],
        point_count=2,
        segment_count=1,
        has_timestamps=False,
        has_elevation=False,
    )
    unchanged = backfill_track_elevation(no_ele)
    assert unchanged.has_elevation is False

    png_bytes = _encode_rgb_png([[(133, 220, 0)] * 256 for _ in range(256)])
    provider = TerrariumDemProvider(tile_fetcher=lambda _z, _x, _y: png_bytes)
    filled = backfill_track_elevation(no_ele, provider=provider)
    assert filled.has_elevation is True
    assert filled.points[0].elev_m == pytest.approx(1500.0)
    assert any(w.code == "DEM_ELEVATION_BACKFILLED" for w in filled.warnings)


def test_photo_alignment_and_pin_snap_without_pytz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "pytz", None)

    # Fictional coordinates keep this timezone-fallback test independent of geography.
    t0 = datetime(2001, 7, 20, 8, 0, 0, tzinfo=UTC)
    pts = [
        TrackPoint(
            lat=0.1200 + i * 0.001,
            lon=0.3400 + i * 0.001,
            elev_m=1000.0 + i * 10.0,
            ts_utc=t0 + timedelta(minutes=i * 5),
        )
        for i in range(6)
    ]
    df, _ = build_silver_dataframe(
        ParsedTrack(
            points=pts, point_count=6, segment_count=1, has_timestamps=True, has_elevation=True
        ),
        trip_id="t-nopytz",
        stage_id="st-nopytz",
    )
    pq_path = write_silver_parquet(df, tmp_path / "st_nopytz.parquet")
    stages = [StageTrackInput(stage_id="st-nopytz", stage_order=0, parquet_path=pq_path)]
    photos = [
        PhotoAlignmentInput(
            id="p1",
            taken_at_utc=t0 + timedelta(minutes=10),
            lat=0.1220,
            lon=0.3420,
        )
    ]

    aligned = align_photos_to_stages(photos, stages)
    assert aligned[0].match_method == "timestamp"
    assert aligned[0].matched_lat == pytest.approx(0.122, abs=1e-4)

    skew = solve_camera_clock_skew(photos, stages)
    assert skew.offset_seconds == 0
    assert skew.matched_count == 1

    pin = snap_photo_pin(stages, lat=0.122, lon=0.342)
    assert pin.stage_id == "st-nopytz"


def test_resolve_ref_data_dir_readonly_falls_back_to_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trip_telemetry import ref_data

    readonly_target = tmp_path / "ro_data" / "ref"
    tmp_root = tmp_path / "tmp_root"
    tmp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_root))

    real_build = ref_data.build_reference_parquets

    def _fake_build(path: Path) -> Path:
        if path == readonly_target.resolve():
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return real_build(path)

    monkeypatch.setattr(ref_data, "build_reference_parquets", _fake_build)

    resolved = resolve_ref_data_dir(readonly_target, ensure_exists=True)
    assert resolved == tmp_root / "trip_ref"
    assert (resolved / "countries.parquet").is_file()
    assert (resolved / "admin_regions.parquet").is_file()
    assert (resolved / "mountain_passes.parquet").is_file()


def test_split_silver_dataframe_by_distance_elapsed_trimmed_and_untimed() -> None:
    t0 = datetime(2001, 8, 1, 8, 0, 0, tzinfo=UTC)
    pts = [
        TrackPoint(
            lat=0.1000 + i * 0.001,
            lon=0.2000 + i * 0.001,
            elev_m=1000.0 + i * 15.0,
            ts_utc=t0 + timedelta(seconds=i * 60),
        )
        for i in range(8)
    ]
    track = ParsedTrack(
        points=pts, point_count=len(pts), segment_count=1, has_timestamps=True, has_elevation=True
    )
    df, full_summary = build_silver_dataframe(track, trip_id="trip-1", stage_id="stage-orig")

    mid_dist = full_summary.distance_m / 2.0
    p1_df, p1_sum, p2_df, p2_sum = split_silver_dataframe(
        df,
        trip_id="trip-1",
        part1_stage_id="stage-p1",
        part2_stage_id="stage-p2",
        split_distance_m=mid_dist,
    )
    assert p1_df.height == 4
    assert p2_df.height == 4
    assert p1_df.get_column("seq").to_list() == [0, 1, 2, 3]
    assert p2_df.get_column("seq").to_list() == [0, 1, 2, 3]
    assert p1_df.get_column("stage_id").to_list() == ["stage-p1"] * 4
    assert p2_df.get_column("stage_id").to_list() == ["stage-p2"] * 4
    assert p1_df.get_column("delta_dist_m")[0] == 0.0
    assert p2_df.get_column("delta_dist_m")[0] == 0.0
    assert p1_df.get_column("cum_dist_m")[0] == 0.0
    assert p2_df.get_column("cum_dist_m")[0] == 0.0
    assert p1_df.get_column("elapsed_s")[0] == 0.0
    assert p2_df.get_column("elapsed_s")[0] == 0.0
    assert p1_df.get_column("elapsed_s")[-1] == pytest.approx(180.0)
    assert p2_df.get_column("elapsed_s")[-1] == pytest.approx(180.0)
    assert not any(p1_df.get_column("is_trimmed").to_list())
    assert not any(p2_df.get_column("is_trimmed").to_list())
    assert p1_sum.point_count == 4
    assert p2_sum.point_count == 4
    assert p1_sum.elapsed_seconds == pytest.approx(180.0)
    assert p2_sum.elapsed_seconds == pytest.approx(180.0)
    assert p1_sum.distance_m > 0.0
    assert p2_sum.distance_m > 0.0
    assert p1_sum.distance_m + p2_sum.distance_m < full_summary.distance_m

    e1_df, e1_sum, e2_df, e2_sum = split_silver_dataframe(
        df,
        trip_id="trip-1",
        part1_stage_id="stage-e1",
        part2_stage_id="stage-e2",
        split_elapsed_s=120.0,
    )
    assert e1_sum.point_count == 3
    assert e2_sum.point_count == 5
    assert e1_sum.elapsed_seconds == pytest.approx(120.0)
    assert e2_sum.elapsed_seconds == pytest.approx(240.0)
    assert e2_df.get_column("ts_utc")[0] == t0 + timedelta(seconds=180)

    trimmed_df, _ = build_silver_dataframe(
        track,
        trip_id="trip-1",
        stage_id="stage-trimmed",
        trim_start_s=60.0,
        trim_end_s=360.0,
    )
    t1_df, t1_sum, t2_df, t2_sum = split_silver_dataframe(
        trimmed_df,
        trip_id="trip-1",
        part1_stage_id="stage-t1",
        part2_stage_id="stage-t2",
        split_elapsed_s=120.0,
    )
    assert t1_sum.point_count == 3
    assert t2_sum.point_count == 3
    assert t1_df.get_column("cum_dist_m")[0] == 0.0
    assert t2_df.get_column("cum_dist_m")[0] == 0.0
    assert not any(t1_df.get_column("is_trimmed").to_list())
    assert not any(t2_df.get_column("is_trimmed").to_list())

    untimed_pts = [
        TrackPoint(lat=0.1000 + i * 0.001, lon=0.2000 + i * 0.001, elev_m=1000.0 + i * 5.0)
        for i in range(6)
    ]
    untimed_df, untimed_sum = build_silver_dataframe(
        ParsedTrack(
            points=untimed_pts,
            point_count=6,
            segment_count=1,
            has_timestamps=False,
            has_elevation=True,
        ),
        trip_id="trip-2",
        stage_id="stage-untimed",
    )
    u1_df, u1_sum, u2_df, u2_sum = split_silver_dataframe(
        untimed_df,
        trip_id="trip-2",
        part1_stage_id="stage-u1",
        part2_stage_id="stage-u2",
        split_distance_m=untimed_sum.distance_m / 2.0,
    )
    assert u1_sum.point_count == 3
    assert u2_sum.point_count == 3
    assert u1_sum.has_timestamps is False
    assert u2_sum.has_timestamps is False
    assert u1_df.get_column("elapsed_s").is_null().all()
    assert u2_df.get_column("elapsed_s").is_null().all()

    with pytest.raises(ValueError, match="without timestamps"):
        split_silver_dataframe(
            untimed_df,
            trip_id="trip-2",
            part1_stage_id="stage-u1",
            part2_stage_id="stage-u2",
            split_elapsed_s=60.0,
        )

    with pytest.raises(ValueError):
        split_silver_dataframe(df, trip_id="t", part1_stage_id="p1", part2_stage_id="p2")
    with pytest.raises(ValueError):
        split_silver_dataframe(
            df,
            trip_id="t",
            part1_stage_id="p1",
            part2_stage_id="p2",
            split_distance_m=100.0,
            split_elapsed_s=60.0,
        )
    with pytest.raises(ValueError, match="at least 2 points"):
        split_silver_dataframe(
            df,
            trip_id="t",
            part1_stage_id="p1",
            part2_stage_id="p2",
            split_elapsed_s=30.0,
        )
    with pytest.raises(ValueError, match="at least 2 points"):
        split_silver_dataframe(
            df,
            trip_id="t",
            part1_stage_id="p1",
            part2_stage_id="p2",
            split_elapsed_s=400.0,
        )
    with pytest.raises(ValueError, match="at least 4 active points"):
        split_silver_dataframe(
            df.head(3),
            trip_id="t",
            part1_stage_id="p1",
            part2_stage_id="p2",
            split_distance_m=50.0,
        )
    with pytest.raises(ValueError):
        split_silver_dataframe(
            df,
            trip_id="t",
            part1_stage_id="p1",
            part2_stage_id="p2",
            split_distance_m=float("nan"),
        )

    jitter_pts = [
        TrackPoint(lat=0.1000, lon=0.2000, elev_m=1000.0, ts_utc=t0),
        TrackPoint(lat=0.1010, lon=0.2010, elev_m=1010.0, ts_utc=t0 + timedelta(seconds=60)),
        TrackPoint(lat=0.1020, lon=0.2020, elev_m=1020.0, ts_utc=t0 + timedelta(seconds=180)),
        TrackPoint(lat=0.1030, lon=0.2030, elev_m=1030.0, ts_utc=t0 + timedelta(seconds=100)),
        TrackPoint(lat=0.1040, lon=0.2040, elev_m=1040.0, ts_utc=t0 + timedelta(seconds=240)),
    ]
    jitter_df, _ = build_silver_dataframe(
        ParsedTrack(
            points=jitter_pts,
            point_count=5,
            segment_count=1,
            has_timestamps=True,
            has_elevation=True,
        ),
        trip_id="trip-jitter",
        stage_id="stage-jitter",
    )
    j1_df, _, j2_df, _ = split_silver_dataframe(
        jitter_df,
        trip_id="trip-jitter",
        part1_stage_id="j1",
        part2_stage_id="j2",
        split_elapsed_s=120.0,
    )
    assert j1_df.get_column("lat").to_list() == pytest.approx([0.1000, 0.1010])
    assert j2_df.get_column("lat").to_list() == pytest.approx([0.1020, 0.1030, 0.1040])
