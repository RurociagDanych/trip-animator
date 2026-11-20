"""Pure-Python Bronze/Silver -> Gold dbt-duckdb materialization and mart validation tasks."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
from trip_api.storage import ObjectStore, S3ObjectStore
from trip_telemetry import run_dbt_gold_build

from trip_airflow.runtime import resolve_gold_output_dir
from trip_airflow.storage_helpers import list_object_keys, list_objects_with_mtime

SILVER_PARQUET_KEY_RE = re.compile(r"^silver/(?P<project_id>[^/]+)/(?P<stage_id>[^/]+)\.parquet$")
_PARITY_TOLERANCE = 1e-6
_MANIFEST_FILE = ".silver_cache_manifest.json"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SilverParquetRef:
    """Validated Silver Parquet object reference."""

    key: str
    project_id: str
    stage_id: str


def _list_silver_parquet_refs(store: ObjectStore) -> list[SilverParquetRef]:
    refs: list[SilverParquetRef] = []
    for key in list_object_keys(store, prefix="silver/"):
        if key.endswith("_events.parquet"):
            continue
        match = SILVER_PARQUET_KEY_RE.match(key)
        if match is None:
            continue
        project_id = match.group("project_id")
        stage_id = match.group("stage_id")
        if project_id in {".", ".."} or stage_id in {".", ".."}:
            continue
        refs.append(SilverParquetRef(key=key, project_id=project_id, stage_id=stage_id))
    return refs


def _list_silver_object_tokens(store: ObjectStore) -> dict[str, str]:
    """Return `{key: version_token}` using S3 ETags when available, else mtime ISO."""
    if isinstance(store, S3ObjectStore):
        tokens: dict[str, str] = {}
        paginator = store.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=store.bucket, Prefix="silver/"):
            for obj in page.get("Contents") or ():
                key = str(obj["Key"])
                if key.endswith("_events.parquet"):
                    continue
                if SILVER_PARQUET_KEY_RE.match(key) is None:
                    continue
                etag = str(obj.get("ETag") or "")
                size = str(obj.get("Size") or "")
                lm = obj.get("LastModified")
                lm_str = lm.isoformat() if isinstance(lm, datetime) else ""
                tokens[key] = f"{etag}:{size}:{lm_str}"
        return dict(sorted(tokens.items()))

    mtimes = list_objects_with_mtime(store, prefix="silver/")
    tokens = {}
    for key, mtime in mtimes.items():
        if key.endswith("_events.parquet"):
            continue
        if SILVER_PARQUET_KEY_RE.match(key) is None:
            continue
        tokens[key] = mtime.isoformat() if isinstance(mtime, datetime) else "unknown"
    return dict(sorted(tokens.items()))


def discover_silver_parquets(store: ObjectStore) -> dict[str, int]:
    """Discover Silver GeoParquet objects and return sanitized summary counts for XCom."""
    refs = _list_silver_parquet_refs(store)
    return {
        "silver_count": len(refs),
        "stage_ids_count": len({ref.stage_id for ref in refs}),
        "project_ids_count": len({ref.project_id for ref in refs}),
    }


def _sync_silver_cache_deltas(
    store: ObjectStore,
    refs: list[SilverParquetRef],
    cache_dir: Path,
) -> tuple[int, int]:
    """Incrementally sync only changed/added Silver Parquets into `cache_dir`."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / _MANIFEST_FILE
    prev_manifest: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_manifest = {str(k): str(v) for k, v in loaded.items()}
        except Exception:
            prev_manifest = {}

    tokens = _list_silver_object_tokens(store)
    active_keys = {ref.key for ref in refs}
    cache_root = cache_dir.resolve()

    for old_key in set(prev_manifest) - active_keys:
        match = SILVER_PARQUET_KEY_RE.match(old_key)
        if match is not None:
            stale_file = (
                cache_dir / match.group("project_id") / f"{match.group('stage_id')}.parquet"
            ).resolve()
            if stale_file.is_relative_to(cache_root) and stale_file.is_file():
                stale_file.unlink(missing_ok=True)

    downloaded = 0
    reused = 0
    new_manifest: dict[str, str] = {}

    for ref in refs:
        dest = (cache_dir / ref.project_id / f"{ref.stage_id}.parquet").resolve()
        if not dest.is_relative_to(cache_root):
            raise ValueError(f"Unsafe staged path for Silver key {ref.key!r}")
        tok = tokens.get(ref.key, "")
        if tok and tok != "unknown" and prev_manifest.get(ref.key) == tok and dest.is_file():
            reused += 1
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(store.get_bytes(ref.key))
            downloaded += 1
        new_manifest[ref.key] = tok

    manifest_path.write_text(json.dumps(new_manifest, sort_keys=True), encoding="utf-8")
    return downloaded, reused


def materialize_gold_marts(
    store: ObjectStore,
    *,
    gold_dir: Path | str | None = None,
    dbt_project_dir: Path | None = None,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Delta-stage Silver GeoParquets, run incremental dbt Gold build, and atomically swap DB."""
    refs = _list_silver_parquet_refs(store)
    if not refs:
        raise ValueError("No Silver Parquet objects found under 'silver/'")

    resolved_gold_dir = resolve_gold_output_dir(gold_dir)
    resolved_gold_dir.mkdir(parents=True, exist_ok=True)
    target_db = resolved_gold_dir / "trip_analytics.duckdb"
    silver_cache_dir = resolved_gold_dir / ".silver_cache"

    downloaded, reused = _sync_silver_cache_deltas(store, refs, silver_cache_dir)

    with tempfile.TemporaryDirectory(prefix="trip_dbt_gold_") as tmp_root:
        tmp_path = Path(tmp_root)
        tmp_gold_dir = tmp_path / "gold"
        tmp_gold_dir.mkdir(parents=True, exist_ok=True)

        # Seed existing Gold DuckDB into tmp_gold_dir so dbt incremental models run delta merges
        seeded_existing = False
        if target_db.is_file():
            try:
                shutil.copyfile(target_db, tmp_gold_dir / "trip_analytics.duckdb")
                seeded_existing = True
            except OSError:
                seeded_existing = False
        elif isinstance(store, S3ObjectStore):
            try:
                seed_bytes = store.get_bytes("gold/trip_analytics.duckdb")
                (tmp_gold_dir / "trip_analytics.duckdb").write_bytes(seed_bytes)
                seeded_existing = True
            except FileNotFoundError:
                seeded_existing = False
            except Exception as exc:
                logger.warning("Best-effort S3 Gold DuckDB seed skipped: %s", exc)
                seeded_existing = False

        silver_glob = str(silver_cache_dir / "*" / "*.parquet")
        try:
            run_dbt_gold_build(
                silver_glob,
                tmp_gold_dir,
                dbt_project_dir=dbt_project_dir,
                timeout_seconds=timeout_seconds,
            )
        except RuntimeError:
            if not seeded_existing:
                raise
            # Schema drift or WAL mismatch on pre-existing DB: retry clean full refresh
            (tmp_gold_dir / "trip_analytics.duckdb").unlink(missing_ok=True)
            run_dbt_gold_build(
                silver_glob,
                tmp_gold_dir,
                dbt_project_dir=dbt_project_dir,
                full_refresh=True,
                timeout_seconds=timeout_seconds,
            )

        built_db = tmp_gold_dir / "trip_analytics.duckdb"
        if not built_db.is_file():
            raise FileNotFoundError("dbt build completed without producing trip_analytics.duckdb")

        # Ensure WAL is checkpointed cleanly before atomic swap
        chk_con = duckdb.connect(str(built_db))
        try:
            chk_con.execute("CHECKPOINT;")
        finally:
            chk_con.close()

        staged_swap = resolved_gold_dir / f".trip_analytics.duckdb.tmp.{uuid.uuid4().hex}"
        try:
            shutil.copyfile(built_db, staged_swap)
            os.replace(staged_swap, target_db)
        except Exception:
            if staged_swap.exists():
                staged_swap.unlink(missing_ok=True)
            raise

    return {
        "status": "success",
        "silver_count": len(refs),
        "stage_ids_count": len({ref.stage_id for ref in refs}),
        "project_ids_count": len({ref.project_id for ref in refs}),
        "downloaded_count": downloaded,
        "cached_count": reused,
    }


def validate_gold_marts(
    *,
    gold_dir: Path | str | None = None,
    expected_stage_count: int | None = None,
) -> dict[str, Any]:
    """Validate Gold DuckDB marts for existence, completeness, non-negativity, and parity."""
    resolved_gold_dir = resolve_gold_output_dir(gold_dir)
    duckdb_path = resolved_gold_dir / "trip_analytics.duckdb"
    if not duckdb_path.is_file():
        raise FileNotFoundError(f"Gold DuckDB database not found at {duckdb_path}")

    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        table_rows = con.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
            """
        ).fetchall()
        tables = {str(r[0]) for r in table_rows}
        required_tables = {
            "mart_stage_summary",
            "mart_trip_daily_summary",
            "fct_climbs",
            "fct_splits",
            "fct_dwell_stops",
            "mart_stage_geo_context",
            "mart_user_career_summary",
            "mart_spatial_coverage_h3",
        }
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            raise ValueError(f"Gold DuckDB missing required mart tables: {missing_tables}")

        stage_agg = con.execute(
            """
            SELECT
                COUNT(*)::BIGINT AS stage_rows,
                COALESCE(SUM(point_count), 0)::BIGINT AS total_points,
                COALESCE(SUM(total_distance_m), 0.0)::DOUBLE AS total_distance_m,
                COALESCE(SUM(elevation_gain_m), 0.0)::DOUBLE AS total_elevation_gain_m,
                COALESCE(SUM(elevation_loss_m), 0.0)::DOUBLE AS total_elevation_loss_m,
                COALESCE(SUM(moving_seconds), 0.0)::DOUBLE AS total_moving_seconds,
                COALESCE(SUM(elapsed_seconds), 0.0)::DOUBLE AS total_elapsed_seconds,
                SUM(
                    CASE
                        WHEN point_count IS NULL
                          OR point_count <= 0
                          OR total_distance_m < 0
                          OR elevation_gain_m < 0
                          OR elevation_loss_m < 0
                          OR moving_seconds < 0
                          OR elapsed_seconds < 0
                        THEN 1
                        ELSE 0
                    END
                )::BIGINT AS invalid_rows
            FROM mart_stage_summary
            """
        ).fetchone()
        assert stage_agg is not None

        daily_agg = con.execute(
            """
            SELECT
                COUNT(*)::BIGINT AS daily_rows,
                COALESCE(SUM(stage_count), 0)::BIGINT AS sum_stage_count,
                COALESCE(SUM(total_points), 0)::BIGINT AS sum_total_points,
                COALESCE(SUM(total_distance_m), 0.0)::DOUBLE AS sum_distance_m,
                COALESCE(SUM(total_elevation_gain_m), 0.0)::DOUBLE AS sum_elevation_gain_m,
                COALESCE(SUM(total_elevation_loss_m), 0.0)::DOUBLE AS sum_elevation_loss_m,
                COALESCE(SUM(total_moving_seconds), 0.0)::DOUBLE AS sum_moving_seconds,
                COALESCE(SUM(total_elapsed_seconds), 0.0)::DOUBLE AS sum_elapsed_seconds
            FROM mart_trip_daily_summary
            """
        ).fetchone()
        assert daily_agg is not None
    finally:
        con.close()

    stage_rows = int(stage_agg[0])
    stage_points = int(stage_agg[1])
    stage_dist = float(stage_agg[2])
    stage_gain = float(stage_agg[3])
    stage_loss = float(stage_agg[4])
    stage_moving = float(stage_agg[5])
    stage_elapsed = float(stage_agg[6])
    invalid_rows = int(stage_agg[7] or 0)

    daily_rows = int(daily_agg[0])
    daily_stage_count = int(daily_agg[1])
    daily_points = int(daily_agg[2])
    daily_dist = float(daily_agg[3])
    daily_gain = float(daily_agg[4])
    daily_loss = float(daily_agg[5])
    daily_moving = float(daily_agg[6])
    daily_elapsed = float(daily_agg[7])

    if stage_rows <= 0:
        raise ValueError("mart_stage_summary is empty")
    if expected_stage_count is not None and stage_rows != expected_stage_count:
        raise ValueError(
            f"mart_stage_summary row count ({stage_rows}) != discovered stages "
            f"({expected_stage_count})"
        )
    if invalid_rows > 0:
        raise ValueError(f"mart_stage_summary contains {invalid_rows} invalid/negative row(s)")

    if stage_rows != daily_stage_count:
        raise ValueError(
            f"Stage count parity mismatch: stage={stage_rows} vs daily={daily_stage_count}"
        )
    if stage_points != daily_points:
        raise ValueError(
            f"Point count parity mismatch: stage={stage_points} vs daily={daily_points}"
        )

    float_checks = [
        ("total_distance_m", stage_dist, daily_dist),
        ("elevation_gain_m", stage_gain, daily_gain),
        ("elevation_loss_m", stage_loss, daily_loss),
        ("moving_seconds", stage_moving, daily_moving),
        ("elapsed_seconds", stage_elapsed, daily_elapsed),
    ]
    for metric_name, stage_val, daily_val in float_checks:
        if abs(stage_val - daily_val) > _PARITY_TOLERANCE:
            raise ValueError(f"Mart parity mismatch for {metric_name}: {stage_val} vs {daily_val}")

    return {
        "validated": True,
        "stage_count": stage_rows,
        "daily_row_count": daily_rows,
        "total_points": stage_points,
        "total_distance_m": round(stage_dist, 2),
        "total_elevation_gain_m": round(stage_gain, 2),
    }
