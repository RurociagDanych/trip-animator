from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from trip_api.db import VaultActivity
from trip_telemetry.tiles import encode_polyline

if TYPE_CHECKING:
    from conftest import AppHarness


def seed(
    harness: AppHarness,
    email: str,
    tracks: list[tuple[str, list[tuple[float, float]] | None, bool]],
) -> None:
    user = harness.create_user(email)
    with harness.session_factory() as db:
        for name, points, gps in tracks:
            db.add(
                VaultActivity(
                    user_id=user.id,
                    source_provider="upload",
                    external_activity_id=name,
                    name=name,
                    started_at=datetime(2025, 1, 1, tzinfo=UTC),
                    local_date=datetime(2025, 1, 1).date(),
                    has_gps=gps,
                    summary_polyline=encode_polyline(points) if points else None,
                    raw_metadata_json={"distance": 1000, "total_elevation_gain": 0},
                )
            )
        db.commit()


def test_map_is_authenticated_private_and_independent_of_list_page(harness: AppHarness) -> None:
    seed(
        harness,
        "map@example.com",
        [(f"Route {i}", [(50, 19), (51, 20)], True) for i in range(4)]
        + [("Indoor", None, False), ("Pending", None, True)],
    )
    seed(harness, "other@example.com", [("Secret", [(30, 40), (31, 41)], True)])
    assert TestClient(harness.app).get("/api/v1/vault/activities/map").status_code == 401
    client, _ = harness.login_client("map@example.com")
    listing = client.get("/api/v1/vault/activities?limit=1").json()
    response = client.get("/api/v1/vault/activities/map?limit=1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert listing["total"] == body["total"] == 6
    assert len(listing["items"]) == 1
    assert body["drawable_count"] == body["returned_count"] == 4
    assert len(body["features"]) == 4
    assert body["no_gps_count"] == body["unavailable_count"] == 1
    assert body["truncated"] is False
    assert {f["properties"]["name"] for f in body["features"]} == {f"Route {i}" for i in range(4)}
    assert body["features"][0]["geometry"]["coordinates"] == [[19, 50], [20, 51]]
    filtered = client.get("/api/v1/vault/activities/map?name=Route%202").json()
    assert filtered["total"] == filtered["returned_count"] == 1


def test_map_keeps_older_italy_route_available_beyond_default_feature_cap(
    harness: AppHarness,
) -> None:
    poland_tracks = [
        (f"Recent Poland {index:03d}", [(52, 21), (52.1, 21.1)], True) for index in range(501)
    ]
    italy_track = ("Older Italy route", [(42, 12), (42.1, 12.1)], True)
    seed(harness, "many-routes@example.com", [*poland_tracks, italy_track])

    older_than_poland = datetime(2020, 1, 1, tzinfo=UTC)
    newer_than_italy = datetime(2025, 1, 1, tzinfo=UTC)
    with harness.session_factory() as db:
        for activity in db.query(VaultActivity).filter(VaultActivity.name.like("Recent Poland %")):
            activity.started_at = newer_than_italy
        italy = db.query(VaultActivity).filter_by(name="Older Italy route").one()
        italy.started_at = older_than_poland
        italy_id = str(italy.id)
        db.commit()

    client, _ = harness.login_client("many-routes@example.com")
    unfiltered = client.get("/api/v1/vault/activities/map")
    area_filtered = client.get("/api/v1/vault/activities/map?area=11,41,13,43")
    assert unfiltered.status_code == area_filtered.status_code == 200

    area_features = area_filtered.json()["features"]
    assert [feature["properties"]["id"] for feature in area_features] == [italy_id]

    assert unfiltered.json()["next_offset"] == 500
    page = unfiltered.json()
    all_features = list(page["features"])
    while page["next_offset"] is not None:
        page = client.get(
            "/api/v1/vault/activities/map", params={"offset": page["next_offset"]}
        ).json()
        all_features.extend(page["features"])
    assert italy_id in {feature["properties"]["id"] for feature in all_features}
    assert len(all_features) == 502


def test_area_uses_segments_not_start_or_overlapping_bbox_and_shares_summary(
    harness: AppHarness,
) -> None:
    seed(
        harness,
        "area@example.com",
        [
            ("Crosses", [(0, -2), (0, 2)], True),
            ("BBox only", [(-2, 0), (0, 2)], True),
            ("Outside", [(10, 10), (11, 11)], True),
            ("Indoor", None, False),
            ("Pending", None, True),
        ],
    )
    client, _ = harness.login_client("area@example.com")
    query = "area=-0.5,-0.5,0.5,0.5"
    listing = client.get(f"/api/v1/vault/activities?{query}&limit=1").json()
    mapped = client.get(f"/api/v1/vault/activities/map?{query}").json()
    assert listing["total"] == mapped["total"] == 1
    assert listing["items"][0]["name"] == "Crosses"
    assert listing["summary"]["distance_m"] == 1000
    assert listing["geographic_excluded_count"] == mapped["geographic_excluded_count"] == 2
    assert mapped["features"][0]["properties"]["id"] == listing["items"][0]["id"]
    assert client.get("/api/v1/vault/activities").json()["summary"]["no_gps_activity_count"] == 1


def test_map_reports_bounded_response_and_bad_geometry_without_failing(harness: AppHarness) -> None:
    seed(harness, "bounded@example.com", [(f"Route {i}", [(0, 0), (1, 1)], True) for i in range(3)])
    with harness.session_factory() as db:
        activity = db.query(VaultActivity).filter_by(name="Route 0").one()
        activity.summary_polyline = "invalid~"
        db.commit()
    client, _ = harness.login_client("bounded@example.com")
    body = client.get("/api/v1/vault/activities/map?max_features=1").json()
    assert body["total"] == 3
    assert body["returned_count"] == len(body["features"]) <= 1
    assert body["unavailable_count"] == body["unavailable_in_page_count"]
    pages = [body]
    while pages[-1]["next_offset"] is not None:
        pages.append(
            client.get(
                "/api/v1/vault/activities/map",
                params={"max_features": 1, "offset": pages[-1]["next_offset"]},
            ).json()
        )
    assert sum(page["drawable_count"] for page in pages) == 2
    assert sum(page["unavailable_in_page_count"] for page in pages) == 1
    page_ids = [feature["properties"]["id"] for page in pages for feature in page["features"]]
    assert len(page_ids) == len(set(page_ids)) == 2
    assert pages[-1]["truncated"] is False


@pytest.mark.parametrize("area", ["bad", "0,1,2", "0,2,1,1", "0,-91,1,1", "nan,0,1,1", "0,0,181,1"])
def test_invalid_area_rejected_by_both_endpoints(harness: AppHarness, area: str) -> None:
    harness.create_user("invalid@example.com")
    client, _ = harness.login_client("invalid@example.com")
    for path in ("activities", "activities/map"):
        response = client.get(f"/api/v1/vault/{path}", params={"area": area})
        assert response.status_code == 422


def test_dateline_area_does_not_include_greenwich(harness: AppHarness) -> None:
    seed(
        harness,
        "dateline@example.com",
        [("Dateline", [(0, 178), (0, -178)], True), ("Greenwich", [(0, -1), (0, 1)], True)],
    )
    client, _ = harness.login_client("dateline@example.com")
    result = client.get("/api/v1/vault/activities?area=179,-1,-179,1").json()
    assert [item["name"] for item in result["items"]] == ["Dateline"]
    result = client.get("/api/v1/vault/activities?area=-1,-1,1,1").json()
    assert [item["name"] for item in result["items"]] == ["Greenwich"]
    mapped = client.get("/api/v1/vault/activities/map?name=Dateline").json()
    assert mapped["features"][0]["geometry"]["coordinates"] == [[178, 0], [182, 0]]


def test_total_geometry_budget_is_disclosed_not_silently_incomplete(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trip_api.routers import vault

    seed(
        harness,
        "budget@example.com",
        [
            ("One", [(0, 0), (1, 1)], True),
            ("Two", [(0, 0), (1, 1)], True),
        ],
    )
    monkeypatch.setattr(vault, "MAX_EVALUATION_CHARS", len(encode_polyline([(0, 0), (1, 1)])))
    client, _ = harness.login_client("budget@example.com")
    body = client.get("/api/v1/vault/activities/map").json()
    assert body["total"] == 2
    assert body["returned_count"] == body["drawable_count"] == 1
    assert body["unevaluated_count"] == 1
    assert body["next_offset"] == 1
    final_page = client.get("/api/v1/vault/activities/map?offset=1").json()
    assert final_page["returned_count"] == 1
    assert final_page["unevaluated_count"] == 0
    assert final_page["next_offset"] is None
    assert final_page["truncated"] is False
    for path in ("activities", "activities/map"):
        response = client.get(f"/api/v1/vault/{path}?area=-1,-1,2,2")
        assert response.status_code == 422
        assert "evaluation limit" in response.json()["detail"]


def test_map_point_budget_limits_response_without_changing_selection(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trip_api.routers import vault

    seed(
        harness,
        "points@example.com",
        [
            ("One", [(0, 0), (0.5, 0.5), (1, 1), (1.5, 1.5), (2, 2)], True),
            ("Two", [(3, 3), (3.5, 3.5), (4, 4), (4.5, 4.5), (5, 5)], True),
        ],
    )
    monkeypatch.setattr(vault, "MAX_RESPONSE_POINTS", 4)
    client, _ = harness.login_client("points@example.com")
    body = client.get("/api/v1/vault/activities/map").json()
    assert body["total"] == body["drawable_count"] == 2
    assert body["returned_count"] == 2
    assert sum(len(feature["geometry"]["coordinates"]) for feature in body["features"]) == 4
    assert body["unevaluated_count"] == 0
    first_route = next(
        feature for feature in body["features"] if feature["properties"]["name"] == "One"
    )
    assert first_route["geometry"]["coordinates"] == [[0, 0], [2, 2]]
    assert body["truncated"] is False
