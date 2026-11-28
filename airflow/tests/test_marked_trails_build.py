from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from trip_airflow.marked_trails_build import run_marked_trails_build
from trip_airflow.marked_trails_fargate import FargateBuildSettings

GIB = 1024**3


def enough_space(_directory: Path) -> int:
    return 100 * GIB


class FakeCli:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self.exit_code


def test_region_and_work_dir_are_passed_to_the_cli_with_resource_caps(tmp_path: Path) -> None:
    cli = FakeCli()

    run_marked_trails_build("europe", work_dir=tmp_path, cli_main=cli, free_bytes=enough_space)

    assert cli.calls == [
        ["build-publish", "--region", "europe", "--work-dir", str(tmp_path), "--cap-resources"]
    ]


def test_work_dir_defaults_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_WORK_DIR", str(tmp_path / "volume"))
    cli = FakeCli()

    run_marked_trails_build("europe/andorra", cli_main=cli, free_bytes=enough_space)

    assert cli.calls[0][cli.calls[0].index("--work-dir") + 1] == str(tmp_path / "volume")


def test_failed_build_fails_the_task(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="europe.*status 1"):
        run_marked_trails_build(
            "europe", work_dir=tmp_path, cli_main=FakeCli(exit_code=1), free_bytes=enough_space
        )


def test_build_does_not_start_with_less_than_12_gib_free(tmp_path: Path) -> None:
    cli = FakeCli()

    with pytest.raises(RuntimeError, match="12 GiB"):
        run_marked_trails_build(
            "europe", work_dir=tmp_path, cli_main=cli, free_bytes=lambda _directory: 12 * GIB - 1
        )

    assert cli.calls == []


def test_build_starts_with_exactly_12_gib_free(tmp_path: Path) -> None:
    cli = FakeCli()

    run_marked_trails_build(
        "europe", work_dir=tmp_path, cli_main=cli, free_bytes=lambda _directory: 12 * GIB
    )

    assert len(cli.calls) == 1


def test_free_space_is_measured_on_the_work_directory(tmp_path: Path) -> None:
    measured: list[Path] = []

    def record(directory: Path) -> int:
        measured.append(directory)
        return 100 * GIB

    work = tmp_path / "volume" / "work"
    run_marked_trails_build("europe", work_dir=work, cli_main=FakeCli(), free_bytes=record)

    assert measured == [work]


FARGATE_ENV = {
    "AWS_REGION": "us-east-1",
    "MARKED_TRAILS_FARGATE_CLUSTER": "cluster-a",
    "MARKED_TRAILS_FARGATE_TASK_DEFINITION": "builder-family",
    "MARKED_TRAILS_FARGATE_SUBNETS": "subnet-a",
    "MARKED_TRAILS_FARGATE_SECURITY_GROUPS": "sg-egress",
}


class FakeFargate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, FargateBuildSettings]] = []

    def __call__(self, region: str, settings: FargateBuildSettings) -> None:
        self.calls.append((region, settings))


def test_local_backend_is_the_default(tmp_path: Path) -> None:
    cli = FakeCli()
    fargate = FakeFargate()

    run_marked_trails_build(
        "europe",
        work_dir=tmp_path,
        cli_main=cli,
        free_bytes=enough_space,
        environ={},
        fargate_build=fargate,
    )

    assert len(cli.calls) == 1
    assert fargate.calls == []


def test_fargate_backend_runs_the_task_and_not_the_local_cli(tmp_path: Path) -> None:
    cli = FakeCli()
    fargate = FakeFargate()

    run_marked_trails_build(
        "europe",
        work_dir=tmp_path,
        cli_main=cli,
        free_bytes=lambda _directory: 0,
        environ={**FARGATE_ENV, "MARKED_TRAILS_BUILD_BACKEND": "fargate"},
        fargate_build=fargate,
    )

    assert cli.calls == []
    region, settings = fargate.calls[0]
    assert region == "europe"
    assert settings.cluster == "cluster-a"
    assert settings.subnets == ("subnet-a",)


def test_fargate_backend_fails_before_starting_when_settings_are_missing(tmp_path: Path) -> None:
    fargate = FakeFargate()
    environ = {"MARKED_TRAILS_BUILD_BACKEND": "fargate"}

    with pytest.raises(ValueError, match="MARKED_TRAILS_FARGATE_CLUSTER"):
        run_marked_trails_build("europe", work_dir=tmp_path, environ=environ, fargate_build=fargate)

    assert fargate.calls == []


def test_unknown_backend_is_rejected(tmp_path: Path) -> None:
    cli = FakeCli()

    with pytest.raises(ValueError, match="local or fargate, got 'ecs'"):
        run_marked_trails_build(
            "europe",
            work_dir=tmp_path,
            cli_main=cli,
            free_bytes=enough_space,
            environ={"MARKED_TRAILS_BUILD_BACKEND": "ecs"},
        )

    assert cli.calls == []
