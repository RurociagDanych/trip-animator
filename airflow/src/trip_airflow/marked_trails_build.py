"""Runs the Marked Trails build-and-publish CLI in an Airflow task, locally or on ECS Fargate."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from trip_marked_trails.cli import main as marked_trails_main

from trip_airflow.marked_trails_fargate import FargateBuildSettings, run_fargate_build

DEFAULT_WORK_DIR = "/work/marked-trails"
POOL_NAME = "marked_trails_build"
BACKEND_ENV = "MARKED_TRAILS_BUILD_BACKEND"
LOCAL_BACKEND = "local"
FARGATE_BACKEND = "fargate"
# The largest extract is France at 5.1 GB; with the filtered files, Planetiler's working files and
# the PMTiles output the peak is about 10 GB.
MIN_FREE_BYTES = 12 * 1024**3


def _free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def run_marked_trails_build(
    region: str,
    *,
    work_dir: Path | None = None,
    cli_main: Callable[[Sequence[str]], int] = marked_trails_main,
    free_bytes: Callable[[Path], int] = _free_bytes,
    environ: Mapping[str, str] | None = None,
    fargate_build: Callable[[str, FargateBuildSettings], None] = run_fargate_build,
) -> None:
    """Build and publish `region`; raise if the build fails.

    `MARKED_TRAILS_BUILD_BACKEND` picks the backend: `local` (the default) runs the CLI in this
    process, and `fargate` runs it as an ECS task and waits for it, see `marked_trails_fargate`.

    The local store comes from `MARKED_TRAILS_STORE` and `MARKED_TRAILS_S3_*`; tools run capped at
    one CPU and 3 GB by the CLI defaults, because Airflow shares a host without swap with the
    editor and the Strava sync. `work_dir` defaults to `MARKED_TRAILS_WORK_DIR`, a volume that
    must have `MIN_FREE_BYTES` free: the local run fails before downloading anything otherwise.
    The Fargate task has its own disk and is not checked here.
    """
    env = os.environ if environ is None else environ
    backend = (env.get(BACKEND_ENV) or LOCAL_BACKEND).strip()
    if backend == FARGATE_BACKEND:
        fargate_build(region, FargateBuildSettings.from_environment(env))
        return
    if backend != LOCAL_BACKEND:
        raise ValueError(
            f"{BACKEND_ENV} must be {LOCAL_BACKEND} or {FARGATE_BACKEND}, got {backend!r}"
        )
    directory = work_dir or Path(env.get("MARKED_TRAILS_WORK_DIR") or DEFAULT_WORK_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    free = free_bytes(directory)
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"{directory} has {free / 1024**3:.1f} GiB free; a Marked Trails build needs 12 GiB"
        )
    status = cli_main(
        ["build-publish", "--region", region, "--work-dir", str(directory), "--cap-resources"]
    )
    if status != 0:
        raise RuntimeError(f"Marked Trails build of {region} failed with status {status}")
