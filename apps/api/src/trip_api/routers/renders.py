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

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from trip_object_store import ObjectStore
from trip_telemetry import calculate_render_credit_quote
from trip_telemetry.schemas import RenderSpecV2

from trip_api.credits import (
    debit_render_credits,
    get_user_credit_balance,
    lock_user_for_credit_update,
    refund_render_credits_idempotent,
)
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
from trip_api.marked_trails import fetch_marked_trails_pin, spec_json_with_pin
from trip_api.rate_limit import check_rate_limit
from trip_api.remote_renders import RenderCallbackPayload, handle_render_callback
from trip_api.render_backends import (
    RenderBackend,
    build_render_backend,
    verify_render_callback_signature,
)
from trip_api.render_events import RenderEventHub
from trip_api.schemas import RenderJobOut, RenderQuoteOut, validate_stage_maps_against_project
from trip_api.services import (
    get_owned_project,
    get_project_photos,
    get_project_stages,
    render_job_to_out,
    stream_video_response,
    write_audit_event,
)

router = APIRouter(tags=["renders"])
logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


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
    if raw.get("photo_card_style") != "cinematic_stop" or raw.get("show_photos") is False:
        return 0.0
    raw_display = raw.get("photo_display_seconds", 3.0)
    display_s = float(raw_display) if isinstance(raw_display, (int, float)) else 3.0
    return max(0, photo_count) * (1.0 + max(0.0, display_s))


def _count_matched_photos(photos: list[ProjectPhoto]) -> int:
    return sum(1 for p in photos if p.match_method != "unmatched")


def _enforce_server_render_caps(spec: RenderSpecV2, settings: AppSettings) -> None:
    if spec.duration_seconds > settings.server_render_max_duration_s:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Server render duration cannot exceed {settings.server_render_max_duration_s:g}s "
                "(use Local Export for longer films)"
            ),
        )
    max_fps = (
        settings.server_render_max_fps_remote
        if settings.render_backend == "aws_sfn_lambda"
        else settings.server_render_max_fps_local
    )
    if spec.fps > max_fps:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Server render FPS cannot exceed {max_fps} for the "
                f"{settings.render_backend} backend"
            ),
        )


@router.post("/api/v1/projects/{project_id}/renders/quote", response_model=RenderQuoteOut)
def quote_render_job(
    project_id: uuid.UUID,
    spec: RenderSpecV2,
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
) -> RenderQuoteOut:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    if not stages:
        raise HTTPException(status_code=422, detail="Cannot quote a project with zero stages")
    validate_stage_maps_against_project(spec, {str(s.id) for s in stages})
    _enforce_server_render_caps(spec, settings)

    photos = get_project_photos(db, project.id, only_enabled=True)
    content_hash = _compute_content_hash(spec, stages, photos)
    now = datetime.now(UTC)
    cached_hit = db.scalar(
        select(RenderJob.id)
        .where(
            RenderJob.project_id == project.id,
            RenderJob.content_hash == content_hash,
            RenderJob.status == "succeeded",
            RenderJob.output_object_key.is_not(None),
            (RenderJob.expires_at.is_(None)) | (RenderJob.expires_at > now),
        )
        .limit(1)
    )
    quote = calculate_render_credit_quote(
        spec,
        matched_photo_count=_count_matched_photos(photos),
        cached=cached_hit is not None,
    )
    balance = get_user_credit_balance(db, auth.user.id)
    return RenderQuoteOut(
        credits=quote.credits,
        width=quote.width,
        height=quote.height,
        fps=quote.fps,
        film_seconds=quote.film_seconds,
        cinematic_photo_stop_seconds=quote.cinematic_photo_stop_seconds,
        terrain_3d=quote.terrain_3d,
        terrain_multiplier=quote.terrain_multiplier,
        cached=quote.cached,
        balance=balance,
        sufficient=balance >= quote.credits,
    )


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

    _enforce_server_render_caps(spec, settings)

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
            credits_charged=0,
            render_backend=settings.render_backend,
            output_object_key=out_key,
            thumbnail_key=thumb_key,
            expires_at=now + timedelta(days=settings.render_retention_days),
            created_at=now,
            updated_at=now,
        )
        debit_cost = 0
    else:
        quote = calculate_render_credit_quote(
            spec,
            matched_photo_count=_count_matched_photos(photos),
            cached=False,
        )
        # Network read before any row lock is taken (see AGENTS.md code invariants).
        pin = fetch_marked_trails_pin(settings) if spec.marked_trail_types else None
        lock_user_for_credit_update(db, auth.user.id)
        existing_after_lock = db.scalar(
            select(RenderJob).where(
                RenderJob.owner_user_id == auth.user.id,
                RenderJob.project_id == project.id,
                RenderJob.idempotency_key == clean_key,
            )
        )
        if existing_after_lock is not None:
            if existing_after_lock.idempotency_hash != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different render specification",
                )
            return render_job_to_out(existing_after_lock)
        active_renders = int(
            db.scalar(
                select(func.count())
                .select_from(RenderJob)
                .where(
                    RenderJob.owner_user_id == auth.user.id,
                    RenderJob.status.in_(("queued", "running")),
                )
            )
            or 0
        )
        if active_renders >= settings.max_active_renders_per_user:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Maximum of {settings.max_active_renders_per_user} active server render(s) "
                    "allowed at a time. Wait for your current render to finish or cancel it."
                ),
            )
        balance = get_user_credit_balance(db, auth.user.id)
        if balance < quote.credits:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Insufficient Render Credits: this export requires {quote.credits} credits, "
                    f"but your balance is {balance}. Redeem a voucher or use free Local Export."
                ),
            )
        job = RenderJob(
            id=new_job_id,
            project_id=project.id,
            owner_user_id=auth.user.id,
            idempotency_key=clean_key,
            idempotency_hash=payload_hash,
            content_hash=content_hash,
            spec_json=spec_json_with_pin(spec.model_dump(mode="json"), pin),
            status="queued",
            progress_pct=0,
            attempt_count=0,
            credits_charged=quote.credits,
            render_backend=settings.render_backend,
            created_at=now,
            updated_at=now,
        )
        debit_cost = quote.credits

    try:
        db.add(job)
        if debit_cost > 0:
            debit_render_credits(
                db,
                user_id=auth.user.id,
                job_id=job.id,
                cost=debit_cost,
                resolution=spec.resolution,
                fps=spec.fps,
                already_locked=True,
                render_job=job,
            )
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
                "credits_charged": job.credits_charged,
            },
        )
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

        hub = cast(RenderEventHub | None, getattr(request.app.state, "render_event_hub", None))
        if use_pg_listen and hub is not None and hub.is_postgres:
            try:
                async with hub.subscribe(job_id) as queue:
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

                        try:
                            await asyncio.wait_for(queue.get(), timeout=15.0)
                        except TimeoutError:
                            if await request.is_disconnected():
                                return
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
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> RenderJobOut:
    stmt = select(RenderJob).where(RenderJob.id == job_id, RenderJob.owner_user_id == auth.user.id)
    if settings.database_url.startswith("postgresql"):
        stmt = stmt.with_for_update()
    job = db.scalar(stmt)
    if job is None:
        raise HTTPException(status_code=404, detail="Render job not found")
    if job.status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel render job in terminal state {job.status!r}",
        )
    external_exec_id = job.external_execution_id
    job_backend_name = job.render_backend
    was_queued = job.status == "queued"
    was_dispatched = job.remote_dispatched_at is not None
    job.status = "cancelled"
    job.updated_at = datetime.now(UTC)
    eligible_for_refund = (
        (not was_dispatched) if job_backend_name == "aws_sfn_lambda" else was_queued
    )
    refunded = False
    if eligible_for_refund:
        refunded = refund_render_credits_idempotent(
            db,
            job=job,
            note="Refund for cancelled render",
        )
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=job.project_id,
        event_type="render.cancelled",
        metadata={"job_id": str(job.id), "credits_refunded": refunded},
    )
    if settings.database_url.startswith("postgresql"):
        try:
            payload = json.dumps({"job_id": str(job.id), "status": "cancelled"})
            db.execute(text("SELECT pg_notify('render_job_events', :p)"), {"p": payload})
        except Exception as exc:
            logger.debug("pg_notify on cancel skipped: %s", exc)
    db.commit()
    db.refresh(job)
    if external_exec_id:
        backend = cast(
            RenderBackend,
            getattr(request.app.state, "render_backend", None) or build_render_backend(settings),
        )
        if backend.is_remote and backend.name == job_backend_name:
            try:
                backend.cancel(external_exec_id)
            except Exception as exc:
                logger.warning(
                    "Best-effort remote cancel failed for job %s (%s): %s",
                    job.id,
                    external_exec_id,
                    exc,
                )
    return render_job_to_out(job)


@router.post("/api/v1/internal/render-callbacks")
async def receive_render_callback(
    request: Request,
    settings: AppSettings,
    store: StoreDep,
    x_trip_timestamp: Annotated[str | None, Header(alias="X-Trip-Timestamp")] = None,
    x_trip_signature: Annotated[str | None, Header(alias="X-Trip-Signature")] = None,
) -> dict[str, str]:
    secret = settings.render_callback_secret.strip()
    if not secret:
        raise HTTPException(status_code=404, detail="Render callbacks are disabled")
    raw_body = await request.body()
    if not verify_render_callback_signature(secret, x_trip_timestamp, x_trip_signature, raw_body):
        raise HTTPException(status_code=401, detail="Invalid render callback signature")
    try:
        payload = RenderCallbackPayload.model_validate_json(raw_body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid render callback payload") from exc

    session_factory = cast(sessionmaker[Session], request.app.state.session_factory)
    backend = cast(
        RenderBackend,
        getattr(request.app.state, "render_backend", None) or build_render_backend(settings),
    )
    try:
        status_after = await asyncio.to_thread(
            handle_render_callback,
            session_factory,
            store,
            settings,
            backend,
            payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": status_after}


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
