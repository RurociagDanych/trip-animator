"""Pure-Python retention reaper, storage reconciliation, and Silver GeoParquet audit tasks."""

from __future__ import annotations

import io
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow.fs as pafs  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session
from trip_api.config import Settings
from trip_api.db import RenderJob, RenderJobChunk, Stage, VaultActivity
from trip_api.deps import ensure_utc
from trip_api.reaper import reap_expired
from trip_api.services import (
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    write_audit_event,
)
from trip_api.storage import LocalObjectStore, ObjectStore, S3ObjectStore

from trip_airflow.storage_helpers import list_objects_with_mtime

REQUIRED_SILVER_COLUMNS: frozenset[str] = frozenset(
    {
        "trip_id",
        "stage_id",
        "seq",
        "ts_utc",
        "lat",
        "lon",
        "elev_m",
        "delta_dist_m",
        "cum_dist_m",
        "speed_mps",
        "segment_idx",
        "is_moving",
        "geometry",
    }
)


class ReconciliationError(RuntimeError):
    """Raised when required PostgreSQL-referenced blobs are missing in ObjectStore."""


class SilverAuditError(RuntimeError):
    """Raised when one or more Silver GeoParquet objects fail schema/metadata validation."""


def run_retention_reap(
    db: Session,
    store: ObjectStore,
    settings: Settings,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run retention reaping and record an AuditEvent when rows or blobs are deleted."""
    report = reap_expired(
        db,
        store,
        render_retention_days=settings.render_retention_days,
        rate_limit_retention_hours=settings.rate_limit_retention_hours,
        dry_run=dry_run,
    )
    summary: dict[str, Any] = {
        "sessions_deleted": report.sessions_deleted,
        "oauth_states_deleted": report.oauth_states_deleted,
        "rate_limits_deleted": report.rate_limits_deleted,
        "render_jobs_deleted": report.render_jobs_deleted,
        "blobs_deleted": report.blobs_deleted,
        "dry_run": report.dry_run,
    }
    total_reaped = (
        report.sessions_deleted
        + report.oauth_states_deleted
        + report.rate_limits_deleted
        + report.render_jobs_deleted
        + report.blobs_deleted
    )
    if not report.dry_run and total_reaped > 0:
        write_audit_event(
            db,
            user_id=None,
            event_type="platform.maintenance_reaped",
            metadata=summary,
        )
        db.commit()
    return summary


def reconcile_postgres_and_object_store(
    db: Session,
    store: ObjectStore,
    *,
    dry_run: bool = False,
    delete_orphans: bool = False,
    orphan_min_age_hours: int = 24,
    fail_on_missing: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile project, library, and render records with stored objects."""
    if orphan_min_age_hours < 0:
        raise ValueError("orphan_min_age_hours must be >= 0")

    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    orphan_cutoff = current_time - timedelta(hours=orphan_min_age_hours)

    stages = list(db.scalars(select(Stage)).all())
    render_jobs = list(db.scalars(select(RenderJob)).all())
    render_chunks = list(db.scalars(select(RenderJobChunk)).all())

    expected_bronze = {s.bronze_object_key for s in stages if s.bronze_object_key}
    expected_silver = {s.silver_object_key for s in stages if s.silver_object_key}
    library_keys = db.execute(
        select(VaultActivity.bronze_object_key, VaultActivity.silver_object_key).where(
            VaultActivity.bronze_object_key.is_not(None)
            | VaultActivity.silver_object_key.is_not(None)
        )
    )
    for bronze_key, silver_key in library_keys:
        if bronze_key:
            expected_bronze.add(bronze_key)
        if silver_key:
            expected_silver.add(silver_key)
    expected_sidecars: set[str] = set()
    for s in stages:
        expected_sidecars.add(stage_lod_cache_key(s.project_id, s.id))
        expected_sidecars.add(stage_chart_cache_key(s.project_id, s.id))
        expected_sidecars.add(stage_events_cache_key(s.project_id, s.id))

    expected_outputs: set[str] = set()
    required_render_keys: set[str] = set()
    active_job_prefixes: tuple[str, ...] = tuple(
        f"outputs/chunks/{job.id}/" for job in render_jobs if job.status in ("queued", "running")
    )
    succeeded_missing_output_ref = 0
    for job in render_jobs:
        if job.output_object_key:
            expected_outputs.add(job.output_object_key)
        if job.thumbnail_key:
            expected_outputs.add(job.thumbnail_key)
        if job.status == "succeeded":
            if job.output_object_key:
                required_render_keys.add(job.output_object_key)
            else:
                succeeded_missing_output_ref += 1
            if job.thumbnail_key:
                required_render_keys.add(job.thumbnail_key)
    for ch in render_chunks:
        if ch.chunk_object_key:
            expected_outputs.add(ch.chunk_object_key)

    actual_bronze = list_objects_with_mtime(store, prefix="bronze/")
    actual_silver = list_objects_with_mtime(store, prefix="silver/")
    actual_outputs = list_objects_with_mtime(store, prefix="outputs/")

    missing_bronze = sorted(expected_bronze - set(actual_bronze.keys()))
    missing_silver = sorted(expected_silver - set(actual_silver.keys()))
    missing_render = sorted(required_render_keys - set(actual_outputs.keys()))

    orphaned_bronze = sorted(set(actual_bronze.keys()) - expected_bronze)
    orphaned_silver: list[str] = []
    orphaned_lod: list[str] = []
    for key in sorted(actual_silver.keys()):
        if key in expected_silver or key in expected_sidecars:
            continue
        if key.endswith((".lod.json.gz", ".chart.json.gz")):
            orphaned_lod.append(key)
        else:
            orphaned_silver.append(key)

    orphaned_outputs = sorted(
        k
        for k in set(actual_outputs.keys()) - expected_outputs
        if not (active_job_prefixes and k.startswith(active_job_prefixes))
    )

    deleted_orphans_count = 0
    skipped_recent_orphans_count = 0

    if delete_orphans and not dry_run:
        orphan_candidates: list[tuple[str, datetime | None]] = (
            [(k, actual_bronze[k]) for k in orphaned_bronze]
            + [(k, actual_silver[k]) for k in orphaned_silver]
            + [(k, actual_silver[k]) for k in orphaned_lod]
            + [(k, actual_outputs[k]) for k in orphaned_outputs]
        )
        for key, mtime in orphan_candidates:
            if orphan_min_age_hours == 0:
                eligible = True
            elif mtime is not None:
                eligible = ensure_utc(mtime) <= orphan_cutoff
            else:
                eligible = False

            if eligible:
                store.delete(key)
                deleted_orphans_count += 1
            else:
                skipped_recent_orphans_count += 1

    missing_total = (
        len(missing_bronze)
        + len(missing_silver)
        + len(missing_render)
        + succeeded_missing_output_ref
    )

    summary: dict[str, Any] = {
        "stages_checked": len(stages),
        "render_jobs_checked": len(render_jobs),
        "missing_bronze": missing_bronze,
        "missing_silver": missing_silver,
        "missing_render": missing_render,
        "missing_bronze_count": len(missing_bronze),
        "missing_silver_count": len(missing_silver),
        "missing_render_count": len(missing_render) + succeeded_missing_output_ref,
        "orphaned_bronze": orphaned_bronze,
        "orphaned_silver": orphaned_silver,
        "orphaned_lod": orphaned_lod,
        "orphaned_outputs": orphaned_outputs,
        "orphaned_bronze_count": len(orphaned_bronze),
        "orphaned_silver_count": len(orphaned_silver),
        "orphaned_lod_count": len(orphaned_lod),
        "orphaned_outputs_count": len(orphaned_outputs),
        "deleted_orphans_count": deleted_orphans_count,
        "skipped_recent_orphans_count": skipped_recent_orphans_count,
        "dry_run": dry_run,
    }

    if fail_on_missing and missing_total > 0:
        raise ReconciliationError(
            f"Storage reconciliation detected {missing_total} missing required object(s): "
            f"bronze={len(missing_bronze)}, silver={len(missing_silver)}, "
            f"render={len(missing_render) + succeeded_missing_output_ref}"
        )

    return summary


def _build_pyarrow_s3_fs(store: S3ObjectStore) -> pafs.S3FileSystem:
    meta = getattr(store.client, "meta", None)
    endpoint_url = getattr(meta, "endpoint_url", None) if meta is not None else None
    region = getattr(meta, "region_name", None) if meta is not None else None

    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    get_creds = getattr(store.client, "_get_credentials", None)
    if callable(get_creds):
        creds = get_creds()
        if creds is not None:
            frozen = creds.get_frozen_credentials()
            access_key = getattr(frozen, "access_key", None)
            secret_key = getattr(frozen, "secret_key", None)
            session_token = getattr(frozen, "token", None)

    kwargs: dict[str, Any] = {}
    if access_key and secret_key:
        kwargs["access_key"] = access_key
        kwargs["secret_key"] = secret_key
        if session_token:
            kwargs["session_token"] = session_token
    if region:
        kwargs["region"] = region
    if endpoint_url:
        kwargs["endpoint_override"] = endpoint_url
        if str(endpoint_url).startswith("http://"):
            kwargs["scheme"] = "http"

    return pafs.S3FileSystem(**kwargs)


logger = logging.getLogger(__name__)


def _open_silver_parquet_file(stage: Stage, store: ObjectStore) -> pq.ParquetFile:
    if isinstance(store, LocalObjectStore):
        return pq.ParquetFile(store.resolve_existing_path(stage.silver_object_key))

    if isinstance(store, S3ObjectStore):
        try:
            s3_fs = _build_pyarrow_s3_fs(store)
            return pq.ParquetFile(f"{store.bucket}/{stage.silver_object_key}", filesystem=s3_fs)
        except Exception as exc:
            # Fallback for unit-test mocked boto3 clients without a live HTTP S3 endpoint
            logger.debug(
                "S3FileSystem footer read fallback for %s: %s", stage.id, type(exc).__name__
            )

    return pq.ParquetFile(io.BytesIO(store.get_bytes(stage.silver_object_key)))


def _validate_single_parquet(stage: Stage, store: ObjectStore) -> None:
    if stage.point_count <= 0:
        raise ValueError(f"Stage {stage.id} has non-positive point_count={stage.point_count}")

    pf = _open_silver_parquet_file(stage, store)
    schema = pf.schema_arrow
    col_names = set(schema.names)
    missing_cols = sorted(REQUIRED_SILVER_COLUMNS - col_names)
    if missing_cols:
        raise ValueError(f"Missing Silver Parquet columns: {missing_cols}")

    num_rows = int(pf.metadata.num_rows)
    if num_rows <= 0:
        raise ValueError(
            f"Row count mismatch: parquet={num_rows} vs stage.point_count={stage.point_count}"
        )

    is_stage_trimmed = stage.trim_start_s is not None or stage.trim_end_s is not None
    if is_stage_trimmed and "is_trimmed" in col_names:
        trimmed_tbl = pf.read(columns=["is_trimmed"])
        active_rows = sum(
            1 for val in trimmed_tbl.column("is_trimmed").to_pylist() if not bool(val)
        )
        if active_rows != stage.point_count:
            raise ValueError(
                "Row count mismatch: "
                f"active_parquet={active_rows} vs stage.point_count={stage.point_count}"
            )
    elif num_rows != stage.point_count:
        raise ValueError(
            f"Row count mismatch: parquet={num_rows} vs stage.point_count={stage.point_count}"
        )

    meta = schema.metadata or {}
    raw_geo = meta.get(b"geo")
    if not raw_geo:
        raise ValueError("Missing GeoParquet 'geo' schema metadata")

    geo = json.loads(raw_geo.decode("utf-8"))
    if not isinstance(geo, dict):
        raise ValueError("GeoParquet 'geo' metadata is not a JSON object")
    if not isinstance(geo.get("version"), str) or not geo["version"]:
        raise ValueError("GeoParquet 'geo' metadata missing version")
    if geo.get("primary_column") != "geometry":
        raise ValueError("GeoParquet primary_column must be 'geometry'")

    columns_meta = geo.get("columns")
    if not isinstance(columns_meta, dict) or not isinstance(columns_meta.get("geometry"), dict):
        raise ValueError("GeoParquet metadata missing columns.geometry")
    geom_meta = columns_meta["geometry"]
    if geom_meta.get("encoding") != "WKB":
        raise ValueError("GeoParquet geometry encoding must be 'WKB'")

    bbox = geom_meta.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("GeoParquet bbox must be a 4-element list")
    floats: list[float] = []
    for val in bbox:
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError("GeoParquet bbox contains non-numeric entry")
        f_val = float(val)
        if not math.isfinite(f_val):
            raise ValueError("GeoParquet bbox contains non-finite value")
        floats.append(f_val)
    if floats[0] > floats[2] or floats[1] > floats[3]:
        raise ValueError("GeoParquet bbox min > max")


def validate_silver_parquet_metadata(
    db: Session,
    store: ObjectStore,
    *,
    fail_on_corrupt: bool = True,
) -> dict[str, Any]:
    """Validate Silver GeoParquet schema, GeoParquet metadata, and row counts per Stage."""
    stages = list(db.scalars(select(Stage).order_by(Stage.created_at.asc())).all())
    corrupt_stages: list[dict[str, str]] = []
    valid_stages = 0

    for stage in stages:
        try:
            _validate_single_parquet(stage, store)
            valid_stages += 1
        except Exception as exc:
            corrupt_stages.append(
                {
                    "stage_id": str(stage.id),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    summary: dict[str, Any] = {
        "checked_stages": len(stages),
        "valid_stages": valid_stages,
        "corrupt_count": len(corrupt_stages),
        "corrupt_stages": corrupt_stages,
    }
    write_audit_event(
        db,
        user_id=None,
        event_type="platform.audit_completed",
        metadata={
            "checked_stages": len(stages),
            "valid_stages": valid_stages,
            "corrupt_count": len(corrupt_stages),
        },
    )
    db.commit()

    if fail_on_corrupt and corrupt_stages:
        raise SilverAuditError(
            f"Silver GeoParquet validation failed for {len(corrupt_stages)} stage(s)"
        )

    return summary
