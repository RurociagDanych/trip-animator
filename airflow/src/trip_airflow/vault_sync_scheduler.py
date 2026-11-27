"""Airflow-owned scheduling and bounded execution for Strava Vault syncs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from trip_api.config import Settings, object_store_config
from trip_api.db import OAuthConnection, User, VaultSyncState
from trip_api.ingest import process_pending_ingest_jobs
from trip_api.library_enrichment import enqueue_library_enrichment
from trip_api.vault_sync import process_pending_vault_syncs, start_or_resume_strava_vault_sync
from trip_object_store import build_object_store

INCREMENTAL_INTERVAL = timedelta(minutes=30)


def discover_queue_and_process_vault_syncs(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime | None = None,
    batch_size: int = 5,
    max_pages_per_sync: int = 2,
    http_client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, int]:
    """Queue due work and advance a bounded number of eligible syncs.

    Only safe aggregate counters leave the task; connection and ORM data stay local.
    Existing queued, running, rate-limited, and failed work is never reset here.
    """
    current_time = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    counters = {
        "connections_discovered": 0,
        "backfills_queued": 0,
        "incrementals_queued": 0,
        "syncs_claimed": 0,
        "syncs_completed": 0,
        "syncs_rate_limited": 0,
        "syncs_requeued": 0,
        "syncs_failed": 0,
        "enrichment_jobs_queued": 0,
        "ingest_jobs_claimed": 0,
        "ingest_jobs_succeeded": 0,
        "ingest_jobs_ignored": 0,
        "ingest_jobs_failed": 0,
        "ingest_jobs_deferred": 0,
    }

    with session_factory() as db:
        user_ids = list(
            db.scalars(
                select(OAuthConnection.user_id)
                .join(User, OAuthConnection.user_id == User.id)
                .where(OAuthConnection.provider == "strava", User.is_active.is_(True))
                .order_by(OAuthConnection.user_id)
            ).all()
        )
        counters["connections_discovered"] = len(user_ids)

        for user_id in user_ids:
            state = db.scalar(
                select(VaultSyncState).where(
                    VaultSyncState.user_id == user_id,
                    VaultSyncState.provider == "strava",
                )
            )
            if state is None:
                mode = "full_backfill"
            elif state.status in {"queued", "running", "rate_limited", "failed"}:
                continue
            elif state.backfill_completed_at is None:
                # History that is in progress or failed is not restarted by discovery.
                continue
            elif (
                state.status == "completed"
                and state.last_synced_at is not None
                and _as_utc(state.last_synced_at) + INCREMENTAL_INTERVAL <= current_time
            ):
                mode = "incremental"
            else:
                continue

            start_or_resume_strava_vault_sync(
                db, settings, user_id=user_id, mode=mode, now=current_time
            )
            counters["backfills_queued" if mode == "full_backfill" else "incrementals_queued"] += 1

    with session_factory() as db:
        stats = process_pending_vault_syncs(
            db,
            settings,
            batch_size=batch_size,
            max_pages_per_sync=max_pages_per_sync,
            http_client_factory=http_client_factory,
            now=current_time,
        )
    counters.update(
        {
            "syncs_claimed": int(stats.get("claimed", 0)),
            "syncs_completed": int(stats.get("completed", 0)),
            "syncs_rate_limited": int(stats.get("rate_limited", 0)),
            "syncs_requeued": int(stats.get("queued", 0)),
            "syncs_failed": int(stats.get("failed", 0)),
        }
    )
    store = build_object_store(object_store_config(settings))
    with session_factory() as db:
        counters["enrichment_jobs_queued"] = enqueue_library_enrichment(
            db, batch_size=batch_size, now=current_time
        )
    with session_factory() as db:
        enrichment_stats = process_pending_ingest_jobs(
            db,
            store,
            settings,
            batch_size=batch_size,
            http_client_factory=http_client_factory,
            now=current_time,
        )
    for key in ("claimed", "succeeded", "ignored", "failed", "deferred"):
        counters[f"ingest_jobs_{key}"] = int(enrichment_stats.get(key, 0))
    return counters


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
