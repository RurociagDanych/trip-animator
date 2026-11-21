"""Exercise project creation and reuse through authenticated HTTP routes."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from trip_api.db import Project, Stage, VaultActivity

if TYPE_CHECKING:
    from conftest import AppHarness


def _seed_activity(
    harness: AppHarness,
    user_id: uuid.UUID,
    *,
    with_artifacts: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    activity_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    bronze_key = f"source/{activity_id}.gpx"
    silver_key = f"source/{activity_id}.parquet"
    if with_artifacts:
        harness.store.put_bytes(bronze_key, b"local gpx")
        harness.store.put_bytes(silver_key, b"local parquet")

    with harness.session_factory() as db:
        activity = VaultActivity(
            id=activity_id,
            user_id=user_id,
            source_provider="strava",
            external_activity_id=str(activity_id),
            name="Library ride",
            activity_type="cycling",
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            local_date=date(2025, 1, 1),
            bronze_object_key=bronze_key,
            has_gps=True,
            tile_precision="full_stream",
        )
        project = Project(owner_user_id=user_id, title="Source project", settings_json={})
        db.add_all([activity, project])
        db.flush()
        stage = Stage(
            id=stage_id,
            project_id=project.id,
            source_type="strava",
            source_format="gpx",
            activity_type="cycling",
            vault_activity_id=activity_id,
            name="Library ride",
            stage_order=0,
            day_index=1,
            bronze_object_key=bronze_key,
            silver_object_key=silver_key,
            point_count=3,
            distance_m=1234.0,
            elevation_gain_m=20.0,
            elevation_loss_m=2.0,
            moving_seconds=300.0,
            elapsed_seconds=320.0,
            has_timestamps=True,
            warnings_json=[],
        )
        db.add(stage)
        db.flush()
        activity.source_stage_id = stage.id
        db.commit()
        source_project_id = project.id
    return activity_id, source_project_id


def _project_rows(harness: AppHarness, project_id: uuid.UUID) -> list[Stage]:
    with harness.session_factory() as db:
        return list(
            db.scalars(
                select(Stage).where(Stage.project_id == project_id).order_by(Stage.stage_order)
            ).all()
        )


def test_create_and_add_reuses_local_artifacts_without_mutating_canonical(
    harness: AppHarness,
) -> None:
    user = harness.create_user("library-owner@example.test")
    activity_id, source_project_id = _seed_activity(harness, user.id)
    client, csrf = harness.login_client(user.email)

    created = client.post(
        "/api/v1/library/projects",
        json={"title": "New trip", "activity_ids": [str(activity_id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    new_project_id = uuid.UUID(created.json()["project_id"])
    reused = _project_rows(harness, new_project_id)
    source = _project_rows(harness, source_project_id)[0]
    assert len(reused) == 1
    assert reused[0].id != source.id
    assert reused[0].project_id == new_project_id
    assert reused[0].vault_activity_id == activity_id
    assert reused[0].bronze_object_key != source.bronze_object_key
    assert reused[0].silver_object_key != source.silver_object_key
    assert harness.store.get_bytes(reused[0].bronze_object_key) == harness.store.get_bytes(
        source.bronze_object_key
    )
    assert harness.store.get_bytes(reused[0].silver_object_key) == harness.store.get_bytes(
        source.silver_object_key
    )
    assert reused[0].trim_start_s is None and reused[0].trim_end_s is None

    added = client.post(
        f"/api/v1/library/projects/{source_project_id}/activities",
        json={"activity_ids": [str(activity_id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert added.status_code == 200, added.text
    stages = _project_rows(harness, source_project_id)
    assert [stage.stage_order for stage in stages] == [0, 1]
    assert stages[1].id != stages[0].id

    source_bronze = harness.store.get_bytes(source.bronze_object_key)
    source_silver = harness.store.get_bytes(source.silver_object_key)
    deleted = client.delete(
        f"/api/v1/projects/{new_project_id}/stages/{reused[0].id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert deleted.status_code == 204, deleted.text
    assert harness.store.get_bytes(source.bronze_object_key) == source_bronze
    assert harness.store.get_bytes(source.silver_object_key) == source_silver

    with harness.session_factory() as db:
        canonical = db.get(VaultActivity, activity_id)
        assert canonical is not None
        assert canonical.source_stage_id == source.id
        assert canonical.distance_m == 0.0
        assert (
            len(list(db.scalars(select(VaultActivity).where(VaultActivity.user_id == user.id))))
            == 1
        )


def test_reuse_rejects_other_users_and_missing_source_artifacts(harness: AppHarness) -> None:
    owner = harness.create_user("library-owner-two@example.test")
    other = harness.create_user("library-other@example.test")
    activity_id, _ = _seed_activity(harness, owner.id)
    missing_id, _ = _seed_activity(harness, owner.id, with_artifacts=False)
    client, csrf = harness.login_client(other.email)

    foreign = client.post(
        "/api/v1/library/projects",
        json={"title": "No access", "activity_ids": [str(activity_id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert foreign.status_code == 404
    owner_client, owner_csrf = harness.login_client(owner.email)
    response = owner_client.post(
        "/api/v1/library/projects",
        json={"title": "Unavailable", "activity_ids": [str(missing_id)]},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert response.status_code == 409
    with harness.session_factory() as db:
        assert db.scalar(select(Project).where(Project.title == "Unavailable")) is None
