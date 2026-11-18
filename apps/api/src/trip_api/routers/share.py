"""Public unauthenticated read-only share routes with strict privacy redaction."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from trip_telemetry import LodLevel

from trip_api.config import Settings
from trip_api.db import Project, ProjectPhoto, RenderJob, Stage
from trip_api.deps import AppSettings, DbSession, StoreDep, get_client_ip
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    ProjectHighlightsOut,
    PublicProjectOut,
    TelemetryChartOut,
    TelemetryGeometryOut,
)
from trip_api.services import (
    build_project_geometry_collection,
    build_project_highlights,
    build_project_telemetry_chart,
    build_stage_geometry_feature,
    get_project_photos,
    get_project_stages,
    public_project_to_out,
    stream_video_response,
)

router = APIRouter(prefix="/api/v1/share/{slug}", tags=["share"])


def _rate_limit_share(request: Request, db: Session, settings: Settings) -> None:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"share:{client_ip}",
        limit=settings.rate_limit_share_max,
        window_seconds=settings.rate_limit_share_window_s,
    )


def _get_shared_project(db: Session, slug: str) -> Project:
    clean_slug = slug.strip()
    if not clean_slug or len(clean_slug) > 64:
        raise HTTPException(status_code=404, detail="Shared trip not found")
    project = db.scalar(
        select(Project).where(
            Project.share_slug == clean_slug,
            Project.share_enabled.is_(True),
        )
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Shared trip not found")
    return project


def _select_shared_stages(
    db: Session,
    project_id: uuid.UUID,
    stage_id: uuid.UUID | None,
) -> list[Stage]:
    stages = get_project_stages(db, project_id)
    if stage_id is not None:
        filtered = [s for s in stages if s.id == stage_id]
        if not filtered:
            raise HTTPException(status_code=404, detail="Stage not found")
        return filtered
    return stages


@router.get("", response_model=PublicProjectOut)
def get_public_shared_project(
    slug: str,
    request: Request,
    db: DbSession,
    settings: AppSettings,
) -> PublicProjectOut:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    stages = get_project_stages(db, project.id)
    photos = get_project_photos(db, project.id, only_enabled=True)
    latest_render_id = db.scalar(
        select(RenderJob.id)
        .where(
            RenderJob.project_id == project.id,
            RenderJob.status == "succeeded",
            RenderJob.output_object_key.is_not(None),
        )
        .order_by(RenderJob.created_at.desc())
        .limit(1)
    )
    return public_project_to_out(
        project,
        stages,
        photos=photos,
        latest_render_id=latest_render_id,
    )


@router.get("/geometry", response_model=TelemetryGeometryOut)
def get_public_shared_geometry(
    slug: str,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    stage_id: uuid.UUID | None = None,
    lod: Annotated[LodLevel, Query()] = "medium",
) -> TelemetryGeometryOut:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    stages = _select_shared_stages(db, project.id, stage_id)
    try:
        return build_project_geometry_collection(store, project.id, stages, lod)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/stages/{stage_id}/geometry")
def get_public_shared_stage_geometry(
    slug: str,
    stage_id: uuid.UUID,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    lod: Annotated[LodLevel, Query()] = "medium",
) -> dict[str, object]:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    stage = db.scalar(select(Stage).where(Stage.id == stage_id, Stage.project_id == project.id))
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    try:
        return build_stage_geometry_feature(store, stage, lod)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/chart", response_model=TelemetryChartOut)
def get_public_shared_chart(
    slug: str,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    stage_id: uuid.UUID | None = None,
    max_points: Annotated[int, Query(ge=2, le=5000)] = 1200,
) -> TelemetryChartOut:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    stages = _select_shared_stages(db, project.id, stage_id)
    try:
        return build_project_telemetry_chart(store, project.id, stages, max_points)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/analytics/highlights", response_model=ProjectHighlightsOut)
def get_public_shared_highlights(
    slug: str,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
) -> ProjectHighlightsOut:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    stages = get_project_stages(db, project.id)
    return build_project_highlights(store, project.id, stages)


@router.get("/render/video")
def stream_public_shared_render_video(
    slug: str,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
) -> Response:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    job = db.scalar(
        select(RenderJob)
        .where(
            RenderJob.project_id == project.id,
            RenderJob.status == "succeeded",
            RenderJob.output_object_key.is_not(None),
        )
        .order_by(RenderJob.created_at.desc())
        .limit(1)
    )
    if job is None or not job.output_object_key:
        raise HTTPException(status_code=404, detail="Rendered video is not available")
    return stream_video_response(
        request=request,
        store=store,
        object_key=job.output_object_key,
        missing_detail="Rendered video artifact missing",
        content_disposition=f'inline; filename="trip-share-{project.id}.mp4"',
        extra_headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/photos/{photo_id}/image")
def get_public_shared_photo_image(
    slug: str,
    photo_id: uuid.UUID,
    request: Request,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    variant: Annotated[Literal["full", "thumb"], Query()] = "full",
) -> Response:
    _rate_limit_share(request, db, settings)
    project = _get_shared_project(db, slug)
    photo = db.scalar(
        select(ProjectPhoto).where(
            ProjectPhoto.id == photo_id,
            ProjectPhoto.project_id == project.id,
            ProjectPhoto.enabled.is_(True),
        )
    )
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    key = photo.thumb_object_key if variant == "thumb" else photo.object_key
    try:
        data = store.get_bytes(key)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Photo artifact missing") from exc
    return Response(
        content=data,
        media_type="image/webp",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=300",
        },
    )
