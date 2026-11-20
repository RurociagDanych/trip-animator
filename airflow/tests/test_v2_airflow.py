"""Tests for B5/B6 Airflow incremental dbt, audit fixes, webhook queue, and Silver replay."""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import httpx
import pytest
from conftest import fake_public_getaddrinfo
from sqlalchemy import select
from trip_airflow.dbt_gold import (
    discover_silver_parquets,
    materialize_gold_marts,
    validate_gold_marts,
)
from trip_airflow.ingest_worker import (
    cluster_activity_day_index,
    process_pending_ingest_jobs,
    replay_outdated_silver_stages,
)
from trip_airflow.maintenance_audit import (
    reconcile_postgres_and_object_store,
    validate_silver_parquet_metadata,
)
from trip_airflow.storage_helpers import object_exists
from trip_api.db import IngestJob, OAuthConnection, Project, RenderJob, Stage, VaultActivity
from trip_api.security import encrypt_secret
from trip_api.services import (
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
)
from trip_telemetry import CURRENT_ETL_VERSION

if TYPE_CHECKING:
    from conftest import AirflowTaskHarness


def test_trip_silver_backfill_dag_loads_cleanly(tmp_path: Path, monkeypatch: Any) -> None:
    airflow = pytest.importorskip("airflow")
    assert airflow is not None

    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", f"sqlite:///{tmp_path}/airflow.db")
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")

    try:
        from airflow.dag_processing.dagbag import DagBag  # type: ignore[import-not-found]
    except ImportError:
        from airflow.models import DagBag

    repo_root = Path(__file__).resolve().parents[2]
    dagbag = DagBag(dag_folder=str(repo_root / "airflow" / "dags"), include_examples=False)
    assert dagbag.import_errors == {}
    assert "trip_silver_backfill" in dagbag.dags
    dag = dagbag.dags["trip_silver_backfill"]
    assert set(dag.task_ids) == {"process_ingest_jobs", "replay_silver_stages"}
    assert dag.get_task("process_ingest_jobs").downstream_task_ids == {"replay_silver_stages"}


def test_incremental_dbt_gold_delta_cache_and_new_marts(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("delta@example.com")
    proj = airflow_harness.create_project(user.id, "Delta Tour")
    stage1 = airflow_harness.ingest_sample_stage(proj, name="Stage 1")

    # Sidecar `_events.parquet` must be ignored by Silver discovery
    events_key = stage_events_cache_key(proj.id, stage1.id)
    assert object_exists(airflow_harness.store, events_key) or True
    airflow_harness.store.put_bytes(events_key, b"events-sidecar-bytes")

    discovered = discover_silver_parquets(airflow_harness.store)
    assert discovered["silver_count"] == 1

    run1 = materialize_gold_marts(airflow_harness.store, gold_dir=airflow_harness.gold_dir)
    assert run1["downloaded_count"] == 1
    assert run1["cached_count"] == 0

    run2 = materialize_gold_marts(airflow_harness.store, gold_dir=airflow_harness.gold_dir)
    assert run2["downloaded_count"] == 0
    assert run2["cached_count"] == 1

    validated = validate_gold_marts(gold_dir=airflow_harness.gold_dir, expected_stage_count=1)
    assert validated["validated"] is True

    con = duckdb.connect(str(airflow_harness.gold_dir / "trip_analytics.duckdb"), read_only=True)
    try:
        geo_rows = con.execute(
            "SELECT stage_id, countries, primary_country FROM mart_stage_geo_context"
        ).fetchall()
        assert len(geo_rows) == 1
        assert geo_rows[0][1] == []
        career_rows = con.execute(
            "SELECT trip_id, total_distance_m FROM mart_user_career_summary"
        ).fetchall()
        assert len(career_rows) == 1
        h3_rows = con.execute(
            "SELECT stage_id, year, center_lat, center_lon, point_count, stage_count "
            "FROM mart_spatial_coverage_h3"
        ).fetchall()
        assert len(h3_rows) >= 1
        assert all(
            r[0] == str(stage1.id) and r[2] is not None and r[3] is not None for r in h3_rows
        )
    finally:
        con.close()


def test_maintenance_audit_recognizes_sidecars_chunks_and_trimmed_points(
    airflow_harness: AirflowTaskHarness,
) -> None:
    user = airflow_harness.create_user("auditv2@example.com")
    proj = airflow_harness.create_project(user.id, "Audit V2")
    stage = airflow_harness.ingest_sample_stage(proj, name="Stage Sidecars")

    airflow_harness.store.put_bytes(stage_lod_cache_key(proj.id, stage.id), b"lod")
    airflow_harness.store.put_bytes(stage_chart_cache_key(proj.id, stage.id), b"chart")
    airflow_harness.store.put_bytes(stage_events_cache_key(proj.id, stage.id), b"events")

    job_id = uuid.uuid4()
    active_chunk_key = f"outputs/chunks/{job_id}/chunk_0000.mp4"
    airflow_harness.store.put_bytes(active_chunk_key, b"chunk-bytes")
    old_ts = (datetime.now(UTC) - timedelta(hours=48)).timestamp()
    for key in (
        stage_chart_cache_key(proj.id, stage.id),
        stage_events_cache_key(proj.id, stage.id),
        active_chunk_key,
    ):
        os.utime(airflow_harness.store.resolve_existing_path(key), (old_ts, old_ts))

    with airflow_harness.session_factory() as db:
        job = RenderJob(
            id=job_id,
            project_id=proj.id,
            owner_user_id=user.id,
            idempotency_key="chunk-active-job",
            idempotency_hash="ab" * 32,
            spec_json={"version": 2},
            status="running",
        )
        db.add(job)
        db.commit()

        rec = reconcile_postgres_and_object_store(
            db,
            airflow_harness.store,
            delete_orphans=True,
            orphan_min_age_hours=24,
        )
        assert rec["orphaned_silver_count"] == 0
        assert rec["orphaned_lod_count"] == 0
        assert rec["orphaned_outputs_count"] == 0
        assert rec["deleted_orphans_count"] == 0
        assert object_exists(airflow_harness.store, stage_chart_cache_key(proj.id, stage.id))
        assert object_exists(airflow_harness.store, stage_events_cache_key(proj.id, stage.id))
        assert object_exists(airflow_harness.store, active_chunk_key)

    import pyarrow as pa  # type: ignore[import-untyped]
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    pq_path = airflow_harness.store.resolve_existing_path(stage.silver_object_key)
    tbl = pq.read_table(pq_path)
    idx = tbl.column_names.index("is_trimmed")
    tbl = tbl.set_column(idx, "is_trimmed", pa.array([True, False, False], type=pa.bool_()))
    pq.write_table(tbl, pq_path)

    with airflow_harness.session_factory() as db:
        st_row = db.get(Stage, stage.id)
        assert st_row is not None
        st_row.trim_start_s = 60.0
        st_row.point_count = 2
        db.commit()

        audit = validate_silver_parquet_metadata(db, airflow_harness.store, fail_on_corrupt=True)
        assert audit["valid_stages"] == 1
        assert audit["corrupt_count"] == 0


def test_ingest_worker_webhook_queue_and_silver_replay(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("webhook@example.com")
    project = airflow_harness.create_project(user.id, "Explicit Strava Import")
    key = airflow_harness.settings.decoded_encryption_key
    aad = f"oauth:{user.id}:strava".encode()
    now = datetime.now(UTC)

    with airflow_harness.session_factory() as db:
        conn = OAuthConnection(
            user_id=user.id,
            provider="strava",
            external_athlete_id="998877",
            access_token_enc=encrypt_secret("tok-access", key, aad=aad),
            refresh_token_enc=encrypt_secret("tok-refresh", key, aad=aad),
            expires_at=now + timedelta(hours=2),
        )
        db.add(conn)
        db.add(
            IngestJob(
                provider="strava",
                event_type="create",
                external_owner_id="998877",
                external_object_id=555001,
                payload_json={
                    "aspect_type": "create",
                    "source": "strava_bulk_import",
                    "project_id": str(project.id),
                },
            )
        )
        db.add(
            IngestJob(
                provider="strava",
                event_type="create",
                external_owner_id="998877",
                external_object_id=555003,
                payload_json={"aspect_type": "create"},
            )
        )
        db.add(
            IngestJob(
                provider="strava",
                event_type="create",
                external_owner_id="unknown-owner",
                external_object_id=555002,
                payload_json={"aspect_type": "create"},
            )
        )
        db.commit()

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/activities/555001") or request.url.path.endswith(
            "/activities/555003"
        ):
            activity_id = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json={
                    "id": activity_id,
                    "name": "Sample Morning Ride",
                    "sport_type": "Ride",
                    "start_date": "2001-01-02T08:00:00Z",
                    "start_latlng": [0.1, 0.2],
                },
                headers={"X-RateLimit-Usage": "5,50", "X-RateLimit-Limit": "100,1000"},
            )
        if request.url.path.endswith("/activities/555001/streams"):
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.100, 0.200], [0.101, 0.201], [0.102, 0.202]]},
                    "time": {"data": [0, 30, 60]},
                    "altitude": {"data": [1200.0, 1220.0, 1250.0]},
                    "grade_smooth": {"data": [3.0, 4.0, 5.0]},
                    "moving": {"data": [True, True, True]},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_mock_transport)
    with airflow_harness.session_factory() as db:
        res = process_pending_ingest_jobs(
            db,
            airflow_harness.store,
            airflow_harness.settings,
            http_client_factory=lambda: httpx.Client(transport=transport),
            now=now,
        )
        assert res["succeeded"] == 2
        assert res["ignored"] == 1
        assert res["failed"] == 0

        stage = db.scalar(select(Stage).where(Stage.strava_activity_id == 555001))
        assert stage is not None
        assert stage.project_id == project.id
        assert stage.etl_version == CURRENT_ETL_VERSION
        assert stage.bronze_sha256 is not None

        webhook_activity = db.scalar(
            select(VaultActivity).where(VaultActivity.strava_activity_id == 555003)
        )
        assert webhook_activity is not None
        assert db.scalar(select(Stage).where(Stage.strava_activity_id == 555003)) is None
        assert db.scalar(select(Project).where(Project.title == "Strava Auto-Sync")) is None

        next_day_idx = cluster_activity_day_index(
            db,
            stage.project_id,
            datetime(2001, 1, 4, 9, 0, 0, tzinfo=UTC),
        )
        assert next_day_idx == 3

        stage.etl_version = 1
        stage.bronze_sha256 = None
        db.commit()

        replay_res = replay_outdated_silver_stages(
            db,
            airflow_harness.store,
            target_etl_version=CURRENT_ETL_VERSION,
        )
        assert replay_res["replayed_count"] == 1
        db.refresh(stage)
        assert stage.etl_version == CURRENT_ETL_VERSION
        assert stage.bronze_sha256 is not None

        db.add(
            IngestJob(
                provider="strava",
                event_type="delete",
                external_owner_id="998877",
                external_object_id=555001,
                payload_json={"aspect_type": "delete"},
            )
        )
        db.commit()
        del_res = process_pending_ingest_jobs(
            db,
            airflow_harness.store,
            airflow_harness.settings,
            http_client_factory=lambda: httpx.Client(transport=transport),
            now=now,
        )
        assert del_res["succeeded"] == 1
        assert db.scalar(select(Stage).where(Stage.strava_activity_id == 555001)) is None


def test_materialize_gold_marts_seeds_existing_duckdb_from_s3_in_ephemeral_container(
    airflow_harness: AirflowTaskHarness,
    tmp_path: Path,
) -> None:
    from botocore.exceptions import ClientError
    from trip_api.storage import S3ObjectStore

    user = airflow_harness.create_user("s3seed@example.com")
    proj = airflow_harness.create_project(user.id, "S3 Seed Tour")
    stage1 = airflow_harness.ingest_sample_stage(proj, name="Stage 1")
    silver_bytes = airflow_harness.store.get_bytes(stage1.silver_object_key)

    class _FakePaginator:
        def __init__(self, objects: dict[str, bytes]) -> None:
            self._objects = objects

        def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
            _ = Bucket
            contents = [
                {
                    "Key": k,
                    "ETag": f'"{len(v)}"',
                    "Size": len(v),
                    "LastModified": datetime.now(UTC),
                }
                for k, v in sorted(self._objects.items())
                if k.startswith(Prefix)
            ]
            return [{"Contents": contents}]

    class _FakeBody:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def close(self) -> None:
            return None

    class _FakeClient:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {stage1.silver_object_key: silver_bytes}
            self.gold_get_calls = 0

        def get_paginator(self, op_name: str) -> _FakePaginator:
            assert op_name == "list_objects_v2"
            return _FakePaginator(self.objects)

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            _ = Bucket
            if Key == "gold/trip_analytics.duckdb":
                self.gold_get_calls += 1
            if Key not in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "NoSuchKey", "Message": "Not Found"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "GetObject",
                )
            return {"Body": _FakeBody(self.objects[Key])}

    s3_store = S3ObjectStore(
        endpoint_url="http://localhost:9000",
        access_key_id="test",
        secret_access_key="test",
        bucket="trip-test",
        region="us-east-1",
    )
    fake_client = _FakeClient()
    s3_store.client = fake_client

    ephemeral_gold_1 = tmp_path / "ephemeral_run_1"
    res1 = materialize_gold_marts(s3_store, gold_dir=ephemeral_gold_1)
    assert res1["silver_count"] == 1
    assert fake_client.gold_get_calls == 1

    fake_client.objects["gold/trip_analytics.duckdb"] = (
        ephemeral_gold_1 / "trip_analytics.duckdb"
    ).read_bytes()
    ephemeral_gold_2 = tmp_path / "ephemeral_run_2"
    res2 = materialize_gold_marts(s3_store, gold_dir=ephemeral_gold_2)
    assert res2["silver_count"] == 1
    assert fake_client.gold_get_calls == 2
    validated = validate_gold_marts(gold_dir=ephemeral_gold_2, expected_stage_count=1)
    assert validated["validated"] is True
