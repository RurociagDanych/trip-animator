"""Builds one Marked Trails PMTiles file from a Geofabrik extract with osmium and Planetiler."""

from __future__ import annotations

import collections
import hashlib
import logging
import os
import re
import shutil
import subprocess  # noqa: S404 - the CLI runs the osmium and Java tools it was packaged with
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

GEOFABRIK_BASE_URL = "https://download.geofabrik.de"
FILTER_EXPRESSION = "r/route=hiking,foot,bicycle,mtb"
PLANETILER_MAIN_CLASS = "com.tripanimator.markedtrails.MarkedTrailsMain"
PLANETILER_HEAP_OPTION = "-Xmx2g"
# Direct buffers count as data memory for prlimit but not for -Xmx; without a cap they could
# push the JVM past the 3 GB limit on top of the 2 GB heap.
PLANETILER_DIRECT_MEMORY_OPTION = "-XX:MaxDirectMemorySize=512m"
DEFAULT_PLANETILER_CLASSPATH = "/opt/marked-trails/classes:/app/libs/*"
# Planetiler's own names; with none given it uses a memory-mapped sparse array, which is what the
# VPS has always run.
NODEMAP_STORAGE_CHOICES = ("mmap", "ram", "direct")
NODEMAP_TYPE_CHOICES = ("sparsearray", "sortedtable", "array")
DOWNLOAD_TIMEOUT_SECONDS = 3600.0
# Europe is estimated at 2 to 4 hours of Planetiler; the cap stops a hung tool, not a slow one.
TOOL_TIMEOUT_SECONDS = 6 * 3600.0
PMTILES_NAME = "marked-trails.pmtiles"
MARKED_TRAIL_COUNT_NAME = "marked-trail-count.txt"
PLANETILER_TMP_NAME = "tmp"
# osmium reads ahead into queues whose default size makes `merge` grow by 48 to 90 MiB per input
# file, so a merge of dozens of inputs ran out of the memory cap; small queues lower the peak.
OSMIUM_QUEUE_ENVIRONMENT = (
    "OSMIUM_MAX_INPUT_QUEUE_SIZE=2",
    "OSMIUM_MAX_OSMDATA_QUEUE_SIZE=2",
)
# Must match MarkedTrailsProfile, which narrows zooms per feature.
TILE_MIN_ZOOM = 5
TILE_MAX_ZOOM = 13
_REGION_PATTERN = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")
_OUTPUT_TAIL_LINES = 40

logger = logging.getLogger(__name__)


class BuildToolError(RuntimeError):
    """A download or build tool failed; the message carries the tail of its output."""


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    """Result of a build step: the PMTiles file to publish and what it was built from."""

    pmtiles_path: Path
    osm_data_date: str | None
    marked_trail_count: int


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Caps for one tool process: `memory_bytes` of data memory, a `nice` level, and CPUs.

    `cpus` is the set of CPU numbers the process is pinned to; None pins to the lowest CPU
    the process may use.
    """

    memory_bytes: int
    nice: int = 10
    cpus: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class BuildTools:
    """Executables, classpath, time, JVM options and resource limits used by the build.

    Defaults match the builder image on the production VPS. Without `limits` tools run
    uncapped, which suits a container that Docker or Fargate already limits.
    """

    osmium: str = "osmium"
    java: str = "java"
    planetiler_classpath: str = DEFAULT_PLANETILER_CLASSPATH
    planetiler_heap_option: str = PLANETILER_HEAP_OPTION
    planetiler_direct_memory_option: str = PLANETILER_DIRECT_MEMORY_OPTION
    planetiler_nodemap_storage: str | None = None
    planetiler_nodemap_type: str | None = None
    tool_timeout_seconds: float = TOOL_TIMEOUT_SECONDS
    limits: ResourceLimits | None = None


class ToolRunner(Protocol):
    """Runs the external tools of a build; tests replace it with a fake."""

    def run(self, args: Sequence[str]) -> None:
        """Run a tool to completion and raise `BuildToolError` if it fails."""
        ...

    def capture(self, args: Sequence[str]) -> str:
        """Run a tool and return its standard output."""
        ...


Downloader = Callable[[str, Path], None]


def directory_size(directory: Path) -> int:
    """Return the bytes of the regular files below `directory`, ignoring files that vanish."""
    total = 0
    for root, _dirs, names in os.walk(directory):
        for name in names:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def log_disk_usage(work_dir: Path, stage: str) -> None:
    """Log the free space of the volume and the size of `work_dir` after `stage`.

    A full disk is the failure that ended the first Europe run, so every stage leaves a line
    that shows how close the build came.
    """
    free = shutil.disk_usage(work_dir).free
    logger.info(
        "Disk after %s: %.1f GiB free, work dir %.1f GiB",
        stage,
        free / 1024**3,
        directory_size(work_dir) / 1024**3,
    )


def check_free_space(directory: Path, minimum_bytes: int) -> None:
    """Raise `BuildToolError` unless the volume of `directory` has `minimum_bytes` free.

    Both build backends call this before anything is downloaded, so a full volume fails in
    seconds and not hours into the build.
    """
    free = shutil.disk_usage(directory).free
    if free < minimum_bytes:
        raise BuildToolError(
            f"{directory} has {free / 1024**3:.1f} GiB free; "
            f"a Marked Trails build needs {minimum_bytes / 1024**3:.1f} GiB"
        )


def limited_command(args: Sequence[str], limits: ResourceLimits | None) -> list[str]:
    """Wrap a command so it runs niced, pinned to its CPUs and with a data memory cap.

    This must work in the Airflow container: it is unprivileged, has no capabilities and a
    read-only root, so cgroups and `docker run` limits are unavailable. The Fargate task
    uses the same wrapper, so a runaway tool fails with an allocation error instead of
    taking the whole task down. `prlimit --data` (RLIMIT_DATA) counts heap and private
    anonymous mappings, which is the memory osmium and the Planetiler JVM use, so a tool
    that needs more fails with an allocation error instead of taking the host's memory.
    RLIMIT_AS would also count the JVM's reserved address space and
    RLIMIT_RSS is not enforced by Linux. The JVM heap is capped separately by -Xmx.

    `setpriv --pdeathsig KILL` makes the kernel kill the tool when the process that started it
    dies, so a task killed by Airflow does not leave a multi-hour tool running. Every wrapper
    execs the next one, so the signal is armed on the tool process itself.

    osmium additionally gets `OSMIUM_QUEUE_ENVIRONMENT` through `env`, which also execs.
    """
    if limits is None:
        return list(args)
    cpus = limits.cpus if limits.cpus is not None else (min(os.sched_getaffinity(0)),)
    environment = ["env", *OSMIUM_QUEUE_ENVIRONMENT] if Path(args[0]).name == "osmium" else []
    return [
        "nice",
        "-n",
        str(limits.nice),
        "prlimit",
        f"--data={limits.memory_bytes}",
        "taskset",
        "-c",
        ",".join(str(cpu) for cpu in cpus),
        "setpriv",
        "--pdeathsig",
        "KILL",
        *environment,
        *args,
    ]


class SubprocessToolRunner:
    """`ToolRunner` that starts real processes with the limits of `BuildTools`."""

    def __init__(self, tools: BuildTools) -> None:
        self._tools = tools

    def run(self, args: Sequence[str]) -> None:
        _run(
            limited_command(args, self._tools.limits),
            self._tools.tool_timeout_seconds,
            tool=Path(args[0]).name,
        )

    def capture(self, args: Sequence[str]) -> str:
        return _capture(
            limited_command(args, self._tools.limits),
            self._tools.tool_timeout_seconds,
            tool=Path(args[0]).name,
        )


def validate_region(region: str) -> str:
    """Return the region unchanged if it is a Geofabrik path such as `europe/andorra`."""
    if not _REGION_PATTERN.fullmatch(region):
        raise ValueError(f"Invalid Geofabrik region: {region!r}")
    return region


def extract_url(region: str) -> str:
    """Return the Geofabrik download URL of the latest extract for `region`."""
    return f"{GEOFABRIK_BASE_URL}/{validate_region(region)}-latest.osm.pbf"


def download_extract(region: str, dest: Path, *, client: httpx.Client | None = None) -> None:
    """Stream the latest Geofabrik extract for `region` to `dest` and verify its md5.

    The checksum is read from the URL the download was redirected to, so it belongs to the
    same dated file even if Geofabrik publishes a new extract meanwhile. Raises
    `BuildToolError` on a mismatch and leaves no file behind.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    owned_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    try:
        digest = hashlib.md5(usedforsecurity=False)
        with http.stream("GET", extract_url(region)) as response:
            response.raise_for_status()
            final_url = str(response.url)
            with partial.open("wb") as fh:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    digest.update(chunk)
                    fh.write(chunk)
        checksum = http.get(f"{final_url}.md5")
        checksum.raise_for_status()
        expected = checksum.text.split()[0].lower() if checksum.text.split() else ""
        if digest.hexdigest() != expected:
            raise BuildToolError(
                f"md5 of {final_url} is {digest.hexdigest()}, Geofabrik publishes {expected!r}"
            )
        partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)
        if owned_client:
            http.close()


def run_build(
    region: str,
    work_dir: Path,
    tools: BuildTools,
    *,
    extract_path: Path | None = None,
    runner: ToolRunner | None = None,
    downloader: Downloader | None = None,
) -> BuildArtifact:
    """Filter and render one region; intermediate files are deleted on success and on failure.

    Without `extract_path` the latest Geofabrik extract is downloaded into `work_dir` and
    deleted after filtering. A given `extract_path` is read in place and never deleted.
    The filter must not omit referenced objects, so osmium reads the input from disk. After a
    failure only the files of the caller remain: no PMTiles output, count file or temporary
    file is left in `work_dir`. Files of a killed earlier run are removed at the start.
    """
    validate_region(region)
    work_dir.mkdir(parents=True, exist_ok=True)
    runner = runner or SubprocessToolRunner(tools)
    filtered = work_dir / "marked-trails-filtered.osm.pbf"
    owns_extract = extract_path is None
    extract = extract_path or work_dir / "extract.osm.pbf"
    if owns_extract:
        extract.unlink(missing_ok=True)
    filtered.unlink(missing_ok=True)
    remove_build_scratch(work_dir, keep_outputs=False)
    succeeded = False
    try:
        if owns_extract:
            (downloader or download_extract)(region, extract)
            log_disk_usage(work_dir, "download")
        osm_data_date = filter_extract(extract, filtered, tools, runner)
        if owns_extract:
            extract.unlink()
        log_disk_usage(work_dir, "filter")
        artifact = build_tiles(filtered, work_dir, osm_data_date, tools, runner)
        succeeded = True
        return artifact
    finally:
        if owns_extract:
            extract.unlink(missing_ok=True)
        filtered.unlink(missing_ok=True)
        remove_build_scratch(work_dir, keep_outputs=succeeded)


def remove_build_scratch(work_dir: Path, *, keep_outputs: bool) -> None:
    """Delete Planetiler's temporary files in `work_dir`, and its outputs unless `keep_outputs`."""
    shutil.rmtree(work_dir / PLANETILER_TMP_NAME, ignore_errors=True)
    if not keep_outputs:
        (work_dir / PMTILES_NAME).unlink(missing_ok=True)
        (work_dir / MARKED_TRAIL_COUNT_NAME).unlink(missing_ok=True)


def filter_extract(
    extract: Path, filtered: Path, tools: BuildTools, runner: ToolRunner
) -> str | None:
    """Keep only the Marked Trail relations of `extract` in `filtered`; return its data date."""
    osm_data_date = _osm_data_date(tools, extract, runner)
    runner.run(
        [
            tools.osmium,
            "tags-filter",
            "--overwrite",
            "-o",
            str(filtered),
            str(extract),
            FILTER_EXPRESSION,
        ]
    )
    return osm_data_date


def build_tiles(
    filtered: Path,
    work_dir: Path,
    osm_data_date: str | None,
    tools: BuildTools,
    runner: ToolRunner,
) -> BuildArtifact:
    """Run Planetiler on a filtered file and delete it; the PMTiles file lands in `work_dir`."""
    pmtiles = work_dir / PMTILES_NAME
    count_file = work_dir / MARKED_TRAIL_COUNT_NAME
    tmp_dir = work_dir / PLANETILER_TMP_NAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    nodemap_arguments = []
    if tools.planetiler_nodemap_storage is not None:
        nodemap_arguments.append(f"--nodemap-storage={tools.planetiler_nodemap_storage}")
    if tools.planetiler_nodemap_type is not None:
        nodemap_arguments.append(f"--nodemap-type={tools.planetiler_nodemap_type}")
    runner.run(
        [
            tools.java,
            tools.planetiler_heap_option,
            tools.planetiler_direct_memory_option,
            "-cp",
            tools.planetiler_classpath,
            PLANETILER_MAIN_CLASS,
            f"--osm_path={filtered}",
            f"--output={pmtiles}",
            f"--tmpdir={tmp_dir}",
            f"--minzoom={TILE_MIN_ZOOM}",
            f"--maxzoom={TILE_MAX_ZOOM}",
            f"--marked_trail_count_path={count_file}",
            *nodemap_arguments,
            "--force",
        ]
    )
    filtered.unlink()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    log_disk_usage(work_dir, "planetiler")
    if not pmtiles.is_file() or pmtiles.stat().st_size == 0:
        raise BuildToolError(f"Planetiler produced no PMTiles output at {pmtiles}")
    marked_trail_count = _read_marked_trail_count(count_file)
    count_file.unlink()
    return BuildArtifact(
        pmtiles_path=pmtiles,
        osm_data_date=osm_data_date,
        marked_trail_count=marked_trail_count,
    )


def _read_marked_trail_count(count_file: Path) -> int:
    try:
        return int(count_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise BuildToolError(f"Planetiler wrote no Marked Trail count at {count_file}") from exc


def _osm_data_date(tools: BuildTools, extract: Path, runner: ToolRunner) -> str | None:
    return _header_option(tools, extract, runner, "osmosis_replication_timestamp")


def _header_option(tools: BuildTools, extract: Path, runner: ToolRunner, option: str) -> str | None:
    value = runner.capture(
        [tools.osmium, "fileinfo", "-g", f"header.option.{option}", str(extract)]
    ).strip()
    return value or None


def _capture(args: Sequence[str], timeout_seconds: float, *, tool: str) -> str:
    """Run a short tool and return its stdout; a missing timestamp is not an error."""
    try:
        result = subprocess.run(  # noqa: S603 - argument list built from validated values, no shell
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildToolError(f"{tool} timed out after {exc.timeout:g} s") from exc
    return result.stdout


def _run(args: Sequence[str], timeout_seconds: float, *, tool: str) -> None:
    """Run a tool, logging each output line as it arrives so long builds show progress."""
    tail: collections.deque[str] = collections.deque(maxlen=_OUTPUT_TAIL_LINES)
    process = subprocess.Popen(  # noqa: S603 - argument list built from validated values, no shell
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    timed_out = threading.Event()

    def kill_on_timeout() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(timeout_seconds, kill_on_timeout)
    timer.start()
    try:
        assert process.stdout is not None  # noqa: S101 - stdout=PIPE always sets it
        for line in process.stdout:
            text = line.rstrip()
            tail.append(text)
            logger.info("%s: %s", tool, text)
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
    if timed_out.is_set():
        raise BuildToolError(f"{tool} timed out after {timeout_seconds:g} s")
    if returncode != 0:
        output = "\n".join(tail)
        raise BuildToolError(f"{tool} exited with status {returncode}: {output}")
