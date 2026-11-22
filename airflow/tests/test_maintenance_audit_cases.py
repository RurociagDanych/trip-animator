"""Comprehensive unit tests for maintenance reaper, storage reconciliation, and Parquet audits."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from sqlalchemy import select
from trip_airflow.maintenance_audit import (
    ReconciliationError,
    SilverAuditError,
    reconcile_postgres_and_object_store,
    run_retention_reap,
    validate_silver_parquet_metadata,
)
from trip_airflow.storage_helpers import object_exists
from trip_api.db import (
    AuditEvent,
    OAuthState,
    RateLimitRecord,
    RenderJob,
    SessionRecord,
    Stage,
    VaultActivity,
)
from trip_api.services import stage_lod_cache_key

if TYPE_CHECKING:
    from conftest import AirflowTaskHarness


def test_retention_reap_cutoffs_sessions_oauth_ratelimits_renders(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("retention_cutoffs@example.com")
    proj = airflow_harness.create_project(user.id, "Retention Project")
    now = datetime.now(UTC)

    expired_render_blob = "outputs/render-expired.mp4"
    expired_thumb_blob = "outputs/render-expired-thumb.jpg"
    active_render_blob = "outputs/render-active.mp4"
    running_render_blob = "outputs/render-running.mp4"
    airflow_harness.store.put_bytes(expired_render_blob, b"expired-mp4")
    airflow_harness.store.put_bytes(expired_thumb_blob, b"expired-thumb")
    airflow_harness.store.put_bytes(active_render_blob, b"active-mp4")
    airflow_harness.store.put_bytes(running_render_blob, b"running-mp4")

    with airflow_harness.session_factory() as db:
        sess_expired = SessionRecord(
            user_id=user.id,
            token_hash="1" * 64,
            csrf_token="csrf-1",
            expires_at=now - timedelta(hours=2),
        )
        sess_active = SessionRecord(
            user_id=user.id,
            token_hash="2" * 64,
            csrf_token="csrf-2",
            expires_at=now + timedelta(hours=24),
        )
        db.add_all([sess_expired, sess_active])
        db.flush()

        state_direct_expired = OAuthState(
            user_id=user.id,
            session_id=sess_active.id,
            provider="strava",
            state_hash="a" * 64,
            code_verifier_enc="v1:test",
            expires_at=now - timedelta(minutes=10),
        )
        state_cascade_expired = OAuthState(
            user_id=user.id,
            session_id=sess_expired.id,
            provider="strava",
            state_hash="b" * 64,
            code_verifier_enc="v1:test",
            expires_at=now + timedelta(minutes=10),
        )
        state_active = OAuthState(
            user_id=user.id,
            session_id=sess_active.id,
            provider="strava",
            state_hash="c" * 64,
            code_verifier_enc="v1:test",
            expires_at=now + timedelta(minutes=10),
        )
        db.add_all([state_direct_expired, state_cascade_expired, state_active])

        rl_expired = RateLimitRecord(
            bucket_key="login:expired",
            window_start=now - timedelta(hours=48),
            request_count=5,
            updated_at=now - timedelta(hours=48),
        )
        rl_recent = RateLimitRecord(
            bucket_key="login:recent",
            window_start=now - timedelta(hours=2),
            request_count=2,
            updated_at=now - timedelta(hours=2),
        )
        db.add_all([rl_expired, rl_recent])

        # Non-terminal running jobs must never be reaped, regardless of age.
        job_expired = RenderJob(
            project_id=proj.id,
            owner_user_id=user.id,
            idempotency_key="job-expired",
            idempotency_hash="e1" * 32,
            spec_json={"version": 2},
            status="succeeded",
            output_object_key=expired_render_blob,
            thumbnail_key=expired_thumb_blob,
            expires_at=now - timedelta(hours=1),
        )
        job_active = RenderJob(
            project_id=proj.id,
            owner_user_id=user.id,
            idempotency_key="job-active",
            idempotency_hash="e2" * 32,
            spec_json={"version": 2},
            status="succeeded",
            output_object_key=active_render_blob,
            expires_at=now + timedelta(days=5),
        )
        job_running = RenderJob(
            project_id=proj.id,
            owner_user_id=user.id,
            idempotency_key="job-running",
            idempotency_hash="e3" * 32,
            spec_json={"version": 2},
            status="running",
            output_object_key=running_render_blob,
            updated_at=now - timedelta(days=60),
        )
        db.add_all([job_expired, job_active, job_running])
        db.commit()

    with airflow_harness.session_factory() as db:
        reaped = run_retention_reap(
            db, airflow_harness.store, airflow_harness.settings, dry_run=False
        )

    assert reaped["sessions_deleted"] == 1
    assert reaped["oauth_states_deleted"] == 2
    assert reaped["rate_limits_deleted"] == 1
    assert reaped["render_jobs_deleted"] == 1
    assert reaped["blobs_deleted"] == 2

    with airflow_harness.session_factory() as db:
        sessions = list(db.scalars(select(SessionRecord)).all())
        assert len(sessions) == 1
        assert sessions[0].token_hash == "2" * 64

        states = list(db.scalars(select(OAuthState)).all())
        assert len(states) == 1
        assert states[0].state_hash == "c" * 64

        rate_limits = list(db.scalars(select(RateLimitRecord)).all())
        assert len(rate_limits) == 1
        assert rate_limits[0].bucket_key == "login:recent"

        render_jobs = list(db.scalars(select(RenderJob)).all())
        assert len(render_jobs) == 2
        job_statuses = {j.idempotency_key: j.status for j in render_jobs}
        assert "job-active" in job_statuses
        assert "job-running" in job_statuses

    assert not object_exists(airflow_harness.store, expired_render_blob)
    assert not object_exists(airflow_harness.store, expired_thumb_blob)
    assert object_exists(airflow_harness.store, active_render_blob)
    assert object_exists(airflow_harness.store, running_render_blob)


def test_retention_reap_dry_run_vs_active_deletion_and_audit(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("dry_run_test@example.com")
    now = datetime.now(UTC)

    with airflow_harness.session_factory() as db:
        sess = SessionRecord(
            user_id=user.id,
            token_hash="3" * 64,
            csrf_token="csrf-3",
            expires_at=now - timedelta(hours=5),
        )
        db.add(sess)
        db.commit()

    with airflow_harness.session_factory() as db:
        dry_summary = run_retention_reap(
            db, airflow_harness.store, airflow_harness.settings, dry_run=True
        )
        assert dry_summary["dry_run"] is True
        assert dry_summary["sessions_deleted"] == 1

        sess_still_there = db.scalar(
            select(SessionRecord).where(SessionRecord.token_hash == "3" * 64)
        )
        assert sess_still_there is not None

        audit_dry = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "platform.maintenance_reaped")
        )
        assert audit_dry is None

    with airflow_harness.session_factory() as db:
        active_summary = run_retention_reap(
            db, airflow_harness.store, airflow_harness.settings, dry_run=False
        )
        assert active_summary["dry_run"] is False
        assert active_summary["sessions_deleted"] == 1

        sess_gone = db.scalar(select(SessionRecord).where(SessionRecord.token_hash == "3" * 64))
        assert sess_gone is None

        audit_active = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "platform.maintenance_reaped")
        )
        assert audit_active is not None
        assert audit_active.metadata_json == active_summary

    with airflow_harness.session_factory() as db:
        prior_audit_count = len(list(db.scalars(select(AuditEvent)).all()))
        clean_summary = run_retention_reap(
            db, airflow_harness.store, airflow_harness.settings, dry_run=False
        )
        assert clean_summary["sessions_deleted"] == 0
        new_audit_count = len(list(db.scalars(select(AuditEvent)).all()))
        assert new_audit_count == prior_audit_count


def test_reconciliation_empty_store_and_missing_buckets(
    airflow_harness: AirflowTaskHarness,
) -> None:
    with airflow_harness.session_factory() as db:
        clean = reconcile_postgres_and_object_store(db, airflow_harness.store, fail_on_missing=True)
    assert clean["stages_checked"] == 0
    assert clean["render_jobs_checked"] == 0
    assert clean["missing_bronze_count"] == 0
    assert clean["missing_silver_count"] == 0
    assert clean["missing_render_count"] == 0
    assert clean["orphaned_bronze_count"] == 0
    assert clean["orphaned_silver_count"] == 0

    user = airflow_harness.create_user("missing_bucket@example.com")
    proj = airflow_harness.create_project(user.id, "Missing Bucket Project")
    stage = airflow_harness.ingest_sample_stage(proj, name="Missing Blobs Stage")

    render_blob = "outputs/render-missing.mp4"
    thumb_blob = "outputs/thumb-missing.jpg"
    with airflow_harness.session_factory() as db:
        job = RenderJob(
            project_id=proj.id,
            owner_user_id=user.id,
            idempotency_key="missing-render",
            idempotency_hash="m1" * 32,
            spec_json={"version": 2},
            status="succeeded",
            output_object_key=render_blob,
            thumbnail_key=thumb_blob,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db.add(job)
        db.commit()

    airflow_harness.store.delete(stage.bronze_object_key)
    airflow_harness.store.delete(stage.silver_object_key)
    airflow_harness.store.delete(stage_lod_cache_key(proj.id, stage.id))

    with airflow_harness.session_factory() as db:
        with pytest.raises(ReconciliationError, match="Storage reconciliation detected 4 missing"):
            reconcile_postgres_and_object_store(db, airflow_harness.store, fail_on_missing=True)

    with airflow_harness.session_factory() as db:
        summary = reconcile_postgres_and_object_store(
            db, airflow_harness.store, fail_on_missing=False
        )

    assert summary["stages_checked"] == 1
    assert summary["render_jobs_checked"] == 1
    assert summary["missing_bronze"] == [stage.bronze_object_key]
    assert summary["missing_silver"] == [stage.silver_object_key]
    assert summary["missing_bronze_count"] == 1
    assert summary["missing_silver_count"] == 1
    assert summary["missing_render_count"] == 2
    assert sorted(summary["missing_render"]) == sorted([render_blob, thumb_blob])


def test_reconciliation_orphaned_blobs_grace_period_and_active_deletion(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("orphans@example.com")
    proj = airflow_harness.create_project(user.id, "Orphans Project")
    stage = airflow_harness.ingest_sample_stage(proj, name="Legitimate Stage")

    now = datetime.now(UTC)

    valid_lod_key = stage_lod_cache_key(proj.id, stage.id)
    assert object_exists(airflow_harness.store, valid_lod_key)

    old_bronze_orphan = f"bronze/{proj.id}/old-orphan.gpx"
    recent_silver_orphan = f"silver/{proj.id}/recent-orphan.parquet"
    old_lod_orphan = f"silver/{proj.id}/old-orphan.lod.json.gz"
    recent_output_orphan = "outputs/recent-orphan.mp4"

    airflow_harness.store.put_bytes(old_bronze_orphan, b"old-gpx")
    airflow_harness.store.put_bytes(recent_silver_orphan, b"recent-pq")
    airflow_harness.store.put_bytes(old_lod_orphan, b"old-lod")
    airflow_harness.store.put_bytes(recent_output_orphan, b"recent-mp4")

    old_ts = (now - timedelta(hours=48)).timestamp()
    os.utime(airflow_harness.store.resolve_existing_path(old_bronze_orphan), (old_ts, old_ts))
    os.utime(airflow_harness.store.resolve_existing_path(old_lod_orphan), (old_ts, old_ts))

    recent_ts = (now - timedelta(hours=2)).timestamp()
    os.utime(
        airflow_harness.store.resolve_existing_path(recent_silver_orphan), (recent_ts, recent_ts)
    )
    os.utime(
        airflow_harness.store.resolve_existing_path(recent_output_orphan), (recent_ts, recent_ts)
    )

    with airflow_harness.session_factory() as db:
        with pytest.raises(ValueError, match="orphan_min_age_hours must be >= 0"):
            reconcile_postgres_and_object_store(db, airflow_harness.store, orphan_min_age_hours=-1)

    with airflow_harness.session_factory() as db:
        rec_no_delete = reconcile_postgres_and_object_store(
            db, airflow_harness.store, delete_orphans=False, now=now
        )
    assert rec_no_delete["orphaned_bronze_count"] == 1
    assert rec_no_delete["orphaned_silver_count"] == 1
    assert rec_no_delete["orphaned_lod_count"] == 1
    assert rec_no_delete["orphaned_outputs_count"] == 1
    assert rec_no_delete["deleted_orphans_count"] == 0

    with airflow_harness.session_factory() as db:
        rec_dry = reconcile_postgres_and_object_store(
            db, airflow_harness.store, delete_orphans=True, dry_run=True, now=now
        )
    assert rec_dry["deleted_orphans_count"] == 0
    assert object_exists(airflow_harness.store, old_bronze_orphan)

    with airflow_harness.session_factory() as db:
        rec_active_24h = reconcile_postgres_and_object_store(
            db,
            airflow_harness.store,
            delete_orphans=True,
            dry_run=False,
            orphan_min_age_hours=24,
            now=now,
        )
    assert rec_active_24h["deleted_orphans_count"] == 2
    assert rec_active_24h["skipped_recent_orphans_count"] == 2
    assert not object_exists(airflow_harness.store, old_bronze_orphan)
    assert not object_exists(airflow_harness.store, old_lod_orphan)
    assert object_exists(airflow_harness.store, recent_silver_orphan)
    assert object_exists(airflow_harness.store, recent_output_orphan)

    with airflow_harness.session_factory() as db:
        rec_active_0h = reconcile_postgres_and_object_store(
            db,
            airflow_harness.store,
            delete_orphans=True,
            dry_run=False,
            orphan_min_age_hours=0,
            now=now,
        )
    assert rec_active_0h["deleted_orphans_count"] == 2
    assert not object_exists(airflow_harness.store, recent_silver_orphan)
    assert not object_exists(airflow_harness.store, recent_output_orphan)


def test_reconciliation_preserves_standalone_library_originals(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("library-originals@example.com")
    activity_id = uuid.uuid4()
    now = datetime.now(UTC)
    bronze = f"bronze/{activity_id}/original.json"
    silver = f"silver/{activity_id}/original.parquet"
    orphan_bronze = f"bronze/{activity_id}/orphan.json"
    orphan_silver = f"silver/{activity_id}/orphan.parquet"
    with airflow_harness.session_factory() as db:
        db.add(
            VaultActivity(
                id=activity_id,
                user_id=user.id,
                source_provider="strava",
                external_activity_id="123",
                name="Standalone route",
                started_at=now,
                local_date=now.date(),
                bronze_object_key=bronze,
                silver_object_key=silver,
            )
        )
        db.commit()

    old_ts = (now - timedelta(hours=48)).timestamp()
    for key in (bronze, silver, orphan_bronze, orphan_silver):
        airflow_harness.store.put_bytes(key, b"synthetic artifact")
        os.utime(airflow_harness.store.resolve_existing_path(key), (old_ts, old_ts))

    with airflow_harness.session_factory() as db:
        summary = reconcile_postgres_and_object_store(
            db, airflow_harness.store, delete_orphans=True, now=now
        )

    assert summary["stages_checked"] == 0
    assert summary["orphaned_bronze"] == [orphan_bronze]
    assert summary["orphaned_silver"] == [orphan_silver]
    assert summary["deleted_orphans_count"] == 2
    assert airflow_harness.store.get_bytes(bronze) == b"synthetic artifact"
    assert airflow_harness.store.get_bytes(silver) == b"synthetic artifact"
    assert not object_exists(airflow_harness.store, orphan_bronze)
    assert not object_exists(airflow_harness.store, orphan_silver)


def test_silver_parquet_metadata_audit_comprehensive(
    airflow_harness: AirflowTaskHarness,
) -> None:
    with airflow_harness.session_factory() as db:
        empty_audit = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=True
        )
    assert empty_audit == {
        "checked_stages": 0,
        "valid_stages": 0,
        "corrupt_count": 0,
        "corrupt_stages": [],
    }

    user = airflow_harness.create_user("parquet_audit@example.com")
    proj = airflow_harness.create_project(user.id, "Audit Project")
    healthy_stage = airflow_harness.ingest_sample_stage(proj, name="Healthy Audit Stage")

    with airflow_harness.session_factory() as db:
        healthy_audit = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=True
        )
    assert healthy_audit["valid_stages"] == 1
    assert healthy_audit["corrupt_count"] == 0

    with airflow_harness.session_factory() as db:
        stg = db.get(Stage, healthy_stage.id)
        assert stg is not None
        stg.point_count = 0
        db.commit()

        audit_res = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=False
        )
        assert audit_res["corrupt_count"] == 1
        assert "non-positive point_count" in audit_res["corrupt_stages"][0]["reason"]

        stg.point_count = 3
        db.commit()

    pq_path = airflow_harness.store.resolve_existing_path(healthy_stage.silver_object_key)
    table = pq.read_table(pq_path)
    missing_col_table = table.drop(["is_moving"])
    pq.write_table(missing_col_table, pq_path)

    with airflow_harness.session_factory() as db:
        audit_res = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=False
        )
        assert audit_res["corrupt_count"] == 1
        assert "Missing Silver Parquet columns" in audit_res["corrupt_stages"][0]["reason"]

    mismatch_table = pa.Table.from_batches(table.slice(0, 1).to_batches(), schema=table.schema)
    pq.write_table(mismatch_table, pq_path)

    with airflow_harness.session_factory() as db:
        audit_res = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=False
        )
        assert audit_res["corrupt_count"] == 1
        assert "Row count mismatch" in audit_res["corrupt_stages"][0]["reason"]

    clean_schema = table.schema.with_metadata({})
    no_geo_table = pa.Table.from_batches(table.to_batches(), schema=clean_schema)
    pq.write_table(no_geo_table, pq_path)

    with airflow_harness.session_factory() as db:
        audit_res = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=False
        )
        assert audit_res["corrupt_count"] == 1
        assert "Missing GeoParquet 'geo'" in audit_res["corrupt_stages"][0]["reason"]

    bad_geo = {
        "version": "1.1.0",
        "primary_column": "wrong_col",
        "columns": {"wrong_col": {"encoding": "WKB", "bbox": [0.1, 0.2, 0.3, 0.4]}},
    }
    bad_geo_schema = table.schema.with_metadata({b"geo": json.dumps(bad_geo).encode()})
    bad_geo_table = pa.Table.from_batches(table.to_batches(), schema=bad_geo_schema)
    pq.write_table(bad_geo_table, pq_path)

    with airflow_harness.session_factory() as db:
        audit_res = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=False
        )
        assert audit_res["corrupt_count"] == 1
        assert "primary_column must be 'geometry'" in audit_res["corrupt_stages"][0]["reason"]

    bbox_invert_geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "bbox": [0.5, 0.4, 0.1, 0.2]}},
    }
    bbox_schema = table.schema.with_metadata({b"geo": json.dumps(bbox_invert_geo).encode()})
    bbox_table = pa.Table.from_batches(table.to_batches(), schema=bbox_schema)
    pq.write_table(bbox_table, pq_path)

    with airflow_harness.session_factory() as db:
        audit_res = validate_silver_parquet_metadata(
            db, airflow_harness.store, fail_on_corrupt=False
        )
        assert audit_res["corrupt_count"] == 1
        assert "GeoParquet bbox min > max" in audit_res["corrupt_stages"][0]["reason"]

        with pytest.raises(SilverAuditError, match="validation failed for 1 stage"):
            validate_silver_parquet_metadata(db, airflow_harness.store, fail_on_corrupt=True)
