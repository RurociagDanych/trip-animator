"""Runs the Marked Trails build-and-publish CLI in an Airflow task, locally or on ECS Fargate."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from airflow.sdk import BaseOperator
from trip_marked_trails.cli import main as marked_trails_main

from trip_airflow.marked_trails_fargate import FargateBuild, FargateBuildSettings

DEFAULT_WORK_DIR = "/work/marked-trails"
POOL_NAME = "marked_trails_build"
BACKEND_ENV = "MARKED_TRAILS_BUILD_BACKEND"
LOCAL_BACKEND = "local"
FARGATE_BACKEND = "fargate"


def fargate_build_from_environment(environ: Mapping[str, str]) -> FargateBuild | None:
    """Return the Fargate build for `environ`, or None when the backend is not `fargate`."""
    if _backend(environ) != FARGATE_BACKEND:
        return None
    return FargateBuild(FargateBuildSettings.from_environment(environ))


def _backend(environ: Mapping[str, str]) -> str:
    backend = (environ.get(BACKEND_ENV) or LOCAL_BACKEND).strip()
    if backend not in (LOCAL_BACKEND, FARGATE_BACKEND):
        raise ValueError(
            f"{BACKEND_ENV} must be {LOCAL_BACKEND} or {FARGATE_BACKEND}, got {backend!r}"
        )
    return backend


def run_marked_trails_build(
    region: str,
    *,
    work_dir: Path | None = None,
    cli_main: Callable[[Sequence[str]], int] = marked_trails_main,
    environ: Mapping[str, str] | None = None,
    fargate_build: FargateBuild | None = None,
    run_id: str = "",
) -> None:
    """Build and publish `region`; raise if the build fails.

    `MARKED_TRAILS_BUILD_BACKEND` picks the backend: `local` (the default) runs the CLI in this
    process, and `fargate` runs it as an ECS task and waits for it, see `marked_trails_fargate`.
    `fargate_build` and `run_id` (the Airflow task instance id and try number) are used only by
    the fargate backend; the operator passes its own `FargateBuild` so that `on_kill` can stop it.

    The local store comes from `MARKED_TRAILS_STORE` and `MARKED_TRAILS_S3_*`; tools run capped at
    one CPU and 3 GB by the CLI defaults, because Airflow shares a host without swap with the
    editor and the Strava sync. `work_dir` defaults to `MARKED_TRAILS_WORK_DIR`. The CLI checks
    its free space before downloading anything, on both backends.
    """
    env = os.environ if environ is None else environ
    if _backend(env) == FARGATE_BACKEND:
        build = fargate_build or FargateBuild(FargateBuildSettings.from_environment(env))
        build.run(region, run_id)
        return
    directory = work_dir or Path(env.get("MARKED_TRAILS_WORK_DIR") or DEFAULT_WORK_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    status = cli_main(
        ["build-publish", "--region", region, "--work-dir", str(directory), "--cap-resources"]
    )
    if status != 0:
        raise RuntimeError(f"Marked Trails build of {region} failed with status {status}")


class MarkedTrailsBuildOperator(BaseOperator):
    """Runs one Marked Trails build and stops its Fargate task when Airflow kills the task.

    Airflow calls `on_kill` from its SIGTERM handler and from the execution timeout, and
    neither raises into `execute`, so the stop cannot live in a `finally` block.
    """

    template_fields = ("region",)

    def __init__(self, *, region: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.region = region
        self._fargate_build: FargateBuild | None = None

    def execute(self, context: Any) -> None:
        """Run the build on the configured backend."""
        task_instance = context["ti"]
        self._fargate_build = fargate_build_from_environment(os.environ)
        run_marked_trails_build(
            self.region,
            fargate_build=self._fargate_build,
            run_id=f"{task_instance.id}-{task_instance.try_number}",
        )

    def on_kill(self) -> None:
        """Stop the Fargate task, if one is running, within Airflow's SIGTERM grace period."""
        if self._fargate_build is not None:
            self._fargate_build.stop()
