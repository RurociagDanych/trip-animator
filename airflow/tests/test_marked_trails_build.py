from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from trip_airflow.marked_trails_build import MarkedTrailsBuildOperator, run_marked_trails_build


class FakeCli:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self.exit_code


class FakeFargate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.stopped = 0

    def run(self, region: str, run_id: str) -> None:
        self.calls.append((region, run_id))

    def stop(self) -> None:
        self.stopped += 1


FARGATE_ENV = {
    "AWS_REGION": "us-east-1",
    "MARKED_TRAILS_FARGATE_CLUSTER": "cluster-a",
    "MARKED_TRAILS_FARGATE_TASK_DEFINITION": "builder-family",
    "MARKED_TRAILS_FARGATE_SUBNETS": "subnet-a",
    "MARKED_TRAILS_FARGATE_SECURITY_GROUPS": "sg-egress",
}


def test_region_and_work_dir_are_passed_to_the_cli_with_resource_caps(tmp_path: Path) -> None:
    cli = FakeCli()

    run_marked_trails_build("europe", work_dir=tmp_path, cli_main=cli, environ={})

    assert cli.calls == [
        ["build-publish", "--region", "europe", "--work-dir", str(tmp_path), "--cap-resources"]
    ]


def test_work_dir_defaults_to_the_environment(tmp_path: Path) -> None:
    cli = FakeCli()
    environ = {"MARKED_TRAILS_WORK_DIR": str(tmp_path / "volume")}

    run_marked_trails_build("europe/andorra", cli_main=cli, environ=environ)

    assert cli.calls[0][cli.calls[0].index("--work-dir") + 1] == str(tmp_path / "volume")


def test_failed_build_fails_the_task(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="europe.*status 1"):
        run_marked_trails_build(
            "europe", work_dir=tmp_path, cli_main=FakeCli(exit_code=1), environ={}
        )


def test_local_backend_is_the_default(tmp_path: Path) -> None:
    cli = FakeCli()
    fargate = FakeFargate()

    run_marked_trails_build(
        "europe",
        work_dir=tmp_path,
        cli_main=cli,
        environ={},
        fargate_build=fargate,  # type: ignore[arg-type]
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
        environ={**FARGATE_ENV, "MARKED_TRAILS_BUILD_BACKEND": "fargate"},
        fargate_build=fargate,  # type: ignore[arg-type]
        run_id="ti-1",
    )

    assert cli.calls == []
    assert fargate.calls == [("europe", "ti-1")]


def test_fargate_backend_fails_before_starting_when_settings_are_missing(tmp_path: Path) -> None:
    environ = {"MARKED_TRAILS_BUILD_BACKEND": "fargate"}

    with pytest.raises(ValueError, match="MARKED_TRAILS_FARGATE_CLUSTER"):
        run_marked_trails_build("europe", work_dir=tmp_path, environ=environ)


def test_unknown_backend_is_rejected(tmp_path: Path) -> None:
    cli = FakeCli()

    with pytest.raises(ValueError, match="local or fargate, got 'ecs'"):
        run_marked_trails_build(
            "europe",
            work_dir=tmp_path,
            cli_main=cli,
            environ={"MARKED_TRAILS_BUILD_BACKEND": "ecs"},
        )

    assert cli.calls == []


def test_on_kill_without_a_fargate_build_does_nothing() -> None:
    MarkedTrailsBuildOperator(task_id="t", region="europe").on_kill()


def test_on_kill_stops_the_fargate_build() -> None:
    operator = MarkedTrailsBuildOperator(task_id="t", region="europe")
    fargate = FakeFargate()
    operator._fargate_build = fargate  # type: ignore[assignment]

    operator.on_kill()

    assert fargate.stopped == 1
