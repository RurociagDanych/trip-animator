from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql
from trip_telemetry import ParsedTrack, TrackPoint, build_silver_dataframe, write_silver_parquet
from trip_worker import (
    LocalObjectStore,
    RenderCancelledError,
    RenderManifest,
    WorkerSettings,
    claim_next_job,
    process_claimed_job,
    run_once,
)
from trip_worker.runner import (
    CLAIM_JOB_SQL,
    DbConnection,
    default_conn_factory,
    mark_job_failed,
    mark_job_succeeded,
    reap_exhausted_jobs,
    record_audit_event,
    update_job_progress,
)

pytestmark = pytest.mark.postgres

_DEFAULT_TEST_PG_URL = "postgresql://trip:trip@127.0.0.1:55432/trip_test"

_SCHEMA_DDL = """
CREATE TABLE users (
    id UUID PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    display_name VARCHAR(120) NOT NULL,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE projects (
    id UUID PRIMARY KEY,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(160) NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    settings_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    share_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    share_slug VARCHAR(64) UNIQUE,
    share_created_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE stages (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_type VARCHAR(16) NOT NULL CHECK (source_type IN ('gpx', 'strava')),
    activity_type VARCHAR(32) NOT NULL DEFAULT 'cycling',
    external_source_id VARCHAR(128),
    strava_activity_id BIGINT,
    name VARCHAR(200) NOT NULL,
    stage_order INTEGER NOT NULL,
    day_index INTEGER NOT NULL,
    trim_start_s DOUBLE PRECISION,
    trim_end_s DOUBLE PRECISION,
    bronze_object_key VARCHAR(512) NOT NULL,
    silver_object_key VARCHAR(512) NOT NULL,
    started_at TIMESTAMPTZ,
    point_count INTEGER NOT NULL,
    distance_m DOUBLE PRECISION NOT NULL,
    elevation_gain_m DOUBLE PRECISION NOT NULL,
    elevation_loss_m DOUBLE PRECISION NOT NULL,
    moving_seconds DOUBLE PRECISION NOT NULL,
    elapsed_seconds DOUBLE PRECISION NOT NULL,
    has_timestamps BOOLEAN NOT NULL,
    min_lat DOUBLE PRECISION,
    max_lat DOUBLE PRECISION,
    min_lon DOUBLE PRECISION,
    max_lon DOUBLE PRECISION,
    warnings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    etl_version INTEGER NOT NULL DEFAULT 1,
    bronze_sha256 VARCHAR(64),
    source_format VARCHAR(16) NOT NULL DEFAULT 'gpx',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_photos (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage_id UUID REFERENCES stages(id) ON DELETE SET NULL,
    source_type VARCHAR(24) NOT NULL DEFAULT 'upload',
    external_media_id VARCHAR(256),
    object_key VARCHAR(512) NOT NULL,
    thumb_object_key VARCHAR(512) NOT NULL,
    mime_type VARCHAR(64) NOT NULL DEFAULT 'image/webp',
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    byte_size INTEGER NOT NULL,
    caption VARCHAR(280) NOT NULL DEFAULT '',
    taken_at_utc TIMESTAMPTZ,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    matched_lat DOUBLE PRECISION,
    matched_lon DOUBLE PRECISION,
    matched_progress DOUBLE PRECISION,
    match_method VARCHAR(24) NOT NULL DEFAULT 'unmatched',
    camera_make_model VARCHAR(128),
    exif_offset_s INTEGER NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_project_photos_project_id ON project_photos(project_id);

CREATE TABLE render_jobs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(128) NOT NULL,
    idempotency_hash VARCHAR(64) NOT NULL,
    spec_json JSONB NOT NULL,
    status VARCHAR(24) NOT NULL,
    progress_pct INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    output_object_key VARCHAR(512),
    thumbnail_key VARCHAR(512),
    content_hash VARCHAR(64),
    is_chunked BOOLEAN NOT NULL DEFAULT FALSE,
    render_backend VARCHAR(32) NOT NULL DEFAULT 'local',
    external_execution_id VARCHAR(512),
    remote_dispatched_at TIMESTAMPTZ,
    total_frames INTEGER,
    expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_render_jobs_idempotency UNIQUE (owner_user_id, project_id, idempotency_key)
);
CREATE INDEX ix_render_jobs_status ON render_jobs(status);
CREATE INDEX ix_render_jobs_project_id ON render_jobs(project_id);
CREATE INDEX ix_render_jobs_content_hash ON render_jobs(content_hash);

CREATE TABLE render_job_chunks (
    id UUID PRIMARY KEY,
    render_job_id UUID NOT NULL REFERENCES render_jobs(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    start_frame INTEGER NOT NULL,
    end_frame INTEGER NOT NULL,
    preroll_frames INTEGER NOT NULL DEFAULT 15,
    status VARCHAR(24) NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    chunk_object_key VARCHAR(512),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_render_job_chunks_job_idx UNIQUE (render_job_id, chunk_index)
);
CREATE INDEX ix_render_job_chunks_render_job_id ON render_job_chunks(render_job_id);
CREATE INDEX ix_render_job_chunks_status_lease ON render_job_chunks(status, lease_expires_at);

CREATE TABLE audit_events (
    id SERIAL PRIMARY KEY,
    user_id UUID,
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
    event_type VARCHAR(64) NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_audit_events_user_id ON audit_events(user_id);
CREATE INDEX ix_audit_events_project_id ON audit_events(project_id);
"""


def _normalize_pg_dsn(raw_url: str) -> str:
    if raw_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + raw_url.removeprefix("postgresql+psycopg://")
    return raw_url


def _dsn_with_search_path(base_dsn: str, schema: str) -> str:
    parsed = urlparse(base_dsn)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["options"] = f"-csearch_path={schema}"
    return urlunparse(parsed._replace(query=urlencode(params)))


class _PgHarness:
    def __init__(self, base_dsn: str, schema: str) -> None:
        self.base_dsn = base_dsn
        self.schema = schema
        self.schema_dsn = _dsn_with_search_path(base_dsn, schema)

    def connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.base_dsn, options=f"-csearch_path={self.schema}")

    def conn_factory(self) -> Callable[[], AbstractContextManager[DbConnection]]:
        @contextmanager
        def _ctx() -> Iterator[DbConnection]:
            with self.connect() as conn:
                yield conn

        return _ctx


@pytest.fixture(scope="module")
def pg_base_dsn() -> str:
    dsn = _normalize_pg_dsn(os.environ.get("TEST_POSTGRES_URL", _DEFAULT_TEST_PG_URL))
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL unreachable at {dsn}: {exc}")
    return dsn


@pytest.fixture
def pg(pg_base_dsn: str) -> Iterator[_PgHarness]:
    schema = f"worker_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_base_dsn) as admin_conn:
        admin_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        admin_conn.commit()

    harness = _PgHarness(pg_base_dsn, schema)
    try:
        with harness.connect() as conn:
            conn.execute(_SCHEMA_DDL)
            conn.commit()
        yield harness
    finally:
        with psycopg.connect(pg_base_dsn) as admin_conn:
            admin_conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            admin_conn.commit()


def _seed_user_project_stage(
    conn: psycopg.Connection[Any],
    store: LocalObjectStore,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 1, 8, 0, 0, tzinfo=UTC)

    parsed = ParsedTrack(
        name="PG Stage 1",
        points=[
            TrackPoint(lat=0.10, lon=0.20, elev_m=1000.0, ts_utc=t0, segment_idx=0),
            TrackPoint(
                lat=0.11,
                lon=0.21,
                elev_m=1080.0,
                ts_utc=t0 + timedelta(seconds=60),
                segment_idx=0,
            ),
        ],
        point_count=2,
        segment_count=1,
        has_timestamps=True,
        has_elevation=True,
    )
    df, summary = build_silver_dataframe(
        parsed,
        trip_id=str(project_id),
        stage_id=str(stage_id),
    )
    silver_key = f"silver/{project_id}/{stage_id}.parquet"
    tmp_pq = store.root_dir / f"_seed_{stage_id}.parquet"
    write_silver_parquet(df, tmp_pq)
    store.put_bytes(silver_key, tmp_pq.read_bytes())
    tmp_pq.unlink()

    conn.execute(
        """
        INSERT INTO users (id, email, display_name, password_hash, is_active)
        VALUES (%(id)s, %(email)s, %(display_name)s, %(password_hash)s, TRUE)
        """,
        {
            "id": user_id,
            "email": f"worker-{user_id.hex[:8]}@example.com",
            "display_name": "Worker Tester",
            "password_hash": "hash",
        },
    )
    conn.execute(
        """
        INSERT INTO projects (id, owner_user_id, title)
        VALUES (%(id)s, %(owner_user_id)s, %(title)s)
        """,
        {
            "id": project_id,
            "owner_user_id": user_id,
            "title": "PostgreSQL Test Trip",
        },
    )
    conn.execute(
        """
        INSERT INTO stages (
            id, project_id, source_type, activity_type, name, stage_order, day_index,
            bronze_object_key, silver_object_key, point_count, distance_m,
            elevation_gain_m, elevation_loss_m, moving_seconds, elapsed_seconds,
            has_timestamps
        )
        VALUES (
            %(id)s, %(project_id)s, 'gpx', 'cycling', 'Stage 1', 0, 1,
            %(bronze_key)s, %(silver_key)s, %(point_count)s, %(distance_m)s,
            %(gain)s, %(loss)s, %(moving_s)s, %(elapsed_s)s, TRUE
        )
        """,
        {
            "id": stage_id,
            "project_id": project_id,
            "bronze_key": f"bronze/{project_id}/{stage_id}.gpx",
            "silver_key": silver_key,
            "point_count": summary.point_count,
            "distance_m": summary.distance_m,
            "gain": summary.elevation_gain_m,
            "loss": summary.elevation_loss_m,
            "moving_s": summary.moving_seconds,
            "elapsed_s": summary.elapsed_seconds,
        },
    )
    conn.commit()
    return user_id, project_id, stage_id


def _insert_render_job(
    conn: psycopg.Connection[Any],
    *,
    owner_user_id: uuid.UUID,
    project_id: uuid.UUID,
    job_id: uuid.UUID | None = None,
    status: str = "queued",
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    jid = job_id or uuid.uuid4()
    ts = created_at or datetime.now(UTC)
    spec = {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0}
    conn.execute(
        """
        INSERT INTO render_jobs (
            id, project_id, owner_user_id, idempotency_key, idempotency_hash,
            spec_json, status, progress_pct, attempt_count, lease_expires_at,
            created_at, updated_at
        )
        VALUES (
            %(id)s, %(project_id)s, %(owner_user_id)s, %(idem_key)s, %(idem_hash)s,
            %(spec_json)s::jsonb, %(status)s, 0, %(attempt_count)s, %(lease_expires_at)s,
            %(created_at)s, %(created_at)s
        )
        """,
        {
            "id": jid,
            "project_id": project_id,
            "owner_user_id": owner_user_id,
            "idem_key": f"idem-{jid.hex}",
            "idem_hash": jid.hex,
            "spec_json": json.dumps(spec),
            "status": status,
            "attempt_count": attempt_count,
            "lease_expires_at": lease_expires_at,
            "created_at": ts,
        },
    )
    conn.commit()
    return jid


def test_postgres_skip_locked_multi_worker_claim(pg: _PgHarness, tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    with pg.connect() as conn:
        user_id, project_id, _ = _seed_user_project_stage(conn, store)
        t0 = datetime.now(UTC) - timedelta(seconds=10)
        job1_id = _insert_render_job(
            conn, owner_user_id=user_id, project_id=project_id, created_at=t0
        )
        job2_id = _insert_render_job(
            conn,
            owner_user_id=user_id,
            project_id=project_id,
            created_at=t0 + timedelta(seconds=1),
        )

    with pg.connect() as conn_a, pg.connect() as conn_b:
        row_a = conn_a.execute(CLAIM_JOB_SQL, {"max_attempts": 3, "lease_seconds": 60}).fetchone()
        assert row_a is not None
        assert uuid.UUID(str(row_a[0])) == job1_id

        claim_b = claim_next_job(conn_b, max_attempts=3, lease_seconds=60)
        assert claim_b is not None
        assert claim_b.id == job2_id
        assert claim_b.attempt_count == 1

        conn_a.commit()

        assert claim_next_job(conn_b, max_attempts=3, lease_seconds=60) is None

    with pg.connect() as conn:
        race_jobs = {
            _insert_render_job(
                conn,
                owner_user_id=user_id,
                project_id=project_id,
                created_at=datetime.now(UTC) + timedelta(seconds=i),
            )
            for i in range(5)
        }

    barrier = threading.Barrier(4)
    claimed_by_threads: list[uuid.UUID] = []
    lock = threading.Lock()

    def _worker_thread() -> None:
        barrier.wait(timeout=5.0)
        with pg.connect() as thread_conn:
            while True:
                job = claim_next_job(thread_conn, max_attempts=3, lease_seconds=60)
                if job is None:
                    break
                with lock:
                    claimed_by_threads.append(job.id)

    threads = [threading.Thread(target=_worker_thread) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(claimed_by_threads) == 5
    assert set(claimed_by_threads) == race_jobs


def test_postgres_fencing_token_rejects_stale_worker_and_reaps_exhausted(
    pg: _PgHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path)
    with pg.connect() as conn:
        user_id, project_id, _ = _seed_user_project_stage(conn, store)
        job_id = _insert_render_job(conn, owner_user_id=user_id, project_id=project_id)

    with pg.connect() as conn_a, pg.connect() as conn_b:
        claim_a = claim_next_job(conn_a, max_attempts=3, lease_seconds=60)
        assert claim_a is not None
        assert claim_a.id == job_id
        assert claim_a.attempt_count == 1

        conn_b.execute(
            "UPDATE render_jobs SET lease_expires_at = NOW() - INTERVAL '10 seconds' WHERE id = %s",
            (job_id,),
        )
        conn_b.commit()

        claim_b = claim_next_job(conn_b, max_attempts=3, lease_seconds=60)
        assert claim_b is not None
        assert claim_b.id == job_id
        assert claim_b.attempt_count == 2

        # Stale Worker A (attempt_count=1) must be rejected by HEARTBEAT_SQL,
        # MARK_SUCCEEDED_SQL, and MARK_FAILED_SQL.
        assert (
            update_job_progress(
                conn_a, job_id=job_id, attempt_count=1, progress_pct=85, lease_seconds=60
            )
            is False
        )
        assert (
            mark_job_succeeded(
                conn_a,
                job_id=job_id,
                attempt_count=1,
                output_object_key="outputs/stale.mp4",
                thumbnail_key="outputs/stale.webp",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            is False
        )
        assert (
            mark_job_failed(
                conn_a,
                job_id=job_id,
                attempt_count=1,
                error_message="stale worker error",
                max_attempts=3,
                fatal=True,
            )
            is None
        )

        row = conn_b.execute(
            """
            SELECT status, progress_pct, attempt_count, output_object_key, error_message
            FROM render_jobs
            WHERE id = %s
            """,
            (job_id,),
        ).fetchone()
        assert row == ("running", 0, 2, None, None)

    # A fenced-out worker must return "lost_lease" without touching the DB or the
    # output of the worker that reclaimed the expired lease.
    with pg.connect() as conn:
        job2_id = _insert_render_job(conn, owner_user_id=user_id, project_id=project_id)
        claim2_a = claim_next_job(conn, max_attempts=3, lease_seconds=60)
        assert claim2_a is not None and claim2_a.id == job2_id and claim2_a.attempt_count == 1

    async def _stale_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, settings, on_progress, is_cancelled
        await asyncio.to_thread(output_mp4_path.write_bytes, b"STALE_MP4")
        return output_mp4_path

    monkeypatch.setattr(
        "trip_worker.runner.verify_mp4_output",
        lambda *_a, **_kw: type("Probe", (), {"width": 1280, "height": 720, "duration_s": 5.0})(),
    )

    def _fake_thumb(_mp4: Path, thumb: Path, **_kw: Any) -> Path:
        thumb.write_bytes(b"STALE_WEBP")
        return thumb

    monkeypatch.setattr("trip_worker.runner.extract_thumbnail", _fake_thumb)

    orig_put = store.put_bytes

    def _steal_and_complete(
        key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        orig_put(key, data, content_type=content_type)
        if key.endswith(".webp"):
            with pg.connect() as conn_win:
                conn_win.execute(
                    "UPDATE render_jobs SET lease_expires_at = NOW() - INTERVAL '5 seconds' "
                    "WHERE id = %s",
                    (job2_id,),
                )
                conn_win.commit()
                reclaim_b = claim_next_job(conn_win, max_attempts=3, lease_seconds=60)
                assert reclaim_b is not None and reclaim_b.attempt_count == 2
                assert mark_job_succeeded(
                    conn_win,
                    job_id=job2_id,
                    attempt_count=2,
                    output_object_key=f"outputs/{job2_id}.mp4",
                    thumbnail_key=f"outputs/{job2_id}.webp",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            orig_put(f"outputs/{job2_id}.mp4", b"WORKER_B_MP4", content_type="video/mp4")

    monkeypatch.setattr(store, "put_bytes", _steal_and_complete)

    res = process_claimed_job(
        pg.conn_factory(),
        claim2_a,
        WorkerSettings(),
        store,
        render_fn=_stale_render,
    )
    assert res == "lost_lease"
    assert store.get_bytes(f"outputs/{job2_id}.mp4") == b"WORKER_B_MP4"

    with pg.connect() as conn:
        final_row = conn.execute(
            "SELECT status, attempt_count FROM render_jobs WHERE id = %s", (job2_id,)
        ).fetchone()
        assert final_row == ("succeeded", 2)

        exhausted_id = _insert_render_job(
            conn,
            owner_user_id=user_id,
            project_id=project_id,
            status="running",
            attempt_count=3,
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        assert reap_exhausted_jobs(conn, max_attempts=3) == 1
        reaped_row = conn.execute(
            "SELECT status, lease_expires_at, error_message FROM render_jobs WHERE id = %s",
            (exhausted_id,),
        ).fetchone()
        assert reaped_row is not None
        assert reaped_row[0] == "failed"
        assert reaped_row[1] is None
        assert "maximum retry attempts" in str(reaped_row[2]).lower()


def test_postgres_lease_renewal_during_long_post_render(
    pg: _PgHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path)
    with pg.connect() as conn:
        user_id, project_id, _ = _seed_user_project_stage(conn, store)
        job_id = _insert_render_job(conn, owner_user_id=user_id, project_id=project_id)

    settings = WorkerSettings(
        database_url=pg.schema_dsn,
        lease_seconds=10,
        lease_heartbeat_interval_seconds=0.05,
    )
    cf = default_conn_factory(settings)
    with cf() as claim_conn:
        claimed = claim_next_job(claim_conn, max_attempts=3, lease_seconds=settings.lease_seconds)
    assert claimed is not None and claimed.id == job_id

    observed_short_lease: list[datetime] = []
    observed_renewed_lease: list[datetime] = []

    async def _quick_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Callable[[int, int], bool] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Path:
        _ = manifest, settings, is_cancelled
        if on_progress is not None:
            assert on_progress(1, 1) is True
        await asyncio.to_thread(output_mp4_path.write_bytes, b"MP4_DATA")
        return output_mp4_path

    def _slow_verify(*_a: Any, **_kw: Any) -> Any:
        with pg.connect() as probe_conn:
            row = probe_conn.execute(
                """
                UPDATE render_jobs
                SET lease_expires_at = NOW() + INTERVAL '2 seconds'
                WHERE id = %s
                RETURNING lease_expires_at
                """,
                (job_id,),
            ).fetchone()
            probe_conn.commit()
            assert row is not None
            observed_short_lease.append(row[0])

        time.sleep(0.25)

        with pg.connect() as probe_conn:
            row_after = probe_conn.execute(
                "SELECT lease_expires_at FROM render_jobs WHERE id = %s", (job_id,)
            ).fetchone()
            assert row_after is not None
            observed_renewed_lease.append(row_after[0])

        return type("Probe", (), {"width": 1280, "height": 720, "duration_s": 5.0})()

    def _fake_thumb(_mp4: Path, thumb: Path, **_kw: Any) -> Path:
        thumb.write_bytes(b"WEBP_DATA")
        return thumb

    monkeypatch.setattr("trip_worker.runner.verify_mp4_output", _slow_verify)
    monkeypatch.setattr("trip_worker.runner.extract_thumbnail", _fake_thumb)

    result = process_claimed_job(cf, claimed, settings, store, render_fn=_quick_render)
    assert result == "succeeded"
    assert len(observed_short_lease) == 1
    assert len(observed_renewed_lease) == 1
    assert observed_renewed_lease[0] > observed_short_lease[0] + timedelta(seconds=5)

    with pg.connect() as conn:
        job_row = conn.execute(
            "SELECT status, progress_pct, lease_expires_at FROM render_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        assert job_row == ("succeeded", 100, None)
        audit_row = conn.execute(
            "SELECT event_type, project_id FROM audit_events WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        assert audit_row == ("render_succeeded", project_id)


def test_postgres_deleted_project_fk_fallback_in_record_audit_event(
    pg: _PgHarness,
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path)
    with pg.connect() as conn:
        user_id, project_id, _ = _seed_user_project_stage(conn, store)

    missing_project_id = uuid.uuid4()
    with pg.connect() as conn:
        record_audit_event(
            conn,
            user_id=user_id,
            project_id=missing_project_id,
            event_type="render_failed",
            metadata={"job_id": "missing-proj-job"},
        )
        rows = conn.execute(
            "SELECT user_id, project_id, event_type, metadata_json FROM audit_events"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == user_id
        assert rows[0][1] is None
        assert rows[0][2] == "render_failed"
        assert rows[0][3] == {"job_id": "missing-proj-job"}

    with pg.connect() as conn:
        _insert_render_job(conn, owner_user_id=user_id, project_id=project_id)

    async def _delete_project_and_cancel(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, output_mp4_path, settings, on_progress, is_cancelled
        with pg.connect() as del_conn:
            del_conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            del_conn.commit()
        raise RenderCancelledError("project deleted mid-render")

    assert (
        run_once(
            WorkerSettings(),
            conn_factory=pg.conn_factory(),
            store=store,
            render_fn=_delete_project_and_cancel,
        )
        is True
    )

    with pg.connect() as conn:
        cancel_rows = conn.execute(
            """
            SELECT project_id, event_type
            FROM audit_events
            WHERE event_type = 'render_cancelled'
            """
        ).fetchall()
        assert cancel_rows == [(None, "render_cancelled")]


def test_postgres_chunked_skip_locked_and_pg_notify(
    pg: _PgHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalObjectStore(tmp_path)
    with pg.connect() as conn:
        user_id, project_id, _ = _seed_user_project_stage(conn, store)
        job_id = uuid.uuid4()
        spec = {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0}
        conn.execute(
            """
            INSERT INTO render_jobs (
                id, project_id, owner_user_id, idempotency_key, idempotency_hash,
                spec_json, status, progress_pct, attempt_count
            )
            VALUES (
                %(id)s, %(project_id)s, %(owner_user_id)s, %(idem_key)s, %(idem_hash)s,
                %(spec_json)s::jsonb, 'queued', 0, 0
            )
            """,
            {
                "id": job_id,
                "project_id": project_id,
                "owner_user_id": user_id,
                "idem_key": f"idem-{job_id.hex}",
                "idem_hash": job_id.hex,
                "spec_json": json.dumps(spec),
            },
        )
        conn.commit()

    received_notifies: list[dict[str, Any]] = []
    listen_conn = pg.connect()
    listen_conn.autocommit = True
    listen_conn.execute("LISTEN render_job_events;")

    async def fake_chunk_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
        frame_start: int = 0,
        frame_end: int | None = None,
        preroll_frames: int = 0,
        photo_assets: dict[str, bytes] | None = None,
        gop_aligned: bool = False,
    ) -> Path:
        _ = manifest, settings, is_cancelled, preroll_frames, photo_assets, gop_aligned
        if on_progress is not None:
            on_progress(1, 1)
        await asyncio.to_thread(
            output_mp4_path.write_bytes, f"PG_CHUNK_{frame_start}_{frame_end}".encode()
        )
        return output_mp4_path

    def fake_concat(chunk_paths: list[Path], output_mp4_path: Path, **_kw: Any) -> Path:
        output_mp4_path.write_bytes(b"|".join(p.read_bytes() for p in chunk_paths))
        return output_mp4_path

    monkeypatch.setattr("trip_worker.runner.concat_mp4_chunks", fake_concat)
    monkeypatch.setattr(
        "trip_worker.runner.verify_mp4_output",
        lambda *_a, **_kw: type("Probe", (), {"width": 1280, "height": 720, "duration_s": 10.0})(),
    )
    monkeypatch.setattr(
        "trip_worker.runner.extract_thumbnail",
        lambda _mp4, thumb_p, **_kw: thumb_p.write_bytes(b"RIFFwebp") or thumb_p,
    )

    try:
        settings = WorkerSettings(
            database_url=pg.schema_dsn,
            chunk_threshold_frames=100,
            chunk_size_frames=150,
            chunk_preroll_frames=15,
        )
        assert (
            run_once(
                settings,
                conn_factory=pg.conn_factory(),
                store=store,
                render_fn=fake_chunk_render,
            )
            is True
        )

        for notify in listen_conn.notifies(timeout=0.5):
            received_notifies.append(json.loads(notify.payload))
    finally:
        listen_conn.close()

    with pg.connect() as conn:
        job_row = conn.execute(
            "SELECT status, progress_pct, is_chunked, total_frames FROM render_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        assert job_row == ("succeeded", 100, True, 300)
        chunk_rows = conn.execute(
            "SELECT chunk_index, status, start_frame, end_frame, preroll_frames "
            "FROM render_job_chunks WHERE render_job_id = %s ORDER BY chunk_index ASC",
            (job_id,),
        ).fetchall()
        assert chunk_rows == [
            (0, "succeeded", 0, 150, 0),
            (1, "succeeded", 150, 300, 15),
        ]

    assert store.get_bytes(f"outputs/{job_id}.mp4") == b"PG_CHUNK_0_150|PG_CHUNK_150_300"
    assert any(n["status"] == "running" for n in received_notifies)
    assert any(n["status"] == "succeeded" and n["progress_pct"] == 100 for n in received_notifies)
