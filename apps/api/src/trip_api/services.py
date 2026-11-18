"""Shared domain services for project ownership, Bronze/Silver ingestion, caching, and mapping."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

import polars as pl
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from trip_telemetry import (
    CURRENT_ETL_VERSION,
    DEFAULT_ACTIVITY_TYPE,
    ActivityType,
    LodLevel,
    ParsedTrack,
    PhotoAlignmentInput,
    StageEventsBundle,
    StageTrackInput,
    align_photos_to_stages,
    apply_non_destructive_trim,
    build_chart_cache_from_parquet,
    build_chart_cache_payload,
    build_lod_cache_from_parquet,
    build_lod_cache_payload,
    build_silver_dataframe,
    build_stage_events_parquet,
    extract_lod_feature_from_cache,
    extract_stage_highlights,
    normalize_activity_type,
    parse_chart_cache_payload,
    parse_stage_events_parquet_bytes,
    query_stage_kpis,
    query_telemetry_profile,
    serialize_stage_events_parquet_bytes,
    write_silver_parquet,
)

from trip_api.db import (
    AuditEvent,
    OAuthConnection,
    Project,
    ProjectPhoto,
    RenderJob,
    Stage,
    User,
)
from trip_api.deps import ensure_utc
from trip_api.schemas import (
    AuditEventOut,
    ClimbHighlightOut,
    DwellStopOut,
    MetricSplitOut,
    PeakEffortOut,
    PhotoMatchMethod,
    PhotoSourceType,
    ProjectHighlightsOut,
    ProjectOut,
    ProjectPhotoOut,
    PublicPhotoOut,
    PublicProjectOut,
    PublicStageOut,
    RenderJobOut,
    StageFormat,
    StageGeoContextOut,
    StageOut,
    TelemetryChartOut,
    TelemetryChartPoint,
    TelemetryGeometryOut,
    UserOut,
)
from trip_api.storage import InvalidRangeError, LocalObjectStore, ObjectStore

logger = logging.getLogger(__name__)

_DEFAULT_CHART_CACHE_POINTS = 1200


def current_etl_version() -> int:
    """Return CURRENT_ETL_VERSION from trip_telemetry."""
    return int(CURRENT_ETL_VERSION)


def stage_lod_cache_key(project_id: uuid.UUID, stage_id: uuid.UUID) -> str:
    """Return the canonical object storage key for a stage's `.lod.json.gz` geometry cache."""
    return f"silver/{project_id}/{stage_id}.lod.json.gz"


def stage_chart_cache_key(project_id: uuid.UUID, stage_id: uuid.UUID) -> str:
    """Return the canonical object storage key for a stage's `.chart.json.gz` profile cache."""
    return f"silver/{project_id}/{stage_id}.chart.json.gz"


def stage_events_cache_key(project_id: uuid.UUID, stage_id: uuid.UUID) -> str:
    """Return the canonical object storage key for a stage's `_events.parquet` sidecar."""
    return f"silver/{project_id}/{stage_id}_events.parquet"


def write_audit_event(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    event_type: str,
    project_id: uuid.UUID | None = None,
    metadata: dict[str, object] | None = None,
) -> AuditEvent:
    """Stage an AuditEvent on the current SQLAlchemy session (caller commits)."""
    event = AuditEvent(
        user_id=user_id,
        project_id=project_id,
        event_type=event_type,
        metadata_json=dict(metadata or {}),
    )
    db.add(event)
    return event


def user_to_out(db: Session, user: User) -> UserOut:
    strava_conn = db.scalar(
        select(OAuthConnection.id).where(
            OAuthConnection.user_id == user.id,
            OAuthConnection.provider == "strava",
        )
    )
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name or user.email.split("@", 1)[0],
        strava_connected=strava_conn is not None,
    )


def _normalize_stage_format(raw: str | None, source_type: str) -> StageFormat:
    if raw in {"gpx", "fit", "tcx", "strava"}:
        return cast(StageFormat, raw)
    return "strava" if source_type == "strava" else "gpx"


def stage_to_out(stage: Stage) -> StageOut:
    source_type = cast(Literal["gpx", "strava"], stage.source_type)
    source_format = _normalize_stage_format(getattr(stage, "source_format", None), source_type)
    etl_version = int(getattr(stage, "etl_version", 1) or 1)
    activity_type = normalize_activity_type(stage.activity_type, DEFAULT_ACTIVITY_TYPE)
    return StageOut(
        id=stage.id,
        project_id=stage.project_id,
        source_type=source_type,
        source_format=source_format,
        etl_version=etl_version,
        activity_type=activity_type,
        external_source_id=stage.external_source_id,
        strava_activity_id=stage.strava_activity_id,
        name=stage.name,
        stage_order=stage.stage_order,
        day_index=stage.day_index,
        trim_start_s=stage.trim_start_s,
        trim_end_s=stage.trim_end_s,
        started_at=ensure_utc(stage.started_at) if stage.started_at else None,
        point_count=stage.point_count,
        distance_m=stage.distance_m,
        elevation_gain_m=stage.elevation_gain_m,
        elevation_loss_m=stage.elevation_loss_m,
        moving_seconds=stage.moving_seconds,
        elapsed_seconds=stage.elapsed_seconds,
        has_timestamps=stage.has_timestamps,
        min_lat=stage.min_lat,
        max_lat=stage.max_lat,
        min_lon=stage.min_lon,
        max_lon=stage.max_lon,
        warnings=list(stage.warnings_json or []),
        created_at=ensure_utc(stage.created_at),
    )


def public_stage_to_out(stage: Stage) -> PublicStageOut:
    """Serialize a Stage for public unauthenticated share links with private metadata stripped."""
    source_type = cast(Literal["gpx", "strava"], stage.source_type)
    source_format = _normalize_stage_format(getattr(stage, "source_format", None), source_type)
    activity_type = normalize_activity_type(stage.activity_type, DEFAULT_ACTIVITY_TYPE)
    return PublicStageOut(
        id=stage.id,
        project_id=stage.project_id,
        source_type=source_type,
        source_format=source_format,
        activity_type=activity_type,
        name=stage.name,
        stage_order=stage.stage_order,
        day_index=stage.day_index,
        trim_start_s=stage.trim_start_s,
        trim_end_s=stage.trim_end_s,
        started_at=ensure_utc(stage.started_at) if stage.started_at else None,
        point_count=stage.point_count,
        distance_m=stage.distance_m,
        elevation_gain_m=stage.elevation_gain_m,
        elevation_loss_m=stage.elevation_loss_m,
        moving_seconds=stage.moving_seconds,
        elapsed_seconds=stage.elapsed_seconds,
        has_timestamps=stage.has_timestamps,
        min_lat=stage.min_lat,
        max_lat=stage.max_lat,
        min_lon=stage.min_lon,
        max_lon=stage.max_lon,
        created_at=ensure_utc(stage.created_at),
    )


def project_to_out(project: Project, stages: list[Stage]) -> ProjectOut:
    total_distance = sum(s.distance_m for s in stages)
    return ProjectOut(
        id=project.id,
        title=project.title,
        revision=project.revision,
        stage_count=len(stages),
        total_distance_m=round(total_distance, 2),
        settings=dict(project.settings_json or {}),
        share_enabled=bool(project.share_enabled),
        share_slug=project.share_slug,
        share_created_at=(
            ensure_utc(project.share_created_at) if project.share_created_at else None
        ),
        created_at=ensure_utc(project.created_at),
        updated_at=ensure_utc(project.updated_at),
    )


def photo_to_out(photo: ProjectPhoto) -> ProjectPhotoOut:
    source_type = cast(PhotoSourceType, photo.source_type)
    match_method = cast(PhotoMatchMethod, photo.match_method)
    return ProjectPhotoOut(
        id=photo.id,
        project_id=photo.project_id,
        stage_id=photo.stage_id,
        source_type=source_type,
        width=photo.width,
        height=photo.height,
        byte_size=photo.byte_size,
        caption=photo.caption,
        taken_at_utc=ensure_utc(photo.taken_at_utc) if photo.taken_at_utc else None,
        lat=photo.lat,
        lon=photo.lon,
        matched_lat=photo.matched_lat,
        matched_lon=photo.matched_lon,
        matched_progress=photo.matched_progress,
        match_method=match_method,
        camera_make_model=getattr(photo, "camera_make_model", None),
        exif_offset_s=int(getattr(photo, "exif_offset_s", 0) or 0),
        enabled=bool(photo.enabled),
        created_at=ensure_utc(photo.created_at),
    )


def public_photo_to_out(photo: ProjectPhoto) -> PublicPhotoOut:
    """Serialize a ProjectPhoto for public share links with internal keys & camera info redacted."""
    match_method = cast(PhotoMatchMethod, photo.match_method)
    return PublicPhotoOut(
        id=photo.id,
        width=photo.width,
        height=photo.height,
        caption=photo.caption,
        taken_at_utc=ensure_utc(photo.taken_at_utc) if photo.taken_at_utc else None,
        lat=photo.lat,
        lon=photo.lon,
        matched_lat=photo.matched_lat,
        matched_lon=photo.matched_lon,
        matched_progress=photo.matched_progress,
        match_method=match_method,
        enabled=bool(photo.enabled),
    )


def get_project_photos(
    db: Session,
    project_id: uuid.UUID,
    *,
    only_enabled: bool = False,
) -> list[ProjectPhoto]:
    stmt = select(ProjectPhoto).where(ProjectPhoto.project_id == project_id)
    if only_enabled:
        stmt = stmt.where(ProjectPhoto.enabled.is_(True))
    stmt = stmt.order_by(
        ProjectPhoto.taken_at_utc.asc().nulls_last(),
        ProjectPhoto.created_at.asc(),
    )
    return list(db.scalars(stmt).all())


def public_project_to_out(
    project: Project,
    stages: list[Stage],
    *,
    photos: list[ProjectPhoto] | None = None,
    latest_render_id: uuid.UUID | None = None,
) -> PublicProjectOut:
    """Serialize a shared Project without owner IDs, share slugs, or private stage metadata."""
    total_distance = sum(s.distance_m for s in stages)
    return PublicProjectOut(
        id=project.id,
        title=project.title,
        stage_count=len(stages),
        total_distance_m=round(total_distance, 2),
        settings=dict(project.settings_json or {}),
        stages=[public_stage_to_out(s) for s in stages],
        photos=[public_photo_to_out(p) for p in (photos or []) if p.enabled],
        has_rendered_video=latest_render_id is not None,
        latest_render_id=latest_render_id,
        created_at=ensure_utc(project.created_at),
        updated_at=ensure_utc(project.updated_at),
    )


def audit_event_to_out(event: AuditEvent) -> AuditEventOut:
    return AuditEventOut(
        id=event.id,
        project_id=event.project_id,
        event_type=event.event_type,
        metadata=dict(event.metadata_json or {}),
        created_at=ensure_utc(event.created_at),
    )


def render_job_to_out(job: RenderJob) -> RenderJobOut:
    return RenderJobOut(
        id=job.id,
        project_id=job.project_id,
        status=job.status,
        progress_pct=job.progress_pct,
        attempt_count=job.attempt_count,
        spec=dict(job.spec_json),
        idempotency_key=job.idempotency_key,
        output_object_key=job.output_object_key,
        thumbnail_key=job.thumbnail_key,
        error_message=job.error_message,
        created_at=ensure_utc(job.created_at),
        updated_at=ensure_utc(job.updated_at),
    )


def get_owned_project(db: Session, project_id: uuid.UUID, user_id: uuid.UUID) -> Project:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user_id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def get_project_stages(db: Session, project_id: uuid.UUID) -> list[Stage]:
    return list(
        db.scalars(
            select(Stage).where(Stage.project_id == project_id).order_by(Stage.stage_order.asc())
        ).all()
    )


def build_stage_chart_cache_bytes(
    parquet_path: Path,
    *,
    max_points: int = _DEFAULT_CHART_CACHE_POINTS,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> bytes:
    """Build `.chart.json.gz` cache payload via trip_telemetry."""
    return build_chart_cache_from_parquet(
        parquet_path,
        max_points=max_points,
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )


def _persist_stage_events_sidecar(
    store: ObjectStore,
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    parquet_path: Path,
    *,
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> None:
    """Persist `_events.parquet` sidecar via trip_telemetry."""
    events_key = stage_events_cache_key(project_id, stage_id)
    with tempfile.TemporaryDirectory(prefix="trip_events_") as tmpdir:
        out_path = Path(tmpdir) / f"{stage_id}_events.parquet"
        build_stage_events_parquet(
            parquet_path,
            out_path,
            stage_id=str(stage_id),
            trim_start_s=trim_start_s,
            trim_end_s=trim_end_s,
        )
        if out_path.exists():
            store.put_bytes(
                events_key,
                out_path.read_bytes(),
                content_type="application/vnd.apache.parquet",
            )


def align_project_photos(
    db: Session,
    store: ObjectStore,
    project_id: uuid.UUID,
    *,
    photo_ids: set[uuid.UUID] | None = None,
    override_offsets: dict[uuid.UUID, int] | None = None,
    persist: bool = True,
) -> list[ProjectPhoto]:
    """Align non-manual project photos against project stages via DuckDB ASOF + spatial."""
    photos = get_project_photos(db, project_id)
    stages = get_project_stages(db, project_id)
    if not photos:
        return []

    target_photos = [
        p
        for p in photos
        if p.match_method != "manual" and (photo_ids is None or p.id in photo_ids)
    ]
    if not target_photos:
        return photos

    if not stages:
        for p in target_photos:
            p.stage_id = None
            p.matched_lat = None
            p.matched_lon = None
            p.matched_progress = None
            p.match_method = "unmatched"
        if persist:
            db.commit()
            for p in target_photos:
                db.refresh(p)
        return photos

    with materialize_silver_parquets(store, stages) as pairs:
        stage_inputs = [
            StageTrackInput(
                stage_id=str(st.id),
                stage_order=st.stage_order,
                parquet_path=pp,
                trim_start_s=st.trim_start_s,
                trim_end_s=st.trim_end_s,
            )
            for st, pp in pairs
        ]
        photo_inputs = [
            PhotoAlignmentInput(
                id=str(p.id),
                taken_at_utc=ensure_utc(p.taken_at_utc) if p.taken_at_utc else None,
                lat=p.lat,
                lon=p.lon,
                exif_offset_s=(
                    override_offsets[p.id]
                    if override_offsets and p.id in override_offsets
                    else int(getattr(p, "exif_offset_s", 0) or 0)
                ),
                camera_make_model=getattr(p, "camera_make_model", None),
                match_method=p.match_method,
                stage_id=str(p.stage_id) if p.stage_id else None,
                matched_lat=p.matched_lat,
                matched_lon=p.matched_lon,
                matched_progress=p.matched_progress,
            )
            for p in target_photos
        ]
        aligned = align_photos_to_stages(photo_inputs, stage_inputs)

    by_id = {uuid.UUID(res.photo_id): res for res in aligned}
    for p in target_photos:
        res = by_id.get(p.id)
        if res is None:
            continue
        p.stage_id = uuid.UUID(res.stage_id) if res.stage_id else None
        p.matched_lat = res.matched_lat
        p.matched_lon = res.matched_lon
        p.matched_progress = res.matched_progress
        p.match_method = res.match_method

    if persist:
        db.commit()
        for p in target_photos:
            db.refresh(p)
    return photos


def ingest_parsed_track(
    *,
    db: Session,
    store: ObjectStore,
    project: Project,
    parsed: ParsedTrack,
    raw_bronze_bytes: bytes,
    bronze_ext: str,
    bronze_content_type: str,
    source_type: Literal["gpx", "strava"],
    stage_name: str,
    day_index: int,
    activity_type: ActivityType = DEFAULT_ACTIVITY_TYPE,
    source_format: StageFormat | None = None,
    external_source_id: str | None = None,
    strava_activity_id: int | None = None,
    auto_align_photos: bool = True,
) -> Stage:
    """Persist Bronze payload, Silver Parquet + caches/sidecars, and insert Stage."""
    stage_id = uuid.uuid4()
    full_sha256 = hashlib.sha256(raw_bronze_bytes).hexdigest()
    digest = full_sha256[:16]
    bronze_key = f"bronze/{project.id}/{stage_id}/{digest}.{bronze_ext}"
    silver_key = f"silver/{project.id}/{stage_id}.parquet"
    lod_key = stage_lod_cache_key(project.id, stage_id)
    chart_key = stage_chart_cache_key(project.id, stage_id)

    store.put_bytes(bronze_key, raw_bronze_bytes, content_type=bronze_content_type)

    df, summary = build_silver_dataframe(
        parsed,
        trip_id=str(project.id),
        stage_id=str(stage_id),
    )
    with tempfile.TemporaryDirectory(prefix="trip_silver_") as tmpdir:
        parquet_path = write_silver_parquet(df, Path(tmpdir) / f"{stage_id}.parquet")
        store.put_bytes(
            silver_key,
            parquet_path.read_bytes(),
            content_type="application/vnd.apache.parquet",
        )
        try:
            chart_gz_bytes = build_stage_chart_cache_bytes(parquet_path)
            store.put_bytes(chart_key, chart_gz_bytes, content_type="application/gzip")
        except Exception as exc:
            logger.warning("Could not write chart cache for stage %s: %s", stage_id, exc)
        try:
            _persist_stage_events_sidecar(store, project.id, stage_id, parquet_path)
        except Exception as exc:
            logger.warning("Could not write events sidecar for stage %s: %s", stage_id, exc)

    lod_gz_bytes = build_lod_cache_payload(parsed.points)
    store.put_bytes(lod_key, lod_gz_bytes, content_type="application/gzip")

    max_order = db.scalar(
        select(func.coalesce(func.max(Stage.stage_order), -1)).where(
            Stage.project_id == project.id
        )
    )
    next_order = int(max_order if max_order is not None else -1) + 1

    started_at: datetime | None = next(
        (pt.ts_utc for pt in parsed.points if pt.ts_utc is not None), None
    )

    resolved_activity = normalize_activity_type(activity_type, DEFAULT_ACTIVITY_TYPE)
    resolved_format = _normalize_stage_format(source_format, source_type)

    stage = Stage(
        id=stage_id,
        project_id=project.id,
        source_type=source_type,
        source_format=resolved_format,
        etl_version=current_etl_version(),
        bronze_sha256=full_sha256,
        activity_type=resolved_activity,
        external_source_id=external_source_id,
        strava_activity_id=strava_activity_id,
        name=stage_name[:200],
        stage_order=next_order,
        day_index=day_index,
        bronze_object_key=bronze_key,
        silver_object_key=silver_key,
        started_at=started_at,
        point_count=summary.point_count,
        distance_m=summary.distance_m,
        elevation_gain_m=summary.elevation_gain_m,
        elevation_loss_m=summary.elevation_loss_m,
        moving_seconds=summary.moving_seconds,
        elapsed_seconds=summary.elapsed_seconds,
        has_timestamps=summary.has_timestamps,
        min_lat=summary.min_lat,
        max_lat=summary.max_lat,
        min_lon=summary.min_lon,
        max_lon=summary.max_lon,
        warnings_json=[w.model_dump(mode="json") for w in parsed.warnings],
    )
    db.add(stage)
    db.commit()
    db.refresh(stage)

    if auto_align_photos:
        try:
            align_project_photos(db, store, project.id, persist=True)
        except Exception as exc:
            logger.warning("Photo auto-alignment failed after stage ingest %s: %s", stage.id, exc)

    return stage


def recompute_stage_trim_artifacts(
    db: Session,
    store: ObjectStore,
    stage: Stage,
    *,
    trim_start_s: float | None,
    trim_end_s: float | None,
) -> tuple[bytes, bytes]:
    """Update Silver Parquet `is_trimmed` and return `(lod_gz_bytes, chart_gz_bytes)`."""
    with materialize_silver_parquet(store, stage) as parquet_path:
        df = pl.read_parquet(parquet_path)
        trimmed_df, summary = apply_non_destructive_trim(
            df,
            trim_start_s=trim_start_s,
            trim_end_s=trim_end_s,
        )
        with tempfile.TemporaryDirectory(prefix="trip_trim_") as tmpdir:
            tmp_parquet = write_silver_parquet(trimmed_df, Path(tmpdir) / f"{stage.id}.parquet")
            store.put_bytes(
                stage.silver_object_key,
                tmp_parquet.read_bytes(),
                content_type="application/vnd.apache.parquet",
            )
            lod_gz = build_lod_cache_from_parquet(
                tmp_parquet,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s,
            )
            chart_gz = build_stage_chart_cache_bytes(
                tmp_parquet,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s,
            )
            try:
                _persist_stage_events_sidecar(
                    store,
                    stage.project_id,
                    stage.id,
                    tmp_parquet,
                    trim_start_s=trim_start_s,
                    trim_end_s=trim_end_s,
                )
            except Exception as exc:
                logger.warning("Failed to rebuild events sidecar for %s: %s", stage.id, exc)

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
    return lod_gz, chart_gz


def get_silver_total_elapsed_s(store: ObjectStore, stage: Stage) -> float:
    """Return total untrimmed duration in seconds from Silver Parquet (fallback to stage column)."""
    try:
        with materialize_silver_parquet(store, stage) as parquet_path:
            df = pl.read_parquet(parquet_path, columns=["ts_utc"])
            if df.height == 0 or not bool(df.get_column("ts_utc").is_not_null().all()):
                return stage.elapsed_seconds
            min_ts = df.get_column("ts_utc").min()
            max_ts = df.get_column("ts_utc").max()
            if isinstance(min_ts, datetime) and isinstance(max_ts, datetime):
                return max((max_ts - min_ts).total_seconds(), 0.0)
    except Exception as exc:
        logger.debug("Falling back to stage.elapsed_seconds for %s: %s", stage.id, exc)
    return stage.elapsed_seconds


@contextmanager
def materialize_silver_parquet(store: ObjectStore, stage: Stage) -> Iterator[Path]:
    """Yield a local filesystem path for one stage's Silver Parquet, cleaning up temp files."""
    if isinstance(store, LocalObjectStore):
        yield store.resolve_existing_path(stage.silver_object_key)
        return

    with tempfile.TemporaryDirectory(prefix="trip_duckdb_one_") as tmpdir:
        target = Path(tmpdir) / f"{stage.id}.parquet"
        target.write_bytes(store.get_bytes(stage.silver_object_key))
        yield target


@contextmanager
def materialize_silver_parquets(
    store: ObjectStore,
    stages: list[Stage],
) -> Iterator[list[tuple[Stage, Path]]]:
    """Yield local filesystem paths for each stage's Silver Parquet, cleaning up temp files."""
    if isinstance(store, LocalObjectStore):
        pairs: list[tuple[Stage, Path]] = []
        for stage in stages:
            path = store.resolve_existing_path(stage.silver_object_key)
            pairs.append((stage, path))
        yield pairs
        return

    with tempfile.TemporaryDirectory(prefix="trip_duckdb_") as tmpdir:
        tmp_root = Path(tmpdir)

        def _fetch_one(stage: Stage) -> tuple[Stage, Path]:
            target = tmp_root / f"{stage.id}.parquet"
            target.write_bytes(store.get_bytes(stage.silver_object_key))
            return stage, target

        if len(stages) <= 1:
            yield [_fetch_one(s) for s in stages]
            return

        with ThreadPoolExecutor(max_workers=min(8, len(stages))) as pool:
            temp_pairs = list(pool.map(_fetch_one, stages))
        yield temp_pairs


def validate_and_build_stage_lod_cache(
    store: ObjectStore,
    stage: Stage,
    *,
    trim_start_s: float | None,
    trim_end_s: float | None,
) -> bytes:
    """Validate trim bounds against Silver Parquet and build `.lod.json.gz` cache bytes."""
    with materialize_silver_parquet(store, stage) as parquet_path:
        if trim_start_s is not None or trim_end_s is not None:
            kpis = query_stage_kpis(
                parquet_path,
                trim_start_s=trim_start_s,
                trim_end_s=trim_end_s,
            )
            if int(kpis["point_count"]) <= 0:
                raise ValueError("Trim window excludes all telemetry points")
        return build_lod_cache_from_parquet(
            parquet_path,
            trim_start_s=trim_start_s,
            trim_end_s=trim_end_s,
        )


def persist_or_delete_stage_lod_cache(
    store: ObjectStore,
    stage: Stage,
    gz_bytes: bytes,
) -> None:
    """Write precomputed `.lod.json.gz` bytes, deleting any stale cache if write fails."""
    lod_key = stage_lod_cache_key(stage.project_id, stage.id)
    try:
        store.put_bytes(lod_key, gz_bytes, content_type="application/gzip")
    except Exception:
        logger.warning("Failed to write LOD cache for stage %s; deleting stale cache", stage.id)
        try:
            store.delete(lod_key)
        except Exception:
            logger.warning("Failed to delete stale LOD cache key %s", lod_key)


def persist_or_delete_stage_chart_cache(
    store: ObjectStore,
    stage: Stage,
    gz_bytes: bytes,
) -> None:
    """Write precomputed `.chart.json.gz` bytes, deleting any stale cache if write fails."""
    chart_key = stage_chart_cache_key(stage.project_id, stage.id)
    try:
        store.put_bytes(chart_key, gz_bytes, content_type="application/gzip")
    except Exception:
        logger.warning("Failed to write chart cache for stage %s; deleting stale cache", stage.id)
        try:
            store.delete(chart_key)
        except Exception:
            logger.warning("Failed to delete stale chart cache key %s", chart_key)


def invalidate_or_rebuild_stage_lod_cache(store: ObjectStore, stage: Stage) -> None:
    """Rebuild `.lod.json.gz` after a stage trim change, falling back to cache deletion."""
    lod_key = stage_lod_cache_key(stage.project_id, stage.id)
    try:
        gz_bytes = validate_and_build_stage_lod_cache(
            store,
            stage,
            trim_start_s=stage.trim_start_s,
            trim_end_s=stage.trim_end_s,
        )
        store.put_bytes(lod_key, gz_bytes, content_type="application/gzip")
    except ValueError:
        raise
    except Exception:
        logger.warning("Failed to rebuild LOD cache for stage %s; deleting stale cache", stage.id)
        try:
            store.delete(lod_key)
        except Exception:
            logger.warning("Failed to delete stale LOD cache key %s", lod_key)


def stream_video_response(
    *,
    request: Request,
    store: ObjectStore,
    object_key: str,
    missing_detail: str,
    content_disposition: str,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Stream an MP4 artifact in chunks with HTTP Range (206) support."""
    range_header = request.headers.get("range")
    try:
        stream = store.open_stream(object_key, range_header=range_header)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=missing_detail) from exc
    except InvalidRangeError as exc:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{exc.total_size}"},
        )

    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(stream.content_length),
        "Content-Disposition": content_disposition,
    }
    if extra_headers:
        headers.update(extra_headers)
    if stream.is_partial:
        headers["Content-Range"] = f"bytes {stream.start}-{stream.end}/{stream.total_size}"

    return StreamingResponse(
        stream.chunks,
        status_code=206 if stream.is_partial else 200,
        media_type="video/mp4",
        headers=headers,
    )


def build_stage_geometry_feature(
    store: ObjectStore,
    stage: Stage,
    lod: LodLevel,
) -> dict[str, object]:
    """Return a GeoJSON Feature for `stage` at `lod`, serving `.lod.json.gz` cache on hit."""
    lod_key = stage_lod_cache_key(stage.project_id, stage.id)
    has_trim = stage.trim_start_s is not None or stage.trim_end_s is not None

    feature: dict[str, object] | None = None
    try:
        cached_gz = store.get_bytes(lod_key)
        feature = extract_lod_feature_from_cache(
            cached_gz,
            lod,
            trim_start_s=stage.trim_start_s,
            trim_end_s=stage.trim_end_s,
        )
    except FileNotFoundError:
        feature = None
    except Exception:
        feature = None

    trimmed_kpis: dict[str, object] | None = None
    if feature is None or has_trim:
        with materialize_silver_parquet(store, stage) as parquet_path:
            if feature is None:
                gz_bytes = build_lod_cache_from_parquet(
                    parquet_path,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )
                try:
                    store.put_bytes(lod_key, gz_bytes, content_type="application/gzip")
                except Exception:
                    logger.warning("Could not persist backfilled LOD cache %s", lod_key)
                feature = extract_lod_feature_from_cache(
                    gz_bytes,
                    lod,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )
            if has_trim:
                trimmed_kpis = query_stage_kpis(
                    parquet_path,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )

    if feature is None:
        raise ValueError(f"Failed to extract {lod} geometry for stage {stage.id}")

    raw_props = feature.get("properties")
    props: dict[str, object] = dict(raw_props) if isinstance(raw_props, dict) else {}
    activity_type = normalize_activity_type(stage.activity_type, DEFAULT_ACTIVITY_TYPE)
    props.update(
        {
            "stage_id": str(stage.id),
            "name": stage.name,
            "day_index": stage.day_index,
            "stage_order": stage.stage_order,
            "source_type": stage.source_type,
            "activity_type": activity_type,
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
        }
    )
    feature["id"] = str(stage.id)
    feature["properties"] = props
    return feature


def build_project_geometry_collection(
    store: ObjectStore,
    project_id: uuid.UUID,
    stages: list[Stage],
    lod: LodLevel,
) -> TelemetryGeometryOut:
    """Resolve GeoJSON features for all `stages` in parallel via `.lod.json.gz` cache."""
    if not stages:
        return TelemetryGeometryOut(project_id=project_id, features=[])

    if len(stages) == 1:
        features = [build_stage_geometry_feature(store, stages[0], lod)]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(stages))) as pool:
            features = list(
                pool.map(lambda st: build_stage_geometry_feature(store, st, lod), stages)
            )
    return TelemetryGeometryOut(project_id=project_id, features=features)


def _downsample_chart_profile(
    profile: list[dict[str, object]],
    target_points: int,
) -> list[dict[str, object]]:
    if len(profile) <= target_points:
        return profile
    if target_points <= 2:
        return [profile[0], profile[-1]] if len(profile) >= 2 else list(profile)
    step = (len(profile) - 1) / (target_points - 1)
    return [profile[round(i * step)] for i in range(target_points)]


def _load_or_build_stage_chart_profile(
    store: ObjectStore,
    project_id: uuid.UUID,
    stage: Stage,
    per_stage_budget: int,
) -> tuple[list[dict[str, object]], float]:
    profile: list[dict[str, object]] | None = None
    chart_key = stage_chart_cache_key(project_id, stage.id)
    if per_stage_budget <= _DEFAULT_CHART_CACHE_POINTS:
        try:
            profile = parse_chart_cache_payload(
                store.get_bytes(chart_key),
                trim_start_s=stage.trim_start_s,
                trim_end_s=stage.trim_end_s,
            )
        except Exception:
            profile = None
        if profile is not None:
            profile = _downsample_chart_profile(profile, per_stage_budget)

    if profile is None:
        with materialize_silver_parquet(store, stage) as parquet_path:
            if per_stage_budget <= _DEFAULT_CHART_CACHE_POINTS:
                full_profile = query_telemetry_profile(
                    parquet_path,
                    max_points=_DEFAULT_CHART_CACHE_POINTS,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )
                try:
                    chart_gz = build_chart_cache_payload(
                        full_profile,
                        trim_start_s=stage.trim_start_s,
                        trim_end_s=stage.trim_end_s,
                        max_points=_DEFAULT_CHART_CACHE_POINTS,
                    )
                    store.put_bytes(chart_key, chart_gz, content_type="application/gzip")
                except Exception:
                    logger.warning("Could not persist backfilled chart cache %s", chart_key)
                profile = _downsample_chart_profile(full_profile, per_stage_budget)
            else:
                profile = query_telemetry_profile(
                    parquet_path,
                    max_points=per_stage_budget,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )

    has_trim = stage.trim_start_s is not None or stage.trim_end_s is not None
    max_row_dist = max((float(cast(float, row["cum_dist_m"])) for row in profile), default=0.0)
    stage_dist = max_row_dist if has_trim else float(stage.distance_m or 0.0)
    return profile, stage_dist


def _load_or_build_stage_events(
    store: ObjectStore,
    project_id: uuid.UUID,
    stage: Stage,
) -> StageEventsBundle:
    events_key = stage_events_cache_key(project_id, stage.id)
    bundle: StageEventsBundle | None = None
    try:
        bundle = parse_stage_events_parquet_bytes(
            store.get_bytes(events_key),
            trim_start_s=stage.trim_start_s,
            trim_end_s=stage.trim_end_s,
        )
    except Exception:
        bundle = None

    if bundle is None:
        with materialize_silver_parquet(store, stage) as parquet_path:
            bundle = extract_stage_highlights(
                parquet_path,
                stage_id=str(stage.id),
                trim_start_s=stage.trim_start_s,
                trim_end_s=stage.trim_end_s,
            )
        try:
            store.put_bytes(
                events_key,
                serialize_stage_events_parquet_bytes(bundle),
                content_type="application/vnd.apache.parquet",
            )
        except Exception:
            logger.warning("Could not persist backfilled events sidecar %s", events_key)

    return bundle


def build_project_telemetry_chart(
    store: ObjectStore,
    project_id: uuid.UUID,
    stages: list[Stage],
    max_points: int,
) -> TelemetryChartOut:
    """Build combined LTTB telemetry chart points with lazy Silver Parquet materialization."""
    if not stages:
        return TelemetryChartOut(project_id=project_id, total_distance_m=0.0, points=[])

    combined: list[TelemetryChartPoint] = []
    distance_offset = 0.0
    per_stage_budget = max(2, min(max_points, max(25, max_points // len(stages))))

    if len(stages) == 1:
        resolved = [
            _load_or_build_stage_chart_profile(store, project_id, stages[0], per_stage_budget)
        ]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(stages))) as pool:
            resolved = list(
                pool.map(
                    lambda st: _load_or_build_stage_chart_profile(
                        store, project_id, st, per_stage_budget
                    ),
                    stages,
                )
            )

    for profile, stage_dist in resolved:
        for row in profile:
            d_val = float(cast(float, row["cum_dist_m"]))
            combined.append(
                TelemetryChartPoint(
                    cum_dist_m=round(distance_offset + d_val, 2),
                    elev_m=cast(float | None, row["elev_m"]),
                    speed_mps=float(cast(float, row["speed_mps"])),
                    grade_pct=cast(float | None, row["grade_pct"]),
                    hr_bpm=cast(float | None, row["hr_bpm"]),
                    watts=cast(float | None, row["watts"]),
                )
            )
        distance_offset += stage_dist

    if len(combined) > max_points:
        step = (len(combined) - 1) / (max_points - 1)
        combined = [combined[round(i * step)] for i in range(max_points)]

    return TelemetryChartOut(
        project_id=project_id,
        total_distance_m=round(distance_offset, 2),
        points=combined,
    )


def build_project_highlights(
    store: ObjectStore,
    project_id: uuid.UUID,
    stages: list[Stage],
) -> ProjectHighlightsOut:
    """Assemble climbs, splits, dwell stops, peak efforts, and geo context for a project."""
    if not stages:
        return ProjectHighlightsOut(project_id=project_id)

    total_project_distance = sum(max(float(s.distance_m or 0.0), 0.0) for s in stages)
    all_climbs: list[ClimbHighlightOut] = []
    all_splits: list[MetricSplitOut] = []
    all_dwells: list[DwellStopOut] = []
    all_peaks: list[PeakEffortOut] = []
    all_geo: list[StageGeoContextOut] = []

    if len(stages) == 1:
        bundles = [_load_or_build_stage_events(store, project_id, stages[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(stages))) as pool:
            bundles = list(
                pool.map(lambda st: _load_or_build_stage_events(store, project_id, st), stages)
            )

    distance_offset = 0.0
    for stage, bundle in zip(stages, bundles, strict=True):
        denom = (
            total_project_distance
            if total_project_distance > 0
            else max(stage.distance_m, 1.0)
        )
        for idx, c in enumerate(bundle.climbs):
            sd = float(c.start_dist_m)
            ed = float(c.end_dist_m)
            all_climbs.append(
                ClimbHighlightOut(
                    id=str(c.id or f"{stage.id}-climb-{idx}"),
                    stage_id=stage.id,
                    name=str(c.name or f"Climb {idx + 1}"),
                    category=c.category,
                    start_dist_m=round(distance_offset + sd, 2),
                    end_dist_m=round(distance_offset + ed, 2),
                    start_progress=round(
                        min(max((distance_offset + sd) / denom, 0.0), 1.0), 6
                    ),
                    end_progress=round(
                        min(max((distance_offset + ed) / denom, 0.0), 1.0), 6
                    ),
                    summit_progress=round(
                        min(
                            max(
                                (distance_offset + sd + (ed - sd) * float(c.summit_progress))
                                / denom,
                                0.0,
                            ),
                            1.0,
                        ),
                        6,
                    ),
                    summit_lat=float(c.summit_lat),
                    summit_lon=float(c.summit_lon),
                    summit_elev_m=float(c.summit_elev_m),
                    distance_m=float(c.distance_m),
                    elevation_gain_m=float(c.elevation_gain_m),
                    avg_grade_pct=float(c.avg_grade_pct),
                    max_grade_pct=float(c.max_grade_pct),
                    fiets_score=float(c.fiets_score),
                )
            )
        for sp in bundle.splits:
            all_splits.append(
                MetricSplitOut(
                    stage_id=stage.id,
                    split_index=int(sp.split_index),
                    split_km=int(sp.split_index) + 1,
                    distance_m=float(sp.distance_m),
                    elapsed_seconds=float(sp.duration_s),
                    moving_seconds=float(sp.duration_s),
                    elevation_gain_m=float(sp.elev_gain_m),
                    elevation_loss_m=float(sp.elev_loss_m),
                    avg_speed_mps=float(sp.avg_speed_mps),
                    avg_grade_pct=sp.avg_grade_pct,
                    avg_hr_bpm=sp.avg_hr_bpm,
                    avg_watts=sp.avg_watts,
                )
            )
        for idx, dw in enumerate(bundle.dwell_stops):
            cd = float(dw.start_dist_m)
            all_dwells.append(
                DwellStopOut(
                    id=str(dw.id or f"{stage.id}-dwell-{idx}"),
                    stage_id=stage.id,
                    lat=float(dw.lat),
                    lon=float(dw.lon),
                    cum_dist_m=round(distance_offset + cd, 2),
                    progress=round(
                        min(max((distance_offset + cd) / denom, 0.0), 1.0), 6
                    ),
                    duration_seconds=float(dw.duration_s),
                    started_at=dw.started_at_utc,
                    ended_at=dw.ended_at_utc,
                    inferred_kind=str(dw.suggested_icon),
                )
            )
        for pk in bundle.peak_efforts:
            win_s = int(pk.window_s)
            win_label = f"{win_s // 60}m" if win_s >= 60 and win_s % 60 == 0 else f"{win_s}s"
            all_peaks.append(
                PeakEffortOut(
                    stage_id=stage.id,
                    metric=str(pk.metric),
                    window_label=win_label,
                    window_seconds=win_s,
                    value=float(pk.value),
                    start_dist_m=round(distance_offset + float(pk.start_dist_m), 2),
                    end_dist_m=round(distance_offset + float(pk.end_dist_m), 2),
                )
            )
        countries_list = list(bundle.geo_context.countries)
        all_geo.append(
            StageGeoContextOut(
                stage_id=stage.id,
                countries=countries_list,
                regions=list(bundle.geo_context.regions),
                passes=list(bundle.geo_context.passes),
                primary_country=countries_list[0] if countries_list else None,
            )
        )

        distance_offset += max(float(stage.distance_m or 0.0), 0.0)

    return ProjectHighlightsOut(
        project_id=project_id,
        climbs=all_climbs,
        splits=all_splits,
        dwell_stops=all_dwells,
        peak_efforts=all_peaks,
        geo_context=all_geo,
    )
