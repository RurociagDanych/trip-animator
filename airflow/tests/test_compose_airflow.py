from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def services() -> dict[str, dict[str, Any]]:
    compose = yaml.safe_load((REPO_ROOT / "infra" / "compose.yaml").read_text(encoding="utf-8"))
    return dict(compose["services"])


def env_files(service: dict[str, Any]) -> list[str]:
    return [
        entry["path"] if isinstance(entry, dict) else entry for entry in service.get("env_file", [])
    ]


def test_airflow_images_build_from_the_repository_without_extra_build_contexts(
    services: dict[str, dict[str, Any]],
) -> None:
    for name in ("airflow", "airflow-init"):
        assert "additional_contexts" not in services[name]["build"], name


def test_airflow_runs_three_tasks_at_once(services: dict[str, dict[str, Any]]) -> None:
    assert services["airflow"]["environment"]["AIRFLOW__CORE__PARALLELISM"] == "3"


def test_only_the_root_env_file_is_loaded_by_the_airflow_service(
    services: dict[str, dict[str, Any]],
) -> None:
    assert env_files(services["airflow"]) == ["../.env"]


def test_no_separate_marked_trails_credential_file_remains() -> None:
    compose = (REPO_ROOT / "infra" / "compose.yaml").read_text(encoding="utf-8")

    assert ".env.marked-trails" not in compose
    assert not (REPO_ROOT / "marked-trails.env.example").exists()


def test_every_service_with_s3_settings_gets_explicit_object_store_keys(
    services: dict[str, dict[str, Any]],
) -> None:
    compose = yaml.safe_load((REPO_ROOT / "infra" / "compose.yaml").read_text(encoding="utf-8"))
    app_env = compose["x-app-env"]

    assert app_env["S3_ACCESS_KEY_ID"].startswith("${S3_ACCESS_KEY_ID")
    assert app_env["S3_SECRET_ACCESS_KEY"].startswith("${S3_SECRET_ACCESS_KEY")
    assert not any(key.startswith("AWS_") for key in app_env)


def test_object_store_region_is_explicit_and_not_the_aws_region() -> None:
    compose = yaml.safe_load((REPO_ROOT / "infra" / "compose.yaml").read_text(encoding="utf-8"))
    app_env = compose["x-app-env"]

    assert app_env["S3_REGION"].startswith("${S3_REGION")
    assert "AWS_REGION" not in app_env
    assert "S3_ENDPOINT_URL" in app_env
