"""Durably enqueue eligible canonical Strava activities for track enrichment."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import String, cast, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trip_api.db import IngestJob, OAuthConnection, User, VaultActivity, VaultSyncState

_JOB_NAMESPACE = uuid.UUID("2ea5e850-d8a4-4f48-a62a-38beed7cf7d8")


def _job_id(activity_id: uuid.UUID) -> uuid.UUID:
    """Return the stable job ID reserved for one canonical activity."""
    return uuid.uuid5(_JOB_NAMESPACE, str(activity_id))


def enqueue_library_enrichment(
    db: Session, *, batch_size: int = 100, now: datetime | None = None
) -> int:
    """Queue a bounded set of connected, GPS-backed activities without provider I/O."""
    if batch_size <= 0:
        return 0
    current_time = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    existing_enrichment_job = exists(
        select(IngestJob.id).where(
            IngestJob.provider == "strava",
            IngestJob.external_owner_id == OAuthConnection.external_athlete_id,
            IngestJob.external_object_id == VaultActivity.strava_activity_id,
            IngestJob.payload_json["kind"].as_string() == "library_enrichment",
            func.replace(IngestJob.payload_json["owner_user_id"].as_string(), "-", "")
            == func.replace(cast(VaultActivity.user_id, String), "-", ""),
            func.replace(IngestJob.payload_json["catalog_activity_id"].as_string(), "-", "")
            == func.replace(cast(VaultActivity.id, String), "-", ""),
        )
    )
    activities = db.execute(
        select(VaultActivity, OAuthConnection.external_athlete_id)
        .join(User, User.id == VaultActivity.user_id)
        .join(
            OAuthConnection,
            (OAuthConnection.user_id == VaultActivity.user_id)
            & (OAuthConnection.provider == "strava"),
        )
        .outerjoin(
            VaultSyncState,
            (VaultSyncState.user_id == VaultActivity.user_id)
            & (VaultSyncState.provider == "strava"),
        )
        .where(
            User.is_active.is_(True),
            VaultActivity.source_provider == "strava",
            VaultActivity.has_gps.is_(True),
            VaultActivity.strava_activity_id.is_not(None),
            VaultActivity.source_stage_id.is_(None),
            VaultActivity.silver_object_key.is_(None),
            VaultActivity.disconnected_at.is_(None),
            VaultActivity.hidden_at.is_(None),
            ~existing_enrichment_job,
            (VaultSyncState.next_retry_at.is_(None))
            | (VaultSyncState.next_retry_at <= current_time),
        )
        .order_by(VaultActivity.started_at, VaultActivity.id)
        .limit(max(batch_size * 10, batch_size))
    ).all()

    queued = 0
    for activity, external_athlete_id in activities:
        if queued >= batch_size:
            break
        stable_id = _job_id(activity.id)
        if db.get(IngestJob, stable_id) is not None:
            continue
        try:
            with db.begin_nested():
                db.add(
                    IngestJob(
                        id=stable_id,
                        provider="strava",
                        event_type="update",
                        external_owner_id=external_athlete_id,
                        external_object_id=activity.strava_activity_id,
                        payload_json={
                            "kind": "library_enrichment",
                            "owner_user_id": str(activity.user_id),
                            "catalog_activity_id": str(activity.id),
                        },
                        status="queued",
                    )
                )
                db.flush()
        except IntegrityError:
            # Concurrent schedulers may race on the deterministic primary key.
            continue
        queued += 1
    if queued:
        db.commit()
    return queued
