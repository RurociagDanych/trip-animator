"""Event-driven Strava webhook queue drain and Bronze -> Silver replay DAG."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.sdk import Param, dag, task
from trip_airflow.ingest_worker import (
    process_pending_ingest_jobs as process_pending_ingest_jobs_fn,
)
from trip_airflow.ingest_worker import (
    replay_outdated_silver_stages as replay_outdated_silver_stages_fn,
)
from trip_airflow.runtime import open_runtime
from trip_telemetry import CURRENT_ETL_VERSION


@dag(
    dag_id="trip_silver_backfill",
    description=(
        "Drain Strava webhook ingest_jobs and replay outdated Bronze objects into Silver v2."
    ),
    schedule="*/5 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    tags=["trip-animator", "bronze-silver", "backfill", "webhooks"],
    params={
        "target_etl_version": Param(CURRENT_ETL_VERSION, type="integer", minimum=1),
        "batch_size": Param(50, type="integer", minimum=1, maximum=500),
        "dry_run": Param(False, type="boolean"),
    },
    default_args={
        "owner": "trip-animator",
        "retries": 1,
        "retry_delay": timedelta(minutes=1),
    },
)
def trip_silver_backfill() -> None:
    @task
    def process_ingest_jobs(**context: Any) -> dict[str, int]:
        params = context.get("params", {})
        batch_size = int(params.get("batch_size", 50))
        with open_runtime() as rt:
            with rt.session_factory() as db:
                return process_pending_ingest_jobs_fn(
                    db,
                    rt.store,
                    rt.settings,
                    batch_size=batch_size,
                )

    @task
    def replay_silver_stages(**context: Any) -> dict[str, int]:
        params = context.get("params", {})
        target_ver = int(params.get("target_etl_version", CURRENT_ETL_VERSION))
        batch_size = int(params.get("batch_size", 50))
        dry_run = bool(params.get("dry_run", False))
        with open_runtime() as rt:
            with rt.session_factory() as db:
                return replay_outdated_silver_stages_fn(
                    db,
                    rt.store,
                    target_etl_version=target_ver,
                    batch_size=batch_size,
                    dry_run=dry_run,
                )

    process_ingest_jobs() >> replay_silver_stages()


trip_silver_backfill()
