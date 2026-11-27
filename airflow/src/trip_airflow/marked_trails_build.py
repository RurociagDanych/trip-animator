"""Runs the Marked Trails build-and-publish CLI inside an Airflow task."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from trip_marked_trails.cli import main as marked_trails_main

DEFAULT_WORK_DIR = "/work/marked-trails"
POOL_NAME = "marked_trails_build"


def run_marked_trails_build(
    region: str,
    *,
    work_dir: Path | None = None,
    cli_main: Callable[[Sequence[str]], int] = marked_trails_main,
) -> None:
    """Build and publish `region` through the CLI in this process; raise if it fails.

    The store comes from `MARKED_TRAILS_STORE` and `MARKED_TRAILS_S3_*` in the environment.
    Tools run capped at one CPU and 3 GB because Airflow shares a host without swap with the
    editor and the Strava sync. `work_dir` defaults to `MARKED_TRAILS_WORK_DIR`, a volume
    that must hold the largest country extract.
    """
    directory = work_dir or Path(os.environ.get("MARKED_TRAILS_WORK_DIR") or DEFAULT_WORK_DIR)
    status = cli_main(
        ["build-publish", "--region", region, "--work-dir", str(directory), "--cap-resources"]
    )
    if status != 0:
        raise RuntimeError(f"Marked Trails build of {region} failed with status {status}")
