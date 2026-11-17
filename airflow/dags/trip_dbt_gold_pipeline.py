"""Hourly Silver GeoParquet -> dbt-duckdb Gold mart pipeline DAG."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.sdk import dag, task
from trip_airflow.dbt_gold import (
    discover_silver_parquets as discover_silver_parquets_fn,
)
from trip_airflow.dbt_gold import (
    materialize_gold_marts as materialize_gold_marts_fn,
)
from trip_airflow.dbt_gold import (
    validate_gold_marts as validate_gold_marts_fn,
)
from trip_airflow.runtime import open_runtime


@dag(
    dag_id="trip_dbt_gold_pipeline",
    description="Materialize and validate dbt-duckdb Gold marts from Silver GeoParquet objects.",
    schedule="0 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    tags=["trip-animator", "medallion", "dbt", "gold"],
    default_args={
        "owner": "trip-animator",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
)
def trip_dbt_gold_pipeline() -> None:
    @task
    def discover_silver_parquets() -> dict[str, int]:
        with open_runtime() as rt:
            return discover_silver_parquets_fn(rt.store)

    @task.short_circuit
    def has_silver_parquets(discovery: dict[str, int]) -> bool:
        return int(discovery.get("silver_count", 0)) > 0

    @task
    def materialize_gold_marts() -> dict[str, Any]:
        with open_runtime() as rt:
            return materialize_gold_marts_fn(rt.store)

    @task
    def validate_gold_marts(materialize_result: dict[str, Any]) -> dict[str, Any]:
        expected = int(materialize_result["silver_count"])
        return validate_gold_marts_fn(expected_stage_count=expected)

    discovery = discover_silver_parquets()
    gate = has_silver_parquets(discovery)
    materialized = materialize_gold_marts()
    gate >> materialized
    validate_gold_marts(materialized)


trip_dbt_gold_pipeline()
