"""Multi-format track upload ingestion (GPX/FIT/TCX) and project stage authoring endpoints."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import Annotated, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl
from fastapi import APIRouter, Body, File, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy import select
from trip_telemetry import (
    ActivityType,
    GpxParseError,
    ParsedTrack,
    TrackParseError,
    apply_non_destructive_trim,
    build_lod_cache_from_parquet,
    build_stage_events_parquet,
    detect_track_format,
    infer_activity_type_from_strava,
    parse_track_with_timeout,
    write_silver_parquet,
)

from trip_api.db import Stage
from trip_api.deps import AppSettings, CsrfAuth, DbSession, StoreDep, ensure_utc
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    PalettePreset,
    ProjectDetailOut,
    StageAutoOrganizeRequest,
    StageFormat,
    StageOut,
    StageReorderRequest,
    StageUpdateRequest,
)
from trip_api.services import (
    align_project_photos,
    build_stage_chart_cache_bytes,
    get_owned_project,
    get_project_stages,
    get_silver_total_elapsed_s,
    invalidate_or_rebuild_stage_lod_cache,
    materialize_silver_parquet,
    persist_or_delete_stage_chart_cache,
    persist_or_delete_stage_lod_cache,
    persist_prepared_stage,
    prepare_stage_artifacts,
    project_to_out,
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    stage_to_out,
    write_audit_event,
)

MAX_GPX_BYTES = 25 * 1024 * 1024  # 25 MiB
_CHUNK_BYTES = 64 * 1024
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024

_PALETTES: dict[PalettePreset, tuple[str, ...]] = {
    "alpine-dawn": (
        "#EA580C",
        "#F59E0B",
        "#0EA5E9",
        "#10B981",
        "#8B5CF6",
        "#EC4899",
        "#14B8A6",
        "#6366F1",
    ),
    "topo-emerald": (
        "#059669",
        "#0D9488",
        "#0284C7",
        "#65A30D",
        "#D97706",
        "#0891B2",
        "#16A34A",
        "#4F46E5",
    ),
    "stage-by-stage": (
        "#E11D48",
        "#2563EB",
        "#16A34A",
        "#D97706",
        "#7C3AED",
        "#0891B2",
        "#DB2777",
        "#4D7C0F",
    ),
}

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["stages"])
logger = logging.getLogger(__name__)


def _detect_track_format(data: bytes, filename: str | None) -> StageFormat | None:
    """Detect `.fit`, `.tcx`, or `.gpx` strictly from magic bytes / root tags."""
    try:
        return cast(StageFormat, detect_track_format(data, filename=filename))
    except TrackParseError:
        return None


def _parse_track_payload(
    raw_bytes: bytes,
    *,
    filename: str | None,
    source_format: StageFormat,
) -> ParsedTrack:
    return parse_track_with_timeout(
        raw_bytes,
        format_hint=source_format,
        filename=filename,
        stitch_tracks=True,
    )


async def _read_bounded_upload(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"GPX file exceeds maximum allowed size of {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _handle_track_upload(
    *,
    project_id: uuid.UUID,
    request: Request,
    file: UploadFile,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    day_index: int,
    name: str | None,
    activity_type: ActivityType | None,
) -> StageOut:
    project = get_owned_project(db, project_id, auth.user.id)
    check_rate_limit(
        db,
        bucket_key=f"gpx:{auth.user.id}",
        limit=settings.rate_limit_gpx_max,
        window_seconds=settings.rate_limit_gpx_window_s,
    )

    content_len_raw = request.headers.get("content-length")
    if content_len_raw is not None:
        try:
            content_len = int(content_len_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        if content_len > MAX_GPX_BYTES + _MULTIPART_OVERHEAD_BYTES:
            raise HTTPException(status_code=413, detail="Upload payload exceeds 25 MiB limit")

    raw_bytes = await _read_bounded_upload(file, MAX_GPX_BYTES)
    if not raw_bytes:
        raise HTTPException(status_code=422, detail="Uploaded track file is empty")

    source_format = _detect_track_format(raw_bytes, file.filename)
    if source_format is None:
        raise HTTPException(
            status_code=422,
            detail="Uploaded file is not valid GPX, FIT, or TCX telemetry",
        )

    try:
        parsed = await asyncio.to_thread(
            _parse_track_payload,
            raw_bytes,
            filename=file.filename,
            source_format=source_format,
        )
    except (GpxParseError, TrackParseError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    fallback_name = Path(file.filename or "stage").stem or "Uploaded Stage"
    resolved_name = (name.strip() if name else "") or parsed.name or fallback_name
    resolved_activity = activity_type or infer_activity_type_from_strava(
        parsed.activity_type_hint,
        f"{resolved_name} {file.filename or ''}",
    )

    content_type_map: dict[StageFormat, str] = {
        "gpx": "application/gpx+xml",
        "fit": "application/octet-stream",
        "tcx": "application/vnd.garmin.tcx+xml",
        "strava": "application/json",
    }

    prepared = await asyncio.to_thread(
        prepare_stage_artifacts,
        store=store,
        project_id=project.id,
        parsed=parsed,
        raw_bronze_bytes=raw_bytes,
        bronze_ext=source_format,
        bronze_content_type=content_type_map[source_format],
    )
    try:
        stage = persist_prepared_stage(
            db=db,
            store=store,
            project=project,
            prepared=prepared,
            source_type="gpx",
            source_format=source_format,
            stage_name=resolved_name,
            day_index=day_index,
            activity_type=resolved_activity,
        )
        write_audit_event(
            db,
            user_id=auth.user.id,
            project_id=project.id,
            event_type="stage.gpx_uploaded",
            metadata={
                "stage_id": str(stage.id),
                "name": stage.name,
                "source_format": source_format,
                "activity_type": stage.activity_type,
                "point_count": stage.point_count,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        for k in prepared.uploaded_keys:
            try:
                await asyncio.to_thread(store.delete, k)
            except Exception:
                logger.warning("Failed to rollback stage key %s", k)
        raise
    return stage_to_out(stage)


@router.post("/gpx", response_model=StageOut, status_code=201)
async def upload_gpx_stage(
    project_id: uuid.UUID,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    day_index: Annotated[int, Form(ge=1, le=365)] = 1,
    name: Annotated[str | None, Form(max_length=200)] = None,
    activity_type: Annotated[ActivityType | None, Form()] = None,
) -> StageOut:
    return await _handle_track_upload(
        project_id=project_id,
        request=request,
        file=file,
        auth=auth,
        db=db,
        settings=settings,
        store=store,
        day_index=day_index,
        name=name,
        activity_type=activity_type,
    )


@router.post("/tracks", response_model=StageOut, status_code=201)
async def upload_track_stage(
    project_id: uuid.UUID,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    day_index: Annotated[int, Form(ge=1, le=365)] = 1,
    name: Annotated[str | None, Form(max_length=200)] = None,
    activity_type: Annotated[ActivityType | None, Form()] = None,
) -> StageOut:
    return await _handle_track_upload(
        project_id=project_id,
        request=request,
        file=file,
        auth=auth,
        db=db,
        settings=settings,
        store=store,
        day_index=day_index,
        name=name,
        activity_type=activity_type,
    )


@router.patch("/stages/{stage_id}", response_model=StageOut)
def update_stage(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    body: StageUpdateRequest,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> StageOut:
    project = get_owned_project(db, project_id, auth.user.id)
    stage = db.scalar(select(Stage).where(Stage.id == stage_id, Stage.project_id == project.id))
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")

    next_start = (
        body.trim_start_s if "trim_start_s" in body.model_fields_set else stage.trim_start_s
    )
    next_end = body.trim_end_s if "trim_end_s" in body.model_fields_set else stage.trim_end_s
    trim_fields_touched = (
        "trim_start_s" in body.model_fields_set or "trim_end_s" in body.model_fields_set
    )
    has_next_trim = next_start is not None or next_end is not None

    if has_next_trim and trim_fields_touched:
        if not stage.has_timestamps:
            raise HTTPException(
                status_code=422,
                detail="Cannot trim a stage without timestamps",
            )
        if next_start is not None and next_end is not None and next_end <= next_start:
            raise HTTPException(
                status_code=422, detail="trim_end_s must be greater than trim_start_s"
            )
        total_elapsed_s = max(stage.elapsed_seconds, get_silver_total_elapsed_s(store, stage))
        if (next_start is not None and next_start > total_elapsed_s) or (
            next_end is not None and next_end > total_elapsed_s
        ):
            raise HTTPException(
                status_code=422,
                detail="Trim bounds cannot exceed stage duration",
            )

    trim_changed = (
        "trim_start_s" in body.model_fields_set and body.trim_start_s != stage.trim_start_s
    ) or ("trim_end_s" in body.model_fields_set and body.trim_end_s != stage.trim_end_s)

    staged_silver_bytes: bytes | None = None
    staged_lod_gz: bytes | None = None
    staged_chart_gz: bytes | None = None
    staged_events_bytes: bytes | None = None

    if trim_changed:
        try:
            with materialize_silver_parquet(store, stage) as parquet_path:
                df = pl.read_parquet(parquet_path)
                trimmed_df, summary = apply_non_destructive_trim(
                    df,
                    trim_start_s=next_start,
                    trim_end_s=next_end,
                )
                with tempfile.TemporaryDirectory(prefix="trip_trim_") as tmpdir:
                    tmp_parquet = write_silver_parquet(
                        trimmed_df,
                        Path(tmpdir) / f"{stage.id}.parquet",
                    )
                    staged_silver_bytes = tmp_parquet.read_bytes()
                    staged_lod_gz = build_lod_cache_from_parquet(
                        tmp_parquet,
                        trim_start_s=next_start,
                        trim_end_s=next_end,
                    )
                    staged_chart_gz = build_stage_chart_cache_bytes(
                        tmp_parquet,
                        trim_start_s=next_start,
                        trim_end_s=next_end,
                    )
                    events_path = Path(tmpdir) / f"{stage.id}_events.parquet"
                    build_stage_events_parquet(
                        tmp_parquet,
                        events_path,
                        stage_id=str(stage.id),
                        trim_start_s=next_start,
                        trim_end_s=next_end,
                    )
                    if events_path.exists():
                        staged_events_bytes = events_path.read_bytes()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        stage.point_count = summary.point_count
        stage.distance_m = summary.distance_m
        stage.elevation_gain_m = summary.elevation_gain_m
        stage.elevation_loss_m = summary.elevation_loss_m
        stage.moving_seconds = summary.moving_seconds
        stage.elapsed_seconds = summary.elapsed_seconds
        stage.min_lat = summary.min_lat
        stage.max_lat = summary.max_lat
        stage.min_lon = summary.min_lon
        stage.max_lon = summary.max_lon

    if body.name is not None:
        stage.name = body.name.strip()
    if body.day_index is not None:
        stage.day_index = body.day_index
    if body.activity_type is not None:
        stage.activity_type = body.activity_type
    if "trim_start_s" in body.model_fields_set:
        stage.trim_start_s = body.trim_start_s
    if "trim_end_s" in body.model_fields_set:
        stage.trim_end_s = body.trim_end_s

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="stage.updated",
        metadata={
            "stage_id": str(stage.id),
            "name": stage.name,
            "activity_type": stage.activity_type,
        },
    )
    db.commit()
    db.refresh(stage)

    if trim_changed:
        if staged_silver_bytes is not None:
            store.put_bytes(
                stage.silver_object_key,
                staged_silver_bytes,
                content_type="application/vnd.apache.parquet",
            )
        if staged_lod_gz is not None:
            persist_or_delete_stage_lod_cache(store, stage, staged_lod_gz)
        else:
            invalidate_or_rebuild_stage_lod_cache(store, stage)
        if staged_chart_gz is not None:
            persist_or_delete_stage_chart_cache(store, stage, staged_chart_gz)
        if staged_events_bytes is not None:
            try:
                store.put_bytes(
                    stage_events_cache_key(stage.project_id, stage.id),
                    staged_events_bytes,
                    content_type="application/vnd.apache.parquet",
                )
            except Exception as exc:
                logger.warning("Failed to update events sidecar for stage %s: %s", stage.id, exc)
        try:
            align_project_photos(db, store, project.id, persist=True)
        except Exception as exc:
            logger.warning("Photo re-alignment failed after trim on stage %s: %s", stage.id, exc)

    return stage_to_out(stage)


@router.delete("/stages/{stage_id}", status_code=204)
def delete_stage(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> Response:
    project = get_owned_project(db, project_id, auth.user.id)
    stage = db.scalar(select(Stage).where(Stage.id == stage_id, Stage.project_id == project.id))
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")

    keys_to_delete = [
        stage.bronze_object_key,
        stage.silver_object_key,
        stage_lod_cache_key(project.id, stage.id),
        stage_chart_cache_key(project.id, stage.id),
        stage_events_cache_key(project.id, stage.id),
    ]
    deleted_stage_name = stage.name
    sid_str = str(stage.id)
    settings_copy = dict(project.settings_json or {})
    changed_settings = False
    for map_key in ("stage_colors", "stage_transitions"):
        raw_map = settings_copy.get(map_key)
        if isinstance(raw_map, dict) and sid_str in raw_map:
            updated_map = {k: v for k, v in raw_map.items() if k != sid_str}
            settings_copy[map_key] = updated_map
            changed_settings = True
    if changed_settings:
        project.settings_json = settings_copy

    db.delete(stage)
    db.flush()

    remaining = get_project_stages(db, project.id)
    for idx, rem in enumerate(remaining):
        rem.stage_order = idx
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="stage.deleted",
        metadata={"stage_id": sid_str, "name": deleted_stage_name},
    )
    db.commit()

    for key in keys_to_delete:
        try:
            store.delete(key)
        except Exception:
            logger.warning("Failed to delete stage object key %s", key)

    return Response(status_code=204)


@router.post("/stages/reorder", response_model=list[StageOut])
def reorder_stages(
    project_id: uuid.UUID,
    body: StageReorderRequest,
    auth: CsrfAuth,
    db: DbSession,
) -> list[StageOut]:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    existing_by_id = {s.id: s for s in stages}

    if len(body.stage_ids) != len(existing_by_id) or set(body.stage_ids) != set(existing_by_id):
        raise HTTPException(
            status_code=422,
            detail="stage_ids must be an exact permutation of the project's stages",
        )

    if body.day_indices is not None:
        for sid, day_idx in body.day_indices.items():
            if sid not in existing_by_id or day_idx < 1 or day_idx > 365:
                raise HTTPException(status_code=422, detail="Invalid stage_id or day_index")

    for order_idx, sid in enumerate(body.stage_ids):
        stage = existing_by_id[sid]
        stage.stage_order = order_idx
        if body.day_indices and sid in body.day_indices:
            stage.day_index = body.day_indices[sid]

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="stage.reordered",
        metadata={"stage_count": len(body.stage_ids)},
    )
    db.commit()
    ordered = get_project_stages(db, project.id)
    return [stage_to_out(s) for s in ordered]


def _resolve_timezone(tz_name: str | None) -> tzinfo:
    if not tz_name:
        return UTC
    cleaned = tz_name.strip()
    if not cleaned:
        return UTC
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return UTC


@router.post("/stages/auto-organize", response_model=ProjectDetailOut)
def auto_organize_stages(
    project_id: uuid.UUID,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
    body: Annotated[StageAutoOrganizeRequest | None, Body()] = None,
) -> ProjectDetailOut:
    """Sort stages chronologically, group day_index by local calendar date, and apply palette."""
    opts = body or StageAutoOrganizeRequest()
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)

    max_dt = datetime.max.replace(tzinfo=UTC)
    stages.sort(
        key=lambda s: (
            s.started_at is None,
            ensure_utc(s.started_at) if s.started_at is not None else max_dt,
            s.stage_order,
        )
    )

    target_tz = _resolve_timezone(opts.timezone)
    current_day = 1
    prev_local_date: date | None = None
    for idx, stage in enumerate(stages):
        stage.stage_order = idx
        curr_local_date = (
            ensure_utc(stage.started_at).astimezone(target_tz).date()
            if stage.started_at is not None
            else None
        )
        if curr_local_date is not None:
            if prev_local_date is not None and curr_local_date > prev_local_date:
                current_day = min(current_day + 1, 365)
            prev_local_date = curr_local_date
        stage.day_index = current_day

    palette = _PALETTES[opts.palette_preset]
    settings_copy = dict(project.settings_json or {})
    stage_colors: dict[str, str] = {}
    for idx, stage in enumerate(stages):
        stage_colors[str(stage.id)] = palette[idx % len(palette)]
    settings_copy["stage_colors"] = stage_colors

    if opts.assign_overnight_icons:
        existing_transitions = settings_copy.get("stage_transitions")
        transitions: dict[str, str] = (
            {str(k): str(v) for k, v in existing_transitions.items()}
            if isinstance(existing_transitions, dict)
            else {}
        )
        for idx in range(len(stages) - 1):
            curr = stages[idx]
            nxt = stages[idx + 1]
            if nxt.day_index > curr.day_index:
                transitions[str(curr.id)] = "tent" if curr.activity_type == "camp" else "bed"
            else:
                transitions.pop(str(curr.id), None)
        if stages:
            transitions.pop(str(stages[-1].id), None)
        settings_copy["stage_transitions"] = transitions

    project.settings_json = settings_copy
    project.revision += 1
    project.updated_at = datetime.now(UTC)

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="stage.auto_organized",
        metadata={
            "stage_count": len(stages),
            "day_count": current_day if stages else 0,
            "palette_preset": opts.palette_preset,
        },
    )
    db.commit()
    db.refresh(project)
    ordered = get_project_stages(db, project.id)

    try:
        align_project_photos(db, store, project.id, persist=True)
    except Exception as exc:
        logger.warning("Photo re-alignment failed after auto-organize: %s", exc)

    base = project_to_out(project, ordered)
    return ProjectDetailOut(
        **base.model_dump(),
        stages=[stage_to_out(s) for s in ordered],
    )
