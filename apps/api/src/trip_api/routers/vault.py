"""Activity Vault Strava sync, live status, and Stage backfill endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from trip_api.db import Project, Stage, VaultActivity
from trip_api.deps import AppSettings, CsrfAuth, CurrentAuth, DbSession, StoreDep
from trip_api.library_geometry import (
    MAX_EVALUATION_CHARS,
    MAX_RESPONSE_POINTS,
    canonical_points,
    intersects_area,
    map_coordinates,
    overview_points,
    parse_area,
)
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    VaultActivityDetailOut,
    VaultActivityListItemOut,
    VaultActivityListOut,
    VaultActivityProjectUsageOut,
    VaultActivityReadinessOut,
    VaultActivitySummaryOut,
    VaultBackfillStagesOut,
    VaultSyncStatusOut,
    VaultSyncStravaRequest,
)
from trip_api.services import write_audit_event
from trip_api.vault_sync import (
    backfill_user_stages_to_vault,
    build_vault_sync_status,
    start_or_resume_strava_vault_sync,
)

router = APIRouter(prefix="/api/v1/vault", tags=["vault"])


_METRIC_KEYS = {
    "distance_m": "distance",
    "elevation_gain_m": "total_elevation_gain",
    "moving_seconds": "moving_time",
    "elapsed_seconds": "elapsed_time",
}


def _metric_expression(field: str) -> ColumnElement[float]:
    return cast(
        ColumnElement[float],
        VaultActivity.raw_metadata_json[_METRIC_KEYS[field]].as_float(),
    )


def _stored_metric(activity: VaultActivity, field: str) -> float | None:
    value = activity.raw_metadata_json.get(_METRIC_KEYS[field])
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _activity_item(activity: VaultActivity) -> VaultActivityListItemOut:
    return VaultActivityListItemOut(
        id=activity.id,
        name=activity.name,
        activity_type=activity.activity_type,
        raw_sport_type=activity.raw_sport_type,
        source_provider=activity.source_provider,
        started_at=activity.started_at,
        local_date=activity.local_date,
        timezone_name=activity.timezone_name,
        date_is_utc_fallback=activity.started_at_local is None,
        distance_m=_stored_metric(activity, "distance_m"),
        elevation_gain_m=_stored_metric(activity, "elevation_gain_m"),
        moving_seconds=_stored_metric(activity, "moving_seconds"),
        elapsed_seconds=_stored_metric(activity, "elapsed_seconds"),
        has_gps=activity.has_gps,
        readiness=VaultActivityReadinessOut(
            metadata_available=bool(activity.raw_metadata_json or activity.bronze_object_key),
            has_gps=activity.has_gps,
            track_ready=activity.tile_precision != "none" or bool(activity.summary_polyline),
            tile_precision=cast(
                Literal["none", "summary_polyline", "full_stream"], activity.tile_precision
            ),
        ),
    )


def _selection_query(
    user_id: UUID,
    *,
    name: str | None,
    start_date: date | None,
    end_date: date | None,
    sport: str | None,
    min_distance_m: float | None,
    max_distance_m: float | None,
    min_elevation_gain_m: float | None,
    max_elevation_gain_m: float | None,
    min_moving_seconds: float | None,
    max_moving_seconds: float | None,
    has_gps: bool | None,
) -> Select[VaultActivity]:
    query = select(VaultActivity).where(VaultActivity.user_id == user_id)
    if name is not None:
        escaped = name.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        query = query.where(VaultActivity.name.ilike(f"%{escaped}%", escape="!"))
    if start_date is not None:
        query = query.where(VaultActivity.local_date >= start_date)
    if end_date is not None:
        query = query.where(VaultActivity.local_date <= end_date)
    if sport is not None:
        query = query.where(VaultActivity.activity_type == sport)
    if min_distance_m is not None:
        query = query.where(_metric_expression("distance_m") >= min_distance_m)
    if max_distance_m is not None:
        query = query.where(_metric_expression("distance_m") <= max_distance_m)
    if min_elevation_gain_m is not None:
        query = query.where(_metric_expression("elevation_gain_m") >= min_elevation_gain_m)
    if max_elevation_gain_m is not None:
        query = query.where(_metric_expression("elevation_gain_m") <= max_elevation_gain_m)
    if min_moving_seconds is not None:
        query = query.where(_metric_expression("moving_seconds") >= min_moving_seconds)
    if max_moving_seconds is not None:
        query = query.where(_metric_expression("moving_seconds") <= max_moving_seconds)
    if has_gps is not None:
        query = query.where(VaultActivity.has_gps.is_(has_gps))
    return query


def _summary(db: Session, query: Select[VaultActivity]) -> VaultActivitySummaryOut:
    activity = VaultActivity
    distance = _metric_expression("distance_m")
    elevation = _metric_expression("elevation_gain_m")
    moving = _metric_expression("moving_seconds")
    elapsed = _metric_expression("elapsed_seconds")
    row = db.execute(
        select(
            func.count(activity.id),
            func.sum(distance),
            func.count(distance),
            func.sum(elevation),
            func.count(elevation),
            func.sum(moving),
            func.count(moving),
            func.sum(elapsed),
            func.count(elapsed),
            func.sum(case((activity.has_gps.is_(True), 1), else_=0)),
        ).where(activity.id.in_(query.with_only_columns(activity.id)))
    ).one()
    count = int(row[0] or 0)
    gps_count = int(row[9] or 0)
    return VaultActivitySummaryOut(
        activity_count=count,
        distance_m=float(row[1]) if row[2] else None,
        distance_known_count=int(row[2] or 0),
        elevation_gain_m=float(row[3]) if row[4] else None,
        elevation_gain_known_count=int(row[4] or 0),
        moving_seconds=float(row[5]) if row[6] else None,
        moving_seconds_known_count=int(row[6] or 0),
        elapsed_seconds=float(row[7]) if row[8] else None,
        elapsed_seconds_known_count=int(row[8] or 0),
        gps_activity_count=gps_count,
        no_gps_activity_count=count - gps_count,
    )


@dataclass
class LibrarySelection:
    query: Select[VaultActivity]
    geographic_excluded_count: int = 0


def library_selection(
    auth: CurrentAuth,
    db: DbSession,
    name: str | None = Query(default=None, max_length=200),
    start_date: date | None = None,
    end_date: date | None = None,
    sport: str | None = Query(default=None, max_length=32),
    min_distance_m: float | None = Query(default=None, ge=0),
    max_distance_m: float | None = Query(default=None, ge=0),
    min_elevation_gain_m: float | None = Query(default=None, ge=0),
    max_elevation_gain_m: float | None = Query(default=None, ge=0),
    min_moving_seconds: float | None = Query(default=None, ge=0),
    max_moving_seconds: float | None = Query(default=None, ge=0),
    has_gps: bool | None = None,
    area: str | None = Query(default=None, max_length=160),
) -> LibrarySelection:
    if start_date is not None and end_date is not None and start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
    for minimum, maximum, label in (
        (min_distance_m, max_distance_m, "distance"),
        (min_elevation_gain_m, max_elevation_gain_m, "elevation gain"),
        (min_moving_seconds, max_moving_seconds, "moving time"),
    ):
        if minimum is not None and maximum is not None and minimum > maximum:
            raise HTTPException(status_code=422, detail=f"minimum {label} exceeds maximum")

    filters = _selection_query(
        auth.user.id,
        name=name,
        start_date=start_date,
        end_date=end_date,
        sport=sport,
        min_distance_m=min_distance_m,
        max_distance_m=max_distance_m,
        min_elevation_gain_m=min_elevation_gain_m,
        max_elevation_gain_m=max_elevation_gain_m,
        min_moving_seconds=min_moving_seconds,
        max_moving_seconds=max_moving_seconds,
        has_gps=has_gps,
    )
    excluded = 0
    if area is not None:
        try:
            bounds = parse_area(area)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # A hard, explicit evaluation limit prevents unbounded request-time work.
        count = int(db.scalar(select(func.count()).select_from(filters.subquery())) or 0)
        if count > 50_000:
            raise HTTPException(
                status_code=422,
                detail="Narrow the selection to 50000 activities before filtering an area",
            )
        matched = []
        geometry_query = filters.with_only_columns(
            VaultActivity.id, VaultActivity.has_gps, VaultActivity.summary_polyline
        ).execution_options(yield_per=100)
        evaluated_chars = 0
        for activity_id, gps, encoded in db.execute(geometry_query):
            evaluated_chars += len(encoded or "")
            if evaluated_chars > MAX_EVALUATION_CHARS:
                raise HTTPException(
                    status_code=422,
                    detail="Narrow the selection: geographic geometry evaluation limit reached",
                )
            points = canonical_points(gps, encoded)
            if points is None:
                excluded += 1
            elif intersects_area(points, bounds):
                matched.append(activity_id)
        filters = filters.where(VaultActivity.id.in_(matched))
    return LibrarySelection(filters, excluded)


Selection = Annotated[LibrarySelection, Depends(library_selection)]


@router.get("/activities", response_model=VaultActivityListOut)
def list_vault_activities(
    db: DbSession,
    selection: Selection,
    sort: str = Query(
        default="started_at",
        pattern="^(started_at|local_date|name|distance_m|elevation_gain_m|moving_seconds)$",
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> VaultActivityListOut:
    filters = selection.query
    sort_column = {
        "started_at": VaultActivity.started_at,
        "local_date": VaultActivity.local_date,
        "name": VaultActivity.name,
        "distance_m": _metric_expression("distance_m"),
        "elevation_gain_m": _metric_expression("elevation_gain_m"),
        "moving_seconds": _metric_expression("moving_seconds"),
    }[sort]
    ordering = sort_column.asc() if order == "asc" else sort_column.desc()
    page_query = filters.order_by(ordering, VaultActivity.id.asc()).offset(offset).limit(limit)
    activities = list(db.scalars(page_query).all())
    total = int(db.scalar(select(func.count()).select_from(filters.subquery())) or 0)
    return VaultActivityListOut(
        items=[_activity_item(item) for item in activities],
        total=total,
        offset=offset,
        limit=limit,
        summary=_summary(db, filters),
        geographic_excluded_count=selection.geographic_excluded_count,
    )


@router.get("/activities/map")
def map_vault_activities(
    db: DbSession,
    selection: Selection,
    max_features: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0, le=50_000),
) -> dict[str, object]:
    features: list[dict[str, object]] = []
    base = selection.query.subquery()
    total, no_gps, unavailable = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(case((base.c.has_gps.is_(False), 1), else_=0)), 0),
            func.coalesce(
                func.sum(
                    case(
                        (
                            base.c.has_gps.is_(True)
                            & (base.c.summary_polyline.is_(None) | (base.c.summary_polyline == "")),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        ).select_from(base)
    ).one()
    candidate_count = int(total) - int(no_gps) - int(unavailable)
    overview_limit = (
        max(2, min(200, MAX_RESPONSE_POINTS // candidate_count)) if candidate_count else 2
    )
    drawable = point_count = evaluated_chars = consumed = unavailable_in_page = 0
    candidate_limit = min(max_features, max(0, 50_000 - offset))
    query = selection.query.with_only_columns(
        VaultActivity.id,
        VaultActivity.name,
        VaultActivity.has_gps,
        VaultActivity.summary_polyline,
    )
    query = (
        query.where(
            VaultActivity.has_gps.is_(True),
            VaultActivity.summary_polyline.is_not(None),
            VaultActivity.summary_polyline != "",
        )
        .order_by(VaultActivity.started_at.desc(), VaultActivity.id.asc())
        .offset(offset)
        .limit(candidate_limit)
    )
    for activity_id, name, gps, encoded in db.execute(query.execution_options(yield_per=100)):
        encoded_length = len(encoded or "")
        # Always make progress on the first row; its encoded size is independently
        # bounded by MAX_ENCODED_LENGTH. Later rows remain at the next cursor.
        if consumed and evaluated_chars + encoded_length > MAX_EVALUATION_CHARS:
            break
        evaluated_chars += encoded_length
        consumed += 1
        points = canonical_points(gps, encoded)
        if points is None:
            unavailable += 1
            unavailable_in_page += 1
            continue
        drawable += 1
        sampled = overview_points(points, overview_limit)
        point_count += len(sampled)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": map_coordinates(sampled),
                },
                "properties": {
                    "id": str(activity_id),
                    "name": name,
                    "precision": "summary_polyline",
                },
            }
        )
    consumed_offset = offset + consumed
    unevaluated = max(0, candidate_count - consumed_offset)
    next_offset = consumed_offset if consumed_offset < min(candidate_count, 50_000) else None
    return {
        "type": "FeatureCollection",
        "features": features,
        "total": total,
        "drawable_count": drawable,
        "no_gps_count": no_gps,
        "unavailable_count": unavailable,
        "returned_count": len(features),
        "truncated": unevaluated > 0 or len(features) < drawable,
        "unevaluated_count": unevaluated,
        "next_offset": next_offset,
        "unavailable_in_page_count": unavailable_in_page,
        "geographic_excluded_count": selection.geographic_excluded_count,
    }


@router.get("/activities/{activity_id}", response_model=VaultActivityDetailOut)
def get_vault_activity(
    activity_id: UUID,
    auth: CurrentAuth,
    db: DbSession,
) -> VaultActivityDetailOut:
    activity = db.scalar(
        select(VaultActivity).where(
            VaultActivity.id == activity_id,
            VaultActivity.user_id == auth.user.id,
        )
    )
    if activity is None:
        raise HTTPException(status_code=404, detail="Activity not found")

    usage_rows = db.execute(
        select(Project.id, Project.title, Stage.id, Stage.name)
        .join(Stage, Stage.project_id == Project.id)
        .where(
            Stage.vault_activity_id == activity.id,
            Project.owner_user_id == auth.user.id,
        )
        .order_by(Project.title, Project.id, Stage.stage_order, Stage.id)
    ).all()
    source_url = (
        f"https://www.strava.com/activities/{activity.strava_activity_id}"
        if activity.source_provider == "strava" and activity.strava_activity_id is not None
        else None
    )
    return VaultActivityDetailOut(
        **_activity_item(activity).model_dump(),
        strava_activity_id=activity.strava_activity_id,
        started_at_local=activity.started_at_local,
        utc_offset_s=activity.utc_offset_s,
        elevation_loss_m=activity.elevation_loss_m,
        elev_high_m=activity.elev_high_m,
        elev_low_m=activity.elev_low_m,
        avg_speed_mps=activity.avg_speed_mps,
        max_speed_mps=activity.max_speed_mps,
        avg_hr_bpm=activity.avg_hr_bpm,
        max_hr_bpm=activity.max_hr_bpm,
        avg_watts=activity.avg_watts,
        weighted_watts=activity.weighted_watts,
        kilojoules=activity.kilojoules,
        avg_cadence=activity.avg_cadence,
        suffer_score=activity.suffer_score,
        gear_id=activity.gear_id,
        gear_name=activity.gear_name,
        is_commute=activity.is_commute,
        is_indoor_virtual=activity.is_indoor_virtual,
        is_private=activity.is_private,
        start_lat=activity.start_lat,
        start_lon=activity.start_lon,
        end_lat=activity.end_lat,
        end_lon=activity.end_lon,
        min_lat=activity.min_lat,
        max_lat=activity.max_lat,
        min_lon=activity.min_lon,
        max_lon=activity.max_lon,
        summary_polyline=activity.summary_polyline,
        tile_count_z14=activity.tile_count_z14,
        new_tiles_z14_count=activity.new_tiles_z14_count,
        primary_country=activity.primary_country,
        bronze_object_key=activity.bronze_object_key,
        source_url=source_url,
        raw_metadata=activity.raw_metadata_json,
        project_usages=[
            VaultActivityProjectUsageOut(
                project_id=row[0],
                project_title=row[1],
                stage_id=row[2],
                stage_name=row[3],
            )
            for row in usage_rows
        ],
    )


@router.post("/sync/strava", response_model=VaultSyncStatusOut)
def sync_strava_vault(
    response: Response,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    body: VaultSyncStravaRequest | None = None,
) -> VaultSyncStatusOut:
    req = body or VaultSyncStravaRequest()
    check_rate_limit(
        db,
        bucket_key=f"vault_sync:{auth.user.id}",
        limit=10,
        window_seconds=60,
    )

    try:
        start_or_resume_strava_vault_sync(
            db,
            settings,
            user_id=auth.user.id,
            mode=req.mode,
            after=req.after,
            before=req.before,
            inline_first_page=req.inline_first_page,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status_out = build_vault_sync_status(db, settings, user_id=auth.user.id)
    write_audit_event(
        db,
        user_id=auth.user.id,
        event_type="vault.strava_sync_triggered",
        metadata={
            "mode": status_out.mode,
            "status": status_out.status,
            "phase": status_out.phase,
            "imported_count": status_out.imported_count,
            "processed_activities": status_out.processed_activities,
        },
    )
    db.commit()

    response.status_code = (
        202 if status_out.status in {"queued", "running", "rate_limited"} else 200
    )
    return status_out


@router.get("/sync/status", response_model=VaultSyncStatusOut)
def get_vault_sync_status(
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
) -> VaultSyncStatusOut:
    return build_vault_sync_status(db, settings, user_id=auth.user.id)


@router.post("/backfill-stages", response_model=VaultBackfillStagesOut)
def backfill_stages_to_vault(
    auth: CsrfAuth,
    db: DbSession,
    store: StoreDep,
) -> VaultBackfillStagesOut:
    check_rate_limit(
        db,
        bucket_key=f"vault_backfill:{auth.user.id}",
        limit=5,
        window_seconds=60,
    )
    result = backfill_user_stages_to_vault(db, store, auth.user.id)
    write_audit_event(
        db,
        user_id=auth.user.id,
        event_type="vault.stages_backfilled",
        metadata=dict(result),
    )
    db.commit()
    return VaultBackfillStagesOut(**result)
