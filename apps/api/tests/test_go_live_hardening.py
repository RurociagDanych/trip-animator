from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from trip_api.cli import run_ingest_worker_loop
from trip_api.config import Settings
from trip_api.credits import grant_user_credits
from trip_api.db import Base, RenderJob, SessionRecord, User
from trip_api.main import create_app
from trip_api.remote_renders import process_remote_renders_once
from trip_api.render_backends import RemoteExecutionRef, RemoteExecutionStatus, RemoteSubmitRequest
from trip_api.render_events import RenderEventHub
from trip_object_store import LocalObjectStore

_VALID_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>Stage One</name>
    <trkseg>
      <trkpt lat="46.5000" lon="7.9000"><ele>1000</ele><time>2026-06-01T08:00:00Z</time></trkpt>
      <trkpt lat="46.5100" lon="7.9100"><ele>1100</ele><time>2026-06-01T08:10:00Z</time></trkpt>
      <trkpt lat="46.5200" lon="7.9200"><ele>1200</ele><time>2026-06-01T08:20:00Z</time></trkpt>
      <trkpt lat="46.5300" lon="7.9300"><ele>1300</ele><time>2026-06-01T08:30:00Z</time></trkpt>
    </trkseg>
  </trk>
</gpx>
"""


def _build_env(
    tmp_path: Path,
    *,
    render_backend: str = "local",
    max_projects_per_user: int = 25,
    max_stages_per_project: int = 30,
    max_active_renders_per_user: int = 1,
    server_render_max_duration_s: float = 120.0,
    server_render_max_fps_local: int = 30,
    server_render_max_fps_remote: int = 60,
    render_remote_max_inflight: int = 3,
    backend_override: Any = None,
) -> tuple[TestClient, sessionmaker[Any], LocalObjectStore, Settings]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    store = LocalObjectStore(tmp_path / "objects")
    settings = Settings(
        app_env="test",
        dev_auth_enabled=True,
        database_url="sqlite+pysqlite:///:memory:",
        object_store_backend="local",
        local_object_store_dir=str(tmp_path / "objects"),
        render_backend=render_backend,
        render_remote_dispatch_interval_s=0.0,
        max_projects_per_user=max_projects_per_user,
        max_stages_per_project=max_stages_per_project,
        max_active_renders_per_user=max_active_renders_per_user,
        server_render_max_duration_s=server_render_max_duration_s,
        server_render_max_fps_local=server_render_max_fps_local,
        server_render_max_fps_remote=server_render_max_fps_remote,
        render_remote_max_inflight=render_remote_max_inflight,
    )
    app = create_app(
        settings=settings,
        engine=engine,
        session_factory=sf,
        object_store=store,
        render_backend=backend_override,
    )
    return TestClient(app), sf, store, settings


def _dev_login(
    client: TestClient, sf: sessionmaker[Any], *, credits: int = 500
) -> tuple[str, uuid.UUID]:
    r = client.post("/api/v1/auth/dev-login")
    assert r.status_code == 200
    csrf = r.json()["csrf_token"]
    user_id = uuid.UUID(r.json()["user"]["id"])
    email = r.json()["user"]["email"]
    if credits > 0:
        with sf() as db:
            grant_user_credits(db, email=email, delta=credits, note="test")
    return csrf, user_id


class _FakeRemoteBackend:
    name = "aws_sfn_lambda"
    is_remote = True

    def __init__(self) -> None:
        self.submitted: list[uuid.UUID] = []
        self.statuses: dict[str, RemoteExecutionStatus] = {}

    def submit(self, req: RemoteSubmitRequest) -> RemoteExecutionRef:
        self.submitted.append(req.job_id)
        exec_id = f"arn:aws:states:eu-central-1:000000000000:execution:test:{req.job_id}"
        self.statuses[exec_id] = RemoteExecutionStatus(state="running", progress_pct=10)
        return RemoteExecutionRef(
            execution_id=exec_id,
            staging_prefix=f"staging/{req.job_id}/",
        )

    def get_status(self, execution_id: str) -> RemoteExecutionStatus:
        return self.statuses.get(
            execution_id, RemoteExecutionStatus(state="running", progress_pct=10)
        )

    def cancel(self, execution_id: str) -> None:
        self.statuses[execution_id] = RemoteExecutionStatus(state="cancelled")

    def download_output(self, execution_id: str, staging_key: str, dest: Path) -> None:
        dest.write_bytes(b"fake-mp4")

    def cleanup_staging(self, job_id: uuid.UUID) -> None:
        return None


def test_project_and_stage_quotas_enforced(tmp_path: Path) -> None:
    client, sf, _store, _settings = _build_env(
        tmp_path,
        max_projects_per_user=2,
        max_stages_per_project=2,
    )
    csrf, _uid = _dev_login(client, sf)
    headers = {"X-CSRF-Token": csrf}

    p1 = client.post("/api/v1/projects", json={"title": "Trip 1"}, headers=headers)
    assert p1.status_code == 201
    p2 = client.post("/api/v1/projects", json={"title": "Trip 2"}, headers=headers)
    assert p2.status_code == 201
    p3 = client.post("/api/v1/projects", json={"title": "Trip 3"}, headers=headers)
    assert p3.status_code == 422
    assert "Maximum of 2 projects per user" in p3.json()["detail"]

    project_id = p1.json()["id"]
    s1 = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("s1.gpx", _VALID_GPX, "application/gpx+xml")},
        headers=headers,
    )
    assert s1.status_code == 201
    s2 = client.post(
        f"/api/v1/projects/{project_id}/tracks",
        files={"file": ("s2.gpx", _VALID_GPX, "application/gpx+xml")},
        headers=headers,
    )
    assert s2.status_code == 201
    s3 = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("s3.gpx", _VALID_GPX, "application/gpx+xml")},
        headers=headers,
    )
    assert s3.status_code == 422
    assert "Maximum of 2 stages per project" in s3.json()["detail"]

    split_resp = client.post(
        f"/api/v1/projects/{project_id}/stages/{s1.json()['id']}/split",
        json={"split_elapsed_s": 600.0},
        headers=headers,
    )
    assert split_resp.status_code == 422
    assert "Maximum of 2 stages per project" in split_resp.json()["detail"]


def test_active_render_quota_and_server_render_caps(tmp_path: Path) -> None:
    client, sf, _store, _settings = _build_env(
        tmp_path,
        render_backend="local",
        max_active_renders_per_user=1,
        server_render_max_duration_s=120.0,
        server_render_max_fps_local=30,
        server_render_max_fps_remote=60,
    )
    csrf, _uid = _dev_login(client, sf, credits=1000)
    headers = {"X-CSRF-Token": csrf}

    proj = client.post("/api/v1/projects", json={"title": "Caps Trip"}, headers=headers).json()
    pid = proj["id"]
    assert (
        client.post(
            f"/api/v1/projects/{pid}/gpx",
            files={"file": ("s1.gpx", _VALID_GPX, "application/gpx+xml")},
            headers=headers,
        ).status_code
        == 201
    )

    r_dur_quote = client.post(
        f"/api/v1/projects/{pid}/renders/quote",
        json={"duration_seconds": 150.0, "fps": 30},
    )
    assert r_dur_quote.status_code == 422

    r_fps_quote = client.post(
        f"/api/v1/projects/{pid}/renders/quote",
        json={"duration_seconds": 30.0, "fps": 60},
    )
    assert r_fps_quote.status_code == 422

    r_fps_submit = client.post(
        f"/api/v1/projects/{pid}/renders",
        json={"duration_seconds": 30.0, "fps": 60},
        headers={**headers, "Idempotency-Key": "idem-cap-fps-0001"},
    )
    assert r_fps_submit.status_code == 422

    r1 = client.post(
        f"/api/v1/projects/{pid}/renders",
        json={"duration_seconds": 30.0, "fps": 30},
        headers={**headers, "Idempotency-Key": "idem-active-0001"},
    )
    assert r1.status_code == 202
    job1_id = r1.json()["id"]

    r1_replay = client.post(
        f"/api/v1/projects/{pid}/renders",
        json={"duration_seconds": 30.0, "fps": 30},
        headers={**headers, "Idempotency-Key": "idem-active-0001"},
    )
    assert r1_replay.status_code == 202
    assert r1_replay.json()["id"] == job1_id

    r2 = client.post(
        f"/api/v1/projects/{pid}/renders",
        json={"duration_seconds": 30.0, "fps": 30},
        headers={**headers, "Idempotency-Key": "idem-active-0002"},
    )
    assert r2.status_code == 429

    assert client.post(f"/api/v1/renders/{job1_id}/cancel", headers=headers).status_code == 200

    r3 = client.post(
        f"/api/v1/projects/{pid}/renders",
        json={"duration_seconds": 30.0, "fps": 30},
        headers={**headers, "Idempotency-Key": "idem-active-0003"},
    )
    assert r3.status_code == 202


def test_remote_backend_allows_60fps_and_enforces_max_inflight(tmp_path: Path) -> None:
    fake_backend = _FakeRemoteBackend()
    client, sf, store, settings = _build_env(
        tmp_path,
        render_backend="aws_sfn_lambda",
        max_active_renders_per_user=10,
        server_render_max_fps_local=30,
        server_render_max_fps_remote=60,
        render_remote_max_inflight=2,
        backend_override=fake_backend,
    )
    csrf, _uid = _dev_login(client, sf, credits=5000)
    headers = {"X-CSRF-Token": csrf}

    proj = client.post("/api/v1/projects", json={"title": "Remote Trip"}, headers=headers).json()
    pid = proj["id"]
    assert (
        client.post(
            f"/api/v1/projects/{pid}/gpx",
            files={"file": ("s1.gpx", _VALID_GPX, "application/gpx+xml")},
            headers=headers,
        ).status_code
        == 201
    )

    job_ids: list[str] = []
    for idx in range(3):
        resp = client.post(
            f"/api/v1/projects/{pid}/renders",
            json={"duration_seconds": 20.0 + idx, "fps": 60},
            headers={**headers, "Idempotency-Key": f"idem-inflight-000{idx}"},
        )
        assert resp.status_code == 202
        job_ids.append(resp.json()["id"])

    report1 = process_remote_renders_once(sf, store, settings, fake_backend)
    assert report1.dispatched == 2
    assert len(fake_backend.submitted) == 2

    report2 = process_remote_renders_once(sf, store, settings, fake_backend)
    assert report2.dispatched == 0

    with sf() as db:
        first_job = db.get(RenderJob, uuid.UUID(job_ids[0]))
        assert first_job is not None
        first_job.status = "succeeded"
        db.commit()

    report3 = process_remote_renders_once(sf, store, settings, fake_backend)
    assert report3.dispatched == 1
    assert len(fake_backend.submitted) == 3


def test_render_event_hub_fans_out_to_multiple_subscribers() -> None:
    async def _run() -> None:
        hub = RenderEventHub("sqlite+pysqlite:///:memory:")
        job_a = uuid.uuid4()
        job_b = uuid.uuid4()
        async with (
            hub.subscribe(job_a) as q1,
            hub.subscribe(job_a) as q2,
            hub.subscribe(job_b) as q3,
        ):
            assert hub.subscriber_count == 3
            hub.publish_local(
                job_a, {"job_id": str(job_a), "status": "running", "progress_pct": 42}
            )
            assert await asyncio.wait_for(q1.get(), timeout=1.0) == {
                "job_id": str(job_a),
                "status": "running",
                "progress_pct": 42,
            }
            assert await asyncio.wait_for(q2.get(), timeout=1.0) == {
                "job_id": str(job_a),
                "status": "running",
                "progress_pct": 42,
            }
            assert q3.empty()

            hub.publish_local(None, {"event": "reconnect"})
            assert await asyncio.wait_for(q1.get(), timeout=1.0) == {"event": "reconnect"}
            assert await asyncio.wait_for(q2.get(), timeout=1.0) == {"event": "reconnect"}
            assert await asyncio.wait_for(q3.get(), timeout=1.0) == {"event": "reconnect"}

        assert hub.subscriber_count == 0
        await hub.close()

    asyncio.run(_run())


def test_ingest_worker_loop_runs_periodic_reap(tmp_path: Path) -> None:
    db_file = tmp_path / "reap_loop.db"
    db_url = f"sqlite+pysqlite:///{db_file}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    now = datetime.now(UTC)
    with sf() as db:
        user = User(
            email="reap-loop@example.com",
            display_name="Reap User",
            password_hash=None,
            is_active=True,
            email_verified_at=now,
        )
        db.add(user)
        db.flush()
        expired_session = SessionRecord(
            user_id=user.id,
            token_hash="expired-hash-0001",
            csrf_token="csrf-0001",
            expires_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=3),
        )
        db.add(expired_session)
        db.commit()
    engine.dispose()

    settings = Settings(
        app_env="test",
        database_url=db_url,
        object_store_backend="local",
        local_object_store_dir=str(tmp_path / "objects"),
    )
    run_ingest_worker_loop(
        settings=settings,
        database_url=db_url,
        poll_interval_s=0.05,
        reap_interval_s=0.05,
        max_iterations=1,
    )

    check_engine = create_engine(db_url, future=True)
    check_sf = sessionmaker(bind=check_engine)
    with check_sf() as db:
        remaining = list(db.scalars(select(SessionRecord)).all())
        assert remaining == []
    check_engine.dispose()
