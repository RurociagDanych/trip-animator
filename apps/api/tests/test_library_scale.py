"""Measure authenticated catalog and bounded map behavior on a 20,000-activity history."""

import json
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import insert
from test_postgres_concurrency import (
    _create_pg_user,
    _login_pg_client,
    pg_app_factory,  # noqa: F401  # registers the isolated PostgreSQL application fixture
    pg_temp_db_url,  # noqa: F401  # registers the disposable database fixture dependency
)
from trip_api.db import VaultActivity
from trip_api.library_geometry import MAX_RESPONSE_POINTS

pytestmark = pytest.mark.postgres


def test_reference_history_catalog_and_complete_bounded_map(request: pytest.FixtureRequest) -> None:
    build_app = request.getfixturevalue("pg_app_factory")
    app, _, _, sf, _ = build_app()
    owner = _create_pg_user(sf, "scale@example.test")
    other = _create_pg_user(sf, "scale-other@example.test")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "id": uuid4(),
            "user_id": owner.id,
            "source_provider": "upload",
            "external_activity_id": f"scale-{index}",
            "name": f"Synthetic reference activity {index}",
            "activity_type": "cycling" if index % 2 else "running",
            "started_at": start - timedelta(hours=index),
            "local_date": (start - timedelta(hours=index)).date(),
            "distance_m": 1000.0,
            "elevation_gain_m": 10.0,
            "moving_seconds": 300.0,
            "elapsed_seconds": 360.0,
            "has_gps": True,
            "summary_polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
            "tile_precision": "summary_polyline",
            "raw_metadata_json": {"source_format": "gpx"},
        }
        for index in range(20_000)
    ]
    with sf() as db:
        db.execute(insert(VaultActivity), rows)
        db.commit()
    client, _ = _login_pg_client(app, owner.email)
    timings: dict[str, list[float]] = {}
    for label, params, expected in (
        ("first_page", {"limit": 50}, 20_000),
        ("last_page", {"limit": 50, "offset": 19_950}, 20_000),
        ("sport_filter", {"limit": 50, "sport": "cycling"}, 10_000),
    ):
        timings[label] = []
        for _ in range(3):
            before = perf_counter()
            response = client.get("/api/v1/vault/activities", params=params)
            timings[label].append(round((perf_counter() - before) * 1000, 2))
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["total"] == expected
            assert payload["summary"]["activity_count"] == expected
            assert len(payload["items"]) == 50
    before = perf_counter()
    ids: set[str] = set()
    points = 0
    pages = 0
    offset = 0
    while True:
        response = client.get(
            "/api/v1/vault/activities/map", params={"offset": offset, "max_features": 1000}
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        features = payload["features"]
        assert len(features) <= 1000
        assert payload["total"] == 20_000
        page_ids = {feature["properties"]["id"] for feature in features}
        assert not ids.intersection(page_ids)
        ids.update(page_ids)
        points += sum(len(feature["geometry"]["coordinates"]) for feature in features)
        pages += 1
        next_offset = payload["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    timings["complete_map"] = [round((perf_counter() - before) * 1000, 2)]
    assert len(ids) == 20_000
    assert points <= MAX_RESPONSE_POINTS
    other_client, _ = _login_pg_client(app, other.email)
    assert other_client.get("/api/v1/vault/activities").json()["total"] == 0
    assert other_client.get("/api/v1/vault/activities/map").json()["features"] == []
    print(
        json.dumps(
            {
                "reference_activities": len(ids),
                "map_pages": pages,
                "map_points": points,
                "timings_ms": timings,
            },
            sort_keys=True,
        )
    )
