"""Career analytics, multi-trip Atlas heatmap, and stage highlights endpoints."""

from __future__ import annotations

import logging
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated, cast

import duckdb
from fastapi import APIRouter, Query
from sqlalchemy import select
from trip_telemetry import ActivityType, LodLevel

from trip_api.db import Project, Stage
from trip_api.deps import AppSettings, CurrentAuth, DbSession, StoreDep, ensure_utc
from trip_api.gold_sync import ensure_local_gold_duckdb
from trip_api.schemas import (
    ActivityBreakdownOut,
    AnalyticsHeatmapOut,
    AtlasTripOut,
    CareerAnalyticsOut,
    CareerBucketOut,
    HeatmapCellOut,
    ProjectHighlightsOut,
)
from trip_api.services import (
    build_project_geometry_collection,
    build_project_highlights,
    get_owned_project,
    get_project_stages,
)

router = APIRouter(tags=["analytics"])
logger = logging.getLogger(__name__)


def _query_gold_countries_and_stages(
    gold_db_path: Path,
    project_ids: list[str],
    *,
    stage_ids: list[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Query countries visited and known Gold stage IDs scoped strictly to `project_ids`."""
    if not project_ids or (stage_ids is not None and not stage_ids) or not gold_db_path.is_file():
        return set(), set()

    countries: set[str] = set()
    gold_stage_ids: set[str] = set()
    con: duckdb.DuckDBPyConnection | None = None
    try:
        con = duckdb.connect(str(gold_db_path), read_only=True)
        tables = {
            str(r[0])
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        if "mart_stage_geo_context" in tables:
            cols = {
                str(r[0])
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'mart_stage_geo_context'"
                ).fetchall()
            }
            if "trip_id" in cols and "primary_country" in cols:
                if stage_ids is not None and "stage_id" in cols:
                    rows = con.execute(
                        "SELECT DISTINCT primary_country FROM mart_stage_geo_context "
                        "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                        "AND list_contains(CAST(? AS VARCHAR[]), stage_id) "
                        "AND primary_country IS NOT NULL",
                        [project_ids, stage_ids],
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT DISTINCT primary_country FROM mart_stage_geo_context "
                        "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                        "AND primary_country IS NOT NULL",
                        [project_ids],
                    ).fetchall()
                for row in rows:
                    val = str(row[0]).strip()
                    if val:
                        countries.add(val)
            elif "trip_id" in cols and "country_name" in cols:
                if stage_ids is not None and "stage_id" in cols:
                    rows = con.execute(
                        "SELECT DISTINCT country_name FROM mart_stage_geo_context "
                        "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                        "AND list_contains(CAST(? AS VARCHAR[]), stage_id) "
                        "AND country_name IS NOT NULL",
                        [project_ids, stage_ids],
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT DISTINCT country_name FROM mart_stage_geo_context "
                        "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                        "AND country_name IS NOT NULL",
                        [project_ids],
                    ).fetchall()
                for row in rows:
                    val = str(row[0]).strip()
                    if val:
                        countries.add(val)
        if "mart_stage_summary" in tables:
            cols = {
                str(r[0])
                for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'mart_stage_summary'"
                ).fetchall()
            }
            if "stage_id" in cols and "trip_id" in cols:
                if stage_ids is not None:
                    srows = con.execute(
                        "SELECT DISTINCT stage_id FROM mart_stage_summary "
                        "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                        "AND list_contains(CAST(? AS VARCHAR[]), stage_id)",
                        [project_ids, stage_ids],
                    ).fetchall()
                else:
                    srows = con.execute(
                        "SELECT DISTINCT stage_id FROM mart_stage_summary "
                        "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id)",
                        [project_ids],
                    ).fetchall()
                for row in srows:
                    gold_stage_ids.add(str(row[0]))
    except Exception as exc:
        logger.warning("Gold DuckDB career query fallback to Postgres: %s", exc)
    finally:
        if con is not None:
            con.close()
    return countries, gold_stage_ids


def _query_gold_heatmap_cells(
    gold_db_path: Path,
    project_ids: list[str],
    *,
    stage_ids: list[str] | None = None,
    year: int | None = None,
    activity_type: ActivityType | None = None,
) -> list[HeatmapCellOut]:
    """Query `mart_spatial_coverage_h3` scoped strictly to owned `project_ids`."""
    if not project_ids or (stage_ids is not None and not stage_ids) or not gold_db_path.is_file():
        return []

    cells: list[HeatmapCellOut] = []
    con: duckdb.DuckDBPyConnection | None = None
    try:
        con = duckdb.connect(str(gold_db_path), read_only=True)
        tables = {
            str(r[0])
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        if "mart_spatial_coverage_h3" not in tables:
            return []

        cols = {
            str(r[0])
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'mart_spatial_coverage_h3'"
            ).fetchall()
        }
        if "trip_id" not in cols or "h3_index" not in cols:
            return []

        has_center_lat = "center_lat" in cols
        has_center_lon = "center_lon" in cols
        has_lat = "lat" in cols
        has_lon = "lon" in cols
        if not ((has_center_lat or has_lat) and (has_center_lon or has_lon)):
            return []

        if has_center_lat and has_center_lon and "stage_id" in cols and "point_count" in cols:
            if stage_ids is not None and "year" in cols:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, "
                    "COUNT(DISTINCT stage_id) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "AND list_contains(CAST(? AS VARCHAR[]), stage_id) "
                    "AND (CAST(? AS INTEGER) IS NULL OR year = CAST(? AS INTEGER)) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids, stage_ids, year, year],
                ).fetchall()
            elif stage_ids is not None:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, "
                    "COUNT(DISTINCT stage_id) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "AND list_contains(CAST(? AS VARCHAR[]), stage_id) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids, stage_ids],
                ).fetchall()
            elif "year" in cols and "activity_type" in cols:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, "
                    "COUNT(DISTINCT stage_id) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "AND (CAST(? AS INTEGER) IS NULL OR year = CAST(? AS INTEGER)) "
                    "AND (CAST(? AS VARCHAR) IS NULL OR activity_type = CAST(? AS VARCHAR)) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids, year, year, activity_type, activity_type],
                ).fetchall()
            elif "year" in cols:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, "
                    "COUNT(DISTINCT stage_id) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "AND (CAST(? AS INTEGER) IS NULL OR year = CAST(? AS INTEGER)) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids, year, year],
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, "
                    "COUNT(DISTINCT stage_id) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids],
                ).fetchall()
        elif has_center_lat and has_center_lon and "point_count" in cols and "stage_count" in cols:
            if "year" in cols and "activity_type" in cols:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, SUM(stage_count) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "AND (CAST(? AS INTEGER) IS NULL OR year = CAST(? AS INTEGER)) "
                    "AND (CAST(? AS VARCHAR) IS NULL OR activity_type = CAST(? AS VARCHAR)) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids, year, year, activity_type, activity_type],
                ).fetchall()
            elif "year" in cols:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, SUM(stage_count) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "AND (CAST(? AS INTEGER) IS NULL OR year = CAST(? AS INTEGER)) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids, year, year],
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT h3_index, "
                    "COALESCE("
                    "ROUND(SUM(center_lat * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lat)"
                    ") AS lat, "
                    "COALESCE("
                    "ROUND(SUM(center_lon * point_count) / NULLIF(SUM(point_count), 0), 5), "
                    "AVG(center_lon)"
                    ") AS lon, "
                    "SUM(point_count) AS point_count, SUM(stage_count) AS stage_count "
                    "FROM mart_spatial_coverage_h3 "
                    "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                    "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                    [project_ids],
                ).fetchall()
        elif "point_count" in cols and has_lat and has_lon:
            rows = con.execute(
                "SELECT h3_index, AVG(lat) AS lat, AVG(lon) AS lon, "
                "SUM(point_count) AS point_count, COUNT(DISTINCT trip_id) AS stage_count "
                "FROM mart_spatial_coverage_h3 "
                "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                [project_ids],
            ).fetchall()
        elif has_lat and has_lon:
            rows = con.execute(
                "SELECT h3_index, AVG(lat) AS lat, AVG(lon) AS lon, "
                "COUNT(*) AS point_count, COUNT(DISTINCT trip_id) AS stage_count "
                "FROM mart_spatial_coverage_h3 "
                "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                [project_ids],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT h3_index, AVG(center_lat) AS lat, AVG(center_lon) AS lon, "
                "COUNT(*) AS point_count, COUNT(DISTINCT trip_id) AS stage_count "
                "FROM mart_spatial_coverage_h3 "
                "WHERE list_contains(CAST(? AS VARCHAR[]), trip_id) "
                "GROUP BY h3_index ORDER BY point_count DESC LIMIT 5000",
                [project_ids],
            ).fetchall()

        for row in rows:
            cells.append(
                HeatmapCellOut(
                    h3_index=str(row[0]),
                    lat=float(row[1] or 0.0),
                    lon=float(row[2] or 0.0),
                    point_count=int(row[3] or 0),
                    stage_count=int(row[4] or 0),
                )
            )
    except Exception as exc:
        logger.warning("Gold DuckDB heatmap query skipped: %s", exc)
    finally:
        if con is not None:
            con.close()
    return cells


@router.get(
    "/api/v1/projects/{project_id}/analytics/highlights",
    response_model=ProjectHighlightsOut,
)
def get_project_analytics_highlights(
    project_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    store: StoreDep,
) -> ProjectHighlightsOut:
    project = get_owned_project(db, project_id, auth.user.id)
    stages = get_project_stages(db, project.id)
    return build_project_highlights(store, project.id, stages)


@router.get("/api/v1/analytics/career", response_model=CareerAnalyticsOut)
def get_career_analytics(
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    year: Annotated[int | None, Query(ge=1970, le=2100)] = None,
    activity_type: ActivityType | None = None,
) -> CareerAnalyticsOut:
    projects = list(
        db.scalars(select(Project).where(Project.owner_user_id == auth.user.id)).all()
    )
    if not projects:
        return CareerAnalyticsOut(
            total_projects=0,
            total_stages=0,
            total_distance_m=0.0,
            total_elevation_gain_m=0.0,
            total_moving_seconds=0.0,
        )

    project_by_id = {p.id: p for p in projects}
    owned_ids_str = [str(p.id) for p in projects]
    stages = list(
        db.scalars(
            select(Stage).where(Stage.project_id.in_(list(project_by_id.keys())))
        ).all()
    )

    matched_project_ids: set[uuid.UUID] = set()
    matched_stage_ids: list[str] = []
    by_year_projects: dict[int, set[uuid.UUID]] = defaultdict(set)
    by_year_stats: dict[int, dict[str, float]] = defaultdict(
        lambda: {"stages": 0, "dist": 0.0, "elev": 0.0, "moving": 0.0}
    )
    by_act_stats: dict[str, dict[str, float]] = defaultdict(
        lambda: {"stages": 0, "dist": 0.0, "elev": 0.0, "moving": 0.0}
    )

    total_stages = 0
    total_distance_m = 0.0
    total_elevation_gain_m = 0.0
    total_moving_seconds = 0.0

    for st in stages:
        proj = project_by_id[st.project_id]
        ref_dt = st.started_at or proj.created_at
        st_year = ensure_utc(ref_dt).year
        if year is not None and st_year != year:
            continue
        if activity_type is not None and st.activity_type != activity_type:
            continue

        matched_project_ids.add(st.project_id)
        matched_stage_ids.append(str(st.id))
        total_stages += 1
        dist = float(st.distance_m or 0.0)
        elev = float(st.elevation_gain_m or 0.0)
        mov = float(st.moving_seconds or 0.0)

        total_distance_m += dist
        total_elevation_gain_m += elev
        total_moving_seconds += mov

        by_year_projects[st_year].add(st.project_id)
        yb = by_year_stats[st_year]
        yb["stages"] += 1
        yb["dist"] += dist
        yb["elev"] += elev
        yb["moving"] += mov

        ab = by_act_stats[st.activity_type]
        ab["stages"] += 1
        ab["dist"] += dist
        ab["elev"] += elev
        ab["moving"] += mov

    gold_db_path = ensure_local_gold_duckdb(store, settings)
    countries_visited, _ = _query_gold_countries_and_stages(
        gold_db_path,
        owned_ids_str,
        stage_ids=matched_stage_ids if (year is not None or activity_type is not None) else None,
    )

    if year is None and activity_type is None:
        total_projects = len(projects)
    else:
        total_projects = len(matched_project_ids)

    by_year = [
        CareerBucketOut(
            year=y,
            project_count=len(by_year_projects[y]),
            stage_count=int(by_year_stats[y]["stages"]),
            distance_m=round(by_year_stats[y]["dist"], 2),
            elevation_gain_m=round(by_year_stats[y]["elev"], 2),
            moving_seconds=round(by_year_stats[y]["moving"], 2),
        )
        for y in sorted(by_year_stats.keys(), reverse=True)
    ]

    by_activity = [
        ActivityBreakdownOut(
            activity_type=act,
            stage_count=int(vals["stages"]),
            distance_m=round(vals["dist"], 2),
            elevation_gain_m=round(vals["elev"], 2),
            moving_seconds=round(vals["moving"], 2),
        )
        for act, vals in sorted(
            by_act_stats.items(),
            key=lambda kv: kv[1]["dist"],
            reverse=True,
        )
    ]

    return CareerAnalyticsOut(
        total_projects=total_projects,
        total_stages=total_stages,
        total_distance_m=round(total_distance_m, 2),
        total_elevation_gain_m=round(total_elevation_gain_m, 2),
        total_moving_seconds=round(total_moving_seconds, 2),
        countries_visited=sorted(countries_visited),
        by_year=by_year,
        by_activity=by_activity,
    )


@router.get("/api/v1/analytics/heatmap", response_model=AnalyticsHeatmapOut)
def get_analytics_heatmap(
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
    year: Annotated[int | None, Query(ge=1970, le=2100)] = None,
    activity_type: ActivityType | None = None,
    lod: Annotated[LodLevel, Query()] = "low",
) -> AnalyticsHeatmapOut:
    projects = list(
        db.scalars(
            select(Project)
            .where(Project.owner_user_id == auth.user.id)
            .order_by(Project.updated_at.desc())
        ).all()
    )
    if not projects:
        return AnalyticsHeatmapOut()

    owned_ids_str = [str(p.id) for p in projects]
    matched_stage_ids: list[str] = []
    trips: list[AtlasTripOut] = []

    for proj in projects:
        stages = get_project_stages(db, proj.id)
        filtered_stages = []
        for st in stages:
            st_year = ensure_utc(st.started_at or proj.created_at).year
            if year is not None and st_year != year:
                continue
            if activity_type is not None and st.activity_type != activity_type:
                continue
            filtered_stages.append(st)
            matched_stage_ids.append(str(st.id))

        if not filtered_stages:
            if year is None and activity_type is None:
                trips.append(
                    AtlasTripOut(
                        project_id=proj.id,
                        title=proj.title,
                        year=ensure_utc(proj.created_at).year,
                        primary_activity="cycling",
                        distance_m=0.0,
                        elevation_gain_m=0.0,
                        stage_count=0,
                        bbox=None,
                        polylines=[],
                    )
                )
            continue

        geom = build_project_geometry_collection(store, proj.id, filtered_stages, lod)
        polylines: list[list[list[float]]] = []
        min_lons: list[float] = []
        min_lats: list[float] = []
        max_lons: list[float] = []
        max_lats: list[float] = []

        for feat in geom.features:
            geometry = feat.get("geometry")
            if isinstance(geometry, dict):
                coords = geometry.get("coordinates")
                if geometry.get("type") == "MultiLineString" and isinstance(coords, list):
                    for line in coords:
                        if isinstance(line, list):
                            polylines.append(cast(list[list[float]], line))
                elif geometry.get("type") == "LineString" and isinstance(coords, list):
                    polylines.append(cast(list[list[float]], coords))
            props = feat.get("properties")
            if isinstance(props, dict):
                fb = props.get("bbox")
                if isinstance(fb, list) and len(fb) == 4:
                    min_lons.append(float(fb[0]))
                    min_lats.append(float(fb[1]))
                    max_lons.append(float(fb[2]))
                    max_lats.append(float(fb[3]))

        bbox = (
            [min(min_lons), min(min_lats), max(max_lons), max(max_lats)]
            if min_lons
            else None
        )

        act_counts: Counter[str] = Counter()
        for st in filtered_stages:
            act_counts[st.activity_type] += 1
        primary_act = act_counts.most_common(1)[0][0]
        first_dt = next((st.started_at for st in filtered_stages if st.started_at), proj.created_at)

        trips.append(
            AtlasTripOut(
                project_id=proj.id,
                title=proj.title,
                year=ensure_utc(first_dt).year,
                primary_activity=primary_act,
                distance_m=round(sum(float(st.distance_m or 0.0) for st in filtered_stages), 2),
                elevation_gain_m=round(
                    sum(float(st.elevation_gain_m or 0.0) for st in filtered_stages), 2
                ),
                stage_count=len(filtered_stages),
                bbox=bbox,
                polylines=polylines,
            )
        )

    gold_db_path = ensure_local_gold_duckdb(store, settings)
    hex_cells = _query_gold_heatmap_cells(
        gold_db_path,
        owned_ids_str,
        stage_ids=matched_stage_ids,
        year=year,
        activity_type=activity_type,
    )
    return AnalyticsHeatmapOut(trips=trips, hex_cells=hex_cells)
