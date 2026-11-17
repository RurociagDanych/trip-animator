"""Event-driven Strava webhook queue worker and deterministic Bronze -> Silver replay."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from trip_api.config import Settings
from trip_api.db import IngestJob, OAuthConnection, Project, Stage, User
from trip_api.deps import ensure_utc
from trip_api.services import (
    align_project_photos,
    build_stage_chart_cache_bytes,
    ingest_parsed_track,
    persist_or_delete_stage_chart_cache,
    persist_or_delete_stage_lod_cache,
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    write_audit_event,
)
from trip_api.storage import ObjectStore
from trip_telemetry import (
    CURRENT_ETL_VERSION,
    build_lod_cache_payload,
    build_silver_dataframe,
    build_stage_events_parquet,
    infer_activity_type_from_strava,
    parse_strava_streams,
    parse_track_bytes,
    write_silver_parquet,
)

from trip_airflow.strava_sync import (
    StravaUpstreamError,
    _coerce_utc_datetime,
    _default_sync_http_client,
    _refresh_access_token_if_needed,
    _resolve_or_create_autosync_project,
    _strava_sync_request,
)

logger = logging.getLogger(__name__)
_LEASE_SECONDS = 120


def _is_rate_limited(headers: Mapping[str, str]) -> bool:
    """Inspect Strava X-RateLimit-Usage / X-RateLimit-Limit headers for backpressure."""
    usage_raw = headers.get("x-ratelimit-usage") or headers.get("X-RateLimit-Usage")
    limit_raw = headers.get("x-ratelimit-limit") or headers.get("X-RateLimit-Limit")
    if not usage_raw or not limit_raw:
        return False
    try:
        u_short, u_daily = (int(x.strip()) for x in usage_raw.split(",", 1))
        l_short, l_daily = (int(x.strip()) for x in limit_raw.split(",", 1))
        return (l_short > 0 and u_short >= int(l_short * 0.9)) or (
            l_daily > 0 and u_daily >= int(l_daily * 0.95)
        )
    except (ValueError, TypeError):
        return False


def cluster_activity_day_index(
    db: Session,
    project_id: uuid.UUID,
    started_at: datetime | None,
) -> int:
    """Assign a multi-day `day_index` (1..365) by clustering stage start calendar dates."""
    if started_at is None:
        return 1
    target_date = ensure_utc(started_at).date()
    existing = list(
        db.scalars(
            select(Stage)
            .where(Stage.project_id == project_id, Stage.started_at.is_not(None))
            .order_by(Stage.started_at.asc())
        ).all()
    )
    if not existing:
        return 1

    date_to_day: dict[date, int] = {}
    for st in existing:
        if st.started_at is not None:
            d = ensure_utc(st.started_at).date()
            date_to_day.setdefault(d, st.day_index)

    if target_date in date_to_day:
        return int(max(1, min(365, date_to_day[target_date])))

    first_date = min(date_to_day.keys())
    delta_days = int((target_date - first_date).days)
    if delta_days >= 0:
        return int(max(1, min(365, delta_days + 1)))
    return 1


def _claim_ingest_jobs(db: Session, *, batch_size: int, now: datetime) -> list[IngestJob]:
    lease_until = now + timedelta(seconds=_LEASE_SECONDS)
    stmt = (
        select(IngestJob)
        .where(
            (IngestJob.status == "queued")
            | ((IngestJob.status == "running") & (IngestJob.lease_expires_at <= now))
        )
        .order_by(IngestJob.created_at.asc())
        .limit(batch_size)
    )
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    jobs = list(db.scalars(stmt).all())
    for job in jobs:
        job.status = "running"
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.lease_expires_at = lease_until
    if jobs:
        db.commit()
    return jobs


def _delete_stage_and_artifacts(db: Session, store: ObjectStore, stage: Stage) -> None:
    keys = [
        stage.bronze_object_key,
        stage.silver_object_key,
        stage_lod_cache_key(stage.project_id, stage.id),
        stage_chart_cache_key(stage.project_id, stage.id),
        stage_events_cache_key(stage.project_id, stage.id),
    ]
    db.delete(stage)
    db.flush()
    for key in keys:
        try:
            store.delete(key)
        except Exception:
            logger.warning("Failed deleting stage artifact %s", key)


def process_pending_ingest_jobs(
    db: Session,
    store: ObjectStore,
    settings: Settings,
    *,
    batch_size: int = 25,
    http_client_factory: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Drain queued Strava webhook `ingest_jobs` honoring rate limits and multi-day clustering."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    client_factory = http_client_factory or _default_sync_http_client

    jobs = _claim_ingest_jobs(db, batch_size=batch_size, now=current_time)
    succeeded = 0
    ignored = 0
    failed = 0
    deferred = 0

    throttled_owners: set[str] = set()

    for job in jobs:
        if job.external_owner_id in throttled_owners:
            job.status = "queued"
            job.lease_expires_at = None
            db.commit()
            deferred += 1
            continue

        conn = db.scalar(
            select(OAuthConnection)
            .join(User, OAuthConnection.user_id == User.id)
            .where(
                OAuthConnection.provider == "strava",
                OAuthConnection.external_athlete_id == job.external_owner_id,
                User.is_active.is_(True),
            )
        )
        if conn is None:
            job.status = "ignored"
            job.error_message = "No active Strava OAuth connection for owner"
            db.commit()
            ignored += 1
            continue

        if job.event_type == "delete":
            matching_stages = list(
                db.scalars(
                    select(Stage)
                    .join(Project, Stage.project_id == Project.id)
                    .where(
                        Project.owner_user_id == conn.user_id,
                        Stage.strava_activity_id == job.external_object_id,
                    )
                ).all()
            )
            touched_projects = {st.project_id for st in matching_stages}
            for st in matching_stages:
                _delete_stage_and_artifacts(db, store, st)
            for pid in touched_projects:
                align_project_photos(db, store, pid)
                write_audit_event(
                    db,
                    user_id=conn.user_id,
                    project_id=pid,
                    event_type="stage.strava_webhook_deleted",
                    metadata={"strava_activity_id": job.external_object_id},
                )
            job.status = "succeeded"
            job.lease_expires_at = None
            db.commit()
            succeeded += 1
            continue

        try:
            access_token = _refresh_access_token_if_needed(
                db=db,
                conn=conn,
                settings=settings,
                user_id=conn.user_id,
                client_factory=client_factory,
                current_time=current_time,
            )
            if access_token is None:
                job.status = "ignored"
                job.error_message = "Strava client credentials unset"
                db.commit()
                ignored += 1
                continue

            auth_headers = {"Authorization": f"Bearer {access_token}"}
            act_resp = _strava_sync_request(
                client_factory,
                settings,
                "GET",
                f"/api/v3/activities/{job.external_object_id}",
                headers=auth_headers,
            )
            if _is_rate_limited(act_resp.headers):
                throttled_owners.add(job.external_owner_id)
            if act_resp.status_code == 404:
                job.status = "ignored"
                job.error_message = "Strava activity not found"
                db.commit()
                ignored += 1
                continue
            if act_resp.status_code != 200:
                raise StravaUpstreamError(f"Activity fetch failed (status={act_resp.status_code})")

            activity_json = act_resp.json()
            if not isinstance(activity_json, dict) or not activity_json.get("start_latlng"):
                job.status = "ignored"
                job.error_message = "Strava activity has no GPS stream"
                db.commit()
                ignored += 1
                continue

            streams_resp = _strava_sync_request(
                client_factory,
                settings,
                "GET",
                f"/api/v3/activities/{job.external_object_id}/streams",
                params={
                    "keys": (
                        "latlng,time,altitude,heartrate,cadence,watts,"
                        "velocity_smooth,grade_smooth,moving,temp,distance"
                    ),
                    "key_by_type": "true",
                },
                headers=auth_headers,
            )
            if _is_rate_limited(streams_resp.headers):
                throttled_owners.add(job.external_owner_id)
            if streams_resp.status_code != 200:
                raise StravaUpstreamError(
                    f"Streams fetch failed (status={streams_resp.status_code})"
                )

            streams_json = streams_resp.json()
            if not isinstance(streams_json, dict):
                raise ValueError("Malformed Strava streams JSON")

            parsed = parse_strava_streams(
                streams_json,
                start_date=activity_json.get("start_date"),
                name=str(activity_json.get("name") or f"Strava {job.external_object_id}"),
            )
            raw_bronze = json.dumps(
                {"activity": activity_json, "streams": streams_json},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            raw_sport = activity_json.get("sport_type") or activity_json.get("type")
            resolved_activity = infer_activity_type_from_strava(
                str(raw_sport) if raw_sport is not None else None,
                parsed.name,
            )

            existing_stage = db.scalar(
                select(Stage)
                .join(Project, Stage.project_id == Project.id)
                .where(
                    Project.owner_user_id == conn.user_id,
                    Stage.strava_activity_id == job.external_object_id,
                )
            )
            if existing_stage is not None:
                _rewrite_stage_from_parsed(
                    db=db,
                    store=store,
                    stage=existing_stage,
                    parsed=parsed,
                    raw_bronze_bytes=raw_bronze,
                    stage_name=parsed.name or existing_stage.name,
                    activity_type=resolved_activity,
                )
                target_project_id = existing_stage.project_id
                stage_id_val = existing_stage.id
            else:
                target_project = _resolve_or_create_autosync_project(db, conn.user_id)
                started_dt = _coerce_utc_datetime(activity_json.get("start_date"))
                day_idx = cluster_activity_day_index(db, target_project.id, started_dt)
                new_stage = ingest_parsed_track(
                    db=db,
                    store=store,
                    project=target_project,
                    parsed=parsed,
                    raw_bronze_bytes=raw_bronze,
                    bronze_ext="json",
                    bronze_content_type="application/json",
                    source_type="strava",
                    source_format="strava",
                    stage_name=parsed.name or f"Strava {job.external_object_id}",
                    day_index=day_idx,
                    activity_type=resolved_activity,
                    external_source_id=str(job.external_object_id),
                    strava_activity_id=job.external_object_id,
                )
                target_project_id = target_project.id
                stage_id_val = new_stage.id

            align_project_photos(db, store, target_project_id)
            write_audit_event(
                db,
                user_id=conn.user_id,
                project_id=target_project_id,
                event_type="stage.strava_webhook_ingested",
                metadata={
                    "stage_id": str(stage_id_val),
                    "strava_activity_id": job.external_object_id,
                    "event_type": job.event_type,
                },
            )
            job.status = "succeeded"
            job.error_message = None
            job.lease_expires_at = None
            db.commit()
            succeeded += 1
        except Exception as exc:
            db.rollback()
            job_row = db.get(IngestJob, job.id)
            if job_row is not None:
                job_row.status = "failed"
                job_row.error_message = f"{type(exc).__name__}: {exc}"[:500]
                job_row.lease_expires_at = None
                db.commit()
            failed += 1

    return {
        "claimed": len(jobs),
        "succeeded": succeeded,
        "ignored": ignored,
        "failed": failed,
        "deferred": deferred,
    }


def _rewrite_stage_from_parsed(
    *,
    db: Session,
    store: ObjectStore,
    stage: Stage,
    parsed: Any,
    raw_bronze_bytes: bytes,
    stage_name: str | None = None,
    activity_type: str | None = None,
    target_etl_version: int = CURRENT_ETL_VERSION,
) -> None:
    df, summary = build_silver_dataframe(
        parsed,
        trip_id=str(stage.project_id),
        stage_id=str(stage.id),
        trim_start_s=stage.trim_start_s,
        trim_end_s=stage.trim_end_s,
    )
    with tempfile.TemporaryDirectory(prefix="trip_replay_") as tmpdir:
        tmp_path = Path(tmpdir)
        pq_path = write_silver_parquet(df, tmp_path / f"{stage.id}.parquet")
        store.put_bytes(
            stage.silver_object_key,
            pq_path.read_bytes(),
            content_type="application/vnd.apache.parquet",
        )
        events_path = tmp_path / f"{stage.id}_events.parquet"
        build_stage_events_parquet(
            pq_path,
            events_path,
            stage_id=str(stage.id),
            trim_start_s=stage.trim_start_s,
            trim_end_s=stage.trim_end_s,
        )
        store.put_bytes(
            stage_events_cache_key(stage.project_id, stage.id),
            events_path.read_bytes(),
            content_type="application/vnd.apache.parquet",
        )
        chart_bytes = build_stage_chart_cache_bytes(
            pq_path,
            trim_start_s=stage.trim_start_s,
            trim_end_s=stage.trim_end_s,
        )
        persist_or_delete_stage_chart_cache(store, stage, chart_bytes)

        active_df = df.filter(~df.get_column("is_trimmed")) if "is_trimmed" in df.columns else df
        active_tuples = [
            (float(lat), float(lon), int(seg))
            for lat, lon, seg in zip(
                active_df.get_column("lat").to_list(),
                active_df.get_column("lon").to_list(),
                active_df.get_column("segment_idx").to_list(),
                strict=True,
            )
        ]
        if active_tuples:
            lod_bytes = build_lod_cache_payload(
                active_tuples,
                trim_start_s=stage.trim_start_s,
                trim_end_s=stage.trim_end_s,
            )
            persist_or_delete_stage_lod_cache(store, stage, lod_bytes)

    bronze_sha = hashlib.sha256(raw_bronze_bytes).hexdigest()
    if stage_name is not None:
        stage.name = stage_name[:200]
    if activity_type is not None:
        stage.activity_type = activity_type
    stage.bronze_sha256 = bronze_sha
    stage.etl_version = target_etl_version
    stage.point_count = summary.point_count
    stage.distance_m = summary.distance_m
    stage.elevation_gain_m = summary.elevation_gain_m
    stage.elevation_loss_m = summary.elevation_loss_m
    stage.moving_seconds = summary.moving_seconds
    stage.elapsed_seconds = summary.elapsed_seconds
    stage.has_timestamps = summary.has_timestamps
    stage.min_lat = summary.min_lat
    stage.max_lat = summary.max_lat
    stage.min_lon = summary.min_lon
    stage.max_lon = summary.max_lon
    stage.warnings_json = [w.model_dump(mode="json") for w in parsed.warnings]
    db.flush()


def replay_outdated_silver_stages(
    db: Session,
    store: ObjectStore,
    *,
    target_etl_version: int = CURRENT_ETL_VERSION,
    batch_size: int = 50,
    dry_run: bool = False,
) -> dict[str, int]:
    """Deterministically replay immutable Bronze -> Silver+caches for outdated `etl_version`."""
    outdated = list(
        db.scalars(
            select(Stage)
            .where(
                (Stage.etl_version < target_etl_version) | (Stage.bronze_sha256.is_(None))
            )
            .order_by(Stage.created_at.asc())
            .limit(batch_size)
        ).all()
    )
    if dry_run:
        return {
            "candidate_count": len(outdated),
            "replayed_count": 0,
            "failed_count": 0,
            "dry_run": 1,
        }

    replayed = 0
    failed = 0
    touched_projects: set[uuid.UUID] = set()

    for stage in outdated:
        try:
            raw_bronze = store.get_bytes(stage.bronze_object_key)
            if stage.source_type == "strava" or stage.source_format == "strava":
                payload = json.loads(raw_bronze.decode("utf-8"))
                act = payload.get("activity") if isinstance(payload, dict) else {}
                streams = payload.get("streams") if isinstance(payload, dict) else payload
                if not isinstance(streams, (Mapping, list)):
                    raise ValueError("Invalid Strava Bronze streams payload")
                parsed = parse_strava_streams(
                    streams,
                    start_date=act.get("start_date") if isinstance(act, dict) else None,
                    name=stage.name,
                )
            else:
                hint = stage.source_format if stage.source_format in ("gpx", "fit", "tcx") else None
                parsed = parse_track_bytes(raw_bronze, format_hint=hint, stitch_tracks=True)
                stage.source_format = parsed.source_format

            _rewrite_stage_from_parsed(
                db=db,
                store=store,
                stage=stage,
                parsed=parsed,
                raw_bronze_bytes=raw_bronze,
                target_etl_version=target_etl_version,
            )
            touched_projects.add(stage.project_id)
            replayed += 1
        except Exception as exc:
            logger.warning("Bronze->Silver replay failed for stage %s: %s", stage.id, exc)
            failed += 1

    for pid in touched_projects:
        align_project_photos(db, store, pid)

    if replayed > 0:
        write_audit_event(
            db,
            user_id=None,
            event_type="platform.silver_replayed",
            metadata={
                "target_etl_version": target_etl_version,
                "replayed_count": replayed,
                "failed_count": failed,
            },
        )
    db.commit()
    return {
        "candidate_count": len(outdated),
        "replayed_count": replayed,
        "failed_count": failed,
        "dry_run": 0,
    }
