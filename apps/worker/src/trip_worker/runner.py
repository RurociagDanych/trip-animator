"""PostgreSQL SKIP LOCKED lease queue, chunked parallel execution, and Playwright runner."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import psycopg
from pydantic import ValidationError
from trip_telemetry import DEFAULT_ACTIVITY_TYPE, RenderSpecV2, normalize_activity_type

from trip_worker.config import WorkerSettings
from trip_worker.encoder import (
    EncoderError,
    concat_mp4_chunks,
    extract_thumbnail,
    verify_mp4_output,
)
from trip_worker.manifest import (
    PhotoRecord,
    RenderManifest,
    StageRecord,
    build_render_manifest,
    chunk_manifest_key,
    photo_asset_key,
)
from trip_worker.renderer import (
    RenderCancelledError,
    RenderError,
    WarmPlaywrightHost,
    probe_manifest_frame_count,
    render_manifest_to_mp4,
)
from trip_worker.storage import ObjectStore, build_object_store

logger = logging.getLogger(__name__)

NOTIFY_RENDER_JOB_SQL = """
SELECT pg_notify('render_job_events', %(payload)s);
"""

CLAIM_JOB_SQL = """
WITH candidate AS (
    SELECT id
    FROM render_jobs
    WHERE (
        status = 'queued'
        OR (
            status = 'running'
            AND COALESCE(is_chunked, FALSE) = FALSE
            AND lease_expires_at < NOW()
        )
    )
      AND attempt_count < %(max_attempts)s
    ORDER BY created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE render_jobs rj
SET status = 'running',
    progress_pct = 0,
    attempt_count = rj.attempt_count + 1,
    lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    updated_at = NOW()
FROM candidate
WHERE rj.id = candidate.id
RETURNING rj.id, rj.project_id, rj.owner_user_id, rj.spec_json, rj.attempt_count;
"""

REAP_EXHAUSTED_SQL = """
UPDATE render_jobs
SET status = 'failed',
    lease_expires_at = NULL,
    error_message = COALESCE(
        error_message,
        'Render job exceeded maximum retry attempts after lease expiration'
    ),
    updated_at = NOW()
WHERE status = 'running'
  AND COALESCE(is_chunked, FALSE) = FALSE
  AND lease_expires_at < NOW()
  AND attempt_count >= %(max_attempts)s
RETURNING id, progress_pct;
"""

HEARTBEAT_SQL = """
UPDATE render_jobs
SET progress_pct = GREATEST(progress_pct, %(progress_pct)s),
    lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    updated_at = NOW()
WHERE id = %(job_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING status, progress_pct;
"""

UPDATE_CHUNKED_PROGRESS_SQL = """
UPDATE render_jobs
SET progress_pct = GREATEST(progress_pct, %(progress_pct)s),
    updated_at = NOW()
WHERE id = %(job_id)s
  AND status = 'running'
RETURNING status, progress_pct;
"""

CHECK_STATUS_SQL = """
SELECT status
FROM render_jobs
WHERE id = %(job_id)s;
"""

LOAD_JOB_SQL = """
SELECT id, project_id, owner_user_id, spec_json, attempt_count, status
FROM render_jobs
WHERE id = %(job_id)s;
"""

MARK_SUCCEEDED_SQL = """
UPDATE render_jobs
SET status = 'succeeded',
    progress_pct = 100,
    lease_expires_at = NULL,
    output_object_key = %(output_object_key)s,
    thumbnail_key = %(thumbnail_key)s,
    expires_at = %(expires_at)s,
    error_message = NULL,
    updated_at = NOW()
WHERE id = %(job_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING id;
"""

MARK_FAILED_SQL = """
UPDATE render_jobs
SET status = CASE
        WHEN attempt_count >= %(max_attempts)s OR %(fatal)s THEN 'failed'
        ELSE 'queued'
    END,
    progress_pct = CASE
        WHEN attempt_count >= %(max_attempts)s OR %(fatal)s THEN progress_pct
        ELSE 0
    END,
    lease_expires_at = NULL,
    error_message = %(error_message)s,
    updated_at = NOW()
WHERE id = %(job_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING status, progress_pct;
"""

MARK_JOB_CHUNKED_SQL = """
UPDATE render_jobs
SET is_chunked = TRUE,
    total_frames = %(total_frames)s,
    lease_expires_at = NULL,
    updated_at = NOW()
WHERE id = %(job_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING id;
"""

FAIL_CHUNKED_JOB_SQL = """
UPDATE render_jobs
SET status = 'failed',
    lease_expires_at = NULL,
    error_message = %(error_message)s,
    updated_at = NOW()
WHERE id = %(job_id)s
  AND status = 'running'
RETURNING id, owner_user_id, project_id, progress_pct;
"""

INSERT_CHUNK_SQL = """
INSERT INTO render_job_chunks (
    id, render_job_id, chunk_index, start_frame, end_frame,
    preroll_frames, status, attempt_count, created_at, updated_at
)
VALUES (
    %(id)s, %(render_job_id)s, %(chunk_index)s, %(start_frame)s, %(end_frame)s,
    %(preroll_frames)s, 'queued', 0, NOW(), NOW()
)
ON CONFLICT (render_job_id, chunk_index) DO NOTHING;
"""

REAP_EXHAUSTED_CHUNKS_SQL = """
UPDATE render_job_chunks
SET status = 'failed',
    lease_expires_at = NULL,
    error_message = COALESCE(
        error_message,
        'Render chunk exceeded maximum retry attempts after lease expiration'
    ),
    updated_at = NOW()
WHERE status = 'running'
  AND lease_expires_at < NOW()
  AND attempt_count >= %(max_attempts)s
RETURNING id, render_job_id, chunk_index;
"""

CLAIM_CHUNK_SQL = """
WITH candidate AS (
    SELECT c.id
    FROM render_job_chunks c
    JOIN render_jobs rj ON rj.id = c.render_job_id
    WHERE rj.status = 'running'
      AND (
          c.status = 'queued'
          OR (c.status = 'running' AND c.lease_expires_at < NOW())
      )
      AND c.attempt_count < %(max_attempts)s
    ORDER BY rj.created_at ASC, c.chunk_index ASC
    FOR UPDATE OF c SKIP LOCKED
    LIMIT 1
)
UPDATE render_job_chunks c
SET status = 'running',
    attempt_count = c.attempt_count + 1,
    lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    updated_at = NOW()
FROM candidate
WHERE c.id = candidate.id
RETURNING c.id, c.render_job_id, c.chunk_index, c.start_frame, c.end_frame,
          c.preroll_frames, c.attempt_count;
"""

CLAIM_CHUNK_FOR_JOB_SQL = """
WITH candidate AS (
    SELECT c.id
    FROM render_job_chunks c
    JOIN render_jobs rj ON rj.id = c.render_job_id
    WHERE c.render_job_id = %(job_id)s
      AND rj.status = 'running'
      AND (
          c.status = 'queued'
          OR (c.status = 'running' AND c.lease_expires_at < NOW())
      )
      AND c.attempt_count < %(max_attempts)s
    ORDER BY c.chunk_index ASC
    FOR UPDATE OF c SKIP LOCKED
    LIMIT 1
)
UPDATE render_job_chunks c
SET status = 'running',
    attempt_count = c.attempt_count + 1,
    lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    updated_at = NOW()
FROM candidate
WHERE c.id = candidate.id
RETURNING c.id, c.render_job_id, c.chunk_index, c.start_frame, c.end_frame,
          c.preroll_frames, c.attempt_count;
"""

CHUNK_HEARTBEAT_SQL = """
UPDATE render_job_chunks
SET lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    updated_at = NOW()
WHERE id = %(chunk_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING status;
"""

MARK_CHUNK_SUCCEEDED_SQL = """
UPDATE render_job_chunks
SET status = 'succeeded',
    lease_expires_at = NULL,
    chunk_object_key = %(chunk_object_key)s,
    error_message = NULL,
    updated_at = NOW()
WHERE id = %(chunk_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING render_job_id;
"""

MARK_CHUNK_FAILED_SQL = """
UPDATE render_job_chunks
SET status = CASE
        WHEN attempt_count >= %(max_attempts)s OR %(fatal)s THEN 'failed'
        ELSE 'queued'
    END,
    lease_expires_at = NULL,
    error_message = %(error_message)s,
    updated_at = NOW()
WHERE id = %(chunk_id)s
  AND status = 'running'
  AND attempt_count = %(attempt_count)s
RETURNING status;
"""

CANCEL_REMAINING_CHUNKS_SQL = """
UPDATE render_job_chunks
SET status = 'cancelled',
    lease_expires_at = NULL,
    updated_at = NOW()
WHERE render_job_id = %(job_id)s
  AND status IN ('queued', 'running');
"""

CHUNK_PROGRESS_SUMMARY_SQL = """
SELECT
    COUNT(*)::INTEGER AS total_chunks,
    COUNT(*) FILTER (WHERE status = 'succeeded')::INTEGER AS succeeded_chunks
FROM render_job_chunks
WHERE render_job_id = %(job_id)s;
"""

CLAIM_READY_FINALIZE_SQL = """
WITH ready_job AS (
    SELECT rj.id
    FROM render_jobs rj
    WHERE rj.status = 'running'
      AND rj.is_chunked = TRUE
      AND (rj.lease_expires_at IS NULL OR rj.lease_expires_at < NOW())
      AND NOT EXISTS (
          SELECT 1
          FROM render_job_chunks c
          WHERE c.render_job_id = rj.id
            AND c.status != 'succeeded'
      )
      AND EXISTS (
          SELECT 1
          FROM render_job_chunks c
          WHERE c.render_job_id = rj.id
      )
    ORDER BY rj.created_at ASC
    FOR UPDATE OF rj SKIP LOCKED
    LIMIT 1
)
UPDATE render_jobs rj
SET lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    progress_pct = GREATEST(rj.progress_pct, 96),
    updated_at = NOW()
FROM ready_job
WHERE rj.id = ready_job.id
RETURNING rj.id, rj.project_id, rj.owner_user_id, rj.spec_json, rj.attempt_count;
"""

CLAIM_SPECIFIC_FINALIZE_SQL = """
WITH ready_job AS (
    SELECT rj.id
    FROM render_jobs rj
    WHERE rj.id = %(job_id)s
      AND rj.status = 'running'
      AND rj.is_chunked = TRUE
      AND (rj.lease_expires_at IS NULL OR rj.lease_expires_at < NOW())
      AND NOT EXISTS (
          SELECT 1
          FROM render_job_chunks c
          WHERE c.render_job_id = rj.id
            AND c.status != 'succeeded'
      )
      AND EXISTS (
          SELECT 1
          FROM render_job_chunks c
          WHERE c.render_job_id = rj.id
      )
    FOR UPDATE OF rj SKIP LOCKED
)
UPDATE render_jobs rj
SET lease_expires_at = NOW() + make_interval(secs => %(lease_seconds)s),
    progress_pct = GREATEST(rj.progress_pct, 96),
    updated_at = NOW()
FROM ready_job
WHERE rj.id = ready_job.id
RETURNING rj.id, rj.project_id, rj.owner_user_id, rj.spec_json, rj.attempt_count;
"""

LOAD_SUCCEEDED_CHUNKS_SQL = """
SELECT chunk_index, start_frame, end_frame, chunk_object_key
FROM render_job_chunks
WHERE render_job_id = %(job_id)s
  AND status = 'succeeded'
ORDER BY chunk_index ASC;
"""

LOAD_PROJECT_SQL = """
SELECT title
FROM projects
WHERE id = %(project_id)s;
"""

LOAD_STAGES_SQL = """
SELECT
    id,
    project_id,
    name,
    stage_order,
    day_index,
    trim_start_s,
    trim_end_s,
    silver_object_key,
    distance_m,
    elevation_gain_m,
    moving_seconds,
    elapsed_seconds,
    has_timestamps,
    activity_type
FROM stages
WHERE project_id = %(project_id)s
ORDER BY stage_order ASC;
"""

LOAD_PROJECT_PHOTOS_SQL = """
SELECT
    id,
    project_id,
    thumb_object_key,
    width,
    height,
    caption,
    taken_at_utc,
    lat,
    lon,
    matched_lat,
    matched_lon,
    matched_progress,
    match_method,
    object_key
FROM project_photos
WHERE project_id = %(project_id)s
  AND enabled = TRUE
ORDER BY taken_at_utc ASC NULLS LAST, created_at ASC;
"""

INSERT_AUDIT_EVENT_SQL = """
INSERT INTO audit_events (user_id, project_id, event_type, metadata_json)
VALUES (%(user_id)s, %(project_id)s, %(event_type)s, %(metadata_json)s::jsonb);
"""


class DbCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...


class DbConnection(Protocol):
    def execute(
        self,
        query: Any,
        params: Mapping[str, Any] | None = None,
    ) -> DbCursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


class RenderCallable(Protocol):
    async def __call__(
        self,
        manifest: RenderManifest,
        output_mp4_path: Path,
        settings: WorkerSettings,
        *,
        on_progress: Callable[[int, int], bool] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Path: ...


@dataclass(frozen=True)
class ClaimedJob:
    """Row claimed from `render_jobs` via `FOR UPDATE SKIP LOCKED`."""

    id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: uuid.UUID
    spec_json: dict[str, object]
    attempt_count: int


@dataclass(frozen=True)
class ClaimedChunk:
    """Row claimed from `render_job_chunks` via `FOR UPDATE SKIP LOCKED`."""

    id: uuid.UUID
    render_job_id: uuid.UUID
    chunk_index: int
    start_frame: int
    end_frame: int
    preroll_frames: int
    attempt_count: int


def _parse_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _parse_spec_json(value: Any) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return dict(loaded)
    raise ValueError("render_jobs.spec_json must be a JSON object")


def _sanitize_error_message(exc: BaseException) -> str:
    """Map internal exceptions to safe, non-leaking user-facing error messages."""
    if isinstance(exc, ValidationError):
        return "Invalid render specification"
    if isinstance(exc, ValueError):
        detail = str(exc).lower()
        if "zero stages" in detail:
            return "Cannot render a project with zero stages"
        if "zero active telemetry points" in detail or "trim" in detail:
            return "Stage trim window is invalid or excludes all telemetry points"
        if "not found" in detail:
            return "Project or stage data could not be found"
        return "Invalid render job parameters"
    if isinstance(exc, FileNotFoundError):
        return "Required track telemetry artifact is missing"
    if isinstance(exc, EncoderError):
        return "Video encoding or verification failed"
    if isinstance(exc, RenderError):
        return "Map rendering failed in headless browser"
    return "Render worker encountered an internal error"


def notify_render_job_event(
    conn: DbConnection,
    *,
    job_id: uuid.UUID,
    status: str,
    progress_pct: int,
) -> None:
    """Emit PostgreSQL `pg_notify('render_job_events', ...)` in the active transaction."""
    payload = json.dumps(
        {
            "job_id": str(job_id),
            "status": status,
            "progress_pct": int(progress_pct),
        },
        separators=(",", ":"),
    )
    conn.execute(NOTIFY_RENDER_JOB_SQL, {"payload": payload})


def _delete_blob_best_effort(store: ObjectStore, key: str) -> None:
    try:
        store.delete(key)
    except Exception:
        logger.debug("Best-effort blob delete failed for %s", key, exc_info=True)


def _render_fn_accepts_browser(render_fn: Callable[..., Any]) -> bool:
    params = inspect.signature(render_fn).parameters
    return "browser" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _invoke_render_fn(
    render_fn: Callable[..., Any],
    manifest: RenderManifest,
    output_mp4_path: Path,
    settings: WorkerSettings,
    *,
    on_progress: Callable[[int, int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    frame_start: int = 0,
    frame_end: int | None = None,
    preroll_frames: int = 0,
    photo_assets: Mapping[str, bytes] | None = None,
    gop_aligned: bool = False,
    browser: Any = None,
) -> Any:
    """Call `render_fn` passing extended chunk/photo/browser kwargs only if supported."""
    sig = inspect.signature(render_fn)
    params = sig.parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    kwargs: dict[str, Any] = {
        "on_progress": on_progress,
        "is_cancelled": is_cancelled,
    }
    optional_kwargs: dict[str, Any] = {
        "frame_start": frame_start,
        "frame_end": frame_end,
        "preroll_frames": preroll_frames,
        "photo_assets": photo_assets,
        "gop_aligned": gop_aligned,
    }
    if browser is not None:
        optional_kwargs["browser"] = browser
    for k, v in optional_kwargs.items():
        if accepts_var_kw or k in params:
            kwargs[k] = v

    return render_fn(manifest, output_mp4_path, settings, **kwargs)


def _execute_render(
    render_fn: RenderCallable,
    manifest: RenderManifest,
    output_mp4_path: Path,
    settings: WorkerSettings,
    *,
    browser_host: WarmPlaywrightHost | None = None,
    on_progress: Callable[[int, int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    frame_start: int = 0,
    frame_end: int | None = None,
    preroll_frames: int = 0,
    photo_assets: Mapping[str, bytes] | None = None,
    gop_aligned: bool = False,
) -> Any:
    """Run `render_fn` on `browser_host`'s warm Chromium/event loop when available."""
    if browser_host is None:
        return asyncio.run(
            _invoke_render_fn(
                render_fn,
                manifest,
                output_mp4_path,
                settings,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
                frame_start=frame_start,
                frame_end=frame_end,
                preroll_frames=preroll_frames,
                photo_assets=photo_assets,
                gop_aligned=gop_aligned,
            )
        )

    async def _run_with_warm_host() -> Any:
        if not _render_fn_accepts_browser(render_fn):
            return await _invoke_render_fn(
                render_fn,
                manifest,
                output_mp4_path,
                settings,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
                frame_start=frame_start,
                frame_end=frame_end,
                preroll_frames=preroll_frames,
                photo_assets=photo_assets,
                gop_aligned=gop_aligned,
            )
        browser = await browser_host.acquire_browser()
        try:
            return await _invoke_render_fn(
                render_fn,
                manifest,
                output_mp4_path,
                settings,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
                frame_start=frame_start,
                frame_end=frame_end,
                preroll_frames=preroll_frames,
                photo_assets=photo_assets,
                gop_aligned=gop_aligned,
                browser=browser,
            )
        except RenderCancelledError:
            if not browser.is_connected():
                await browser_host.reset_browser()
            raise
        except Exception:
            await browser_host.reset_browser()
            raise

    return browser_host.run_coro(_run_with_warm_host())


def reap_exhausted_jobs(conn: DbConnection, *, max_attempts: int) -> int:
    """Fail expired running jobs that have already reached `max_attempts`."""
    cur = conn.execute(REAP_EXHAUSTED_SQL, {"max_attempts": max_attempts})
    rows = cur.fetchall()
    for r in rows:
        notify_render_job_event(
            conn,
            job_id=_parse_uuid(r[0]),
            status="failed",
            progress_pct=int(r[1]) if len(r) > 1 and r[1] is not None else 0,
        )
    conn.commit()
    return max(len(rows), max(0, int(cur.rowcount)))


def reap_exhausted_chunks(conn: DbConnection, *, max_attempts: int) -> int:
    """Fail expired running chunks at `max_attempts` and fail their parent jobs."""
    cur = conn.execute(REAP_EXHAUSTED_CHUNKS_SQL, {"max_attempts": max_attempts})
    rows = cur.fetchall()
    if rows:
        failed_job_ids: set[uuid.UUID] = set()
        for r in rows:
            failed_job_ids.add(_parse_uuid(r[1]))
        for jid in failed_job_ids:
            parent_row = conn.execute(
                FAIL_CHUNKED_JOB_SQL,
                {
                    "job_id": jid,
                    "error_message": (
                        "Render chunk exceeded maximum retry attempts after lease expiration"
                    ),
                },
            ).fetchone()
            conn.execute(CANCEL_REMAINING_CHUNKS_SQL, {"job_id": jid})
            if parent_row is not None:
                notify_render_job_event(
                    conn,
                    job_id=jid,
                    status="failed",
                    progress_pct=int(parent_row[3]) if parent_row[3] is not None else 0,
                )
    conn.commit()
    return max(len(rows), max(0, int(cur.rowcount)))


def claim_next_job(
    conn: DbConnection,
    *,
    max_attempts: int,
    lease_seconds: int,
) -> ClaimedJob | None:
    """Claim the oldest queued or expired-lease render job using `FOR UPDATE SKIP LOCKED`."""
    row = conn.execute(
        CLAIM_JOB_SQL,
        {"max_attempts": max_attempts, "lease_seconds": lease_seconds},
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    job = ClaimedJob(
        id=_parse_uuid(row[0]),
        project_id=_parse_uuid(row[1]),
        owner_user_id=_parse_uuid(row[2]),
        spec_json=_parse_spec_json(row[3]),
        attempt_count=int(row[4]),
    )
    notify_render_job_event(conn, job_id=job.id, status="running", progress_pct=0)
    conn.commit()
    return job


def claim_next_chunk(
    conn: DbConnection,
    *,
    max_attempts: int,
    lease_seconds: int,
    job_id: uuid.UUID | None = None,
) -> ClaimedChunk | None:
    """Claim the next queued or expired-lease chunk from `render_job_chunks`."""
    if job_id is not None:
        row = conn.execute(
            CLAIM_CHUNK_FOR_JOB_SQL,
            {"job_id": job_id, "max_attempts": max_attempts, "lease_seconds": lease_seconds},
        ).fetchone()
    else:
        row = conn.execute(
            CLAIM_CHUNK_SQL,
            {"max_attempts": max_attempts, "lease_seconds": lease_seconds},
        ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ClaimedChunk(
        id=_parse_uuid(row[0]),
        render_job_id=_parse_uuid(row[1]),
        chunk_index=int(row[2]),
        start_frame=int(row[3]),
        end_frame=int(row[4]),
        preroll_frames=int(row[5]),
        attempt_count=int(row[6]),
    )


def update_job_progress(
    conn: DbConnection,
    *,
    job_id: uuid.UUID,
    attempt_count: int,
    progress_pct: int,
    lease_seconds: int,
) -> bool:
    """Update `progress_pct` and renew lease; return False if fenced or no longer running."""
    clamped = max(0, min(99, progress_pct))
    cur = conn.execute(
        HEARTBEAT_SQL,
        {
            "job_id": job_id,
            "attempt_count": attempt_count,
            "progress_pct": clamped,
            "lease_seconds": lease_seconds,
        },
    )
    row = cur.fetchone()
    if row is not None and str(row[0]) == "running":
        actual_pct = int(row[1]) if len(row) > 1 and row[1] is not None else clamped
        notify_render_job_event(conn, job_id=job_id, status="running", progress_pct=actual_pct)
        conn.commit()
        return True
    conn.commit()
    return False


def update_chunk_progress(
    conn: DbConnection,
    *,
    chunk_id: uuid.UUID,
    attempt_count: int,
    lease_seconds: int,
) -> bool:
    """Renew chunk lease; return False if fenced or no longer running."""
    row = conn.execute(
        CHUNK_HEARTBEAT_SQL,
        {"chunk_id": chunk_id, "attempt_count": attempt_count, "lease_seconds": lease_seconds},
    ).fetchone()
    conn.commit()
    return row is not None and str(row[0]) == "running"


def is_job_cancelled(conn: DbConnection, *, job_id: uuid.UUID) -> bool:
    """Return True if the job was cancelled or removed."""
    row = conn.execute(CHECK_STATUS_SQL, {"job_id": job_id}).fetchone()
    conn.commit()
    if row is None:
        return True
    return str(row[0]) == "cancelled"


def mark_job_succeeded(
    conn: DbConnection,
    *,
    job_id: uuid.UUID,
    attempt_count: int,
    output_object_key: str,
    thumbnail_key: str,
    expires_at: datetime,
) -> bool:
    """Transition a running job to `succeeded`; return False if cancelled or fenced out."""
    cur = conn.execute(
        MARK_SUCCEEDED_SQL,
        {
            "job_id": job_id,
            "attempt_count": attempt_count,
            "output_object_key": output_object_key,
            "thumbnail_key": thumbnail_key,
            "expires_at": expires_at,
        },
    )
    row = cur.fetchone()
    if row is not None:
        notify_render_job_event(conn, job_id=job_id, status="succeeded", progress_pct=100)
        conn.commit()
        return True
    conn.commit()
    return False


def mark_job_failed(
    conn: DbConnection,
    *,
    job_id: uuid.UUID,
    attempt_count: int,
    error_message: str,
    max_attempts: int,
    fatal: bool = False,
) -> str | None:
    """Mark a running job as `failed` (terminal) or re-queue if attempts remain."""
    cur = conn.execute(
        MARK_FAILED_SQL,
        {
            "job_id": job_id,
            "attempt_count": attempt_count,
            "error_message": error_message[:1000],
            "max_attempts": max_attempts,
            "fatal": fatal,
        },
    )
    row = cur.fetchone()
    if row is not None:
        final_status = str(row[0])
        pct = int(row[1]) if len(row) > 1 and row[1] is not None else 0
        notify_render_job_event(conn, job_id=job_id, status=final_status, progress_pct=pct)
        conn.commit()
        return final_status
    conn.commit()
    return None


def load_project_title(conn: DbConnection, *, project_id: uuid.UUID) -> str:
    row = conn.execute(LOAD_PROJECT_SQL, {"project_id": project_id}).fetchone()
    conn.commit()
    if row is None:
        raise ValueError(f"Project {project_id} not found")
    return str(row[0])


def load_project_stages(conn: DbConnection, *, project_id: uuid.UUID) -> list[StageRecord]:
    rows = conn.execute(LOAD_STAGES_SQL, {"project_id": project_id}).fetchall()
    conn.commit()
    return [
        StageRecord(
            id=_parse_uuid(r[0]),
            project_id=_parse_uuid(r[1]),
            name=str(r[2]),
            stage_order=int(r[3]),
            day_index=int(r[4]),
            trim_start_s=float(r[5]) if r[5] is not None else None,
            trim_end_s=float(r[6]) if r[6] is not None else None,
            silver_object_key=str(r[7]),
            distance_m=float(r[8]),
            elevation_gain_m=float(r[9]),
            moving_seconds=float(r[10]),
            elapsed_seconds=float(r[11]),
            has_timestamps=bool(r[12]),
            activity_type=(
                normalize_activity_type(r[13], DEFAULT_ACTIVITY_TYPE)
                if len(r) > 13
                else DEFAULT_ACTIVITY_TYPE
            ),
        )
        for r in rows
    ]


def load_project_photos(conn: DbConnection, *, project_id: uuid.UUID) -> list[PhotoRecord]:
    rows = conn.execute(LOAD_PROJECT_PHOTOS_SQL, {"project_id": project_id}).fetchall()
    conn.commit()
    return [
        PhotoRecord(
            id=_parse_uuid(r[0]),
            project_id=_parse_uuid(r[1]),
            thumb_object_key=str(r[2]),
            width=int(r[3]),
            height=int(r[4]),
            caption=str(r[5] or ""),
            taken_at_utc=r[6],
            lat=float(r[7]) if r[7] is not None else None,
            lon=float(r[8]) if r[8] is not None else None,
            matched_lat=float(r[9]) if r[9] is not None else None,
            matched_lon=float(r[10]) if r[10] is not None else None,
            matched_progress=float(r[11]) if r[11] is not None else None,
            match_method=str(r[12] or "unmatched"),
            object_key=str(r[13]) if len(r) > 13 and r[13] else "",
        )
        for r in rows
    ]


def record_audit_event(
    conn: DbConnection,
    *,
    user_id: uuid.UUID,
    event_type: str,
    metadata: dict[str, object],
    project_id: uuid.UUID | None = None,
) -> None:
    """Persist an audit event, falling back to `project_id=None` if the project was deleted."""
    params: dict[str, Any] = {
        "user_id": user_id,
        "project_id": project_id,
        "event_type": event_type,
        "metadata_json": json.dumps(metadata),
    }
    try:
        conn.execute(INSERT_AUDIT_EVENT_SQL, params)
        conn.commit()
    except psycopg.errors.ForeignKeyViolation:
        conn.rollback()
        if project_id is not None:
            logger.warning(
                "Project %s no longer exists; recording audit event %s without project_id",
                project_id,
                event_type,
            )
            try:
                conn.execute(INSERT_AUDIT_EVENT_SQL, {**params, "project_id": None})
                conn.commit()
                return
            except psycopg.errors.ForeignKeyViolation:
                conn.rollback()
        logger.warning(
            "Skipping audit event %s due to missing foreign key target",
            event_type,
        )


def default_conn_factory(
    settings: WorkerSettings,
) -> Callable[[], AbstractContextManager[DbConnection]]:
    """Build a context-managed psycopg connection factory from WorkerSettings."""

    @contextmanager
    def _connect() -> Iterator[DbConnection]:
        with psycopg.connect(settings.psycopg_dsn) as conn:
            yield conn

    return _connect


class _JobLeaseSession:
    """Thread-safe, connection-reusing lease heartbeat and DB session for a claimed job."""

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[DbConnection]],
        *,
        job: ClaimedJob,
        lease_seconds: int,
        heartbeat_interval_s: float,
    ) -> None:
        self._conn_factory = conn_factory
        self._job = job
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_s = heartbeat_interval_s
        self._lock = threading.Lock()
        self._stack = ExitStack()
        self._conn: DbConnection | None = None
        self._progress_pct = 0
        self._last_db_heartbeat_at: float | None = None
        self._last_cancel_check_at: float | None = None
        self._lease_lost = False
        self._stop_event = threading.Event()
        self._renew_thread: threading.Thread | None = None

    def __enter__(self) -> _JobLeaseSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop_background_renewer()
        with self._lock:
            self._conn = None
            self._stack.close()

    def _get_conn_locked(self) -> DbConnection:
        if self._conn is None:
            self._conn = self._stack.enter_context(self._conn_factory())
        return self._conn

    def _reset_conn_locked(self) -> None:
        self._conn = None
        self._stack.close()
        self._stack = ExitStack()

    def run[T](self, fn: Callable[[DbConnection], T]) -> T:
        """Execute a DB operation on the shared connection, reconnecting once if dropped."""
        with self._lock:
            try:
                return fn(self._get_conn_locked())
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset_conn_locked()
                return fn(self._get_conn_locked())

    def update_progress(self, progress_pct: int, *, force: bool = False) -> bool:
        """Update progress and renew lease, throttling redundant frame heartbeats."""
        clamped = max(0, min(99, progress_pct))
        with self._lock:
            self._progress_pct = max(self._progress_pct, clamped)
            if self._lease_lost:
                return False
            now = time.monotonic()
            if (
                not force
                and self._last_db_heartbeat_at is not None
                and (now - self._last_db_heartbeat_at) < self._heartbeat_interval_s
            ):
                return True

            def _do_update(conn: DbConnection) -> bool:
                return update_job_progress(
                    conn,
                    job_id=self._job.id,
                    attempt_count=self._job.attempt_count,
                    progress_pct=self._progress_pct,
                    lease_seconds=self._lease_seconds,
                )

            try:
                ok = _do_update(self._get_conn_locked())
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset_conn_locked()
                ok = _do_update(self._get_conn_locked())

            done_at = time.monotonic()
            self._last_db_heartbeat_at = done_at
            self._last_cancel_check_at = done_at
            if not ok:
                self._lease_lost = True
            return ok

    def on_render_progress(self, completed_frames: int, total_frames: int) -> bool:
        pct = max(1, min(95, round((completed_frames / max(1, total_frames)) * 95)))
        return self.update_progress(pct, force=completed_frames >= total_frames)

    def is_cancelled(self) -> bool:
        """Check cancellation with time-based throttling to avoid per-frame DB polling."""
        with self._lock:
            if self._lease_lost:
                return True
            now = time.monotonic()
            if (
                self._last_cancel_check_at is not None
                and (now - self._last_cancel_check_at) < self._heartbeat_interval_s
            ):
                return False

            try:
                cancelled = is_job_cancelled(self._get_conn_locked(), job_id=self._job.id)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset_conn_locked()
                cancelled = is_job_cancelled(self._get_conn_locked(), job_id=self._job.id)

            self._last_cancel_check_at = time.monotonic()
            return cancelled

    def is_job_cancelled_or_deleted(self) -> bool:
        return self.run(lambda conn: is_job_cancelled(conn, job_id=self._job.id))

    def start_background_renewer(self) -> None:
        if self._renew_thread is not None:
            return
        self._stop_event.clear()

        def _renew_loop() -> None:
            while not self._stop_event.wait(timeout=self._heartbeat_interval_s):
                try:
                    if not self.update_progress(self._progress_pct, force=True):
                        self._stop_event.set()
                        return
                except Exception:
                    logger.warning(
                        "Background lease renewal failed for job %s",
                        self._job.id,
                        exc_info=True,
                    )

        self._renew_thread = threading.Thread(
            target=_renew_loop,
            name=f"trip-lease-renew-{self._job.id}",
            daemon=True,
        )
        self._renew_thread.start()

    def stop_background_renewer(self) -> None:
        self._stop_event.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=5.0)
            self._renew_thread = None


class _ChunkLeaseSession:
    """Thread-safe lease heartbeat and cancellation monitor for a claimed chunk."""

    def __init__(
        self,
        conn_factory: Callable[[], AbstractContextManager[DbConnection]],
        *,
        chunk: ClaimedChunk,
        lease_seconds: int,
        heartbeat_interval_s: float,
    ) -> None:
        self._conn_factory = conn_factory
        self._chunk = chunk
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_s = heartbeat_interval_s
        self._lock = threading.Lock()
        self._stack = ExitStack()
        self._conn: DbConnection | None = None
        self._last_heartbeat_at: float | None = None
        self._lease_lost = False
        self._stop_event = threading.Event()
        self._renew_thread: threading.Thread | None = None

    def __enter__(self) -> _ChunkLeaseSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop_background_renewer()
        with self._lock:
            self._conn = None
            self._stack.close()

    def _get_conn_locked(self) -> DbConnection:
        if self._conn is None:
            self._conn = self._stack.enter_context(self._conn_factory())
        return self._conn

    def _reset_conn_locked(self) -> None:
        self._conn = None
        self._stack.close()
        self._stack = ExitStack()

    def run[T](self, fn: Callable[[DbConnection], T]) -> T:
        with self._lock:
            try:
                return fn(self._get_conn_locked())
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset_conn_locked()
                return fn(self._get_conn_locked())

    def renew_lease(self, *, force: bool = False) -> bool:
        with self._lock:
            if self._lease_lost:
                return False
            now = time.monotonic()
            if (
                not force
                and self._last_heartbeat_at is not None
                and (now - self._last_heartbeat_at) < self._heartbeat_interval_s
            ):
                return True

            def _do_renew(conn: DbConnection) -> bool:
                if is_job_cancelled(conn, job_id=self._chunk.render_job_id):
                    return False
                return update_chunk_progress(
                    conn,
                    chunk_id=self._chunk.id,
                    attempt_count=self._chunk.attempt_count,
                    lease_seconds=self._lease_seconds,
                )

            try:
                ok = _do_renew(self._get_conn_locked())
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset_conn_locked()
                ok = _do_renew(self._get_conn_locked())

            self._last_heartbeat_at = time.monotonic()
            if not ok:
                self._lease_lost = True
            return ok

    def on_render_progress(self, _completed_frames: int, _total_frames: int) -> bool:
        return self.renew_lease(force=False)

    def is_cancelled(self) -> bool:
        with self._lock:
            if self._lease_lost:
                return True
            now = time.monotonic()
            if (
                self._last_heartbeat_at is not None
                and (now - self._last_heartbeat_at) < self._heartbeat_interval_s
            ):
                return False
            try:
                cancelled = is_job_cancelled(
                    self._get_conn_locked(), job_id=self._chunk.render_job_id
                )
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._reset_conn_locked()
                cancelled = is_job_cancelled(
                    self._get_conn_locked(), job_id=self._chunk.render_job_id
                )
            self._last_heartbeat_at = time.monotonic()
            return cancelled

    def start_background_renewer(self) -> None:
        if self._renew_thread is not None:
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(timeout=self._heartbeat_interval_s):
                try:
                    if not self.renew_lease(force=True):
                        self._stop_event.set()
                        return
                except Exception:
                    logger.warning(
                        "Background chunk lease renewal failed for chunk %s",
                        self._chunk.id,
                        exc_info=True,
                    )

        self._renew_thread = threading.Thread(
            target=_loop,
            name=f"trip-chunk-renew-{self._chunk.id}",
            daemon=True,
        )
        self._renew_thread.start()

    def stop_background_renewer(self) -> None:
        self._stop_event.set()
        if self._renew_thread is not None:
            self._renew_thread.join(timeout=5.0)
            self._renew_thread = None


def _compute_spec_total_frames(spec_json: dict[str, object]) -> int:
    spec = RenderSpecV2.model_validate(spec_json)
    fps = 15 if spec.is_preview else spec.fps
    duration_s = (
        min(float(spec.duration_seconds), 5.0) if spec.is_preview else float(spec.duration_seconds)
    )
    return max(1, round(duration_s * fps))


FrameCountCallable = Callable[[RenderManifest, WorkerSettings, WarmPlaywrightHost | None], int]


def probe_film_frame_count(
    manifest: RenderManifest,
    settings: WorkerSettings,
    browser_host: WarmPlaywrightHost | None = None,
) -> int:
    """Ask the render page for the cinematic film frame count on a warm or fresh browser."""
    if browser_host is None:
        return asyncio.run(probe_manifest_frame_count(manifest, settings))

    async def _probe_with_warm_host() -> int:
        browser = await browser_host.acquire_browser()
        try:
            return await probe_manifest_frame_count(manifest, settings, browser=browser)
        except Exception:
            await browser_host.reset_browser()
            raise

    return browser_host.run_coro(_probe_with_warm_host())


def _with_film_frame_count(manifest: RenderManifest, total_frames: int) -> RenderManifest:
    return manifest.model_copy(
        update={"total_frames": total_frames, "duration_s": total_frames / manifest.fps}
    )


def _load_manifest_and_photo_assets(
    conn: DbConnection,
    *,
    job_id: uuid.UUID,
    project_id: uuid.UUID,
    spec_json: dict[str, object],
    settings: WorkerSettings,
    store: ObjectStore,
    load_assets: bool = True,
) -> tuple[bool, RenderManifest | None, dict[str, bytes]]:
    if is_job_cancelled(conn, job_id=job_id):
        return (True, None, {})
    title = load_project_title(conn, project_id=project_id)
    stages = load_project_stages(conn, project_id=project_id)
    photos = load_project_photos(conn, project_id=project_id)
    photo_assets: dict[str, bytes] = {}
    manifest = build_render_manifest(
        job_id=job_id,
        project_id=project_id,
        project_title=title,
        spec_json=spec_json,
        stages=stages,
        store=store,
        photos=photos,
        max_points_per_stage=settings.max_manifest_points_per_stage,
        max_total_manifest_points=settings.max_total_manifest_points,
        embed_photo_data_uris=False,
        load_assets=load_assets,
        out_photo_assets=photo_assets if load_assets else None,
    )
    return (False, manifest, photo_assets)


def _load_pre_serialized_manifest_and_photo_assets(
    conn: DbConnection,
    *,
    job_id: uuid.UUID,
    project_id: uuid.UUID,
    spec_json: dict[str, object],
    settings: WorkerSettings,
    store: ObjectStore,
    load_assets: bool = True,
) -> tuple[bool, RenderManifest | None, dict[str, bytes]]:
    if is_job_cancelled(conn, job_id=job_id):
        return (True, None, {})

    manifest_key = chunk_manifest_key(job_id)
    manifest: RenderManifest | None = None
    try:
        raw_json = store.get_bytes(manifest_key)
        manifest = RenderManifest.model_validate_json(raw_json)
    except FileNotFoundError:
        manifest = None
    except Exception as exc:
        logger.warning(
            "Failed loading pre-serialized manifest %s: %s; falling back to rebuild",
            manifest_key,
            exc,
        )
        manifest = None

    if manifest is None:
        return _load_manifest_and_photo_assets(
            conn,
            job_id=job_id,
            project_id=project_id,
            spec_json=spec_json,
            settings=settings,
            store=store,
            load_assets=load_assets,
        )

    photo_assets: dict[str, bytes] = {}
    if load_assets and manifest.photos:
        needed_ids = {p.id for p in manifest.photos}
        photos = load_project_photos(conn, project_id=project_id)
        for photo in photos:
            photo_id_str = str(photo.id)
            if photo_id_str not in needed_ids:
                continue
            asset_key = photo_asset_key(manifest.spec, photo)
            try:
                photo_assets[photo_id_str] = store.get_bytes(asset_key)
            except FileNotFoundError:
                logger.warning(
                    "Photo asset %s missing for project %s; skipping",
                    asset_key,
                    project_id,
                )

    return (False, manifest, photo_assets)


def _split_job_into_chunks(
    conn: DbConnection,
    *,
    job: ClaimedJob,
    total_frames: int,
    chunk_size_frames: int,
    preroll_frames: int,
) -> bool:
    marked = conn.execute(
        MARK_JOB_CHUNKED_SQL,
        {
            "job_id": job.id,
            "attempt_count": job.attempt_count,
            "total_frames": total_frames,
        },
    ).fetchone()
    if marked is None:
        conn.commit()
        return False

    chunk_index = 0
    for start in range(0, total_frames, chunk_size_frames):
        end = min(total_frames, start + chunk_size_frames)
        conn.execute(
            INSERT_CHUNK_SQL,
            {
                "id": uuid.uuid4(),
                "render_job_id": job.id,
                "chunk_index": chunk_index,
                "start_frame": start,
                "end_frame": end,
                "preroll_frames": 0 if start == 0 else preroll_frames,
            },
        )
        chunk_index += 1
    conn.commit()
    return True


def _finalize_chunked_job(
    conn_factory: Callable[[], AbstractContextManager[DbConnection]],
    job: ClaimedJob,
    settings: WorkerSettings,
    store: ObjectStore,
    *,
    started_at: datetime | None = None,
) -> str:
    """Assemble succeeded chunks into final MP4, verify, extract thumbnail, and mark succeeded."""
    t_start = started_at or datetime.now(UTC)
    heartbeat_interval_s = min(
        settings.lease_heartbeat_interval_seconds,
        max(0.05, settings.lease_seconds / 3.0),
    )

    with _JobLeaseSession(
        conn_factory,
        job=job,
        lease_seconds=settings.lease_seconds,
        heartbeat_interval_s=heartbeat_interval_s,
    ) as lease:
        try:

            def _load_for_finalize(
                conn: DbConnection,
            ) -> tuple[bool, RenderManifest | None, list[tuple[int, int, int, str]]]:
                cancelled, manifest, _ = _load_pre_serialized_manifest_and_photo_assets(
                    conn,
                    job_id=job.id,
                    project_id=job.project_id,
                    spec_json=job.spec_json,
                    settings=settings,
                    store=store,
                    load_assets=False,
                )
                if cancelled or manifest is None:
                    return (True, None, [])
                chunk_rows = conn.execute(LOAD_SUCCEEDED_CHUNKS_SQL, {"job_id": job.id}).fetchall()
                conn.commit()
                chunks_meta = [
                    (int(r[0]), int(r[1]), int(r[2]), str(r[3]))
                    for r in chunk_rows
                    if r[3] is not None
                ]
                return (False, manifest, chunks_meta)

            cancelled, manifest, chunks_meta = lease.run(_load_for_finalize)
            if cancelled or manifest is None:
                if cancelled:
                    _delete_blob_best_effort(store, chunk_manifest_key(job.id))
                return "cancelled"
            if not chunks_meta:
                raise ValueError("No succeeded chunks available for finalization")

            output_key = f"outputs/{job.id}.mp4"
            thumbnail_key = f"outputs/{job.id}.webp"

            lease.start_background_renewer()
            try:
                with tempfile.TemporaryDirectory(prefix="trip_concat_") as tmpdir:
                    tmp_root = Path(tmpdir)
                    chunk_paths: list[Path] = []
                    for c_idx, _sf, _ef, obj_key in chunks_meta:
                        c_path = tmp_root / f"chunk_{c_idx:04d}.mp4"
                        c_path.write_bytes(store.get_bytes(obj_key))
                        chunk_paths.append(c_path)

                    mp4_path = tmp_root / f"{job.id}.mp4"
                    thumb_path = tmp_root / f"{job.id}.webp"

                    concat_mp4_chunks(
                        chunk_paths,
                        mp4_path,
                        timeout_seconds=settings.ffmpeg_timeout_seconds,
                    )

                    if not lease.update_progress(97, force=True):
                        raise RenderCancelledError(
                            f"Render job {job.id} cancelled or fenced before ffprobe"
                        )

                    probe = verify_mp4_output(
                        mp4_path,
                        expected_width=manifest.width,
                        expected_height=manifest.height,
                        expected_duration_s=manifest.duration_s,
                        duration_tolerance_s=max(1.5, manifest.duration_s * 0.25),
                        timeout_seconds=settings.ffprobe_timeout_seconds,
                    )

                    extract_thumbnail(
                        mp4_path,
                        thumb_path,
                        seek_seconds=min(0.5, manifest.duration_s * 0.25),
                        width=min(640, manifest.width),
                        timeout_seconds=settings.ffmpeg_timeout_seconds,
                    )

                    if not lease.update_progress(99, force=True):
                        raise RenderCancelledError(
                            f"Render job {job.id} cancelled or fenced before upload"
                        )

                    store.put_bytes(output_key, mp4_path.read_bytes(), content_type="video/mp4")
                    store.put_bytes(
                        thumbnail_key, thumb_path.read_bytes(), content_type="image/webp"
                    )
            finally:
                lease.stop_background_renewer()

            completed_at = datetime.now(UTC)
            expires_at = completed_at + timedelta(hours=settings.output_ttl_hours)

            def _commit_final(conn: DbConnection) -> str:
                committed = mark_job_succeeded(
                    conn,
                    job_id=job.id,
                    attempt_count=job.attempt_count,
                    output_object_key=output_key,
                    thumbnail_key=thumbnail_key,
                    expires_at=expires_at,
                )
                if not committed:
                    if is_job_cancelled(conn, job_id=job.id):
                        _delete_blob_best_effort(store, output_key)
                        _delete_blob_best_effort(store, thumbnail_key)
                        _delete_blob_best_effort(store, chunk_manifest_key(job.id))
                        return "cancelled"
                    return "lost_lease"

                _delete_blob_best_effort(store, chunk_manifest_key(job.id))
                for _ci, _sf, _ef, chunk_key in chunks_meta:
                    try:
                        store.delete(chunk_key)
                    except OSError:
                        pass

                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_succeeded",
                    metadata={
                        "job_id": str(job.id),
                        "project_id": str(job.project_id),
                        "width": probe.width,
                        "height": probe.height,
                        "duration_s": probe.duration_s,
                        "fps": manifest.fps,
                        "is_preview": manifest.is_preview,
                        "is_chunked": True,
                        "chunk_count": len(chunks_meta),
                        "render_started_at": t_start.isoformat(),
                        "render_completed_at": completed_at.isoformat(),
                    },
                )
                return "succeeded"

            return lease.run(_commit_final)

        except RenderCancelledError:
            lease.stop_background_renewer()

            def _cancel_cleanup(conn: DbConnection) -> str:
                if not is_job_cancelled(conn, job_id=job.id):
                    return "lost_lease"
                conn.execute(CANCEL_REMAINING_CHUNKS_SQL, {"job_id": job.id})
                conn.commit()
                _delete_blob_best_effort(store, chunk_manifest_key(job.id))
                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_cancelled",
                    metadata={"job_id": str(job.id), "project_id": str(job.project_id)},
                )
                return "cancelled"

            return lease.run(_cancel_cleanup)

        except Exception as exc:
            lease.stop_background_renewer()
            msg = _sanitize_error_message(exc)
            logger.exception("Chunked job %s finalization failed: %s", job.id, msg)

            def _fail_final(conn: DbConnection) -> str:
                row = conn.execute(
                    FAIL_CHUNKED_JOB_SQL, {"job_id": job.id, "error_message": msg}
                ).fetchone()
                conn.execute(CANCEL_REMAINING_CHUNKS_SQL, {"job_id": job.id})
                if row is not None:
                    notify_render_job_event(
                        conn,
                        job_id=job.id,
                        status="failed",
                        progress_pct=int(row[3]) if row[3] is not None else 0,
                    )
                conn.commit()
                _delete_blob_best_effort(store, chunk_manifest_key(job.id))
                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_failed",
                    metadata={"job_id": str(job.id), "error": msg, "is_chunked": True},
                )
                return "failed"

            return lease.run(_fail_final)


def process_claimed_chunk(
    conn_factory: Callable[[], AbstractContextManager[DbConnection]],
    chunk: ClaimedChunk,
    settings: WorkerSettings,
    store: ObjectStore,
    *,
    render_fn: RenderCallable = render_manifest_to_mp4,
    browser_host: WarmPlaywrightHost | None = None,
) -> str:
    """Render a single GOP-aligned chunk `[start_frame, end_frame)` and finalize if last."""
    heartbeat_interval_s = min(
        settings.lease_heartbeat_interval_seconds,
        max(0.05, settings.lease_seconds / 3.0),
    )

    job_meta: ClaimedJob | None = None
    with _ChunkLeaseSession(
        conn_factory,
        chunk=chunk,
        lease_seconds=settings.lease_seconds,
        heartbeat_interval_s=heartbeat_interval_s,
    ) as lease:
        try:

            def _load_inputs(
                conn: DbConnection,
            ) -> tuple[bool, ClaimedJob | None, RenderManifest | None, dict[str, bytes]]:
                row = conn.execute(LOAD_JOB_SQL, {"job_id": chunk.render_job_id}).fetchone()
                if row is None or str(row[5]) != "running":
                    conn.commit()
                    if row is not None and str(row[5]) in {"cancelled", "failed", "succeeded"}:
                        _delete_blob_best_effort(store, chunk_manifest_key(chunk.render_job_id))
                    return (True, None, None, {})
                loaded_job = ClaimedJob(
                    id=_parse_uuid(row[0]),
                    project_id=_parse_uuid(row[1]),
                    owner_user_id=_parse_uuid(row[2]),
                    spec_json=_parse_spec_json(row[3]),
                    attempt_count=int(row[4]),
                )
                cancelled, manifest, photo_assets = _load_pre_serialized_manifest_and_photo_assets(
                    conn,
                    job_id=loaded_job.id,
                    project_id=loaded_job.project_id,
                    spec_json=loaded_job.spec_json,
                    settings=settings,
                    store=store,
                    load_assets=True,
                )
                conn.commit()
                return (cancelled, loaded_job, manifest, photo_assets)

            cancelled_at_start, job_meta, manifest, photo_assets = lease.run(_load_inputs)
            if cancelled_at_start or job_meta is None or manifest is None:
                return "cancelled"

            chunk_key = f"outputs/chunks/{chunk.render_job_id}/chunk_{chunk.chunk_index:04d}.mp4"
            lease.start_background_renewer()
            try:
                with tempfile.TemporaryDirectory(prefix="trip_chunk_") as tmpdir:
                    chunk_mp4 = Path(tmpdir) / f"chunk_{chunk.chunk_index:04d}.mp4"
                    _execute_render(
                        render_fn,
                        manifest,
                        chunk_mp4,
                        settings,
                        browser_host=browser_host,
                        on_progress=lease.on_render_progress,
                        is_cancelled=lease.is_cancelled,
                        frame_start=chunk.start_frame,
                        frame_end=chunk.end_frame,
                        preroll_frames=chunk.preroll_frames,
                        photo_assets=photo_assets,
                        gop_aligned=True,
                    )
                    if not lease.renew_lease(force=True):
                        raise RenderCancelledError(
                            f"Chunk {chunk.id} cancelled or lost lease before upload"
                        )
                    store.put_bytes(chunk_key, chunk_mp4.read_bytes(), content_type="video/mp4")
            finally:
                lease.stop_background_renewer()

            def _complete_chunk(conn: DbConnection) -> tuple[str, ClaimedJob | None]:
                row = conn.execute(
                    MARK_CHUNK_SUCCEEDED_SQL,
                    {
                        "chunk_id": chunk.id,
                        "attempt_count": chunk.attempt_count,
                        "chunk_object_key": chunk_key,
                    },
                ).fetchone()
                if row is None:
                    conn.commit()
                    if is_job_cancelled(conn, job_id=chunk.render_job_id):
                        store.delete(chunk_key)
                        return ("cancelled", None)
                    return ("lost_lease", None)

                summary_row = conn.execute(
                    CHUNK_PROGRESS_SUMMARY_SQL, {"job_id": chunk.render_job_id}
                ).fetchone()
                total_c = int(summary_row[0]) if summary_row else 1
                done_c = int(summary_row[1]) if summary_row else 1
                pct = max(1, min(95, round((done_c / max(1, total_c)) * 95)))
                prog_row = conn.execute(
                    UPDATE_CHUNKED_PROGRESS_SQL,
                    {"job_id": chunk.render_job_id, "progress_pct": pct},
                ).fetchone()
                if prog_row is not None:
                    notify_render_job_event(
                        conn,
                        job_id=chunk.render_job_id,
                        status="running",
                        progress_pct=int(prog_row[1]),
                    )
                conn.commit()

                if done_c >= total_c:
                    fin_row = conn.execute(
                        CLAIM_SPECIFIC_FINALIZE_SQL,
                        {"job_id": chunk.render_job_id, "lease_seconds": settings.lease_seconds},
                    ).fetchone()
                    conn.commit()
                    if fin_row is not None:
                        return (
                            "ready_to_finalize",
                            ClaimedJob(
                                id=_parse_uuid(fin_row[0]),
                                project_id=_parse_uuid(fin_row[1]),
                                owner_user_id=_parse_uuid(fin_row[2]),
                                spec_json=_parse_spec_json(fin_row[3]),
                                attempt_count=int(fin_row[4]),
                            ),
                        )
                return ("succeeded", None)

            status, finalize_job = lease.run(_complete_chunk)
            if status == "ready_to_finalize" and finalize_job is not None:
                return _finalize_chunked_job(conn_factory, finalize_job, settings, store)
            return status

        except RenderCancelledError:
            lease.stop_background_renewer()

            def _handle_chunk_cancel(conn: DbConnection) -> str:
                if not is_job_cancelled(conn, job_id=chunk.render_job_id):
                    return "lost_lease"
                conn.execute(CANCEL_REMAINING_CHUNKS_SQL, {"job_id": chunk.render_job_id})
                conn.commit()
                _delete_blob_best_effort(store, chunk_manifest_key(chunk.render_job_id))
                return "cancelled"

            return lease.run(_handle_chunk_cancel)

        except Exception as exc:
            lease.stop_background_renewer()
            fatal = isinstance(exc, (ValidationError, ValueError))
            msg = _sanitize_error_message(exc)
            logger.warning(
                "Chunk %s (job %s) failed: %s",
                chunk.id,
                chunk.render_job_id,
                msg,
                exc_info=True,
            )

            def _handle_chunk_error(conn: DbConnection) -> str:
                res_row = conn.execute(
                    MARK_CHUNK_FAILED_SQL,
                    {
                        "chunk_id": chunk.id,
                        "attempt_count": chunk.attempt_count,
                        "error_message": msg,
                        "max_attempts": settings.max_attempts,
                        "fatal": fatal,
                    },
                ).fetchone()
                if res_row is None:
                    conn.commit()
                    return "lost_lease"
                chunk_status = str(res_row[0])
                if chunk_status == "failed":
                    parent_row = conn.execute(
                        FAIL_CHUNKED_JOB_SQL,
                        {"job_id": chunk.render_job_id, "error_message": msg},
                    ).fetchone()
                    conn.execute(CANCEL_REMAINING_CHUNKS_SQL, {"job_id": chunk.render_job_id})
                    if parent_row is not None:
                        notify_render_job_event(
                            conn,
                            job_id=chunk.render_job_id,
                            status="failed",
                            progress_pct=int(parent_row[3]) if parent_row[3] is not None else 0,
                        )
                    conn.commit()
                    _delete_blob_best_effort(store, chunk_manifest_key(chunk.render_job_id))
                    if job_meta is not None:
                        record_audit_event(
                            conn,
                            user_id=job_meta.owner_user_id,
                            project_id=job_meta.project_id,
                            event_type="render_failed",
                            metadata={
                                "job_id": str(chunk.render_job_id),
                                "chunk_id": str(chunk.id),
                                "error": msg,
                                "fatal": fatal,
                            },
                        )
                else:
                    conn.commit()
                return chunk_status

            return lease.run(_handle_chunk_error)


def process_claimed_job(
    conn_factory: Callable[[], AbstractContextManager[DbConnection]],
    job: ClaimedJob,
    settings: WorkerSettings,
    store: ObjectStore,
    *,
    render_fn: RenderCallable = render_manifest_to_mp4,
    browser_host: WarmPlaywrightHost | None = None,
    frame_count_fn: FrameCountCallable = probe_film_frame_count,
) -> str:
    """Execute unchunked or chunked Playwright frame capture, FFmpeg encode, probe, and upload."""
    started_at = datetime.now(UTC)
    heartbeat_interval_s = min(
        settings.lease_heartbeat_interval_seconds,
        max(0.05, settings.lease_seconds / 3.0),
    )

    with _JobLeaseSession(
        conn_factory,
        job=job,
        lease_seconds=settings.lease_seconds,
        heartbeat_interval_s=heartbeat_interval_s,
    ) as lease:
        try:
            total_frames_spec = _compute_spec_total_frames(job.spec_json)
            cinematic_stops = job.spec_json.get("photo_card_style") == "cinematic_stop"
            if cinematic_stops:

                def _load_probe_manifest(
                    conn: DbConnection,
                ) -> tuple[bool, RenderManifest | None, dict[str, bytes]]:
                    return _load_manifest_and_photo_assets(
                        conn,
                        job_id=job.id,
                        project_id=job.project_id,
                        spec_json=job.spec_json,
                        settings=settings,
                        store=store,
                        load_assets=False,
                    )

                probe_cancelled, probe_manifest, _ = lease.run(_load_probe_manifest)
                if probe_cancelled or probe_manifest is None:
                    return "cancelled"
                if probe_manifest.photos:
                    total_frames_spec = frame_count_fn(probe_manifest, settings, browser_host)

            if total_frames_spec > settings.chunk_threshold_frames:

                def _prepare_chunked(
                    conn: DbConnection,
                ) -> tuple[bool, bool]:
                    if is_job_cancelled(conn, job_id=job.id):
                        return (True, False)
                    # Build and pre-serialize manifest once before fanning out chunks
                    _cancelled, _manifest, _assets = _load_manifest_and_photo_assets(
                        conn,
                        job_id=job.id,
                        project_id=job.project_id,
                        spec_json=job.spec_json,
                        settings=settings,
                        store=store,
                        load_assets=False,
                    )
                    if _cancelled or _manifest is None:
                        return (True, False)
                    if cinematic_stops:
                        _manifest = _with_film_frame_count(_manifest, total_frames_spec)
                    manifest_key = chunk_manifest_key(job.id)
                    store.put_bytes(
                        manifest_key,
                        _manifest.model_dump_json().encode("utf-8"),
                        content_type="application/json",
                    )
                    split_ok = _split_job_into_chunks(
                        conn,
                        job=job,
                        total_frames=total_frames_spec,
                        chunk_size_frames=settings.chunk_size_frames,
                        preroll_frames=settings.chunk_preroll_frames,
                    )
                    if not split_ok:
                        _delete_blob_best_effort(store, manifest_key)
                    return (False, split_ok)

                cancelled_at_start, split_ok = lease.run(_prepare_chunked)
                if cancelled_at_start:
                    return "cancelled"
                if not split_ok:
                    return "lost_lease"

                # Close parent job session before processing chunks so workers act uniformly
                last_result = "chunked_in_progress"
                while True:
                    with conn_factory() as chunk_conn:
                        next_chunk = claim_next_chunk(
                            chunk_conn,
                            max_attempts=settings.max_attempts,
                            lease_seconds=settings.lease_seconds,
                            job_id=job.id,
                        )
                    if next_chunk is None:
                        break
                    last_result = process_claimed_chunk(
                        conn_factory,
                        next_chunk,
                        settings,
                        store,
                        render_fn=render_fn,
                        browser_host=browser_host,
                    )
                    if last_result in {"succeeded", "failed", "cancelled"}:
                        with conn_factory() as check_conn:
                            row = check_conn.execute(
                                CHECK_STATUS_SQL, {"job_id": job.id}
                            ).fetchone()
                            check_conn.commit()
                        if row is not None and str(row[0]) in {"succeeded", "failed", "cancelled"}:
                            return str(row[0])
                return last_result

            def _load_job_inputs(
                conn: DbConnection,
            ) -> tuple[bool, RenderManifest | None, dict[str, bytes]]:
                return _load_manifest_and_photo_assets(
                    conn,
                    job_id=job.id,
                    project_id=job.project_id,
                    spec_json=job.spec_json,
                    settings=settings,
                    store=store,
                )

            cancelled_at_start, manifest, photo_assets = lease.run(_load_job_inputs)
            if cancelled_at_start or manifest is None:
                return "cancelled"
            if cinematic_stops:
                manifest = _with_film_frame_count(manifest, total_frames_spec)

            output_key = f"outputs/{job.id}.mp4"
            thumbnail_key = f"outputs/{job.id}.webp"

            lease.start_background_renewer()
            try:
                with tempfile.TemporaryDirectory(prefix="trip_render_") as tmpdir:
                    tmp_root = Path(tmpdir)
                    mp4_path = tmp_root / f"{job.id}.mp4"
                    thumb_path = tmp_root / f"{job.id}.webp"

                    _execute_render(
                        render_fn,
                        manifest,
                        mp4_path,
                        settings,
                        browser_host=browser_host,
                        on_progress=lease.on_render_progress,
                        is_cancelled=lease.is_cancelled,
                        photo_assets=photo_assets,
                    )

                    if not lease.update_progress(97, force=True):
                        raise RenderCancelledError(
                            f"Render job {job.id} cancelled or fenced before ffprobe"
                        )

                    probe = verify_mp4_output(
                        mp4_path,
                        expected_width=manifest.width,
                        expected_height=manifest.height,
                        expected_duration_s=manifest.duration_s,
                        duration_tolerance_s=max(1.5, manifest.duration_s * 0.25),
                        timeout_seconds=settings.ffprobe_timeout_seconds,
                    )

                    extract_thumbnail(
                        mp4_path,
                        thumb_path,
                        seek_seconds=min(0.5, manifest.duration_s * 0.25),
                        width=min(640, manifest.width),
                        timeout_seconds=settings.ffmpeg_timeout_seconds,
                    )

                    if not lease.update_progress(99, force=True):
                        raise RenderCancelledError(
                            f"Render job {job.id} cancelled or fenced before upload"
                        )

                    store.put_bytes(output_key, mp4_path.read_bytes(), content_type="video/mp4")
                    store.put_bytes(
                        thumbnail_key, thumb_path.read_bytes(), content_type="image/webp"
                    )
            finally:
                lease.stop_background_renewer()

            completed_at = datetime.now(UTC)
            expires_at = completed_at + timedelta(hours=settings.output_ttl_hours)

            def _complete_job(conn: DbConnection) -> str:
                committed = mark_job_succeeded(
                    conn,
                    job_id=job.id,
                    attempt_count=job.attempt_count,
                    output_object_key=output_key,
                    thumbnail_key=thumbnail_key,
                    expires_at=expires_at,
                )
                if not committed:
                    if is_job_cancelled(conn, job_id=job.id):
                        store.delete(output_key)
                        store.delete(thumbnail_key)
                        return "cancelled"
                    logger.warning(
                        "Lost lease/fencing token for job %s (attempt=%d) before commit",
                        job.id,
                        job.attempt_count,
                    )
                    return "lost_lease"
                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_succeeded",
                    metadata={
                        "job_id": str(job.id),
                        "project_id": str(job.project_id),
                        "width": probe.width,
                        "height": probe.height,
                        "duration_s": probe.duration_s,
                        "fps": manifest.fps,
                        "is_preview": manifest.is_preview,
                        "render_started_at": started_at.isoformat(),
                        "render_completed_at": completed_at.isoformat(),
                    },
                )
                return "succeeded"

            return lease.run(_complete_job)

        except RenderCancelledError:
            lease.stop_background_renewer()

            def _handle_cancel(conn: DbConnection) -> str:
                if not is_job_cancelled(conn, job_id=job.id):
                    logger.warning(
                        "Lost lease/fencing token for job %s (attempt=%d); aborting cleanly",
                        job.id,
                        job.attempt_count,
                    )
                    return "lost_lease"
                logger.info("Render job %s cancelled; aborting cleanly", job.id)
                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_cancelled",
                    metadata={"job_id": str(job.id), "project_id": str(job.project_id)},
                )
                return "cancelled"

            return lease.run(_handle_cancel)

        except (ValidationError, ValueError) as exc:
            lease.stop_background_renewer()
            msg = _sanitize_error_message(exc)
            logger.warning("Render job %s failed validation fatally: %s", job.id, msg)

            def _handle_fatal(conn: DbConnection) -> str:
                final_status = mark_job_failed(
                    conn,
                    job_id=job.id,
                    attempt_count=job.attempt_count,
                    error_message=msg,
                    max_attempts=settings.max_attempts,
                    fatal=True,
                )
                if final_status is None:
                    logger.warning(
                        "Fencing token lost for job %s (attempt=%d) during fatal failure",
                        job.id,
                        job.attempt_count,
                    )
                    return "lost_lease"
                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_failed",
                    metadata={"job_id": str(job.id), "error": msg, "fatal": True},
                )
                return final_status

            return lease.run(_handle_fatal)

        except Exception as exc:
            lease.stop_background_renewer()
            msg = _sanitize_error_message(exc)
            logger.exception("Render job %s failed: %s", job.id, msg)

            def _handle_error(conn: DbConnection) -> str:
                final_status = mark_job_failed(
                    conn,
                    job_id=job.id,
                    attempt_count=job.attempt_count,
                    error_message=msg,
                    max_attempts=settings.max_attempts,
                    fatal=False,
                )
                if final_status is None:
                    logger.warning(
                        "Fencing token lost for job %s (attempt=%d) during error handling",
                        job.id,
                        job.attempt_count,
                    )
                    return "lost_lease"
                record_audit_event(
                    conn,
                    user_id=job.owner_user_id,
                    project_id=job.project_id,
                    event_type="render_failed",
                    metadata={
                        "job_id": str(job.id),
                        "attempt": job.attempt_count,
                        "status_after": final_status,
                        "error": msg,
                    },
                )
                return final_status

            return lease.run(_handle_error)


def _claim_ready_finalize_job(
    conn: DbConnection,
    *,
    lease_seconds: int,
) -> ClaimedJob | None:
    row = conn.execute(CLAIM_READY_FINALIZE_SQL, {"lease_seconds": lease_seconds}).fetchone()
    conn.commit()
    if row is None:
        return None
    return ClaimedJob(
        id=_parse_uuid(row[0]),
        project_id=_parse_uuid(row[1]),
        owner_user_id=_parse_uuid(row[2]),
        spec_json=_parse_spec_json(row[3]),
        attempt_count=int(row[4]),
    )


def run_once(
    settings: WorkerSettings | None = None,
    *,
    conn_factory: Callable[[], AbstractContextManager[DbConnection]] | None = None,
    store: ObjectStore | None = None,
    render_fn: RenderCallable = render_manifest_to_mp4,
    browser_host: WarmPlaywrightHost | None = None,
    frame_count_fn: FrameCountCallable = probe_film_frame_count,
) -> bool:
    """Reap expired leases, claim a ready chunk or render job (or finalize), and process it."""
    cfg = settings or WorkerSettings()
    cf = conn_factory or default_conn_factory(cfg)
    obj_store = store or build_object_store(cfg)

    with cf() as conn:
        reap_exhausted_jobs(conn, max_attempts=cfg.max_attempts)
        reap_exhausted_chunks(conn, max_attempts=cfg.max_attempts)

        chunk = claim_next_chunk(
            conn,
            max_attempts=cfg.max_attempts,
            lease_seconds=cfg.lease_seconds,
        )
        if chunk is None:
            finalize_job = _claim_ready_finalize_job(conn, lease_seconds=cfg.lease_seconds)
        else:
            finalize_job = None

        if chunk is None and finalize_job is None:
            job = claim_next_job(
                conn,
                max_attempts=cfg.max_attempts,
                lease_seconds=cfg.lease_seconds,
            )
        else:
            job = None

    if chunk is not None:
        process_claimed_chunk(
            cf,
            chunk,
            cfg,
            obj_store,
            render_fn=render_fn,
            browser_host=browser_host,
        )
        return True

    if finalize_job is not None:
        _finalize_chunked_job(cf, finalize_job, cfg, obj_store)
        return True

    if job is None:
        return False

    process_claimed_job(
        cf,
        job,
        cfg,
        obj_store,
        render_fn=render_fn,
        browser_host=browser_host,
        frame_count_fn=frame_count_fn,
    )
    return True


def _worker_thread_loop(
    cfg: WorkerSettings,
    stop_event: threading.Event,
    worker_idx: int,
) -> None:
    logger.debug("Worker thread %d started", worker_idx)
    accepts_browser_host = "browser_host" in inspect.signature(run_once).parameters
    with WarmPlaywrightHost(cfg) as browser_host:
        while not stop_event.is_set():
            try:
                did_work = (
                    run_once(cfg, browser_host=browser_host)
                    if accepts_browser_host
                    else run_once(cfg)
                )
            except Exception:
                logger.exception("Worker loop iteration failed (worker=%d)", worker_idx)
                did_work = False

            if not did_work and not stop_event.is_set():
                stop_event.wait(timeout=cfg.poll_interval_seconds)


def run_worker_loop(settings: WorkerSettings | None = None, *, once: bool = False) -> None:
    """Poll PostgreSQL `render_jobs` / `render_job_chunks` lease queue and execute renders."""
    cfg = settings or WorkerSettings()
    logger.info(
        "Worker initialized (render_base_url=%s, concurrency=%d, once=%s)",
        cfg.render_base_url,
        cfg.worker_concurrency,
        once,
    )
    if once:
        run_once(cfg)
        return

    stop_event = threading.Event()

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if cfg.worker_concurrency <= 1:
        _worker_thread_loop(cfg, stop_event, 0)
        return

    threads = [
        threading.Thread(
            target=_worker_thread_loop,
            args=(cfg, stop_event, idx),
            name=f"trip-worker-{idx}",
            daemon=False,
        )
        for idx in range(cfg.worker_concurrency)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
