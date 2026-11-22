"""Render job submission (idempotent + content-addressed cache), LISTEN/NOTIFY SSE, downloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from trip_telemetry.schemas import RenderSpecV2

from trip_api.db import ProjectPhoto, RenderJob, Stage
from trip_api.deps import (
    AppSettings,
    CsrfAuth,
    CurrentAuth,
    DbSession,
    StoreDep,
    authenticate_request,
    ensure_utc,
)
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import RenderJobOut, validate_stage_maps_against_project
from trip_api.services import (
    get_owned_project,
    get_project_photos,
    get_project_stages,
    render_job_to_out,
    stream_video_response,
    write_audit_event,
)
from trip_api.storage import ObjectStore

router = APIRouter(tags=["renders"])
logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _to_psycopg_dsn(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix("postgresql+psycopg://")
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url.removeprefix("postgres://")
    return database_url


def _get_owned_job(db: Session, job_id: uuid.UUID, user_id: uuid.UUID) -> RenderJob:
    job = db.scalar(
        select(RenderJob).where(RenderJob.id == job_id, RenderJob.owner_user_id == user_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Render job not found")
    return job


def _compute_idempotency_hash(project_id: uuid.UUID, spec: RenderSpecV2) -> str:
    canonical = json.dumps(
        {"project_id": str(project_id), "spec": spec.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_content_hash(
    spec: RenderSpecV2,
    stages: list[Stage],
    photos: list[ProjectPhoto],
) -> str:
    stage_payload = [
        {
            "id": str(s.id),
            "silver_object_key": s.silver_object_key,
            "etl_version": int(getattr(s, "etl_version", 1) or 1),
            "bronze_sha256": getattr(s, "bronze_sha256", None),
            "trim_start_s": s.trim_start_s,
            "trim_end_s": s.trim_end_s,
            "day_index": s.day_index,
            "stage_order": s.stage_order,
            "activity_type": s.activity_type,
            "name": s.name,
        }
        for s in stages
    ]
    photo_payload = [
        {
            "id": str(p.id),
            "taken_at_utc": ensure_utc(p.taken_at_utc).isoformat() if p.taken_at_utc else None,
            "exif_offset_s": int(getattr(p, "exif_offset_s", 0) or 0),
            "matched_progress": p.matched_progress,
            "match_method": p.match_method,
            "caption": p.caption,
        }
        for p in sorted(photos, key=lambda ph: str(ph.id))
    ]
    canonical = json.dumps(
        {
            "spec": spec.model_dump(mode="json"),
            "stages": stage_payload,
            "photos": photo_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _try_copy_cached_render_artifacts(
    store: ObjectStore,
    cached_job: RenderJob,
    new_job_id: uuid.UUID,
) -> tuple[str, str | None] | None:
    if not cached_job.output_object_key:
        return None
    new_out_key = f"outputs/{new_job_id}.mp4"
    try:
        store.copy(cached_job.output_object_key, new_out_key, content_type="video/mp4")
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Failed copying cached render video %s: %s", cached_job.id, exc)
        return None

    new_thumb_key: str | None = None
    if cached_job.thumbnail_key:
        candidate_thumb_key = f"outputs/{new_job_id}.webp"
        try:
            store.copy(cached_job.thumbnail_key, candidate_thumb_key, content_type="image/webp")
            new_thumb_key = candidate_thumb_key
        except FileNotFoundError:
            new_thumb_key = None
        except Exception as exc:
            logger.warning("Failed copying cached render thumbnail %s: %s", cached_job.id, exc)
            new_thumb_key = None

    return new_out_key, new_thumb_key


def _cinematic_extra_seconds(raw: dict[str, object], photo_count: int) -> float:
    """Upper bound of film time added by cinematic photo stops, assuming one stop per photo."""
    if raw.get("photo_card_style") != "cinematic_stop" or raw.get("show_photos") is False:
        return 0.0
    raw_display = raw.get("photo_display_seconds", 3.0)
    display_s = float(raw_display) if isinstance(raw_display, (int, float)) else 3.0
    return max(0, photo_count) * (1.0 + max(0.0, display_s))


def _sse_deadline_seconds(spec_json: dict[str, object] | None, photo_count: int = 0) -> float:
    raw = spec_json or {}
    raw_dur = raw.get("duration_seconds", 30.0)
    raw_fps = raw.get("fps", 30)
    duration_s = float(raw_dur) if isinstance(raw_dur, (int, float)) else 30.0
    fps = int(raw_fps) if isinstance(raw_fps, (int, float)) else 30
    film_s = duration_s + _cinematic_extra_seconds(raw, photo_count)
    return max(300.0, film_s * (20.0 if fps == 60 else 12.0) + 120.0)


@router.post("/api/v1/projects/{project_id}/renders", response_model=RenderJobOut, status_code=202)
def create_render_job(
    project_id: uuid.UUID,
    spec: RenderSpecV2,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> RenderJobOut:
    project = get_owned_project(db, project_id, auth.user.id)
    check_rate_limit(
        db,
        bucket_key=f"renders:{auth.user.id}",
        limit=settings.rate_limit_render_max,
        window_seconds=settings.rate_limit_render_window_s,
    )

    if not idempotency_key or len(idempotency_key.strip()) < 8 or len(idempotency_key) > 128:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required (8-128 characters)",
        )
    clean_key = idempotency_key.strip()

    stages = get_project_stages(db, project.id)
    if not stages:
        raise HTTPException(status_code=422, detail="Cannot render a project with zero stages")
    validate_stage_maps_against_project(spec, {str(s.id) for s in stages})

    payload_hash = _compute_idempotency_hash(project.id, spec)
    existing = db.scalar(
        select(RenderJob).where(
            RenderJob.owner_user_id == auth.user.id,
            RenderJob.project_id == project.id,
            RenderJob.idempotency_key == clean_key,
        )
    )
    if existing is not None:
        if existing.idempotency_hash != payload_hash:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with a different render specification",
            )
        return render_job_to_out(existing)

    photos = get_project_photos(db, project.id, only_enabled=True)
    content_hash = _compute_content_hash(spec, stages, photos)
    now = datetime.now(UTC)

    cached_hit = db.scalar(
        select(RenderJob)
        .where(
            RenderJob.project_id == project.id,
            RenderJob.content_hash == content_hash,
            RenderJob.status == "succeeded",
            RenderJob.output_object_key.is_not(None),
            (RenderJob.expires_at.is_(None)) | (RenderJob.expires_at > now),
        )
        .order_by(RenderJob.created_at.desc())
        .limit(1)
    )

    new_job_id = uuid.uuid4()
    copied_keys: tuple[str, str | None] | None = None
    if cached_hit is not None:
        copied_keys = _try_copy_cached_render_artifacts(store, cached_hit, new_job_id)

    if copied_keys is not None:
        out_key, thumb_key = copied_keys
        job = RenderJob(
            id=new_job_id,
            project_id=project.id,
            owner_user_id=auth.user.id,
            idempotency_key=clean_key,
            idempotency_hash=payload_hash,
            content_hash=content_hash,
            spec_json=spec.model_dump(mode="json"),
            status="succeeded",
            progress_pct=100,
            attempt_count=0,
            output_object_key=out_key,
            thumbnail_key=thumb_key,
            expires_at=now + timedelta(days=settings.render_retention_days),
            created_at=now,
            updated_at=now,
        )
    else:
        job = RenderJob(
            id=new_job_id,
            project_id=project.id,
            owner_user_id=auth.user.id,
            idempotency_key=clean_key,
            idempotency_hash=payload_hash,
            content_hash=content_hash,
            spec_json=spec.model_dump(mode="json"),
            status="queued",
            progress_pct=0,
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )

    db.add(job)
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="render.submitted",
        metadata={
            "job_id": str(job.id),
            "resolution": spec.resolution,
            "fps": spec.fps,
            "cache_hit": copied_keys is not None,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if copied_keys is not None:
            for k in copied_keys:
                if k:
                    try:
                        store.delete(k)
                    except Exception:
                        logger.warning("Failed to rollback copied cache artifact %s", k)
        existing = db.scalar(
            select(RenderJob).where(
                RenderJob.owner_user_id == auth.user.id,
                RenderJob.project_id == project.id,
                RenderJob.idempotency_key == clean_key,
            )
        )
        if existing is not None:
            if existing.idempotency_hash != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different render specification",
                ) from exc
            return render_job_to_out(existing)
        raise

    db.refresh(job)
    return render_job_to_out(job)


@router.get("/api/v1/projects/{project_id}/renders", response_model=list[RenderJobOut])
def list_project_renders(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
) -> list[RenderJobOut]:
    project = get_owned_project(db, project_id, auth.user.id)
    jobs = list(
        db.scalars(
            select(RenderJob)
            .where(RenderJob.project_id == project.id, RenderJob.owner_user_id == auth.user.id)
            .order_by(RenderJob.created_at.desc())
        ).all()
    )
    return [render_job_to_out(j) for j in jobs]


@router.get("/api/v1/renders/{job_id}", response_model=RenderJobOut)
def get_render_job(
    job_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
) -> RenderJobOut:
    job = _get_owned_job(db, job_id, auth.user.id)
    return render_job_to_out(job)


def _fetch_job_snapshot(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    user_id: uuid.UUID,
) -> RenderJob | None:
    with session_factory() as poll_db:
        return poll_db.scalar(
            select(RenderJob).where(
                RenderJob.id == job_id,
                RenderJob.owner_user_id == user_id,
            )
        )


@router.get("/api/v1/renders/{job_id}/events")
async def stream_render_events(
    job_id: uuid.UUID,
    request: Request,
    settings: AppSettings,
) -> StreamingResponse:
    session_factory = cast(sessionmaker[Session], request.app.state.session_factory)
    # Authenticate and authorize in a short-lived session so no DB connection stays
    # checked out for the lifetime of the SSE StreamingResponse.
    with session_factory() as db:
        auth = authenticate_request(request, db, settings)
        initial_job = _get_owned_job(db, job_id, auth.user.id)
        user_id = auth.user.id
        photo_count = (
            db.scalar(
                select(func.count())
                .select_from(ProjectPhoto)
                .where(
                    ProjectPhoto.project_id == initial_job.project_id,
                    ProjectPhoto.enabled.is_(True),
                )
            )
            or 0
        )
        deadline_s = _sse_deadline_seconds(initial_job.spec_json, photo_count)

    has_test_poll_cap = hasattr(request.app.state, "sse_max_polls")
    use_pg_listen = settings.database_url.startswith("postgresql") and not has_test_poll_cap
    poll_interval = float(getattr(request.app.state, "sse_poll_interval_s", 0.5))
    max_polls: int | None = int(request.app.state.sse_max_polls) if has_test_poll_cap else None

    async def event_stream() -> AsyncIterator[str]:
        last_sig: tuple[str, int, str | None] | None = None
        start_mono = time.monotonic()

        if use_pg_listen:
            dsn = _to_psycopg_dsn(settings.database_url)
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as aconn:
                    await aconn.execute("LISTEN render_job_events")

                    while (time.monotonic() - start_mono) < deadline_s:
                        if await request.is_disconnected():
                            break
                        current = await asyncio.to_thread(
                            _fetch_job_snapshot, session_factory, job_id, user_id
                        )
                        if current is None:
                            yield 'event: error\ndata: {"detail":"Render job not found"}\n\n'
                            return
                        sig = (current.status, current.progress_pct, current.error_message)
                        if sig != last_sig:
                            last_sig = sig
                            payload = render_job_to_out(current).model_dump(mode="json")
                            yield f"event: status\ndata: {json.dumps(payload)}\n\n"
                        if current.status in _TERMINAL_STATES:
                            return

                        received_for_job = False
                        async for notify in aconn.notifies(timeout=15.0):
                            if await request.is_disconnected():
                                return
                            try:
                                msg = json.loads(notify.payload)
                                if isinstance(msg, dict) and msg.get("job_id") == str(job_id):
                                    received_for_job = True
                                    break
                            except Exception:
                                received_for_job = True
                                break
                        if not received_for_job:
                            yield ": keepalive\n\n"
                    return
            except Exception as exc:
                logger.warning("PG LISTEN fallback to adaptive polling for %s: %s", job_id, exc)

        polls_done = 0
        last_keepalive = time.monotonic()
        while (time.monotonic() - start_mono) < deadline_s:
            if max_polls is not None and polls_done >= max_polls:
                break
            polls_done += 1
            if await request.is_disconnected():
                break
            current = await asyncio.to_thread(_fetch_job_snapshot, session_factory, job_id, user_id)
            if current is None:
                yield 'event: error\ndata: {"detail":"Render job not found"}\n\n'
                break
            sig = (current.status, current.progress_pct, current.error_message)
            if sig != last_sig:
                last_sig = sig
                payload = render_job_to_out(current).model_dump(mode="json")
                yield f"event: status\ndata: {json.dumps(payload)}\n\n"
                last_keepalive = time.monotonic()
            elif (time.monotonic() - last_keepalive) >= 15.0:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
            if current.status in _TERMINAL_STATES:
                break
            await asyncio.sleep(poll_interval)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/v1/renders/{job_id}/cancel", response_model=RenderJobOut)
def cancel_render_job(
    job_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> RenderJobOut:
    job = _get_owned_job(db, job_id, auth.user.id)
    if job.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel render job in terminal state {job.status!r}",
        )
    job.status = "cancelled"
    job.updated_at = datetime.now(UTC)
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=job.project_id,
        event_type="render.cancelled",
        metadata={"job_id": str(job.id)},
    )
    if settings.database_url.startswith("postgresql"):
        try:
            payload = json.dumps({"job_id": str(job.id), "status": "cancelled"})
            db.execute(text("SELECT pg_notify('render_job_events', :p)"), {"p": payload})
        except Exception as exc:
            logger.debug("pg_notify on cancel skipped: %s", exc)
    db.commit()
    db.refresh(job)
    return render_job_to_out(job)


@router.get("/api/v1/renders/{job_id}/download")
def download_render_output(
    job_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
) -> Response:
    job = _get_owned_job(db, job_id, auth.user.id)
    if job.status != "succeeded" or not job.output_object_key:
        raise HTTPException(status_code=409, detail="Render output is not available")
    return stream_video_response(
        request=request,
        store=store,
        object_key=job.output_object_key,
        missing_detail="Render output artifact missing",
        content_disposition=f'attachment; filename="trip-render-{job.id}.mp4"',
    )


@router.get("/api/v1/renders/{job_id}/thumbnail")
def download_render_thumbnail(
    job_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
) -> Response:
    job = _get_owned_job(db, job_id, auth.user.id)
    if not job.thumbnail_key:
        raise HTTPException(status_code=404, detail="Render thumbnail is not available")
    try:
        data = store.get_bytes(job.thumbnail_key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Render thumbnail artifact missing") from exc
    return Response(content=data, media_type="image/webp")
