from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from trip_airflow.marked_trails_fargate import (
    CONTAINER_NAME,
    FargateBuildError,
    FargateBuildSettings,
    run_fargate_build,
)

TASK_ARN = "arn:aws:ecs:us-east-1:000000000000:task/cluster-a/0123456789abcdef"
CLUSTER = "cluster-a"
COMMAND = [
    "build-publish",
    "--region",
    "europe",
    "--work-dir",
    "/work",
    "--cap-resources",
]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Killed(BaseException):
    pass


@pytest.fixture
def settings() -> FargateBuildSettings:
    return FargateBuildSettings(
        region="us-east-1",
        cluster=CLUSTER,
        task_definition="builder-family",
        subnets=("subnet-public",),
        security_groups=("sg-egress",),
        timeout_seconds=60.0,
    )


@pytest.fixture
def ecs() -> Any:
    client = boto3.client(
        "ecs",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    with Stubber(client) as stubber:
        client.stubber = stubber  # type: ignore[attr-defined]
        yield client


def _run_task_ok() -> dict[str, Any]:
    return {"tasks": [{"taskArn": TASK_ARN, "lastStatus": "PROVISIONING"}], "failures": []}


def _run_task_expected() -> dict[str, Any]:
    return {
        "cluster": CLUSTER,
        "taskDefinition": "builder-family",
        "launchType": "FARGATE",
        "count": 1,
        "startedBy": "trip-animator-airflow",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-public"],
                "securityGroups": ["sg-egress"],
                "assignPublicIp": "ENABLED",
            }
        },
        "overrides": {"containerOverrides": [{"name": CONTAINER_NAME, "command": COMMAND}]},
    }


def _describe(status: str, *, exit_code: int | None = None, stop_reason: str | None = None) -> dict:
    container: dict[str, Any] = {"name": CONTAINER_NAME}
    if exit_code is not None:
        container["exitCode"] = exit_code
    task: dict[str, Any] = {"taskArn": TASK_ARN, "lastStatus": status, "containers": [container]}
    if stop_reason is not None:
        task["stoppedReason"] = stop_reason
    return {"tasks": [task], "failures": []}


def _describe_expected() -> dict[str, Any]:
    return {"cluster": CLUSTER, "tasks": [TASK_ARN]}


def _stop_expected() -> dict[str, Any]:
    return {
        "cluster": CLUSTER,
        "task": TASK_ARN,
        "reason": "Marked Trails build stopped by Airflow",
    }


def _add_run_task(ecs: Any) -> None:
    ecs.stubber.add_response("run_task", _run_task_ok(), _run_task_expected())


def _add_describe(ecs: Any, response: dict[str, Any]) -> None:
    ecs.stubber.add_response("describe_tasks", response, _describe_expected())


def test_successful_build_returns_after_exit_code_zero(
    ecs: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_run_task(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    _add_describe(ecs, _describe("STOPPED", exit_code=0, stop_reason="Essential container exited"))

    run_fargate_build(
        "europe", settings, ecs_client=ecs, clock=clock, sleep=clock.sleep, poll_seconds=30
    )

    ecs.stubber.assert_no_pending_responses()
    assert clock.sleeps == [30]


def test_non_zero_exit_fails_the_build(ecs: Any, settings: FargateBuildSettings) -> None:
    clock = Clock()
    _add_run_task(ecs)
    _add_describe(ecs, _describe("STOPPED", exit_code=1, stop_reason="Essential container exited"))

    with pytest.raises(FargateBuildError, match="exited with status 1.*Essential container"):
        run_fargate_build(
            "europe", settings, ecs_client=ecs, clock=clock, sleep=clock.sleep, poll_seconds=30
        )

    ecs.stubber.assert_no_pending_responses()


def test_task_stopped_without_an_exit_code_fails_the_build(
    ecs: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_run_task(ecs)
    stopped = _describe("STOPPED", exit_code=None, stop_reason="CannotPullContainerError")
    _add_describe(ecs, stopped)

    with pytest.raises(FargateBuildError, match="CannotPullContainerError"):
        run_fargate_build(
            "europe", settings, ecs_client=ecs, clock=clock, sleep=clock.sleep, poll_seconds=30
        )


def test_ecs_refusing_the_task_fails_before_any_wait(
    ecs: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    refused = {"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]}
    ecs.stubber.add_response("run_task", refused, _run_task_expected())

    with pytest.raises(FargateBuildError, match="RESOURCE:ENI"):
        run_fargate_build(
            "europe", settings, ecs_client=ecs, clock=clock, sleep=clock.sleep, poll_seconds=30
        )

    assert clock.sleeps == []


def test_timeout_stops_the_task_and_fails(ecs: Any, settings: FargateBuildSettings) -> None:
    clock = Clock()
    _add_run_task(ecs)
    for _ in range(3):
        _add_describe(ecs, _describe("RUNNING"))
    ecs.stubber.add_response("stop_task", {"task": {"taskArn": TASK_ARN}}, _stop_expected())

    with pytest.raises(FargateBuildError, match="did not stop within 60 s"):
        run_fargate_build(
            "europe", settings, ecs_client=ecs, clock=clock, sleep=clock.sleep, poll_seconds=30
        )

    ecs.stubber.assert_no_pending_responses()
    assert clock.sleeps == [30, 30]


def test_airflow_kill_stops_the_task_and_propagates(
    ecs: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_run_task(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    ecs.stubber.add_response("stop_task", {"task": {"taskArn": TASK_ARN}}, _stop_expected())
    calls = 0

    def kill_on_second_wait(seconds: float) -> None:
        nonlocal calls
        calls += 1
        raise Killed

    with pytest.raises(Killed):
        run_fargate_build(
            "europe",
            settings,
            ecs_client=ecs,
            clock=clock,
            sleep=kill_on_second_wait,
            poll_seconds=30,
        )

    ecs.stubber.assert_no_pending_responses()
    assert calls == 1


def test_failed_stop_during_a_kill_keeps_the_original_exception(
    ecs: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_run_task(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    ecs.stubber.add_client_error(
        "stop_task", service_error_code="AccessDeniedException", expected_params=_stop_expected()
    )

    def kill(seconds: float) -> None:
        raise Killed

    with pytest.raises(Killed):
        run_fargate_build(
            "europe", settings, ecs_client=ecs, clock=clock, sleep=kill, poll_seconds=30
        )


def test_settings_are_read_from_the_environment() -> None:
    environ = {
        "AWS_REGION": "us-east-1",
        "MARKED_TRAILS_FARGATE_CLUSTER": "cluster-a",
        "MARKED_TRAILS_FARGATE_TASK_DEFINITION": "builder-family",
        "MARKED_TRAILS_FARGATE_SUBNETS": " subnet-a , subnet-b ",
        "MARKED_TRAILS_FARGATE_SECURITY_GROUPS": "sg-egress",
    }

    settings = FargateBuildSettings.from_environment(environ)

    assert settings.subnets == ("subnet-a", "subnet-b")
    assert settings.timeout_seconds == 7 * 3600


def test_settings_name_every_missing_variable() -> None:
    with pytest.raises(
        ValueError, match="MARKED_TRAILS_FARGATE_CLUSTER, MARKED_TRAILS_FARGATE_SUBNETS"
    ):
        FargateBuildSettings.from_environment(
            {
                "AWS_REGION": "us-east-1",
                "MARKED_TRAILS_FARGATE_TASK_DEFINITION": "builder-family",
                "MARKED_TRAILS_FARGATE_SECURITY_GROUPS": "sg-egress",
            }
        )


@pytest.mark.parametrize("timeout", ["0", "-5", "soon"])
def test_timeout_must_be_a_positive_number(timeout: str) -> None:
    with pytest.raises(ValueError, match="MARKED_TRAILS_FARGATE_TIMEOUT_SECONDS"):
        FargateBuildSettings.from_environment(
            {
                "AWS_REGION": "us-east-1",
                "MARKED_TRAILS_FARGATE_CLUSTER": "cluster-a",
                "MARKED_TRAILS_FARGATE_TASK_DEFINITION": "builder-family",
                "MARKED_TRAILS_FARGATE_SUBNETS": "subnet-a",
                "MARKED_TRAILS_FARGATE_SECURITY_GROUPS": "sg-egress",
                "MARKED_TRAILS_FARGATE_TIMEOUT_SECONDS": timeout,
            }
        )
