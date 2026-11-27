"""Strava webhook IngestJob queue processor and deterministic Bronze -> Silver replay."""

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
from sqlalchemy import case, select
from sqlalchemy.orm import Session
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

from trip_api.config import Settings
from trip_api.db import IngestJob, OAuthConnection, Project, Stage, User, VaultActivity
from trip_api.deps import ensure_utc
from trip_api.security import decrypt_secret, encrypt_secret, validate_outbound_url
from trip_api.services import (
    align_project_photos,
    build_stage_chart_cache_bytes,
    ingest_parsed_track,
    persist_or_delete_stage_chart_cache,
    persist_or_delete_stage_lod_cache,
    prepare_stage_artifacts,
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    stage_timeline_cache_key,
    write_audit_event,
)
from trip_api.storage import ObjectStore
from trip_api.vault_sync import (
    PreparedStageVaultGeometry,
    apply_strava_track_geometry,
    bump_user_vault_version,
    delete_strava_vault_activity,
    mirror_stage_to_vault,
    prepare_vault_geometry_from_parsed_track,
    recompute_user_chronological_new_tiles,
    upsert_strava_activity_to_vault,
)

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 120
_DEFAULT_MAX_ATTEMPTS = 5
_RATE_LIMIT_BACKOFF_SECONDS = 60


class StravaUpstreamError(RuntimeError):
    """Raised when Strava upstream returns an unexpected status or redirect."""


def _oauth_aad(user_id: uuid.UUID) -> bytes:
    return f"oauth:{user_id}:strava".encode()


def _default_sync_http_client() -> httpx.Client:
    return httpx.Client(timeout=15.0, follow_redirects=False)


def _coerce_utc_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt: datetime = ensure_utc(datetime.fromisoformat(text))
        return dt
    dt_val: datetime = ensure_utc(value)
    return dt_val


def _strava_sync_request(
    client_factory: Callable[[], httpx.Client],
    settings: Settings,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    base = settings.strava_api_base_url.rstrip("/")
    url = f"{base}{path}"
    validate_outbound_url(
        url,
        allowed_hosts=settings.strava_allowed_hosts,
        resolve_dns=True,
    )

    with client_factory() as client:
        try:
            resp = client.request(method, url, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("Strava HTTP request failed: %s", type(exc).__name__)
            raise StravaUpstreamError("Strava upstream request failed") from exc

    if 300 <= resp.status_code < 400:
        raise StravaUpstreamError("Unexpected redirect from Strava upstream")
    return resp


def _find_existing_autosync_project(db: Session, user_id: uuid.UUID) -> Project | None:
    user_projects = list(
        db.scalars(
            select(Project)
            .where(Project.owner_user_id == user_id)
            .order_by(Project.created_at.asc())
        ).all()
    )
    for proj in user_projects:
        if (
            isinstance(proj.settings_json, dict)
            and proj.settings_json.get("strava_auto_sync") is True
        ):
            return proj
    for proj in user_projects:
        if proj.title == "Strava Auto-Sync":
            return proj
    return None


def _resolve_or_create_autosync_project(db: Session, user_id: uuid.UUID) -> Project:
    existing = _find_existing_autosync_project(db, user_id)
    if existing is not None:
        return existing

    project = Project(
        owner_user_id=user_id,
        title="Strava Auto-Sync",
        settings_json={"strava_auto_sync": True},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _resolve_target_project_for_job(
    db: Session,
    user_id: uuid.UUID,
    payload_json: dict[str, object] | None,
) -> Project:
    if isinstance(payload_json, dict):
        raw_project_id = payload_json.get("project_id")
        if isinstance(raw_project_id, str):
            try:
                parsed_project_id = uuid.UUID(raw_project_id)
            except ValueError:
                parsed_project_id = None
            if parsed_project_id is not None:
                owned_project = db.scalar(
                    select(Project).where(
                        Project.id == parsed_project_id,
                        Project.owner_user_id == user_id,
                    )
                )
                if owned_project is not None:
                    return owned_project
    return _resolve_or_create_autosync_project(db, user_id)


def _refresh_access_token_if_needed(
    *,
    db: Session,
    conn: OAuthConnection,
    settings: Settings,
    user_id: uuid.UUID,
    client_factory: Callable[[], httpx.Client],
    current_time: datetime,
) -> str | None:
    key = settings.decoded_encryption_key
    aad = _oauth_aad(user_id)
    access_token: str = decrypt_secret(conn.access_token_enc, key, aad=aad)

    if ensure_utc(conn.expires_at) > current_time + timedelta(seconds=60):
        return access_token

    if not settings.strava_client_id or not settings.strava_client_secret:
        logger.warning(
            "Skipping Strava token refresh for connection %s: client credentials unset",
            conn.id,
        )
        return None

    dialect = db.get_bind().dialect.name
    stmt = (
        select(OAuthConnection)
        .where(OAuthConnection.id == conn.id)
        .execution_options(populate_existing=True)
    )
    if dialect == "postgresql":
        stmt = stmt.with_for_update()
    locked_conn = db.scalar(stmt) or conn

    if ensure_utc(locked_conn.expires_at) > current_time + timedelta(seconds=60):
        return str(decrypt_secret(locked_conn.access_token_enc, key, aad=aad))

    refresh_token: str = decrypt_secret(locked_conn.refresh_token_enc, key, aad=aad)
    resp = _strava_sync_request(
        client_factory,
        settings,
        "POST",
        "/oauth/token",
        data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if resp.status_code != 200:
        raise StravaUpstreamError(
            f"Failed to refresh Strava access token (status={resp.status_code})"
        )

    payload = resp.json()
    if (
        not isinstance(payload, dict)
        or "access_token" not in payload
        or "expires_at" not in payload
    ):
        raise StravaUpstreamError("Malformed Strava token refresh response")

    new_access = str(payload["access_token"])
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    expires_epoch = int(payload["expires_at"])

    locked_conn.access_token_enc = encrypt_secret(new_access, key, aad=aad)
    locked_conn.refresh_token_enc = encrypt_secret(new_refresh, key, aad=aad)
    locked_conn.expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC)
    db.commit()
    return new_access


def _is_rate_limited(headers: Mapping[str, str]) -> bool:
    """Inspect Strava X-ReadRateLimit-* or X-RateLimit-* headers for backpressure."""
    lower = {k.lower(): v for k, v in headers.items()}
    usage_raw = lower.get("x-readratelimit-usage") or lower.get("x-ratelimit-usage")
    limit_raw = lower.get("x-readratelimit-limit") or lower.get("x-ratelimit-limit")
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


def _eligible_ingest_condition(now: datetime) -> Any:
    return (
        (IngestJob.status == "queued")
        & (IngestJob.lease_expires_at.is_(None) | (IngestJob.lease_expires_at <= now))
    ) | (
        (IngestJob.status == "running")
        & IngestJob.lease_expires_at.is_not(None)
        & (IngestJob.lease_expires_at <= now)
    )


def reap_exhausted_ingest_jobs(
    db: Session,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> int:
    """Transition eligible poison IngestJobs at or above `max_attempts` to `failed`."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    stmt = select(IngestJob).where(
        _eligible_ingest_condition(current_time),
        IngestJob.attempt_count >= max_attempts,
    )
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    exhausted = list(db.scalars(stmt).all())
    for job in exhausted:
        job.status = "failed"
        job.lease_expires_at = None
        if not job.error_message:
            job.error_message = f"Exceeded maximum ingest attempts ({max_attempts})"
    if exhausted:
        db.commit()
    return len(exhausted)


def count_pending_ingest_jobs(
    db: Session,
    *,
    batch_size: int = 25,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> int:
    """Count eligible IngestJobs for dry-run reporting without mutating state."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    stmt = (
        select(IngestJob.id)
        .where(
            _eligible_ingest_condition(current_time),
            IngestJob.attempt_count < max_attempts,
        )
        .order_by(
            case((IngestJob.payload_json["kind"].as_string() == "library_enrichment", 1), else_=0),
            IngestJob.created_at.asc(),
        )
        .limit(batch_size)
    )
    return len(list(db.scalars(stmt).all()))


def _claim_ingest_jobs(
    db: Session,
    *,
    batch_size: int,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    now: datetime,
) -> list[IngestJob]:
    reap_exhausted_ingest_jobs(db, max_attempts=max_attempts, now=now)

    lease_until = now + timedelta(seconds=_LEASE_SECONDS)
    stmt = (
        select(IngestJob)
        .where(
            _eligible_ingest_condition(now),
            IngestJob.attempt_count < max_attempts,
        )
        .order_by(
            case((IngestJob.payload_json["kind"].as_string() == "library_enrichment", 1), else_=0),
            IngestJob.created_at.asc(),
        )
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
    bronze_key = stage.bronze_object_key
    keys = [
        stage.silver_object_key,
        stage_lod_cache_key(stage.project_id, stage.id),
        stage_chart_cache_key(stage.project_id, stage.id),
        stage_timeline_cache_key(stage.project_id, stage.id),
        stage_events_cache_key(stage.project_id, stage.id),
    ]
    db.delete(stage)
    db.flush()
    shared_bronze = db.scalar(
        select(Stage.id).where(Stage.bronze_object_key == bronze_key).limit(1)
    )
    if shared_bronze is None:
        keys.insert(0, bronze_key)
    for key in keys:
        try:
            store.delete(key)
        except Exception:
            logger.warning("Failed deleting stage artifact %s", key)


def _catalog_target_is_current(
    db: Session,
    *,
    connection_id: uuid.UUID,
    owner_id: uuid.UUID,
    external_object_id: int,
    expected_id: uuid.UUID | None,
) -> bool:
    connection = db.scalar(
        select(OAuthConnection)
        .join(User, OAuthConnection.user_id == User.id)
        .where(
            OAuthConnection.id == connection_id,
            OAuthConnection.user_id == owner_id,
            OAuthConnection.provider == "strava",
            User.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
        .with_for_update(of=OAuthConnection)
    )
    if connection is None:
        return False
    activity = db.scalar(
        select(VaultActivity)
        .where(
            VaultActivity.user_id == owner_id,
            VaultActivity.source_provider == "strava",
            VaultActivity.strava_activity_id == external_object_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if expected_id is not None and (activity is None or activity.id != expected_id):
        return False
    return activity is None or activity.disconnected_at is None


def process_pending_ingest_jobs(
    db: Session,
    store: ObjectStore,
    settings: Settings,
    *,
    batch_size: int = 25,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    retry_backoff_seconds: int = _RATE_LIMIT_BACKOFF_SECONDS,
    http_client_factory: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Drain queued Strava webhook `ingest_jobs` honoring rate limits and multi-day clustering."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    client_factory = http_client_factory or _default_sync_http_client

    jobs = _claim_ingest_jobs(
        db,
        batch_size=batch_size,
        max_attempts=max_attempts,
        now=current_time,
    )
    succeeded = 0
    ignored = 0
    failed = 0
    deferred = 0

    throttled_owners: set[str] = set()

    for job in jobs:
        if job.external_owner_id in throttled_owners:
            job.status = "queued"
            job.attempt_count = max(0, int(job.attempt_count or 1) - 1)
            job.lease_expires_at = current_time + timedelta(seconds=retry_backoff_seconds)
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
            job.lease_expires_at = None
            job.error_message = "No active Strava OAuth connection for owner"
            db.commit()
            ignored += 1
            continue

        if job.event_type == "delete":
            deleted_vault_count = delete_strava_vault_activity(
                db,
                user_id=conn.user_id,
                strava_activity_id=job.external_object_id,
            )
            if deleted_vault_count > 0:
                write_audit_event(
                    db,
                    user_id=conn.user_id,
                    event_type="vault.strava_webhook_deleted",
                    metadata={
                        "strava_activity_id": job.external_object_id,
                        "deleted_count": deleted_vault_count,
                    },
                )
            job.status = "succeeded"
            job.lease_expires_at = None
            db.commit()
            succeeded += 1
            continue

        is_library_enrichment = job.payload_json.get("kind") == "library_enrichment"
        original = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == conn.user_id,
                VaultActivity.source_provider == "strava",
                VaultActivity.strava_activity_id == job.external_object_id,
            )
        )
        expected_catalog_id = original.id if original is not None else None
        if (original is not None and original.disconnected_at is not None) or (
            is_library_enrichment
            and (
                job.payload_json.get("owner_user_id") != str(conn.user_id)
                or expected_catalog_id is None
                or job.payload_json.get("catalog_activity_id") != str(expected_catalog_id)
            )
        ):
            job.status = "ignored"
            job.lease_expires_at = None
            job.error_message = "Catalog activity no longer eligible for enrichment"
            db.commit()
            ignored += 1
            continue
        owner_id, connection_id = conn.user_id, conn.id
        is_bulk_import = (
            isinstance(job.payload_json, dict)
            and job.payload_json.get("source") == "strava_bulk_import"
        )
        if is_bulk_import:
            existing_bulk_stage_id = db.scalar(
                select(Stage.id)
                .join(Project, Stage.project_id == Project.id)
                .where(
                    Project.owner_user_id == conn.user_id,
                    Stage.strava_activity_id == job.external_object_id,
                )
                .limit(1)
            )
            if existing_bulk_stage_id is not None:
                job.status = "succeeded"
                job.error_message = None
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
                job.lease_expires_at = None
                job.error_message = "Strava client credentials unset"
                db.commit()
                ignored += 1
                continue

            db.commit()
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
            if act_resp.status_code == 429:
                throttled_owners.add(job.external_owner_id)
                if int(job.attempt_count or 0) < max_attempts:
                    job.status = "queued"
                    job.lease_expires_at = current_time + timedelta(seconds=retry_backoff_seconds)
                    job.error_message = "Strava rate limit (429); retrying after backoff"
                    db.commit()
                    deferred += 1
                    continue
                raise StravaUpstreamError("Activity fetch rate limited (status=429)")
            if act_resp.status_code == 404:
                job.status = "ignored"
                job.lease_expires_at = None
                job.error_message = "Strava activity not found"
                db.commit()
                ignored += 1
                continue
            if act_resp.status_code != 200:
                raise StravaUpstreamError(f"Activity fetch failed (status={act_resp.status_code})")

            activity_json = act_resp.json()
            if not isinstance(activity_json, dict):
                raise ValueError("Malformed Strava activity JSON")
            if activity_json.get("id") != job.external_object_id:
                raise ValueError("Provider activity identity differs from the queued target")

            existing_stage = (
                None
                if not is_bulk_import
                else db.scalar(
                    select(Stage)
                    .join(Project, Stage.project_id == Project.id)
                    .where(
                        Project.owner_user_id == conn.user_id,
                        Stage.strava_activity_id == job.external_object_id,
                    )
                )
            )

            if not is_bulk_import and existing_stage is None:
                if not _catalog_target_is_current(
                    db,
                    connection_id=connection_id,
                    owner_id=owner_id,
                    external_object_id=job.external_object_id,
                    expected_id=expected_catalog_id,
                ):
                    job.status = "ignored"
                    job.lease_expires_at = None
                    db.commit()
                    ignored += 1
                    continue
                expected_catalog_id = upsert_strava_activity_to_vault(
                    db, user_id=owner_id, item=activity_json, now=current_time
                ).vault_activity.id
            db.commit()

            if not activity_json.get("start_latlng"):
                if not is_bulk_import:
                    if not _catalog_target_is_current(
                        db,
                        connection_id=connection_id,
                        owner_id=owner_id,
                        external_object_id=job.external_object_id,
                        expected_id=expected_catalog_id,
                    ):
                        job.status = "ignored"
                        job.lease_expires_at = None
                        db.commit()
                        ignored += 1
                        continue
                    upsert_res = upsert_strava_activity_to_vault(
                        db,
                        user_id=conn.user_id,
                        item=activity_json,
                        now=current_time,
                    )
                    activity = upsert_res.vault_activity
                    activity.bronze_object_key = None
                    activity.silver_object_key = None
                    activity.source_sha256 = None
                    activity.source_stage_id = None
                    apply_strava_track_geometry(
                        db,
                        activity,
                        PreparedStageVaultGeometry(summary_polyline=None, tiles_z14=frozenset()),
                        now=current_time,
                    )
                    recompute_user_chronological_new_tiles(db, conn.user_id)
                    bump_user_vault_version(db, conn.user_id)
                    job.status = "succeeded"
                    job.error_message = None
                    job.lease_expires_at = None
                    db.commit()
                    succeeded += 1
                    continue
                job.status = "ignored"
                job.lease_expires_at = None
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
            if streams_resp.status_code == 429:
                throttled_owners.add(job.external_owner_id)
                if int(job.attempt_count or 0) < max_attempts:
                    job.status = "queued"
                    job.lease_expires_at = current_time + timedelta(seconds=retry_backoff_seconds)
                    job.error_message = "Strava streams rate limit (429); retrying after backoff"
                    db.commit()
                    deferred += 1
                    continue
                raise StravaUpstreamError("Streams fetch rate limited (status=429)")
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
            vault_geom = prepare_vault_geometry_from_parsed_track(parsed)
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

            if not is_bulk_import and existing_stage is None:
                assert expected_catalog_id is not None
                artifacts = prepare_stage_artifacts(
                    store=store,
                    project_id=expected_catalog_id,
                    parsed=parsed,
                    raw_bronze_bytes=raw_bronze,
                    bronze_ext="json",
                    bronze_content_type="application/json",
                )
                if not _catalog_target_is_current(
                    db,
                    connection_id=connection_id,
                    owner_id=owner_id,
                    external_object_id=job.external_object_id,
                    expected_id=expected_catalog_id,
                ):
                    job.status = "ignored"
                    job.lease_expires_at = None
                    job.error_message = "Catalog lifecycle changed during enrichment"
                    db.commit()
                    for key in (
                        artifacts.bronze_key,
                        artifacts.silver_key,
                        artifacts.lod_key,
                        artifacts.chart_key,
                        artifacts.events_key,
                    ):
                        store.delete(key)
                    ignored += 1
                    continue
                upsert_res = upsert_strava_activity_to_vault(
                    db, user_id=owner_id, item=activity_json, now=current_time
                )
                vault_activity = upsert_res.vault_activity
                vault_activity.bronze_object_key = artifacts.bronze_key
                vault_activity.silver_object_key = artifacts.silver_key
                vault_activity.source_sha256 = artifacts.full_sha256
                vault_activity.source_stage_id = None
                apply_strava_track_geometry(db, vault_activity, vault_geom, now=current_time)
                recompute_user_chronological_new_tiles(db, conn.user_id)
                bump_user_vault_version(db, conn.user_id)
                write_audit_event(
                    db,
                    user_id=conn.user_id,
                    event_type="vault.strava_webhook_upserted",
                    metadata={
                        "vault_activity_id": str(vault_activity.id),
                        "strava_activity_id": job.external_object_id,
                        "event_type": job.event_type,
                    },
                )
                job.status = "succeeded"
                job.error_message = None
                job.lease_expires_at = None
                db.commit()
                succeeded += 1
                continue

            if existing_stage is not None:
                # Refresh the canonical Strava record, never the project stage:
                # that stage may have been renamed, trimmed, or split locally.
                mirror_stage_to_vault(
                    db,
                    user_id=conn.user_id,
                    stage=existing_stage,
                    geometry=vault_geom,
                    trusted_provider_geometry=True,
                    strava_activity_json=activity_json,
                    recompute_new_tiles=True,
                    now=current_time,
                )
                target_project_id = existing_stage.project_id
                stage_id_val = existing_stage.id
            else:
                target_project = _resolve_target_project_for_job(db, conn.user_id, job.payload_json)
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
                    max_stages_per_project=settings.max_stages_per_project,
                )
                mirror_stage_to_vault(
                    db,
                    user_id=conn.user_id,
                    stage=new_stage,
                    geometry=vault_geom,
                    trusted_provider_geometry=True,
                    strava_activity_json=activity_json,
                    recompute_new_tiles=True,
                    now=current_time,
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
    warnings: list[dict[str, object]] = [w.model_dump(mode="json") for w in parsed.warnings]
    split_exclusion: dict[str, object] = {"code": "vault_canonical_excluded_split_segment"}
    if (
        any(
            warning.get("code") == "vault_canonical_excluded_split_segment"
            for warning in (stage.warnings_json or [])
            if isinstance(warning, dict)
        )
        and split_exclusion not in warnings
    ):
        warnings.append(split_exclusion)
    stage.warnings_json = warnings
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
            .where((Stage.etl_version < target_etl_version) | (Stage.bronze_sha256.is_(None)))
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
