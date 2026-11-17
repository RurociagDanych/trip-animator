"""Incremental Strava OAuth2 -> Bronze JSON -> Silver GeoParquet sync DAG."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.sdk import dag, task
from trip_airflow.runtime import open_runtime
from trip_airflow.strava_sync import (
    discover_active_strava_connections as discover_active_strava_connections_fn,
)
from trip_airflow.strava_sync import (
    summarize_strava_sync as summarize_strava_sync_fn,
)
from trip_airflow.strava_sync import (
    sync_strava_connection as sync_strava_connection_fn,
)


@dag(
    dag_id="trip_strava_incremental_sync",
    description="Incrementally sync active Strava OAuth connections into Bronze/Silver telemetry.",
    schedule="*/30 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    tags=["trip-animator", "strava", "ingestion", "bronze-silver"],
    default_args={
        "owner": "trip-animator",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
)
def trip_strava_incremental_sync() -> None:
    @task
    def discover_active_strava_connections() -> list[dict[str, str]]:
        with open_runtime() as rt:
            with rt.session_factory() as db:
                return discover_active_strava_connections_fn(db)

    @task.short_circuit
    def has_active_strava_connections(connections: list[dict[str, str]]) -> bool:
        return len(connections) > 0

    @task(max_active_tis_per_dag=2, retries=2, retry_delay=timedelta(minutes=2))
    def sync_strava_connection(conn_ref: dict[str, str]) -> dict[str, Any]:
        with open_runtime() as rt:
            with rt.session_factory() as db:
                return sync_strava_connection_fn(
                    db=db,
                    store=rt.store,
                    settings=rt.settings,
                    connection_id=conn_ref["connection_id"],
                    user_id=conn_ref["user_id"],
                )

    @task
    def summarize_strava_sync(results: list[dict[str, Any]]) -> dict[str, int]:
        return summarize_strava_sync_fn(results)

    connections = discover_active_strava_connections()
    gate = has_active_strava_connections(connections)
    synced = sync_strava_connection.expand(conn_ref=connections)
    gate >> synced
    summarize_strava_sync(synced)


trip_strava_incremental_sync()
