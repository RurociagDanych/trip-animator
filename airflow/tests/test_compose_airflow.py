from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_FILE = "../.env.marked-trails"


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


def test_only_the_airflow_service_loads_the_marked_trails_credential_file(
    services: dict[str, dict[str, Any]],
) -> None:
    loaders = [name for name, service in services.items() if CREDENTIAL_FILE in env_files(service)]

    assert loaders == ["airflow"]


def test_no_service_maps_the_marked_trails_credentials_in_its_environment(
    services: dict[str, dict[str, Any]],
) -> None:
    mapped = [
        name
        for name, service in services.items()
        if any("MARKED_TRAILS_AWS" in key for key in service.get("environment", {}))
    ]

    assert mapped == []


def test_credential_file_is_optional_and_has_a_tracked_example() -> None:
    compose = (REPO_ROOT / "infra" / "compose.yaml").read_text(encoding="utf-8")

    assert "path: ../.env.marked-trails\n        required: false" in compose
    example = (REPO_ROOT / ".env.marked-trails.example").read_text(encoding="utf-8")
    assert "MARKED_TRAILS_AWS_ACCESS_KEY_ID=\n" in example
    assert "MARKED_TRAILS_AWS_SECRET_ACCESS_KEY=\n" in example
    assert "MARKED_TRAILS_AWS_SESSION_TOKEN=\n" in example
