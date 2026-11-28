"""Runs the Marked Trails build-and-publish CLI inside an Airflow task."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

from trip_marked_trails.cli import main as marked_trails_main

DEFAULT_WORK_DIR = "/work/marked-trails"
POOL_NAME = "marked_trails_build"
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
) -> None:
    """Build and publish `region` through the CLI in this process; raise if it fails.

    The store comes from `MARKED_TRAILS_STORE` and `MARKED_TRAILS_S3_*` in the environment.
    Tools run capped at one CPU and 3 GB because Airflow shares a host without swap with the
    editor and the Strava sync. `work_dir` defaults to `MARKED_TRAILS_WORK_DIR`, a volume
    that must have `MIN_FREE_BYTES` free: the run fails before downloading anything otherwise.
    """
    directory = work_dir or Path(os.environ.get("MARKED_TRAILS_WORK_DIR") or DEFAULT_WORK_DIR)
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
