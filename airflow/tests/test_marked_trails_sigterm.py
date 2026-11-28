from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = r"""
import json, os, signal, sys, time

from trip_airflow import marked_trails_build as build
from trip_airflow.marked_trails_fargate import FargateBuild, FargateBuildSettings

marker = sys.argv[1]
ARN = "arn:aws:ecs:us-east-1:111122223333:task/cluster-a/abc"


class Ecs:
    def list_tasks(self, **kwargs):
        return {"taskArns": []}

    def run_task(self, **kwargs):
        return {"tasks": [{"taskArn": ARN}], "failures": []}

    def describe_tasks(self, **kwargs):
        print("RUNNING", flush=True)
        return {"tasks": [{"taskArn": ARN, "lastStatus": "RUNNING"}], "failures": []}


class Stopper:
    def stop_task(self, **kwargs):
        time.sleep(0.5)
        with open(marker, "w") as fh:
            json.dump({**kwargs, "at": time.monotonic()}, fh)


settings = FargateBuildSettings("us-east-1", "cluster-a", "family", ("s",), ("g",), 3600.0)
build.fargate_build_from_environment = lambda env: FargateBuild(
    settings, ecs_client=Ecs(), stop_client=Stopper(), poll_seconds=30
)
operator = build.MarkedTrailsBuildOperator(task_id="t", region="europe")
# The handler Airflow 3 installs in task_runner._execute_task: it calls on_kill and returns.
signal.signal(signal.SIGTERM, lambda signum, frame: operator.on_kill())
os.environ["MARKED_TRAILS_BUILD_BACKEND"] = "fargate"


class TaskInstance:
    id = "11111111-2222-3333-4444-555555555555"
    try_number = 1


try:
    operator.execute({"ti": TaskInstance()})
except Exception as exc:
    print("RAISED", type(exc).__name__, exc, flush=True)
"""


def test_sigterm_stops_the_ecs_task_inside_airflows_five_second_window(tmp_path: Path) -> None:
    marker = tmp_path / "stopped.json"
    process = subprocess.Popen(  # noqa: S603 - fixed argument list, no shell
        [sys.executable, "-c", SCRIPT, str(marker)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if line.strip() == "RUNNING":
                break
        else:
            raise AssertionError("the launcher never reached the wait loop")

        sent_at = time.monotonic()
        os.kill(process.pid, signal.SIGTERM)
        deadline = sent_at + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        stopped_after = time.monotonic() - sent_at
        remaining_output = process.communicate(timeout=10)[0]
    finally:
        process.kill()
        process.wait()

    assert marker.exists(), "the ECS task was not stopped within 5 s of SIGTERM"
    assert stopped_after < 5.0
    assert "RAISED FargateBuildError" in remaining_output
    assert "stopped by Airflow" in remaining_output
