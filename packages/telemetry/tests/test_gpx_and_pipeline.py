import gzip
import json
import multiprocessing
import struct
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from trip_telemetry import (
    DEFAULT_ACTIVITY_TYPE,
    LOD_CACHE_SCHEMA_VERSION,
    VALID_ACTIVITY_TYPES,
    ActivityType,
    GpxParseError,
    ParsedTrack,
    RenderSpecV2,
    TrackPoint,
    apply_non_destructive_trim,
    build_lod_cache_payload,
    build_lod_geojson_feature,
    build_lod_geojson_feature_from_tuples,
    build_silver_dataframe,
    build_silver_from_strava,
    compute_unwrapped_bounds,
    extract_lod_feature_from_cache,
    generate_lod_geometries,
    haversine_distance_m,
    infer_activity_type_from_strava,
    is_activity_type,
    normalize_activity_type,
    parse_gpx_bytes,
    parse_gpx_with_timeout,
    parse_strava_streams,
    simplify_coord_tuples,
    simplify_track_points,
    unwrap_longitudes,
    write_silver_parquet,
)
from trip_telemetry import gpx_parser as gpx_parser_mod
from trip_telemetry.gpx_parser import (
    _subprocess_parse_worker,
    _synthesize_uniform_timestamps,
)
from trip_telemetry.pipeline import _parse_start_date
from trip_telemetry.schemas import StatisticsPanelConfig

# Constructed fixture data: synthetic coordinates, names, and dates only.
SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk>
    <name>Constructed Track A</name>
    <trkseg>
      <trkpt lat="0.1200" lon="0.3400">
        <ele>1000.0</ele>
        <time>2001-07-01T08:00:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>132</gpxtpx:hr><gpxtpx:cad>85</gpxtpx:cad></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1205" lon="0.3405">
        <ele>1025.0</ele>
        <time>2001-07-01T08:01:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>145</gpxtpx:hr><gpxtpx:cad>88</gpxtpx:cad></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1210" lon="0.3410">
        <ele>1040.0</ele>
        <time>2001-07-01T08:02:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr><gpxtpx:cad>90</gpxtpx:cad></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
    </trkseg>
  </trk>
</gpx>"""


def test_parse_gpx_rejects_xxe_dtd() -> None:
    malicious = b"""<?xml version="1.0"?>
<!DOCTYPE gpx [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<gpx><trk><trkseg><trkpt lat="0" lon="0"><name>&xxe;</name></trkpt></trkseg></trk></gpx>"""
    with pytest.raises(GpxParseError):
        parse_gpx_bytes(malicious)


def test_parse_gpx_rejects_billion_laughs_xml_bomb() -> None:
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<gpx><trk><name>&lol3;</name><trkseg><trkpt lat="0" lon="0"/></trkseg></trk></gpx>"""
    with pytest.raises(GpxParseError):
        parse_gpx_bytes(bomb)


def test_parse_gpx_rejects_external_parameter_entity() -> None:
    ext_param = b"""<?xml version="1.0"?>
<!DOCTYPE gpx [
  <!ENTITY % remote SYSTEM "http://127.0.0.1:9999/evil.dtd">
  %remote;
]>
<gpx><trk><trkseg><trkpt lat="0.120" lon="0.340"/></trkseg></trk></gpx>"""
    with pytest.raises(GpxParseError, match="Blocked unsafe XML"):
        parse_gpx_bytes(ext_param)


def test_parse_gpx_rejects_empty_non_utf8_and_truncated_xml() -> None:
    with pytest.raises(GpxParseError, match="empty"):
        parse_gpx_bytes(b"")

    with pytest.raises(GpxParseError, match="Malformed GPX XML"):
        parse_gpx_bytes(b"\xff\xfe\xfa\xfb\x00\x01")

    invalid_utf8_in_tag = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<gpx><trk><name>Bad \xff\xfe Name</name><trkseg>"
        b'<trkpt lat="0.120" lon="0.340"/></trkseg></trk></gpx>'
    )
    with pytest.raises(GpxParseError, match="Malformed GPX XML"):
        parse_gpx_bytes(invalid_utf8_in_tag)

    truncated = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<gpx version="1.1"><trk><trkseg><trkpt lat="0.120" lon="0.340"><ele>1000.0'
    )
    with pytest.raises(GpxParseError, match="Malformed GPX XML"):
        parse_gpx_bytes(truncated)


def test_parse_gpx_rejects_missing_trk_trkseg_and_waypoints_only() -> None:
    empty_gpx = b'<?xml version="1.0"?><gpx version="1.1"></gpx>'
    with pytest.raises(GpxParseError, match="zero valid trackpoints"):
        parse_gpx_bytes(empty_gpx)

    missing_trkseg = b'<?xml version="1.0"?><gpx version="1.1"><trk><name>No Seg</name></trk></gpx>'
    with pytest.raises(GpxParseError, match="zero valid trackpoints"):
        parse_gpx_bytes(missing_trkseg)

    empty_trkseg = b'<?xml version="1.0"?><gpx version="1.1"><trk><trkseg></trkseg></trk></gpx>'
    with pytest.raises(GpxParseError, match="zero valid trackpoints"):
        parse_gpx_bytes(empty_trkseg)

    waypoints_only = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="0.1200" lon="0.3400"><ele>1000.0</ele><name>Camp 1</name></wpt>
  <wpt lat="0.1450" lon="0.3650"><ele>1200.0</ele><name>Summit</name></wpt>
  <rte><rtept lat="0.1200" lon="0.3400"/><rtept lat="0.1450" lon="0.3650"/></rte>
</gpx>"""
    with pytest.raises(GpxParseError, match="zero valid trackpoints"):
        parse_gpx_bytes(waypoints_only)


def test_parse_gpx_rejects_excessive_size_and_depth() -> None:
    oversized = b"x" * (25 * 1024 * 1024 + 1)
    with pytest.raises(GpxParseError, match="maximum size"):
        parse_gpx_bytes(oversized)

    deep_open = b"".join(f"<n{i}>".encode() for i in range(70))
    deep_close = b"".join(f"</n{i}>".encode() for i in reversed(range(70)))
    deep_xml = b'<?xml version="1.0"?><gpx>' + deep_open + deep_close + b"</gpx>"
    with pytest.raises(GpxParseError, match="depth"):
        parse_gpx_bytes(deep_xml)


def test_parse_gpx_enforces_coordinate_bounds_and_extracts_extensions() -> None:
    parsed = parse_gpx_bytes(SAMPLE_GPX)
    assert parsed.name == "Constructed Track A"
    assert parsed.point_count == 3
    assert parsed.has_timestamps is True
    assert parsed.has_elevation is True
    assert parsed.is_distance_only is False
    assert parsed.points[0].hr_bpm == 132
    assert parsed.points[0].cadence == 85
    assert parsed.points[2].elev_m == 1040.0


def test_parse_gpx_poles_and_out_of_range_coordinates() -> None:
    poles_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="90.0" lon="180.0"><ele>0.0</ele></trkpt>
    <trkpt lat="-90.0" lon="-180.0"><ele>2835.0</ele></trkpt>
    <trkpt lat="0.0" lon="0.0"><ele>10.0</ele></trkpt>
    <trkpt lat="90.0001" lon="0.0"><ele>10.0</ele></trkpt>
    <trkpt lat="-90.0001" lon="0.0"><ele>10.0</ele></trkpt>
    <trkpt lat="0.0" lon="180.0001"><ele>10.0</ele></trkpt>
    <trkpt lat="0.0" lon="-180.0001"><ele>10.0</ele></trkpt>
    <trkpt lat="invalid" lon="7.0"><ele>10.0</ele></trkpt>
    <trkpt lon="7.0"><ele>10.0</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    parsed = parse_gpx_bytes(poles_gpx)
    assert parsed.point_count == 3
    assert parsed.points[0].lat == 90.0 and parsed.points[0].lon == 180.0
    assert parsed.points[1].lat == -90.0 and parsed.points[1].lon == -180.0
    assert parsed.points[2].lat == 0.0 and parsed.points[2].lon == 0.0
    warning_map = {w.code: w.count for w in parsed.warnings}
    assert warning_map["INVALID_COORD"] == 6


def test_parse_gpx_sanitizes_nan_inf_out_of_range_and_missing_elevation() -> None:
    dirty_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <trkseg>
      <trkpt lat="NaN" lon="0.3400"><ele>100</ele></trkpt>
      <trkpt lat="0.1200" lon="inf"><ele>100</ele></trkpt>
      <trkpt lat="95.0" lon="0.3400"></trkpt>
      <trkpt lat="0.1200" lon="-181.0"></trkpt>
      <trkpt lat="0.1200" lon="0.3400"><ele>NaN</ele></trkpt>
      <trkpt lat="0.1205" lon="0.3405"></trkpt>
    </trkseg>
    <trkseg>
      <trkpt lat="0.1205" lon="0.3405"></trkpt>
      <trkpt lat="0.1210" lon="0.3410"></trkpt>
    </trkseg>
  </trk>
</gpx>"""
    parsed = parse_gpx_bytes(dirty_gpx)
    assert parsed.point_count == 4
    assert parsed.segment_count == 2
    assert parsed.points[0].segment_idx == 0
    assert parsed.points[2].segment_idx == 1
    assert parsed.has_elevation is False
    assert parsed.has_timestamps is False
    assert parsed.is_distance_only is True

    warning_map = {w.code: w.count for w in parsed.warnings}
    assert warning_map["INVALID_COORD"] == 4
    assert warning_map["INVALID_ELEVATION"] == 1
    assert warning_map["MISSING_ELEVATION"] == 1
    assert warning_map["MISSING_TIMESTAMPS"] == 1


def test_parse_gpx_partial_elevation_and_extension_power_bounds() -> None:
    partial_ele_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk><trkseg>
    <trkpt lat="0.1200" lon="0.3400">
      <ele>1000.5</ele>
      <extensions>
        <gpxtpx:TrackPointExtension>
          <gpxtpx:hr>   </gpxtpx:hr>
          <gpxtpx:cad>not-an-int</gpxtpx:cad>
          <gpxtpx:power>265</gpxtpx:power>
        </gpxtpx:TrackPointExtension>
      </extensions>
    </trkpt>
    <trkpt lat="0.1205" lon="0.3405">
      <ele>   </ele>
      <extensions><watts>3500</watts></extensions>
    </trkpt>
    <trkpt lat="0.1210" lon="0.3410">
      <ele>-inf</ele>
    </trkpt>
    <trkpt lat="0.1215" lon="0.3415">
      <ele>not_float</ele>
    </trkpt>
  </trkseg></trk>
</gpx>"""
    parsed = parse_gpx_bytes(partial_ele_gpx)
    assert parsed.point_count == 4
    assert parsed.has_elevation is True
    assert parsed.points[0].elev_m == pytest.approx(1000.5)
    assert parsed.points[0].hr_bpm is None
    assert parsed.points[0].cadence is None
    assert parsed.points[0].watts == 265
    assert parsed.points[1].elev_m is None
    assert parsed.points[1].watts is None

    warning_map = {w.code: w.count for w in parsed.warnings}
    assert warning_map["PARTIAL_ELEVATION"] == 1
    assert warning_map["INVALID_ELEVATION"] == 2


def test_parse_gpx_missing_timestamps_synthetic_fallback() -> None:
    no_time_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele></trkpt>
    <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele></trkpt>
    <trkpt lat="0.1210" lon="0.3410"><ele>1020</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    dist_only = parse_gpx_bytes(no_time_gpx, synthesize_timestamps=False)
    assert dist_only.has_timestamps is False
    assert dist_only.is_distance_only is True
    assert all(p.ts_utc is None for p in dist_only.points)

    synth = parse_gpx_bytes(no_time_gpx, synthesize_timestamps=True, synthetic_speed_mps=5.0)
    assert synth.has_timestamps is False
    assert synth.is_distance_only is True
    assert any(w.code == "SYNTHETIC_TIMESTAMPS" for w in synth.warnings)
    assert all(p.ts_utc is not None for p in synth.points)
    t0, t1, t2 = (p.ts_utc for p in synth.points)
    assert t0 is not None and t1 is not None and t2 is not None
    assert t0 < t1 < t2


def test_parse_gpx_mixed_backwards_duplicate_and_timezone_timestamps() -> None:
    assert _synthesize_uniform_timestamps([]) == []

    mixed_ts_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <trkseg>
      <trkpt lat="0.1200" lon="0.3400"><time>2001-07-01T08:00:00Z</time></trkpt>
      <trkpt lat="0.1205" lon="0.3405"><time>   </time></trkpt>
      <trkpt lat="0.1210" lon="0.3410"><time>not-a-timestamp</time></trkpt>
    </trkseg>
    <trkseg>
      <trkpt lat="0.1225" lon="0.3425"></trkpt>
      <trkpt lat="0.1230" lon="0.3430"><time>2001-07-01T08:10:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>"""
    parsed_mixed = parse_gpx_bytes(mixed_ts_gpx, synthesize_timestamps=True)
    assert parsed_mixed.has_timestamps is False
    assert parsed_mixed.is_distance_only is True
    assert all(p.ts_utc is not None for p in parsed_mixed.points)
    assert parsed_mixed.points[0].ts_utc == datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)
    assert parsed_mixed.points[4].ts_utc == datetime(2001, 7, 1, 8, 10, 0, tzinfo=UTC)
    warn_codes = {w.code: w.count for w in parsed_mixed.warnings}
    assert warn_codes["INVALID_TIMESTAMP"] == 1
    assert warn_codes["SYNTHETIC_TIMESTAMPS"] == 1

    backwards_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele><time>2001-07-01T08:02:00Z</time></trkpt>
    <trkpt lat="0.1200" lon="0.3400"><ele>1001</ele><time>2001-07-01T08:02:10Z</time></trkpt>
    <trkpt lat="0.1200" lon="0.3400"><ele>1002</ele><time>2001-07-01T08:02:20Z</time></trkpt>
    <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele><time>2001-07-01T08:01:00Z</time></trkpt>
    <trkpt lat="0.1210" lon="0.3410"><ele>1020</ele><time>2001-07-01T08:01:00Z</time></trkpt>
    <trkpt lat="0.1215" lon="0.3415"><ele>1030</ele><time>2001-07-01T08:03:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
    parsed_back = parse_gpx_bytes(backwards_gpx)
    assert parsed_back.point_count == 4
    back_warns = {w.code: w.count for w in parsed_back.warnings}
    assert back_warns["DUPLICATE_POINT"] == 2
    assert back_warns["NON_MONOTONIC_TIME"] == 1
    df_back, summary_back = build_silver_dataframe(parsed_back, trip_id="t1", stage_id="s1")
    assert all(s >= 0.0 for s in df_back.get_column("speed_mps").to_list())
    assert summary_back.moving_seconds >= 0.0

    tz_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele><time>2001-07-01T10:00:00+02:00</time></trkpt>
    <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele><time>2001-07-01T04:01:00-04:00</time></trkpt>
    <trkpt lat="0.1210" lon="0.3410"><ele>1020</ele><time>2001-07-01T08:02:00</time></trkpt>
  </trkseg></trk>
</gpx>"""
    parsed_tz = parse_gpx_bytes(tz_gpx)
    assert [p.ts_utc for p in parsed_tz.points] == [
        datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC),
        datetime(2001, 7, 1, 8, 1, 0, tzinfo=UTC),
        datetime(2001, 7, 1, 8, 2, 0, tzinfo=UTC),
    ]
    assert not any(w.code == "NON_MONOTONIC_TIME" for w in parsed_tz.warnings)


def test_parse_gpx_multiple_tracks_and_resource_guard_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    multi_trk_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Primary Track</name>
    <trkseg>
      <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele></trkpt>
      <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele></trkpt>
    </trkseg>
  </trk>
  <trk>
    <name>Secondary Track</name>
    <trkseg>
      <trkpt lat="0.2000" lon="0.3900"><ele>500</ele></trkpt>
    </trkseg>
  </trk>
  <trk>
    <name>Tertiary Track</name>
    <trkseg>
      <trkpt lat="0.3000" lon="0.4400"><ele>600</ele></trkpt>
    </trkseg>
  </trk>
</gpx>"""
    parsed = parse_gpx_bytes(multi_trk_gpx)
    assert parsed.name == "Primary Track"
    assert parsed.point_count == 2
    warn_map = {w.code: w.count for w in parsed.warnings}
    assert warn_map["MULTIPLE_TRACKS"] == 2

    monkeypatch.setattr(gpx_parser_mod, "MAX_TRACKS", 2)
    with pytest.raises(GpxParseError, match="Track count exceeded"):
        parse_gpx_bytes(multi_trk_gpx)
    monkeypatch.undo()

    monkeypatch.setattr(gpx_parser_mod, "MAX_SEGMENTS", 1)
    two_seg_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1"><trk>
  <trkseg><trkpt lat="0.120" lon="0.340"/></trkseg>
  <trkseg><trkpt lat="0.150" lon="0.390"/></trkseg>
</trk></gpx>"""
    with pytest.raises(GpxParseError, match="Segment count exceeded"):
        parse_gpx_bytes(two_seg_gpx)
    monkeypatch.undo()

    monkeypatch.setattr(gpx_parser_mod, "MAX_POINTS", 2)
    with pytest.raises(GpxParseError, match="Point count exceeded"):
        parse_gpx_bytes(SAMPLE_GPX)


def test_parse_gpx_with_timeout_succeeds_and_enforces_deadline() -> None:
    parsed = parse_gpx_with_timeout(SAMPLE_GPX, timeout_seconds=10.0)
    assert parsed.point_count == 3

    with pytest.raises(GpxParseError, match="wall-clock limit"):
        parse_gpx_with_timeout(SAMPLE_GPX, timeout_seconds=0.001)

    with pytest.raises(GpxParseError, match="maximum size"):
        parse_gpx_with_timeout(b"x" * (25 * 1024 * 1024 + 1))

    with pytest.raises(GpxParseError, match="Malformed GPX XML"):
        parse_gpx_with_timeout(b"<gpx><unclosed>", timeout_seconds=10.0)

    q_ok: multiprocessing.Queue[tuple[bool, object]] = multiprocessing.Queue(maxsize=1)
    _subprocess_parse_worker(SAMPLE_GPX, False, q_ok)
    ok_flag, ok_payload = q_ok.get(timeout=5.0)
    assert ok_flag is True
    assert isinstance(ok_payload, dict)

    q_err: multiprocessing.Queue[tuple[bool, object]] = multiprocessing.Queue(maxsize=1)
    _subprocess_parse_worker(b"", False, q_err)
    err_flag, err_payload = q_err.get(timeout=5.0)
    assert err_flag is False
    assert "empty" in str(err_payload)


def test_antimeridian_unwrapping_bounds_distance_and_simplification() -> None:
    pacific_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><trkseg>
    <trkpt lat="0.0" lon="179.99"><ele>0</ele><time>2001-07-01T00:00:00Z</time></trkpt>
    <trkpt lat="0.0" lon="-179.99"><ele>10</ele><time>2001-07-01T00:10:00Z</time></trkpt>
    <trkpt lat="0.0" lon="-179.97"><ele>20</ele><time>2001-07-01T00:20:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
    parsed = parse_gpx_bytes(pacific_gpx)
    assert parsed.crosses_antimeridian is True
    assert parsed.bounds is not None
    min_lon, min_lat, max_lon, max_lat = parsed.bounds
    assert min_lat == 0.0 and max_lat == 0.0
    assert (max_lon - min_lon) == pytest.approx(0.04, abs=1e-6)

    df, summary = build_silver_dataframe(parsed, trip_id="t-pac", stage_id="s-pac")
    assert summary.crosses_antimeridian is True
    assert 4_400.0 < summary.distance_m < 4_500.0
    assert (summary.max_lon - summary.min_lon) == pytest.approx(0.04, abs=1e-6)
    assert df.get_column("lon_unwrapped").to_list() == pytest.approx([179.99, 180.01, 180.03])

    lods = generate_lod_geometries(parsed.points)
    coords = lods["low"]["geometry"]["coordinates"][0]
    assert coords[0][0] == pytest.approx(179.99)
    assert coords[-1][0] == pytest.approx(180.03)
    assert lods["low"]["properties"]["crosses_antimeridian"] is True

    west_unwrapped, west_crosses = unwrap_longitudes([-179.99, 179.99])
    assert west_crosses is True
    assert west_unwrapped == pytest.approx([-179.99, -180.01])
    assert unwrap_longitudes([]) == ([], False)
    with pytest.raises(ValueError, match="empty point sequence"):
        compute_unwrapped_bounds([])
    assert haversine_distance_m(0.0, 179.99, 0.0, -179.99) == pytest.approx(
        haversine_distance_m(0.0, -179.99, 0.0, 179.99)
    )
    assert haversine_distance_m(0.0, -540.0, 0.0, 540.0) == pytest.approx(0.0, abs=1e-6)


def test_strava_stream_ingestion_and_non_destructive_trim(tmp_path: Path) -> None:
    streams = {
        "latlng": {
            "data": [
                [0.1200, 0.3400],
                [0.1205, 0.3405],
                None,
                [0.1215, 0.3415],
                [0.1220, 0.3420],
            ]
        },
        "time": {"data": [0, 30, 45, 60, 90]},
        "altitude": {"data": [1000.0, 1020.0, 1025.0, 1030.0, 1050.0]},
        "velocity_smooth": {"data": [0.0, 4.5, 0.0, 5.0, 5.5]},
        "heartrate": {"data": [125, 142, 140, 155, 160]},
        "cadence": {"data": [0, 82, 0, 88, 92]},
        "watts": {"data": [0, 220, 0, 250, 270]},
        "grade_smooth": {"data": [0.0, 4.0, 0.0, 3.0, 5.0]},
        "moving": {"data": [False, True, False, True, True]},
    }
    parsed = parse_strava_streams(
        streams,
        start_date=datetime(2001, 7, 2, 7, 0, 0, tzinfo=UTC),
        name="Constructed Stream Track",
    )
    assert parsed.point_count == 4
    assert parsed.segment_count == 2
    assert parsed.has_timestamps is True
    assert parsed.points[0].watts == 0
    assert parsed.points[3].hr_bpm == 160

    df, summary = build_silver_from_strava(
        streams,
        trip_id="trip-strava",
        stage_id="stage-strava",
        start_date="2001-07-02T07:00:00Z",
    )
    assert summary.point_count == 4
    assert summary.elapsed_seconds == pytest.approx(90.0)
    assert df.get_column("is_moving").to_list() == [False, True, True, True]

    trimmed_df, trimmed_summary = apply_non_destructive_trim(df, trim_start_s=30.0, trim_end_s=90.0)
    assert trimmed_df.height == 4
    assert trimmed_df.get_column("is_trimmed").to_list() == [True, False, False, False]
    assert trimmed_summary.is_trimmed is True
    assert trimmed_summary.point_count == 3
    assert trimmed_summary.elapsed_seconds == pytest.approx(60.0)
    assert trimmed_summary.distance_m < summary.distance_m

    pq_path = write_silver_parquet(trimmed_df, tmp_path / "silver" / "strava.parquet")
    meta = pq.read_metadata(pq_path)
    assert b"geo" in meta.metadata
    geo = json.loads(meta.metadata[b"geo"].decode("utf-8"))
    assert geo["version"] == "1.1.0"
    assert geo["primary_column"] == "geometry"
    assert geo["columns"]["geometry"]["encoding"] == "WKB"

    table = pq.read_table(pq_path)
    first_wkb = table.column("geometry")[0].as_py()
    byte_order, wkb_type, lon, lat = struct.unpack("<BIdd", first_wkb)
    assert byte_order == 1 and wkb_type == 1
    assert lon == pytest.approx(0.3400) and lat == pytest.approx(0.1200)

    lods = generate_lod_geometries(parsed.points)
    assert lods["full"]["properties"]["segment_count"] == 2
    assert lods["low"]["properties"]["segment_count"] == 2
    assert len(simplify_track_points(parsed.points, tolerance_deg=0.01)) == 4


def test_strava_streams_length_mismatches_negative_velocity_and_missing_fields() -> None:
    with pytest.raises(ValueError, match="latlng"):
        parse_strava_streams({})
    with pytest.raises(ValueError, match="latlng"):
        parse_strava_streams({"latlng": []})
    with pytest.raises(ValueError, match="latlng"):
        parse_strava_streams({"time": [0, 10, 20]})
    with pytest.raises(ValueError, match="zero valid latlng"):
        parse_strava_streams(
            {"latlng": [None, [0.120], ["bad", 0.340], [95.0, 0.340], [0.120, -185.0]]}
        )

    list_streams: list[dict[str, Any]] = [
        {
            "type": "latlng",
            "data": [
                [0.1200, 0.3400],
                [95.0, 0.3400],
                [0.1205, 0.3405],
                [0.1210, 0.3410],
                [0.1215, 0.3415],
            ],
        },
        {"type": "time", "data": [0, 10, 30, 20]},
        {"type": "velocity_smooth", "data": [-4.5, 0.0, None, 6.0]},
        {"type": "grade_smooth", "data": [-140.0, 0.0, 135.0]},
        {"type": "heartrate", "data": [True, 0, -5, 152, "bad"]},
        {"type": "cadence", "data": ["bad", 0, 400, 88]},
        {"type": "watts", "data": [None, 0, 5000, 240]},
        {"type": "moving", "data": ["invalid", False, True]},
    ]
    parsed = parse_strava_streams(
        list_streams,
        start_date=datetime(2001, 7, 2, 9, 0, 0),
        name="Mismatched Stream Test",
    )
    assert parsed.point_count == 4
    assert parsed.segment_count == 2
    assert parsed.has_timestamps is False
    assert parsed.has_elevation is False
    assert parsed.points[0].velocity_smooth is None
    assert parsed.points[1].velocity_smooth is None
    assert parsed.points[2].velocity_smooth == pytest.approx(6.0)
    assert parsed.points[0].grade_smooth == pytest.approx(-100.0)
    assert parsed.points[1].grade_smooth == pytest.approx(135.0 if False else 100.0)
    assert parsed.points[0].hr_bpm is None
    assert parsed.points[2].hr_bpm == 152
    assert parsed.points[2].cadence == 88
    assert parsed.points[2].watts == 240

    warn_codes = {w.code: w.count for w in parsed.warnings}
    assert warn_codes["INVALID_COORD"] == 1
    assert warn_codes["MISSING_TIMESTAMPS"] == 1
    assert warn_codes["MISSING_ELEVATION"] == 1
    assert warn_codes["NON_MONOTONIC_TIME"] == 1

    assert _parse_start_date(None) is None
    assert _parse_start_date("   ") is None
    assert _parse_start_date("2001-07-02T10:00:00+02:00") == datetime(
        2001, 7, 2, 8, 0, 0, tzinfo=UTC
    )
    assert _parse_start_date(
        datetime(2001, 7, 2, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    ) == datetime(2001, 7, 2, 8, 0, 0, tzinfo=UTC)


def test_apply_non_destructive_trim_edge_cases(tmp_path: Path) -> None:
    parsed = parse_gpx_bytes(SAMPLE_GPX)
    df, full_summary = build_silver_dataframe(parsed, trip_id="t-trim", stage_id="s-trim")
    assert full_summary.elapsed_seconds == pytest.approx(120.0)

    df_exact, sum_exact = apply_non_destructive_trim(df, trim_start_s=0.0, trim_end_s=120.0)
    assert sum_exact.is_trimmed is True
    assert sum_exact.point_count == 3
    assert sum_exact.distance_m == pytest.approx(full_summary.distance_m)
    assert df_exact.get_column("is_trimmed").to_list() == [False, False, False]

    df_reset, sum_reset = apply_non_destructive_trim(df_exact, trim_start_s=None, trim_end_s=None)
    assert sum_reset.is_trimmed is False
    assert sum_reset.point_count == 3
    assert df_reset.get_column("is_trimmed").to_list() == [False, False, False]

    df_one, sum_one = apply_non_destructive_trim(df, trim_start_s=55.0, trim_end_s=65.0)
    assert sum_one.point_count == 1
    assert sum_one.distance_m == pytest.approx(0.0)
    assert sum_one.elevation_gain_m == pytest.approx(0.0)
    assert sum_one.elapsed_seconds == pytest.approx(0.0)
    assert sum_one.vam_m_per_h == pytest.approx(0.0)
    assert df_one.get_column("is_trimmed").to_list() == [True, False, True]

    df_no_elapsed = df.drop("elapsed_s")
    _, sum_recomputed = apply_non_destructive_trim(
        df_no_elapsed, trim_start_s=60.0, trim_end_s=120.0
    )
    assert sum_recomputed.point_count == 2
    assert sum_recomputed.elapsed_seconds == pytest.approx(60.0)

    with pytest.raises(ValueError, match="excludes all telemetry points"):
        apply_non_destructive_trim(df, trim_start_s=200.0, trim_end_s=300.0)

    with pytest.raises(ValueError, match="empty Silver DataFrame"):
        apply_non_destructive_trim(df.clear(), trim_start_s=0.0, trim_end_s=10.0)

    no_ts_gpx = b"""<?xml version="1.0"?>
<gpx version="1.1"><trk><trkseg>
  <trkpt lat="0.1200" lon="0.3400"><ele>1000</ele></trkpt>
  <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele></trkpt>
</trkseg></trk></gpx>"""
    df_no_ts, _ = build_silver_dataframe(
        parse_gpx_bytes(no_ts_gpx), trip_id="t-nots", stage_id="s-nots"
    )
    with pytest.raises(ValueError, match="without complete timestamps"):
        apply_non_destructive_trim(df_no_ts, trim_start_s=0.0, trim_end_s=10.0)

    for bad_start, bad_end in (
        (-1.0, 10.0),
        (float("nan"), 10.0),
        (0.0, -5.0),
        (0.0, float("inf")),
        (30.0, 30.0),
        (60.0, 30.0),
    ):
        with pytest.raises(ValueError):
            apply_non_destructive_trim(df, trim_start_s=bad_start, trim_end_s=bad_end)

    parsed_mismatch = parsed.model_copy(
        update={"velocity_smooth": [5.0], "grade_smooth": [2.0], "moving": [True]}
    )
    df_direct_trim, sum_direct_trim = build_silver_dataframe(
        parsed_mismatch,
        trip_id="t-dir",
        stage_id="s-dir",
        trim_start_s=60.0,
        trim_end_s=120.0,
    )
    assert df_direct_trim.height == 3
    assert sum_direct_trim.point_count == 2

    empty_track = ParsedTrack(
        points=[],
        point_count=0,
        segment_count=0,
        has_timestamps=False,
    )
    with pytest.raises(ValueError, match="zero trackpoints"):
        build_silver_dataframe(empty_track, trip_id="t-empty", stage_id="s-empty")

    pq_no_unwrap = write_silver_parquet(df.drop("lon_unwrapped"), tmp_path / "no_unwrap.parquet")
    assert pq_no_unwrap.is_file()


def test_lod_geometry_and_cache_edge_cases() -> None:
    # Closed-loop track (first point == last point -> dx == 0, dy == 0 in _perp_distance_deg)
    loop_tuples = [
        (0.1200, 0.3400, 0),
        (0.1250, 0.3450, 0),
        (0.1200, 0.3400, 0),
        (0.1300, 0.3500, 1),
    ]
    simplified = simplify_coord_tuples(loop_tuples, tolerance_deg=0.0001)
    assert len(simplified) == 4
    assert simplify_coord_tuples(loop_tuples[:2], tolerance_deg=0.01) == loop_tuples[:2]
    two_pts = [TrackPoint(lat=0.120, lon=0.340), TrackPoint(lat=0.150, lon=0.390)]
    assert simplify_track_points(two_pts, tolerance_deg=0.01) == two_pts

    feat = build_lod_geojson_feature_from_tuples(loop_tuples, lod="medium", tolerance_deg=0.0001)
    assert feat["properties"]["segment_count"] == 2
    assert len(feat["geometry"]["coordinates"][1]) == 2

    with pytest.raises(ValueError, match="zero trackpoints"):
        build_lod_geojson_feature_from_tuples([], lod="low", tolerance_deg=0.001)
    with pytest.raises(ValueError, match="zero trackpoints"):
        build_lod_geojson_feature([], lod="low", tolerance_deg=0.001)
    with pytest.raises(ValueError, match="zero trackpoints"):
        build_lod_cache_payload([])

    gz_valid = build_lod_cache_payload(loop_tuples, trim_start_s=10.0, trim_end_s=50.0)
    assert (
        extract_lod_feature_from_cache(gz_valid, "low", trim_start_s=10.0, trim_end_s=50.0)
        is not None
    )
    assert (
        extract_lod_feature_from_cache(gz_valid, "low", trim_start_s=10.0, trim_end_s=60.0) is None
    )

    for bad_payload in (
        {"v": 999, "lods": {}},
        {
            "v": LOD_CACHE_SCHEMA_VERSION,
            "trim_start_s": "not-a-float",
            "trim_end_s": 50.0,
            "lods": {},
        },
        {
            "v": LOD_CACHE_SCHEMA_VERSION,
            "trim_start_s": 10.0,
            "trim_end_s": 50.0,
            "lods": "not-a-dict",
        },
        {
            "v": LOD_CACHE_SCHEMA_VERSION,
            "trim_start_s": 10.0,
            "trim_end_s": 50.0,
            "lods": {"low": {"type": "NotFeature"}},
        },
        {
            "v": LOD_CACHE_SCHEMA_VERSION,
            "trim_start_s": 10.0,
            "trim_end_s": 50.0,
            "lods": {"low": {"type": "Feature", "geometry": "bad", "properties": {}}},
        },
    ):
        raw_gz = gzip.compress(json.dumps(bad_payload).encode("utf-8"))
        assert (
            extract_lod_feature_from_cache(raw_gz, "low", trim_start_s=10.0, trim_end_s=50.0)
            is None
        )


def test_parse_gpx_with_timeout_large_track_no_pipe_deadlock_and_iterative_rdp() -> None:
    pts_xml = "".join(
        f'<trkpt lat="{0.1000 + i * 0.0001:.5f}" lon="{0.3000 + (0.0005 if i % 2 else 0.0):.5f}">'
        f"<ele>{1000 + (i % 50)}</ele>"
        f"<time>2001-07-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z</time>"
        "</trkpt>"
        for i in range(3500)
    )
    large_gpx = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        b"<trk><trkseg>" + pts_xml.encode("utf-8") + b"</trkseg></trk></gpx>"
    )
    parsed = parse_gpx_with_timeout(large_gpx, timeout_seconds=10.0)
    assert parsed.point_count == 3500

    # Zig-zag track with >1000 vertices tests iterative RDP (no RecursionError)
    simplified = simplify_track_points(parsed.points, tolerance_deg=0.00001)
    assert len(simplified) == 3500


def test_non_finite_integer_telemetry_does_not_overflow() -> None:
    gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk><trkseg>
    <trkpt lat="0.120" lon="0.340">
      <ele>1000</ele>
      <extensions><gpxtpx:TrackPointExtension>
        <gpxtpx:hr>inf</gpxtpx:hr>
        <gpxtpx:cad>NaN</gpxtpx:cad>
        <gpxtpx:power>-inf</gpxtpx:power>
      </gpxtpx:TrackPointExtension></extensions>
    </trkpt>
    <trkpt lat="0.1205" lon="0.3405"><ele>1010</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    parsed = parse_gpx_bytes(gpx)
    assert parsed.point_count == 2
    assert parsed.points[0].hr_bpm is None
    assert parsed.points[0].cadence is None
    assert parsed.points[0].watts is None

    strava = {
        "latlng": [[0.120, 0.340], [0.1205, 0.3405]],
        "heartrate": [float("inf"), 145],
        "cadence": [float("nan"), 85],
        "watts": [float("-inf"), 220],
    }
    strava_parsed = parse_strava_streams(strava)
    assert strava_parsed.points[0].hr_bpm is None
    assert strava_parsed.points[0].cadence is None
    assert strava_parsed.points[0].watts is None
    assert strava_parsed.points[1].hr_bpm == 145


def test_parse_strava_streams_preserves_smoothed_streams_into_silver_dataframe() -> None:
    streams = {
        "latlng": {"data": [[0.1200, 0.3400], None, [0.1205, 0.3405]]},
        "velocity_smooth": {"data": [8.5, 99.0, 9.2]},
        "grade_smooth": {"data": [4.5, 50.0, 5.1]},
        "moving": {"data": [True, False, False]},
    }
    parsed = parse_strava_streams(streams, name="Constructed Stream Check")
    assert parsed.point_count == 2
    assert parsed.velocity_smooth == [8.5, 9.2]
    assert parsed.grade_smooth == [4.5, 5.1]
    assert parsed.moving == [True, False]
    assert [p.velocity_smooth for p in parsed.points] == [8.5, 9.2]
    assert [p.grade_smooth for p in parsed.points] == [4.5, 5.1]
    assert [p.moving for p in parsed.points] == [True, False]

    df_from_parsed, summary_from_parsed = build_silver_dataframe(
        parsed,
        trip_id="trip-1",
        stage_id="stage-1",
    )
    df_direct, summary_direct = build_silver_from_strava(
        streams,
        trip_id="trip-1",
        stage_id="stage-1",
    )

    assert df_from_parsed.get_column("speed_mps").to_list() == pytest.approx([8.5, 9.2])
    assert df_from_parsed.get_column("grade_pct").to_list() == pytest.approx([4.5, 5.1])
    assert df_from_parsed.get_column("is_moving").to_list() == [True, False]
    assert summary_from_parsed == summary_direct
    assert df_from_parsed.to_dict(as_series=False) == df_direct.to_dict(as_series=False)


def test_parse_gpx_trk_type_not_shadowed_by_link_or_extension_type() -> None:
    gpx_with_link_type = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <link href="https://example.com/activities/42">
      <text>Activity Link</text>
      <type>text/html</type>
    </link>
    <extensions>
      <type>internal_extension_type</type>
      <name>Ignored Extension Name</name>
    </extensions>
    <name>Constructed Elevation Track</name>
    <type>hiking</type>
    <trkseg>
      <trkpt lat="0.1200" lon="0.3400"><ele>1500.0</ele></trkpt>
      <trkpt lat="0.1205" lon="0.3405"><ele>1520.0</ele></trkpt>
    </trkseg>
  </trk>
</gpx>"""
    parsed = parse_gpx_bytes(gpx_with_link_type)
    assert parsed.name == "Constructed Elevation Track"
    assert parsed.activity_type_hint == "hiking"

    gpx_only_link_type = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Link Only Track</name>
    <link href="https://example.com/activities/42">
      <type>text/html</type>
    </link>
    <trkseg>
      <trkpt lat="0.1200" lon="0.3400"><ele>1500.0</ele></trkpt>
      <trkpt lat="0.1205" lon="0.3405"><ele>1520.0</ele></trkpt>
    </trkseg>
  </trk>
</gpx>"""
    parsed_no_trk_type = parse_gpx_bytes(gpx_only_link_type)
    assert parsed_no_trk_type.name == "Link Only Track"
    assert parsed_no_trk_type.activity_type_hint == ""


@pytest.mark.parametrize(
    ("sport_type", "activity_name", "fallback", "expected"),
    [
        ("Ride", "Morning Loop", "cycling", "cycling"),
        ("VirtualRide", "Constructed Virtual Ride", "cycling", "cycling"),
        ("EBikeRide", "Commute", "cycling", "cycling"),
        ("GravelRide", "Constructed Gravel Ride", "cycling", "gravel_mtb"),
        ("MountainBikeRide", "Constructed Mountain Bike Ride", "cycling", "gravel_mtb"),
        ("EMountainBikeRide", "Enduro Loop", "cycling", "gravel_mtb"),
        ("Velomobile", "Fast Recumbent", "cycling", "gravel_mtb"),
        ("Handcycle", "Adaptive Ride", "cycling", "gravel_mtb"),
        ("Ride", "Constructed Bikepacking Stage", "cycling", "gravel_mtb"),
        ("Ride", "Constructed Enduro Practice", "cycling", "gravel_mtb"),
        ("Ride", "Forest Mountain Biking", "cycling", "gravel_mtb"),
        ("Hike", "Constructed Ridge Walk", "cycling", "hiking"),
        ("Snowshoe", "Winter Basin Trek", "cycling", "hiking"),
        ("Workout", "Heavy Rucking Session", "cycling", "hiking"),
        ("TrailRun", "Constructed Trail Event", "cycling", "trail_run"),
        ("Run", "Constructed Trail Run", "cycling", "trail_run"),
        ("Run", "Constructed Fell Run", "cycling", "trail_run"),
        ("Run", "Constructed Ultra Distance Run", "cycling", "trail_run"),
        ("Run", "Flat City Marathon", "cycling", "running"),
        ("VirtualRun", "Treadmill Tempo", "cycling", "running"),
        ("Walk", "Evening City Stroll", "cycling", "walking"),
        ("InlineSkate", "Constructed Inline Skate Session", "cycling", "walking"),
        ("Wheelchair", "Track Session", "cycling", "walking"),
        ("BackcountrySki", "Constructed Backcountry Traverse", "cycling", "skiing"),
        ("AlpineSki", "Constructed Piste Session", "cycling", "skiing"),
        ("NordicSki", "Constructed Nordic Marathon", "cycling", "skiing"),
        ("RollerSki", "Summer Biathlon", "cycling", "skiing"),
        ("Snowboard", "Powder Day Splitboard", "cycling", "skiing"),
        ("Kayaking", "Constructed Fjord Paddle", "cycling", "kayak"),
        ("Canoeing", "Constructed Lake Paddle", "cycling", "kayak"),
        ("StandUpPaddling", "Constructed Harbor Paddle", "cycling", "kayak"),
        ("Rowing", "Morning Scull", "cycling", "kayak"),
        ("Sail", "Constructed Regatta", "cycling", "kayak"),
        ("Windsurf", "Constructed Thermal Windsurf", "cycling", "kayak"),
        ("Kitesurf", "Constructed Downwinder", "cycling", "kayak"),
        ("Swim", "Pool Laps", "cycling", "swimming"),
        ("OpenWaterSwim", "Constructed Open Water Crossing", "cycling", "swimming"),
        ("RockClimbing", "Constructed Granite Pitch", "cycling", "climbing"),
        ("Bouldering", "Constructed Boulder Circuit", "cycling", "climbing"),
        ("", "Constructed Via Ferrata", "cycling", "climbing"),
        ("Motorcycle", "Constructed Mountain Motorcycle Ride", "cycling", "motorcycle"),
        ("", "Vespa Scooter Coastal Transfer", "cycling", "motorcycle"),
        ("Drive", "Autobahn Transfer", "cycling", "car"),
        ("", "Constructed Overland Road Trip", "cycling", "car"),
        ("Train", "Constructed Scenic Rail Journey", "cycling", "train"),
        ("", "Constructed Rail Transfer", "cycling", "train"),
        ("Camp", "Constructed High Camp Bivouac", "cycling", "camp"),
        ("", "Constructed Overnight Bivouac", "cycling", "camp"),
        ("   ", "   ", "hiking", "hiking"),
        ("UnknownSport", "Unrelated Title", "skiing", "skiing"),
    ],
)
def test_parameterized_strava_activity_type_inference(
    sport_type: str | None,
    activity_name: str | None,
    fallback: ActivityType,
    expected: ActivityType,
) -> None:
    assert infer_activity_type_from_strava(sport_type, activity_name, fallback) == expected


def test_is_and_normalize_activity_type_helpers() -> None:
    for act in VALID_ACTIVITY_TYPES:
        assert is_activity_type(act) is True
        assert normalize_activity_type(act) == act
    assert is_activity_type("not_an_activity") is False
    assert is_activity_type(123) is False
    assert normalize_activity_type("MountainBikeRide") == "gravel_mtb"
    assert normalize_activity_type("   ", fallback="kayak") == "kayak"
    assert normalize_activity_type(None) == DEFAULT_ACTIVITY_TYPE


@pytest.mark.parametrize(
    "invalid_hex",
    [
        "",
        "#",
        "#123",
        "#12345",
        "#1234567",
        "123456",
        "#GGGGGG",
        "#12345g",
        "#12 456",
        "rgb(255,0,0)",
        "red",
        "#123456\n",
    ],
)
def test_render_spec_v2_rejects_invalid_hex_colors(invalid_hex: str) -> None:
    with pytest.raises(ValidationError, match="Invalid hex color for stage"):
        RenderSpecV2(stage_colors={"stage-1": invalid_hex})
    with pytest.raises(ValidationError):
        RenderSpecV2(track_color=invalid_hex)
    with pytest.raises(ValidationError):
        RenderSpecV2(marker_color=invalid_hex)
    with pytest.raises(ValidationError):
        StatisticsPanelConfig(text_color=invalid_hex)
    with pytest.raises(ValidationError):
        StatisticsPanelConfig(background_color=invalid_hex)


@settings(max_examples=50, deadline=None)
@given(
    coords=st.lists(
        st.tuples(
            st.floats(min_value=-89.9, max_value=89.9, allow_nan=False, allow_infinity=False),
            st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False),
            st.floats(min_value=-400.0, max_value=8848.0, allow_nan=False, allow_infinity=False),
            st.integers(min_value=1, max_value=120),
            st.booleans(),
        ),
        min_size=2,
        max_size=35,
    )
)
def test_hypothesis_monotonic_cumulative_distance_and_non_negative_elevation_gain(
    coords: list[tuple[float, float, float, int, bool]],
) -> None:
    base_ts = datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)
    elapsed = 0
    seg_idx = 0
    points: list[TrackPoint] = []
    for idx, (lat, lon, elev, dt_sec, new_seg) in enumerate(coords):
        if idx > 0 and new_seg:
            seg_idx += 1
        elapsed += dt_sec
        points.append(
            TrackPoint(
                lat=lat,
                lon=lon,
                elev_m=elev,
                ts_utc=base_ts + timedelta(seconds=elapsed),
                segment_idx=seg_idx,
            )
        )

    track = ParsedTrack(
        name="Hypothesis Track",
        points=points,
        point_count=len(points),
        segment_count=seg_idx + 1,
        has_timestamps=True,
        has_elevation=True,
    )
    df, summary = build_silver_dataframe(track, trip_id="hyp-trip", stage_id="hyp-stage")

    cum_dists = df.get_column("cum_dist_m").to_list()
    delta_dists = df.get_column("delta_dist_m").to_list()
    seg_indices = df.get_column("segment_idx").to_list()

    assert cum_dists[0] == 0.0
    for i in range(1, len(cum_dists)):
        assert delta_dists[i] >= 0.0
        assert cum_dists[i] >= cum_dists[i - 1] - 1e-9
        if seg_indices[i] != seg_indices[i - 1]:
            assert delta_dists[i] == 0.0

    assert summary.distance_m >= 0.0
    assert summary.distance_m == pytest.approx(cum_dists[-1], abs=1e-6)
    assert summary.elevation_gain_m >= 0.0
    assert summary.elevation_loss_m >= 0.0
    assert summary.moving_seconds >= 0.0
    assert summary.elapsed_seconds >= 0.0
    assert summary.vam_m_per_h >= 0.0


@settings(max_examples=40, deadline=None)
@given(
    lons=st.lists(
        st.floats(min_value=-180.0, max_value=180.0, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=50,
    )
)
def test_hypothesis_unwrap_longitudes_preserves_bounded_step_deltas(
    lons: list[float],
) -> None:
    unwrapped, crosses = unwrap_longitudes(lons)
    assert len(unwrapped) == len(lons)
    for i in range(1, len(unwrapped)):
        step = unwrapped[i] - unwrapped[i - 1]
        assert -180.0 - 1e-9 <= step <= 180.0 + 1e-9
    if not crosses:
        assert unwrapped == pytest.approx(lons)
