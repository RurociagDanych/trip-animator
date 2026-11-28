from __future__ import annotations

import re
from pathlib import Path

DOCKER_DIR = Path(__file__).resolve().parents[2] / "infra" / "docker"
AIRFLOW = (DOCKER_DIR / "Dockerfile.airflow").read_text(encoding="utf-8")
BUILDER = (DOCKER_DIR / "Dockerfile.marked-trails-builder").read_text(encoding="utf-8")


def planetiler_image(dockerfile: str) -> str:
    match = re.search(r"^ARG PLANETILER_IMAGE=(\S+)$", dockerfile, re.MULTILINE)
    assert match, "PLANETILER_IMAGE argument is missing"
    return match.group(1)


def test_airflow_image_uses_the_planetiler_digest_of_the_builder_image() -> None:
    assert "@sha256:" in planetiler_image(AIRFLOW)
    assert planetiler_image(AIRFLOW) == planetiler_image(BUILDER)


def test_airflow_image_does_not_depend_on_a_prebuilt_builder_image() -> None:
    assert "marked-trails-builder" not in AIRFLOW.replace("Dockerfile.marked-trails-builder", "")
    assert not re.search(r"^FROM\s+trip-", AIRFLOW, re.MULTILINE)


def test_airflow_image_installs_the_tools_from_debian_and_checks_them() -> None:
    install = re.search(r"apt-get install .*?(?=\n\n)", AIRFLOW, re.DOTALL)
    assert install
    for token in ("osmium-tool", "openjdk-25-jre-headless"):
        assert token in install.group(0)
    for check in ("osmium --version", "java -version", "prlimit --version"):
        assert f"&& {check}" in install.group(0)
