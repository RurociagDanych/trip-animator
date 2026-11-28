"""Manual DAG that builds the Marked Trails PMTiles for a region and publishes it to the store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from airflow.sdk import Param, dag, task
from trip_airflow.marked_trails_build import POOL_NAME, run_marked_trails_build


@dag(
    dag_id="trip_marked_trails_build",
    schedule=None,
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    tags=["trip-animator", "marked-trails"],
    params={
        "region": Param(
            "europe",
            type="string",
            pattern=r"^[a-z0-9-]+(/[a-z0-9-]+)*$",
            description="Geofabrik region path, for example europe or europe/andorra",
        ),
    },
    default_args={"owner": "trip-animator", "retries": 0},
)
def trip_marked_trails_build() -> None:
    # Eight hours is above the 2 to 4 hours estimated for Europe; it stops a hung build. The
    # fargate backend stops its task before this limit (7 h by default, see the launcher).
    @task(pool=POOL_NAME, pool_slots=1, execution_timeout=timedelta(hours=8))
    def build_and_publish(**context: Any) -> None:
        run_marked_trails_build(str(context["params"]["region"]))

    build_and_publish()


trip_marked_trails_build()
