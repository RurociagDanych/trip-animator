from __future__ import annotations

import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import httpx
import polars as pl
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from PIL import ExifTags, Image, TiffImagePlugin
from sqlalchemy import inspect, select
from trip_api.cli import main as cli_main
from trip_api.db import IngestJob, RenderJob, RenderJobChunk, Stage, create_db_engine
from trip_api.reaper import reap_expired
from trip_api.services import stage_chart_cache_key, stage_lod_cache_key

if TYPE_CHECKING:
    from conftest import HarnessFactory


SAMPLE_TCX = b"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Biking">
      <Id>2001-01-01T08:00:00Z</Id>
      <Lap StartTime="2001-01-01T08:00:00Z">
        <Track>
          <Trackpoint>
            <Time>2001-01-01T08:00:00Z</Time>
            <Position><LatitudeDegrees>0.1000</LatitudeDegrees><LongitudeDegrees>0.2000</LongitudeDegrees></Position>
            <AltitudeMeters>1000.0</AltitudeMeters>
            <HeartRateBpm><Value>132</Value></HeartRateBpm>
          </Trackpoint>
          <Trackpoint>
            <Time>2001-01-01T08:01:00Z</Time>
            <Position><LatitudeDegrees>0.1020</LatitudeDegrees><LongitudeDegrees>0.2020</LongitudeDegrees></Position>
            <AltitudeMeters>1040.0</AltitudeMeters>
            <HeartRateBpm><Value>148</Value></HeartRateBpm>
          </Trackpoint>
          <Trackpoint>
            <Time>2001-01-01T08:02:00Z</Time>
            <Position><LatitudeDegrees>0.1040</LatitudeDegrees><LongitudeDegrees>0.2040</LongitudeDegrees></Position>
            <AltitudeMeters>1085.0</AltitudeMeters>
            <HeartRateBpm><Value>158</Value></HeartRateBpm>
          </Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>"""


def _make_jpeg_with_camera_exif(
    *,
    make: str = "SONY",
    model: str = "ILCE-7M4",
    date_str: str = "2001:01:01 08:01:00",
    offset_str: str = "+00:00",
    lat_dms: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None = (
        (0, 1),
        (6, 1),
        (5, 1),
    ),
    lon_dms: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None = (
        (0, 1),
        (12, 1),
        (5, 1),
    ),
) -> bytes:
    img = Image.new("RGB", (64, 48), color=(40, 110, 180))
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = make
    exif[ExifTags.Base.Model] = model
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    exif_ifd[36867] = date_str
    exif_ifd[36881] = offset_str
    if lat_dms is not None and lon_dms is not None:
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps_ifd[1] = "N"
        gps_ifd[2] = tuple(TiffImagePlugin.IFDRational(n, d) for n, d in lat_dms)
        gps_ifd[3] = "E"
        gps_ifd[4] = tuple(TiffImagePlugin.IFDRational(n, d) for n, d in lon_dms)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes(), quality=90)
    return buf.getvalue()


def _make_gpx_with_start(name: str, start_iso: str, lat0: float = 0.1, lon0: float = 0.2) -> bytes:
    dt0 = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    pts = []
    for idx in range(5):
        ts = (dt0 + timedelta(minutes=idx)).isoformat().replace("+00:00", "Z")
        lat = lat0 + idx * 0.002
        lon = lon0 + idx * 0.002
        ele = 1000.0 + idx * 25.0
        pts.append(
            f'<trkpt lat="{lat:.4f}" lon="{lon:.4f}">'
            f"<ele>{ele:.1f}</ele><time>{ts}</time></trkpt>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>{name}</name><trkseg>{''.join(pts)}</trkseg></trk></gpx>"
    )
    return xml.encode("utf-8")


def test_alembic_0004_upgrade_downgrade_upgrade_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "alembic_0004.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    api_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    alembic_command.upgrade(cfg, "head")
    alembic_command.check(cfg)

    engine = create_db_engine(db_url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        assert {"render_job_chunks", "ingest_jobs"} <= tables
        stage_cols = {c["name"] for c in insp.get_columns("stages")}
        assert {"etl_version", "bronze_sha256", "source_format"} <= stage_cols
        photo_cols = {c["name"] for c in insp.get_columns("project_photos")}
        assert {"camera_make_model", "exif_offset_s"} <= photo_cols
        rj_cols = {c["name"] for c in insp.get_columns("render_jobs")}
        assert {"content_hash", "is_chunked", "total_frames"} <= rj_cols
    finally:
        engine.dispose()

    alembic_command.downgrade(cfg, "0003_project_photos")
    engine_down = create_db_engine(db_url)
    try:
        insp_down = inspect(engine_down)
        assert "render_job_chunks" not in insp_down.get_table_names()
        assert "ingest_jobs" not in insp_down.get_table_names()
        assert "etl_version" not in {c["name"] for c in insp_down.get_columns("stages")}
    finally:
        engine_down.dispose()

    alembic_command.upgrade(cfg, "head")
    alembic_command.check(cfg)


def test_multiformat_tcx_upload_trim_parity_and_auto_organize(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    h = make_harness(rate_limit_gpx_max=50)
    h.create_user("alice@example.com")
    alice, csrf = h.login_client("alice@example.com")

    proj = alice.post(
        "/api/v1/projects",
        json={"title": "Sample Tour"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    proj_id = proj["id"]

    # Reject bad magic bytes
    bad = alice.post(
        f"/api/v1/projects/{proj_id}/tracks",
        files={"file": ("not_a_track.fit", b"NOT-A-FIT-FILE", "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 422

    # Upload TCX via /tracks alias
    tcx_up = alice.post(
        f"/api/v1/projects/{proj_id}/tracks",
        files={"file": ("stage_day2.tcx", SAMPLE_TCX, "application/vnd.garmin.tcx+xml")},
        data={"name": "Day 2 Ride", "day_index": "1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert tcx_up.status_code == 201, tcx_up.text
    tcx_stage = tcx_up.json()
    assert tcx_stage["source_type"] == "gpx"
    assert tcx_stage["source_format"] == "tcx"
    assert tcx_stage["etl_version"] >= 2
    assert tcx_stage["point_count"] == 3

    # Upload an earlier GPX stage so chronological sorting flips order.
    gpx_early = _make_gpx_with_start("Day 1 Morning", "2000-12-31T08:00:00Z")
    s1 = alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("day1.gpx", gpx_early, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert s1["source_format"] == "gpx"

    # Trim parity on s1: trim [60, 180] -> Silver Parquet is_trimmed & Postgres KPIs updated
    trimmed = alice.patch(
        f"/api/v1/projects/{proj_id}/stages/{s1['id']}",
        json={"trim_start_s": 60.0, "trim_end_s": 180.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert trimmed.status_code == 200, trimmed.text
    t_json = trimmed.json()
    assert t_json["point_count"] == 3
    assert t_json["distance_m"] < s1["distance_m"]
    assert t_json["elapsed_seconds"] == pytest.approx(120.0, abs=0.1)

    with h.session_factory() as db:
        st_row = db.get(Stage, uuid.UUID(s1["id"]))
        assert st_row is not None
        df = pl.read_parquet(h.store.resolve_existing_path(st_row.silver_object_key))
        assert int(df.filter(~pl.col("is_trimmed")).height) == 3
        assert int(df.filter(pl.col("is_trimmed")).height) == 2

    assert h.store.resolve_existing_path(
        stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(s1["id"]))
    ).is_file()
    assert h.store.resolve_existing_path(
        stage_chart_cache_key(uuid.UUID(proj_id), uuid.UUID(s1["id"]))
    ).is_file()

    # Re-trimming back to untrimmed [0, 240] must succeed (checks full Silver duration)
    untrimmed = alice.patch(
        f"/api/v1/projects/{proj_id}/stages/{s1['id']}",
        json={"trim_start_s": None, "trim_end_s": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert untrimmed.status_code == 200
    assert untrimmed.json()["point_count"] == 5

    # Auto-organize sorts chronologically, clusters day_index across >6h gap, sets palette + bed
    org = alice.post(
        f"/api/v1/projects/{proj_id}/stages/auto-organize",
        json={"overnight_gap_hours": 6.0, "palette_preset": "topo-emerald"},
        headers={"X-CSRF-Token": csrf},
    )
    assert org.status_code == 200, org.text
    org_json = org.json()
    assert org_json["revision"] == 2
    ordered = org_json["stages"]
    assert [st["id"] for st in ordered] == [s1["id"], tcx_stage["id"]]
    assert [st["day_index"] for st in ordered] == [1, 2]
    assert org_json["settings"]["stage_colors"][s1["id"]] == "#059669"
    assert org_json["settings"]["stage_transitions"][s1["id"]] == "bed"


def test_photos_exif_camera_align_clock_shift_and_pin(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness(rate_limit_photo_max=30)
    h.create_user("alice@example.com")
    h.create_user("bob@example.com")
    alice, csrf = h.login_client("alice@example.com")
    bob, bob_csrf = h.login_client("bob@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "Photo Align Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    gpx = _make_gpx_with_start("Stage 1", "2001-01-01T08:00:00Z")
    stage = alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s1.gpx", gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()

    # Upload photo taken at 10:02:00+00:00 (2h ahead of track 08:00..08:04Z) with GPS near track
    jpg_skew = _make_jpeg_with_camera_exif(
        make="SONY",
        model="ILCE-7M4",
        date_str="2001:01:01 10:02:00",
        offset_str="+00:00",
        lat_dms=((0, 1), (6, 1), (14, 1)),  # ~0.1039 (near track pt 2: 0.104)
        lon_dms=((0, 1), (12, 1), (14, 1)),
    )
    up = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("cam1.jpg", jpg_skew, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    )
    assert up.status_code == 201, up.text
    p1 = up.json()
    assert p1["camera_make_model"] == "SONY ILCE-7M4"
    # Auto-aligned via GPS since timestamp was 2h off (>300s)
    assert p1["match_method"] in {"gps", "timestamp"}

    # Upload photo without GPS whose timestamp is also 2h ahead (10:01:00Z) -> unmatched initially
    jpg_nogps = _make_jpeg_with_camera_exif(
        make="SONY",
        model="ILCE-7M4",
        date_str="2001:01:01 10:01:00",
        offset_str="+00:00",
        lat_dms=None,
        lon_dms=None,
    )
    p2 = alice.post(
        f"/api/v1/projects/{proj_id}/photos",
        files={"file": ("cam2.jpg", jpg_nogps, "image/jpeg")},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert p2["match_method"] == "unmatched"

    # Dry-run shift-clock (-7200s) previews match without persisting
    dry = alice.post(
        f"/api/v1/projects/{proj_id}/photos/shift-clock",
        json={"offset_seconds": -7200, "dry_run": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert dry.status_code == 200, dry.text
    dry_json = dry.json()
    assert dry_json["dry_run"] is True
    assert dry_json["applied_offset_seconds"] == -7200
    assert dry_json["newly_matched_count"] >= 1

    # Verify DB was NOT mutated by dry_run
    still_unmatched = [
        p for p in alice.get(f"/api/v1/projects/{proj_id}/photos").json() if p["id"] == p2["id"]
    ][0]
    assert still_unmatched["match_method"] == "unmatched"
    assert still_unmatched["exif_offset_s"] == 0

    # Auto-detect clock offset & apply (dry_run=False)
    auto_shift = alice.post(
        f"/api/v1/projects/{proj_id}/photos/shift-clock",
        json={"dry_run": False, "camera_make_model": "SONY ILCE-7M4"},
        headers={"X-CSRF-Token": csrf},
    )
    assert auto_shift.status_code == 200, auto_shift.text
    assert auto_shift.json()["applied_offset_seconds"] == -7200
    p2_after = [p for p in auto_shift.json()["photos"] if p["id"] == p2["id"]][0]
    assert p2_after["match_method"] == "timestamp"
    assert p2_after["exif_offset_s"] == -7200

    # Pin photo manually by progress
    pinned = alice.post(
        f"/api/v1/projects/{proj_id}/photos/{p2['id']}/pin",
        json={"progress": 0.5},
        headers={"X-CSRF-Token": csrf},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["match_method"] == "manual"
    assert pinned.json()["stage_id"] == stage["id"]
    assert pinned.json()["matched_progress"] == pytest.approx(0.5, abs=1e-4)

    # BOLA check on pin
    assert (
        bob.post(
            f"/api/v1/projects/{proj_id}/photos/{p2['id']}/pin",
            json={"progress": 0.25},
            headers={"X-CSRF-Token": bob_csrf},
        ).status_code
        == 404
    )

    # Share redaction check: camera_make_model must NOT appear on PublicPhotoOut
    slug = alice.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": csrf},
    ).json()["share_slug"]
    pub_photos = TestClient(h.app).get(f"/api/v1/share/{slug}").json()["photos"]
    assert len(pub_photos) == 2
    for ph in pub_photos:
        assert "camera_make_model" not in ph
        assert "exif_offset_s" not in ph


def test_analytics_career_heatmap_highlights_and_bola(
    make_harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    gold_dir = tmp_path / "gold_data"
    gold_dir.mkdir(parents=True, exist_ok=True)
    h = make_harness(gold_output_dir=str(gold_dir), rate_limit_gpx_max=30)
    h.create_user("alice@example.com")
    h.create_user("bob@example.com")
    alice, csrf = h.login_client("alice@example.com")
    bob, bob_csrf = h.login_client("bob@example.com")

    proj_a = alice.post(
        "/api/v1/projects",
        json={"title": "Alice Sample Project"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    proj_b = bob.post(
        "/api/v1/projects",
        json={"title": "Bob Secret Trip"},
        headers={"X-CSRF-Token": bob_csrf},
    ).json()["id"]

    gpx_a = _make_gpx_with_start("Sample Climb", "2001-01-01T08:00:00Z")
    gpx_b = _make_gpx_with_start("Second Sample", "2001-01-02T08:00:00Z", lat0=0.3, lon0=0.4)
    alice.post(
        f"/api/v1/projects/{proj_a}/gpx",
        files={"file": ("a.gpx", gpx_a, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    bob.post(
        f"/api/v1/projects/{proj_b}/gpx",
        files={"file": ("b.gpx", gpx_b, "application/gpx+xml")},
        headers={"X-CSRF-Token": bob_csrf},
    )

    # Seed Gold DuckDB with data for both Alice and Bob to verify strict BOLA scoping
    con = duckdb.connect(str(gold_dir / "trip_analytics.duckdb"))
    con.execute(
        "CREATE TABLE mart_stage_geo_context "
        "(trip_id VARCHAR, stage_id VARCHAR, primary_country VARCHAR)"
    )
    con.execute(
        "INSERT INTO mart_stage_geo_context VALUES "
        "(?, 's1', 'Fictional Region A'), (?, 's2', 'Fictional Region B')",
        [proj_a, proj_b],
    )
    con.execute(
        "CREATE TABLE mart_spatial_coverage_h3 "
        "(trip_id VARCHAR, h3_index VARCHAR, center_lat DOUBLE, center_lon DOUBLE, "
        "point_count BIGINT, stage_count BIGINT, year INTEGER, activity_type VARCHAR)"
    )
    con.execute(
        "INSERT INTO mart_spatial_coverage_h3 VALUES "
        "(?, 'hex_10_20', 0.1, 0.2, 100, 1, 2001, 'cycling'), "
        "(?, 'hex_30_40', 0.3, 0.4, 200, 1, 2001, 'cycling')",
        [proj_a, proj_b],
    )
    con.close()

    career = alice.get("/api/v1/analytics/career")
    assert career.status_code == 200, career.text
    c_json = career.json()
    assert c_json["total_projects"] == 1
    assert c_json["total_stages"] == 1
    assert c_json["countries_visited"] == ["Fictional Region A"]  # Bob's region is excluded.

    heatmap = alice.get("/api/v1/analytics/heatmap?lod=low")
    assert heatmap.status_code == 200, heatmap.text
    hm_json = heatmap.json()
    assert [t["project_id"] for t in hm_json["trips"]] == [proj_a]
    assert len(hm_json["trips"][0]["polylines"]) >= 1
    assert [cell["h3_index"] for cell in hm_json["hex_cells"]] == ["hex_10_20"]

    # Highlights on owned project & public share link
    hl = alice.get(f"/api/v1/projects/{proj_a}/analytics/highlights")
    assert hl.status_code == 200, hl.text
    assert hl.json()["project_id"] == proj_a
    assert len(hl.json()["splits"]) >= 1

    slug = alice.post(
        f"/api/v1/projects/{proj_a}/share",
        headers={"X-CSRF-Token": csrf},
    ).json()["share_slug"]
    pub_hl = TestClient(h.app).get(f"/api/v1/share/{slug}/analytics/highlights")
    assert pub_hl.status_code == 200
    assert pub_hl.json()["project_id"] == proj_a


def test_strava_polyline_webhook_queue_render_cache_reaper_and_cli(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.69", 443))],
    )

    h = make_harness(rate_limit_render_max=20)
    user = h.create_user("alice@example.com")
    alice, csrf = h.login_client("alice@example.com")

    # Strava activity summary_polyline + extra stream keys
    from trip_api.db import OAuthConnection
    from trip_api.security import encrypt_secret

    with h.session_factory() as db:
        aad = f"oauth:{user.id}:strava".encode()
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="123456",
                access_token_enc=encrypt_secret("acc", h.settings.decoded_encryption_key, aad=aad),
                refresh_token_enc=encrypt_secret("ref", h.settings.decoded_encryption_key, aad=aad),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
                scope="read,activity:read_all",
            )
        )
        db.commit()

    seen_stream_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 777,
                        "name": "Sample Climb",
                        "sport_type": "Ride",
                        "start_date": "2001-01-02T07:00:00Z",
                        "distance": 25000.0,
                        "total_elevation_gain": 900.0,
                        "moving_time": 3600,
                        "start_latlng": [0.1, 0.2],
                        # Google's standard encoded-polyline example tests pass-through.
                        "map": {"summary_polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"},
                    }
                ],
            )
        if request.url.path == "/api/v3/activities/777":
            return httpx.Response(
                200,
                json={"id": 777, "name": "Sample Climb", "start_date": "2001-01-02T07:00:00Z"},
            )
        if request.url.path == "/api/v3/activities/777/streams":
            seen_stream_keys.append(str(request.url.params.get("keys", "")))
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.10, 0.20], [0.11, 0.21]]},
                    "time": {"data": [0, 60]},
                    "altitude": {"data": [1500.0, 1550.0]},
                },
            )
        return httpx.Response(404)

    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    acts = alice.get("/api/v1/strava/activities")
    assert acts.status_code == 200
    assert acts.json()[0]["summary_polyline"] == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "Cache & Replay"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    imp = alice.post(
        f"/api/v1/projects/{proj_id}/import-strava",
        json={"activity_id": 777},
        headers={"X-CSRF-Token": csrf},
    )
    assert imp.status_code == 201
    assert "grade_smooth" in seen_stream_keys[0]
    assert "temp" in seen_stream_keys[0]
    assert "moving" in seen_stream_keys[0]

    # Webhook enqueues IngestJob for activity create/update/delete
    wh = TestClient(h.app).post(
        "/api/v1/strava/webhook",
        headers={"X-Strava-Verify-Token": "webhook-verify-token-secret"},
        json={
            "object_type": "activity",
            "aspect_type": "delete",
            "owner_id": 123456,
            "object_id": 777,
        },
    )
    assert wh.status_code == 200
    with h.session_factory() as db:
        ing_rows = db.scalars(select(IngestJob)).all()
        assert len(ing_rows) == 1
        assert ing_rows[0].event_type == "delete"
        assert ing_rows[0].status == "queued"

    # Content-addressed render cache hit copies artifacts to isolated job keys
    r1 = alice.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={"resolution": "1280x720", "fps": 30},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "cache-miss-0001"},
    )
    assert r1.status_code == 202
    j1_id = r1.json()["id"]
    h.store.put_bytes(f"outputs/{j1_id}.mp4", b"CACHED-VIDEO", content_type="video/mp4")
    h.store.put_bytes(f"outputs/{j1_id}.webp", b"CACHED-THUMB", content_type="image/webp")
    with h.session_factory() as db:
        j1 = db.get(RenderJob, uuid.UUID(j1_id))
        assert j1 is not None
        j1.status = "succeeded"
        j1.progress_pct = 100
        j1.output_object_key = f"outputs/{j1_id}.mp4"
        j1.thumbnail_key = f"outputs/{j1_id}.webp"
        j1.expires_at = datetime.now(UTC) + timedelta(days=3)
        db.commit()

    r2 = alice.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={"resolution": "1280x720", "fps": 30},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "cache-hit-0002"},
    )
    assert r2.status_code == 202
    j2 = r2.json()
    assert j2["id"] != j1_id
    assert j2["status"] == "succeeded"
    assert j2["progress_pct"] == 100
    assert h.store.get_bytes(f"outputs/{j2['id']}.mp4") == b"CACHED-VIDEO"
    assert h.store.get_bytes(f"outputs/{j2['id']}.webp") == b"CACHED-THUMB"

    # Reaper cleans up render_job_chunks blobs as well
    h.store.put_bytes("outputs/chunks/exp/0000.mp4", b"CHUNK-0")
    h.store.put_bytes(f"outputs/{j1_id}.mp4", b"CACHED-VIDEO")
    with h.session_factory() as db:
        exp_job = db.get(RenderJob, uuid.UUID(j1_id))
        assert exp_job is not None
        exp_job.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        db.add(
            RenderJobChunk(
                render_job_id=exp_job.id,
                chunk_index=0,
                start_frame=0,
                end_frame=300,
                status="succeeded",
                chunk_object_key="outputs/chunks/exp/0000.mp4",
            )
        )
        db.commit()
        rep = reap_expired(db, h.store, now=datetime.now(UTC))
        assert rep.render_jobs_deleted == 1
        assert rep.blobs_deleted == 3  # mp4 + webp + chunk
        assert db.scalars(select(RenderJobChunk)).all() == []
    with pytest.raises(FileNotFoundError):
        h.store.resolve_existing_path("outputs/chunks/exp/0000.mp4")

    # CLI process-ingest-jobs processes delete job
    monkeypatch.setenv("LOCAL_OBJECT_STORE_DIR", str(h.store.root_dir))
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "local")
    db_url = f"sqlite:///{h.db_path}"
    assert cli_main(["process-ingest-jobs", "--database-url", db_url]) == 0
    with h.session_factory() as db:
        assert db.scalar(select(Stage).where(Stage.strava_activity_id == 777)) is None
        ing = db.scalar(select(IngestJob))
        assert ing is not None
        assert ing.status == "succeeded"

    # CLI replay-silver replays stage with etl_version=1 to target=2
    alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("rep.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    with h.session_factory() as db:
        st = db.scalar(select(Stage))
        assert st is not None
        st.etl_version = 1
        db.commit()

    assert cli_main(["replay-silver", "--target-etl-version", "2", "--database-url", db_url]) == 0
    with h.session_factory() as db:
        st_after = db.scalar(select(Stage))
        assert st_after is not None
        assert st_after.etl_version == 2
        assert json.dumps(st_after.warnings_json) is not None
