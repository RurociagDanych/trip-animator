"""Pure-Python Airflow task functions for Trip Animator orchestration."""

from trip_airflow.dbt_gold import (
    discover_silver_parquets,
    materialize_gold_marts,
    validate_gold_marts,
)
from trip_airflow.maintenance_audit import (
    ReconciliationError,
    SilverAuditError,
    reconcile_postgres_and_object_store,
    run_retention_reap,
    validate_silver_parquet_metadata,
)
from trip_airflow.runtime import AirflowRuntime, open_runtime, resolve_gold_output_dir
from trip_airflow.storage_helpers import (
    list_object_keys,
    list_objects_with_mtime,
    object_exists,
)
from trip_airflow.strava_sync import (
    StravaUpstreamError,
    discover_active_strava_connections,
    summarize_strava_sync,
    sync_strava_connection,
)

__all__ = [
    "AirflowRuntime",
    "ReconciliationError",
    "SilverAuditError",
    "StravaUpstreamError",
    "discover_active_strava_connections",
    "discover_silver_parquets",
    "list_object_keys",
    "list_objects_with_mtime",
    "materialize_gold_marts",
    "object_exists",
    "open_runtime",
    "reconcile_postgres_and_object_store",
    "resolve_gold_output_dir",
    "run_retention_reap",
    "summarize_strava_sync",
    "sync_strava_connection",
    "validate_gold_marts",
    "validate_silver_parquet_metadata",
]
