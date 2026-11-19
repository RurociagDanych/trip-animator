from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
import trip_api.routers.stages as stages_router
from trip_api.db import RenderJob, Stage
from trip_api.services import (
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
)
from trip_telemetry import parse_gpx_bytes

if TYPE_CHECKING:
    from conftest import AppHarness, HarnessFactory


def test_project_crud_optimistic_locking_and_bola(harness: AppHarness) -> None:
    harness.create_user("alice@example.com")
    harness.create_user("bob@example.com")
    alice, alice_csrf = harness.login_client("alice@example.com")
    bob, bob_csrf = harness.login_client("bob@example.com")

    # Missing CSRF blocked
    assert alice.post("/api/v1/projects", json={"title": "Fictional"}).status_code == 403

    create_resp = alice.post(
        "/api/v1/projects",
        json={"title": "Fictional Loop"},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert create_resp.status_code == 201
    proj = create_resp.json()
    project_id = proj["id"]
    assert proj["revision"] == 1

    # Bob cannot access or mutate Alice's project
    assert bob.get(f"/api/v1/projects/{project_id}").status_code == 404
    assert (
        bob.patch(
            f"/api/v1/projects/{project_id}",
            json={"revision": 1, "title": "Stolen"},
            headers={"X-CSRF-Token": bob_csrf},
        ).status_code
        == 404
    )
    assert (
        bob.delete(
            f"/api/v1/projects/{project_id}",
            headers={"X-CSRF-Token": bob_csrf},
        ).status_code
        == 404
    )

    # Valid update increments revision
    upd1 = alice.patch(
        f"/api/v1/projects/{project_id}",
        json={"revision": 1, "title": "Fictional Tour"},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert upd1.status_code == 200
    assert upd1.json()["revision"] == 2
    assert upd1.json()["title"] == "Fictional Tour"

    # Stale revision triggers 409 Conflict
    conflict = alice.patch(
        f"/api/v1/projects/{project_id}",
        json={"revision": 1, "title": "Stale Write"},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert conflict.status_code == 409

    # Foreign stage ID in stage_colors / stage_transitions rejected with 422
    foreign_stage_id = str(uuid.uuid4())
    bad_settings = alice.patch(
        f"/api/v1/projects/{project_id}",
        json={
            "revision": 2,
            "settings": {
                "stage_colors": {foreign_stage_id: "#FF0000"},
                "stage_transitions": {foreign_stage_id: "bed"},
            },
        },
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert bad_settings.status_code == 422


def test_gpx_upload_stages_reorder_trim_and_duckdb_telemetry(
    harness: AppHarness, sample_gpx: bytes
) -> None:
    harness.create_user("alice@example.com")
    client, csrf = harness.login_client("alice@example.com")

    proj_resp = client.post(
        "/api/v1/projects",
        json={"title": "Synthetic Project"},
        headers={"X-CSRF-Token": csrf},
    )
    project_id = proj_resp.json()["id"]

    # Upload Stage 1
    up1 = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("day1.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "1", "name": "Day 1 Climb"},
        headers={"X-CSRF-Token": csrf},
    )
    assert up1.status_code == 201, up1.text
    stage1 = up1.json()
    assert stage1["source_type"] == "gpx"
    assert stage1["day_index"] == 1
    assert stage1["point_count"] == 4
    assert stage1["distance_m"] > 300.0
    assert stage1["elevation_gain_m"] == 85.0

    # Upload Stage 2
    up2 = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("day2.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "2", "name": "Day 2 Descent"},
        headers={"X-CSRF-Token": csrf},
    )
    assert up2.status_code == 201
    stage2 = up2.json()

    # Validate stage_colors & stage_transitions with real stage IDs
    patch_ok = client.patch(
        f"/api/v1/projects/{project_id}",
        json={
            "revision": 1,
            "settings": {
                "stage_colors": {stage1["id"]: "#22C55E", stage2["id"]: "#3B82F6"},
                "stage_transitions": {stage1["id"]: "bed", stage2["id"]: "none"},
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_ok.status_code == 200

    # Reorder stages (reverse order) + reject invalid permutation
    bad_reorder = client.post(
        f"/api/v1/projects/{project_id}/stages/reorder",
        json={"stage_ids": [stage1["id"]]},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_reorder.status_code == 422

    reorder_ok = client.post(
        f"/api/v1/projects/{project_id}/stages/reorder",
        json={
            "stage_ids": [stage2["id"], stage1["id"]],
            "day_indices": {stage2["id"]: 1, stage1["id"]: 2},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert reorder_ok.status_code == 200
    reordered = reorder_ok.json()
    assert [s["id"] for s in reordered] == [stage2["id"], stage1["id"]]
    assert reordered[0]["day_index"] == 1
    assert reordered[1]["day_index"] == 2

    # Non-destructive trim validation
    bad_trim = client.patch(
        f"/api/v1/projects/{project_id}/stages/{stage1['id']}",
        json={"trim_start_s": 120.0, "trim_end_s": 60.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_trim.status_code == 422

    good_trim = client.patch(
        f"/api/v1/projects/{project_id}/stages/{stage1['id']}",
        json={"trim_start_s": 30.0, "trim_end_s": 150.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert good_trim.status_code == 200
    assert good_trim.json()["trim_start_s"] == 30.0

    # DuckDB telemetry chart, geometry & KPIs
    chart_resp = client.get(f"/api/v1/projects/{project_id}/telemetry/chart?max_points=20")
    assert chart_resp.status_code == 200
    chart = chart_resp.json()
    assert len(chart["points"]) >= 2
    assert chart["total_distance_m"] > 0

    geom_resp = client.get(f"/api/v1/projects/{project_id}/telemetry/geometry?lod=low")
    assert geom_resp.status_code == 200
    geom = geom_resp.json()
    assert geom["type"] == "FeatureCollection"
    assert len(geom["features"]) == 2
    assert geom["features"][0]["geometry"]["type"] in ("LineString", "MultiLineString")
    trimmed_feature = next(
        f for f in geom["features"] if f["properties"]["stage_id"] == stage1["id"]
    )
    assert trimmed_feature["properties"]["distance_m"] < stage1["distance_m"]

    kpis_resp = client.get(f"/api/v1/projects/{project_id}/kpis")
    assert kpis_resp.status_code == 200
    kpis_body = kpis_resp.json()
    assert kpis_body["stage_count"] == 2
    assert kpis_body["total_distance_m"] < (stage1["distance_m"] + stage2["distance_m"])

    # Deleting stage cleans up settings references and compacts order
    del_resp = client.delete(
        f"/api/v1/projects/{project_id}/stages/{stage1['id']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert del_resp.status_code == 204
    detail = client.get(f"/api/v1/projects/{project_id}").json()
    assert len(detail["stages"]) == 1
    assert stage1["id"] not in detail["settings"]["stage_colors"]


def test_gpx_security_xxe_size_cap_magic_bytes_and_malformed_multipart(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness(rate_limit_gpx_max=20)
    h.create_user("sec@example.com")
    client, csrf = h.login_client("sec@example.com")
    project_id = client.post(
        "/api/v1/projects",
        json={"title": "Security Test"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # 1. XXE DTD payload rejected
    xxe_payload = b"""<?xml version="1.0"?>
<!DOCTYPE gpx [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<gpx><trk><trkseg><trkpt lat="0.1" lon="0.2"><name>&xxe;</name></trkpt></trkseg></trk></gpx>"""
    r_xxe = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("evil.gpx", xxe_payload, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_xxe.status_code == 422
    assert "traceback" not in r_xxe.text.lower()
    assert "/etc/passwd" not in r_xxe.text

    # 2. Billion-laughs entity expansion rejected
    billion_laughs = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">
]>
<gpx><trk><trkseg><trkpt lat="0.1" lon="0.2"><name>&lol2;</name></trkpt></trkseg></trk></gpx>"""
    r_lol = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("bomb.gpx", billion_laughs, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_lol.status_code == 422

    # 3. Non-XML magic bytes rejected
    r_bin = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("fake.gpx", b"\x89PNG\r\n\x1a\nnot-xml", "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_bin.status_code == 422

    # 4. Oversized upload > 25 MiB rejected with 413
    oversized = b"<gpx>" + (b" " * (25 * 1024 * 1024 + 100)) + b"</gpx>"
    r_big = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("huge.gpx", oversized, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_big.status_code == 413

    # 5. Malformed multipart boundary returns sanitized RFC 7807 JSON without stack traces
    r_malformed = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        content=b"--broken-boundary\r\nnot valid multipart",
        headers={
            "Content-Type": "multipart/form-data; boundary=wrong-boundary",
            "X-CSRF-Token": csrf,
        },
    )
    assert r_malformed.status_code == 400
    body = r_malformed.json()
    assert body["status"] == 400
    assert body["type"] == "about:blank"
    assert "traceback" not in r_malformed.text.lower()
    assert "starlette" not in r_malformed.text.lower()


def test_gpx_upload_edge_cases_empty_binary_1point_bom_and_size_bounds(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness(rate_limit_gpx_max=30)
    h.create_user("edge@example.com")
    client, csrf = h.login_client("edge@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "GPX Edge Cases"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # 1. Empty upload -> 422
    r_empty = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("empty.gpx", b"", "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_empty.status_code == 422

    # 2. Binary garbage -> 422
    r_garbage = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("garbage.gpx", b"\x00\xff\xfe\xfd\x7fELF", "application/octet-stream")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_garbage.status_code == 422

    # 3. Valid XML prefix but broken XML / 0 trackpoints -> 422
    r_truncated = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("broken.gpx", b'<?xml version="1.0"?><gpx><trk>', "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_truncated.status_code == 422

    r_no_pts = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={
            "file": (
                "nopts.gpx",
                b'<?xml version="1.0"?><gpx version="1.1"><trk><trkseg/></trk></gpx>',
                "application/gpx+xml",
            )
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r_no_pts.status_code == 422

    # 4. Oversized upload > MAX_GPX_BYTES via streaming reader -> 413 (patched tight cap)
    one_pt_gpx = (
        b'\xef\xbb\xbf  \n<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        b"<trk><name>Solo Point</name><trkseg>"
        b'<trkpt lat="0.1000" lon="0.2000"><ele>1000.0</ele></trkpt>'
        b"</trkseg></trk></gpx>"
    )
    monkeypatch.setattr(stages_router, "MAX_GPX_BYTES", len(one_pt_gpx) - 1)
    r_over_stream = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("over.gpx", one_pt_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_over_stream.status_code == 413

    # Exact MAX_GPX_BYTES boundary + UTF-8 BOM + 1-point GPX succeeds -> 201
    monkeypatch.setattr(stages_router, "MAX_GPX_BYTES", len(one_pt_gpx))
    r_1pt = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("solo.gpx", one_pt_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_1pt.status_code == 201, r_1pt.text
    stage_1pt = r_1pt.json()
    assert stage_1pt["point_count"] == 1
    assert stage_1pt["distance_m"] == 0.0
    assert stage_1pt["has_timestamps"] is False

    # Downstream KPIs, chart, geometry, and exports handle 1-point stage cleanly
    assert client.get(f"/api/v1/projects/{proj_id}/kpis").status_code == 200
    assert client.get(f"/api/v1/projects/{proj_id}/telemetry/chart").status_code == 200
    geom_1pt = client.get(f"/api/v1/projects/{proj_id}/stages/{stage_1pt['id']}/geometry?lod=full")
    assert geom_1pt.status_code == 200
    assert len(geom_1pt.json()["geometry"]["coordinates"][0]) == 2
    gpx_1pt = client.get(f"/api/v1/projects/{proj_id}/export?format=gpx")
    assert gpx_1pt.status_code == 200
    assert parse_gpx_bytes(gpx_1pt.content).point_count == 1

    # 5. Content-Length guard unit check (bad header -> 400; > MAX + overhead -> 413)
    import asyncio
    import io

    from fastapi import HTTPException, UploadFile
    from starlette.requests import Request as StarletteRequest

    monkeypatch.undo()

    async def _check_cl_guard() -> None:
        from trip_api.db import SessionRecord, User
        from trip_api.deps import AuthContext

        bad_req = StarletteRequest(
            {
                "type": "http",
                "headers": [(b"content-length", b"not-an-int")],
                "method": "POST",
                "path": "/",
            }
        )
        over_cl = stages_router.MAX_GPX_BYTES + stages_router._MULTIPART_OVERHEAD_BYTES + 1
        big_req = StarletteRequest(
            {
                "type": "http",
                "headers": [(b"content-length", str(over_cl).encode("ascii"))],
                "method": "POST",
                "path": "/",
            }
        )
        with h.session_factory() as db:
            user = db.query(User).first()
            assert user is not None
            sess = SessionRecord(
                user_id=user.id,
                token_hash="z" * 64,
                csrf_token="z",
                expires_at=user.created_at,
            )
            auth_ctx = AuthContext(session=sess, user=user)
            dummy_upload = UploadFile(filename="a.gpx", file=io.BytesIO(b""))

            with pytest.raises(HTTPException) as bad_exc:
                await stages_router.upload_gpx_stage(
                    project_id=uuid.UUID(proj_id),
                    request=bad_req,
                    file=dummy_upload,
                    auth=auth_ctx,
                    db=db,
                    settings=h.settings,
                    store=h.store,
                )
            assert bad_exc.value.status_code == 400

            with pytest.raises(HTTPException) as big_exc:
                await stages_router.upload_gpx_stage(
                    project_id=uuid.UUID(proj_id),
                    request=big_req,
                    file=dummy_upload,
                    auth=auth_ctx,
                    db=db,
                    settings=h.settings,
                    store=h.store,
                )
            assert big_exc.value.status_code == 413

    asyncio.run(_check_cl_guard())


def test_whitespace_only_titles_and_stage_names_rejected_and_trimmed(
    harness: AppHarness, sample_gpx: bytes
) -> None:
    harness.create_user("ws@example.com")
    client, csrf = harness.login_client("ws@example.com")

    for bad_title in ("", "   ", "\t\n "):
        resp = client.post(
            "/api/v1/projects",
            json={"title": bad_title},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 422

    created = client.post(
        "/api/v1/projects",
        json={"title": "  Fictional Tour  "},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201
    proj_id = created.json()["id"]
    assert created.json()["title"] == "Fictional Tour"

    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}",
            json={"revision": 1, "title": "   \t  "},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )

    stage_id = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("stage.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{stage_id}",
            json={"name": "   \n "},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )

    renamed = client.patch(
        f"/api/v1/projects/{proj_id}/stages/{stage_id}",
        json={"name": "  Passo Pordoi  "},
        headers={"X-CSRF-Token": csrf},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Passo Pordoi"


def test_stage_trim_validation_and_geojson_export_accuracy(
    harness: AppHarness, sample_gpx: bytes
) -> None:
    harness.create_user("trim@example.com")
    client, csrf = harness.login_client("trim@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Trim Validation Tour"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # 1. GPX track without <time> tags cannot be trimmed
    no_time_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Untimed</name><trkseg>
    <trkpt lat="0.1000" lon="0.2000"><ele>1000</ele></trkpt>
    <trkpt lat="0.1010" lon="0.2010"><ele>1010</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    untimed_stage = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("untimed.gpx", no_time_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert untimed_stage["has_timestamps"] is False

    r_untimed = client.patch(
        f"/api/v1/projects/{proj_id}/stages/{untimed_stage['id']}",
        json={"trim_start_s": 0.0, "trim_end_s": 10.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert r_untimed.status_code == 422
    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{untimed_stage['id']}",
            json={"trim_start_s": 5.0},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )

    # 2. Timestamped GPX stage (0s, 60s, 120s, 180s -> elapsed_seconds=180.0)
    timed_stage = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("timed.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()
    sid = timed_stage["id"]
    lod_key = stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(sid))
    assert harness.store.resolve_existing_path(lod_key).is_file()

    # Exceeds bounds completely
    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{sid}",
            json={"trim_start_s": 500.0, "trim_end_s": 600.0},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )
    # trim_end_s exceeds stage.elapsed_seconds
    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{sid}",
            json={"trim_start_s": 0.0, "trim_end_s": 250.0},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )
    # Within [0, 180] but leaves 0 points (between 60s and 120s)
    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{sid}",
            json={"trim_start_s": 65.0, "trim_end_s": 115.0},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )

    # Failed trims did NOT persist or destroy LOD cache or break downstream endpoints
    assert harness.store.resolve_existing_path(lod_key).is_file()
    detail = client.get(f"/api/v1/projects/{proj_id}").json()
    timed_after_bad = next(s for s in detail["stages"] if s["id"] == sid)
    assert timed_after_bad["trim_start_s"] is None
    assert timed_after_bad["trim_end_s"] is None
    assert client.get(f"/api/v1/projects/{proj_id}/telemetry/geometry").status_code == 200
    assert client.get(f"/api/v1/projects/{proj_id}/kpis").status_code == 200

    # Delete untimed stage so we test clean GeoJSON export on trimmed stage
    assert (
        client.delete(
            f"/api/v1/projects/{proj_id}/stages/{untimed_stage['id']}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 204
    )

    # Valid trim [60.0, 120.0] leaves 2 points
    trimmed = client.patch(
        f"/api/v1/projects/{proj_id}/stages/{sid}",
        json={"trim_start_s": 60.0, "trim_end_s": 120.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert trimmed.status_code == 200

    kpis = client.get(f"/api/v1/projects/{proj_id}/kpis").json()["stages"][0]
    geojson = client.get(f"/api/v1/projects/{proj_id}/export?format=geojson").json()
    feat = geojson["features"][0]
    assert len(feat["geometry"]["coordinates"][0]) == 2
    assert feat["properties"]["distance_m"] == pytest.approx(kpis["total_distance_m"])
    assert feat["properties"]["distance_m"] < timed_stage["distance_m"]
    assert feat["properties"]["elevation_gain_m"] == pytest.approx(kpis["elevation_gain_m"])

    # Trim down to a single point [60.0, 61.0] -> GeoJSON duplicates point per RFC 7946 §3.1.4
    one_pt_trim = client.patch(
        f"/api/v1/projects/{proj_id}/stages/{sid}",
        json={"trim_start_s": 60.0, "trim_end_s": 61.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert one_pt_trim.status_code == 200
    geojson_1pt = client.get(f"/api/v1/projects/{proj_id}/export?format=geojson").json()
    coords_1pt = geojson_1pt["features"][0]["geometry"]["coordinates"][0]
    assert len(coords_1pt) == 2
    assert coords_1pt[0] == coords_1pt[1]
    assert geojson_1pt["features"][0]["properties"]["distance_m"] == 0.0

    # Clearing trim restores full untrimmed GeoJSON export
    cleared = client.patch(
        f"/api/v1/projects/{proj_id}/stages/{sid}",
        json={"trim_start_s": None, "trim_end_s": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert cleared.status_code == 200
    geojson_full = client.get(f"/api/v1/projects/{proj_id}/export?format=geojson").json()
    assert len(geojson_full["features"][0]["geometry"]["coordinates"][0]) == 4
    assert geojson_full["features"][0]["properties"]["distance_m"] == pytest.approx(
        timed_stage["distance_m"]
    )

    # Also test standalone 1-point GPX export duplicates coordinates per RFC 7946 §3.1.4
    one_pt_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Single Point</name><trkseg>
    <trkpt lat="0.1000" lon="0.2000"><ele>1000</ele></trkpt>
  </trkseg></trk>
</gpx>"""
    client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("single.gpx", one_pt_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    geojson_both = client.get(f"/api/v1/projects/{proj_id}/export?format=geojson").json()
    single_feat = geojson_both["features"][1]
    assert single_feat["geometry"]["type"] == "MultiLineString"
    assert len(single_feat["geometry"]["coordinates"][0]) == 2
    assert (
        single_feat["geometry"]["coordinates"][0][0] == single_feat["geometry"]["coordinates"][0][1]
    )


def test_trim_export_reimport_roundtrip_and_multisegment_gpx(
    harness: AppHarness,
) -> None:
    harness.create_user("roundtrip@example.com")
    user_client, csrf = harness.login_client("roundtrip@example.com")

    five_pt_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Synthetic Pass Hike</name>
    <type>hiking</type>
    <trkseg>
      <trkpt lat="0.1000" lon="0.2000"><ele>1000.0</ele><time>2000-01-02T08:00:00Z</time></trkpt>
      <trkpt lat="0.1020" lon="0.2020"><ele>1020.0</ele><time>2000-01-02T08:01:00Z</time></trkpt>
      <trkpt lat="0.1040" lon="0.2040"><ele>1055.0</ele><time>2000-01-02T08:02:00Z</time></trkpt>
      <trkpt lat="0.1060" lon="0.2060"><ele>1080.0</ele><time>2000-01-02T08:03:00Z</time></trkpt>
      <trkpt lat="0.1080" lon="0.2080"><ele>1100.0</ele><time>2000-01-02T08:04:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>"""

    p1 = user_client.post(
        "/api/v1/projects",
        json={"title": "Source Roundtrip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    st = user_client.post(
        f"/api/v1/projects/{p1}/gpx",
        files={"file": ("synthetic-pass.gpx", five_pt_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()
    sid = st["id"]
    assert st["point_count"] == 5
    assert st["activity_type"] == "hiking"

    # Trim middle window [60.0, 180.0] -> keeps 3 points (elev 1020, 1055, 1080; gain = 60.0)
    trimmed = user_client.patch(
        f"/api/v1/projects/{p1}/stages/{sid}",
        json={"trim_start_s": 60.0, "trim_end_s": 180.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert trimmed.status_code == 200

    kpis = user_client.get(f"/api/v1/projects/{p1}/kpis").json()
    assert kpis["stages"][0]["point_count"] == 3
    assert kpis["total_elevation_gain_m"] == pytest.approx(60.0)
    trimmed_dist = kpis["total_distance_m"]
    assert 0.0 < trimmed_dist < st["distance_m"]

    chart = user_client.get(
        f"/api/v1/projects/{p1}/telemetry/chart?stage_id={sid}&max_points=100"
    ).json()
    assert chart["total_distance_m"] == pytest.approx(trimmed_dist)
    assert len(chart["points"]) == 3

    geom = user_client.get(
        f"/api/v1/projects/{p1}/telemetry/geometry?stage_id={sid}&lod=full"
    ).json()
    assert len(geom["features"][0]["geometry"]["coordinates"][0]) == 3
    assert geom["features"][0]["properties"]["distance_m"] == pytest.approx(trimmed_dist, abs=0.01)

    stage_geom = user_client.get(f"/api/v1/projects/{p1}/stages/{sid}/geometry?lod=full").json()
    assert len(stage_geom["geometry"]["coordinates"][0]) == 3
    assert stage_geom["properties"]["elevation_gain_m"] == pytest.approx(60.0)

    geojson_exp = user_client.get(f"/api/v1/projects/{p1}/export?format=geojson").json()
    assert len(geojson_exp["features"][0]["geometry"]["coordinates"][0]) == 3
    assert geojson_exp["features"][0]["properties"]["distance_m"] == pytest.approx(
        trimmed_dist, abs=0.01
    )
    assert geojson_exp["features"][0]["properties"]["elevation_gain_m"] == pytest.approx(60.0)

    gpx_exp = user_client.get(f"/api/v1/projects/{p1}/export?format=gpx")
    assert gpx_exp.status_code == 200
    gpx_bytes = gpx_exp.content
    assert b"<type>hiking</type>" in gpx_bytes
    assert b"2000-01-02T08:00:00Z" not in gpx_bytes
    assert b"2000-01-02T08:04:00Z" not in gpx_bytes
    assert b"2000-01-02T08:01:00Z" in gpx_bytes
    assert b"2000-01-02T08:03:00Z" in gpx_bytes

    # Re-import exported GPX into a fresh project and verify telemetry parity
    p2 = user_client.post(
        "/api/v1/projects",
        json={"title": "Reimported Roundtrip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    reimported = user_client.post(
        f"/api/v1/projects/{p2}/gpx",
        files={"file": ("reimport.gpx", gpx_bytes, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert reimported.status_code == 201, reimported.text
    re_stage = reimported.json()
    assert re_stage["point_count"] == 3
    assert re_stage["activity_type"] == "hiking"
    assert re_stage["elevation_gain_m"] == pytest.approx(60.0, abs=0.1)
    assert re_stage["distance_m"] == pytest.approx(trimmed_dist, rel=1e-4)
    assert re_stage["elapsed_seconds"] == pytest.approx(120.0)

    # Multi-segment GPX export preserves separate <trkseg> blocks and MultiLineString lines
    multi_seg_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Two Segments</name>
    <trkseg>
      <trkpt lat="0.100" lon="0.200"><ele>1000</ele></trkpt>
      <trkpt lat="0.101" lon="0.201"><ele>1010</ele></trkpt>
    </trkseg>
    <trkseg>
      <trkpt lat="0.110" lon="0.210"><ele>1020</ele></trkpt>
      <trkpt lat="0.111" lon="0.211"><ele>1035</ele></trkpt>
    </trkseg>
  </trk>
</gpx>"""
    p3 = user_client.post(
        "/api/v1/projects",
        json={"title": "MultiSeg"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    user_client.post(
        f"/api/v1/projects/{p3}/gpx",
        files={"file": ("multiseg.gpx", multi_seg_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    ms_geojson = user_client.get(f"/api/v1/projects/{p3}/export?format=geojson").json()
    assert ms_geojson["features"][0]["geometry"]["type"] == "MultiLineString"
    assert len(ms_geojson["features"][0]["geometry"]["coordinates"]) == 2

    ms_gpx = user_client.get(f"/api/v1/projects/{p3}/export?format=gpx").content
    assert ms_gpx.count(b"<trkseg>") == 2
    assert parse_gpx_bytes(ms_gpx).point_count == 4


def test_stage_reorder_delete_project_list_and_cascade_cleanup(
    harness: AppHarness,
    sample_gpx: bytes,
) -> None:
    harness.create_user("cascade@example.com")
    client, csrf = harness.login_client("cascade@example.com")
    me_id = uuid.UUID(client.get("/api/v1/auth/me").json()["user"]["id"])

    # Export on 0-stage project fails with 422
    empty_proj = client.post(
        "/api/v1/projects",
        json={"title": "Empty Export"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    assert client.get(f"/api/v1/projects/{empty_proj}/export?format=gpx").status_code == 422

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Three Stage Tour"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # List projects returns both in updated_at desc order
    listed = client.get("/api/v1/projects").json()
    assert len(listed) == 2
    assert listed[0]["id"] == proj_id

    stages = []
    for idx in range(3):
        s = client.post(
            f"/api/v1/projects/{proj_id}/gpx",
            files={"file": (f"s{idx}.gpx", sample_gpx, "application/gpx+xml")},
            data={"day_index": str(idx + 1), "name": f"Stage {idx}"},
            headers={"X-CSRF-Token": csrf},
        ).json()
        stages.append(s)

    s0, s1, s2 = stages
    client.patch(
        f"/api/v1/projects/{proj_id}",
        json={
            "revision": 1,
            "settings": {
                "stage_colors": {s0["id"]: "#111111", s1["id"]: "#222222", s2["id"]: "#333333"},
                "stage_transitions": {s0["id"]: "bed", s1["id"]: "none", s2["id"]: "bed"},
            },
        },
        headers={"X-CSRF-Token": csrf},
    )

    # Reorder edge cases: duplicate IDs, invalid day_index, foreign stage ID -> 422
    assert (
        client.post(
            f"/api/v1/projects/{proj_id}/stages/reorder",
            json={"stage_ids": [s0["id"], s0["id"], s1["id"]]},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/projects/{proj_id}/stages/reorder",
            json={
                "stage_ids": [s0["id"], s1["id"], s2["id"]],
                "day_indices": {s0["id"]: 0},
            },
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/projects/{proj_id}/stages/reorder",
            json={"stage_ids": []},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )

    # Patch day_index and missing stage -> 404
    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{s1['id']}",
            json={"day_index": 7},
            headers={"X-CSRF-Token": csrf},
        ).json()["day_index"]
        == 7
    )
    missing_sid = str(uuid.uuid4())
    assert (
        client.patch(
            f"/api/v1/projects/{proj_id}/stages/{missing_sid}",
            json={"name": "Missing"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/projects/{proj_id}/stages/{missing_sid}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 404
    )

    # Delete middle stage s1: removes its blobs, cleans settings, compacts order [0, 1]
    from trip_api.db import Stage as StageModel

    with harness.session_factory() as db:
        s1_row = db.get(StageModel, uuid.UUID(s1["id"]))
        s2_row = db.get(StageModel, uuid.UUID(s2["id"]))
        assert s1_row is not None and s2_row is not None
        s1_keys = [
            s1_row.bronze_object_key,
            s1_row.silver_object_key,
            stage_lod_cache_key(uuid.UUID(proj_id), s1_row.id),
        ]
        s2_keys = [
            s2_row.bronze_object_key,
            s2_row.silver_object_key,
            stage_lod_cache_key(uuid.UUID(proj_id), s2_row.id),
        ]

    assert (
        client.delete(
            f"/api/v1/projects/{proj_id}/stages/{s1['id']}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 204
    )
    for k in s1_keys:
        with pytest.raises(FileNotFoundError):
            harness.store.resolve_existing_path(k)

    detail = client.get(f"/api/v1/projects/{proj_id}").json()
    assert [st["id"] for st in detail["stages"]] == [s0["id"], s2["id"]]
    assert [st["stage_order"] for st in detail["stages"]] == [0, 1]
    assert s1["id"] not in detail["settings"]["stage_colors"]
    assert s1["id"] not in detail["settings"]["stage_transitions"]

    # Seed a render job on the project and delete the entire project
    job_id = uuid.uuid4()
    mp4_key = f"outputs/{job_id}.mp4"
    thumb_key = f"outputs/{job_id}.webp"
    harness.store.put_bytes(mp4_key, b"MP4")
    harness.store.put_bytes(thumb_key, b"WEBP")
    with harness.session_factory() as db:
        db.add(
            RenderJob(
                id=job_id,
                project_id=uuid.UUID(proj_id),
                owner_user_id=me_id,
                idempotency_key="cascade-del-1",
                idempotency_hash="c" * 64,
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
                output_object_key=mp4_key,
                thumbnail_key=thumb_key,
            )
        )
        db.commit()

    assert (
        client.delete(f"/api/v1/projects/{proj_id}", headers={"X-CSRF-Token": csrf}).status_code
        == 204
    )
    assert client.get(f"/api/v1/projects/{proj_id}").status_code == 404
    for k in [*s2_keys, mp4_key, thumb_key]:
        with pytest.raises(FileNotFoundError):
            harness.store.resolve_existing_path(k)


def test_telemetry_missing_stage_and_corrupt_parquet_errors(
    harness: AppHarness,
    sample_gpx: bytes,
) -> None:
    harness.create_user("telem@example.com")
    client, csrf = harness.login_client("telem@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Telemetry Errors"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    stage_id = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    unknown_stage_id = str(uuid.uuid4())
    assert (
        client.get(
            f"/api/v1/projects/{proj_id}/telemetry/chart?stage_id={unknown_stage_id}"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/projects/{proj_id}/telemetry/geometry?stage_id={unknown_stage_id}"
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/projects/{proj_id}/stages/{unknown_stage_id}/geometry").status_code
        == 404
    )

    # Corrupt Silver Parquet + delete LOD cache -> sanitized 500 without stack trace
    from fastapi.testclient import TestClient
    from trip_api.db import Stage as StageModel

    with harness.session_factory() as db:
        st = db.get(StageModel, uuid.UUID(stage_id))
        assert st is not None
        silver_key = st.silver_object_key
    harness.store.delete(stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(stage_id)))
    harness.store.delete(stage_chart_cache_key(uuid.UUID(proj_id), uuid.UUID(stage_id)))
    harness.store.put_bytes(silver_key, b"NOT-A-PARQUET-FILE")

    safe_client = TestClient(
        harness.app, raise_server_exceptions=False, cookies=dict(client.cookies)
    )
    err_resp = safe_client.get(f"/api/v1/projects/{proj_id}/telemetry/chart")
    assert err_resp.status_code == 500
    assert err_resp.json()["title"] == "Internal server error"
    assert "traceback" not in err_resp.text.lower()


def test_stage_camera_overrides_and_climb_callout_duration_api_lifecycle(
    harness: AppHarness,
    sample_gpx: bytes,
) -> None:
    harness.create_user("camera@example.com")
    client, csrf = harness.login_client("camera@example.com")

    proj_resp = client.post(
        "/api/v1/projects",
        json={"title": "Camera Focus Tour"},
        headers={"X-CSRF-Token": csrf},
    )
    assert proj_resp.status_code == 201
    proj_id = proj_resp.json()["id"]

    stage1 = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("day1.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "1", "name": "Stage One"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    stage2 = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("day2.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "2", "name": "Stage Two"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    s1_id = stage1["id"]
    s2_id = stage2["id"]
    foreign_stage_id = str(uuid.uuid4())

    # PATCH rejects foreign stage UUID in stage_camera_overrides with HTTP 422
    bad_patch = client.patch(
        f"/api/v1/projects/{proj_id}",
        json={
            "revision": 1,
            "settings": {
                "stage_camera_overrides": {
                    foreign_stage_id: {"focus_mode": "street"},
                },
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_patch.status_code == 422
    assert "stage_camera_overrides" in bad_patch.text

    # POST /renders rejects foreign stage UUID in stage_camera_overrides with HTTP 422
    bad_render = client.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={
            "stage_camera_overrides": {
                foreign_stage_id: {"focus_mode": "close"},
            },
        },
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "render-foreign-override-1",
        },
    )
    assert bad_render.status_code == 422
    assert "stage_camera_overrides" in bad_render.text

    # PATCH accepts valid project stage IDs and applies preset defaults + climb_callout_duration_s
    good_patch = client.patch(
        f"/api/v1/projects/{proj_id}",
        json={
            "revision": 1,
            "settings": {
                "climb_callout_duration_s": 6.5,
                "stage_camera_overrides": {
                    s1_id: {"focus_mode": "street"},
                    s2_id: {
                        "focus_mode": "custom",
                        "target_zoom": 14.5,
                        "speed_multiplier": 0.6,
                        "transition_margin": 0.04,
                    },
                },
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert good_patch.status_code == 200, good_patch.text
    patched_settings = good_patch.json()["settings"]
    assert patched_settings["climb_callout_duration_s"] == pytest.approx(6.5)
    assert patched_settings["stage_camera_overrides"][s1_id] == {
        "focus_mode": "street",
        "target_zoom": 15.8,
        "speed_multiplier": 0.2,
        "transition_margin": 0.02,
    }
    assert patched_settings["stage_camera_overrides"][s2_id] == {
        "focus_mode": "custom",
        "target_zoom": 14.5,
        "speed_multiplier": 0.6,
        "transition_margin": 0.04,
    }

    # Lowered speed_multiplier bounds: 0.05 and 3.0 accepted; 0.04 and 3.01 rejected
    for bad_speed in (0.04, 3.01):
        bad_speed_resp = client.patch(
            f"/api/v1/projects/{proj_id}",
            json={
                "revision": 2,
                "settings": {
                    "stage_camera_overrides": {
                        s1_id: {"focus_mode": "custom", "speed_multiplier": bad_speed},
                    },
                },
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert bad_speed_resp.status_code == 422

    bounds_patch = client.patch(
        f"/api/v1/projects/{proj_id}",
        json={
            "revision": 2,
            "settings": {
                "stage_camera_overrides": {
                    s1_id: {"focus_mode": "custom", "speed_multiplier": 0.05},
                    s2_id: {"focus_mode": "custom", "speed_multiplier": 3.0},
                },
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert bounds_patch.status_code == 200, bounds_patch.text
    assert bounds_patch.json()["settings"]["stage_camera_overrides"][s1_id][
        "speed_multiplier"
    ] == pytest.approx(0.05)
    assert bounds_patch.json()["settings"]["stage_camera_overrides"][s2_id][
        "speed_multiplier"
    ] == pytest.approx(3.0)

    # POST /renders accepts valid project stage IDs in stage_camera_overrides
    good_render = client.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={
            "climb_callout_duration_s": 0.0,
            "stage_camera_overrides": {
                s1_id: {"focus_mode": "close"},
            },
        },
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "render-valid-override-1",
        },
    )
    assert good_render.status_code == 202, good_render.text
    render_spec = good_render.json()["spec"]
    assert render_spec["climb_callout_duration_s"] == 0.0
    assert render_spec["stage_camera_overrides"][s1_id]["target_zoom"] == pytest.approx(13.8)
    assert render_spec["stage_camera_overrides"][s1_id]["speed_multiplier"] == pytest.approx(0.7)

    # DELETE stage prunes its stage_camera_overrides entry while keeping other stages intact
    del_resp = client.delete(
        f"/api/v1/projects/{proj_id}/stages/{s1_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert del_resp.status_code == 204
    detail = client.get(f"/api/v1/projects/{proj_id}").json()
    remaining_overrides = detail["settings"]["stage_camera_overrides"]
    assert s1_id not in remaining_overrides
    assert s2_id in remaining_overrides
    assert remaining_overrides[s2_id]["focus_mode"] == "custom"


def test_stage_split_api_lifecycle_and_shared_bronze_deletion(
    harness: AppHarness,
    sample_gpx: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.create_user("split-owner@example.com")
    harness.create_user("split-other@example.com")
    client, csrf = harness.login_client("split-owner@example.com")
    other_client, other_csrf = harness.login_client("split-other@example.com")

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Split Lifecycle Tour"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    eight_pt_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Long Pass</name>
    <type>cycling</type>
    <trkseg>
      <trkpt lat="0.1000" lon="0.2000"><ele>1000.0</ele><time>2001-06-01T08:00:00Z</time></trkpt>
      <trkpt lat="0.1010" lon="0.2010"><ele>1020.0</ele><time>2001-06-01T08:01:00Z</time></trkpt>
      <trkpt lat="0.1020" lon="0.2020"><ele>1040.0</ele><time>2001-06-01T08:02:00Z</time></trkpt>
      <trkpt lat="0.1030" lon="0.2030"><ele>1060.0</ele><time>2001-06-01T08:03:00Z</time></trkpt>
      <trkpt lat="0.1040" lon="0.2040"><ele>1080.0</ele><time>2001-06-01T08:04:00Z</time></trkpt>
      <trkpt lat="0.1050" lon="0.2050"><ele>1100.0</ele><time>2001-06-01T08:05:00Z</time></trkpt>
      <trkpt lat="0.1060" lon="0.2060"><ele>1120.0</ele><time>2001-06-01T08:06:00Z</time></trkpt>
      <trkpt lat="0.1070" lon="0.2070"><ele>1140.0</ele><time>2001-06-01T08:07:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>"""

    untimed_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Untimed Track</name><trkseg>
    <trkpt lat="0.1000" lon="0.2000"><ele>900.0</ele></trkpt>
    <trkpt lat="0.1010" lon="0.2010"><ele>910.0</ele></trkpt>
    <trkpt lat="0.1020" lon="0.2020"><ele>920.0</ele></trkpt>
    <trkpt lat="0.1030" lon="0.2030"><ele>930.0</ele></trkpt>
  </trkseg></trk>
</gpx>"""

    s0 = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s0.gpx", sample_gpx, "application/gpx+xml")},
        data={"day_index": "1", "name": "Warmup"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    s1 = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s1.gpx", eight_pt_gpx, "application/gpx+xml")},
        data={"day_index": "2", "name": "Mountain Pass"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    s2 = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s2.gpx", untimed_gpx, "application/gpx+xml")},
        data={"day_index": "3", "name": "Finale"},
        headers={"X-CSRF-Token": csrf},
    ).json()

    # Attach settings and strava/external metadata to s1 to verify preservation vs Part 2 clearing
    patch_resp = client.patch(
        f"/api/v1/projects/{proj_id}",
        json={
            "revision": 1,
            "settings": {
                "stage_colors": {s1["id"]: "#FF5500"},
                "stage_camera_overrides": {s1["id"]: {"focus_mode": "street"}},
            },
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["revision"] == 2

    with harness.session_factory() as db:
        s1_db = db.get(Stage, uuid.UUID(s1["id"]))
        assert s1_db is not None
        s1_db.strava_activity_id = 987654321
        s1_db.external_source_id = "strava:987654321"
        db.commit()
        bronze_path = s1_db.bronze_object_key
        orig_s1_silver_key = s1_db.silver_object_key

    # Security / error checks: missing CSRF -> 403, BOLA -> 404, unknown stage -> 404
    assert (
        client.post(
            f"/api/v1/projects/{proj_id}/stages/{s1['id']}/split",
            json={"split_distance_m": 200.0},
        ).status_code
        == 403
    )
    assert (
        other_client.post(
            f"/api/v1/projects/{proj_id}/stages/{s1['id']}/split",
            json={"split_distance_m": 200.0},
            headers={"X-CSRF-Token": other_csrf},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/projects/{proj_id}/stages/{uuid.uuid4()}/split",
            json={"split_distance_m": 200.0},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 404
    )

    # Validation 422s: neither axis, both axes, non-positive, leaving <2 pts, untimed elapsed
    for bad_payload in (
        {},
        {"split_distance_m": 200.0, "split_elapsed_s": 120.0},
        {"split_distance_m": 0.0},
        {"split_elapsed_s": -5.0},
        {"split_distance_m": 1.0},
        {"split_distance_m": s1["distance_m"] * 2.0},
    ):
        assert (
            client.post(
                f"/api/v1/projects/{proj_id}/stages/{s1['id']}/split",
                json=bad_payload,
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 422
        )
    assert (
        client.post(
            f"/api/v1/projects/{proj_id}/stages/{s2['id']}/split",
            json={"split_elapsed_s": 30.0},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 422
    )

    # Split middle stage s1 by distance with default names
    split1 = client.post(
        f"/api/v1/projects/{proj_id}/stages/{s1['id']}/split",
        json={"split_distance_m": s1["distance_m"] / 2.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert split1.status_code == 200, split1.text
    body1 = split1.json()
    p1 = body1["part1"]
    p2 = body1["part2"]
    ordered1 = body1["stages"]

    assert body1["revision"] == 3
    assert p1["id"] == s1["id"]
    assert p2["id"] != s1["id"]
    assert p1["name"] == "Mountain Pass (Part 1)"
    assert p2["name"] == "Mountain Pass (Part 2)"
    assert p1["stage_order"] == 1
    assert p2["stage_order"] == 2
    assert p1["day_index"] == 2
    assert p2["day_index"] == 2
    assert p1["point_count"] == 4
    assert p2["point_count"] == 4
    assert p1["strava_activity_id"] == 987654321
    assert p1["external_source_id"] == "strava:987654321"
    assert p2["strava_activity_id"] is None
    assert p2["external_source_id"] is None
    assert p1["trim_start_s"] is None and p1["trim_end_s"] is None
    assert p2["trim_start_s"] is None and p2["trim_end_s"] is None
    assert [st["id"] for st in ordered1] == [s0["id"], p1["id"], p2["id"], s2["id"]]
    assert [st["stage_order"] for st in ordered1] == [0, 1, 2, 3]

    # Verify settings preserved for Part 1, ObjectStore keys updated, and old Silver key deleted
    detail1 = client.get(f"/api/v1/projects/{proj_id}").json()
    assert detail1["settings"]["stage_colors"][p1["id"]] == "#FF5500"
    assert detail1["settings"]["stage_camera_overrides"][p1["id"]]["focus_mode"] == "street"

    with harness.session_factory() as db:
        p1_db = db.get(Stage, uuid.UUID(p1["id"]))
        p2_db = db.get(Stage, uuid.UUID(p2["id"]))
        assert p1_db is not None and p2_db is not None
        assert p1_db.bronze_object_key == bronze_path
        assert p2_db.bronze_object_key == bronze_path
        assert p1_db.silver_object_key != orig_s1_silver_key
        assert p2_db.silver_object_key != p1_db.silver_object_key
        p1_silver_key = p1_db.silver_object_key
        p2_silver_key = p2_db.silver_object_key

    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path(orig_s1_silver_key)
    for expected_key in (
        bronze_path,
        p1_silver_key,
        stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(p1["id"])),
        stage_chart_cache_key(uuid.UUID(proj_id), uuid.UUID(p1["id"])),
        stage_events_cache_key(uuid.UUID(proj_id), uuid.UUID(p1["id"])),
        p2_silver_key,
        stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(p2["id"])),
        stage_chart_cache_key(uuid.UUID(proj_id), uuid.UUID(p2["id"])),
        stage_events_cache_key(uuid.UUID(proj_id), uuid.UUID(p2["id"])),
    ):
        assert harness.store.resolve_existing_path(expected_key).is_file()

    # Downstream telemetry endpoints reflect all 4 stages cleanly
    assert client.get(f"/api/v1/projects/{proj_id}/kpis").json()["stage_count"] == 4
    geom = client.get(f"/api/v1/projects/{proj_id}/telemetry/geometry?lod=full").json()
    assert len(geom["features"]) == 4
    assert client.get(f"/api/v1/projects/{proj_id}/telemetry/chart").status_code == 200
    assert client.get(f"/api/v1/projects/{proj_id}/export?format=gpx").status_code == 200
    assert client.get(f"/api/v1/projects/{proj_id}/export?format=geojson").status_code == 200

    # Audit event recorded
    audit_events = client.get(f"/api/v1/projects/{proj_id}/audit").json()
    split_events = [e for e in audit_events if e["event_type"] == "stage.split"]
    assert len(split_events) == 1
    assert split_events[0]["metadata"]["part2_id"] == p2["id"]

    # Split Part 2 by elapsed_s with custom names
    split2 = client.post(
        f"/api/v1/projects/{proj_id}/stages/{p2['id']}/split",
        json={
            "split_elapsed_s": 60.0,
            "part1_name": "  Upper Ascent  ",
            "part2_name": "  Summit Push  ",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert split2.status_code == 200, split2.text
    body2 = split2.json()
    assert body2["revision"] == 4
    assert body2["part1"]["name"] == "Upper Ascent"
    assert body2["part2"]["name"] == "Summit Push"
    assert body2["part1"]["point_count"] == 2
    assert body2["part2"]["point_count"] == 2
    p2b = body2["part2"]

    # Shared bronze_object_key deletion safety:
    # Deleting Part 1 keeps shared bronze blob while Part 2 and Part 2b still reference it
    assert (
        client.delete(
            f"/api/v1/projects/{proj_id}/stages/{p1['id']}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 204
    )
    assert harness.store.resolve_existing_path(bronze_path).is_file()
    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path(p1_silver_key)

    # Deleting Part 2 still keeps shared bronze blob because Part 2b still references it
    assert (
        client.delete(
            f"/api/v1/projects/{proj_id}/stages/{p2['id']}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 204
    )
    assert harness.store.resolve_existing_path(bronze_path).is_file()

    # Deleting Part 2b (last reference) finally deletes the shared bronze blob
    assert (
        client.delete(
            f"/api/v1/projects/{proj_id}/stages/{p2b['id']}",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 204
    )
    with pytest.raises(FileNotFoundError):
        harness.store.resolve_existing_path(bronze_path)

    # DB failure during split rolls back newly uploaded blobs and leaves Part 1 sidecars untouched
    s0_lod_key = stage_lod_cache_key(uuid.UUID(proj_id), uuid.UUID(s0["id"]))
    orig_s0_lod_bytes = harness.store.get_bytes(s0_lod_key)

    def _boom_audit(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(stages_router, "write_audit_event", _boom_audit)
    from fastapi.testclient import TestClient

    safe_client = TestClient(
        harness.app, raise_server_exceptions=False, cookies=dict(client.cookies)
    )
    fail_resp = safe_client.post(
        f"/api/v1/projects/{proj_id}/stages/{s0['id']}/split",
        json={"split_elapsed_s": 60.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert fail_resp.status_code == 500
    assert harness.store.get_bytes(s0_lod_key) == orig_s0_lod_bytes
    # Stage count unchanged (still s0 and s2)
    after_fail = client.get(f"/api/v1/projects/{proj_id}").json()
    assert [st["id"] for st in after_fail["stages"]] == [s0["id"], s2["id"]]


def test_stage_split_concurrent_change_returns_409_and_strava_delete_keeps_shared_bronze(
    harness: AppHarness,
    sample_gpx: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trip_api.ingest import _delete_stage_and_artifacts
    from trip_api.services import prepare_split_stage_artifacts as orig_prepare_split

    harness.create_user("split-race@example.com")
    client, csrf = harness.login_client("split-race@example.com")

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Split Race & Shared Bronze"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    six_pt_gpx = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Race Track</name><trkseg>
    <trkpt lat="0.1000" lon="0.2000"><ele>1000.0</ele><time>2001-06-02T08:00:00Z</time></trkpt>
    <trkpt lat="0.1010" lon="0.2010"><ele>1010.0</ele><time>2001-06-02T08:01:00Z</time></trkpt>
    <trkpt lat="0.1020" lon="0.2020"><ele>1020.0</ele><time>2001-06-02T08:02:00Z</time></trkpt>
    <trkpt lat="0.1030" lon="0.2030"><ele>1030.0</ele><time>2001-06-02T08:03:00Z</time></trkpt>
    <trkpt lat="0.1040" lon="0.2040"><ele>1040.0</ele><time>2001-06-02T08:04:00Z</time></trkpt>
    <trkpt lat="0.1050" lon="0.2050"><ele>1050.0</ele><time>2001-06-02T08:05:00Z</time></trkpt>
  </trkseg></trk>
</gpx>"""
    st = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("race.gpx", six_pt_gpx, "application/gpx+xml")},
        data={"day_index": "1", "name": "Race Track"},
        headers={"X-CSRF-Token": csrf},
    ).json()

    # Simulate a concurrent telemetry mutation (e.g., trim or split) between prepare and lock
    def _prepare_with_concurrent_trim(**kwargs: object) -> object:
        prepared = orig_prepare_split(**kwargs)  # type: ignore[arg-type]
        with harness.session_factory() as db2:
            row = db2.get(Stage, uuid.UUID(st["id"]))
            assert row is not None
            row.trim_start_s = 10.0
            db2.commit()
        return prepared

    monkeypatch.setattr(
        stages_router, "prepare_split_stage_artifacts", _prepare_with_concurrent_trim
    )
    conflict_resp = client.post(
        f"/api/v1/projects/{proj_id}/stages/{st['id']}/split",
        json={"split_elapsed_s": 120.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert conflict_resp.status_code == 409

    # Clear the simulated trim and verify real split succeeds while Strava delete keeps bronze
    monkeypatch.undo()
    with harness.session_factory() as db:
        row = db.get(Stage, uuid.UUID(st["id"]))
        assert row is not None
        row.trim_start_s = None
        db.commit()

    split_resp = client.post(
        f"/api/v1/projects/{proj_id}/stages/{st['id']}/split",
        json={"split_elapsed_s": 120.0},
        headers={"X-CSRF-Token": csrf},
    )
    assert split_resp.status_code == 200, split_resp.text
    p1_id = split_resp.json()["part1"]["id"]
    p2_id = split_resp.json()["part2"]["id"]

    with harness.session_factory() as db:
        p1_row = db.get(Stage, uuid.UUID(p1_id))
        p2_row = db.get(Stage, uuid.UUID(p2_id))
        assert p1_row is not None and p2_row is not None
        shared_bronze_key = p1_row.bronze_object_key
        _delete_stage_and_artifacts(db, harness.store, p1_row)
        db.commit()
        assert harness.store.resolve_existing_path(shared_bronze_key).is_file()

        _delete_stage_and_artifacts(db, harness.store, p2_row)
        db.commit()
        with pytest.raises(FileNotFoundError):
            harness.store.resolve_existing_path(shared_bronze_key)
