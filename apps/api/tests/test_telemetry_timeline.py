from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from trip_api import services
from trip_api.schemas import TelemetryTimelineStageOut
from trip_telemetry import (
    STAGE_TIMELINE_MAX_POINTS,
    ParsedTrack,
    TrackPoint,
    build_silver_dataframe,
    query_stage_timeline_points,
    write_silver_parquet,
)

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


_EPOCH_MS_0800 = int(datetime(2000, 1, 2, 8, 0, tzinfo=UTC).timestamp() * 1000)


def _upload_stage(harness: AppHarness, email: str) -> tuple[TestClient, str, str]:
    harness.create_user(email)
    owner, csrf = harness.login_client(email)
    proj_id = owner.post(
        "/api/v1/projects", json={"title": "Cache"}, headers={"X-CSRF-Token": csrf}
    ).json()["id"]
    owner.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("timeline.gpx", _FIVE_POINT_GPX, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    return owner, proj_id, csrf


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
    assert points[0]["ts_ms"] == _EPOCH_MS_0800
    assert points[-1]["ts_ms"] == _EPOCH_MS_0800 + 4 * 60_000
    assert points[0]["cum_dist_m"] == 0.0
    assert points[-1]["cum_dist_m"] > 0.0
    assert all(set(pt) == {"cum_dist_m", "ts_ms"} for pt in points)

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
    assert shared_points[0]["ts_ms"] == _EPOCH_MS_0800
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


def test_timeline_cache_hit_returns_identical_bytes_without_silver_read(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, proj_id, _ = _upload_stage(harness, "timeline-cache@example.com")
    url = f"/api/v1/projects/{proj_id}/telemetry/timeline"

    first = owner.get(url)
    assert first.status_code == 200, first.text
    assert any(".timeline.v" in str(p) for p in harness.store.root_dir.rglob("*"))

    def _fail(*_a: object, **_k: object) -> None:
        raise AssertionError("cache hit must not re-read Silver")

    monkeypatch.setattr(services, "query_stage_timeline_points", _fail)
    second = owner.get(url)
    assert second.status_code == 200
    assert second.content == first.content


def test_timeline_cache_schema_version_change_forces_rebuild(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, proj_id, _ = _upload_stage(harness, "timeline-version@example.com")
    url = f"/api/v1/projects/{proj_id}/telemetry/timeline"
    first = owner.get(url)
    calls: list[int] = []
    real = services.query_stage_timeline_points

    def _counting(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(services, "query_stage_timeline_points", _counting)
    owner.get(url)
    assert calls == []

    monkeypatch.setattr(services, "TIMELINE_CACHE_SCHEMA_VERSION", 2)
    rebuilt = owner.get(url)
    assert calls == [1]
    assert rebuilt.content == first.content
    owner.get(url)
    assert calls == [1]


def test_timeline_corrupt_cache_entry_falls_back_to_silver(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, proj_id, _ = _upload_stage(harness, "timeline-corrupt@example.com")
    url = f"/api/v1/projects/{proj_id}/telemetry/timeline"
    first = owner.get(url)
    cache_files = [p for p in harness.store.root_dir.rglob("*") if ".timeline.v" in p.name]
    assert len(cache_files) == 1
    cache_files[0].write_bytes(b"not gzip at all")

    again = owner.get(url)
    assert again.status_code == 200
    assert again.content == first.content
    assert gzip.decompress(cache_files[0].read_bytes())

    cache_files[0].unlink()
    assert owner.get(url).content == first.content


def test_timeline_payload_for_200_km_stage_with_25000_raw_points(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw_count = 25_000
    t0 = datetime(2001, 6, 1, 6, 0, 0, tzinfo=UTC)
    points = [
        TrackPoint(
            lat=0.0,
            lon=i * 200_000 / 111_320.0 / (raw_count - 1),
            elev_m=500.0 + 80.0 * ((i // 250) % 7) + (i % 11),
            ts_utc=t0 + timedelta(seconds=i * 1.2),
            segment_idx=0,
        )
        for i in range(raw_count)
    ]
    parsed = ParsedTrack(
        name="Long",
        points=points,
        point_count=raw_count,
        segment_count=1,
        has_timestamps=True,
        has_elevation=True,
    )
    df, _ = build_silver_dataframe(parsed, trip_id="t", stage_id="s")
    path = tmp_path / "long.parquet"
    write_silver_parquet(df, path)

    timeline_points = query_stage_timeline_points(path)
    body = TelemetryTimelineStageOut(
        stage_id="00000000-0000-0000-0000-000000000001",  # type: ignore[arg-type]
        stage_order=0,
        points=[{"cum_dist_m": p.cum_dist_m, "ts_ms": p.ts_ms} for p in timeline_points],  # type: ignore[misc]
    ).model_dump_json()
    size = len(body.encode("utf-8"))
    with capsys.disabled():
        print(f"\ntimeline payload: {len(timeline_points)} points, {size} bytes")

    assert timeline_points[-1].cum_dist_m == pytest.approx(200_000, rel=0.01)
    assert len(timeline_points) <= STAGE_TIMELINE_MAX_POINTS
    assert size < 150_000
