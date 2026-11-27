from __future__ import annotations

import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select
from trip_api.cli import main as cli_main
from trip_api.cli import run_ingest_worker_loop, run_process_ingest_jobs
from trip_api.config import Settings
from trip_api.db import IngestJob, OAuthConnection, Stage, VaultActivity
from trip_api.gold_sync import ensure_local_gold_duckdb, reset_gold_sync_cache
from trip_api.ingest import process_pending_ingest_jobs
from trip_api.security import encrypt_secret
from trip_api.services import (
    build_project_highlights,
    build_project_telemetry_chart,
    get_project_stages,
    stage_chart_cache_key,
    stage_events_cache_key,
)
from trip_object_store import LocalObjectStore, ObjectStream, S3ObjectStore

if TYPE_CHECKING:
    from conftest import HarnessFactory


def _build_duckdb_bytes(tmp_dir: Path, sql: str) -> bytes:
    db_file = tmp_dir / f"src_{uuid.uuid4().hex}.duckdb"
    con = duckdb.connect(str(db_file))
    try:
        con.execute(sql)
        con.execute("CHECKPOINT;")
    finally:
        con.close()
    return db_file.read_bytes()


def _make_gpx(name: str, start_iso: str, *, lat0: float = 0.1, lon0: float = 0.2) -> bytes:
    base_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    pts: list[str] = []
    for idx in range(5):
        ts = (base_dt + timedelta(minutes=idx)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lat = lat0 + idx * 0.002
        lon = lon0 + idx * 0.002
        elev = 1000.0 + idx * 25.0
        pts.append(
            f'<trkpt lat="{lat:.5f}" lon="{lon:.5f}"><ele>{elev:.1f}</ele><time>{ts}</time></trkpt>'
        )
    body = "\n".join(pts)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <trk><name>{name}</name><trkseg>\n{body}\n  </trkseg></trk>\n</gpx>"
    ).encode()


class _FakeS3Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False

    def iter_chunks(self, chunk_size: int = 65536) -> Any:
        for idx in range(0, len(self._payload), chunk_size):
            yield self._payload[idx : idx + chunk_size]

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.head_calls = 0
        self.get_calls = 0
        self.head_error: Exception | None = None
        self.get_error: Exception | None = None

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        self.head_calls += 1
        if self.head_error is not None:
            raise self.head_error
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        payload, etag = self.objects[Key]
        return {"ETag": etag, "ContentLength": len(payload)}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        self.get_calls += 1
        if self.get_error is not None:
            raise self.get_error
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        payload, etag = self.objects[Key]
        return {"Body": _FakeS3Body(payload), "ETag": etag}


def test_gold_sync_s3_etag_atomic_swap_and_404_fallback(tmp_path: Path) -> None:
    reset_gold_sync_cache()
    gold_dir = tmp_path / "ecs_gold"
    cfg = Settings(
        app_env="test",
        object_store_backend="s3",
        gold_output_dir=str(gold_dir),
        gold_sync_interval_s=60.0,
        s3_access_key_id="test",
        s3_secret_access_key="test",
        s3_bucket="trip-test-bucket",
    )
    store = S3ObjectStore(
        endpoint_url="http://localhost:9000",
        access_key_id="test",
        secret_access_key="test",
        bucket="trip-test-bucket",
        region="us-east-1",
    )
    fake_client = _FakeS3Client()
    store.client = fake_client

    path0 = ensure_local_gold_duckdb(store, cfg)
    assert path0 == gold_dir / "trip_analytics.duckdb"
    assert not path0.exists()
    assert fake_client.head_calls == 1
    assert fake_client.get_calls == 0

    v1_bytes = _build_duckdb_bytes(
        tmp_path, "CREATE TABLE t_ver (v INTEGER); INSERT INTO t_ver VALUES (1);"
    )
    fake_client.objects[cfg.gold_s3_key] = (v1_bytes, '"etag-v1"')
    path1 = ensure_local_gold_duckdb(store, cfg)
    assert path1.is_file()
    assert fake_client.head_calls == 2
    assert fake_client.get_calls == 1
    con = duckdb.connect(str(path1), read_only=True)
    try:
        assert con.execute("SELECT v FROM t_ver").fetchone() == (1,)
    finally:
        con.close()

    path_cached = ensure_local_gold_duckdb(store, cfg)
    assert path_cached == path1
    assert fake_client.head_calls == 2
    assert fake_client.get_calls == 1

    cfg_no_ttl = cfg.model_copy(update={"gold_sync_interval_s": 0.0})
    ensure_local_gold_duckdb(store, cfg_no_ttl)
    assert fake_client.head_calls == 3
    assert fake_client.get_calls == 1

    v2_bytes = _build_duckdb_bytes(
        tmp_path, "CREATE TABLE t_ver (v INTEGER); INSERT INTO t_ver VALUES (2);"
    )
    fake_client.objects[cfg.gold_s3_key] = (v2_bytes, '"etag-v2"')
    path2 = ensure_local_gold_duckdb(store, cfg_no_ttl)
    assert fake_client.head_calls == 4
    assert fake_client.get_calls == 2
    con = duckdb.connect(str(path2), read_only=True)
    try:
        assert con.execute("SELECT v FROM t_ver").fetchone() == (2,)
    finally:
        con.close()

    # Corrupt payload or transient S3 error preserves stale valid local file
    fake_client.objects[cfg.gold_s3_key] = (b"NOT_A_DUCKDB_FILE", '"etag-bad"')
    ensure_local_gold_duckdb(store, cfg_no_ttl)
    con = duckdb.connect(str(path2), read_only=True)
    try:
        assert con.execute("SELECT v FROM t_ver").fetchone() == (2,)
    finally:
        con.close()

    fake_client.head_error = RuntimeError("S3 unavailable")
    ensure_local_gold_duckdb(store, cfg_no_ttl)
    con = duckdb.connect(str(path2), read_only=True)
    try:
        assert con.execute("SELECT v FROM t_ver").fetchone() == (2,)
    finally:
        con.close()
    assert not list(gold_dir.glob(".trip_analytics.duckdb.tmp.*"))


def test_heatmap_and_career_year_activity_filters_and_mutated_activity_type(
    make_harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    gold_dir = tmp_path / "gold_data"
    gold_dir.mkdir(parents=True, exist_ok=True)
    h = make_harness(gold_output_dir=str(gold_dir), rate_limit_gpx_max=30)
    h.create_user("alice@example.com")
    alice, csrf = h.login_client("alice@example.com")

    proj1 = alice.post(
        "/api/v1/projects",
        json={"title": "Project One"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    proj2 = alice.post(
        "/api/v1/projects",
        json={"title": "Project Two"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    s1 = alice.post(
        f"/api/v1/projects/{proj1}/gpx",
        files={
            "file": ("s1.gpx", _make_gpx("Stage 1", "2001-06-01T08:00:00Z"), "application/gpx+xml")
        },
        data={"activity_type": "cycling"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    s2 = alice.post(
        f"/api/v1/projects/{proj1}/gpx",
        files={
            "file": ("s2.gpx", _make_gpx("Stage 2", "2002-06-01T08:00:00Z"), "application/gpx+xml")
        },
        data={"activity_type": "cycling"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    s3 = alice.post(
        f"/api/v1/projects/{proj2}/gpx",
        files={
            "file": (
                "s3.gpx",
                _make_gpx("Stage 3", "2001-07-01T08:00:00Z", lat0=0.4, lon0=0.5),
                "application/gpx+xml",
            )
        },
        data={"activity_type": "hiking"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    con = duckdb.connect(str(gold_dir / "trip_analytics.duckdb"))
    try:
        con.execute(
            "CREATE TABLE mart_stage_geo_context "
            "(trip_id VARCHAR, stage_id VARCHAR, primary_country VARCHAR)"
        )
        con.execute(
            "INSERT INTO mart_stage_geo_context VALUES "
            "(?, ?, 'Country One'), (?, ?, 'Country Two'), (?, ?, 'Country Three')",
            [proj1, s1, proj1, s2, proj2, s3],
        )
        con.execute("CREATE TABLE mart_stage_summary (trip_id VARCHAR, stage_id VARCHAR)")
        con.execute(
            "INSERT INTO mart_stage_summary VALUES (?, ?), (?, ?), (?, ?)",
            [proj1, s1, proj1, s2, proj2, s3],
        )
        con.execute(
            "CREATE TABLE mart_spatial_coverage_h3 "
            "(trip_id VARCHAR, stage_id VARCHAR, year INTEGER, h3_index VARCHAR, "
            "center_lat DOUBLE, center_lon DOUBLE, point_count BIGINT, stage_count INTEGER)"
        )
        con.execute(
            "INSERT INTO mart_spatial_coverage_h3 VALUES "
            "(?, ?, 2001, 'hex_1_2', 10.0, 20.0, 30, 1), "
            "(?, ?, 2002, 'hex_1_2', 14.0, 24.0, 10, 1), "
            "(?, ?, 2001, 'hex_3_4', 40.0, 50.0, 50, 1)",
            [proj1, s1, proj1, s2, proj2, s3],
        )
    finally:
        con.close()

    hm_all = alice.get("/api/v1/analytics/heatmap?lod=low")
    assert hm_all.status_code == 200
    cells_all = {c["h3_index"]: c for c in hm_all.json()["hex_cells"]}
    assert cells_all["hex_1_2"]["point_count"] == 40
    assert cells_all["hex_1_2"]["stage_count"] == 2
    assert cells_all["hex_1_2"]["lat"] == pytest.approx(11.0)
    assert cells_all["hex_1_2"]["lon"] == pytest.approx(21.0)
    assert cells_all["hex_3_4"]["point_count"] == 50

    hm_2001 = alice.get("/api/v1/analytics/heatmap?year=2001")
    assert hm_2001.status_code == 200
    cells_2001 = {c["h3_index"]: c for c in hm_2001.json()["hex_cells"]}
    assert cells_2001["hex_1_2"]["point_count"] == 30
    assert cells_2001["hex_1_2"]["stage_count"] == 1
    assert cells_2001["hex_1_2"]["lat"] == pytest.approx(10.0)
    assert cells_2001["hex_3_4"]["point_count"] == 50

    career_2002 = alice.get("/api/v1/analytics/career?year=2002")
    assert career_2002.status_code == 200
    assert career_2002.json()["countries_visited"] == ["Country Two"]

    patch_resp = alice.patch(
        f"/api/v1/projects/{proj1}/stages/{s1}",
        json={"activity_type": "trail_run"},
        headers={"X-CSRF-Token": csrf},
    )
    assert patch_resp.status_code == 200

    hm_trail = alice.get("/api/v1/analytics/heatmap?year=2001&activity_type=trail_run")
    assert hm_trail.status_code == 200
    assert [c["h3_index"] for c in hm_trail.json()["hex_cells"]] == ["hex_1_2"]
    assert hm_trail.json()["hex_cells"][0]["point_count"] == 30

    hm_cyc_2001 = alice.get("/api/v1/analytics/heatmap?year=2001&activity_type=cycling")
    assert hm_cyc_2001.status_code == 200
    assert hm_cyc_2001.json()["hex_cells"] == []

    career_trail = alice.get("/api/v1/analytics/career?year=2001&activity_type=trail_run")
    assert career_trail.status_code == 200
    assert career_trail.json()["countries_visited"] == ["Country One"]


class _SpyObjectStore:
    """ObjectStore wrapper tracking object reads without using LocalObjectStore fast-path."""

    def __init__(self, backing: LocalObjectStore) -> None:
        self._backing = backing
        self.read_keys: list[str] = []

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self._backing.put_bytes(key, data, content_type=content_type)

    def get_bytes(self, key: str) -> bytes:
        self.read_keys.append(key)
        return self._backing.get_bytes(key)

    def get_file(self, key: str, dest: Path) -> None:
        self.read_keys.append(key)
        self._backing.get_file(key, dest)

    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = 65536,
    ) -> ObjectStream:
        return self._backing.open_stream(key, range_header=range_header, chunk_size=chunk_size)

    def copy(self, src_key: str, dst_key: str, *, content_type: str | None = None) -> None:
        self._backing.copy(src_key, dst_key, content_type=content_type)

    def delete(self, key: str) -> None:
        self._backing.delete(key)


def test_lazy_silver_materialization_on_cache_hit_chart_and_highlights(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness(rate_limit_gpx_max=20)
    h.create_user("alice@example.com")
    alice, csrf = h.login_client("alice@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "Multi Stage Cache Test"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={
            "file": ("s1.gpx", _make_gpx("Stage 1", "2001-01-01T08:00:00Z"), "application/gpx+xml")
        },
        headers={"X-CSRF-Token": csrf},
    )
    alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={
            "file": ("s2.gpx", _make_gpx("Stage 2", "2001-01-02T08:00:00Z"), "application/gpx+xml")
        },
        headers={"X-CSRF-Token": csrf},
    )

    proj_uuid = uuid.UUID(proj_id)
    with h.session_factory() as db:
        stages = get_project_stages(db, proj_uuid)
    assert len(stages) == 2

    spy = _SpyObjectStore(h.store)

    # Full cache hit with per_stage_budget=50 (< 1200) must read 0 Silver Parquets
    chart = build_project_telemetry_chart(spy, proj_uuid, stages, max_points=100)
    assert len(chart.points) > 0
    silver_keys = {st.silver_object_key for st in stages}
    assert not (set(spy.read_keys) & silver_keys)

    spy.read_keys.clear()
    highlights = build_project_highlights(spy, proj_uuid, stages)
    assert len(highlights.geo_context) == 2
    assert not (set(spy.read_keys) & silver_keys)

    s0_chart_key = stage_chart_cache_key(proj_uuid, stages[0].id)
    s0_events_key = stage_events_cache_key(proj_uuid, stages[0].id)
    h.store.delete(s0_chart_key)
    h.store.delete(s0_events_key)

    spy.read_keys.clear()
    chart_after = build_project_telemetry_chart(spy, proj_uuid, stages, max_points=100)
    assert len(chart_after.points) > 0
    assert stages[0].silver_object_key in spy.read_keys
    assert stages[1].silver_object_key not in spy.read_keys
    assert h.store.resolve_existing_path(s0_chart_key).is_file()

    spy.read_keys.clear()
    build_project_highlights(spy, proj_uuid, stages)
    assert stages[0].silver_object_key in spy.read_keys
    assert stages[1].silver_object_key not in spy.read_keys
    assert h.store.resolve_existing_path(s0_events_key).is_file()

    spy.read_keys.clear()
    build_project_telemetry_chart(spy, proj_uuid, stages, max_points=100)
    build_project_highlights(spy, proj_uuid, stages)
    assert not (set(spy.read_keys) & silver_keys)


def test_ingest_cli_loop_max_attempts_and_throttled_backoff(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.69", 443))],
    )
    h = make_harness()
    user = h.create_user("worker@example.com")
    key = h.settings.decoded_encryption_key
    aad = f"oauth:{user.id}:strava".encode()
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)

    with h.session_factory() as db:
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="998877",
                access_token_enc=encrypt_secret("acc", key, aad=aad),
                refresh_token_enc=encrypt_secret("ref", key, aad=aad),
                expires_at=now + timedelta(hours=4),
            )
        )
        poison = IngestJob(
            provider="strava",
            event_type="create",
            external_owner_id="998877",
            external_object_id=1001,
            payload_json={},
            status="queued",
            attempt_count=5,
        )
        job1 = IngestJob(
            provider="strava",
            event_type="create",
            external_owner_id="998877",
            external_object_id=1002,
            payload_json={},
            status="queued",
        )
        job2 = IngestJob(
            provider="strava",
            event_type="create",
            external_owner_id="998877",
            external_object_id=1003,
            payload_json={},
            status="queued",
        )
        db.add_all([poison, job1, job2])
        db.commit()
        poison_id = poison.id
        job2_id = job2.id

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/activities/1002"):
            return httpx.Response(
                200,
                json={
                    "id": 1002,
                    "name": "First Activity",
                    "sport_type": "Ride",
                    "start_date": "2026-05-01T08:00:00Z",
                    "start_latlng": [0.1, 0.2],
                },
                headers={"X-RateLimit-Usage": "95,100", "X-RateLimit-Limit": "100,1000"},
            )
        if request.url.path.endswith("/activities/1003"):
            return httpx.Response(
                200,
                json={
                    "id": 1003,
                    "name": "Second Activity",
                    "sport_type": "Ride",
                    "start_date": "2026-05-01T10:00:00Z",
                    "start_latlng": [0.1, 0.2],
                },
                headers={"X-RateLimit-Usage": "10,100", "X-RateLimit-Limit": "100,1000"},
            )
        if request.url.path.endswith("/streams"):
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.10, 0.20], [0.11, 0.21]]},
                    "time": {"data": [0, 30]},
                    "altitude": {"data": [100.0, 110.0]},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_mock_transport)
    with h.session_factory() as db:
        res1 = process_pending_ingest_jobs(
            db,
            h.store,
            h.settings,
            max_attempts=5,
            retry_backoff_seconds=60,
            http_client_factory=lambda: httpx.Client(transport=transport),
            now=now,
        )
        assert res1 == {"claimed": 2, "succeeded": 1, "ignored": 0, "failed": 0, "deferred": 1}

        poison_row = db.get(IngestJob, poison_id)
        assert poison_row is not None
        assert poison_row.status == "failed"
        assert poison_row.error_message is not None

        deferred_row = db.get(IngestJob, job2_id)
        assert deferred_row is not None
        assert deferred_row.status == "queued"
        assert deferred_row.attempt_count == 0
        assert deferred_row.lease_expires_at is not None

        res_early = process_pending_ingest_jobs(
            db,
            h.store,
            h.settings,
            max_attempts=5,
            http_client_factory=lambda: httpx.Client(transport=transport),
            now=now + timedelta(seconds=10),
        )
        assert res_early["claimed"] == 0

        res_late = process_pending_ingest_jobs(
            db,
            h.store,
            h.settings,
            max_attempts=5,
            http_client_factory=lambda: httpx.Client(transport=transport),
            now=now + timedelta(seconds=61),
        )
        assert res_late["succeeded"] == 1

    with h.session_factory() as db:
        db.add(
            IngestJob(
                provider="strava",
                event_type="delete",
                external_owner_id="unknown-owner",
                external_object_id=9999,
                payload_json={},
                status="queued",
            )
        )
        db.commit()

    monkeypatch.setenv("LOCAL_OBJECT_STORE_DIR", str(h.store.root_dir))
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "local")
    db_url = f"sqlite:///{h.db_path}"

    dry_rep = run_process_ingest_jobs(h.settings, dry_run=True, database_url=db_url)
    assert dry_rep.dry_run is True
    assert dry_rep.matched_jobs == 1

    heartbeats: list[str] = []
    outcomes: list[dict[str, int]] = []
    monkeypatch.setattr(
        "trip_api.cli.start_service_heartbeat",
        lambda service, **kwargs: (heartbeats.append(service), kwargs["stop_event"])[1],
    )
    monkeypatch.setattr(
        "trip_api.cli.record_ingest_outcome",
        lambda **kwargs: outcomes.append(kwargs),
    )

    stop_ev = threading.Event()
    loop_rep = run_ingest_worker_loop(
        h.settings,
        database_url=db_url,
        poll_interval_s=0.05,
        stop_event=stop_ev,
        max_iterations=1,
    )
    assert loop_rep.matched_jobs == 1
    assert loop_rep.ignored_jobs == 1
    assert heartbeats == ["ingest"]
    assert outcomes == [{"claimed": 1, "succeeded": 0, "ignored": 1, "failed": 0, "deferred": 0}]

    assert cli_main(["process-ingest-jobs", "--dry-run", "--json", "--database-url", db_url]) == 0
    with h.session_factory() as db:
        assert len(db.scalars(select(Stage)).all()) == 0
        assert len(db.scalars(select(VaultActivity)).all()) == 2
