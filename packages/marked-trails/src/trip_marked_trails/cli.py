"""Command line entry point: build a Marked Trails region and publish it to the trail store."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from trip_object_store import ObjectStoreConfig, build_object_store

from trip_marked_trails.pipeline import (
    DEFAULT_PLANETILER_CLASSPATH,
    BuildArtifact,
    BuildTools,
    run_build,
    validate_region,
)
from trip_marked_trails.publish import publish_build

logger = logging.getLogger("trip_marked_trails")

BuildStep = Callable[[str, Path], BuildArtifact]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trip-marked-trails")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser(
        "build-publish",
        help="Build a Geofabrik region and publish it to the trail store",
    )
    publish.add_argument(
        "--region", required=True, type=validate_region, help="e.g. europe/andorra"
    )
    publish.add_argument("--store-dir", required=True, type=Path, help="Local trail store root")
    publish.add_argument(
        "--work-dir", required=True, type=Path, help="Scratch directory for the build"
    )
    publish.add_argument(
        "--build-date",
        type=date.fromisoformat,
        default=None,
        help="Build date as YYYY-MM-DD; defaults to today in UTC",
    )
    publish.add_argument(
        "--planetiler-classpath",
        default=os.environ.get("MARKED_TRAILS_PLANETILER_CLASSPATH", DEFAULT_PLANETILER_CLASSPATH),
        help="Classpath with the compiled Marked Trails profile and Planetiler jars",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, build_step: BuildStep | None = None) -> int:
    """Run a command and return the process exit code.

    `build_step` replaces the osmium and Planetiler build in tests; publication policy is
    the same either way.
    """
    args = build_parser().parse_args(argv)
    build_date = args.build_date or datetime.now(UTC).date()
    store = build_object_store(ObjectStoreConfig(backend="local", local_dir=args.store_dir))
    step = build_step or _default_build_step(args.planetiler_classpath)
    try:
        artifact = step(args.region, args.work_dir)
        pointer = publish_build(
            store,
            region=args.region,
            build_date=build_date,
            artifact=artifact,
        )
    except Exception:
        logger.exception(
            "Marked Trails build for %s failed; the published pointer is unchanged", args.region
        )
        return 1
    logger.info(
        "Published build %s for %s (%d bytes, sha256 %s)",
        pointer.current_build_id,
        pointer.region,
        pointer.size_bytes,
        pointer.sha256,
    )
    return 0


def _default_build_step(planetiler_classpath: str) -> BuildStep:
    tools = BuildTools(planetiler_classpath=planetiler_classpath)

    def step(region: str, work_dir: Path) -> BuildArtifact:
        return run_build(region, work_dir, tools)

    return step


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(main())
