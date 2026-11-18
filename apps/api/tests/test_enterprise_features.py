from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from trip_api.db import OAuthConnection, RenderJob, create_db_engine
from trip_api.reaper import reap_expired
from trip_api.security import encrypt_secret
from trip_api.services import (
    build_project_telemetry_chart,
    build_stage_geometry_feature,
    invalidate_or_rebuild_stage_lod_cache,
    persist_or_delete_stage_lod_cache,
    stage_lod_cache_key,
)
from trip_api.storage import LocalObjectStore, ObjectStream, S3ObjectStore, parse_byte_range
from trip_telemetry import parse_gpx_bytes

if TYPE_CHECKING:
    from conftest import AppHarness, HarnessFactory


def _fake_public_getaddrinfo(
    *args: Any, **kwargs: Any
) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("151.101.1.69", 443),
        )
    ]


def test_alembic_migration_0002_upgrade_and_downgrade(tmp_path: Path, monkeypatch: Any) -> None:
    db_file = tmp_path / "alembic_0002.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    api_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    alembic_command.upgrade(cfg, "head")
    engine = create_db_engine(db_url)
    try:
        insp = inspect(engine)
        proj_cols = {c["name"] for c in insp.get_columns("projects")}
        stage_cols = {c["name"] for c in insp.get_columns("stages")}
        audit_cols = {c["name"] for c in insp.get_columns("audit_events")}
        assert {"share_enabled", "share_slug", "share_created_at"} <= proj_cols
        assert "activity_type" in stage_cols
        assert "project_id" in audit_cols
    finally:
        engine.dispose()

    alembic_command.downgrade(cfg, "0001_initial")
    engine_down = create_db_engine(db_url)
    try:
        insp_down = inspect(engine_down)
        proj_cols_down = {c["name"] for c in insp_down.get_columns("projects")}
        stage_cols_down = {c["name"] for c in insp_down.get_columns("stages")}
        audit_cols_down = {c["name"] for c in insp_down.get_columns("audit_events")}
        assert "share_slug" not in proj_cols_down
        assert "activity_type" not in stage_cols_down
        assert "project_id" not in audit_cols_down
    finally:
        engine_down.dispose()

    alembic_command.upgrade(cfg, "head")


def test_per_stage_cached_geometry_activity_type_and_chart_floor(
    make_harness: HarnessFactory, sample_gpx: bytes
) -> None:
    harness = make_harness(rate_limit_gpx_max=50)
    assert harness.app.title == "Trip Animator API"

    harness.create_user("alice@example.com")
    harness.create_user("bob@example.com")
    alice, csrf = harness.login_client("alice@example.com")
    bob, _ = harness.login_client("bob@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={
            "title": "Fictional Tour",
            "settings": {"camera_preset": "dynamic-3d", "camera_bearing_deg": 35.0},
        },
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # 1. GPX filename/name inference + explicit override
    up_hike = alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("ridge_hike.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "1", "name": "Ridge Hike"},
        headers={"X-CSRF-Token": csrf},
    )
    assert up_hike.status_code == 201, up_hike.text
    stage_hike = up_hike.json()
    assert stage_hike["activity_type"] == "hiking"

    # .lod.json.gz cache should have been eagerly persisted on upload
    lod_key = stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(stage_hike["id"]))
    assert harness.store.resolve_existing_path(lod_key).is_file()

    # Single-stage cached geometry endpoint returns Feature with activity_type
    sg = alice.get(f"/api/v1/projects/{proj_id}/stages/{stage_hike['id']}/geometry?lod=medium")
    assert sg.status_code == 200, sg.text
    feat = sg.json()
    assert feat["type"] == "Feature"
    assert feat["id"] == stage_hike["id"]
    assert feat["properties"]["activity_type"] == "hiking"

    # BOLA check on per-stage geometry
    assert (
        bob.get(
            f"/api/v1/projects/{proj_id}/stages/{stage_hike['id']}/geometry?lod=medium"
        ).status_code
        == 404
    )

    # Delete .lod.json.gz and verify lazy backfill via /telemetry/geometry
    harness.store.delete(lod_key)
    all_geom = alice.get(f"/api/v1/projects/{proj_id}/telemetry/geometry?lod=low")
    assert all_geom.status_code == 200
    assert all_geom.json()["features"][0]["properties"]["activity_type"] == "hiking"
    assert harness.store.resolve_existing_path(lod_key).is_file()

    # PATCH stage activity_type & trim invalidates/rebuilds cache
    bad_act = alice.patch(
        f"/api/v1/projects/{proj_id}/stages/{stage_hike['id']}",
        json={"activity_type": "spaceship"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_act.status_code == 422

    patched = alice.patch(
        f"/api/v1/projects/{proj_id}/stages/{stage_hike['id']}",
        json={"activity_type": "trail_run", "trim_start_s": 60.0, "trim_end_s": 180.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert patched.status_code == 200
    assert patched.json()["activity_type"] == "trail_run"

    sg_trimmed = alice.get(
        f"/api/v1/projects/{proj_id}/stages/{stage_hike['id']}/geometry?lod=full"
    ).json()
    assert sg_trimmed["properties"]["activity_type"] == "trail_run"
    assert sg_trimmed["properties"]["distance_m"] < stage_hike["distance_m"]

    # Multi-stage chart floor check: upload 11 more stages (12 total) and request max_points=120
    for idx in range(2, 13):
        resp = alice.post(
            f"/api/v1/projects/{proj_id}/gpx",
            files={"file": (f"stage_{idx}.gpx", sample_gpx, "application/gpx+xml")},
            data={"day_index": str(idx), "activity_type": "gravel_mtb"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 201
    chart = alice.get(f"/api/v1/projects/{proj_id}/telemetry/chart?max_points=120").json()
    assert len(chart["points"]) <= 120
    assert len(chart["points"]) >= 40  # Never collapsed to 2 pts/stage


def test_strava_date_forwarding_and_batch_import(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness(rate_limit_gpx_max=2, rate_limit_strava_import_max=10)

    captured_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/athlete/activities":
            captured_params.append(dict(request.url.params))
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1001,
                        "name": "Gravel Fondo",
                        "sport_type": "GravelRide",
                        "start_date": "2001-01-01T08:00:00Z",
                        "distance": 62000.0,
                        "total_elevation_gain": 1200.0,
                        "moving_time": 9000,
                        "start_latlng": [0.1, 0.2],
                    }
                ],
            )
        if path == "/api/v3/activities/1001":
            return httpx.Response(
                200,
                json={
                    "id": 1001,
                    "name": "Gravel Fondo",
                    "sport_type": "GravelRide",
                    "start_date": "2001-01-01T08:00:00Z",
                },
            )
        if path == "/api/v3/activities/1001/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.10, 0.20], [0.11, 0.21], [0.12, 0.22]]},
                    "time": {"data": [0, 60, 120]},
                    "altitude": {"data": [1000.0, 1020.0, 1045.0]},
                },
            )
        if path == "/api/v3/activities/1002":
            return httpx.Response(
                200,
                json={
                    "id": 1002,
                    "name": "Sample Hike",
                    "sport_type": "Hike",
                    "start_date": "2001-01-02T09:00:00Z",
                },
            )
        if path == "/api/v3/activities/1002/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.30, 0.40], [0.31, 0.41]]},
                    "time": {"data": [0, 120]},
                    "altitude": {"data": [1500.0, 1580.0]},
                },
            )
        if path == "/api/v3/activities/9999":
            return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )

    user = h.create_user("rider@example.com")
    with h.session_factory() as db:
        aad = f"oauth:{user.id}:strava".encode()
        key = h.settings.decoded_encryption_key
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="555",
                access_token_enc=encrypt_secret("acc", key, aad=aad),
                refresh_token_enc=encrypt_secret("ref", key, aad=aad),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
                scope="read,activity:read_all",
            )
        )
        db.commit()

    client, csrf = h.login_client("rider@example.com")

    # Date validation
    assert (
        client.get("/api/v1/strava/activities?after=2001-06-20&before=2001-06-10").status_code
        == 422
    )
    assert client.get("/api/v1/strava/activities?after=not-a-date").status_code == 422

    # Date forwarding converts ISO dates to UTC epoch seconds
    acts = client.get(
        "/api/v1/strava/activities?page=1&per_page=30&after=2001-06-01&before=2001-06-30"
    )
    assert acts.status_code == 200
    expected_after = str(int(datetime(2001, 6, 1, 0, 0, 0, tzinfo=UTC).timestamp()))
    expected_before = str(int(datetime(2001, 6, 30, 23, 59, 59, tzinfo=UTC).timestamp()))
    assert captured_params[-1]["after"] == expected_after
    assert captured_params[-1]["before"] == expected_before

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Strava Batch"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # Batch import with partial failure (9999 -> 404) & dedicated rate-limit bucket
    batch = client.post(
        f"/api/v1/projects/{proj_id}/import-strava/batch",
        json={
            "items": [
                {"activity_id": 1001, "day_index": 1},
                {"activity_id": 1002, "day_index": 2, "custom_name": "Seceda Hike"},
                {"activity_id": 9999, "day_index": 3},
            ]
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert batch.status_code == 200, batch.text
    body = batch.json()
    assert len(body["imported"]) == 2
    assert body["imported"][0]["activity_type"] == "gravel_mtb"
    assert body["imported"][0]["strava_activity_id"] == 1001
    assert body["imported"][1]["activity_type"] == "hiking"
    assert body["imported"][1]["name"] == "Seceda Hike"
    assert body["failed"] == [{"activity_id": 9999, "detail": "Strava activity not found"}]


def test_public_share_privacy_audit_and_gpx_geojson_export(
    harness: AppHarness, sample_gpx: bytes
) -> None:
    harness.create_user("owner@example.com")
    harness.create_user("stranger@example.com")
    owner, owner_csrf = harness.login_client("owner@example.com")
    stranger, stranger_csrf = harness.login_client("stranger@example.com")

    proj_id = owner.post(
        "/api/v1/projects",
        json={"title": "Public Fictional Tour"},
        headers={"X-CSRF-Token": owner_csrf},
    ).json()["id"]

    stage = owner.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("day1.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "1", "name": "Passo Gardena", "activity_type": "cycling"},
        headers={"X-CSRF-Token": owner_csrf},
    ).json()

    # Seed a succeeded render job with MP4 bytes
    render_id = uuid.uuid4()
    mp4_key = f"outputs/{render_id}.mp4"
    harness.store.put_bytes(mp4_key, b"PUBLIC-MP4-DATA", content_type="video/mp4")
    with harness.session_factory() as db:
        db.add(
            RenderJob(
                id=render_id,
                project_id=uuid.UUID(proj_id),
                owner_user_id=uuid.UUID(owner.get("/api/v1/auth/me").json()["user"]["id"]),
                idempotency_key="share-render-seed-1",
                idempotency_hash="abc",
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
                output_object_key=mp4_key,
            )
        )
        db.commit()

    # Stranger cannot enable share or read audit/export
    assert (
        stranger.post(
            f"/api/v1/projects/{proj_id}/share",
            headers={"X-CSRF-Token": stranger_csrf},
        ).status_code
        == 404
    )
    assert stranger.get(f"/api/v1/projects/{proj_id}/audit").status_code == 404
    assert stranger.get(f"/api/v1/projects/{proj_id}/export?format=gpx").status_code == 404

    # Enable sharing
    share_resp = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert share_resp.status_code == 200
    slug = share_resp.json()["share_slug"]
    assert slug and len(slug) >= 16
    assert share_resp.json()["share_enabled"] is True

    # Unauthenticated client can access redacted public share routes
    anon = TestClient(harness.app)
    pub = anon.get(f"/api/v1/share/{slug}")
    assert pub.status_code == 200, pub.text
    pub_json = pub.json()
    assert pub_json["title"] == "Public Fictional Tour"
    assert pub_json["has_rendered_video"] is True
    assert pub_json["latest_render_id"] == str(render_id)
    assert "owner_user_id" not in pub_json
    assert "share_slug" not in pub_json
    assert "owner@example.com" not in pub.text
    pub_stage = pub_json["stages"][0]
    for forbidden_key in (
        "bronze_object_key",
        "silver_object_key",
        "external_source_id",
        "strava_activity_id",
        "warnings",
    ):
        assert forbidden_key not in pub_stage

    assert anon.get(f"/api/v1/share/{slug}/geometry?lod=medium").status_code == 200
    assert (
        anon.get(f"/api/v1/share/{slug}/stages/{stage['id']}/geometry?lod=medium").status_code
        == 200
    )
    assert anon.get(f"/api/v1/share/{slug}/chart").status_code == 200
    vid = anon.get(f"/api/v1/share/{slug}/render/video")
    assert vid.status_code == 200
    assert vid.content == b"PUBLIC-MP4-DATA"
    assert vid.headers["x-content-type-options"] == "nosniff"
    assert vid.headers["accept-ranges"] == "bytes"
    assert vid.headers["content-length"] == str(len(b"PUBLIC-MP4-DATA"))

    vid_range = anon.get(
        f"/api/v1/share/{slug}/render/video",
        headers={"Range": "bytes=0-5"},
    )
    assert vid_range.status_code == 206
    assert vid_range.content == b"PUBLIC"
    assert vid_range.headers["content-range"] == f"bytes 0-5/{len(b'PUBLIC-MP4-DATA')}"

    vid_bad_range = anon.get(
        f"/api/v1/share/{slug}/render/video",
        headers={"Range": "bytes=999-"},
    )
    assert vid_bad_range.status_code == 416

    # Rotate slug invalidates old slug immediately
    rot = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        json={"rotate": True},
        headers={"X-CSRF-Token": owner_csrf},
    )
    new_slug = rot.json()["share_slug"]
    assert new_slug != slug
    assert anon.get(f"/api/v1/share/{slug}").status_code == 404
    assert anon.get(f"/api/v1/share/{new_slug}").status_code == 200

    # Disable share revokes public access
    dis = owner.delete(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert dis.status_code == 200
    assert dis.json()["share_enabled"] is False
    assert anon.get(f"/api/v1/share/{new_slug}").status_code == 404
    assert anon.get(f"/api/v1/share/{new_slug}/render/video").status_code == 404

    # GPX & GeoJSON export
    gpx_resp = owner.get(f"/api/v1/projects/{proj_id}/export?format=gpx")
    assert gpx_resp.status_code == 200
    assert gpx_resp.headers["content-type"].startswith("application/gpx+xml")
    reparsed = parse_gpx_bytes(gpx_resp.content)
    assert reparsed.point_count == 4
    assert reparsed.activity_type_hint == "cycling"

    geojson_resp = owner.get(f"/api/v1/projects/{proj_id}/export?format=geojson")
    assert geojson_resp.status_code == 200
    fc = geojson_resp.json()
    assert fc["type"] == "FeatureCollection"
    assert fc["features"][0]["properties"]["activity_type"] == "cycling"

    # Audit log contains project lifecycle events
    audit_resp = owner.get(f"/api/v1/projects/{proj_id}/audit?limit=50")
    assert audit_resp.status_code == 200
    events = audit_resp.json()
    event_types = {e["event_type"] for e in events}
    assert {
        "project.created",
        "stage.gpx_uploaded",
        "project.share_enabled",
        "project.share_rotated",
        "project.share_disabled",
        "project.exported",
    } <= event_types


def test_share_rate_limit_expire_slug_edge_cases_and_range_matrix(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    h = make_harness(rate_limit_share_max=15, rate_limit_share_window_s=60)
    owner_user = h.create_user("sharematrix@example.com")
    owner, csrf = h.login_client("sharematrix@example.com")

    proj_id = owner.post(
        "/api/v1/projects",
        json={"title": "Share Matrix"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    stage_id = owner.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # Enable share idempotently (re-posting without rotate=True preserves slug)
    slug1 = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": csrf},
    ).json()["share_slug"]
    slug2 = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        json={"rotate": False},
        headers={"X-CSRF-Token": csrf},
    ).json()["share_slug"]
    assert slug1 == slug2

    anon = TestClient(h.app)

    # Blank/overlong slug -> 404; missing stage_id -> 404; no render yet -> 404
    assert anon.get(f"/api/v1/share/{'x' * 65}").status_code == 404
    assert anon.get("/api/v1/share/%20%20").status_code == 404
    missing_sid = str(uuid.uuid4())
    assert anon.get(f"/api/v1/share/{slug1}/geometry?stage_id={missing_sid}").status_code == 404
    assert anon.get(f"/api/v1/share/{slug1}/stages/{missing_sid}/geometry").status_code == 404
    assert anon.get(f"/api/v1/share/{slug1}/chart?stage_id={missing_sid}").status_code == 404
    assert anon.get(f"/api/v1/share/{slug1}/render/video").status_code == 404
    assert anon.get(f"/api/v1/share/{slug1}").json()["has_rendered_video"] is False

    # Seed a succeeded render job with known 26-byte MP4 payload and test full Range matrix
    payload = b"abcdefghijklmnopqrstuvwxyz"
    job_id = uuid.uuid4()
    mp4_key = f"outputs/{job_id}.mp4"
    h.store.put_bytes(mp4_key, payload, content_type="video/mp4")
    with h.session_factory() as db:
        db.add(
            RenderJob(
                id=job_id,
                project_id=uuid.UUID(proj_id),
                owner_user_id=owner_user.id,
                idempotency_key="share-range-job",
                idempotency_hash="r" * 64,
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
                output_object_key=mp4_key,
                expires_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        db.commit()

    # Exact Range header test cases on /share/{slug}/render/video
    r_0_9 = anon.get(f"/api/v1/share/{slug1}/render/video", headers={"Range": "bytes=0-9"})
    assert r_0_9.status_code == 206
    assert r_0_9.content == b"abcdefghij"
    assert r_0_9.headers["content-range"] == "bytes 0-9/26"

    r_10_end = anon.get(f"/api/v1/share/{slug1}/render/video", headers={"Range": "bytes=10-"})
    assert r_10_end.status_code == 206
    assert r_10_end.content == b"klmnopqrstuvwxyz"
    assert r_10_end.headers["content-range"] == "bytes 10-25/26"

    r_suffix = anon.get(f"/api/v1/share/{slug1}/render/video", headers={"Range": "bytes=-5"})
    assert r_suffix.status_code == 206
    assert r_suffix.content == b"vwxyz"
    assert r_suffix.headers["content-range"] == "bytes 21-25/26"

    r_416 = anon.get(f"/api/v1/share/{slug1}/render/video", headers={"Range": "bytes=999999-"})
    assert r_416.status_code == 416
    assert r_416.headers["content-range"] == "bytes */26"

    # Non-bytes / multi-range falls back to full 200
    r_multi = anon.get(
        f"/api/v1/share/{slug1}/render/video", headers={"Range": "bytes=0-1,2-3"}
    )
    assert r_multi.status_code == 200
    assert r_multi.content == payload

    # Reaping the expired render job clears public video availability cleanly
    with h.session_factory() as db:
        report = reap_expired(db, h.store)
        assert report.render_jobs_deleted == 1
        assert report.blobs_deleted == 1

    assert anon.get(f"/api/v1/share/{slug1}").json()["has_rendered_video"] is False
    assert anon.get(f"/api/v1/share/{slug1}/render/video").status_code == 404

    # Remaining share tokens consumed -> 429 with Retry-After
    for _ in range(15):
        resp = anon.get(f"/api/v1/share/{slug1}/stages/{stage_id}/geometry?lod=low")
        if resp.status_code == 429:
            assert "retry-after" in resp.headers
            break
    else:
        pytest.fail("Expected public share rate limit 429")


def test_public_share_video_s3_missing_object_returns_404(
    harness: AppHarness,
    sample_gpx: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.create_user("share404@example.com")
    owner, csrf = harness.login_client("share404@example.com")
    proj_id = owner.post(
        "/api/v1/projects",
        json={"title": "Share S3 404"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    owner.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("day1.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    slug = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": csrf},
    ).json()["share_slug"]

    render_id = uuid.uuid4()
    with harness.session_factory() as db:
        db.add(
            RenderJob(
                id=render_id,
                project_id=uuid.UUID(proj_id),
                owner_user_id=uuid.UUID(owner.get("/api/v1/auth/me").json()["user"]["id"]),
                idempotency_key="share-s3-404",
                idempotency_hash="k" * 64,
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
                output_object_key=f"outputs/{render_id}.mp4",
            )
        )
        db.commit()

    class MissingS3Client:
        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: MissingS3Client())
    harness.app.state.object_store = S3ObjectStore(
        endpoint_url="",
        access_key_id="",
        secret_access_key="",
        bucket="b",
        region="",
    )

    anon = TestClient(harness.app)
    resp = anon.get(f"/api/v1/share/{slug}/render/video")
    assert resp.status_code == 404


def test_non_local_store_materialization_and_lod_cache_error_fallbacks(
    harness: AppHarness,
    sample_gpx: bytes,
) -> None:
    """Exercise non-LocalObjectStore parallel Parquet fetching and cache-failure fallbacks."""
    harness.create_user("nonlocal@example.com")
    client, csrf = harness.login_client("nonlocal@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "S3 Mem Store"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    for idx in range(2):
        assert (
            client.post(
                f"/api/v1/projects/{proj_id}/gpx",
                files={"file": (f"s{idx}.gpx", sample_gpx, "application/gpx+xml")},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 201
        )

    # Mirror stored objects into a non-LocalObjectStore in-memory ObjectStore implementation
    class MemoryStore:
        def __init__(self, backing: LocalObjectStore) -> None:
            self._blobs: dict[str, bytes] = {}
            for path in backing.root_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(backing.root_dir).as_posix()
                    self._blobs[rel] = path.read_bytes()
            self.fail_lod_writes = False

        def put_bytes(
            self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
        ) -> None:
            _ = content_type
            if self.fail_lod_writes and key.endswith(".lod.json.gz"):
                raise OSError("transient object store outage")
            self._blobs[key] = data

        def get_bytes(self, key: str) -> bytes:
            if key not in self._blobs:
                raise FileNotFoundError(key)
            return self._blobs[key]

        def open_stream(
            self,
            key: str,
            *,
            range_header: str | None = None,
            chunk_size: int = 65536,
        ) -> ObjectStream:
            data = self.get_bytes(key)
            _ = (range_header, chunk_size)
            _ = parse_byte_range(range_header, len(data)) if range_header else None
            return ObjectStream(
                chunks=iter([data]),
                total_size=len(data),
                content_length=len(data),
                start=0,
                end=max(len(data) - 1, 0),
                is_partial=False,
            )

        def copy(
            self, src_key: str, dst_key: str, *, content_type: str | None = None
        ) -> None:
            _ = content_type
            self._blobs[dst_key] = self.get_bytes(src_key)

        def delete(self, key: str) -> None:
            self._blobs.pop(key, None)

    mem_store = MemoryStore(harness.store)
    harness.app.state.object_store = mem_store

    # Exercises materialize_silver_parquets ThreadPoolExecutor path (2 stages) + chart/kpis/geom
    chart = client.get(f"/api/v1/projects/{proj_id}/telemetry/chart?max_points=50")
    assert chart.status_code == 200
    assert len(chart.json()["points"]) >= 4

    kpis = client.get(f"/api/v1/projects/{proj_id}/kpis")
    assert kpis.status_code == 200
    assert kpis.json()["stage_count"] == 2

    # Empty stage list in chart & geometry services
    empty_chart = build_project_telemetry_chart(mem_store, uuid.UUID(proj_id), [], 100)
    assert empty_chart.total_distance_m == 0.0
    assert empty_chart.points == []

    # Cache write failure fallback deletes stale LOD cache cleanly
    from trip_api.db import Stage as StageModel

    with harness.session_factory() as db:
        st = db.query(StageModel).first()
        assert st is not None
        lod_key = stage_lod_cache_key(st.project_id, st.id)
        mem_store.fail_lod_writes = True
        persist_or_delete_stage_lod_cache(mem_store, st, b"dummy")
        assert lod_key not in mem_store._blobs
        invalidate_or_rebuild_stage_lod_cache(mem_store, st)
        assert lod_key not in mem_store._blobs

        # Backfill succeeds even when write fails in build_stage_geometry_feature
        feat = build_stage_geometry_feature(mem_store, st, "medium")
        assert feat["type"] == "Feature"
