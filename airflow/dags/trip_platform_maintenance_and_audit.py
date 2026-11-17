"""Daily retention reaper, PostgreSQL/ObjectStore reconciliation, and GeoParquet audit DAG."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.sdk import Param, dag, task
from trip_airflow.maintenance_audit import (
    reconcile_postgres_and_object_store as reconcile_postgres_and_object_store_fn,
)
from trip_airflow.maintenance_audit import (
    run_retention_reap as run_retention_reap_fn,
)
from trip_airflow.maintenance_audit import (
    validate_silver_parquet_metadata as validate_silver_parquet_metadata_fn,
)
from trip_airflow.runtime import open_runtime


@dag(
    dag_id="trip_platform_maintenance_and_audit",
    description=(
        "Reap expired sessions/renders, reconcile Postgres vs ObjectStore, and audit Parquet."
    ),
    schedule="0 3 * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    tags=["trip-animator", "maintenance", "reaper", "audit"],
    params={
        "dry_run": Param(False, type="boolean"),
        "delete_orphans": Param(False, type="boolean"),
        "orphan_min_age_hours": Param(24, type="integer", minimum=0),
        "fail_on_missing": Param(True, type="boolean"),
        "fail_on_corrupt": Param(True, type="boolean"),
    },
    default_args={
        "owner": "trip-animator",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
)
def trip_platform_maintenance_and_audit() -> None:
    @task
    def run_retention_reap(**context: Any) -> dict[str, Any]:
        params = context.get("params", {})
        dry_run = bool(params.get("dry_run", False))
        with open_runtime() as rt:
            with rt.session_factory() as db:
                return run_retention_reap_fn(db, rt.store, rt.settings, dry_run=dry_run)

    @task
    def reconcile_postgres_and_object_store(**context: Any) -> dict[str, Any]:
        params = context.get("params", {})
        with open_runtime() as rt:
            with rt.session_factory() as db:
                full = reconcile_postgres_and_object_store_fn(
                    db,
                    rt.store,
                    dry_run=bool(params.get("dry_run", False)),
                    delete_orphans=bool(params.get("delete_orphans", False)),
                    orphan_min_age_hours=int(params.get("orphan_min_age_hours", 24)),
                    fail_on_missing=bool(params.get("fail_on_missing", True)),
                )
        return {
            "stages_checked": full["stages_checked"],
            "render_jobs_checked": full["render_jobs_checked"],
            "missing_bronze_count": full["missing_bronze_count"],
            "missing_silver_count": full["missing_silver_count"],
            "missing_render_count": full["missing_render_count"],
            "orphaned_bronze_count": full["orphaned_bronze_count"],
            "orphaned_silver_count": full["orphaned_silver_count"],
            "orphaned_lod_count": full["orphaned_lod_count"],
            "orphaned_outputs_count": full["orphaned_outputs_count"],
            "deleted_orphans_count": full["deleted_orphans_count"],
            "skipped_recent_orphans_count": full["skipped_recent_orphans_count"],
            "dry_run": full["dry_run"],
        }

    @task
    def validate_silver_parquet_metadata(**context: Any) -> dict[str, Any]:
        params = context.get("params", {})
        with open_runtime() as rt:
            with rt.session_factory() as db:
                full = validate_silver_parquet_metadata_fn(
                    db,
                    rt.store,
                    fail_on_corrupt=bool(params.get("fail_on_corrupt", True)),
                )
        return {
            "checked_stages": full["checked_stages"],
            "valid_stages": full["valid_stages"],
            "corrupt_count": full["corrupt_count"],
        }

    reap = run_retention_reap()
    reconcile = reconcile_postgres_and_object_store()
    audit = validate_silver_parquet_metadata()
    reap >> reconcile >> audit


trip_platform_maintenance_and_audit()
