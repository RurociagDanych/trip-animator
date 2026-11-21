"""Exercise catalog visibility and suppression while preserving project data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_library_projects import _seed_activity
from test_postgres_concurrency import (  # noqa: F401  # registers isolated PostgreSQL fixtures
    pg_app_factory,
    pg_temp_db_url,
)
from trip_api.db import Stage, VaultActivity
from trip_api.vault_sync import delete_strava_vault_activity, upsert_strava_activity_to_vault

if TYPE_CHECKING:
    from conftest import AppHarness


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def lifecycle_harness(request: pytest.FixtureRequest, harness: AppHarness) -> AppHarness:
    """Exercise lifecycle against SQLite and migrated isolated PostgreSQL databases."""
    if request.param == "sqlite":
        return harness
    factory = request.getfixturevalue("pg_app_factory")
    app, settings, engine, sessions, store = factory()
    return type(harness)(app, settings, engine, sessions, store, db_path=harness.db_path)


def test_hide_restore_scopes_lists_summaries_maps_and_ownership(
    lifecycle_harness: AppHarness,
) -> None:
    harness = lifecycle_harness
    owner = harness.create_user("visibility-owner@example.test")
    foreign = harness.create_user("visibility-foreign@example.test")
    activity_id, _ = _seed_activity(harness, owner.id)
    client, csrf = harness.login_client(owner.email)
    other, other_csrf = harness.login_client(foreign.email)
    path = f"/api/v1/vault/activities/{activity_id}/visibility"
    assert client.patch(path, json={"hidden": True}).status_code == 403
    assert (
        other.patch(path, json={"hidden": True}, headers={"X-CSRF-Token": other_csrf}).status_code
        == 404
    )
    response = client.patch(path, json={"hidden": True}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    hidden = client.get("/api/v1/vault/activities").json()
    assert hidden["total"] == hidden["summary"]["activity_count"] == 0
    map_response = client.get("/api/v1/vault/activities/map")
    assert map_response.status_code == 200, map_response.text
    assert map_response.json()["features"] == []
    assert map_response.json()["total"] == 0
    included = client.get("/api/v1/vault/activities?include_hidden=true").json()
    assert included["total"] == 1
    assert included["items"][0]["hidden"] is True
    restored = client.patch(path, json={"hidden": False}, headers={"X-CSRF-Token": csrf})
    assert restored.status_code == 200
    assert client.get("/api/v1/vault/activities").json()["total"] == 1


def test_disconnect_requires_current_impact_preserves_project_bytes_and_suppresses_reimport(
    lifecycle_harness: AppHarness,
) -> None:
    harness = lifecycle_harness
    owner = harness.create_user("disconnect-owner@example.test")
    foreign = harness.create_user("disconnect-foreign@example.test")
    activity_id, source_project_id = _seed_activity(harness, owner.id)
    with harness.session_factory() as db:
        activity = db.get(VaultActivity, activity_id)
        assert activity is not None
        activity.external_activity_id = "9821"
        activity.strava_activity_id = 9821
        db.commit()
        source = db.scalar(select(Stage).where(Stage.project_id == source_project_id))
        assert source is not None
        source_id = source.id
        bronze_key, silver_key = source.bronze_object_key, source.silver_object_key
    before = (harness.store.get_bytes(bronze_key), harness.store.get_bytes(silver_key))
    client, csrf = harness.login_client(owner.email)
    other, other_csrf = harness.login_client(foreign.email)
    root = f"/api/v1/vault/activities/{activity_id}"
    assert other.get(root + "/disconnect-preview").status_code == 404
    preview = client.get(root + "/disconnect-preview")
    assert preview.status_code == 200, preview.text
    body = {"confirmation": preview.json()["confirmation"]}
    assert preview.json()["permanent_purge_available"] is False
    assert preview.json()["project_usages"][0]["stage_id"] == str(source_id)
    assert client.post(root + "/disconnect", json=body).status_code == 403
    assert (
        other.post(
            root + "/disconnect", json=body, headers={"X-CSRF-Token": other_csrf}
        ).status_code
        == 404
    )
    with harness.session_factory() as db:
        source = db.get(Stage, source_id)
        assert source is not None
        db.add(
            Stage(
                id=uuid4(),
                project_id=source_project_id,
                source_type="strava",
                source_format="gpx",
                activity_type="cycling",
                vault_activity_id=activity_id,
                name="Later usage",
                point_count=3,
                distance_m=100,
                stage_order=1,
                day_index=2,
                bronze_object_key=bronze_key,
                silver_object_key=silver_key,
            )
        )
        db.commit()
    assert (
        client.post(root + "/disconnect", json=body, headers={"X-CSRF-Token": csrf}).status_code
        == 409
    )
    body = {"confirmation": client.get(root + "/disconnect-preview").json()["confirmation"]}
    response = client.post(root + "/disconnect", json=body, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["permanently_deleted"] is False
    assert client.get("/api/v1/vault/activities?include_hidden=true").json()["total"] == 0
    assert client.get(root).status_code == 404
    assert (
        client.post(root + "/disconnect", json=body, headers={"X-CSRF-Token": csrf}).status_code
        == 404
    )
    with harness.session_factory() as db:
        result = upsert_strava_activity_to_vault(
            db,
            user_id=owner.id,
            item={
                "id": 9821,
                "name": "Resurrected",
                "type": "Ride",
                "start_date": "2025-01-01T00:00:00Z",
            },
            now=datetime.now(UTC),
        )
        assert result.vault_activity.id == activity_id
        assert result.vault_activity.disconnected_at is not None
        assert result.vault_activity.name == "Library ride"
        assert delete_strava_vault_activity(db, user_id=owner.id, strava_activity_id=9821) == 0
        db.commit()
        assert db.get(Stage, source_id) is not None
    assert client.get("/api/v1/vault/activities?include_hidden=true").json()["total"] == 0
    assert (harness.store.get_bytes(bronze_key), harness.store.get_bytes(silver_key)) == before
    reuse = client.post(
        "/api/v1/library/projects",
        json={"title": "No resurrection", "activity_ids": [str(activity_id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert reuse.status_code == 404
