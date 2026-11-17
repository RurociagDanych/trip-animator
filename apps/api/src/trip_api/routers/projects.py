"""Project CRUD, sharing, audit log, and GPX/GeoJSON export endpoints."""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Body, HTTPException, Query, Response
from sqlalchemy import select
from trip_telemetry import query_stage_export_points, query_stage_kpis

from trip_api.db import AuditEvent, Project, ProjectPhoto, RenderJob, Stage
from trip_api.deps import CsrfAuth, CurrentAuth, DbSession, StoreDep, ensure_utc
from trip_api.schemas import (
    AuditEventOut,
    ProjectCreateRequest,
    ProjectDetailOut,
    ProjectOut,
    ProjectUpdateRequest,
    ShareSettingsOut,
    ShareUpdateRequest,
    validate_stage_maps_against_project,
)
from trip_api.services import (
    audit_event_to_out,
    get_owned_project,
    get_project_stages,
    materialize_silver_parquets,
    project_to_out,
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    stage_to_out,
    write_audit_event,
)

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])
logger = logging.getLogger(__name__)


def _safe_export_slug(title: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", title.strip().lower()).strip("-")
    return cleaned[:48] or "trip"


def _share_settings_from_project(project: Project) -> ShareSettingsOut:
    return ShareSettingsOut(
        share_enabled=bool(project.share_enabled),
        share_slug=project.share_slug,
        share_created_at=(
            ensure_utc(project.share_created_at) if project.share_created_at else None
        ),
    )


@router.get("", response_model=list[ProjectOut])
def list_projects(
    auth: CurrentAuth,
    db: DbSession,
) -> list[ProjectOut]:
    projects = list(
        db.scalars(
            select(Project)
            .where(Project.owner_user_id == auth.user.id)
            .order_by(Project.updated_at.desc())
        ).all()
    )
    return [project_to_out(p, get_project_stages(db, p.id)) for p in projects]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectCreateRequest,
    auth: CsrfAuth,
    db: DbSession,
) -> ProjectOut:
    validate_stage_maps_against_project(body.settings, set())
    now = datetime.now(UTC)
    project = Project(
        owner_user_id=auth.user.id,
        title=body.title.strip(),
        revision=1,
        settings_json=body.settings.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    db.flush()
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="project.created",
        metadata={"title": project.title},
    )
    db.commit()
    db.refresh(project)
    return project_to_out(project, [])


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
) -> ProjectDetailOut:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    base = project_to_out(project, stages)
    return ProjectDetailOut(
        **base.model_dump(),
        stages=[stage_to_out(s) for s in stages],
    )


@router.patch("/{project_id}", response_model=ProjectDetailOut)
def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdateRequest,
    auth: CsrfAuth,
    db: DbSession,
) -> ProjectDetailOut:
    project = get_owned_project(db, project_id, auth.user.id)
    if body.revision != project.revision:
        raise HTTPException(
            status_code=409,
            detail=f"Revision conflict: expected {project.revision}, got {body.revision}",
        )

    stages = get_project_stages(db, project.id)
    if body.settings is not None:
        valid_stage_ids = {str(s.id) for s in stages}
        validate_stage_maps_against_project(body.settings, valid_stage_ids)
        project.settings_json = body.settings.model_dump(mode="json")

    if body.title is not None:
        project.title = body.title.strip()

    project.revision += 1
    project.updated_at = datetime.now(UTC)
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="project.updated",
        metadata={
            "revision": project.revision,
            "title_updated": body.title is not None,
            "settings_updated": body.settings is not None,
        },
    )
    db.commit()
    db.refresh(project)

    base = project_to_out(project, stages)
    return ProjectDetailOut(
        **base.model_dump(),
        stages=[stage_to_out(s) for s in stages],
    )


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> Response:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    photos = list(
        db.scalars(select(ProjectPhoto).where(ProjectPhoto.project_id == project.id)).all()
    )
    jobs = list(db.scalars(select(RenderJob).where(RenderJob.project_id == project.id)).all())

    keys_to_delete: list[str] = []
    for stage in stages:
        keys_to_delete.extend(
            [
                stage.bronze_object_key,
                stage.silver_object_key,
                stage_lod_cache_key(project.id, stage.id),
                stage_chart_cache_key(project.id, stage.id),
                stage_events_cache_key(project.id, stage.id),
            ]
        )
    for photo in photos:
        keys_to_delete.extend([photo.object_key, photo.thumb_object_key])
    for job in jobs:
        if job.output_object_key:
            keys_to_delete.append(job.output_object_key)
        if job.thumbnail_key:
            keys_to_delete.append(job.thumbnail_key)

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=None,
        event_type="project.deleted",
        metadata={"deleted_project_id": str(project.id), "title": project.title},
    )
    db.delete(project)
    db.commit()

    for key in keys_to_delete:
        try:
            store.delete(key)
        except Exception:
            logger.warning("Failed to delete object key %s during project cleanup", key)

    return Response(status_code=204)


@router.post("/{project_id}/share", response_model=ShareSettingsOut)
def enable_or_rotate_share(
    project_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
    body: Annotated[ShareUpdateRequest | None, Body()] = None,
) -> ShareSettingsOut:
    project = get_owned_project(db, project_id, auth.user.id)
    rotate = bool(body.rotate) if body is not None else False
    now = datetime.now(UTC)

    had_slug = bool(project.share_slug)
    if not had_slug or rotate:
        project.share_slug = secrets.token_urlsafe(16)
        project.share_created_at = now
    project.share_enabled = True
    project.updated_at = now

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="project.share_rotated" if (rotate and had_slug) else "project.share_enabled",
        metadata={"share_enabled": True},
    )
    db.commit()
    db.refresh(project)
    return _share_settings_from_project(project)


@router.delete("/{project_id}/share", response_model=ShareSettingsOut)
def disable_share(
    project_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
) -> ShareSettingsOut:
    project = get_owned_project(db, project_id, auth.user.id)
    project.share_enabled = False
    project.updated_at = datetime.now(UTC)
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="project.share_disabled",
        metadata={"share_enabled": False},
    )
    db.commit()
    db.refresh(project)
    return _share_settings_from_project(project)


@router.get("/{project_id}/audit", response_model=list[AuditEventOut])
def list_project_audit_events(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditEventOut]:
    project = get_owned_project(db, project_id, auth.user.id)
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.project_id == project.id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
        ).all()
    )
    return [audit_event_to_out(e) for e in events]


@router.get("/{project_id}/export")
def export_project_tracks(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
    format: Annotated[Literal["gpx", "geojson"], Query()] = "gpx",
) -> Response:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    if not stages:
        raise HTTPException(status_code=422, detail="Cannot export a project with zero stages")

    slug = _safe_export_slug(project.title)

    try:
        with materialize_silver_parquets(store, stages) as pairs:
            stage_tracks: list[
                tuple[
                    Stage,
                    list[tuple[float, float, float | None, datetime | None, int]],
                    dict[str, object] | None,
                ]
            ] = []
            for stage, parquet_path in pairs:
                pts = query_stage_export_points(
                    parquet_path,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )
                trimmed_kpis: dict[str, object] | None = None
                if stage.trim_start_s is not None or stage.trim_end_s is not None:
                    trimmed_kpis = query_stage_kpis(
                        parquet_path,
                        trim_start_s=stage.trim_start_s,
                        trim_end_s=stage.trim_end_s,
                    )
                stage_tracks.append((stage, pts, trimmed_kpis))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="project.exported",
        metadata={"format": format, "stage_count": len(stages)},
    )
    db.commit()

    if format == "geojson":
        features: list[dict[str, object]] = []
        for stage, pts, trimmed_kpis in stage_tracks:
            lines: list[list[list[float]]] = []
            cur_seg: int | None = None
            cur_line: list[list[float]] = []
            for lat, lon, elev_m, _ts, seg_idx in pts:
                coord = (
                    [round(lon, 6), round(lat, 6), round(elev_m, 2)]
                    if elev_m is not None
                    else [round(lon, 6), round(lat, 6)]
                )
                if cur_seg is None or seg_idx != cur_seg:
                    if cur_line:
                        lines.append(
                            cur_line if len(cur_line) >= 2 else [cur_line[0], cur_line[0]]
                        )
                    cur_seg = seg_idx
                    cur_line = [coord]
                else:
                    cur_line.append(coord)
            if cur_line:
                lines.append(cur_line if len(cur_line) >= 2 else [cur_line[0], cur_line[0]])
            features.append(
                {
                    "type": "Feature",
                    "id": str(stage.id),
                    "geometry": {
                        "type": "MultiLineString",
                        "coordinates": lines,
                    },
                    "properties": {
                        "stage_id": str(stage.id),
                        "name": stage.name,
                        "day_index": stage.day_index,
                        "stage_order": stage.stage_order,
                        "activity_type": stage.activity_type,
                        "distance_m": (
                            float(cast(float, trimmed_kpis["total_distance_m"]))
                            if trimmed_kpis is not None
                            else stage.distance_m
                        ),
                        "elevation_gain_m": (
                            float(cast(float, trimmed_kpis["elevation_gain_m"]))
                            if trimmed_kpis is not None
                            else stage.elevation_gain_m
                        ),
                    },
                }
            )
        payload = json.dumps(
            {
                "type": "FeatureCollection",
                "name": project.title,
                "features": features,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return Response(
            content=payload,
            media_type="application/geo+json",
            headers={"Content-Disposition": f'attachment; filename="{slug}.geojson"'},
        )

    xml_lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Trip Animator" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{xml_escape(project.title)}</name>",
        "  </metadata>",
    ]
    for stage, pts, _trimmed_kpis in stage_tracks:
        xml_lines.append("  <trk>")
        xml_lines.append(f"    <name>{xml_escape(stage.name)}</name>")
        xml_lines.append(f"    <type>{xml_escape(stage.activity_type)}</type>")
        cur_seg = None
        for lat, lon, elev_m, ts_utc, seg_idx in pts:
            if cur_seg is None or seg_idx != cur_seg:
                if cur_seg is not None:
                    xml_lines.append("    </trkseg>")
                xml_lines.append("    <trkseg>")
                cur_seg = seg_idx
            xml_lines.append(f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">')
            if elev_m is not None:
                xml_lines.append(f"        <ele>{elev_m:.2f}</ele>")
            if ts_utc is not None:
                iso_ts = ts_utc.astimezone(UTC).isoformat().replace("+00:00", "Z")
                xml_lines.append(f"        <time>{iso_ts}</time>")
            xml_lines.append("      </trkpt>")
        if cur_seg is not None:
            xml_lines.append("    </trkseg>")
        xml_lines.append("  </trk>")
    xml_lines.append("</gpx>")

    return Response(
        content="\n".join(xml_lines).encode("utf-8"),
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{slug}.gpx"'},
    )
