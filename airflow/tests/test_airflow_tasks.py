from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import httpx
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import pytest
from sqlalchemy import select
from trip_airflow.dbt_gold import (
    discover_silver_parquets,
    materialize_gold_marts,
    validate_gold_marts,
)
from trip_airflow.maintenance_audit import (
    ReconciliationError,
    SilverAuditError,
    reconcile_postgres_and_object_store,
    run_retention_reap,
    validate_silver_parquet_metadata,
)
from trip_airflow.storage_helpers import list_object_keys, object_exists
from trip_airflow.strava_sync import (
    StravaUpstreamError,
    discover_active_strava_connections,
    summarize_strava_sync,
    sync_strava_connection,
)
from trip_api.db import (
    AuditEvent,
    OAuthConnection,
    OAuthState,
    Project,
    RateLimitRecord,
    RenderJob,
    SessionRecord,
    Stage,
)
from trip_api.security import SsrfBlockedError, decrypt_secret, encrypt_secret
from trip_api.services import stage_lod_cache_key
from trip_telemetry import build_silver_dataframe, parse_gpx_bytes, write_silver_parquet

if TYPE_CHECKING:
    from conftest import AirflowTaskHarness

from conftest import SAMPLE_GPX, FakeObjectStore, fake_public_getaddrinfo


def test_dbt_gold_pipeline_empty_store_and_materialization(
    airflow_harness: AirflowTaskHarness,
) -> None:
    empty_summary = discover_silver_parquets(airflow_harness.store)
    assert empty_summary == {
        "silver_count": 0,
        "stage_ids_count": 0,
        "project_ids_count": 0,
    }
    with pytest.raises(ValueError, match="No Silver Parquet"):
        materialize_gold_marts(airflow_harness.store, gold_dir=airflow_harness.gold_dir)

    user = airflow_harness.create_user("gold@example.com")
    proj = airflow_harness.create_project(user.id, "Gold Tour")
    stage1 = airflow_harness.ingest_sample_stage(proj, name="Stage 1")

    gpx2 = SAMPLE_GPX.replace(b"2000-01-01T08:", b"2000-01-02T09:")
    stage2 = airflow_harness.ingest_sample_stage(proj, name="Stage 2", gpx_bytes=gpx2)

    # .lod.json.gz files exist in silver/, but discovery must count only .parquet files
    lod_key = stage_lod_cache_key(proj.id, stage1.id)
    assert object_exists(airflow_harness.store, lod_key)

    discovered = discover_silver_parquets(airflow_harness.store)
    assert discovered == {
        "silver_count": 2,
        "stage_ids_count": 2,
        "project_ids_count": 1,
    }

    mat = materialize_gold_marts(airflow_harness.store, gold_dir=airflow_harness.gold_dir)
    assert mat["status"] == "success"
    assert mat["silver_count"] == 2

    duckdb_file = airflow_harness.gold_dir / "trip_analytics.duckdb"
    assert duckdb_file.is_file()

    validated = validate_gold_marts(
        gold_dir=airflow_harness.gold_dir,
        expected_stage_count=2,
    )
    assert validated["validated"] is True
    assert validated["stage_count"] == 2
    assert validated["daily_row_count"] == 2
    assert validated["total_points"] == stage1.point_count + stage2.point_count
    assert validated["total_distance_m"] > 0.0

    with pytest.raises(ValueError, match="row count"):
        validate_gold_marts(gold_dir=airflow_harness.gold_dir, expected_stage_count=99)


def test_dbt_gold_pipeline_s3_fake_store_parity(tmp_path: Path) -> None:
    fake_store = FakeObjectStore()
    parsed = parse_gpx_bytes(SAMPLE_GPX)
    proj_id = str(uuid.uuid4())
    stage_id = str(uuid.uuid4())
    df, _ = build_silver_dataframe(parsed, trip_id=proj_id, stage_id=stage_id)
    pq_path = write_silver_parquet(df, tmp_path / "stage.parquet")

    silver_key = f"silver/{proj_id}/{stage_id}.parquet"
    fake_store.put_bytes(silver_key, pq_path.read_bytes())
    fake_store.put_bytes(f"silver/{proj_id}/{stage_id}.lod.json.gz", b"gz-cache")

    assert list_object_keys(fake_store, prefix="silver/") == [
        f"silver/{proj_id}/{stage_id}.lod.json.gz",
        silver_key,
    ]
    assert object_exists(fake_store, silver_key)
    assert not object_exists(fake_store, f"silver/{proj_id}/missing.parquet")

    discovered = discover_silver_parquets(fake_store)
    assert discovered == {"silver_count": 1, "stage_ids_count": 1, "project_ids_count": 1}

    gold_dir = tmp_path / "gold_s3"
    materialize_gold_marts(fake_store, gold_dir=gold_dir)
    validated = validate_gold_marts(gold_dir=gold_dir, expected_stage_count=1)
    assert validated["stage_count"] == 1

    # Negative-value check triggers validation failure
    con = duckdb.connect(str(gold_dir / "trip_analytics.duckdb"))
    try:
        con.execute("UPDATE mart_stage_summary SET total_distance_m = -1.0")
    finally:
        con.close()
    with pytest.raises(ValueError, match="invalid/negative"):
        validate_gold_marts(gold_dir=gold_dir, expected_stage_count=1)


def test_strava_incremental_sync_discovery_refresh_dedup_and_ssrf(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    active_user = airflow_harness.create_user("active@example.com", is_active=True)
    inactive_user = airflow_harness.create_user("inactive@example.com", is_active=False)
    key = airflow_harness.settings.decoded_encryption_key

    with airflow_harness.session_factory() as db:
        active_conn = OAuthConnection(
            user_id=active_user.id,
            provider="strava",
            external_athlete_id="9001",
            access_token_enc=encrypt_secret(
                "old-access-secret-tok", key, aad=f"oauth:{active_user.id}:strava".encode()
            ),
            refresh_token_enc=encrypt_secret(
                "old-refresh-secret-tok", key, aad=f"oauth:{active_user.id}:strava".encode()
            ),
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
            scope="read,activity:read_all",
        )
        inactive_conn = OAuthConnection(
            user_id=inactive_user.id,
            provider="strava",
            external_athlete_id="9002",
            access_token_enc=encrypt_secret(
                "inactive-tok", key, aad=f"oauth:{inactive_user.id}:strava".encode()
            ),
            refresh_token_enc=encrypt_secret(
                "inactive-ref", key, aad=f"oauth:{inactive_user.id}:strava".encode()
            ),
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            scope="read",
        )
        other_provider = OAuthConnection(
            user_id=active_user.id,
            provider="garmin",
            external_athlete_id="9003",
            access_token_enc="v1:unused",
            refresh_token_enc="v1:unused",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            scope="read",
        )
        db.add_all([active_conn, inactive_conn, other_provider])
        db.commit()
        active_conn_id = active_conn.id

    with airflow_harness.session_factory() as db:
        discovered = discover_active_strava_connections(db)
    assert discovered == [{"connection_id": str(active_conn_id), "user_id": str(active_user.id)}]
    assert set(discovered[0].keys()) == {"connection_id", "user_id"}

    # Pre-seed a Strava stage in ANOTHER project owned by active_user for cross-project dedup
    manual_proj = airflow_harness.create_project(active_user.id, "Manual Tour")
    airflow_harness.ingest_sample_stage(
        manual_proj, name="Already Imported", strava_activity_id=101
    )

    # SSRF negative tests before monkeypatching DNS
    with airflow_harness.session_factory() as db:
        bad_http = airflow_harness.settings.model_copy(
            update={"strava_api_base_url": "http://www.strava.com"}
        )
        with pytest.raises(SsrfBlockedError):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=bad_http,
                connection_id=active_conn_id,
                user_id=active_user.id,
            )
        bad_loopback = airflow_harness.settings.model_copy(
            update={"strava_api_base_url": "https://127.0.0.1"}
        )
        with pytest.raises(SsrfBlockedError):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=bad_loopback,
                connection_id=active_conn_id,
                user_id=active_user.id,
            )
        bad_metadata = airflow_harness.settings.model_copy(
            update={"strava_api_base_url": "https://169.254.169.254"}
        )
        with pytest.raises(SsrfBlockedError):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=bad_metadata,
                connection_id=active_conn_id,
                user_id=active_user.id,
            )

    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)

    # 302 redirect from Strava upstream must be rejected
    redirect_transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    )
    with airflow_harness.session_factory() as db:
        with pytest.raises(StravaUpstreamError, match="redirect"):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=airflow_harness.settings,
                connection_id=active_conn_id,
                user_id=active_user.id,
                http_client_factory=lambda: httpx.Client(
                    transport=redirect_transport, follow_redirects=False
                ),
            )

    seen_auth_headers: list[str] = []

    def strava_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-access-secret-tok",
                    "refresh_token": "fresh-refresh-secret-tok",
                    "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                },
            )
        seen_auth_headers.append(request.headers.get("Authorization", ""))
        if path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {"id": 101, "name": "Duplicate", "start_latlng": [0.1, 0.2]},
                    {"id": 102, "name": "Indoor Trainer", "start_latlng": []},
                    {"id": 103, "name": "Sample Climb", "start_latlng": [0.12, 0.22]},
                    {"id": 104, "name": "Corrupt Empty Stream", "start_latlng": [0.13, 0.41]},
                ],
            )
        if path == "/api/v3/activities/103":
            return httpx.Response(
                200,
                json={
                    "id": 103,
                    "name": "Sample Climb",
                    "sport_type": "Ride",
                    "start_date": "2001-07-02T07:30:00Z",
                },
            )
        if path == "/api/v3/activities/103/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.12, 0.4], [0.125, 0.405], [0.13, 0.41]]},
                    "time": {"data": [0, 60, 120]},
                    "altitude": {"data": [2000.0, 2050.0, 2110.0]},
                    "heartrate": {"data": [140, 155, 162]},
                },
            )
        if path == "/api/v3/activities/104":
            return httpx.Response(
                200,
                json={"id": 104, "name": "Corrupt Empty Stream", "sport_type": "Hike"},
            )
        if path == "/api/v3/activities/104/streams":
            return httpx.Response(200, json={"latlng": {"data": []}})
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(strava_handler)
    with airflow_harness.session_factory() as db:
        result = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=active_conn_id,
            user_id=active_user.id,
            http_client_factory=lambda: httpx.Client(transport=transport, follow_redirects=False),
        )

    assert result == {
        "connection_id": str(active_conn_id),
        "fetched": 4,
        "skipped_duplicate": 1,
        "skipped_no_gps": 1,
        "ingested_count": 1,
        "failed_count": 1,
    }
    assert all(h == "Bearer fresh-access-secret-tok" for h in seen_auth_headers)
    serialized = json.dumps(result)
    assert "fresh-access-secret-tok" not in serialized
    assert "fresh-refresh-secret-tok" not in serialized

    # Verify refreshed encrypted tokens persisted with AEAD
    with airflow_harness.session_factory() as db:
        refreshed_conn = db.get(OAuthConnection, active_conn_id)
        assert refreshed_conn is not None
        assert (
            decrypt_secret(
                refreshed_conn.access_token_enc,
                key,
                aad=f"oauth:{active_user.id}:strava".encode(),
            )
            == "fresh-access-secret-tok"
        )
        assert (
            decrypt_secret(
                refreshed_conn.refresh_token_enc,
                key,
                aad=f"oauth:{active_user.id}:strava".encode(),
            )
            == "fresh-refresh-secret-tok"
        )

        auto_proj = db.scalar(
            select(Project).where(
                Project.owner_user_id == active_user.id,
                Project.title == "Strava Auto-Sync",
            )
        )
        assert auto_proj is not None
        synced_stage = db.scalar(
            select(Stage).where(
                Stage.project_id == auto_proj.id,
                Stage.strava_activity_id == 103,
            )
        )
        assert synced_stage is not None
        assert object_exists(airflow_harness.store, synced_stage.bronze_object_key)
        assert object_exists(airflow_harness.store, synced_stage.silver_object_key)
        assert object_exists(
            airflow_harness.store, stage_lod_cache_key(auto_proj.id, synced_stage.id)
        )

        audit = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "stage.strava_auto_synced")
        )
        assert audit is not None

    agg = summarize_strava_sync([result])
    assert agg == {
        "connections_processed": 1,
        "fetched": 4,
        "skipped_duplicate": 1,
        "skipped_no_gps": 1,
        "ingested_count": 1,
        "failed_count": 1,
    }


def test_maintenance_reaper_reconciliation_and_parquet_audit(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("ops@example.com")
    proj = airflow_harness.create_project(user.id, "Ops Project")
    stage = airflow_harness.ingest_sample_stage(proj, name="Healthy Stage")

    now = datetime.now(UTC)
    expired_render_key = "outputs/expired.mp4"
    active_render_key = "outputs/active.mp4"
    airflow_harness.store.put_bytes(expired_render_key, b"old-mp4")
    airflow_harness.store.put_bytes(active_render_key, b"good-mp4")

    with airflow_harness.session_factory() as db:
        sess = SessionRecord(
            user_id=user.id,
            token_hash="a" * 64,
            csrf_token="csrf",
            expires_at=now - timedelta(hours=1),
        )
        db.add(sess)
        db.flush()
        db.add(
            OAuthState(
                user_id=user.id,
                session_id=sess.id,
                provider="strava",
                state_hash="b" * 64,
                code_verifier_enc="v1:test",
                expires_at=now - timedelta(minutes=5),
            )
        )
        db.add(
            RateLimitRecord(
                bucket_key="login:old",
                window_start=now - timedelta(hours=48),
                request_count=3,
                updated_at=now - timedelta(hours=48),
            )
        )
        db.add(
            RenderJob(
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="expired-job",
                idempotency_hash="c" * 64,
                spec_json={"version": 2},
                status="succeeded",
                output_object_key=expired_render_key,
                expires_at=now - timedelta(hours=1),
            )
        )
        db.add(
            RenderJob(
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="active-job",
                idempotency_hash="d" * 64,
                spec_json={"version": 2},
                status="succeeded",
                output_object_key=active_render_key,
                expires_at=now + timedelta(days=5),
            )
        )
        db.commit()

    # 1. Retention reap dry_run vs execute
    with airflow_harness.session_factory() as db:
        dry = run_retention_reap(db, airflow_harness.store, airflow_harness.settings, dry_run=True)
        assert dry["dry_run"] is True
        assert dry["sessions_deleted"] == 1
        assert dry["render_jobs_deleted"] == 1
        assert object_exists(airflow_harness.store, expired_render_key)

        reaped = run_retention_reap(
            db, airflow_harness.store, airflow_harness.settings, dry_run=False
        )
        assert reaped["dry_run"] is False
        assert reaped["sessions_deleted"] == 1
        assert reaped["blobs_deleted"] == 1
        assert not object_exists(airflow_harness.store, expired_render_key)
        assert object_exists(airflow_harness.store, active_render_key)

    # 2. Reconciliation: clean state (valid .lod.json.gz must NOT be flagged as orphan)
    with airflow_harness.session_factory() as db:
        clean = reconcile_postgres_and_object_store(db, airflow_harness.store)
        assert clean["missing_bronze_count"] == 0
        assert clean["missing_silver_count"] == 0
        assert clean["missing_render_count"] == 0
        assert clean["orphaned_silver_count"] == 0
        assert clean["orphaned_lod_count"] == 0

    # Add an old orphan and a recent orphan; verify grace period deletion
    old_orphan_key = f"silver/{proj.id}/old-orphan.lod.json.gz"
    recent_orphan_key = f"outputs/{uuid.uuid4()}.mp4"
    airflow_harness.store.put_bytes(old_orphan_key, b"stale-lod")
    airflow_harness.store.put_bytes(recent_orphan_key, b"recent-mp4")
    old_ts = (now - timedelta(hours=48)).timestamp()
    old_path = airflow_harness.store.resolve_existing_path(old_orphan_key)
    os.utime(old_path, (old_ts, old_ts))

    with airflow_harness.session_factory() as db:
        rec = reconcile_postgres_and_object_store(
            db,
            airflow_harness.store,
            delete_orphans=True,
            orphan_min_age_hours=24,
            now=now,
        )
        assert rec["orphaned_lod_count"] == 1
        assert rec["orphaned_outputs_count"] == 1
        assert rec["deleted_orphans_count"] == 1
        assert rec["skipped_recent_orphans_count"] == 1
        assert not object_exists(airflow_harness.store, old_orphan_key)
        assert object_exists(airflow_harness.store, recent_orphan_key)

    # 3. Parquet metadata audit passes on healthy stage
    with airflow_harness.session_factory() as db:
        ok_audit = validate_silver_parquet_metadata(db, airflow_harness.store)
        assert ok_audit == {
            "checked_stages": 1,
            "valid_stages": 1,
            "corrupt_count": 0,
            "corrupt_stages": [],
        }

    # Corrupt Parquet row count mismatch detection
    pq_path = airflow_harness.store.resolve_existing_path(stage.silver_object_key)
    table = pq.read_table(pq_path)
    truncated = pa.Table.from_batches(table.slice(0, 1).to_batches(), schema=table.schema)
    pq.write_table(truncated, pq_path)

    with airflow_harness.session_factory() as db:
        with pytest.raises(SilverAuditError, match="validation failed"):
            validate_silver_parquet_metadata(db, airflow_harness.store, fail_on_corrupt=True)

        audit_row = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "platform.audit_completed")
        )
        assert audit_row is not None

    # Missing Silver blob detection
    airflow_harness.store.delete(stage.silver_object_key)
    with airflow_harness.session_factory() as db:
        with pytest.raises(ReconciliationError, match="missing required object"):
            reconcile_postgres_and_object_store(db, airflow_harness.store, fail_on_missing=True)
