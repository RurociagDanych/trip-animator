from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from trip_airflow.marked_trails_fargate import (
    CONTAINER_NAME,
    FargateBuild,
    FargateBuildError,
    FargateBuildSettings,
    client_token,
    task_definition_family,
)

CLUSTER = "cluster-a"
ACCOUNT_ARN_PREFIX = "arn:aws:ecs:us-east-1:111122223333"
TASK_ARN = f"{ACCOUNT_ARN_PREFIX}:task/{CLUSTER}/0123456789abcdef"
FAMILY_ARN = f"{ACCOUNT_ARN_PREFIX}:task-definition/builder-family:7"
RUN_ID = "11111111-2222-3333-4444-555555555555-1"
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


@pytest.fixture
def settings() -> FargateBuildSettings:
    return FargateBuildSettings(
        region="us-east-1",
        cluster=CLUSTER,
        task_definition="builder-family",
        subnets=("subnet-public-a", "subnet-public-b"),
        security_groups=("sg-egress",),
        timeout_seconds=3600.0,
    )


def _ecs_client() -> Any:
    return boto3.client(
        "ecs",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def ecs() -> Any:
    client = _ecs_client()
    with Stubber(client) as stubber:
        client.stubber = stubber
        yield client


@pytest.fixture
def stopper() -> Any:
    client = _ecs_client()
    with Stubber(client) as stubber:
        client.stubber = stubber
        yield client


def _build(
    settings: FargateBuildSettings, ecs: Any, stopper: Any, clock: Clock, **options: Any
) -> FargateBuild:
    return FargateBuild(
        settings,
        ecs_client=ecs,
        stop_client=stopper,
        clock=clock,
        sleep=clock.sleep,
        poll_seconds=30,
        **options,
    )


def _run_task_expected(deadline: int = 3600 + 900) -> dict[str, Any]:
    return {
        "cluster": CLUSTER,
        "taskDefinition": "builder-family",
        "launchType": "FARGATE",
        "count": 1,
        "startedBy": "trip-animator-airflow",
        "clientToken": client_token(RUN_ID),
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": ["subnet-public-a", "subnet-public-b"],
                "securityGroups": ["sg-egress"],
                "assignPublicIp": "ENABLED",
            }
        },
        "overrides": {
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "command": COMMAND,
                    "environment": [
                        {"name": "MARKED_TRAILS_DEADLINE_SECONDS", "value": str(deadline)}
                    ],
                }
            ]
        },
    }


def _describe(
    status: str,
    *,
    exit_code: int | None = None,
    stop_reason: str | None = None,
    container_reason: str | None = None,
) -> dict[str, Any]:
    container: dict[str, Any] = {"name": CONTAINER_NAME}
    if exit_code is not None:
        container["exitCode"] = exit_code
    if container_reason is not None:
        container["reason"] = container_reason
    task: dict[str, Any] = {"taskArn": TASK_ARN, "lastStatus": status, "containers": [container]}
    if stop_reason is not None:
        task["stoppedReason"] = stop_reason
    return {"tasks": [task], "failures": []}


def _stop_expected() -> dict[str, Any]:
    return {
        "cluster": CLUSTER,
        "task": TASK_ARN,
        "reason": "Marked Trails build stopped by Airflow",
    }


def _add_start(ecs: Any, timeout: int = 3600) -> None:
    ecs.stubber.add_response(
        "list_tasks", {"taskArns": []}, {"cluster": CLUSTER, "startedBy": "trip-animator-airflow"}
    )
    ecs.stubber.add_response(
        "run_task",
        {"tasks": [{"taskArn": TASK_ARN, "lastStatus": "PROVISIONING"}], "failures": []},
        _run_task_expected(timeout + 900),
    )


def _short(settings: FargateBuildSettings) -> FargateBuildSettings:
    return replace(settings, timeout_seconds=60.0)


def _add_describe(ecs: Any, response: dict[str, Any]) -> None:
    ecs.stubber.add_response("describe_tasks", response, {"cluster": CLUSTER, "tasks": [TASK_ARN]})


def _add_describe_error(ecs: Any, code: str, status: int = 400) -> None:
    ecs.stubber.add_client_error(
        "describe_tasks",
        service_error_code=code,
        http_status_code=status,
        expected_params={"cluster": CLUSTER, "tasks": [TASK_ARN]},
    )


def _add_stop(stopper: Any) -> None:
    stopper.stubber.add_response("stop_task", {"task": {"taskArn": TASK_ARN}}, _stop_expected())


def test_successful_build_returns_after_exit_code_zero(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    _add_describe(ecs, _describe("STOPPED", exit_code=0, stop_reason="Essential container exited"))

    _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()
    assert sum(clock.sleeps) == 30


def test_start_logs_the_cloudwatch_log_stream(
    ecs: Any, stopper: Any, settings: FargateBuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _add_start(ecs)
    _add_describe(ecs, _describe("STOPPED", exit_code=0))

    _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)

    assert "marked-trails/marked-trails-builder/0123456789abcdef" in caplog.text


def test_non_zero_exit_reports_the_container_and_task_stop_reasons(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    _add_start(ecs)
    stopped = _describe(
        "STOPPED",
        exit_code=1,
        stop_reason="Essential container exited",
        container_reason="OutOfMemoryError: Container killed",
    )
    _add_describe(ecs, stopped)

    with pytest.raises(FargateBuildError) as raised:
        _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)

    message = str(raised.value)
    assert "exited with status 1" in message
    assert "OutOfMemoryError: Container killed" in message
    assert "Essential container exited" in message
    assert "marked-trails/marked-trails-builder/0123456789abcdef" in message


@pytest.mark.parametrize(
    "exit_code, text",
    [(124, "in-container deadline expired"), (137, "killed with SIGKILL")],
)
def test_deadline_and_kill_exit_codes_get_a_clear_message(
    ecs: Any, stopper: Any, settings: FargateBuildSettings, exit_code: int, text: str
) -> None:
    _add_start(ecs)
    _add_describe(ecs, _describe("STOPPED", exit_code=exit_code))

    with pytest.raises(FargateBuildError, match=text):
        _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)


def test_task_stopped_without_an_exit_code_fails_the_build(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    _add_start(ecs)
    _add_describe(ecs, _describe("STOPPED", stop_reason="CannotPullContainerError"))

    with pytest.raises(FargateBuildError, match="CannotPullContainerError"):
        _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)


def test_ecs_refusing_the_task_fails_before_any_wait(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    ecs.stubber.add_response(
        "list_tasks", {"taskArns": []}, {"cluster": CLUSTER, "startedBy": "trip-animator-airflow"}
    )
    refused = {"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]}
    ecs.stubber.add_response("run_task", refused, _run_task_expected())

    with pytest.raises(FargateBuildError, match="RESOURCE:ENI"):
        _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    assert clock.sleeps == []


def test_run_task_access_denied_fails_without_a_stop(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    ecs.stubber.add_response(
        "list_tasks", {"taskArns": []}, {"cluster": CLUSTER, "startedBy": "trip-animator-airflow"}
    )
    ecs.stubber.add_client_error(
        "run_task", service_error_code="AccessDeniedException", expected_params=_run_task_expected()
    )

    with pytest.raises(FargateBuildError, match="AccessDeniedException"):
        _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)

    stopper.stubber.assert_no_pending_responses()


def test_timeout_stops_the_task_and_fails(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs, timeout=60)
    for _ in range(3):
        _add_describe(ecs, _describe("RUNNING"))
    _add_stop(stopper)

    with pytest.raises(FargateBuildError, match="did not stop within 60 s"):
        _build(_short(settings), ecs, stopper, clock).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()
    stopper.stubber.assert_no_pending_responses()
    assert sum(clock.sleeps) == 60


def test_stop_from_a_signal_handler_stops_the_task_once_and_ends_the_wait(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    _add_stop(stopper)
    build = _build(settings, ecs, stopper, clock)
    original_sleep = clock.sleep

    def interrupted_sleep(seconds: float) -> None:
        original_sleep(seconds)
        build.stop()

    build._sleep = interrupted_sleep

    with pytest.raises(FargateBuildError, match="stopped by Airflow"):
        build.run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()
    stopper.stubber.assert_no_pending_responses()


def test_stop_before_the_task_exists_stops_it_as_soon_as_it_starts(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    _add_start(ecs)
    _add_stop(stopper)
    build = _build(settings, ecs, stopper, Clock())
    build.stop()

    with pytest.raises(FargateBuildError, match="stopped before it started"):
        build.run("europe", RUN_ID)

    stopper.stubber.assert_no_pending_responses()


def test_failed_stop_during_a_kill_keeps_the_original_error(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    stopper.stubber.add_client_error(
        "stop_task", service_error_code="AccessDeniedException", expected_params=_stop_expected()
    )
    build = _build(settings, ecs, stopper, clock)
    build._sleep = lambda _seconds: build.stop()

    with pytest.raises(FargateBuildError, match="stopped by Airflow"):
        build.run("europe", RUN_ID)


def test_stop_of_a_task_that_already_stopped_is_not_an_error(
    ecs: Any, stopper: Any, settings: FargateBuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    _add_start(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    stopper.stubber.add_client_error(
        "stop_task",
        service_error_code="InvalidParameterException",
        expected_params=_stop_expected(),
    )
    build = _build(settings, ecs, stopper, Clock())
    build._sleep = lambda _seconds: build.stop()

    with pytest.raises(FargateBuildError):
        build.run("europe", RUN_ID)

    assert "had already stopped" in caplog.text


@pytest.mark.parametrize(
    "code, status",
    [("ThrottlingException", 400), ("ServerException", 500), ("SomethingElse", 503)],
)
def test_throttling_and_server_errors_are_retried_until_the_task_stops(
    ecs: Any, stopper: Any, settings: FargateBuildSettings, code: str, status: int
) -> None:
    clock = Clock()
    _add_start(ecs)
    _add_describe_error(ecs, code, status)
    _add_describe_error(ecs, code, status)
    _add_describe(ecs, _describe("STOPPED", exit_code=0))

    _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()
    assert sum(clock.sleeps) == 60 + 120


def test_connection_errors_are_retried_with_a_growing_delay(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs)
    _add_describe(ecs, _describe("STOPPED", exit_code=0))
    failures = [EndpointConnectionError(endpoint_url="https://ecs.invalid")] * 2
    real_describe = ecs.describe_tasks

    def flaky(**kwargs: Any) -> Any:
        if failures:
            raise failures.pop()
        return real_describe(**kwargs)

    ecs.describe_tasks = flaky

    _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    assert sum(clock.sleeps) == 60 + 120


@pytest.mark.parametrize("code", ["AccessDeniedException", "InvalidParameterException"])
def test_access_denied_and_validation_errors_fail_fast_and_stop_the_task(
    ecs: Any, stopper: Any, settings: FargateBuildSettings, code: str
) -> None:
    clock = Clock()
    _add_start(ecs)
    _add_describe_error(ecs, code)
    _add_stop(stopper)

    with pytest.raises(FargateBuildError, match=code):
        _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    assert clock.sleeps == []
    stopper.stubber.assert_no_pending_responses()


def test_missing_task_is_tolerated_during_the_first_minutes(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs)
    missing = {"tasks": [], "failures": [{"arn": TASK_ARN, "reason": "MISSING"}]}
    _add_describe(ecs, missing)
    _add_describe(ecs, missing)
    _add_describe(ecs, _describe("STOPPED", exit_code=0))

    _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()


def test_missing_task_after_the_grace_period_fails(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    clock = Clock()
    _add_start(ecs)
    missing = {"tasks": [], "failures": [{"arn": TASK_ARN, "reason": "MISSING"}]}
    for _ in range(12):
        _add_describe(ecs, missing)
    _add_stop(stopper)

    with pytest.raises(FargateBuildError, match="MISSING"):
        _build(settings, ecs, stopper, clock).run("europe", RUN_ID)

    assert sum(clock.sleeps) == 300


def test_missing_task_after_it_was_seen_fails_at_once(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    _add_start(ecs)
    _add_describe(ecs, _describe("RUNNING"))
    _add_describe(ecs, {"tasks": [], "failures": [{"arn": TASK_ARN, "reason": "MISSING"}]})
    _add_stop(stopper)

    with pytest.raises(FargateBuildError, match="MISSING"):
        _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)


def test_orphans_of_the_same_family_are_stopped_before_the_new_task(
    ecs: Any, stopper: Any, settings: FargateBuildSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    orphan = f"{ACCOUNT_ARN_PREFIX}:task/{CLUSTER}/orphan1"
    other = f"{ACCOUNT_ARN_PREFIX}:task/{CLUSTER}/other"
    done = f"{ACCOUNT_ARN_PREFIX}:task/{CLUSTER}/done"
    started_by = {"cluster": CLUSTER, "startedBy": "trip-animator-airflow"}
    ecs.stubber.add_response("list_tasks", {"taskArns": [orphan], "nextToken": "n"}, started_by)
    ecs.stubber.add_response(
        "list_tasks", {"taskArns": [other, done]}, {**started_by, "nextToken": "n"}
    )
    ecs.stubber.add_response(
        "describe_tasks",
        {
            "tasks": [
                {"taskArn": orphan, "taskDefinitionArn": FAMILY_ARN, "lastStatus": "RUNNING"},
                {
                    "taskArn": other,
                    "taskDefinitionArn": f"{ACCOUNT_ARN_PREFIX}:task-definition/other:1",
                    "lastStatus": "RUNNING",
                },
                {"taskArn": done, "taskDefinitionArn": FAMILY_ARN, "lastStatus": "STOPPED"},
            ],
            "failures": [],
        },
        {"cluster": CLUSTER, "tasks": [orphan, other, done]},
    )
    ecs.stubber.add_response(
        "stop_task",
        {"task": {"taskArn": orphan}},
        {
            "cluster": CLUSTER,
            "task": orphan,
            "reason": "Marked Trails orphan stopped before a new run",
        },
    )
    ecs.stubber.add_response(
        "run_task",
        {"tasks": [{"taskArn": TASK_ARN}], "failures": []},
        _run_task_expected(),
    )
    _add_describe(ecs, _describe("STOPPED", exit_code=0))

    _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()
    assert orphan in caplog.text
    assert other not in caplog.text


def test_orphan_that_stops_by_itself_first_is_not_an_error(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    orphan = f"{ACCOUNT_ARN_PREFIX}:task/{CLUSTER}/orphan1"
    ecs.stubber.add_response(
        "list_tasks",
        {"taskArns": [orphan]},
        {"cluster": CLUSTER, "startedBy": "trip-animator-airflow"},
    )
    ecs.stubber.add_response(
        "describe_tasks",
        {
            "tasks": [
                {"taskArn": orphan, "taskDefinitionArn": FAMILY_ARN, "lastStatus": "RUNNING"}
            ],
            "failures": [],
        },
        {"cluster": CLUSTER, "tasks": [orphan]},
    )
    ecs.stubber.add_client_error(
        "stop_task",
        service_error_code="InvalidParameterException",
        expected_params={
            "cluster": CLUSTER,
            "task": orphan,
            "reason": "Marked Trails orphan stopped before a new run",
        },
    )
    ecs.stubber.add_response(
        "run_task", {"tasks": [{"taskArn": TASK_ARN}], "failures": []}, _run_task_expected()
    )
    _add_describe(ecs, _describe("STOPPED", exit_code=0))

    _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()


def test_failed_orphan_sweep_does_not_start_a_second_task(
    ecs: Any, stopper: Any, settings: FargateBuildSettings
) -> None:
    ecs.stubber.add_client_error(
        "list_tasks",
        service_error_code="AccessDeniedException",
        expected_params={"cluster": CLUSTER, "startedBy": "trip-animator-airflow"},
    )

    with pytest.raises(FargateBuildError, match="AccessDeniedException"):
        _build(settings, ecs, stopper, Clock()).run("europe", RUN_ID)

    ecs.stubber.assert_no_pending_responses()


def test_client_token_is_stable_and_at_most_64_characters() -> None:
    seed = "11111111-2222-3333-4444-555555555555-1"

    assert client_token(seed) == client_token(seed)
    assert client_token(seed) == f"mt-{seed}"
    long_token = client_token("x" * 200)
    assert len(long_token) == 64
    assert client_token("x" * 200) == long_token
    assert client_token("a b") != client_token("a c")
    assert len(client_token("a b")) <= 64


@pytest.mark.parametrize(
    "task_definition, family",
    [
        ("builder-family", "builder-family"),
        ("builder-family:7", "builder-family"),
        (FAMILY_ARN, "builder-family"),
    ],
)
def test_task_definition_family_accepts_family_revision_and_arn(
    task_definition: str, family: str
) -> None:
    assert task_definition_family(task_definition) == family


def test_real_clients_use_standard_retries_and_a_no_retry_stop_client(
    settings: FargateBuildSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    build = FargateBuild(settings)

    retries = build._client.meta.config.retries
    assert retries["mode"] == "standard"
    assert retries["total_max_attempts"] == 11
    stop_config = build._stop_client.meta.config
    assert stop_config.retries["total_max_attempts"] == 1
    assert stop_config.connect_timeout + stop_config.read_timeout < 5


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
