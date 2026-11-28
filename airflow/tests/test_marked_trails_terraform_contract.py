from __future__ import annotations

import re
from pathlib import Path

from trip_airflow.marked_trails_fargate import CONTAINER_NAME, LOG_STREAM_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = (
    REPO_ROOT / "infra" / "terraform" / "standalone-remote-render" / "marked_trails_builder.tf"
).read_text(encoding="utf-8")
CLI_SOURCE = (
    REPO_ROOT / "packages" / "marked-trails" / "src" / "trip_marked_trails" / "cli.py"
).read_text(encoding="utf-8")
DOCKERFILE = (REPO_ROOT / "infra" / "docker" / "Dockerfile.marked-trails-builder").read_text(
    encoding="utf-8"
)


def test_container_name_matches_the_launcher_constant() -> None:
    assert f'name      = "{CONTAINER_NAME}"' in TERRAFORM


def test_log_stream_prefix_matches_the_launcher_constant() -> None:
    assert f'awslogs-stream-prefix = "{LOG_STREAM_PREFIX}"' in TERRAFORM


def test_every_tool_environment_variable_is_read_by_the_cli() -> None:
    names = re.findall(r'name = "(MARKED_TRAILS_(?:TOOL|PLANETILER)_[A-Z_]+)"', TERRAFORM)

    assert len(names) >= 4
    for name in names:
        assert f'"{name}"' in CLI_SOURCE, f"{name} is set in Terraform but not read by the CLI"


def test_the_image_entrypoint_is_the_deadline_wrapper() -> None:
    assert 'ENTRYPOINT ["/usr/local/bin/marked-trails-entrypoint"]' in DOCKERFILE
