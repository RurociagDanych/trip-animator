from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("airflow")


def _load_dagbag(tmp_path: Path, monkeypatch: Any) -> Any:
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", f"sqlite:///{tmp_path}/airflow.db")
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")

    try:
        from airflow.dag_processing.dagbag import DagBag  # type: ignore[import-not-found]
    except ImportError:
        from airflow.models import DagBag

    repo_root = Path(__file__).resolve().parents[2]
    dag_folder = repo_root / "airflow" / "dags"
    return DagBag(dag_folder=str(dag_folder), include_examples=False)


def test_all_three_dags_load_without_errors_or_cycles(tmp_path: Path, monkeypatch: Any) -> None:
    dagbag = _load_dagbag(tmp_path, monkeypatch)
    assert dagbag.import_errors == {}

    expected_ids = {
        "trip_dbt_gold_pipeline",
        "trip_strava_incremental_sync",
        "trip_platform_maintenance_and_audit",
    }
    assert expected_ids <= set(dagbag.dags.keys())

    for dag_id in expected_ids:
        dag = dagbag.dags[dag_id]
        assert dag.catchup is False
        assert dag.max_active_runs == 1
        assert "trip-animator" in dag.tags
        # Topological sort raises AirflowDagCycleException if any cycle exists
        sorted_tasks = dag.topological_sort()
        assert len(sorted_tasks) == len(dag.tasks)


def test_dag_schedules_tasks_edges_and_zero_secret_literals(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from airflow.serialization.enums import Encoding
    from airflow.serialization.serialized_objects import SerializedDAG

    dagbag = _load_dagbag(tmp_path, monkeypatch)

    gold_dag = dagbag.dags["trip_dbt_gold_pipeline"]
    assert str(gold_dag.schedule) == "0 * * * *"
    assert set(gold_dag.tags) == {"trip-animator", "medallion", "dbt", "gold"}
    assert set(gold_dag.task_ids) == {
        "discover_silver_parquets",
        "has_silver_parquets",
        "materialize_gold_marts",
        "validate_gold_marts",
    }
    assert gold_dag.get_task("discover_silver_parquets").downstream_task_ids == {
        "has_silver_parquets"
    }
    assert gold_dag.get_task("has_silver_parquets").downstream_task_ids == {
        "materialize_gold_marts"
    }
    assert gold_dag.get_task("materialize_gold_marts").downstream_task_ids == {
        "validate_gold_marts"
    }

    strava_dag = dagbag.dags["trip_strava_incremental_sync"]
    assert str(strava_dag.schedule) == "* * * * *"
    assert strava_dag.is_paused_upon_creation is False
    assert set(strava_dag.tags) == {"trip-animator", "strava", "vault"}
    assert set(strava_dag.task_ids) == {"schedule_and_process_vault_syncs"}
    serialized = SerializedDAG.to_dict(strava_dag)
    assert serialized["dag"]["dag_id"] == "trip_strava_incremental_sync"
    serialized_task = serialized["dag"]["tasks"][0][Encoding.VAR]
    assert serialized_task["task_id"] == "schedule_and_process_vault_syncs"
    assert serialized_task["task_type"] == "_PythonDecoratedOperator"

    maint_dag = dagbag.dags["trip_platform_maintenance_and_audit"]
    assert str(maint_dag.schedule) == "0 3 * * *"
    assert set(maint_dag.tags) == {"trip-animator", "maintenance", "reaper", "audit"}
    assert set(maint_dag.task_ids) == {
        "run_retention_reap",
        "reconcile_postgres_and_object_store",
        "validate_silver_parquet_metadata",
    }
    assert maint_dag.get_task("run_retention_reap").downstream_task_ids == {
        "reconcile_postgres_and_object_store"
    }
    assert maint_dag.get_task("reconcile_postgres_and_object_store").downstream_task_ids == {
        "validate_silver_parquet_metadata"
    }
    assert maint_dag.params["dry_run"] is False
    assert maint_dag.params["delete_orphans"] is False
    assert maint_dag.params["orphan_min_age_hours"] == 24
    assert maint_dag.params["fail_on_missing"] is True
    assert maint_dag.params["fail_on_corrupt"] is True

    # Ensure DAG source files and serialized params contain no hardcoded secret literals
    repo_root = Path(__file__).resolve().parents[2]
    forbidden_markers = (
        "dev-postgres-password",
        "minioadmin-change-me",
        "Bearer ",
        "dev-only-session-secret",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    for dag_file in (repo_root / "airflow" / "dags").glob("*.py"):
        content = dag_file.read_text(encoding="utf-8")
        for marker in forbidden_markers:
            assert marker not in content

    params_blob = json.dumps({k: maint_dag.params[k] for k in maint_dag.params})
    for marker in forbidden_markers:
        assert marker not in params_blob
