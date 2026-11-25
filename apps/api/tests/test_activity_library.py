from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from trip_api.db import Project, Stage, VaultActivity

if TYPE_CHECKING:
    from conftest import AppHarness


def _activity(
    user_id: uuid.UUID,
    name: str,
    *,
    started: datetime,
    distance_m: float = 1000.0,
    elevation_gain_m: float = 10.0,
    moving_seconds: float = 600.0,
    elapsed_seconds: float = 700.0,
    activity_type: str = "cycling",
    has_gps: bool = True,
    started_local: datetime | None = None,
    raw_metadata: dict[str, object] | None = None,
) -> VaultActivity:
    return VaultActivity(
        id=uuid.uuid4(),
        user_id=user_id,
        source_provider="strava",
        external_activity_id=str(uuid.uuid4()),
        strava_activity_id=None,
        name=name,
        raw_sport_type=activity_type,
        activity_type=activity_type,
        started_at=started,
        started_at_local=started_local,
        local_date=started_local.date() if started_local else started.date(),
        distance_m=distance_m,
        elevation_gain_m=elevation_gain_m,
        moving_seconds=moving_seconds,
        elapsed_seconds=elapsed_seconds,
        has_gps=has_gps,
        is_commute=False,
        is_indoor_virtual=not has_gps,
        is_private=False,
        tile_precision="none",
        tile_count_z14=0,
        new_tiles_z14_count=0,
        raw_metadata_json=(
            raw_metadata
            if raw_metadata is not None
            else {
                "fixture": True,
                "distance": distance_m,
                "total_elevation_gain": elevation_gain_m,
                "moving_time": moving_seconds,
                "elapsed_time": elapsed_seconds,
            }
        ),
    )


def _seed(h: AppHarness, *activities: VaultActivity) -> None:
    with h.session_factory() as db:
        db.add_all(activities)
        db.commit()


def test_activity_library_requires_authentication(harness: AppHarness) -> None:
    harness.create_user("catalog@example.com")
    assert TestClient(harness.app).get("/api/v1/vault/activities").status_code == 401


def test_catalog_sports_are_distinct_sorted_and_follow_owner_visibility(
    harness: AppHarness,
) -> None:
    endpoint = "/api/v1/vault/activities/sports"
    assert TestClient(harness.app).get(endpoint).status_code == 401

    owner = harness.create_user("sports@example.com")
    other = harness.create_user("other-sports@example.com")
    when = datetime(2025, 3, 1, tzinfo=UTC)
    run = _activity(owner.id, "Run", started=when, activity_type="running", has_gps=False)
    run.raw_sport_type = "VirtualRun"
    hidden = _activity(owner.id, "Hike", started=when, activity_type="hiking")
    hidden.hidden_at = when
    disconnected = _activity(owner.id, "Walk", started=when, activity_type="walking")
    disconnected.disconnected_at = when
    _seed(
        harness,
        run,
        hidden,
        disconnected,
        _activity(owner.id, "First ride", started=when),
        _activity(owner.id, "Second ride", started=when),
        _activity(other.id, "Ski", started=when, activity_type="skiing"),
    )
    client, _ = harness.login_client("sports@example.com")
    response = client.get(endpoint, params={"name": "No match", "sport": "cycling", "offset": 100})
    assert response.status_code == 200, response.text
    assert response.json() == ["cycling", "running"]

    response = client.get(endpoint, params={"include_hidden": True})
    assert response.status_code == 200, response.text
    assert response.json() == ["cycling", "hiking", "running"]


def test_activity_library_searches_beyond_first_page_and_sorts_stably(
    harness: AppHarness,
) -> None:
    user = harness.create_user("catalog@example.com")
    when = datetime(2025, 3, 1, tzinfo=UTC)
    _seed(
        harness,
        *[_activity(user.id, f"Ride {i}", started=when) for i in range(4)],
        _activity(user.id, "Alpine Dawn", started=when),
    )
    client, _ = harness.login_client("catalog@example.com")

    page = client.get("/api/v1/vault/activities?offset=0&limit=2").json()
    search = client.get("/api/v1/vault/activities?name=Alpine&offset=0&limit=2").json()

    assert page["total"] == 5
    assert len(page["items"]) == 2
    assert page["items"][0]["id"] < page["items"][1]["id"]
    assert search["total"] == 1
    assert search["items"][0]["name"] == "Alpine Dawn"


def test_activity_library_name_search_ignores_accents_across_views(harness: AppHarness) -> None:
    user = harness.create_user("accent-search@example.com")
    started = datetime(2025, 3, 1, tzinfo=UTC)
    activities = [
        _activity(user.id, "Koło", started=started),
        _activity(user.id, "Koło12", started=started),
    ]
    for activity in activities:
        activity.summary_polyline = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    _seed(harness, *activities)
    client, _ = harness.login_client("accent-search@example.com")

    for term in ("kolo", "KOŁO", "koło"):
        listing = client.get("/api/v1/vault/activities", params={"name": term, "limit": 1})
        assert listing.status_code == 200, listing.text
        assert listing.json()["total"] == 2
        assert len(listing.json()["items"]) == 1
        assert listing.json()["summary"]["activity_count"] == 2

        map_view = client.get("/api/v1/vault/activities/map", params={"name": term})
        assert map_view.status_code == 200, map_view.text
        assert map_view.json()["total"] == 2
        assert {item["properties"]["name"] for item in map_view.json()["features"]} == {
            "Koło",
            "Koło12",
        }


def test_activity_library_filters_and_summary_use_entire_filtered_selection(
    harness: AppHarness,
) -> None:
    user = harness.create_user("summary@example.com")
    activities = [
        _activity(
            user.id,
            "GPS ride",
            started=datetime(2025, 3, 1, tzinfo=UTC),
            distance_m=15000,
            elevation_gain_m=500,
            moving_seconds=3600,
        ),
        _activity(
            user.id,
            "No GPS spin",
            started=datetime(2025, 3, 2, tzinfo=UTC),
            distance_m=0,
            elevation_gain_m=0,
            moving_seconds=1800,
            elapsed_seconds=1900,
            has_gps=False,
        ),
        _activity(
            user.id,
            "Run",
            started=datetime(2025, 4, 1, tzinfo=UTC),
            activity_type="running",
            distance_m=5000,
        ),
    ]
    _seed(harness, *activities)
    client, _ = harness.login_client("summary@example.com")

    response = client.get(
        "/api/v1/vault/activities?start_date=2025-03-01&end_date=2025-03-31&"
        "sport=cycling&min_distance_m=0&max_distance_m=20000&has_gps=false&limit=1"
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert [item["name"] for item in payload["items"]] == ["No GPS spin"]
    assert payload["summary"] == {
        "activity_count": 1,
        "distance_m": 0.0,
        "distance_known_count": 1,
        "elevation_gain_m": 0.0,
        "elevation_gain_known_count": 1,
        "moving_seconds": 1800.0,
        "moving_seconds_known_count": 1,
        "elapsed_seconds": 1900.0,
        "elapsed_seconds_known_count": 1,
        "gps_activity_count": 0,
        "no_gps_activity_count": 1,
    }


def test_repeated_sports_share_selection_across_list_summary_and_map(harness: AppHarness) -> None:
    user = harness.create_user("multi-sport@example.com")
    when = datetime(2025, 3, 1, tzinfo=UTC)
    activities = [
        _activity(user.id, "Morning ride", started=when, distance_m=1200),
        _activity(user.id, "Morning run", started=when, activity_type="running", distance_m=800),
        _activity(user.id, "Morning hike", started=when, activity_type="hiking"),
        _activity(user.id, "Evening ride", started=when),
    ]
    for activity in activities:
        activity.summary_polyline = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    _seed(harness, *activities)
    client, _ = harness.login_client("multi-sport@example.com")

    params = {"sport": ["cycling", "running", "cycling"], "name": "Morning"}
    listing = client.get("/api/v1/vault/activities", params={**params, "limit": 1})
    mapped = client.get("/api/v1/vault/activities/map", params=params)
    assert listing.status_code == 200, listing.text
    assert mapped.status_code == 200, mapped.text
    body = listing.json()
    assert body["total"] == body["summary"]["activity_count"] == mapped.json()["total"] == 2
    assert len(body["items"]) == 1
    assert body["summary"]["distance_m"] == 2000
    assert {entry["activity_type"]: entry["activity_count"] for entry in body["by_sport"]} == {
        "cycling": 1,
        "running": 1,
    }
    assert {feature["properties"]["name"] for feature in mapped.json()["features"]} == {
        "Morning ride",
        "Morning run",
    }


def test_sport_filter_bounds_allow_unknown_and_empty_values(harness: AppHarness) -> None:
    user = harness.create_user("sport-bounds@example.com")
    when = datetime(2025, 3, 1, tzinfo=UTC)
    custom_sport = " Unmapped, " + "x" * 21
    _seed(
        harness,
        _activity(user.id, "Run", started=when, activity_type="running"),
        _activity(user.id, "Ride", started=when),
    )
    client, _ = harness.login_client("sport-bounds@example.com")

    for endpoint in ("/api/v1/vault/activities", "/api/v1/vault/activities/map"):
        sports = ["running", "", custom_sport] + ["unknown"] * 29
        response = client.get(endpoint, params={"sport": sports})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert client.get(endpoint, params={"sport": ""}).json()["total"] == 0
        assert client.get(endpoint, params={"sport": ["running"] * 33}).status_code == 422
        assert client.get(endpoint, params={"sport": ["running", "x" * 33]}).status_code == 422


def test_activity_library_summary_groups_metrics_by_exact_sport(harness: AppHarness) -> None:
    user = harness.create_user("sport-summary@example.com")
    _seed(
        harness,
        _activity(
            user.id,
            "Ride",
            started=datetime(2025, 3, 1, tzinfo=UTC),
            activity_type="cycling",
            distance_m=12000,
        ),
        _activity(
            user.id,
            "Run",
            started=datetime(2025, 3, 2, tzinfo=UTC),
            activity_type="running",
            distance_m=5000,
            has_gps=False,
        ),
        _activity(
            user.id,
            "Gravel ride",
            started=datetime(2025, 3, 3, tzinfo=UTC),
            activity_type="gravel_mtb",
            distance_m=2500,
        ),
    )
    client, _ = harness.login_client("sport-summary@example.com")

    response = client.get("/api/v1/vault/activities?limit=1")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 1
    assert payload["by_sport"] == [
        {
            "activity_type": "cycling",
            "activity_count": 1,
            "distance_m": 12000.0,
            "distance_known_count": 1,
            "elevation_gain_m": 10.0,
            "elevation_gain_known_count": 1,
            "moving_seconds": 600.0,
            "moving_seconds_known_count": 1,
            "elapsed_seconds": 700.0,
            "elapsed_seconds_known_count": 1,
            "gps_activity_count": 1,
            "no_gps_activity_count": 0,
        },
        {
            "activity_type": "running",
            "activity_count": 1,
            "distance_m": 5000.0,
            "distance_known_count": 1,
            "elevation_gain_m": 10.0,
            "elevation_gain_known_count": 1,
            "moving_seconds": 600.0,
            "moving_seconds_known_count": 1,
            "elapsed_seconds": 700.0,
            "elapsed_seconds_known_count": 1,
            "gps_activity_count": 0,
            "no_gps_activity_count": 1,
        },
        {
            "activity_type": "gravel_mtb",
            "activity_count": 1,
            "distance_m": 2500.0,
            "distance_known_count": 1,
            "elevation_gain_m": 10.0,
            "elevation_gain_known_count": 1,
            "moving_seconds": 600.0,
            "moving_seconds_known_count": 1,
            "elapsed_seconds": 700.0,
            "elapsed_seconds_known_count": 1,
            "gps_activity_count": 1,
            "no_gps_activity_count": 0,
        },
    ]


def test_activity_details_are_private_and_include_readiness_and_project_usages(
    harness: AppHarness,
) -> None:
    alice = harness.create_user("alice@example.com")
    harness.create_user("bob@example.com")
    activity = _activity(
        alice.id,
        "Local source",
        started=datetime(2025, 3, 1, tzinfo=UTC),
        raw_metadata={"fixture": True},
    )
    with harness.session_factory() as db:
        project = Project(owner_user_id=alice.id, title="Tour")
        db.add(project)
        db.flush()
        stage = Stage(
            project_id=project.id,
            source_type="strava",
            source_format="strava",
            activity_type="cycling",
            name="Tour stage",
            stage_order=1,
            day_index=1,
            bronze_object_key="bronze/test",
            silver_object_key="silver/test",
            point_count=2,
            distance_m=1000,
            elevation_gain_m=10,
            elevation_loss_m=0,
            moving_seconds=600,
            elapsed_seconds=700,
            has_timestamps=True,
            vault_activity_id=activity.id,
        )
        db.add_all([activity, stage])
        db.commit()
        activity_id = activity.id
        project_id = project.id

    alice_client, _ = harness.login_client("alice@example.com")
    bob_client, _ = harness.login_client("bob@example.com")
    detail = alice_client.get(f"/api/v1/vault/activities/{activity_id}")

    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == str(activity_id)
    assert body["raw_metadata"] == {"fixture": True}
    assert body["readiness"]["has_gps"] is True
    assert body["readiness"]["tile_precision"] == "none"
    assert body["project_usages"] == [
        {
            "project_id": str(project_id),
            "project_title": "Tour",
            "stage_id": str(stage.id),
            "stage_name": "Tour stage",
        }
    ]
    assert bob_client.get(f"/api/v1/vault/activities/{activity_id}").status_code == 404
    assert bob_client.get("/api/v1/vault/activities").json()["total"] == 0


def test_activity_library_rejects_invalid_filter_ranges(harness: AppHarness) -> None:
    harness.create_user("ranges@example.com")
    client, _ = harness.login_client("ranges@example.com")
    assert (
        client.get("/api/v1/vault/activities?start_date=2025-04-01&end_date=2025-03-01").status_code
        == 422
    )
    assert (
        client.get("/api/v1/vault/activities?min_distance_m=5&max_distance_m=1").status_code == 422
    )
    assert client.get("/api/v1/vault/activities?sort=unknown").status_code == 422


def test_activity_library_distinguishes_missing_measurements_from_explicit_zero(
    harness: AppHarness,
) -> None:
    user = harness.create_user("measurements@example.com")
    _seed(
        harness,
        _activity(
            user.id,
            "Missing measurements",
            started=datetime(2025, 3, 1, tzinfo=UTC),
            distance_m=0,
            elevation_gain_m=0,
            moving_seconds=0,
            elapsed_seconds=0,
            raw_metadata={},
        ),
        _activity(
            user.id,
            "Measured zero",
            started=datetime(2025, 3, 2, tzinfo=UTC),
            distance_m=0,
            elevation_gain_m=0,
            moving_seconds=0,
            elapsed_seconds=0,
            raw_metadata={
                "distance": 0,
                "total_elevation_gain": 0,
                "moving_time": 0,
                "elapsed_time": 0,
            },
        ),
    )
    client, _ = harness.login_client("measurements@example.com")

    response = client.get("/api/v1/vault/activities?min_distance_m=0&sort=distance_m")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["name"] == "Measured zero"
    assert payload["items"][0]["distance_m"] == 0
    assert payload["summary"]["activity_count"] == 1
    assert payload["summary"]["distance_m"] == 0
    assert payload["summary"]["distance_known_count"] == 1

    all_rows = client.get("/api/v1/vault/activities?sort=started_at&order=asc").json()
    missing = next(row for row in all_rows["items"] if row["name"] == "Missing measurements")
    assert missing["distance_m"] is None
    assert missing["elevation_gain_m"] is None
    assert missing["moving_seconds"] is None
    assert missing["elapsed_seconds"] is None
    assert all_rows["summary"]["distance_m"] == 0
    assert all_rows["summary"]["distance_known_count"] == 1


def test_activity_library_reports_local_date_provenance(harness: AppHarness) -> None:
    user = harness.create_user("dates@example.com")
    started = datetime(2025, 3, 1, 23, 30, tzinfo=UTC)
    _seed(
        harness,
        _activity(
            user.id,
            "Provider local date",
            started=started,
            started_local=datetime(2025, 3, 2, 1, 30),
        ),
        _activity(user.id, "UTC fallback", started=started),
    )
    client, _ = harness.login_client("dates@example.com")
    payload = client.get("/api/v1/vault/activities?sort=name&order=asc").json()

    date_rows = [
        (item["name"], item["local_date"], item["date_is_utc_fallback"])
        for item in payload["items"]
    ]
    assert date_rows == [
        ("Provider local date", date(2025, 3, 2).isoformat(), False),
        ("UTC fallback", date(2025, 3, 1).isoformat(), True),
    ]
