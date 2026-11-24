from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from trip_telemetry import ParsedTrack, TrackPoint, build_silver_dataframe, write_silver_parquet
from trip_worker import (
    ClaimedJob,
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
    HEARTBEAT_SQL,
    MARK_FAILED_SQL,
    MARK_SUCCEEDED_SQL,
    mark_job_failed,
    mark_job_succeeded,
    record_audit_event,
    update_job_progress,
)


class _MemoryCursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self._rowcount = rowcount

    @property
    def rowcount(self) -> int:
        return self._rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class InMemoryRenderDatabase:
    """In-memory state machine simulating PostgreSQL `render_jobs`, `render_job_chunks`, etc."""

    def __init__(self) -> None:
        self.projects: dict[uuid.UUID, str] = {}
        self.stages: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.photos: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.jobs: dict[uuid.UUID, dict[str, Any]] = {}
        self.chunks: dict[uuid.UUID, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.cancel_after_progress_calls: int | None = None
        self.progress_updates = 0
        self.conn_open_count = 0

    def conn_factory(self) -> Any:
        db = self

        class _Conn:
            def __init__(self) -> None:
                self.aborted = False
                self.rollbacks = 0

            def execute(self, query: Any, params: Any = None) -> _MemoryCursor:
                if self.aborted:
                    raise psycopg.errors.InFailedSqlTransaction("current transaction is aborted")
                p = dict(params or {})
                sql = str(query)

                if "pg_notify('render_job_events'" in sql:
                    db.notifications.append(json.loads(str(p["payload"])))
                    return _MemoryCursor(rowcount=1)

                if "UPDATE render_jobs" in sql and "SET is_chunked = TRUE" in sql:
                    job = db.jobs.get(p["job_id"])
                    if (
                        job is None
                        or job["status"] != "running"
                        or job["attempt_count"] != p["attempt_count"]
                    ):
                        return _MemoryCursor([])
                    job["is_chunked"] = True
                    job["total_frames"] = int(p["total_frames"])
                    job["lease_expires_at"] = None
                    job["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(job["id"],)], rowcount=1)

                if "INSERT INTO render_job_chunks" in sql:
                    for existing in db.chunks.values():
                        if (
                            existing["render_job_id"] == p["render_job_id"]
                            and existing["chunk_index"] == p["chunk_index"]
                        ):
                            return _MemoryCursor(rowcount=0)
                    now = datetime.now(UTC)
                    db.chunks[p["id"]] = {
                        "id": p["id"],
                        "render_job_id": p["render_job_id"],
                        "chunk_index": int(p["chunk_index"]),
                        "start_frame": int(p["start_frame"]),
                        "end_frame": int(p["end_frame"]),
                        "preroll_frames": int(p["preroll_frames"]),
                        "status": "queued",
                        "attempt_count": 0,
                        "lease_expires_at": None,
                        "chunk_object_key": None,
                        "error_message": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                    return _MemoryCursor(rowcount=1)

                if (
                    "UPDATE render_job_chunks" in sql
                    and "attempt_count >= %(max_attempts)s" in sql
                    and "lease_expires_at < NOW()" in sql
                ):
                    now = datetime.now(UTC)
                    reaped_rows: list[tuple[Any, ...]] = []
                    for ch in db.chunks.values():
                        if (
                            ch["status"] == "running"
                            and ch["lease_expires_at"] is not None
                            and ch["lease_expires_at"] < now
                            and ch["attempt_count"] >= p["max_attempts"]
                        ):
                            ch["status"] = "failed"
                            ch["lease_expires_at"] = None
                            ch["error_message"] = (
                                ch["error_message"]
                                or "Render chunk exceeded maximum retry attempts"
                            )
                            reaped_rows.append((ch["id"], ch["render_job_id"], ch["chunk_index"]))
                    return _MemoryCursor(reaped_rows, rowcount=len(reaped_rows))

                if "FROM render_job_chunks" in sql and "FOR UPDATE OF c SKIP LOCKED" in sql:
                    now = datetime.now(UTC)
                    target_job = p.get("job_id")
                    eligible = [
                        ch
                        for ch in db.chunks.values()
                        if (target_job is None or ch["render_job_id"] == target_job)
                        and (db.jobs.get(ch["render_job_id"], {}).get("status") == "running")
                        and (
                            ch["status"] == "queued"
                            or (
                                ch["status"] == "running"
                                and ch["lease_expires_at"] is not None
                                and ch["lease_expires_at"] < now
                            )
                        )
                        and ch["attempt_count"] < p["max_attempts"]
                    ]
                    eligible.sort(
                        key=lambda ch: (
                            db.jobs[ch["render_job_id"]]["created_at"],
                            ch["chunk_index"],
                        )
                    )
                    if not eligible:
                        return _MemoryCursor([])
                    ch = eligible[0]
                    ch["status"] = "running"
                    ch["attempt_count"] += 1
                    ch["lease_expires_at"] = now + timedelta(seconds=int(p["lease_seconds"]))
                    ch["updated_at"] = now
                    return _MemoryCursor(
                        [
                            (
                                ch["id"],
                                ch["render_job_id"],
                                ch["chunk_index"],
                                ch["start_frame"],
                                ch["end_frame"],
                                ch["preroll_frames"],
                                ch["attempt_count"],
                            )
                        ],
                        rowcount=1,
                    )

                if "UPDATE render_job_chunks" in sql and "SET lease_expires_at = NOW()" in sql:
                    ch = db.chunks.get(p["chunk_id"])
                    if (
                        ch is None
                        or ch["status"] != "running"
                        or ch["attempt_count"] != p["attempt_count"]
                    ):
                        return _MemoryCursor([])
                    ch["lease_expires_at"] = datetime.now(UTC) + timedelta(
                        seconds=int(p["lease_seconds"])
                    )
                    ch["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(ch["status"],)], rowcount=1)

                if "UPDATE render_job_chunks" in sql and "SET status = 'succeeded'" in sql:
                    ch = db.chunks.get(p["chunk_id"])
                    if (
                        ch is None
                        or ch["status"] != "running"
                        or ch["attempt_count"] != p["attempt_count"]
                    ):
                        return _MemoryCursor([])
                    ch["status"] = "succeeded"
                    ch["lease_expires_at"] = None
                    ch["chunk_object_key"] = p["chunk_object_key"]
                    ch["error_message"] = None
                    ch["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(ch["render_job_id"],)], rowcount=1)

                if (
                    "UPDATE render_job_chunks" in sql
                    and "WHEN attempt_count >= %(max_attempts)s" in sql
                ):
                    ch = db.chunks.get(p["chunk_id"])
                    if (
                        ch is None
                        or ch["status"] != "running"
                        or ch["attempt_count"] != p["attempt_count"]
                    ):
                        return _MemoryCursor([])
                    is_terminal = ch["attempt_count"] >= p["max_attempts"] or bool(p["fatal"])
                    ch["status"] = "failed" if is_terminal else "queued"
                    ch["lease_expires_at"] = None
                    ch["error_message"] = p["error_message"]
                    ch["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(ch["status"],)], rowcount=1)

                if "UPDATE render_job_chunks" in sql and "SET status = 'cancelled'" in sql:
                    count = 0
                    for ch in db.chunks.values():
                        if ch["render_job_id"] == p["job_id"] and ch["status"] in (
                            "queued",
                            "running",
                        ):
                            ch["status"] = "cancelled"
                            ch["lease_expires_at"] = None
                            count += 1
                    return _MemoryCursor(rowcount=count)

                if "FILTER (WHERE status = 'succeeded')" in sql:
                    job_chunks = [
                        ch for ch in db.chunks.values() if ch["render_job_id"] == p["job_id"]
                    ]
                    succ = sum(1 for ch in job_chunks if ch["status"] == "succeeded")
                    return _MemoryCursor([(len(job_chunks), succ)])

                if "FROM render_job_chunks" in sql and "AND status = 'succeeded'" in sql:
                    job_chunks = sorted(
                        (
                            ch
                            for ch in db.chunks.values()
                            if ch["render_job_id"] == p["job_id"] and ch["status"] == "succeeded"
                        ),
                        key=lambda ch: ch["chunk_index"],
                    )
                    return _MemoryCursor(
                        [
                            (
                                ch["chunk_index"],
                                ch["start_frame"],
                                ch["end_frame"],
                                ch["chunk_object_key"],
                            )
                            for ch in job_chunks
                        ]
                    )

                if "FOR UPDATE OF rj SKIP LOCKED" in sql:
                    now = datetime.now(UTC)
                    target_job = p.get("job_id")
                    ready = []
                    for j in db.jobs.values():
                        if target_job is not None and j["id"] != target_job:
                            continue
                        if j["status"] != "running" or not j.get("is_chunked"):
                            continue
                        if j["lease_expires_at"] is not None and j["lease_expires_at"] >= now:
                            continue
                        j_chunks = [
                            ch for ch in db.chunks.values() if ch["render_job_id"] == j["id"]
                        ]
                        if j_chunks and all(ch["status"] == "succeeded" for ch in j_chunks):
                            ready.append(j)
                    ready.sort(key=lambda j: j["created_at"])
                    if not ready:
                        return _MemoryCursor([])
                    chosen = ready[0]
                    chosen["lease_expires_at"] = now + timedelta(seconds=int(p["lease_seconds"]))
                    chosen["progress_pct"] = max(int(chosen["progress_pct"]), 96)
                    chosen["updated_at"] = now
                    return _MemoryCursor(
                        [
                            (
                                chosen["id"],
                                chosen["project_id"],
                                chosen["owner_user_id"],
                                chosen["spec_json"],
                                chosen["attempt_count"],
                            )
                        ],
                        rowcount=1,
                    )

                if (
                    "UPDATE render_jobs" in sql
                    and "SET status = 'failed'" in sql
                    and "WHERE id = %(job_id)s" in sql
                ):
                    job = db.jobs.get(p["job_id"])
                    if job is None or job["status"] != "running":
                        return _MemoryCursor([])
                    job["status"] = "failed"
                    job["lease_expires_at"] = None
                    job["error_message"] = p["error_message"]
                    job["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor(
                        [(job["id"], job["owner_user_id"], job["project_id"], job["progress_pct"])],
                        rowcount=1,
                    )

                if "WHEN attempt_count >= %(max_attempts)s" in sql:
                    job = db.jobs.get(p["job_id"])
                    if (
                        job is None
                        or job["status"] != "running"
                        or job["attempt_count"] != p["attempt_count"]
                    ):
                        return _MemoryCursor([])
                    is_terminal = job["attempt_count"] >= p["max_attempts"] or bool(p["fatal"])
                    job["status"] = "failed" if is_terminal else "queued"
                    if not is_terminal:
                        job["progress_pct"] = 0
                    job["lease_expires_at"] = None
                    job["error_message"] = p["error_message"]
                    job["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(job["status"], job["progress_pct"])], rowcount=1)

                if (
                    "UPDATE render_jobs" in sql
                    and "lease_expires_at < NOW()" in sql
                    and "attempt_count >= %(max_attempts)s" in sql
                ):
                    now = datetime.now(UTC)
                    reaped_jobs: list[tuple[Any, ...]] = []
                    for job in db.jobs.values():
                        if (
                            job["status"] == "running"
                            and not job.get("is_chunked", False)
                            and job["lease_expires_at"] is not None
                            and job["lease_expires_at"] < now
                            and job["attempt_count"] >= p["max_attempts"]
                        ):
                            job["status"] = "failed"
                            job["lease_expires_at"] = None
                            job["error_message"] = (
                                job["error_message"] or "Render job exceeded maximum retry attempts"
                            )
                            reaped_jobs.append((job["id"], job["progress_pct"]))
                    return _MemoryCursor(reaped_jobs, rowcount=len(reaped_jobs))

                if "FOR UPDATE SKIP LOCKED" in sql:
                    now = datetime.now(UTC)
                    eligible = [
                        j
                        for j in db.jobs.values()
                        if (
                            j["status"] == "queued"
                            or (
                                j["status"] == "running"
                                and not j.get("is_chunked", False)
                                and j["lease_expires_at"] is not None
                                and j["lease_expires_at"] < now
                            )
                        )
                        and j["attempt_count"] < p["max_attempts"]
                    ]
                    eligible.sort(key=lambda j: j["created_at"])
                    if not eligible:
                        return _MemoryCursor([])
                    chosen = eligible[0]
                    chosen["status"] = "running"
                    chosen["progress_pct"] = 0
                    chosen["attempt_count"] += 1
                    chosen["lease_expires_at"] = now + timedelta(seconds=int(p["lease_seconds"]))
                    chosen["updated_at"] = now
                    return _MemoryCursor(
                        [
                            (
                                chosen["id"],
                                chosen["project_id"],
                                chosen["owner_user_id"],
                                chosen["spec_json"],
                                chosen["attempt_count"],
                            )
                        ],
                        rowcount=1,
                    )

                if "SET progress_pct = GREATEST" in sql:
                    job = db.jobs.get(p["job_id"])
                    if job is None or job["status"] != "running":
                        return _MemoryCursor([])
                    if "attempt_count" in p and job["attempt_count"] != p["attempt_count"]:
                        return _MemoryCursor([])
                    db.progress_updates += 1
                    if (
                        db.cancel_after_progress_calls is not None
                        and db.progress_updates >= db.cancel_after_progress_calls
                    ):
                        job["status"] = "cancelled"
                        return _MemoryCursor([])
                    job["progress_pct"] = max(int(job["progress_pct"]), int(p["progress_pct"]))
                    if "lease_seconds" in p:
                        job["lease_expires_at"] = datetime.now(UTC) + timedelta(
                            seconds=int(p["lease_seconds"])
                        )
                    job["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(job["status"], job["progress_pct"])], rowcount=1)

                if "SELECT id, project_id, owner_user_id, spec_json, attempt_count, status" in sql:
                    job = db.jobs.get(p["job_id"])
                    if job is None:
                        return _MemoryCursor([])
                    return _MemoryCursor(
                        [
                            (
                                job["id"],
                                job["project_id"],
                                job["owner_user_id"],
                                job["spec_json"],
                                job["attempt_count"],
                                job["status"],
                            )
                        ]
                    )

                if "SELECT status" in sql and "FROM render_jobs" in sql:
                    job = db.jobs.get(p["job_id"])
                    return _MemoryCursor([(job["status"],)] if job else [])

                if "SET status = 'succeeded'" in sql:
                    job = db.jobs.get(p["job_id"])
                    if (
                        job is None
                        or job["status"] != "running"
                        or job["attempt_count"] != p["attempt_count"]
                    ):
                        return _MemoryCursor([])
                    job["status"] = "succeeded"
                    job["progress_pct"] = 100
                    job["lease_expires_at"] = None
                    job["output_object_key"] = p["output_object_key"]
                    job["thumbnail_key"] = p["thumbnail_key"]
                    job["expires_at"] = p["expires_at"]
                    job["error_message"] = None
                    job["updated_at"] = datetime.now(UTC)
                    return _MemoryCursor([(job["id"],)], rowcount=1)

                if "FROM projects" in sql:
                    title = db.projects.get(p["project_id"])
                    return _MemoryCursor([(title,)] if title is not None else [])

                if "FROM stages" in sql:
                    rows = sorted(
                        db.stages.get(p["project_id"], []),
                        key=lambda s: s["stage_order"],
                    )
                    return _MemoryCursor(
                        [
                            (
                                s["id"],
                                s["project_id"],
                                s["name"],
                                s["stage_order"],
                                s["day_index"],
                                s["trim_start_s"],
                                s["trim_end_s"],
                                s["silver_object_key"],
                                s["distance_m"],
                                s["elevation_gain_m"],
                                s["moving_seconds"],
                                s["elapsed_seconds"],
                                s["has_timestamps"],
                                s.get("activity_type", "cycling"),
                            )
                            for s in rows
                        ]
                    )

                if "FROM project_photos" in sql:
                    photo_rows = [
                        ph for ph in db.photos.get(p["project_id"], []) if ph.get("enabled", True)
                    ]
                    return _MemoryCursor(
                        [
                            (
                                ph["id"],
                                ph["project_id"],
                                ph["thumb_object_key"],
                                ph["width"],
                                ph["height"],
                                ph.get("caption", ""),
                                ph.get("taken_at_utc"),
                                ph.get("lat"),
                                ph.get("lon"),
                                ph.get("matched_lat"),
                                ph.get("matched_lon"),
                                ph.get("matched_progress"),
                                ph.get("match_method", "unmatched"),
                                ph.get("object_key"),
                            )
                            for ph in photo_rows
                        ]
                    )

                if "INSERT INTO audit_events" in sql:
                    proj_id = p.get("project_id")
                    if proj_id is not None and proj_id not in db.projects:
                        self.aborted = True
                        raise psycopg.errors.ForeignKeyViolation(
                            'insert or update on table "audit_events" violates '
                            'foreign key constraint "audit_events_project_id_fkey"'
                        )
                    db.audit_events.append(
                        {
                            "user_id": p["user_id"],
                            "project_id": proj_id,
                            "event_type": p["event_type"],
                            "metadata": json.loads(p["metadata_json"]),
                        }
                    )
                    return _MemoryCursor(rowcount=1)

                raise AssertionError(f"Unhandled SQL in mock DB: {sql}")

            def commit(self) -> None:
                return None

            def rollback(self) -> None:
                self.aborted = False
                self.rollbacks += 1

        @contextmanager
        def _ctx() -> Iterator[_Conn]:
            db.conn_open_count += 1
            yield _Conn()

        return _ctx


def _seed_project_and_stage(
    db: InMemoryRenderDatabase,
    store: LocalObjectStore,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 1, 8, 0, 0, tzinfo=UTC)

    parsed = ParsedTrack(
        name="Stage 1",
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
    tmp_pq = store.root_dir / "_seed.parquet"
    write_silver_parquet(df, tmp_pq)
    store.put_bytes(silver_key, tmp_pq.read_bytes())
    tmp_pq.unlink()

    db.projects[project_id] = "Sella Ronda"
    db.stages[project_id] = [
        {
            "id": stage_id,
            "project_id": project_id,
            "name": "Stage 1",
            "stage_order": 0,
            "day_index": 1,
            "trim_start_s": None,
            "trim_end_s": None,
            "silver_object_key": silver_key,
            "distance_m": summary.distance_m,
            "elevation_gain_m": summary.elevation_gain_m,
            "moving_seconds": summary.moving_seconds,
            "elapsed_seconds": summary.elapsed_seconds,
            "has_timestamps": True,
        }
    ]
    return owner_id, project_id, stage_id


def test_claim_sql_uses_skip_locked_and_processes_job_to_succeeded(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    assert "FOR UPDATE SKIP LOCKED" in CLAIM_JOB_SQL
    assert "lease_expires_at < NOW()" in CLAIM_JOB_SQL

    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fake_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = settings, is_cancelled
        assert manifest.width == 1280
        assert manifest.height == 720
        assert on_progress(75, 150) is True
        assert db.jobs[job_id]["progress_pct"] == 48
        await asyncio.to_thread(output_mp4_path.write_bytes, b"FAKE_MP4_STREAM")
        return output_mp4_path

    monkeypatch.setattr(
        "trip_worker.runner.verify_mp4_output",
        lambda path, **_kw: type(
            "Probe",
            (),
            {"width": 1280, "height": 720, "duration_s": 5.0},
        )(),
    )

    def fake_thumb(_mp4: Path, thumb: Path, **_kw: Any) -> Path:
        thumb.write_bytes(b"RIFF_WEBP")
        return thumb

    monkeypatch.setattr("trip_worker.runner.extract_thumbnail", fake_thumb)

    cf = db.conn_factory()
    did_work = run_once(
        WorkerSettings(),
        conn_factory=cf,
        store=store,
        render_fn=fake_render,
    )
    assert did_work is True

    job = db.jobs[job_id]
    assert job["status"] == "succeeded"
    assert job["progress_pct"] == 100
    assert job["attempt_count"] == 1
    assert job["lease_expires_at"] is None
    assert job["output_object_key"] == f"outputs/{job_id}.mp4"
    assert job["thumbnail_key"] == f"outputs/{job_id}.webp"
    assert store.get_bytes(f"outputs/{job_id}.mp4") == b"FAKE_MP4_STREAM"
    assert store.get_bytes(f"outputs/{job_id}.webp") == b"RIFF_WEBP"
    assert db.audit_events[-1]["event_type"] == "render_succeeded"

    assert run_once(WorkerSettings(), conn_factory=cf, store=store, render_fn=fake_render) is False


def test_runner_preserves_cancelled_status_and_retries_transient_failures(
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    cf = db.conn_factory()

    # Mid-render cancellation must leave status='cancelled' untouched
    cancel_job_id = uuid.uuid4()
    db.jobs[cancel_job_id] = {
        "id": cancel_job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    db.cancel_after_progress_calls = 1

    async def cancel_mid_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, output_mp4_path, settings, is_cancelled
        if on_progress(10, 100) is False:
            from trip_worker import RenderCancelledError

            raise RenderCancelledError("cancelled")
        return output_mp4_path

    assert (
        run_once(WorkerSettings(), conn_factory=cf, store=store, render_fn=cancel_mid_render)
        is True
    )
    assert db.jobs[cancel_job_id]["status"] == "cancelled"
    assert db.jobs[cancel_job_id]["output_object_key"] is None
    assert db.audit_events[-1]["event_type"] == "render_cancelled"

    db.cancel_after_progress_calls = None
    retry_job_id = uuid.uuid4()
    db.jobs[retry_job_id] = {
        "id": retry_job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def failing_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, output_mp4_path, settings, is_cancelled
        if on_progress is not None:
            on_progress(50, 100)
        raise RuntimeError("transient chromium crash at /tmp/secret/dsn=postgresql://user:pw@db")

    settings = WorkerSettings(max_attempts=2)
    with cf() as c:
        claimed_1 = claim_next_job(c, max_attempts=2, lease_seconds=60)
    assert isinstance(claimed_1, ClaimedJob)
    res1 = process_claimed_job(cf, claimed_1, settings, store, render_fn=failing_render)
    assert res1 == "queued"
    assert db.jobs[retry_job_id]["attempt_count"] == 1
    assert db.jobs[retry_job_id]["progress_pct"] == 0
    assert "/tmp/secret" not in str(db.jobs[retry_job_id]["error_message"])
    assert "postgresql://" not in str(db.jobs[retry_job_id]["error_message"])

    with cf() as c:
        claimed_2 = claim_next_job(c, max_attempts=2, lease_seconds=60)
    assert claimed_2 is not None
    res2 = process_claimed_job(cf, claimed_2, settings, store, render_fn=failing_render)
    assert res2 == "failed"
    assert db.jobs[retry_job_id]["attempt_count"] == 2
    assert db.jobs[retry_job_id]["error_message"] == "Render worker encountered an internal error"

    stale_id = uuid.uuid4()
    db.jobs[stale_id] = {
        "id": stale_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2},
        "status": "running",
        "progress_pct": 20,
        "attempt_count": 2,
        "lease_expires_at": datetime.now(UTC) - timedelta(seconds=10),
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    assert run_once(settings, conn_factory=cf, store=store, render_fn=failing_render) is False
    assert db.jobs[stale_id]["status"] == "failed"

    empty_proj_id = uuid.uuid4()
    db.projects[empty_proj_id] = "Empty Trip"
    db.stages[empty_proj_id] = []
    fatal_id = uuid.uuid4()
    db.jobs[fatal_id] = {
        "id": fatal_id,
        "project_id": empty_proj_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    assert run_once(settings, conn_factory=cf, store=store, render_fn=failing_render) is True
    assert db.jobs[fatal_id]["status"] == "failed"
    assert db.jobs[fatal_id]["attempt_count"] == 1
    assert "zero stages" in str(db.jobs[fatal_id]["error_message"]).lower()


def test_fencing_token_prevents_stale_worker_overwrites(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    assert "AND attempt_count = %(attempt_count)s" in HEARTBEAT_SQL
    assert "AND attempt_count = %(attempt_count)s" in MARK_SUCCEEDED_SQL
    assert "AND attempt_count = %(attempt_count)s" in MARK_FAILED_SQL

    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    cf = db.conn_factory()
    with cf() as c:
        stale_claim = claim_next_job(c, max_attempts=3, lease_seconds=60)
    assert stale_claim is not None and stale_claim.attempt_count == 1

    db.jobs[job_id]["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=5)
    with cf() as c:
        active_claim = claim_next_job(c, max_attempts=3, lease_seconds=60)
    assert active_claim is not None and active_claim.attempt_count == 2

    # Stale Worker 1 calls must be fenced out without touching Worker 2's row
    with cf() as c:
        assert (
            update_job_progress(
                c, job_id=job_id, attempt_count=1, progress_pct=80, lease_seconds=60
            )
            is False
        )
        assert (
            mark_job_succeeded(
                c,
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
                c,
                job_id=job_id,
                attempt_count=1,
                error_message="stale error",
                max_attempts=3,
                fatal=True,
            )
            is None
        )
    assert db.jobs[job_id]["status"] == "running"
    assert db.jobs[job_id]["progress_pct"] == 0
    assert db.jobs[job_id]["attempt_count"] == 2

    # If Worker 2 already uploaded output and succeeded while Worker 1 was finishing upload,
    # fenced-out Worker 1 must NOT delete the shared output key.
    db.jobs[job_id]["attempt_count"] = 1
    db.jobs[job_id]["status"] = "running"

    async def fake_render_while_stolen(
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

    def steal_before_commit(
        key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        orig_put(key, data, content_type=content_type)
        if key.endswith(".webp"):
            db.jobs[job_id]["attempt_count"] = 2
            db.jobs[job_id]["status"] = "succeeded"
            orig_put(f"outputs/{job_id}.mp4", b"ACTIVE_WORKER_2_MP4", content_type="video/mp4")

    monkeypatch.setattr(store, "put_bytes", steal_before_commit)
    result = process_claimed_job(
        cf,
        ClaimedJob(
            id=job_id,
            project_id=project_id,
            owner_user_id=owner_id,
            spec_json=db.jobs[job_id]["spec_json"],
            attempt_count=1,
        ),
        WorkerSettings(),
        store,
        render_fn=fake_render_while_stolen,
    )
    assert result == "lost_lease"
    assert store.get_bytes(f"outputs/{job_id}.mp4") == b"ACTIVE_WORKER_2_MP4"

    # Fenced-out Worker 1 failing with exception must not emit a bogus render_failed audit event
    events_before = len(db.audit_events)
    db.jobs[job_id]["status"] = "running"
    db.jobs[job_id]["attempt_count"] = 2

    async def fail_while_stolen(*_a: Any, **_kw: Any) -> Path:
        raise RuntimeError("boom")

    res_fail = process_claimed_job(
        cf,
        ClaimedJob(
            id=job_id,
            project_id=project_id,
            owner_user_id=owner_id,
            spec_json=db.jobs[job_id]["spec_json"],
            attempt_count=1,
        ),
        WorkerSettings(),
        store,
        render_fn=fail_while_stolen,
    )
    assert res_fail == "lost_lease"
    assert db.jobs[job_id]["status"] == "running"
    assert len(db.audit_events) == events_before


def test_runner_reuses_connection_throttles_heartbeats_and_renews_during_post_render(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def rapid_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, settings
        for i in range(1, 51):
            assert is_cancelled() is False
            assert on_progress(i, 50) is True
        await asyncio.to_thread(output_mp4_path.write_bytes, b"MP4_BYTES")
        return output_mp4_path

    monkeypatch.setattr(
        "trip_worker.runner.verify_mp4_output",
        lambda *_a, **_kw: type("Probe", (), {"width": 1280, "height": 720, "duration_s": 5.0})(),
    )

    def _thumb(_mp4: Path, thumb: Path, **_kw: Any) -> Path:
        thumb.write_bytes(b"WEBP")
        return thumb

    monkeypatch.setattr("trip_worker.runner.extract_thumbnail", _thumb)

    cf = db.conn_factory()
    with cf() as c:
        claimed = claim_next_job(c, max_attempts=3, lease_seconds=60)
    assert claimed is not None

    db.conn_open_count = 0
    db.progress_updates = 0
    res = process_claimed_job(
        cf,
        claimed,
        WorkerSettings(lease_seconds=120, lease_heartbeat_interval_seconds=10.0),
        store,
        render_fn=rapid_render,
    )
    assert res == "succeeded"
    assert db.conn_open_count == 1
    assert db.progress_updates == 4

    job2_id = uuid.uuid4()
    db.jobs[job2_id] = {
        "id": job2_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def slow_finish_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, settings, is_cancelled
        on_progress(1, 1)
        await asyncio.sleep(0.12)
        await asyncio.to_thread(output_mp4_path.write_bytes, b"MP4_SLOW")
        return output_mp4_path

    def slow_verify(*_a: Any, **_kw: Any) -> Any:
        time.sleep(0.12)
        return type("Probe", (), {"width": 1280, "height": 720, "duration_s": 5.0})()

    monkeypatch.setattr("trip_worker.runner.verify_mp4_output", slow_verify)

    with cf() as c:
        claimed2 = claim_next_job(c, max_attempts=3, lease_seconds=10)
    assert claimed2 is not None

    db.progress_updates = 0
    res2 = process_claimed_job(
        cf,
        claimed2,
        WorkerSettings(lease_seconds=10, lease_heartbeat_interval_seconds=0.04),
        store,
        render_fn=slow_finish_render,
    )
    assert res2 == "succeeded"
    assert db.progress_updates > 3


def test_audit_events_handle_deleted_projects_without_crashing(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    cf = db.conn_factory()

    missing_proj_id = uuid.uuid4()
    with cf() as conn:
        record_audit_event(
            conn,
            user_id=owner_id,
            project_id=missing_proj_id,
            event_type="render_failed",
            metadata={"job_id": "j1"},
        )
    assert db.audit_events[-1]["project_id"] is None
    assert db.audit_events[-1]["event_type"] == "render_failed"

    deleted_proj_job = uuid.uuid4()
    db.jobs[deleted_proj_job] = {
        "id": deleted_proj_job,
        "project_id": missing_proj_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def noop_render(*_a: Any, **_kw: Any) -> Path:
        raise AssertionError("should not render")

    assert run_once(WorkerSettings(), conn_factory=cf, store=store, render_fn=noop_render) is True
    assert db.jobs[deleted_proj_job]["status"] == "failed"
    assert db.audit_events[-1]["event_type"] == "render_failed"
    assert db.audit_events[-1]["project_id"] is None

    cancel_job = uuid.uuid4()
    db.jobs[cancel_job] = {
        "id": cancel_job,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def delete_project_and_cancel(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = manifest, output_mp4_path, settings, on_progress, is_cancelled
        db.projects.pop(project_id, None)
        db.jobs[cancel_job]["status"] = "cancelled"
        raise RenderCancelledError("cancelled after project deletion")

    assert (
        run_once(
            WorkerSettings(),
            conn_factory=cf,
            store=store,
            render_fn=delete_project_and_cancel,
        )
        is True
    )
    assert db.audit_events[-1]["event_type"] == "render_cancelled"
    assert db.audit_events[-1]["project_id"] is None


def test_runner_loads_only_enabled_photos_into_manifest_and_ssrf_blocks_api(
    tmp_path: Path,
) -> None:
    from trip_worker import is_render_request_allowed

    db = InMemoryRenderDatabase()
    cf = db.conn_factory()
    store = LocalObjectStore(tmp_path)

    owner_id = uuid.uuid4()
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    job_id = uuid.uuid4()

    db.projects[project_id] = "Photo Runner Trip"
    t0 = datetime(2001, 7, 1, 8, 0, 0, tzinfo=UTC)
    parsed = ParsedTrack(
        name="Stage 1",
        points=[
            TrackPoint(lat=0.1, lon=0.2, elev_m=1000.0, ts_utc=t0, segment_idx=0),
            TrackPoint(
                lat=0.11,
                lon=0.21,
                elev_m=1020.0,
                ts_utc=t0 + timedelta(seconds=60),
                segment_idx=0,
            ),
        ],
        point_count=2,
        segment_count=1,
        has_timestamps=True,
        has_elevation=True,
    )
    df, summary = build_silver_dataframe(parsed, trip_id=str(project_id), stage_id=str(stage_id))
    silver_key = f"silver/{project_id}/{stage_id}.parquet"
    tmp_pq = tmp_path / "tmp.parquet"
    write_silver_parquet(df, tmp_pq)
    store.put_bytes(silver_key, tmp_pq.read_bytes(), content_type="application/vnd.apache.parquet")

    db.stages[project_id] = [
        {
            "id": stage_id,
            "project_id": project_id,
            "name": "Stage 1",
            "stage_order": 0,
            "day_index": 1,
            "trim_start_s": None,
            "trim_end_s": None,
            "silver_object_key": silver_key,
            "distance_m": summary.distance_m,
            "elevation_gain_m": summary.elevation_gain_m,
            "moving_seconds": 60.0,
            "elapsed_seconds": 60.0,
            "has_timestamps": True,
        }
    ]

    enabled_photo_id = uuid.uuid4()
    disabled_photo_id = uuid.uuid4()
    enabled_key = f"photos/{project_id}/{enabled_photo_id}_thumb.webp"
    disabled_key = f"photos/{project_id}/{disabled_photo_id}_thumb.webp"
    store.put_bytes(
        enabled_key,
        b"RIFF\x10\x00\x00\x00WEBPVP8 \x04\x00\x00\x00ok!!",
        content_type="image/webp",
    )
    store.put_bytes(
        disabled_key,
        b"RIFF\x10\x00\x00\x00WEBPVP8 \x04\x00\x00\x00no!!",
        content_type="image/webp",
    )

    db.photos[project_id] = [
        {
            "id": enabled_photo_id,
            "project_id": project_id,
            "thumb_object_key": enabled_key,
            "width": 640,
            "height": 480,
            "caption": "Summit",
            "taken_at_utc": t0 + timedelta(seconds=30),
            "lat": 0.105,
            "lon": 0.205,
            "enabled": True,
        },
        {
            "id": disabled_photo_id,
            "project_id": project_id,
            "thumb_object_key": disabled_key,
            "width": 640,
            "height": 480,
            "caption": "Hidden",
            "taken_at_utc": t0 + timedelta(seconds=45),
            "lat": 0.108,
            "lon": 0.208,
            "enabled": False,
        },
    ]

    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    captured_manifests: list[RenderManifest] = []

    async def capture_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Any = None,
        is_cancelled: Any = None,
    ) -> Path:
        _ = settings, on_progress, is_cancelled
        captured_manifests.append(manifest)
        await asyncio.to_thread(output_mp4_path.write_bytes, b"\x00\x00\x00\x18ftypmp42")
        return output_mp4_path

    from unittest.mock import patch

    from trip_worker.encoder import ProbeResult

    with (
        patch(
            "trip_worker.runner.verify_mp4_output",
            return_value=ProbeResult(
                codec_name="h264",
                pix_fmt="yuv420p",
                width=1280,
                height=720,
                duration_s=5.0,
                nb_frames=150,
            ),
        ),
        patch(
            "trip_worker.runner.extract_thumbnail",
            side_effect=lambda _mp4, thumb_p, **_kw: thumb_p.write_bytes(b"RIFFwebp"),
        ),
    ):
        assert run_once(WorkerSettings(), conn_factory=cf, store=store, render_fn=capture_render)

    assert len(captured_manifests) == 1
    m = captured_manifests[0]
    assert len(m.photos) == 1
    assert m.photos[0].id == str(enabled_photo_id)
    assert m.photos[0].data_uri == ""
    assert m.photos[0].asset_url == f"https://render-assets.local/{enabled_photo_id}"

    # Verify SSRF invariant: virtual asset host is not externally allowed, /api/* remains blocked
    assert not is_render_request_allowed(
        m.photos[0].asset_url,
        render_origin="http://web:8080",
        allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        f"http://web:8080/api/v1/projects/{project_id}/photos/{enabled_photo_id}/image",
        render_origin="http://web:8080",
        allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
        resolve_dns=False,
    )


def test_chunked_job_fanout_render_concat_notifications_and_failure(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    cf = db.conn_factory()

    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    seen_chunks: list[tuple[int, int | None, int, bool, dict[str, bytes] | None]] = []

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
        _ = manifest, settings, is_cancelled
        seen_chunks.append((frame_start, frame_end, preroll_frames, gop_aligned, photo_assets))
        if on_progress is not None:
            on_progress(1, 1)
        await asyncio.to_thread(
            output_mp4_path.write_bytes, f"CHUNK_{frame_start}_{frame_end}".encode()
        )
        return output_mp4_path

    def fake_concat(chunk_paths: list[Path], output_mp4_path: Path, **_kw: Any) -> Path:
        combined = b"+".join(p.read_bytes() for p in chunk_paths)
        output_mp4_path.write_bytes(combined)
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

    settings = WorkerSettings(
        chunk_threshold_frames=100,
        chunk_size_frames=120,
        chunk_preroll_frames=15,
    )
    assert run_once(settings, conn_factory=cf, store=store, render_fn=fake_chunk_render) is True

    job = db.jobs[job_id]
    assert job["status"] == "succeeded"
    assert job["is_chunked"] is True
    assert job["total_frames"] == 300
    assert job["progress_pct"] == 100
    assert seen_chunks == [
        (0, 120, 0, True, {}),
        (120, 240, 15, True, {}),
        (240, 300, 15, True, {}),
    ]
    assert store.get_bytes(f"outputs/{job_id}.mp4") == b"CHUNK_0_120+CHUNK_120_240+CHUNK_240_300"
    for c_idx in range(3):
        try:
            store.get_bytes(f"outputs/chunks/{job_id}/chunk_{c_idx:04d}.mp4")
            raise AssertionError("Expected intermediate chunk object to be deleted")
        except FileNotFoundError:
            pass

    job_notifs = [n for n in db.notifications if n["job_id"] == str(job_id)]
    assert job_notifs[0]["status"] == "running"
    assert job_notifs[-1] == {"job_id": str(job_id), "status": "succeeded", "progress_pct": 100}

    fail_job_id = uuid.uuid4()
    db.jobs[fail_job_id] = {
        "id": fail_job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fail_first_chunk(*_a: Any, **_kw: Any) -> Path:
        raise ValueError("invalid chunk telemetry")

    with caplog.at_level("WARNING", logger="trip_worker.runner"):
        assert run_once(settings, conn_factory=cf, store=store, render_fn=fail_first_chunk) is True
    assert db.jobs[fail_job_id]["status"] == "failed"
    chunk_failure_records = [
        r for r in caplog.records if "failed:" in r.getMessage() and r.name == "trip_worker.runner"
    ]
    assert chunk_failure_records
    assert all(r.exc_info is not None for r in chunk_failure_records)
    fail_chunks = [ch for ch in db.chunks.values() if ch["render_job_id"] == fail_job_id]
    assert any(ch["status"] == "failed" for ch in fail_chunks)
    assert all(ch["status"] in {"failed", "cancelled"} for ch in fail_chunks)
    first_chunk_id = str(fail_chunks[0]["id"])
    assert f"Chunk {first_chunk_id} (job {fail_job_id}) failed: Invalid render job parameters" in [
        r.getMessage() for r in chunk_failure_records
    ]


def test_failed_unchunked_job_warning_keeps_original_wording(
    tmp_path: Path,
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    cf = db.conn_factory()
    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fail_render(*_a: Any, **_kw: Any) -> Path:
        raise RuntimeError("boom")

    settings = WorkerSettings(chunk_threshold_frames=10**9)
    with caplog.at_level("WARNING", logger="trip_worker.runner"):
        run_once(settings, conn_factory=cf, store=store, render_fn=fail_render)
    assert f"Render job {job_id} failed: Render worker encountered an internal error" in [
        r.getMessage() for r in caplog.records if r.name == "trip_worker.runner"
    ]


def test_run_worker_loop_concurrency_spawns_and_stops_threads(
    monkeypatch: Any,
) -> None:
    import threading

    from trip_worker.runner import run_worker_loop

    seen_threads: set[str] = set()
    lock = threading.Lock()
    registered_handlers: list[Any] = []

    def fake_signal(_sig: int, handler: Any) -> Any:
        registered_handlers.append(handler)
        return None

    def fake_run_once(_cfg: WorkerSettings) -> bool:
        with lock:
            seen_threads.add(threading.current_thread().name)
            if len(seen_threads) >= 2 and registered_handlers:
                registered_handlers[0](15, None)
        return False

    monkeypatch.setattr("trip_worker.runner.signal.signal", fake_signal)
    monkeypatch.setattr("trip_worker.runner.run_once", fake_run_once)

    cfg = WorkerSettings(worker_concurrency=2, poll_interval_seconds=0.01)
    run_worker_loop(cfg, once=False)
    assert len(seen_threads) == 2
    assert all(name.startswith("trip-worker-") for name in seen_threads)


def test_chunked_job_uses_pre_serialized_manifest_without_rebuilding(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from trip_worker import manifest as manifest_mod
    from trip_worker import runner as runner_mod
    from trip_worker.manifest import chunk_manifest_key

    class SpyLocalStore(LocalObjectStore):
        def __init__(self, root_dir: Path) -> None:
            super().__init__(root_dir)
            self.get_keys: list[str] = []

        def get_bytes(self, key: str) -> bytes:
            self.get_keys.append(key)
            return super().get_bytes(key)

    store = SpyLocalStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, stage_id = _seed_project_and_stage(db, store)
    silver_key = db.stages[project_id][0]["silver_object_key"]

    photo_id = uuid.uuid4()
    thumb_key = f"photos/{project_id}/{photo_id}_thumb.webp"
    store.put_bytes(thumb_key, b"RIFFwebp-thumb", content_type="image/webp")
    db.photos[project_id] = [
        {
            "id": photo_id,
            "project_id": project_id,
            "thumb_object_key": thumb_key,
            "object_key": f"photos/{project_id}/{photo_id}.webp",
            "width": 640,
            "height": 480,
            "caption": "Summit",
            "taken_at_utc": None,
            "lat": None,
            "lon": None,
            "matched_lat": None,
            "matched_lon": None,
            "matched_progress": None,
            "match_method": "unmatched",
            "enabled": True,
        }
    ]

    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    db.jobs[job_id]["spec_json"]["photo_card_style"] = "cinematic_stop"

    build_calls: list[dict[str, Any]] = []
    orig_build = manifest_mod.build_render_manifest

    def spy_build(*args: Any, **kwargs: Any) -> RenderManifest:
        build_calls.append(kwargs)
        return orig_build(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "build_render_manifest", spy_build)

    manifest_seen_in_chunks: list[bool] = []
    chunk_photo_assets: list[dict[str, bytes] | None] = []

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
        _ = manifest, settings, on_progress, is_cancelled, preroll_frames, gop_aligned
        manifest_seen_in_chunks.append(
            store.resolve_existing_path(chunk_manifest_key(job_id)).is_file()
        )
        chunk_photo_assets.append(dict(photo_assets) if photo_assets is not None else None)
        await asyncio.to_thread(
            output_mp4_path.write_bytes, f"CHUNK_{frame_start}_{frame_end}".encode()
        )
        return output_mp4_path

    monkeypatch.setattr(
        "trip_worker.runner.concat_mp4_chunks",
        lambda paths, out, **_kw: out.write_bytes(b"+".join(p.read_bytes() for p in paths)) or out,
    )
    monkeypatch.setattr(
        "trip_worker.runner.verify_mp4_output",
        lambda *_a, **_kw: type("Probe", (), {"width": 1280, "height": 720, "duration_s": 10.0})(),
    )
    monkeypatch.setattr(
        "trip_worker.runner.extract_thumbnail",
        lambda _mp4, thumb_p, **_kw: thumb_p.write_bytes(b"RIFFwebp") or thumb_p,
    )

    cf = db.conn_factory()
    settings = WorkerSettings(
        chunk_threshold_frames=100,
        chunk_size_frames=150,
        chunk_preroll_frames=15,
    )
    assert (
        run_once(
            settings,
            conn_factory=cf,
            store=store,
            render_fn=fake_chunk_render,
            frame_count_fn=lambda *_args: 300,
        )
        is True
    )
    assert db.jobs[job_id]["status"] == "succeeded"

    # One manifest for the cinematic frame-count probe and one shared by both chunks.
    assert [call["load_assets"] for call in build_calls] == [False, False]
    assert manifest_seen_in_chunks == [True, True]
    assert chunk_photo_assets == [
        {str(photo_id): b"RIFFwebp-thumb"},
        {str(photo_id): b"RIFFwebp-thumb"},
    ]
    assert silver_key not in store.get_keys
    assert chunk_manifest_key(job_id) in store.get_keys
    with pytest.raises(FileNotFoundError):
        store.resolve_existing_path(chunk_manifest_key(job_id))

    fail_job_id = uuid.uuid4()
    db.jobs[fail_job_id] = {
        "id": fail_job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fail_chunk(*_a: Any, **_kw: Any) -> Path:
        raise ValueError("fatal chunk error")

    assert run_once(settings, conn_factory=cf, store=store, render_fn=fail_chunk) is True
    assert db.jobs[fail_job_id]["status"] == "failed"
    with pytest.raises(FileNotFoundError):
        store.resolve_existing_path(chunk_manifest_key(fail_job_id))

    cancel_job_id = uuid.uuid4()
    db.jobs[cancel_job_id] = {
        "id": cancel_job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def cancel_chunk(*_a: Any, **_kw: Any) -> Path:
        db.jobs[cancel_job_id]["status"] = "cancelled"
        raise RenderCancelledError("cancelled by user")

    assert run_once(settings, conn_factory=cf, store=store, render_fn=cancel_chunk) is True
    assert db.jobs[cancel_job_id]["status"] == "cancelled"
    with pytest.raises(FileNotFoundError):
        store.resolve_existing_path(chunk_manifest_key(cancel_job_id))


def _run_cinematic_job(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    duration_seconds: float,
    photo_count: int,
    probed_frames: int,
    settings: WorkerSettings,
) -> tuple[InMemoryRenderDatabase, uuid.UUID, dict[str, Any]]:
    store = LocalObjectStore(tmp_path)
    db = InMemoryRenderDatabase()
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    db.photos[project_id] = []
    for index in range(photo_count):
        photo_id = uuid.uuid4()
        thumb_key = f"photos/{project_id}/{photo_id}_thumb.webp"
        full_key = f"photos/{project_id}/{photo_id}.webp"
        store.put_bytes(thumb_key, b"THUMB", content_type="image/webp")
        store.put_bytes(full_key, b"FULL", content_type="image/webp")
        db.photos[project_id].append(
            {
                "id": photo_id,
                "project_id": project_id,
                "thumb_object_key": thumb_key,
                "object_key": full_key,
                "width": 4000,
                "height": 3000,
                "caption": f"Photo {index}",
                "matched_progress": 0.4 + index * 0.001,
                "match_method": "gps",
                "enabled": True,
            }
        )

    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": duration_seconds,
            "photo_card_style": "cinematic_stop",
        },
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    seen: dict[str, Any] = {"probes": [], "verify": [], "ranges": [], "assets": []}

    def fake_probe(manifest: RenderManifest, _settings: WorkerSettings, _host: Any) -> int:
        seen["probes"].append(len(manifest.photos))
        return probed_frames

    async def fake_render(
        manifest: RenderManifest,
        output_mp4_path: Path,
        _settings: WorkerSettings,
        *,
        frame_start: int = 0,
        frame_end: int | None = None,
        photo_assets: dict[str, bytes] | None = None,
        **_kw: Any,
    ) -> Path:
        seen["ranges"].append((frame_start, frame_end))
        seen["assets"].append(dict(photo_assets or {}))
        seen["manifest_frames"] = manifest.total_frames
        await asyncio.to_thread(output_mp4_path.write_bytes, b"MP4")
        return output_mp4_path

    def fake_verify(_path: Path, **kwargs: Any) -> Any:
        seen["verify"].append(kwargs)
        return type("Probe", (), {"width": 1280, "height": 720, "duration_s": 1.0})()

    monkeypatch.setattr("trip_worker.runner.verify_mp4_output", fake_verify)
    monkeypatch.setattr(
        "trip_worker.runner.concat_mp4_chunks",
        lambda paths, out, **_kw: out.write_bytes(b"+".join(p.read_bytes() for p in paths)) or out,
    )
    monkeypatch.setattr(
        "trip_worker.runner.extract_thumbnail",
        lambda _mp4, thumb_p, **_kw: thumb_p.write_bytes(b"RIFFwebp") or thumb_p,
    )

    assert (
        run_once(
            settings,
            conn_factory=db.conn_factory(),
            store=store,
            render_fn=fake_render,
            frame_count_fn=fake_probe,
        )
        is True
    )
    assert db.jobs[job_id]["status"] == "succeeded"
    return db, job_id, seen


def test_cinematic_job_without_photos_skips_probe_and_keeps_base_duration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = WorkerSettings(chunk_threshold_frames=1800, chunk_size_frames=300)
    db, job_id, seen = _run_cinematic_job(
        tmp_path,
        monkeypatch,
        duration_seconds=10.0,
        photo_count=0,
        probed_frames=9999,
        settings=settings,
    )
    assert seen["probes"] == []
    assert db.jobs[job_id]["is_chunked"] is False
    assert seen["verify"][0]["expected_duration_s"] == 10.0


def test_cinematic_job_without_photos_still_chunks_long_base_duration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = WorkerSettings(chunk_threshold_frames=1800, chunk_size_frames=300)
    db, job_id, seen = _run_cinematic_job(
        tmp_path,
        monkeypatch,
        duration_seconds=100.0,
        photo_count=0,
        probed_frames=9999,
        settings=settings,
    )
    assert seen["probes"] == []
    assert db.jobs[job_id]["is_chunked"] is True
    assert db.jobs[job_id]["total_frames"] == 3000
    assert seen["ranges"][0] == (0, 300)
    assert seen["ranges"][-1] == (2700, 3000)


def test_cinematic_job_with_one_photo_uses_browser_total_and_full_variant(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = WorkerSettings(chunk_threshold_frames=1800, chunk_size_frames=300)
    db, job_id, seen = _run_cinematic_job(
        tmp_path,
        monkeypatch,
        duration_seconds=10.0,
        photo_count=1,
        probed_frames=390,
        settings=settings,
    )
    assert seen["probes"] == [1]
    assert db.jobs[job_id]["is_chunked"] is False
    assert seen["manifest_frames"] == 390
    assert seen["verify"][0]["expected_duration_s"] == 13.0
    assert seen["verify"][0]["duration_tolerance_s"] == max(1.5, 13.0 * 0.25)
    assert list(seen["assets"][0].values()) == [b"FULL"]


def test_cinematic_job_with_grouped_photos_chunks_on_the_browser_total(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = WorkerSettings(chunk_threshold_frames=1800, chunk_size_frames=300)
    db, job_id, seen = _run_cinematic_job(
        tmp_path,
        monkeypatch,
        duration_seconds=30.0,
        photo_count=3,
        probed_frames=2400,
        settings=settings,
    )
    assert seen["probes"] == [3]
    assert db.jobs[job_id]["is_chunked"] is True
    assert db.jobs[job_id]["total_frames"] == 2400
    assert seen["ranges"] == [(start, start + 300) for start in range(0, 2400, 300)]
    assert all(list(assets.values()) == [b"FULL"] * 3 for assets in seen["assets"])
    assert seen["verify"][0]["expected_duration_s"] == 80.0


def test_cinematic_job_logs_lifecycle_probe_and_chunk_stage_events(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings = WorkerSettings(chunk_threshold_frames=1800, chunk_size_frames=300)
    caplog.set_level("INFO", logger="trip_worker.runner")
    db, job_id, _seen = _run_cinematic_job(
        tmp_path,
        monkeypatch,
        duration_seconds=10.0,
        photo_count=1,
        probed_frames=300,
        settings=settings,
    )
    assert db.jobs[job_id]["status"] == "succeeded"

    events = {
        r.event: r  # type: ignore[attr-defined]
        for r in caplog.records
        if r.name == "trip_worker.runner" and hasattr(r, "event")
    }
    assert str(events["job_start"].job_id) == str(job_id)  # type: ignore[attr-defined]
    assert events["job_finish"].status == "succeeded"  # type: ignore[attr-defined]
    assert events["job_finish"].duration_ms >= 0  # type: ignore[attr-defined]
    assert events["probe_finish"].stage == "probe"  # type: ignore[attr-defined]
    assert events["probe_finish"].duration_ms >= 0  # type: ignore[attr-defined]


def test_chunked_job_logs_chunk_start_and_finish_with_rss(
    tmp_path: Path, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings = WorkerSettings(chunk_threshold_frames=1800, chunk_size_frames=300)
    caplog.set_level("INFO", logger="trip_worker.runner")
    db, job_id, _seen = _run_cinematic_job(
        tmp_path,
        monkeypatch,
        duration_seconds=100.0,
        photo_count=0,
        probed_frames=9999,
        settings=settings,
    )
    assert db.jobs[job_id]["is_chunked"] is True

    chunk_finishes = [
        r
        for r in caplog.records
        if r.name == "trip_worker.runner" and getattr(r, "event", None) == "chunk_finish"
    ]
    assert len(chunk_finishes) == 10
    first = chunk_finishes[0]
    assert first.stage == "chunk"  # type: ignore[attr-defined]
    assert str(first.job_id) == str(job_id)  # type: ignore[attr-defined]
    assert first.status == "succeeded"  # type: ignore[attr-defined]
    assert first.duration_ms >= 0  # type: ignore[attr-defined]
    assert first.rss_mb is None or first.rss_mb > 0  # type: ignore[attr-defined]


def test_run_once_signals_requeue_and_worker_loop_waits_backoff(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import threading

    from trip_worker.runner import _worker_thread_loop

    db = InMemoryRenderDatabase()
    store = LocalObjectStore(tmp_path / "objects")
    owner_id, project_id, _ = _seed_project_and_stage(db, store)
    cf = db.conn_factory()
    job_id = uuid.uuid4()
    db.jobs[job_id] = {
        "id": job_id,
        "project_id": project_id,
        "owner_user_id": owner_id,
        "spec_json": {"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        "status": "queued",
        "progress_pct": 0,
        "attempt_count": 0,
        "lease_expires_at": None,
        "output_object_key": None,
        "thumbnail_key": None,
        "expires_at": None,
        "error_message": None,
        "is_chunked": False,
        "total_frames": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fail_render(*_args: Any, **_kwargs: Any) -> Path:
        raise RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED at http://caddy/render/test")

    requeued_calls = 0

    def _on_requeued() -> None:
        nonlocal requeued_calls
        requeued_calls += 1

    settings = WorkerSettings(chunk_threshold_frames=10**9, max_attempts=3)
    did_work = run_once(
        settings,
        conn_factory=cf,
        store=store,
        render_fn=fail_render,
        on_requeued=_on_requeued,
    )
    assert did_work is True
    assert requeued_calls == 1
    assert db.jobs[job_id]["status"] == "queued"

    waits: list[float | None] = []
    stop_event = threading.Event()
    original_wait = stop_event.wait

    def recording_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        stop_event.set()
        return original_wait(timeout=0)

    monkeypatch.setattr(stop_event, "wait", recording_wait)

    def fake_run_once(
        _cfg: WorkerSettings,
        *,
        browser_host: Any = None,
        on_requeued: Any = None,
    ) -> bool:
        _ = browser_host
        if on_requeued is not None:
            on_requeued()
        return True

    monkeypatch.setattr("trip_worker.runner.run_once", fake_run_once)

    loop_cfg = WorkerSettings(poll_interval_seconds=2.0, retry_backoff_seconds=7.5)
    _worker_thread_loop(loop_cfg, stop_event, 0)
    assert waits == [7.5]
