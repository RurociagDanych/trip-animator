"""Runs the Marked Trails builder as an ECS Fargate task and waits for its exit code."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3

logger = logging.getLogger(__name__)

CONTAINER_NAME = "marked-trails-builder"
WORK_DIR = "/work"
STARTED_BY = "trip-animator-airflow"
REGION_ENV = "AWS_REGION"
CLUSTER_ENV = "MARKED_TRAILS_FARGATE_CLUSTER"
TASK_DEFINITION_ENV = "MARKED_TRAILS_FARGATE_TASK_DEFINITION"
SUBNETS_ENV = "MARKED_TRAILS_FARGATE_SUBNETS"
SECURITY_GROUPS_ENV = "MARKED_TRAILS_FARGATE_SECURITY_GROUPS"
TIMEOUT_ENV = "MARKED_TRAILS_FARGATE_TIMEOUT_SECONDS"
# The build task has an 8 h execution_timeout; the default stops the Fargate task first, so
# the stop and the timeout error come from this module and not from Airflow killing the process.
DEFAULT_TIMEOUT_SECONDS = 7 * 3600.0
POLL_SECONDS = 30.0


class FargateBuildError(RuntimeError):
    """The Fargate build task could not start, exited without status 0, or was stopped."""


@dataclass(frozen=True, slots=True)
class FargateBuildSettings:
    """Where and how the builder task runs; `subnets` must be public with a public IP."""

    region: str
    cluster: str
    task_definition: str
    subnets: tuple[str, ...]
    security_groups: tuple[str, ...]
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> FargateBuildSettings:
        """Read the settings from the environment; raise `ValueError` for any missing or bad one."""
        required = (
            REGION_ENV,
            CLUSTER_ENV,
            TASK_DEFINITION_ENV,
            SUBNETS_ENV,
            SECURITY_GROUPS_ENV,
        )
        missing = [name for name in required if not environ.get(name, "").strip()]
        if missing:
            raise ValueError(
                f"The fargate Marked Trails backend needs {', '.join(missing)} to be set"
            )
        raw_timeout = environ.get(TIMEOUT_ENV, "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        except ValueError:
            raise ValueError(
                f"{TIMEOUT_ENV} must be a number of seconds, got {raw_timeout!r}"
            ) from None
        if timeout <= 0:
            raise ValueError(f"{TIMEOUT_ENV} must be greater than zero, got {raw_timeout!r}")
        return cls(
            region=environ[REGION_ENV].strip(),
            cluster=environ[CLUSTER_ENV].strip(),
            task_definition=environ[TASK_DEFINITION_ENV].strip(),
            subnets=_split(environ[SUBNETS_ENV]),
            security_groups=_split(environ[SECURITY_GROUPS_ENV]),
            timeout_seconds=timeout,
        )


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def run_fargate_build(
    region: str,
    settings: FargateBuildSettings,
    *,
    ecs_client: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = POLL_SECONDS,
) -> None:
    """Run `build-publish` for `region` on Fargate and return when the task exits with 0.

    Raises `FargateBuildError` when ECS refuses the task, the container exits with any other
    status, or the task has not stopped within `settings.timeout_seconds`. When the wait is
    interrupted (a timeout, or Airflow killing the task) the ECS task is stopped before the
    exception propagates; the ECS task is not stopped after a normal stop.
    """
    client = ecs_client or boto3.client("ecs", region_name=settings.region)
    task_arn = _start_task(client, settings, region)
    logger.info("Started Marked Trails build of %s as Fargate task %s", region, task_arn)
    try:
        task = _wait_until_stopped(
            client, settings, task_arn, clock=clock, sleep=sleep, poll_seconds=poll_seconds
        )
    except BaseException:
        _stop_task(client, settings.cluster, task_arn)
        raise
    _check_exit(task_arn, task)
    logger.info("Marked Trails Fargate task %s exited with status 0", task_arn)


def _start_task(client: Any, settings: FargateBuildSettings, region: str) -> str:
    response = client.run_task(
        cluster=settings.cluster,
        taskDefinition=settings.task_definition,
        launchType="FARGATE",
        count=1,
        startedBy=STARTED_BY,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": list(settings.subnets),
                "securityGroups": list(settings.security_groups),
                # The subnet is public and has no NAT gateway, so the task needs its own IP.
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": CONTAINER_NAME,
                    "command": [
                        "build-publish",
                        "--region",
                        region,
                        "--work-dir",
                        WORK_DIR,
                        "--cap-resources",
                    ],
                }
            ]
        },
    )
    failures = response.get("failures") or []
    if failures:
        reason = failures[0].get("reason", "no reason given")
        raise FargateBuildError(f"ECS did not start the Marked Trails task: {reason}")
    tasks = response.get("tasks") or []
    if len(tasks) != 1:
        raise FargateBuildError(f"ECS returned {len(tasks)} tasks for one Marked Trails run")
    return str(tasks[0]["taskArn"])


def _wait_until_stopped(
    client: Any,
    settings: FargateBuildSettings,
    task_arn: str,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = clock() + settings.timeout_seconds
    while True:
        response = client.describe_tasks(cluster=settings.cluster, tasks=[task_arn])
        failures = response.get("failures") or []
        if failures:
            reason = failures[0].get("reason", "no reason given")
            raise FargateBuildError(f"ECS could not describe the Marked Trails task: {reason}")
        tasks = response.get("tasks") or []
        if len(tasks) == 1 and tasks[0].get("lastStatus") == "STOPPED":
            return dict(tasks[0])
        if clock() >= deadline:
            raise FargateBuildError(
                f"Marked Trails Fargate task did not stop within {settings.timeout_seconds:g} s"
            )
        sleep(poll_seconds)


def _check_exit(task_arn: str, task: dict[str, Any]) -> None:
    containers = [item for item in task.get("containers", []) if item.get("name") == CONTAINER_NAME]
    exit_code = containers[0].get("exitCode") if containers else None
    if exit_code == 0:
        return
    stop_reason = task.get("stoppedReason") or task.get("stopCode") or "no stop reason given"
    raise FargateBuildError(
        f"Marked Trails Fargate task {task_arn} exited with status {exit_code!r}: {stop_reason}"
    )


def _stop_task(client: Any, cluster: str, task_arn: str) -> None:
    try:
        client.stop_task(
            cluster=cluster,
            task=task_arn,
            reason="Marked Trails build stopped by Airflow",
        )
    except Exception:
        # The caller re-raises the original error; a task left running is logged for the operator.
        logger.exception("Could not stop Marked Trails Fargate task %s", task_arn)
