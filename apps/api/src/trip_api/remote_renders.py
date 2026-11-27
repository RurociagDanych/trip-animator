from __future__ import annotations

import json
import logging
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, sessionmaker
from trip_object_store import ObjectStore, S3ObjectStore
from trip_telemetry import (
    DEFAULT_ACTIVITY_TYPE,
    PhotoRecord,
    StageRecord,
    build_render_bundle,
    normalize_activity_type,
)

from trip_api.config import Settings
from trip_api.credits import refund_render_credits_idempotent
from trip_api.db import Project, RenderJob
from trip_api.render_backends import (
    AwsSfnLambdaBackend,
    RemoteSubmitRequest,
    RenderBackend,
    sanitize_remote_error,
)
from trip_api.services import get_project_photos, get_project_stages, write_audit_event

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_DISPATCH_LEASE_SECONDS = 120


class RenderCallbackPayload(BaseModel):
    job_id: uuid.UUID
    event: Literal["running", "succeeded", "failed"]
    progress_pct: int | None = Field(default=None, ge=0, le=100)
    chunk_done: int | None = Field(default=None, ge=1, le=10_000)
    total_chunks: int | None = Field(default=None, ge=1, le=10_000)
    output_key: str | None = Field(default=None, max_length=512)
    thumbnail_key: str | None = Field(default=None, max_length=512)
    error_message: str | None = Field(default=None, max_length=1000)
    probe: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RemoteRenderCycleReport:
    dispatched: int = 0
    dispatch_failed: int = 0
    reconciled_running: int = 0
    reconciled_succeeded: int = 0
    reconciled_failed: int = 0
    reconciled_cancelled: int = 0


def _notify_job_event(
    db: Session,
    *,
    job_id: uuid.UUID,
    status: str,
    progress_pct: int,
    database_url: str,
) -> None:
    if not database_url.startswith("postgresql"):
        return
    try:
        payload = json.dumps(
            {"job_id": str(job_id), "status": status, "progress_pct": progress_pct}
        )
        db.execute(text("SELECT pg_notify('render_job_events', :p)"), {"p": payload})
    except Exception as exc:
        logger.debug("pg_notify skipped for job %s: %s", job_id, exc)


def _select_job_for_update(db: Session, job_id: uuid.UUID, database_url: str) -> RenderJob | None:
    stmt = select(RenderJob).where(RenderJob.id == job_id)
    if database_url.startswith("postgresql"):
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def _validate_staging_key(job_id: uuid.UUID, key: str | None) -> str | None:
    if key is None:
        return None
    cleaned = key.strip()
    if not cleaned:
        return None
    expected_prefix = f"staging/{job_id}/"
    if not cleaned.startswith(expected_prefix) or ".." in Path(cleaned).parts:
        raise ValueError(f"Invalid staging artifact key for job {job_id}: {key!r}")
    return cleaned


def _resolve_callback_url(settings: Settings) -> str | None:
    base = settings.render_callback_base_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/api/v1/internal/render-callbacks"


def _can_use_direct_s3_copy(
    store: ObjectStore,
    backend: RenderBackend,
    settings: Settings,
) -> bool:
    if not isinstance(store, S3ObjectStore) or not isinstance(backend, AwsSfnLambdaBackend):
        return False
    return (
        settings.object_store_backend == "s3"
        and store.bucket == backend.staging_bucket
        and settings.s3_region.strip() == backend.region
    )


def _ingest_remote_outputs(
    session_factory: sessionmaker[Session],
    store: ObjectStore,
    settings: Settings,
    backend: RenderBackend,
    *,
    job_id: uuid.UUID,
    output_key: str | None,
    thumbnail_key: str | None,
    probe: dict[str, Any] | None,
) -> str:
    safe_output_key = _validate_staging_key(job_id, output_key)
    safe_thumb_key = _validate_staging_key(job_id, thumbnail_key)
    if safe_output_key is None:
        raise ValueError(f"Missing output_key for completed remote job {job_id}")

    with session_factory() as db:
        job = db.scalar(select(RenderJob).where(RenderJob.id == job_id))
        if job is None:
            return "missing"
        if job.status in _TERMINAL_STATES:
            if job.status == "cancelled":
                backend.cleanup_staging(job_id)
            return job.status
        execution_ref = job.external_execution_id or ""

    primary_output_key = f"outputs/{job_id}.mp4"
    primary_thumb_key: str | None = None

    if _can_use_direct_s3_copy(store, backend, settings):
        store.copy(safe_output_key, primary_output_key, content_type="video/mp4")
        if safe_thumb_key is not None:
            try:
                store.copy(safe_thumb_key, f"outputs/{job_id}.webp", content_type="image/webp")
                primary_thumb_key = f"outputs/{job_id}.webp"
            except FileNotFoundError:
                primary_thumb_key = None
    else:
        with tempfile.TemporaryDirectory(prefix="trip_remote_ingest_") as tmpdir:
            tmp_root = Path(tmpdir)
            local_mp4 = tmp_root / f"{job_id}.mp4"
            backend.download_output(execution_ref, safe_output_key, local_mp4)
            store.put_file(primary_output_key, local_mp4, content_type="video/mp4")

            if safe_thumb_key is not None:
                local_thumb = tmp_root / f"{job_id}.webp"
                try:
                    backend.download_output(execution_ref, safe_thumb_key, local_thumb)
                    primary_thumb_key = f"outputs/{job_id}.webp"
                    store.put_bytes(
                        primary_thumb_key,
                        local_thumb.read_bytes(),
                        content_type="image/webp",
                    )
                except FileNotFoundError:
                    primary_thumb_key = None

    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.render_retention_days)

    with session_factory() as db:
        job = _select_job_for_update(db, job_id, settings.database_url)
        if job is None:
            store.delete(primary_output_key)
            if primary_thumb_key is not None:
                store.delete(primary_thumb_key)
            backend.cleanup_staging(job_id)
            return "missing"
        if job.status in _TERMINAL_STATES:
            if job.status != "succeeded":
                store.delete(primary_output_key)
                if primary_thumb_key is not None:
                    store.delete(primary_thumb_key)
                backend.cleanup_staging(job_id)
            return job.status

        job.status = "succeeded"
        job.progress_pct = 100
        job.lease_expires_at = None
        job.output_object_key = primary_output_key
        job.thumbnail_key = primary_thumb_key
        job.expires_at = expires_at
        job.error_message = None
        job.updated_at = now

        meta: dict[str, Any] = {
            "job_id": str(job.id),
            "project_id": str(job.project_id),
            "render_backend": job.render_backend,
        }
        if probe:
            meta["probe"] = probe
        write_audit_event(
            db,
            user_id=job.owner_user_id,
            project_id=job.project_id,
            event_type="render_succeeded",
            metadata=meta,
        )
        _notify_job_event(
            db,
            job_id=job.id,
            status="succeeded",
            progress_pct=100,
            database_url=settings.database_url,
        )
        db.commit()

    backend.cleanup_staging(job_id)
    return "succeeded"


def _mark_remote_failed(
    session_factory: sessionmaker[Session],
    settings: Settings,
    backend: RenderBackend,
    *,
    job_id: uuid.UUID,
    error_message: str,
) -> str:
    now = datetime.now(UTC)
    sanitized = sanitize_remote_error(error_message)
    with session_factory() as db:
        job = _select_job_for_update(db, job_id, settings.database_url)
        if job is None:
            return "missing"
        if job.status in _TERMINAL_STATES:
            return job.status
        job.status = "failed"
        job.lease_expires_at = None
        job.error_message = sanitized
        job.updated_at = now
        refund_render_credits_idempotent(
            db,
            job=job,
            note="Refund for failed remote render",
        )
        write_audit_event(
            db,
            user_id=job.owner_user_id,
            project_id=job.project_id,
            event_type="render_failed",
            metadata={
                "job_id": str(job.id),
                "render_backend": job.render_backend,
                "error": sanitized,
            },
        )
        _notify_job_event(
            db,
            job_id=job.id,
            status="failed",
            progress_pct=job.progress_pct,
            database_url=settings.database_url,
        )
        db.commit()
    backend.cleanup_staging(job_id)
    return "failed"


def _mark_remote_cancelled(
    session_factory: sessionmaker[Session],
    settings: Settings,
    backend: RenderBackend,
    *,
    job_id: uuid.UUID,
) -> str:
    now = datetime.now(UTC)
    with session_factory() as db:
        job = _select_job_for_update(db, job_id, settings.database_url)
        if job is None:
            return "missing"
        if job.status in _TERMINAL_STATES:
            return job.status
        job.status = "cancelled"
        job.lease_expires_at = None
        job.updated_at = now
        _notify_job_event(
            db,
            job_id=job.id,
            status="cancelled",
            progress_pct=job.progress_pct,
            database_url=settings.database_url,
        )
        db.commit()
    backend.cleanup_staging(job_id)
    return "cancelled"


def _update_remote_progress(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    job_id: uuid.UUID,
    progress_pct: int | None = None,
    chunk_done: int | None = None,
    total_chunks: int | None = None,
) -> str:
    now = datetime.now(UTC)
    with session_factory() as db:
        job = _select_job_for_update(db, job_id, settings.database_url)
        if job is None:
            return "missing"
        if job.status in _TERMINAL_STATES:
            return job.status
        if progress_pct is not None:
            target = max(0, min(99, int(progress_pct)))
            new_pct = max(job.progress_pct, target)
        elif chunk_done is not None and total_chunks is not None and total_chunks > 0:
            step = max(1, round((chunk_done / total_chunks) * 90))
            new_pct = min(95, job.progress_pct + step)
        else:
            new_pct = job.progress_pct
        changed = job.status != "running" or new_pct != job.progress_pct
        job.status = "running"
        job.progress_pct = new_pct
        job.updated_at = now
        if changed:
            _notify_job_event(
                db,
                job_id=job.id,
                status="running",
                progress_pct=new_pct,
                database_url=settings.database_url,
            )
        db.commit()
        return "running"


def handle_render_callback(
    session_factory: sessionmaker[Session],
    store: ObjectStore,
    settings: Settings,
    backend: RenderBackend,
    payload: RenderCallbackPayload,
) -> str:
    if payload.event == "running":
        return _update_remote_progress(
            session_factory,
            settings,
            job_id=payload.job_id,
            progress_pct=payload.progress_pct,
            chunk_done=payload.chunk_done,
            total_chunks=payload.total_chunks,
        )
    if payload.event == "succeeded":
        return _ingest_remote_outputs(
            session_factory,
            store,
            settings,
            backend,
            job_id=payload.job_id,
            output_key=payload.output_key,
            thumbnail_key=payload.thumbnail_key,
            probe=payload.probe,
        )
    return _mark_remote_failed(
        session_factory,
        settings,
        backend,
        job_id=payload.job_id,
        error_message=payload.error_message or "Remote render reported failure",
    )


@dataclass(frozen=True, slots=True)
class _ClaimedDispatchJob:
    job_id: uuid.UUID
    project_id: uuid.UUID
    owner_user_id: uuid.UUID
    project_title: str
    spec_json: dict[str, object]
    attempt_count: int
    stages: list[StageRecord]
    photos: list[PhotoRecord]


def _reap_exhausted_remote_dispatches(
    session_factory: sessionmaker[Session],
    settings: Settings,
    backend_name: str,
) -> int:
    now = datetime.now(UTC)
    reaped = 0
    with session_factory() as db:
        stmt = select(RenderJob).where(
            RenderJob.render_backend == backend_name,
            RenderJob.status == "queued",
            RenderJob.external_execution_id.is_(None),
            RenderJob.attempt_count >= settings.render_remote_max_attempts,
            or_(
                RenderJob.lease_expires_at.is_(None),
                RenderJob.lease_expires_at < now,
            ),
        )
        if settings.database_url.startswith("postgresql"):
            stmt = stmt.with_for_update(skip_locked=True)
        stranded = list(db.scalars(stmt).all())
        for job in stranded:
            job.status = "failed"
            job.lease_expires_at = None
            job.error_message = (
                job.error_message or "Remote render dispatch exceeded maximum retry attempts"
            )
            job.updated_at = now
            refund_render_credits_idempotent(
                db,
                job=job,
                note="Refund for exhausted remote render dispatch",
            )
            _notify_job_event(
                db,
                job_id=job.id,
                status="failed",
                progress_pct=job.progress_pct,
                database_url=settings.database_url,
            )
            reaped += 1
        if reaped:
            db.commit()
    return reaped


def _claim_undispatched_remote_job(
    session_factory: sessionmaker[Session],
    settings: Settings,
    backend_name: str,
    *,
    exclude_job_ids: set[uuid.UUID] | None = None,
) -> _ClaimedDispatchJob | None:
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=_DISPATCH_LEASE_SECONDS)
    with session_factory() as db:
        stmt = (
            select(RenderJob)
            .where(
                RenderJob.render_backend == backend_name,
                RenderJob.status == "queued",
                RenderJob.external_execution_id.is_(None),
                RenderJob.attempt_count < settings.render_remote_max_attempts,
                or_(
                    RenderJob.lease_expires_at.is_(None),
                    RenderJob.lease_expires_at < now,
                ),
            )
            .order_by(RenderJob.created_at.asc())
            .limit(1)
        )
        if exclude_job_ids:
            stmt = stmt.where(RenderJob.id.not_in(exclude_job_ids))
        if settings.database_url.startswith("postgresql"):
            stmt = stmt.with_for_update(skip_locked=True)
        job = db.scalar(stmt)
        if job is None:
            return None

        job.attempt_count += 1
        job.lease_expires_at = lease_until
        job.updated_at = now
        attempt_count = job.attempt_count

        project = db.scalar(select(Project).where(Project.id == job.project_id))
        project_title = project.title if project is not None else "Trip"
        db_stages = get_project_stages(db, job.project_id)
        db_photos = get_project_photos(db, job.project_id, only_enabled=True)

        stages = [
            StageRecord(
                id=s.id,
                project_id=s.project_id,
                name=s.name,
                stage_order=s.stage_order,
                day_index=s.day_index,
                trim_start_s=s.trim_start_s,
                trim_end_s=s.trim_end_s,
                silver_object_key=s.silver_object_key,
                distance_m=s.distance_m,
                elevation_gain_m=s.elevation_gain_m,
                moving_seconds=s.moving_seconds,
                elapsed_seconds=s.elapsed_seconds,
                has_timestamps=s.has_timestamps,
                activity_type=normalize_activity_type(s.activity_type, DEFAULT_ACTIVITY_TYPE),
            )
            for s in db_stages
        ]
        photos = [
            PhotoRecord(
                id=p.id,
                project_id=p.project_id,
                thumb_object_key=p.thumb_object_key,
                object_key=p.object_key,
                width=p.width,
                height=p.height,
                caption=p.caption,
                taken_at_utc=p.taken_at_utc,
                lat=p.lat,
                lon=p.lon,
                matched_lat=p.matched_lat,
                matched_lon=p.matched_lon,
                matched_progress=p.matched_progress,
                match_method=p.match_method,
            )
            for p in db_photos
        ]
        claimed = _ClaimedDispatchJob(
            job_id=job.id,
            project_id=job.project_id,
            owner_user_id=job.owner_user_id,
            project_title=project_title,
            spec_json=dict(job.spec_json),
            attempt_count=attempt_count,
            stages=stages,
            photos=photos,
        )
        db.commit()
        return claimed


def process_remote_renders_once(
    session_factory: sessionmaker[Session],
    store: ObjectStore,
    settings: Settings,
    backend: RenderBackend,
    *,
    max_dispatch: int = 5,
    max_reconcile: int = 20,
) -> RemoteRenderCycleReport:
    if not backend.is_remote:
        return RemoteRenderCycleReport()

    dispatched = 0
    dispatch_failed = _reap_exhausted_remote_dispatches(
        session_factory,
        settings,
        backend.name,
    )
    reconciled_running = 0
    reconciled_succeeded = 0
    reconciled_failed = 0
    reconciled_cancelled = 0

    callback_url = _resolve_callback_url(settings)
    attempted_this_cycle: set[uuid.UUID] = set()

    for _ in range(max_dispatch):
        claimed = _claim_undispatched_remote_job(
            session_factory,
            settings,
            backend.name,
            exclude_job_ids=attempted_this_cycle,
        )
        if claimed is None:
            break
        attempted_this_cycle.add(claimed.job_id)
        try:
            bundle = build_render_bundle(
                job_id=claimed.job_id,
                project_id=claimed.project_id,
                project_title=claimed.project_title,
                spec_json=claimed.spec_json,
                stages=claimed.stages,
                store=store,
                photos=claimed.photos,
            )
            ref = backend.submit(
                RemoteSubmitRequest(
                    job_id=claimed.job_id,
                    bundle=bundle,
                    callback_url=callback_url,
                    chunk_threshold_frames=settings.render_remote_chunk_threshold_frames,
                    chunk_size_frames=settings.render_remote_chunk_size_frames,
                    preroll_frames=settings.render_remote_preroll_frames,
                )
            )
        except Exception as exc:
            msg = sanitize_remote_error(exc, fallback="Remote render dispatch failed")
            logger.warning("Remote dispatch failed for job %s: %s", claimed.job_id, msg)
            now = datetime.now(UTC)
            exhausted = claimed.attempt_count >= settings.render_remote_max_attempts
            with session_factory() as db:
                job = _select_job_for_update(db, claimed.job_id, settings.database_url)
                if job is not None and job.status not in _TERMINAL_STATES:
                    job.status = "failed" if exhausted else "queued"
                    job.lease_expires_at = None
                    job.error_message = msg
                    job.updated_at = now
                    if exhausted:
                        refund_render_credits_idempotent(
                            db,
                            job=job,
                            note="Refund for failed remote render dispatch",
                        )
                    _notify_job_event(
                        db,
                        job_id=job.id,
                        status=job.status,
                        progress_pct=job.progress_pct,
                        database_url=settings.database_url,
                    )
                    db.commit()
            dispatch_failed += 1
            continue

        now = datetime.now(UTC)
        cancelled_mid_dispatch = False
        with session_factory() as db:
            job = _select_job_for_update(db, claimed.job_id, settings.database_url)
            if job is None or job.status == "cancelled":
                cancelled_mid_dispatch = True
            elif job.status not in _TERMINAL_STATES:
                job.status = "running"
                job.external_execution_id = ref.execution_id
                job.remote_dispatched_at = now
                job.lease_expires_at = None
                job.updated_at = now
                _notify_job_event(
                    db,
                    job_id=job.id,
                    status="running",
                    progress_pct=job.progress_pct,
                    database_url=settings.database_url,
                )
                db.commit()

        if cancelled_mid_dispatch:
            try:
                backend.cancel(ref.execution_id)
            except Exception as exc:
                logger.warning("Failed cancelling remote execution %s: %s", ref.execution_id, exc)
            backend.cleanup_staging(claimed.job_id)
        else:
            dispatched += 1

    with session_factory() as db:
        active_rows = list(
            db.execute(
                select(RenderJob.id, RenderJob.external_execution_id)
                .where(
                    RenderJob.render_backend == backend.name,
                    RenderJob.status == "running",
                    RenderJob.external_execution_id.is_not(None),
                )
                .order_by(RenderJob.updated_at.asc())
                .limit(max_reconcile)
            ).all()
        )

    for active_job_id, execution_id in active_rows:
        if not execution_id:
            continue
        try:
            remote_status = backend.get_status(str(execution_id))
        except Exception as exc:
            logger.warning("Remote status poll failed for job %s: %s", active_job_id, exc)
            continue

        if remote_status.state == "running":
            _update_remote_progress(
                session_factory,
                settings,
                job_id=active_job_id,
                progress_pct=remote_status.progress_pct,
            )
            reconciled_running += 1
        elif remote_status.state == "succeeded":
            try:
                final = _ingest_remote_outputs(
                    session_factory,
                    store,
                    settings,
                    backend,
                    job_id=active_job_id,
                    output_key=remote_status.output_key,
                    thumbnail_key=remote_status.thumbnail_key,
                    probe=remote_status.probe,
                )
                if final == "succeeded":
                    reconciled_succeeded += 1
            except Exception as exc:
                logger.warning("Output ingest failed for job %s: %s", active_job_id, exc)
                _mark_remote_failed(
                    session_factory,
                    settings,
                    backend,
                    job_id=active_job_id,
                    error_message="Remote render output ingest failed",
                )
                reconciled_failed += 1
        elif remote_status.state == "cancelled":
            _mark_remote_cancelled(
                session_factory,
                settings,
                backend,
                job_id=active_job_id,
            )
            reconciled_cancelled += 1
        elif remote_status.state == "failed":
            _mark_remote_failed(
                session_factory,
                settings,
                backend,
                job_id=active_job_id,
                error_message=remote_status.error_message or "Remote render failed",
            )
            reconciled_failed += 1

    return RemoteRenderCycleReport(
        dispatched=dispatched,
        dispatch_failed=dispatch_failed,
        reconciled_running=reconciled_running,
        reconciled_succeeded=reconciled_succeeded,
        reconciled_failed=reconciled_failed,
        reconciled_cancelled=reconciled_cancelled,
    )


def run_remote_render_loop(
    session_factory: sessionmaker[Session],
    store: ObjectStore,
    settings: Settings,
    backend: RenderBackend,
    stop_event: threading.Event,
) -> None:
    interval = max(0.2, settings.render_remote_dispatch_interval_s)
    while not stop_event.is_set():
        try:
            process_remote_renders_once(session_factory, store, settings, backend)
        except Exception:
            logger.exception("Remote render outbox loop iteration failed")
        stop_event.wait(timeout=interval)
