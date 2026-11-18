"""DuckDB-backed telemetry chart, cached LOD GeoJSON geometry, and KPI endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from trip_telemetry import LodLevel, query_stage_kpis

from trip_api.db import Stage
from trip_api.deps import CurrentAuth, DbSession, StoreDep
from trip_api.schemas import TelemetryChartOut, TelemetryGeometryOut
from trip_api.services import (
    build_project_geometry_collection,
    build_project_telemetry_chart,
    build_stage_geometry_feature,
    get_owned_project,
    get_project_stages,
    materialize_silver_parquets,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["telemetry"])


def _select_stages(
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


@router.get("/telemetry/chart", response_model=TelemetryChartOut)
def get_telemetry_chart(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
    stage_id: uuid.UUID | None = None,
    max_points: Annotated[int, Query(ge=2, le=5000)] = 1200,
) -> TelemetryChartOut:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = _select_stages(db, project.id, stage_id)
    try:
        return build_project_telemetry_chart(store, project.id, stages, max_points)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/telemetry/geometry", response_model=TelemetryGeometryOut)
def get_telemetry_geometry(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
    stage_id: uuid.UUID | None = None,
    lod: Annotated[LodLevel, Query()] = "medium",
) -> TelemetryGeometryOut:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = _select_stages(db, project.id, stage_id)
    try:
        return build_project_geometry_collection(store, project.id, stages, lod)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/stages/{stage_id}/geometry")
def get_stage_geometry(
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
    lod: Annotated[LodLevel, Query()] = "medium",
) -> dict[str, object]:
    project = get_owned_project(db, project_id, auth.user.id)
    stage = db.scalar(select(Stage).where(Stage.id == stage_id, Stage.project_id == project.id))
    if stage is None:
        raise HTTPException(status_code=404, detail="Stage not found")
    try:
        return build_stage_geometry_feature(store, stage, lod)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/kpis")
def get_project_kpis(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
) -> dict[str, object]:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    stage_kpis: list[dict[str, object]] = []
    total_distance_m = 0.0
    total_elevation_gain_m = 0.0
    total_moving_seconds = 0.0

    with materialize_silver_parquets(store, stages) as pairs:
        for stage, parquet_path in pairs:
            try:
                kpis = query_stage_kpis(
                    parquet_path,
                    trim_start_s=stage.trim_start_s,
                    trim_end_s=stage.trim_end_s,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            total_distance_m += float(kpis["total_distance_m"])
            total_elevation_gain_m += float(kpis["elevation_gain_m"])
            total_moving_seconds += float(kpis["moving_time_s"])
            stage_kpis.append(
                {
                    "stage_id": str(stage.id),
                    "name": stage.name,
                    "day_index": stage.day_index,
                    **kpis,
                }
            )
    return {
        "project_id": str(project.id),
        "stage_count": len(stages),
        "total_distance_m": round(total_distance_m, 2),
        "total_elevation_gain_m": round(total_elevation_gain_m, 2),
        "total_moving_seconds": round(total_moving_seconds, 2),
        "stages": stage_kpis,
    }
