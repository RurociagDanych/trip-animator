from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from trip_api.db import IngestJob, OAuthConnection, Project, Stage, VaultActivity
from trip_api.ingest import process_pending_ingest_jobs
from trip_api.security import encrypt_secret
from trip_api.vault_sync import upsert_strava_activity_to_vault


def test_provider_delete_removes_owned_canonical_activity_only(
    make_harness,
) -> None:
    harness = make_harness()
    owner = harness.create_user("owner-ingest@example.com")
    other_owner = harness.create_user("other-ingest@example.com")
    key = harness.settings.decoded_encryption_key
    now = datetime.now(UTC)
    with harness.session_factory() as db:
        db.add(
            OAuthConnection(
                user_id=owner.id,
                provider="strava",
                external_athlete_id="7711",
                access_token_enc=encrypt_secret(
                    "access", key, aad=f"oauth:{owner.id}:strava".encode()
                ),
                refresh_token_enc=encrypt_secret(
                    "refresh", key, aad=f"oauth:{owner.id}:strava".encode()
                ),
                expires_at=now + timedelta(hours=1),
                scope="read,activity:read_all",
            )
        )
        owner_activity = upsert_strava_activity_to_vault(
            db,
            user_id=owner.id,
            item={
                "id": 8811,
                "name": "Owner canonical",
                "sport_type": "Ride",
                "type": "Ride",
                "start_date": "2026-01-01T08:00:00Z",
                "start_date_local": "2026-01-01T08:00:00Z",
                "distance": 12000.0,
                "total_elevation_gain": 300.0,
                "moving_time": 1800,
                "elapsed_time": 1900,
                "map": {"summary_polyline": ""},
            },
            now=now,
        ).vault_activity
        other_activity = upsert_strava_activity_to_vault(
            db,
            user_id=other_owner.id,
            item={
                "id": 8811,
                "name": "Other canonical",
                "sport_type": "Ride",
                "type": "Ride",
                "start_date": "2026-01-01T08:00:00Z",
                "start_date_local": "2026-01-01T08:00:00Z",
                "distance": 12000.0,
                "total_elevation_gain": 300.0,
                "moving_time": 1800,
                "elapsed_time": 1900,
                "map": {"summary_polyline": ""},
            },
            now=now,
        ).vault_activity
        owner_project = Project(owner_user_id=owner.id, title="Owner project", settings_json={})
        other_project = Project(
            owner_user_id=other_owner.id, title="Other project", settings_json={}
        )
        db.add_all([owner_project, other_project])
        db.flush()
        owner_stage = Stage(
            project_id=owner_project.id,
            source_type="strava",
            source_format="strava",
            activity_type="cycling",
            external_source_id="8811",
            strava_activity_id=8811,
            vault_activity_id=owner_activity.id,
            name="Owner edited stage",
            stage_order=0,
            day_index=1,
            bronze_object_key="bronze/owner/8811.json",
            silver_object_key="silver/owner/8811.parquet",
            point_count=2,
            distance_m=9000.0,
            trim_start_s=15.0,
            trim_end_s=120.0,
        )
        other_stage = Stage(
            project_id=other_project.id,
            source_type="strava",
            source_format="strava",
            activity_type="cycling",
            external_source_id="8811",
            strava_activity_id=8811,
            vault_activity_id=other_activity.id,
            name="Other stage",
            stage_order=0,
            day_index=1,
            bronze_object_key="bronze/other/8811.json",
            silver_object_key="silver/other/8811.parquet",
            point_count=2,
            distance_m=12000.0,
        )
        db.add_all([owner_stage, other_stage])
        db.flush()
        owner_stage_id = owner_stage.id
        other_stage_id = other_stage.id
        artifact_keys = [
            owner_stage.bronze_object_key,
            owner_stage.silver_object_key,
            other_stage.bronze_object_key,
            other_stage.silver_object_key,
        ]
        artifact_values = [b"owner source", b"owner edited", b"other source", b"other edited"]
        for artifact_key, artifact_value in zip(artifact_keys, artifact_values, strict=True):
            harness.store.put_bytes(artifact_key, artifact_value)
        db.add(
            IngestJob(
                provider="strava",
                event_type="delete",
                external_owner_id="7711",
                external_object_id=8811,
                payload_json={"object_type": "activity", "aspect_type": "delete"},
                status="queued",
            )
        )
        db.commit()

        result = process_pending_ingest_jobs(db, harness.store, harness.settings)
        assert result["succeeded"] == 1
        assert db.scalar(select(VaultActivity).where(VaultActivity.id == owner_activity.id)) is None
        assert (
            db.scalar(select(VaultActivity).where(VaultActivity.id == other_activity.id))
            is not None
        )
        preserved_owner_stage = db.get(Stage, owner_stage_id)
        preserved_other_stage = db.get(Stage, other_stage_id)
        assert preserved_owner_stage is not None
        assert preserved_other_stage is not None
        assert preserved_owner_stage.vault_activity_id is None
        assert preserved_owner_stage.name == "Owner edited stage"
        assert preserved_owner_stage.trim_start_s == 15.0
        assert preserved_owner_stage.trim_end_s == 120.0
        assert preserved_other_stage.vault_activity_id == other_activity.id
        assert [harness.store.get_bytes(k) for k in artifact_keys] == artifact_values

        db.add(
            IngestJob(
                provider="strava",
                event_type="delete",
                external_owner_id="7711",
                external_object_id=8811,
                payload_json={"object_type": "activity", "aspect_type": "delete"},
                status="queued",
            )
        )
        db.commit()
        replay_result = process_pending_ingest_jobs(db, harness.store, harness.settings)
        assert replay_result["succeeded"] == 1
        assert (
            db.scalar(select(VaultActivity).where(VaultActivity.id == other_activity.id))
            is not None
        )
        assert db.get(Stage, owner_stage_id) is not None
        assert db.get(Stage, other_stage_id) is not None
        assert [harness.store.get_bytes(k) for k in artifact_keys] == artifact_values
