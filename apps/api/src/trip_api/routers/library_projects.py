"""Create projects from private Activity Library material."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trip_api.db import Project, Stage, VaultActivity
from trip_api.deps import CsrfAuth, DbSession, StoreDep

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
        if activity.source_stage_id is None:
            source = _find_unlinked_original(db, store, user_id, activity)
        else:
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
    for stage in candidates:
        if not _is_original_provider_stage(stage, activity):
            continue
        project = db.get(Project, stage.project_id)
        if project is None or project.owner_user_id != owner_id:
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
        if activity.source_stage_id is None:
            source.vault_activity_id = activity.id
            activity.source_stage_id = source.id


def _lock_and_validate_sources(
    db: Session,
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
    for (prepared_activity, prepared_source), (activity_snapshot, source_snapshot) in zip(
        prepared, snapshots, strict=True
    ):
        activity = by_id[prepared_activity.id]
        if _row_snapshot(activity) != activity_snapshot:
            raise HTTPException(status_code=409, detail="Library activity changed during reuse")
        if activity_snapshot["source_stage_id"] is None:
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
    for _, source in sources:
        try:
            artifacts.append(
                (
                    store.get_bytes(source.bronze_object_key),
                    store.get_bytes(source.silver_object_key),
                )
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
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
            db, auth.user.id, body.activity_ids, sources, snapshots
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
            db, auth.user.id, body.activity_ids, sources, snapshots
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
