"""Minute scheduler for canonical Strava Vault backfill and reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime

from airflow.sdk import dag, task
from trip_airflow.runtime import open_runtime
from trip_airflow.vault_sync_scheduler import discover_queue_and_process_vault_syncs


@dag(
    dag_id="trip_strava_incremental_sync",
    description="Queue due Strava history/reconciliation work and advance the Vault worker.",
    schedule="* * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    tags=["trip-animator", "strava", "vault"],
    default_args={"owner": "trip-animator", "retries": 1},
)
def trip_strava_incremental_sync() -> None:
    @task
    def schedule_and_process_vault_syncs() -> dict[str, int]:
        with open_runtime() as runtime:
            return discover_queue_and_process_vault_syncs(
                runtime.session_factory,
                runtime.settings,
                batch_size=5,
                max_pages_per_sync=2,
            )

    schedule_and_process_vault_syncs()


trip_strava_incremental_sync()
