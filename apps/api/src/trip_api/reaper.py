"""Retention reaper for expired sessions, OAuth states, rate limits, and render artifacts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from trip_api.config import Settings
from trip_api.db import (
    OAuthState,
    RateLimitRecord,
    RenderJob,
    RenderJobChunk,
    SessionRecord,
    create_db_engine,
    create_session_factory,
)
from trip_api.deps import ensure_utc
from trip_api.storage import LocalObjectStore, ObjectStore, S3ObjectStore

logger = logging.getLogger(__name__)

_TERMINAL_RENDER_STATUSES = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True, slots=True)
class ReapReport:
    """Summary counts produced by a retention reap pass."""

    sessions_deleted: int
    oauth_states_deleted: int
    rate_limits_deleted: int
    render_jobs_deleted: int
    blobs_deleted: int
    dry_run: bool = False


def build_object_store(cfg: Settings) -> ObjectStore:
    """Construct the configured ObjectStore backend."""
    if cfg.object_store_backend == "s3":
        return S3ObjectStore(
            endpoint_url=cfg.s3_endpoint_url,
            access_key_id=cfg.s3_access_key_id,
            secret_access_key=cfg.s3_secret_access_key,
            bucket=cfg.s3_bucket,
            region=cfg.s3_region,
        )
    return LocalObjectStore(Path(cfg.local_object_store_dir))


def _delete_blob_safe(store: ObjectStore, key: str) -> tuple[bool, bool]:
    """Delete an object key from storage.

    Returns ``(ok, deleted)`` where ``ok`` indicates the blob is absent after the call
    and ``deleted`` indicates a stored object was removed.
    """
    if isinstance(store, LocalObjectStore):
        try:
            store.resolve_existing_path(key)
        except FileNotFoundError:
            return True, False
    try:
        store.delete(key)
        return True, True
    except FileNotFoundError:
        return True, False
    except Exception as exc:
        logger.warning("Failed to delete expired blob %r: %s", key, type(exc).__name__)
        return False, False


def reap_expired(
    db: Session,
    store: ObjectStore,
    *,
    now: datetime | None = None,
    render_retention_days: int = 7,
    rate_limit_retention_hours: int = 24,
    dry_run: bool = False,
) -> ReapReport:
    """Delete expired sessions, OAuth states, stale rate limits, and expired render artifacts."""
    if render_retention_days < 1:
        raise ValueError("render_retention_days must be >= 1")
    if rate_limit_retention_hours < 1:
        raise ValueError("rate_limit_retention_hours must be >= 1")

    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    render_cutoff = current_time - timedelta(days=render_retention_days)
    rate_limit_cutoff = current_time - timedelta(hours=rate_limit_retention_hours)

    expired_sessions_cond = SessionRecord.expires_at <= current_time
    expired_session_ids = select(SessionRecord.id).where(expired_sessions_cond)
    expired_oauth_cond = (OAuthState.expires_at <= current_time) | (
        OAuthState.session_id.in_(expired_session_ids)
    )
    stale_rate_limit_cond = RateLimitRecord.updated_at <= rate_limit_cutoff
    expired_render_cond = RenderJob.status.in_(_TERMINAL_RENDER_STATUSES) & (
        ((RenderJob.expires_at.is_not(None)) & (RenderJob.expires_at <= current_time))
        | (RenderJob.updated_at <= render_cutoff)
    )

    expired_jobs = list(db.scalars(select(RenderJob).where(expired_render_cond)).all())
    expired_job_ids = [job.id for job in expired_jobs]
    chunks_by_job: dict[Any, list[RenderJobChunk]] = {}
    if expired_job_ids:
        expired_chunks = list(
            db.scalars(
                select(RenderJobChunk).where(
                    RenderJobChunk.render_job_id.in_(expired_job_ids)
                )
            ).all()
        )
        for ch in expired_chunks:
            chunks_by_job.setdefault(ch.render_job_id, []).append(ch)

    if dry_run:
        oauth_count = int(
            db.scalar(select(func.count()).select_from(OAuthState).where(expired_oauth_cond)) or 0
        )
        session_count = int(
            db.scalar(select(func.count()).select_from(SessionRecord).where(expired_sessions_cond))
            or 0
        )
        rate_limit_count = int(
            db.scalar(
                select(func.count()).select_from(RateLimitRecord).where(stale_rate_limit_cond)
            )
            or 0
        )
        blob_count = sum(
            int(bool(job.output_object_key))
            + int(bool(job.thumbnail_key))
            + sum(1 for ch in chunks_by_job.get(job.id, []) if ch.chunk_object_key)
            for job in expired_jobs
        )
        return ReapReport(
            sessions_deleted=session_count,
            oauth_states_deleted=oauth_count,
            rate_limits_deleted=rate_limit_count,
            render_jobs_deleted=len(expired_jobs),
            blobs_deleted=blob_count,
            dry_run=True,
        )

    oauth_res = cast(CursorResult[Any], db.execute(delete(OAuthState).where(expired_oauth_cond)))
    session_res = cast(
        CursorResult[Any], db.execute(delete(SessionRecord).where(expired_sessions_cond))
    )
    rate_res = cast(
        CursorResult[Any], db.execute(delete(RateLimitRecord).where(stale_rate_limit_cond))
    )

    blobs_deleted = 0
    reaped_job_ids = []
    for job in expired_jobs:
        blob_ok = True
        chunk_keys = [
            ch.chunk_object_key
            for ch in chunks_by_job.get(job.id, [])
            if ch.chunk_object_key
        ]
        for key in (job.output_object_key, job.thumbnail_key, *chunk_keys):
            if not key:
                continue
            ok, deleted = _delete_blob_safe(store, key)
            if not ok:
                blob_ok = False
            elif deleted:
                blobs_deleted += 1
        if blob_ok:
            reaped_job_ids.append(job.id)

    jobs_deleted = 0
    if reaped_job_ids:
        db.execute(
            delete(RenderJobChunk).where(RenderJobChunk.render_job_id.in_(reaped_job_ids))
        )
        job_res = cast(
            CursorResult[Any],
            db.execute(delete(RenderJob).where(RenderJob.id.in_(reaped_job_ids))),
        )
        jobs_deleted = max(0, int(job_res.rowcount or 0))

    db.commit()

    return ReapReport(
        sessions_deleted=max(0, int(session_res.rowcount or 0)),
        oauth_states_deleted=max(0, int(oauth_res.rowcount or 0)),
        rate_limits_deleted=max(0, int(rate_res.rowcount or 0)),
        render_jobs_deleted=jobs_deleted,
        blobs_deleted=blobs_deleted,
        dry_run=False,
    )


def run_reap(
    settings: Settings | None = None,
    *,
    dry_run: bool = False,
    render_retention_days: int | None = None,
    rate_limit_retention_hours: int | None = None,
    database_url: str | None = None,
) -> ReapReport:
    """Execute a retention reap pass using application settings and ObjectStore."""
    cfg = settings or Settings()
    resolved_url = database_url or cfg.database_url
    days = render_retention_days if render_retention_days is not None else cfg.render_retention_days
    hours = (
        rate_limit_retention_hours
        if rate_limit_retention_hours is not None
        else cfg.rate_limit_retention_hours
    )

    engine = create_db_engine(resolved_url)
    session_factory = create_session_factory(engine)
    store = build_object_store(cfg)
    try:
        with session_factory() as db:
            return reap_expired(
                db,
                store,
                render_retention_days=days,
                rate_limit_retention_hours=hours,
                dry_run=dry_run,
            )
    finally:
        engine.dispose()
