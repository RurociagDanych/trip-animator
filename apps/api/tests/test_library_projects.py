"""Exercise project creation and reuse through authenticated HTTP routes."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from trip_api.db import Project, Stage, VaultActivity
from trip_api.vault_sync import (
    PreparedStageVaultGeometry,
    mirror_stage_to_vault,
    upsert_strava_activity_to_vault,
)

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


def _seed_unlinked_strava_original(
    harness: AppHarness,
    user_id: uuid.UUID,
    *,
    variant: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, bytes, bytes]:
    activity_id = 91002
    bronze_payload = {
        "activity": {"id": activity_id, "name": "Provider original"},
        "streams": {"latlng": {"data": [[1.0, 2.0], [1.1, 2.1]]}},
    }
    if variant == "bronze_identity_mismatch":
        bronze_payload["activity"]["id"] = activity_id + 1
    raw_bronze = json.dumps(bronze_payload, separators=(",", ":"), sort_keys=True).encode()
    raw_silver = b"original local track"
    bronze_key = f"bronze/legacy/{uuid.uuid4()}.json"
    silver_key = f"silver/legacy/{uuid.uuid4()}.parquet"
    harness.store.put_bytes(bronze_key, raw_bronze)
    harness.store.put_bytes(silver_key, raw_silver)
    backlink_owner_id = (
        harness.create_user(f"foreign-backlink-{uuid.uuid4()}@example.test").id
        if variant == "foreign_activity_backlink"
        else user_id
    )
    project_owner_id = uuid.uuid4() if variant == "foreign_owner" else user_id
    with harness.session_factory() as db:
        catalog = upsert_strava_activity_to_vault(
            db,
            user_id=user_id,
            item={"id": activity_id, "name": "Provider original", "type": "Ride"},
            now=datetime(2025, 1, 1, tzinfo=UTC),
        ).vault_activity
        if variant == "foreign_activity_backlink":
            backlink_activity = upsert_strava_activity_to_vault(
                db,
                user_id=backlink_owner_id,
                item={"id": activity_id, "name": "Other owner's same activity", "type": "Ride"},
                now=datetime(2025, 1, 1, tzinfo=UTC),
            ).vault_activity
            backlink_id = backlink_activity.id
        else:
            backlink_id = catalog.id if variant == "same_activity_backlink" else None
        if variant == "disconnected":
            catalog.disconnected_at = datetime(2025, 1, 2, tzinfo=UTC)
        project = Project(owner_user_id=project_owner_id, title="Legacy import", settings_json={})
        db.add(project)
        db.flush()
        source = Stage(
            project_id=project.id,
            source_type="strava",
            source_format="strava",
            bronze_sha256=hashlib.sha256(raw_bronze).hexdigest(),
            external_source_id=(
                str(activity_id + 1) if variant == "identity_mismatch" else str(activity_id)
            ),
            strava_activity_id=activity_id,
            vault_activity_id=backlink_id,
            activity_type="cycling",
            name="Provider original",
            stage_order=0,
            day_index=1,
            bronze_object_key=bronze_key,
            silver_object_key=silver_key,
            point_count=2,
            distance_m=500.0,
            trim_start_s=1.0 if variant == "trim_start" else None,
            trim_end_s=2.0 if variant == "trim_end" else None,
            warnings_json=(
                [{"code": "vault_canonical_excluded_split_segment"}]
                if variant == "split_warning"
                else []
            ),
        )
        if variant == "sha256_mismatch":
            source.bronze_sha256 = "0" * 64
        db.add(source)
        db.commit()
        return catalog.id, project.id, source.id, raw_bronze, raw_silver


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


def test_catalog_first_trusted_import_becomes_reusable_without_provider_calls(
    harness: AppHarness,
) -> None:
    user = harness.create_user("catalog-first-reuse@example.test")
    activity_id = 91001
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    bronze_key = "source/catalog-first.json"
    silver_key = "source/catalog-first.parquet"
    raw_bronze = json.dumps(
        {
            "activity": {"id": activity_id, "name": "Provider catalog name"},
            "streams": {"latlng": {"data": [[1.0, 2.0], [1.1, 2.1], [1.2, 2.2]]}},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    raw_silver = b"trusted local parquet"
    harness.store.put_bytes(bronze_key, raw_bronze)
    harness.store.put_bytes(silver_key, raw_silver)

    with harness.session_factory() as db:
        catalog = upsert_strava_activity_to_vault(
            db,
            user_id=user.id,
            item={
                "id": activity_id,
                "name": "Provider catalog name",
                "type": "Ride",
                "start_date": started_at.isoformat().replace("+00:00", "Z"),
                "distance": 1234.0,
                "moving_time": 300,
                "elapsed_time": 320,
            },
            now=started_at,
        ).vault_activity
        assert catalog.source_stage_id is None
        project = Project(owner_user_id=user.id, title="Trusted import", settings_json={})
        db.add(project)
        db.flush()
        source = Stage(
            project_id=project.id,
            source_type="strava",
            source_format="strava",
            bronze_sha256=hashlib.sha256(raw_bronze).hexdigest(),
            external_source_id=str(activity_id),
            strava_activity_id=activity_id,
            activity_type="cycling",
            name="Edited project-stage name",
            stage_order=0,
            day_index=1,
            bronze_object_key=bronze_key,
            silver_object_key=silver_key,
            point_count=3,
            distance_m=1234.0,
            trim_start_s=None,
            trim_end_s=None,
            warnings_json=[],
        )
        db.add(source)
        db.flush()
        mirror_stage_to_vault(
            db,
            user_id=user.id,
            stage=source,
            geometry=PreparedStageVaultGeometry(summary_polyline=None, tiles_z14=frozenset()),
            trusted_provider_geometry=True,
            strava_activity_json={
                "id": activity_id,
                "name": "Provider catalog name",
                "type": "Ride",
                "start_date": started_at.isoformat().replace("+00:00", "Z"),
                "distance": 1234.0,
                "moving_time": 300,
                "elapsed_time": 320,
            },
        )
        source_project_id = project.id
        assert catalog.source_stage_id == source.id
        assert source.vault_activity_id == catalog.id
        db.commit()
        canonical_id = catalog.id

    client, csrf = harness.login_client(user.email)
    created = client.post(
        "/api/v1/library/projects",
        json={"title": "Catalog reuse", "activity_ids": [str(canonical_id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    created_project_id = uuid.UUID(created.json()["project_id"])

    appended = client.post(
        f"/api/v1/library/projects/{source_project_id}/activities",
        json={"activity_ids": [str(canonical_id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert appended.status_code == 200, appended.text

    source = _project_rows(harness, source_project_id)[0]
    created_stage = _project_rows(harness, created_project_id)[0]
    appended_stages = _project_rows(harness, source_project_id)
    appended_stage = appended_stages[-1]
    assert created_stage.id != source.id
    assert appended_stage.id != source.id
    assert created_stage.bronze_object_key != source.bronze_object_key
    assert appended_stage.bronze_object_key != source.bronze_object_key
    assert harness.store.get_bytes(created_stage.bronze_object_key) == raw_bronze
    assert harness.store.get_bytes(appended_stage.silver_object_key) == raw_silver
    with harness.session_factory() as db:
        canonical = db.get(VaultActivity, canonical_id)
        assert canonical is not None
        assert canonical.source_stage_id == source.id
        assert canonical.name == "Provider catalog name"


@pytest.mark.parametrize("operation", ["create", "append"])
def test_reuse_recovers_unlinked_original_from_verified_provider_bronze(
    harness: AppHarness,
    operation: str,
) -> None:
    user = harness.create_user(f"recover-unlinked-{operation}@example.test")
    canonical_id, source_project_id, source_id, raw_bronze, raw_silver = (
        _seed_unlinked_strava_original(harness, user.id)
    )
    client, csrf = harness.login_client(user.email)
    if operation == "create":
        response = client.post(
            "/api/v1/library/projects",
            json={"title": "Recovered source", "activity_ids": [str(canonical_id)]},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 201, response.text
        target_project_id = uuid.UUID(response.json()["project_id"])
    else:
        response = client.post(
            f"/api/v1/library/projects/{source_project_id}/activities",
            json={"activity_ids": [str(canonical_id)]},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        target_project_id = source_project_id

    source = next(
        stage for stage in _project_rows(harness, source_project_id) if stage.id == source_id
    )
    copied = next(
        stage for stage in _project_rows(harness, target_project_id) if stage.id != source_id
    )
    assert copied.id != source.id
    assert copied.vault_activity_id is None
    assert copied.bronze_object_key != source.bronze_object_key
    assert copied.silver_object_key != source.silver_object_key
    assert harness.store.get_bytes(copied.bronze_object_key) == raw_bronze
    assert harness.store.get_bytes(copied.silver_object_key) == raw_silver
    with harness.session_factory() as db:
        canonical = db.get(VaultActivity, canonical_id)
        assert canonical is not None
        assert canonical.source_stage_id == source_id
        recovered = db.get(Stage, canonical.source_stage_id)
        assert recovered is not None and recovered.project_id == source_project_id
        assert recovered.vault_activity_id == canonical_id


@pytest.mark.parametrize("operation", ["create", "append"])
def test_reuse_recovers_original_with_same_activity_backlink_and_missing_source_pointer(
    harness: AppHarness,
    operation: str,
) -> None:
    user = harness.create_user(f"recover-backlink-{operation}@example.test")
    canonical_id, source_project_id, source_id, raw_bronze, raw_silver = (
        _seed_unlinked_strava_original(harness, user.id, variant="same_activity_backlink")
    )
    with harness.session_factory() as db:
        canonical = db.get(VaultActivity, canonical_id)
        source = db.get(Stage, source_id)
        assert canonical is not None and canonical.source_stage_id is None
        assert source is not None and source.vault_activity_id == canonical_id

    client, csrf = harness.login_client(user.email)
    if operation == "create":
        response = client.post(
            "/api/v1/library/projects",
            json={"title": "Recovered backlink", "activity_ids": [str(canonical_id)]},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 201, response.text
        target_project_id = uuid.UUID(response.json()["project_id"])
    else:
        response = client.post(
            f"/api/v1/library/projects/{source_project_id}/activities",
            json={"activity_ids": [str(canonical_id)]},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        target_project_id = source_project_id

    source = next(
        stage for stage in _project_rows(harness, source_project_id) if stage.id == source_id
    )
    copied = next(
        stage for stage in _project_rows(harness, target_project_id) if stage.id != source_id
    )
    assert source.vault_activity_id == canonical_id
    assert copied.vault_activity_id == canonical_id
    assert harness.store.get_bytes(copied.bronze_object_key) == raw_bronze
    assert harness.store.get_bytes(copied.silver_object_key) == raw_silver
    with harness.session_factory() as db:
        canonical = db.get(VaultActivity, canonical_id)
        assert canonical is not None and canonical.source_stage_id == source_id


@pytest.mark.parametrize("operation", ["create", "append"])
@pytest.mark.parametrize(
    ("variant", "status_code"),
    [
        ("identity_mismatch", 409),
        ("bronze_identity_mismatch", 409),
        ("sha256_mismatch", 409),
        ("foreign_owner", 409),
        ("trim_start", 409),
        ("trim_end", 409),
        ("split_warning", 409),
        ("foreign_activity_backlink", 409),
        ("disconnected", 404),
    ],
)
def test_reuse_rejects_untrusted_unlinked_original_without_mutation(
    harness: AppHarness,
    operation: str,
    variant: str,
    status_code: int,
) -> None:
    user = harness.create_user(f"reject-{variant}-{operation}@example.test")
    canonical_id, source_project_id, source_id, raw_bronze, raw_silver = (
        _seed_unlinked_strava_original(harness, user.id, variant=variant)
    )
    client, csrf = harness.login_client(user.email)
    with harness.session_factory() as db:
        before_project_count = len(list(db.scalars(select(Project)).all()))
        before_stage_count = len(list(db.scalars(select(Stage)).all()))
        canonical_before = db.get(VaultActivity, canonical_id)
        assert canonical_before is not None
        canonical_snapshot = (
            canonical_before.name,
            canonical_before.distance_m,
            canonical_before.summary_polyline,
            canonical_before.tile_count_z14,
            canonical_before.source_stage_id,
        )

    if operation == "create":
        response = client.post(
            "/api/v1/library/projects",
            json={"title": f"Rejected {variant}", "activity_ids": [str(canonical_id)]},
            headers={"X-CSRF-Token": csrf},
        )
    else:
        response = client.post(
            f"/api/v1/library/projects/{source_project_id}/activities",
            json={"activity_ids": [str(canonical_id)]},
            headers={"X-CSRF-Token": csrf},
        )
    assert response.status_code == status_code, response.text
    with harness.session_factory() as db:
        assert len(list(db.scalars(select(Project)).all())) == before_project_count
        assert len(list(db.scalars(select(Stage)).all())) == before_stage_count
        canonical_after = db.get(VaultActivity, canonical_id)
        source_after = db.get(Stage, source_id)
        assert canonical_after is not None and source_after is not None
        assert (
            canonical_after.name,
            canonical_after.distance_m,
            canonical_after.summary_polyline,
            canonical_after.tile_count_z14,
            canonical_after.source_stage_id,
        ) == canonical_snapshot
        assert canonical_after.source_stage_id is None
        if variant == "foreign_activity_backlink":
            assert source_after.vault_activity_id is not None
            assert source_after.vault_activity_id != canonical_id
            foreign_backlink = db.get(VaultActivity, source_after.vault_activity_id)
            assert foreign_backlink is not None
            assert foreign_backlink.strava_activity_id == canonical_after.strava_activity_id
            assert foreign_backlink.user_id != user.id
        else:
            assert source_after.vault_activity_id is None
        assert (
            len(list(db.scalars(select(Stage).where(Stage.project_id == source_project_id)).all()))
            == 1
        )
    assert harness.store.get_bytes(source_after.bronze_object_key) == raw_bronze
    assert harness.store.get_bytes(source_after.silver_object_key) == raw_silver


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
