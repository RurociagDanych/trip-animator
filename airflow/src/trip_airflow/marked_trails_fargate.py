"""Runs the Marked Trails builder as an ECS Fargate task and waits for its exit code."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

logger = logging.getLogger(__name__)

CONTAINER_NAME = "marked-trails-builder"
LOG_STREAM_PREFIX = "marked-trails"
WORK_DIR = "/work"
STARTED_BY = "trip-animator-airflow"
DEADLINE_ENV = "MARKED_TRAILS_DEADLINE_SECONDS"
REGION_ENV = "AWS_REGION"
CLUSTER_ENV = "MARKED_TRAILS_FARGATE_CLUSTER"
TASK_DEFINITION_ENV = "MARKED_TRAILS_FARGATE_TASK_DEFINITION"
SUBNETS_ENV = "MARKED_TRAILS_FARGATE_SUBNETS"
SECURITY_GROUPS_ENV = "MARKED_TRAILS_FARGATE_SECURITY_GROUPS"
TIMEOUT_ENV = "MARKED_TRAILS_FARGATE_TIMEOUT_SECONDS"
# The build task has an 8 h execution_timeout; the default stops the Fargate task first, so
# the stop and the timeout error come from this module and not from Airflow killing the process.
DEFAULT_TIMEOUT_SECONDS = 7 * 3600.0
# The in-container deadline only matters when this launcher is dead, so it sits above the wait
# timeout: in every other case the launcher stops the task first and cleanly.
DEADLINE_MARGIN_SECONDS = 15 * 60.0
POLL_SECONDS = 30.0
MAX_ERROR_BACKOFF_SECONDS = 300.0
# DescribeTasks is eventually consistent and answers MISSING for a task RunTask just returned.
MISSING_GRACE_SECONDS = 300.0
# Airflow sends SIGTERM and SIGKILLs the task process 5 s later, so the stop call must finish
# well inside that window: one attempt, short timeouts, and a bounded wait for its thread.
STOP_CONNECT_TIMEOUT_SECONDS = 1
STOP_READ_TIMEOUT_SECONDS = 2
STOP_JOIN_SECONDS = 3.5
SLEEP_SLICE_SECONDS = 1.0
CLIENT_TOKEN_MAX_LENGTH = 64
DESCRIBE_BATCH_SIZE = 100
EXIT_DEADLINE = 124
EXIT_KILLED = 137
STOP_CODE_ESSENTIAL_EXITED = "EssentialContainerExited"
# Throttling and transient server codes that are worth waiting out; HTTP 5xx is added separately.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "ThrottledException",
        "RequestThrottled",
        "RequestThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "LimitExceededException",
        "BandwidthLimitExceeded",
        "SlowDown",
        "PriorRequestNotComplete",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServerException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalError",
    }
)
# Other BotoCoreError subclasses (bad parameters, missing credentials) cannot succeed on a retry.
RETRYABLE_BOTOCORE_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
# What ECS answers for a task that stopped between ListTasks and StopTask.
TASK_GONE_ERROR_CODES = frozenset({"InvalidParameterException", "ResourceNotFoundException"})


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


def client_token(seed: str) -> str:
    """Return an ECS `clientToken` of at most 64 characters that is stable for `seed`.

    A botocore retry of a RunTask whose response was lost must not start a second task, so the
    token comes from the Airflow task instance and try number and not from a random value.
    """
    token = f"mt-{seed}"
    if len(token) <= CLIENT_TOKEN_MAX_LENGTH and re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return token
    return f"mt-{hashlib.sha256(seed.encode()).hexdigest()}"[:CLIENT_TOKEN_MAX_LENGTH]


def task_definition_family(task_definition: str) -> str:
    """Return the family of a task definition given as a family, `family:revision` or ARN."""
    return task_definition.rsplit("/", 1)[-1].split(":", 1)[0]


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in RETRYABLE_ERROR_CODES or status >= 500
    return isinstance(error, RETRYABLE_BOTOCORE_ERRORS)


def _error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", "unknown"))
    return type(error).__name__


class FargateBuild:
    """One Marked Trails build on Fargate: start, wait, and stop when interrupted.

    `stop` is safe to call from a signal handler: Airflow's SIGTERM handler calls the operator's
    `on_kill` and returns without raising, then SIGKILLs the process 5 s later, so the ECS task
    must be stopped inside that window and the wait loop must not rely on an exception.
    """

    def __init__(
        self,
        settings: FargateBuildSettings,
        *,
        ecs_client: Any | None = None,
        stop_client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._settings = settings
        # standard mode retries throttling, 5xx and connection errors with exponential backoff.
        self._client = ecs_client or boto3.client(
            "ecs",
            region_name=settings.region,
            config=Config(retries={"mode": "standard", "max_attempts": 10}),
        )
        # A separate client with no retries and short timeouts: a retry could outlast the SIGKILL.
        self._stop_client = stop_client or boto3.client(
            "ecs",
            region_name=settings.region,
            config=Config(
                retries={"mode": "standard", "total_max_attempts": 1},
                connect_timeout=STOP_CONNECT_TIMEOUT_SECONDS,
                read_timeout=STOP_READ_TIMEOUT_SECONDS,
            ),
        )
        self._clock = clock
        self._sleep = sleep
        self._poll_seconds = poll_seconds
        self._task_arn: str | None = None
        self._stop_requested = False
        self._stop_in_flight = False

    def run(self, region: str, run_id: str) -> None:
        """Run `build-publish` for `region` on Fargate and return when the task exits with 0.

        `run_id` identifies this attempt (Airflow task instance id and try number) and makes
        the RunTask call idempotent. Orphans of earlier runs are stopped first. Raises
        `FargateBuildError` when ECS refuses the task, a non-retryable ECS error occurs, the
        container exits with any other status, the task has not stopped within the settings
        timeout, or `stop` was called. The ECS task is stopped before any exception other than
        an exit-status failure propagates.
        """
        self._stop_orphans()
        if self._stop_requested:
            raise FargateBuildError("The Marked Trails build was stopped before it started")
        task_arn = self._start_task(region, run_id)
        self._task_arn = task_arn
        stream = self._log_stream(task_arn)
        logger.info(
            "Started Marked Trails build of %s as Fargate task %s, CloudWatch log stream %s",
            region,
            task_arn,
            stream,
        )
        try:
            if self._stop_requested:
                raise FargateBuildError("The Marked Trails build was stopped before it started")
            task = self._wait_until_stopped(task_arn)
        except BaseException:
            self.stop()
            raise
        self._check_exit(task_arn, stream, task)
        logger.info("Marked Trails Fargate task %s exited with status 0", task_arn)

    def stop(self) -> None:
        """Stop the ECS task with one short attempt and make the wait loop raise.

        Called from `on_kill`, which Airflow runs inside a SIGTERM handler. The call runs in a
        thread, so the interrupted main thread never re-enters a boto3 client it was using, and
        the wait for that thread is bounded.
        """
        self._stop_requested = True
        task_arn = self._task_arn
        if task_arn is None or self._stop_in_flight:
            return
        self._stop_in_flight = True
        thread = threading.Thread(target=self._send_stop, args=(task_arn,), daemon=True)
        thread.start()
        thread.join(STOP_JOIN_SECONDS)

    def _send_stop(self, task_arn: str) -> None:
        try:
            self._stop_client.stop_task(
                cluster=self._settings.cluster,
                task=task_arn,
                reason="Marked Trails build stopped by Airflow",
            )
        except (ClientError, BotoCoreError) as exc:
            if isinstance(exc, ClientError) and _error_code(exc) in TASK_GONE_ERROR_CODES:
                logger.info("Marked Trails Fargate task %s had already stopped", task_arn)
                return
            # The caller re-raises its own error; a task left running is logged for the operator
            # and is bounded by the in-container deadline and the next run's orphan sweep.
            logger.error(
                "Could not stop Marked Trails Fargate task %s: %s", task_arn, _error_code(exc)
            )
            self._stop_in_flight = False
        else:
            logger.warning("Asked ECS to stop Marked Trails Fargate task %s", task_arn)

    def _stop_orphans(self) -> None:
        """Stop running tasks of this family that an earlier, killed run left behind.

        Only one build runs at a time (one DAG run, one pool slot), so any running task started
        by this launcher is an orphan that would double the cost and write to the same prefix.
        """
        family = task_definition_family(self._settings.task_definition)
        arns = self._call("list the running Marked Trails tasks", self._list_started_by)
        for batch in _batches(arns, DESCRIBE_BATCH_SIZE):
            response = self._call(
                "describe the running Marked Trails tasks",
                self._client.describe_tasks,
                cluster=self._settings.cluster,
                tasks=batch,
            )
            for task in response.get("tasks") or []:
                arn = str(task["taskArn"])
                running = task.get("lastStatus") != "STOPPED"
                if running and task_definition_family(str(task.get("taskDefinitionArn"))) == family:
                    logger.warning(
                        "Stopping orphaned Marked Trails Fargate task %s (status %s)",
                        arn,
                        task.get("lastStatus"),
                    )
                    self._stop_orphan(arn)

    def _list_started_by(self) -> list[str]:
        arns: list[str] = []
        token: str | None = None
        while True:
            # ECS rejects startedBy combined with another filter, so the family is checked after.
            arguments: dict[str, Any] = {
                "cluster": self._settings.cluster,
                "startedBy": STARTED_BY,
            }
            if token:
                arguments["nextToken"] = token
            page = self._client.list_tasks(**arguments)
            arns.extend(page.get("taskArns") or [])
            token = page.get("nextToken")
            if not token:
                return arns

    def _stop_orphan(self, arn: str) -> None:
        try:
            self._client.stop_task(
                cluster=self._settings.cluster,
                task=arn,
                reason="Marked Trails orphan stopped before a new run",
            )
        except (ClientError, BotoCoreError) as exc:
            if isinstance(exc, ClientError) and _error_code(exc) in TASK_GONE_ERROR_CODES:
                logger.info("Orphaned task %s stopped by itself first", arn)
                return
            raise FargateBuildError(
                f"Could not stop the orphaned Marked Trails task {arn}: {_error_code(exc)}"
            ) from exc

    def _call(self, action: str, call: Callable[..., Any], **arguments: Any) -> Any:
        try:
            return call(**arguments)
        except (ClientError, BotoCoreError) as exc:
            raise FargateBuildError(f"ECS could not {action}: {_error_code(exc)}") from exc

    def _start_task(self, region: str, run_id: str) -> str:
        deadline = int(self._settings.timeout_seconds + DEADLINE_MARGIN_SECONDS)
        try:
            response = self._client.run_task(
                cluster=self._settings.cluster,
                taskDefinition=self._settings.task_definition,
                launchType="FARGATE",
                count=1,
                startedBy=STARTED_BY,
                clientToken=client_token(run_id),
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": list(self._settings.subnets),
                        "securityGroups": list(self._settings.security_groups),
                        # The subnets are public and have no NAT gateway, so the task needs its
                        # own IP.
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
                            "environment": [{"name": DEADLINE_ENV, "value": str(deadline)}],
                        }
                    ]
                },
            )
        except (ClientError, BotoCoreError) as exc:
            raise FargateBuildError(
                f"ECS did not start the Marked Trails task: {_error_code(exc)}"
            ) from exc
        failures = response.get("failures") or []
        if failures:
            reason = failures[0].get("reason", "no reason given")
            raise FargateBuildError(f"ECS did not start the Marked Trails task: {reason}")
        tasks = response.get("tasks") or []
        if len(tasks) != 1:
            raise FargateBuildError(f"ECS returned {len(tasks)} tasks for one Marked Trails run")
        return str(tasks[0]["taskArn"])

    def _wait_until_stopped(self, task_arn: str) -> dict[str, Any]:
        started = self._clock()
        deadline = started + self._settings.timeout_seconds
        errors = 0
        status = None
        while True:
            self._raise_if_stopped()
            delay = self._poll_seconds
            try:
                response = self._client.describe_tasks(
                    cluster=self._settings.cluster, tasks=[task_arn]
                )
            except (ClientError, BotoCoreError) as exc:
                if not _is_retryable(exc):
                    raise FargateBuildError(
                        f"ECS refused to describe the Marked Trails task: {_error_code(exc)}"
                    ) from exc
                errors += 1
                delay = min(self._poll_seconds * 2**errors, MAX_ERROR_BACKOFF_SECONDS)
                logger.warning(
                    "ECS DescribeTasks failed with %s; retrying in %g s", _error_code(exc), delay
                )
            else:
                errors = 0
                task = self._read_task(response, self._clock() - started, status is not None)
                if task is not None:
                    if task.get("lastStatus") == "STOPPED":
                        return task
                    if task.get("lastStatus") != status:
                        status = task.get("lastStatus")
                        logger.info("Marked Trails Fargate task %s is %s", task_arn, status)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise FargateBuildError(
                    f"Marked Trails Fargate task did not stop within "
                    f"{self._settings.timeout_seconds:g} s"
                )
            self._pause(min(delay, remaining))

    def _read_task(
        self, response: Mapping[str, Any], elapsed: float, seen: bool
    ) -> dict[str, Any] | None:
        failures = response.get("failures") or []
        if failures:
            reason = str(failures[0].get("reason", "no reason given"))
            if reason == "MISSING" and not seen and elapsed < MISSING_GRACE_SECONDS:
                logger.info("ECS does not list the new task yet (MISSING after %g s)", elapsed)
                return None
            raise FargateBuildError(f"ECS could not describe the Marked Trails task: {reason}")
        tasks = response.get("tasks") or []
        return dict(tasks[0]) if len(tasks) == 1 else None

    def _raise_if_stopped(self) -> None:
        if self._stop_requested:
            raise FargateBuildError("The Marked Trails build was stopped by Airflow")

    def _pause(self, seconds: float) -> None:
        end = self._clock() + seconds
        while not self._stop_requested:
            remaining = end - self._clock()
            if remaining <= 0:
                return
            self._sleep(min(SLEEP_SLICE_SECONDS, remaining))

    @staticmethod
    def _log_stream(task_arn: str) -> str:
        task_id = task_arn.rsplit("/", 1)[-1]
        return f"{LOG_STREAM_PREFIX}/{CONTAINER_NAME}/{task_id}"

    def _check_exit(self, task_arn: str, stream: str, task: dict[str, Any]) -> None:
        containers = [
            item for item in task.get("containers", []) if item.get("name") == CONTAINER_NAME
        ]
        container = containers[0] if containers else {}
        exit_code = container.get("exitCode")
        if exit_code == 0:
            return
        details = [
            str(value)
            for value in (container.get("reason"), task.get("stoppedReason"), task.get("stopCode"))
            if value
        ]
        reason = "; ".join(dict.fromkeys(details)) or "no stop reason given"
        raise FargateBuildError(
            f"Marked Trails Fargate task {task_arn} "
            f"{_describe_exit(exit_code, task.get('stopCode'))}: {reason} "
            f"(CloudWatch log stream {stream})"
        )


def _describe_exit(exit_code: object, stop_code: object) -> str:
    if exit_code == EXIT_DEADLINE:
        # timeout also exits 124 when a stop signal reaches it under an init process, so only
        # a task that ended by itself proves that the deadline expired.
        if stop_code == STOP_CODE_ESSENTIAL_EXITED:
            return (
                "exited with status 124: the in-container deadline expired and the build was "
                "stopped by timeout"
            )
        return (
            f"exited with status 124 after an external stop (stop code {stop_code or 'unknown'}), "
            "not necessarily the in-container deadline"
        )
    if exit_code == EXIT_KILLED:
        return (
            "exited with status 137: the build was killed with SIGKILL (out of memory, the "
            "timeout kill-after, or a task stop that outlasted its grace period)"
        )
    return f"exited with status {exit_code!r}"


def _batches(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
