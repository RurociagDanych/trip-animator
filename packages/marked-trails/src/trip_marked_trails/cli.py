"""Command line entry point: build a Marked Trails region and publish it to the trail store."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from trip_marked_trails.pipeline import (
    DEFAULT_PLANETILER_CLASSPATH,
    EXTRACT_NAME,
    FILTERED_NAME,
    NODEMAP_STORAGE_CHOICES,
    NODEMAP_TYPE_CHOICES,
    PLANETILER_DIRECT_MEMORY_OPTION,
    PLANETILER_HEAP_OPTION,
    BuildArtifact,
    BuildTools,
    ResourceLimits,
    check_free_space,
    log_disk_usage,
    remove_build_scratch,
    run_build,
    validate_region,
)
from trip_marked_trails.publish import (
    DEFAULT_SIZE_BUDGET_BYTES,
    PointerWriteError,
    delete_superseded_builds,
    publish_build,
    read_pointer,
)
from trip_marked_trails.regional import SCRATCH_DIR_NAME, run_regional_build
from trip_marked_trails.trail_store import (
    DEFAULT_S3_PREFIX,
    TrailStoreSettings,
    build_trail_store,
)

logger = logging.getLogger("trip_marked_trails")

STORE_CHOICES = ("local", "s3")

BuildStep = Callable[[str, Path], BuildArtifact]

# The production host has 8 GB and no swap; osmium needs about 2.4 GB and Planetiler about 2.3 GB.
TOOL_MEMORY_LIMIT_BYTES = 3 * 1024**3
TOOL_CPU_COUNT = 1
# Estimated peak of a Europe build is 17 to 19 GB (the largest extract, the merged and filtered
# files, Planetiler's temporary files and the output) and the worst case about 25 GB. 24 GiB is
# about 25.8 GB, so the threshold sits above the worst case estimate with about 3% margin; it is
# an estimate and not a measurement, and a smaller volume is refused before any download.
MIN_FREE_BYTES = 24 * 1024**3
_JVM_SIZE_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def positive_int(value: str) -> int:
    """Parse an integer argument that must be at least 1."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than zero")
    return number


def jvm_size_bytes(option: str, prefix: str) -> int:
    """Return the bytes of a JVM size option such as `-Xmx9g`; raise `ValueError` if malformed."""
    value = option.removeprefix(prefix).lower()
    unit = value[-1:]
    digits = value[:-1] if unit in _JVM_SIZE_UNITS else value
    if not digits.isdigit() or int(digits) <= 0:
        raise ValueError(f"{option!r} must be {prefix} followed by a size such as 512m or 2g")
    return int(digits) * _JVM_SIZE_UNITS.get(unit, 1)


def jvm_option(prefix: str) -> Callable[[str], str]:
    """Return a parser for a JVM option that starts with `prefix`, such as `-Xmx`.

    A malformed option would only fail inside the JVM after the download, so it is rejected
    when the command starts.
    """

    def parse(value: str) -> str:
        if not value.startswith(prefix) or len(value) == len(prefix):
            raise argparse.ArgumentTypeError(f"{value!r} must start with {prefix} and a value")
        try:
            jvm_size_bytes(value, prefix)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the `trip-marked-trails` command."""
    parser = argparse.ArgumentParser(prog="trip-marked-trails")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser(
        "build-publish",
        help="Build a Geofabrik region and publish it to the trail store",
    )
    publish.add_argument(
        "--region", required=True, type=validate_region, help="e.g. europe/andorra"
    )
    publish.add_argument(
        "--extract",
        type=Path,
        default=None,
        help="Local .osm.pbf extract to build instead of downloading the region from Geofabrik",
    )
    publish.add_argument(
        "--store",
        default=os.environ.get("MARKED_TRAILS_STORE") or "local",
        help="Trail store backend; defaults to MARKED_TRAILS_STORE, then local",
    )
    publish.add_argument(
        "--store-dir",
        type=Path,
        default=os.environ.get("MARKED_TRAILS_STORE_DIR") or None,
        help="Local trail store root; defaults to MARKED_TRAILS_STORE_DIR",
    )
    publish.add_argument(
        "--s3-bucket",
        default=os.environ.get("MARKED_TRAILS_S3_BUCKET", ""),
        help="S3 bucket of the trail store; defaults to MARKED_TRAILS_S3_BUCKET",
    )
    publish.add_argument(
        "--s3-prefix",
        default=os.environ.get("MARKED_TRAILS_S3_PREFIX") or DEFAULT_S3_PREFIX,
        help="Key prefix inside the bucket; defaults to MARKED_TRAILS_S3_PREFIX, then "
        f"{DEFAULT_S3_PREFIX}",
    )
    publish.add_argument(
        "--s3-region",
        default=os.environ.get("AWS_REGION") or None,
        help="AWS region of the bucket; defaults to AWS_REGION, then the AWS chain",
    )
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
    publish.add_argument(
        "--size-budget-bytes",
        type=positive_int,
        default=os.environ.get("MARKED_TRAILS_SIZE_BUDGET_BYTES") or DEFAULT_SIZE_BUDGET_BYTES,
        help="Largest PMTiles file a build may have, in bytes; defaults to 1 GiB",
    )
    publish.add_argument(
        "--cap-resources",
        action="store_true",
        help="Run osmium and Planetiler niced, pinned to --tool-cpus CPUs and with a data memory "
        "cap of --tool-memory-bytes, for hosts where the CLI is not already limited by its "
        "container",
    )
    publish.add_argument(
        "--tool-memory-bytes",
        type=positive_int,
        default=os.environ.get("MARKED_TRAILS_TOOL_MEMORY_BYTES") or TOOL_MEMORY_LIMIT_BYTES,
        help="Data memory cap of each tool with --cap-resources, in bytes; defaults to "
        "MARKED_TRAILS_TOOL_MEMORY_BYTES, then 3 GiB",
    )
    publish.add_argument(
        "--tool-cpus",
        type=positive_int,
        default=os.environ.get("MARKED_TRAILS_TOOL_CPUS") or TOOL_CPU_COUNT,
        help="Lowest CPUs each tool is pinned to with --cap-resources; defaults to "
        "MARKED_TRAILS_TOOL_CPUS, then 1",
    )
    publish.add_argument(
        "--planetiler-heap",
        type=jvm_option("-Xmx"),
        default=os.environ.get("MARKED_TRAILS_PLANETILER_HEAP") or PLANETILER_HEAP_OPTION,
        help="Planetiler JVM heap option, for example -Xmx9g; defaults to "
        "MARKED_TRAILS_PLANETILER_HEAP, then -Xmx2g",
    )
    publish.add_argument(
        "--planetiler-direct-memory",
        type=jvm_option("-XX:MaxDirectMemorySize="),
        default=os.environ.get("MARKED_TRAILS_PLANETILER_DIRECT_MEMORY")
        or PLANETILER_DIRECT_MEMORY_OPTION,
        help="Planetiler JVM direct memory option, for example -XX:MaxDirectMemorySize=1g; "
        "defaults to MARKED_TRAILS_PLANETILER_DIRECT_MEMORY, then 512 MiB",
    )
    publish.add_argument(
        "--planetiler-nodemap-storage",
        choices=NODEMAP_STORAGE_CHOICES,
        default=os.environ.get("MARKED_TRAILS_PLANETILER_NODEMAP_STORAGE") or None,
        help="Where Planetiler keeps the node location map; defaults to "
        "MARKED_TRAILS_PLANETILER_NODEMAP_STORAGE, then Planetiler's own default (mmap). "
        "ram keeps it in the JVM heap, so size --planetiler-heap for it",
    )
    publish.add_argument(
        "--planetiler-nodemap-type",
        choices=NODEMAP_TYPE_CHOICES,
        default=os.environ.get("MARKED_TRAILS_PLANETILER_NODEMAP_TYPE") or None,
        help="Planetiler node location map type; defaults to "
        "MARKED_TRAILS_PLANETILER_NODEMAP_TYPE, then Planetiler's own default (sparsearray)",
    )
    publish.add_argument(
        "--min-free-bytes",
        type=positive_int,
        default=os.environ.get("MARKED_TRAILS_MIN_FREE_BYTES") or MIN_FREE_BYTES,
        help="Free space the work directory volume needs before the build starts, in bytes; "
        "defaults to MARKED_TRAILS_MIN_FREE_BYTES, then 24 GiB",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    build_step: BuildStep | None = None,
    s3_client: Any | None = None,
) -> int:
    """Run a command and return the process exit code.

    Order: check the pointer and delete the builds older than the current one, run the build,
    validate the local file, upload it under its own prefix, then overwrite `current.json`.
    Any failure returns 1. Before the pointer write `current.json` is left as it was; if the
    pointer write itself fails it may or may not have changed, and the log says so. An invalid
    pointer deletes nothing. A parent region such as `europe` is built one Geofabrik child extract
    at a time; a leaf region or a local `--extract` is built directly.

    With the s3 store, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are read
    from the environment and passed to the trail store client; with both empty the default
    AWS chain applies and exactly one of them set is an error.

    `build_step` replaces the osmium and Planetiler build in tests and `s3_client` replaces
    the boto3 client; publication policy is the same either way. The built file in the work
    directory is deleted once published.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    build_date = args.build_date or datetime.now(UTC).date()
    if os.environ.get("MARKED_TRAILS_S3_REGION", "").strip():
        parser.error("MARKED_TRAILS_S3_REGION was renamed to AWS_REGION; rename it in .env")
    if args.store not in STORE_CHOICES:
        parser.error(
            f"MARKED_TRAILS_STORE must be one of {', '.join(STORE_CHOICES)}, got {args.store!r}"
        )
    if args.store == "local" and args.store_dir is None:
        parser.error("--store-dir is required for the local store")
    if args.store == "s3":
        if args.store_dir is not None:
            parser.error("--store-dir cannot be used with --store s3")
        if not args.s3_bucket.strip():
            parser.error("--s3-bucket or MARKED_TRAILS_S3_BUCKET is required for the s3 store")
        if not args.s3_prefix.strip().strip("/"):
            parser.error("--s3-prefix or MARKED_TRAILS_S3_PREFIX must not be empty")
    try:
        store = build_trail_store(
            TrailStoreSettings(
                backend=args.store,
                store_dir=args.store_dir,
                s3_bucket=args.s3_bucket,
                s3_prefix=args.s3_prefix,
                s3_region=args.s3_region,
                s3_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
                s3_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            ),
            s3_client=s3_client,
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        step = build_step or _default_build_step(
            _build_tools(args), args.extract, args.min_free_bytes
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        pointer = read_pointer(store)
        for key in delete_superseded_builds(store, pointer):
            logger.info("Deleted superseded build object %s", key)
        artifact = step(args.region, args.work_dir)
        published = publish_build(
            store,
            region=args.region,
            build_date=build_date,
            artifact=artifact,
            previous=pointer,
            size_budget_bytes=args.size_budget_bytes,
        )
    except PointerWriteError:
        logger.exception(
            "Marked Trails build for %s failed while writing the pointer; "
            "the published pointer may have changed",
            args.region,
        )
        return 1
    except Exception:
        logger.exception(
            "Marked Trails build for %s failed; the published pointer is unchanged", args.region
        )
        return 1
    artifact.pmtiles_path.unlink(missing_ok=True)
    logger.info(
        "Published build %s for %s (%d bytes, %s Marked Trail relations, sha256 %s)",
        published.current_build_id,
        published.region,
        published.size_bytes,
        published.marked_trail_count,
        published.sha256,
    )
    return 0


def _build_tools(args: argparse.Namespace) -> BuildTools:
    limits = None
    if args.cap_resources:
        available = sorted(os.sched_getaffinity(0))
        if args.tool_cpus > len(available):
            raise ValueError(
                f"--tool-cpus is {args.tool_cpus} but this process may use only "
                f"{len(available)} CPUs"
            )
        heap = jvm_size_bytes(args.planetiler_heap, "-Xmx")
        direct = jvm_size_bytes(args.planetiler_direct_memory, "-XX:MaxDirectMemorySize=")
        if heap + direct >= args.tool_memory_bytes:
            raise ValueError(
                f"Planetiler heap and direct memory ({(heap + direct) / 1024**3:.1f} GiB) "
                f"must be below --tool-memory-bytes ({args.tool_memory_bytes / 1024**3:.1f} GiB), "
                "which also holds the JVM's own memory"
            )
        limits = ResourceLimits(
            memory_bytes=args.tool_memory_bytes, cpus=tuple(available[: args.tool_cpus])
        )
    return BuildTools(
        planetiler_classpath=args.planetiler_classpath,
        planetiler_heap_option=args.planetiler_heap,
        planetiler_direct_memory_option=args.planetiler_direct_memory,
        planetiler_nodemap_storage=args.planetiler_nodemap_storage,
        planetiler_nodemap_type=args.planetiler_nodemap_type,
        limits=limits,
    )


def _remove_stale_scratch(work_dir: Path, *, remove_extract: bool) -> None:
    shutil.rmtree(work_dir / SCRATCH_DIR_NAME, ignore_errors=True)
    remove_build_scratch(work_dir, keep_outputs=False)
    (work_dir / FILTERED_NAME).unlink(missing_ok=True)
    if remove_extract:
        (work_dir / EXTRACT_NAME).unlink(missing_ok=True)


def _default_build_step(
    tools: BuildTools, extract_path: Path | None, min_free_bytes: int
) -> BuildStep:
    def step(region: str, work_dir: Path) -> BuildArtifact:
        work_dir.mkdir(parents=True, exist_ok=True)
        # A killed run can leave up to about 25 GB of scratch that would fail the check below.
        _remove_stale_scratch(work_dir, remove_extract=extract_path is None)
        check_free_space(work_dir, min_free_bytes)
        log_disk_usage(work_dir, "start")
        if extract_path is not None:
            return run_build(region, work_dir, tools, extract_path=extract_path)
        return run_regional_build(region, work_dir, tools)

    return step


def run() -> None:
    """Console script entry point: configure logging and exit with the status of `main`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(main())
