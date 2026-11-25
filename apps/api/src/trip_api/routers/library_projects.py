"""Create projects from private Activity Library material."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Literal

import polars as pl
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from trip_telemetry import build_silver_dataframe, parse_strava_streams, parse_track_bytes

from trip_api.db import IngestJob, Project, Stage, VaultActivity
from trip_api.deps import CsrfAuth, DbSession, StoreDep
from trip_api.reuse_preview import ReusePreviewCache
from trip_api.schemas import ProjectReuseEligibility

router = APIRouter(prefix="/api/v1/library/projects", tags=["library-projects"])
logger = logging.getLogger(__name__)


class LibraryProjectCreateRequest(BaseModel):
    """Describe a project created from selected library activities."""

    title: str = Field(min_length=1, max_length=160)
    activity_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator("activity_ids")
    @classmethod
    def unique_activity_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("activity_ids must be unique")
        return value


class LibraryProjectReuseRequest(BaseModel):
    """Describe library activities appended to an existing project."""

    activity_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator("activity_ids")
    @classmethod
    def unique_activity_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("activity_ids must be unique")
        return value


class LibraryProjectOut(BaseModel):
    """Identify a project and the stages created by a library action."""

    project_id: uuid.UUID
    title: str
    stage_ids: list[uuid.UUID]


def _owned_activities(
    db: Session, store: StoreDep, user_id: uuid.UUID, activity_ids: list[uuid.UUID]
) -> list[tuple[VaultActivity, Stage]]:
    activities = list(
        db.scalars(
            select(VaultActivity).where(
                VaultActivity.user_id == user_id,
                VaultActivity.id.in_(activity_ids),
                VaultActivity.disconnected_at.is_(None),
            )
        ).all()
    )
    by_id = {activity.id: activity for activity in activities}
    if len(by_id) != len(activity_ids):
        raise HTTPException(status_code=404, detail="One or more library activities were not found")

    result: list[tuple[VaultActivity, Stage]] = []
    for activity_id in activity_ids:
        activity = by_id[activity_id]
        # Canonical standalone bytes are independent of project stages, which
        # may have been locally trimmed or edited since provider sync.
        source = _standalone_original(store, activity)
        if source is None and activity.source_stage_id is None:
            source = _find_unlinked_original(db, store, user_id, activity)
        elif source is None:
            source = db.scalar(
                select(Stage)
                .join(Project, Project.id == Stage.project_id)
                .where(
                    Stage.id == activity.source_stage_id,
                    Project.owner_user_id == user_id,
                    Stage.vault_activity_id == activity.id,
                )
            )
        if source is None or not source.silver_object_key or not source.bronze_object_key:
            raise HTTPException(status_code=409, detail="Activity has no ready local track data")
        result.append((activity, source))
    return result


def project_reuse_eligibility(
    db: Session, store: StoreDep, activity: VaultActivity
) -> Literal["ready", "waiting", "no_gps", "unavailable"]:
    """Report readiness using the same owner-scoped original validation as project reuse."""
    if not activity.has_gps:
        return "no_gps"
    try:
        sources = _owned_activities(db, store, activity.user_id, [activity.id])
        _validate_artifacts(store, sources)
    except HTTPException as exc:
        if exc.status_code not in (404, 409):
            raise
    else:
        return "ready"
    failed_job = db.scalar(
        select(IngestJob.id)
        .where(
            IngestJob.provider == "strava",
            IngestJob.payload_json["kind"].as_string() == "library_enrichment",
            IngestJob.payload_json["owner_user_id"].as_string() == str(activity.user_id),
            IngestJob.payload_json["catalog_activity_id"].as_string() == str(activity.id),
            IngestJob.status.in_(["failed", "ignored"]),
        )
        .limit(1)
    )
    if (
        failed_job is not None
        or activity.disconnected_at is not None
        or activity.silver_object_key
        or activity.source_stage_id is not None
    ):
        return "unavailable"
    return "waiting"


def batch_reuse_preview(
    db: Session,
    store: StoreDep,
    cache: ReusePreviewCache,
    user_id: uuid.UUID,
    activities: list[VaultActivity],
) -> dict[uuid.UUID, ProjectReuseEligibility]:
    """Validate connected, owner-scoped activities without per-activity SQL."""
    statuses: dict[uuid.UUID, ProjectReuseEligibility] = {}
    unresolved = []
    for activity in activities:
        if not activity.has_gps:
            statuses[activity.id] = "no_gps"
        elif cache.is_ready(activity):
            statuses[activity.id] = "ready"
        elif _standalone_original(store, activity) is not None:
            # Standalone resolution compares the full telemetry, not just geometry.
            cache.mark_ready(activity)
            statuses[activity.id] = "ready"
        else:
            unresolved.append(activity)
    if not unresolved:
        return statuses

    linked_ids = {a.source_stage_id for a in unresolved if a.source_stage_id is not None}
    provider_ids = {
        a.strava_activity_id
        for a in unresolved
        if a.source_stage_id is None
        and a.source_provider == "strava"
        and a.strava_activity_id is not None
        and a.external_activity_id == str(a.strava_activity_id)
    }
    linked: dict[uuid.UUID, Stage] = {}
    by_provider: dict[int, list[Stage]] = defaultdict(list)
    if linked_ids or provider_ids:
        for stage in db.scalars(
            select(Stage)
            .join(Project, Project.id == Stage.project_id)
            .where(
                Project.owner_user_id == user_id,
                Stage.id.in_(linked_ids) | Stage.strava_activity_id.in_(provider_ids),
            )
        ):
            if stage.id in linked_ids:
                linked[stage.id] = stage
            if stage.strava_activity_id is not None:
                by_provider[stage.strava_activity_id].append(stage)

    waiting = []
    for activity in unresolved:
        if activity.source_stage_id is not None:
            source = linked.get(activity.source_stage_id)
            if source is not None and source.vault_activity_id != activity.id:
                source = None
        elif (
            activity.strava_activity_id in provider_ids
            and activity.source_provider == "strava"
            and activity.external_activity_id == str(activity.strava_activity_id)
        ):
            source = _verified_unlinked_original(
                store, activity, by_provider.get(activity.strava_activity_id, [])
            )
        else:
            source = None
        if source is not None and source.bronze_object_key and source.silver_object_key:
            try:
                _validate_artifacts(store, [(activity, source)])
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
            else:
                statuses[activity.id] = "ready"
                continue
        if activity.silver_object_key or activity.source_stage_id is not None:
            statuses[activity.id] = "unavailable"
        else:
            waiting.append(activity)

    if waiting:
        catalog_id = IngestJob.payload_json["catalog_activity_id"].as_string()
        failed = set(
            db.scalars(
                select(catalog_id)
                .where(
                    IngestJob.provider == "strava",
                    IngestJob.payload_json["kind"].as_string() == "library_enrichment",
                    IngestJob.payload_json["owner_user_id"].as_string() == str(user_id),
                    catalog_id.in_([str(a.id) for a in waiting]),
                    IngestJob.status.in_(["failed", "ignored"]),
                )
                .distinct()
            )
        )
        for activity in waiting:
            statuses[activity.id] = "unavailable" if str(activity.id) in failed else "waiting"
    return statuses


def _standalone_original(store: StoreDep, activity: VaultActivity) -> Stage | None:
    """Build transient stage metadata only from digest-verified original provider bytes."""
    if (
        activity.source_provider != "strava"
        or activity.strava_activity_id is None
        or activity.external_activity_id != str(activity.strava_activity_id)
        or not activity.bronze_object_key
        or not activity.silver_object_key
        or not re.fullmatch(r"[0-9a-f]{64}", activity.source_sha256 or "")
    ):
        return None
    try:
        raw = store.get_bytes(activity.bronze_object_key)
        bronze = json.loads(raw)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    provider = bronze.get("activity") if isinstance(bronze, dict) else None
    streams = bronze.get("streams") if isinstance(bronze, dict) else None
    if (
        hashlib.sha256(raw).hexdigest() != activity.source_sha256
        or not isinstance(provider, dict)
        or str(provider.get("id")) != str(activity.strava_activity_id)
        or not isinstance(streams, dict)
        or not isinstance(streams.get("latlng"), dict)
        or not streams["latlng"].get("data")
        or not isinstance(activity.bronze_object_key, str)
        or not isinstance(activity.silver_object_key, str)
        or not activity.bronze_object_key.startswith(f"bronze/{activity.id}/")
        or not activity.silver_object_key.startswith(f"silver/{activity.id}/")
    ):
        return None
    try:
        parsed = parse_strava_streams(
            streams, start_date=provider.get("start_date"), name=activity.name
        )
        expected, summary = build_silver_dataframe(
            parsed, trip_id=str(activity.id), stage_id="original"
        )
        silver = pl.read_parquet(BytesIO(store.get_bytes(activity.silver_object_key)))
        columns = [name for name in expected.columns if name not in {"trip_id", "stage_id"}]
        if not expected.select(columns).equals(silver.select(columns)):
            return None
    except (FileNotFoundError, OSError, ValueError, TypeError, pl.exceptions.PolarsError):
        return None
    return Stage(
        id=uuid.uuid5(activity.id, "standalone-source"),
        source_type="strava",
        source_format="strava",
        bronze_sha256=activity.source_sha256,
        activity_type=activity.activity_type,
        external_source_id=activity.external_activity_id,
        strava_activity_id=activity.strava_activity_id,
        vault_activity_id=activity.id,
        name=activity.name,
        bronze_object_key=activity.bronze_object_key,
        silver_object_key=activity.silver_object_key,
        started_at=activity.started_at,
        point_count=parsed.point_count,
        distance_m=summary.distance_m,
        elevation_gain_m=summary.elevation_gain_m,
        elevation_loss_m=summary.elevation_loss_m,
        moving_seconds=summary.moving_seconds,
        elapsed_seconds=summary.elapsed_seconds,
        has_timestamps=parsed.has_timestamps,
        warnings_json=[],
    )


def _is_original_provider_stage(stage: Stage, activity: VaultActivity) -> bool:
    if (
        stage.source_type != "strava"
        or stage.source_format != "strava"
        or stage.strava_activity_id is None
        or stage.strava_activity_id != activity.strava_activity_id
        or stage.external_source_id != str(activity.strava_activity_id)
        or stage.trim_start_s is not None
        or stage.trim_end_s is not None
        or stage.vault_activity_id not in (None, activity.id)
        or not re.fullmatch(r"[0-9a-f]{64}", stage.bronze_sha256 or "")
        or any(
            isinstance(warning, dict)
            and warning.get("code") == "vault_canonical_excluded_split_segment"
            for warning in stage.warnings_json or []
        )
    ):
        return False
    return True


def _find_unlinked_original(
    db: Session, store: StoreDep, owner_id: uuid.UUID, activity: VaultActivity
) -> Stage | None:
    if (
        activity.source_provider != "strava"
        or activity.strava_activity_id is None
        or activity.external_activity_id != str(activity.strava_activity_id)
    ):
        return None
    candidates = list(
        db.scalars(
            select(Stage)
            .join(Project, Project.id == Stage.project_id)
            .where(
                Project.owner_user_id == owner_id,
                Stage.strava_activity_id == activity.strava_activity_id,
                (Stage.vault_activity_id.is_(None) | (Stage.vault_activity_id == activity.id)),
            )
        ).all()
    )
    return _verified_unlinked_original(store, activity, candidates)


def _verified_unlinked_original(
    store: StoreDep, activity: VaultActivity, candidates: Iterable[Stage]
) -> Stage | None:
    for stage in candidates:
        if not _is_original_provider_stage(stage, activity):
            continue
        try:
            raw = store.get_bytes(stage.bronze_object_key)
            bronze = json.loads(raw)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        provider_activity = bronze.get("activity") if isinstance(bronze, dict) else None
        if (
            hashlib.sha256(raw).hexdigest() == stage.bronze_sha256
            and isinstance(provider_activity, dict)
            and str(provider_activity.get("id")) == str(activity.strava_activity_id)
            and isinstance(bronze.get("streams"), dict)
        ):
            return stage
    return None


def _row_snapshot(row: Any) -> dict[str, Any]:
    return {column.key: deepcopy(getattr(row, column.key)) for column in row.__table__.columns}


def _attach_recovered_sources(sources: list[tuple[VaultActivity, Stage]]) -> None:
    for activity, source in sources:
        if source.id == uuid.uuid5(activity.id, "standalone-source"):
            continue
        if activity.source_stage_id is None:
            source.vault_activity_id = activity.id
            activity.source_stage_id = source.id


def _lock_and_validate_sources(
    db: Session,
    store: StoreDep,
    user_id: uuid.UUID,
    activity_ids: list[uuid.UUID],
    prepared: list[tuple[VaultActivity, Stage]],
    snapshots: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[tuple[VaultActivity, Stage]]:
    locked_activities = list(
        db.scalars(
            select(VaultActivity)
            .where(
                VaultActivity.user_id == user_id,
                VaultActivity.id.in_(activity_ids),
                VaultActivity.disconnected_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    by_id = {activity.id: activity for activity in locked_activities}
    if len(by_id) != len(activity_ids):
        raise HTTPException(status_code=409, detail="Library activity changed during reuse")

    validated: list[tuple[VaultActivity, Stage]] = []
    source: Stage | None
    for (prepared_activity, prepared_source), (activity_snapshot, source_snapshot) in zip(
        prepared, snapshots, strict=True
    ):
        activity = by_id[prepared_activity.id]
        if _row_snapshot(activity) != activity_snapshot:
            raise HTTPException(status_code=409, detail="Library activity changed during reuse")
        if prepared_source.id == uuid.uuid5(activity.id, "standalone-source"):
            # Artifacts were verified before locking. Under locks, only
            # database snapshots are revalidated; do no storage I/O.
            source = prepared_source
        elif activity_snapshot["source_stage_id"] is None:
            source = db.scalar(
                select(Stage)
                .join(Project, Project.id == Stage.project_id)
                .where(
                    Stage.id == prepared_source.id,
                    Project.owner_user_id == user_id,
                    (Stage.vault_activity_id.is_(None) | (Stage.vault_activity_id == activity.id)),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        else:
            source = db.scalar(
                select(Stage)
                .join(Project, Project.id == Stage.project_id)
                .where(
                    Stage.id == activity.source_stage_id,
                    Project.owner_user_id == user_id,
                    Stage.vault_activity_id == activity.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if (
            source is None
            or not source.bronze_object_key
            or not source.silver_object_key
            or source.id != prepared_source.id
            or _row_snapshot(source) != source_snapshot
        ):
            raise HTTPException(status_code=409, detail="Library activity changed during reuse")
        validated.append((activity, source))
    return validated


def _validate_artifacts(
    store: StoreDep, sources: list[tuple[VaultActivity, Stage]]
) -> list[tuple[bytes, bytes]]:
    artifacts: list[tuple[bytes, bytes]] = []
    for activity, source in sources:
        namespace = (
            activity.id
            if source.id == uuid.uuid5(activity.id, "standalone-source")
            else source.project_id
        )
        try:
            bronze = store.get_bytes(source.bronze_object_key)
            silver = store.get_bytes(source.silver_object_key)
            if (
                hashlib.sha256(bronze).hexdigest() != source.bronze_sha256
                or not source.bronze_object_key.startswith(f"bronze/{namespace}/")
                or not source.silver_object_key.startswith(f"silver/{namespace}/")
                or source.trim_start_s is not None
                or source.trim_end_s is not None
                or any(
                    isinstance(warning, dict)
                    and warning.get("code") == "vault_canonical_excluded_split_segment"
                    for warning in source.warnings_json or []
                )
            ):
                raise ValueError("Unverified or edited source")
            if source.source_format == "strava":
                payload = json.loads(bronze)
                provider = payload["activity"]
                if (
                    not isinstance(provider, dict)
                    or str(provider.get("id")) != str(activity.strava_activity_id)
                    or source.strava_activity_id != activity.strava_activity_id
                ):
                    raise ValueError("Source identity mismatch")
                parsed = parse_strava_streams(
                    payload["streams"], start_date=provider.get("start_date"), name=source.name
                )
            else:
                parsed = parse_track_bytes(bronze, format_hint=source.source_format)
            expected, _ = build_silver_dataframe(parsed, trip_id="source", stage_id="source")
            stored = pl.read_parquet(BytesIO(silver))
            if not set(expected.columns).issubset(stored.columns):
                raise ValueError("Incomplete source telemetry schema")
            columns = ["lat", "lon", "ts_utc", "segment_idx", "is_trimmed"]
            if not expected.select(columns).equals(stored.select(columns)):
                raise ValueError("Source geometry mismatch")
            artifacts.append((bronze, silver))
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            pl.exceptions.PolarsError,
        ) as exc:
            raise HTTPException(
                status_code=409,
                detail="Activity source artifacts are unavailable",
            ) from exc
    return artifacts


def _copy_stage(
    source: Stage,
    project_id: uuid.UUID,
    stage_order: int,
    day_index: int,
    stage_id: uuid.UUID,
) -> Stage:
    bronze_extension = (source.source_format or "gpx").lower().lstrip(".")
    return Stage(
        id=stage_id,
        project_id=project_id,
        source_type=source.source_type,
        source_format=source.source_format,
        etl_version=source.etl_version,
        bronze_sha256=source.bronze_sha256,
        activity_type=source.activity_type,
        external_source_id=source.external_source_id,
        strava_activity_id=source.strava_activity_id,
        vault_activity_id=source.vault_activity_id,
        name=source.name,
        stage_order=stage_order,
        day_index=day_index,
        trim_start_s=None,
        trim_end_s=None,
        bronze_object_key=f"bronze/{project_id}/{stage_id}/{stage_id}.{bronze_extension}",
        silver_object_key=f"silver/{project_id}/{stage_id}.parquet",
        started_at=source.started_at,
        point_count=source.point_count,
        distance_m=source.distance_m,
        elevation_gain_m=source.elevation_gain_m,
        elevation_loss_m=source.elevation_loss_m,
        moving_seconds=source.moving_seconds,
        elapsed_seconds=source.elapsed_seconds,
        has_timestamps=source.has_timestamps,
        min_lat=source.min_lat,
        max_lat=source.max_lat,
        min_lon=source.min_lon,
        max_lon=source.max_lon,
        warnings_json=list(source.warnings_json or []),
    )


def _build_stages(
    project: Project,
    sources: list[tuple[VaultActivity, Stage]],
) -> list[Stage]:
    stages = [_copy_stage(source, project.id, 0, 1, uuid.uuid4()) for _, source in sources]
    return stages


def _copy_artifacts(
    store: StoreDep,
    stages: list[Stage],
    artifacts: list[tuple[bytes, bytes]],
) -> list[str]:
    created_keys: list[str] = []
    try:
        for stage, (bronze_bytes, silver_bytes) in zip(stages, artifacts, strict=True):
            created_keys.extend((stage.bronze_object_key, stage.silver_object_key))
            bronze_content_type = {
                "gpx": "application/gpx+xml",
                "tcx": "application/vnd.garmin.tcx+xml",
            }.get((stage.source_format or "").lower(), "application/octet-stream")
            store.put_bytes(stage.bronze_object_key, bronze_bytes, content_type=bronze_content_type)
            store.put_bytes(
                stage.silver_object_key,
                silver_bytes,
                content_type="application/vnd.apache.parquet",
            )
    except Exception:
        for key in created_keys:
            try:
                store.delete(key)
            except Exception:
                logger.warning("Could not clean up copied library artifact %s", key, exc_info=True)
        raise
    return created_keys


def _cleanup_artifacts(store: StoreDep, keys: list[str]) -> None:
    for key in keys:
        try:
            store.delete(key)
        except Exception:
            logger.warning("Could not clean up copied library artifact %s", key, exc_info=True)


@router.post("", response_model=LibraryProjectOut, status_code=201)
def create_project_from_library(
    body: LibraryProjectCreateRequest,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> LibraryProjectOut:
    """Create a project with independent local copies of selected activities."""
    sources = _owned_activities(db, store, auth.user.id, body.activity_ids)
    snapshots = [(_row_snapshot(activity), _row_snapshot(source)) for activity, source in sources]
    artifacts = _validate_artifacts(store, sources)
    project = Project(
        id=uuid.uuid4(),
        owner_user_id=auth.user.id,
        title=body.title.strip(),
        revision=1,
        settings_json={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    stages = _build_stages(project, sources)
    created_keys = _copy_artifacts(store, stages, artifacts)
    try:
        sources = _lock_and_validate_sources(
            db, store, auth.user.id, body.activity_ids, sources, snapshots
        )
        db.add(project)
        db.add_all(stages)
        _attach_recovered_sources(sources)
        project.updated_at = datetime.now(UTC)
        project.revision = 1
        for index, stage in enumerate(stages):
            stage.stage_order = index
            stage.day_index = index + 1
        db.commit()
    except Exception:
        db.rollback()
        _cleanup_artifacts(store, created_keys)
        raise
    return LibraryProjectOut(
        project_id=project.id,
        title=project.title,
        stage_ids=[stage.id for stage in stages],
    )


@router.post("/{project_id}/activities", response_model=LibraryProjectOut)
def add_library_activities_to_project(
    project_id: uuid.UUID,
    body: LibraryProjectReuseRequest,
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> LibraryProjectOut:
    """Append independent local copies of selected activities to a project."""
    sources = _owned_activities(db, store, auth.user.id, body.activity_ids)
    snapshots = [(_row_snapshot(activity), _row_snapshot(source)) for activity, source in sources]
    artifacts = _validate_artifacts(store, sources)
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == auth.user.id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    stages = _build_stages(project, sources)
    created_keys = _copy_artifacts(store, stages, artifacts)
    try:
        sources = _lock_and_validate_sources(
            db, store, auth.user.id, body.activity_ids, sources, snapshots
        )
        project = db.scalar(
            select(Project)
            .where(Project.id == project_id, Project.owner_user_id == auth.user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        max_order = db.scalar(
            select(func.coalesce(func.max(Stage.stage_order), -1)).where(
                Stage.project_id == project.id
            )
        )
        order = int(max_order if max_order is not None else -1) + 1
        for index, stage in enumerate(stages):
            stage.stage_order = order + index
            stage.day_index = order + index + 1
        db.add_all(stages)
        _attach_recovered_sources(sources)
        project.updated_at = datetime.now(UTC)
        project.revision += 1
        db.commit()
    except Exception:
        db.rollback()
        _cleanup_artifacts(store, created_keys)
        raise
    return LibraryProjectOut(
        project_id=project.id,
        title=project.title,
        stage_ids=[stage.id for stage in stages],
    )
