from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from conftest import AppHarness

_FIVE_POINT_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Timeline Ride</name>
    <type>cycling</type>
    <trkseg>
      <trkpt lat="0.1000" lon="0.2000"><ele>1000.0</ele><time>2000-01-02T08:00:00Z</time></trkpt>
      <trkpt lat="0.1020" lon="0.2020"><ele>1020.0</ele><time>2000-01-02T08:01:00Z</time></trkpt>
      <trkpt lat="0.1040" lon="0.2040"><ele>1055.0</ele><time>2000-01-02T08:02:00Z</time></trkpt>
      <trkpt lat="0.1060" lon="0.2060"><ele>1080.0</ele><time>2000-01-02T08:03:00Z</time></trkpt>
      <trkpt lat="0.1080" lon="0.2080"><ele>1100.0</ele><time>2000-01-02T08:04:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>"""


def test_telemetry_timeline_returns_real_timestamps_with_ownership(harness: AppHarness) -> None:
    harness.create_user("timeline-owner@example.com")
    harness.create_user("timeline-stranger@example.com")
    owner, owner_csrf = harness.login_client("timeline-owner@example.com")
    stranger, _ = harness.login_client("timeline-stranger@example.com")

    proj_id = owner.post(
        "/api/v1/projects",
        json={"title": "Timeline Project"},
        headers={"X-CSRF-Token": owner_csrf},
    ).json()["id"]
    stage = owner.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("timeline.gpx", _FIVE_POINT_GPX, "application/gpx+xml")},
        headers={"X-CSRF-Token": owner_csrf},
    ).json()
    stage_id = stage["id"]

    resp = owner.get(f"/api/v1/projects/{proj_id}/telemetry/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == proj_id
    assert len(body["stages"]) == 1
    timeline_stage = body["stages"][0]
    assert timeline_stage["stage_id"] == stage_id
    assert timeline_stage["stage_order"] == 0
    points = timeline_stage["points"]
    assert len(points) == 5
    assert points[0]["ts_utc"] == "2000-01-02T08:00:00Z"
    assert points[-1]["ts_utc"] == "2000-01-02T08:04:00Z"
    assert points[0]["cum_dist_m"] == 0.0
    assert points[-1]["cum_dist_m"] > 0.0
    assert {"lat", "lon", "elapsed_s", "segment_idx"} <= set(points[0])

    filtered = owner.get(
        f"/api/v1/projects/{proj_id}/telemetry/timeline?stage_id={stage_id}"
    ).json()
    assert [s["stage_id"] for s in filtered["stages"]] == [stage_id]

    assert stranger.get(f"/api/v1/projects/{proj_id}/telemetry/timeline").status_code == 404
    anonymous = TestClient(harness.app)
    assert anonymous.get(f"/api/v1/projects/{proj_id}/telemetry/timeline").status_code in (401, 403)


def test_share_timeline_is_public_and_follows_share_state(harness: AppHarness) -> None:
    harness.create_user("share-timeline@example.com")
    owner, owner_csrf = harness.login_client("share-timeline@example.com")
    proj_id = owner.post(
        "/api/v1/projects",
        json={"title": "Shared Timeline"},
        headers={"X-CSRF-Token": owner_csrf},
    ).json()["id"]
    owner.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("timeline.gpx", _FIVE_POINT_GPX, "application/gpx+xml")},
        headers={"X-CSRF-Token": owner_csrf},
    )
    slug = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": owner_csrf},
    ).json()["share_slug"]

    anon = TestClient(harness.app)
    shared = anon.get(f"/api/v1/share/{slug}/timeline")
    assert shared.status_code == 200, shared.text
    shared_points = shared.json()["stages"][0]["points"]
    assert shared_points[0]["ts_utc"] == "2000-01-02T08:00:00Z"
    assert (
        anon.get(
            f"/api/v1/share/{slug}/timeline?stage_id=00000000-0000-0000-0000-000000000000"
        ).status_code
        == 404
    )

    owner.delete(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert anon.get(f"/api/v1/share/{slug}/timeline").status_code == 404
