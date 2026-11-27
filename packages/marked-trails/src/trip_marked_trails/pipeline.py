"""Builds one Marked Trails PMTiles file from a Geofabrik extract with osmium and Planetiler."""

from __future__ import annotations

import collections
import hashlib
import logging
import re
import shutil
import subprocess  # noqa: S404 - the CLI runs the osmium and Java tools it was packaged with
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

GEOFABRIK_BASE_URL = "https://download.geofabrik.de"
FILTER_EXPRESSION = "r/route=hiking,foot,bicycle,mtb"
PLANETILER_MAIN_CLASS = "com.tripanimator.markedtrails.MarkedTrailsMain"
PLANETILER_HEAP_OPTION = "-Xmx2g"
DEFAULT_PLANETILER_CLASSPATH = "/opt/marked-trails/classes:/app/libs/*"
DOWNLOAD_TIMEOUT_SECONDS = 3600.0
# Europe is estimated at 2 to 4 hours of Planetiler; the cap stops a hung tool, not a slow one.
TOOL_TIMEOUT_SECONDS = 6 * 3600.0
PMTILES_NAME = "marked-trails.pmtiles"
MARKED_TRAIL_COUNT_NAME = "marked-trail-count.txt"
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
class BuildTools:
    """Executables, classpath and time limit used by the build, defaulting to the builder image."""

    osmium: str = "osmium"
    java: str = "java"
    planetiler_classpath: str = DEFAULT_PLANETILER_CLASSPATH
    tool_timeout_seconds: float = TOOL_TIMEOUT_SECONDS


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
) -> BuildArtifact:
    """Filter and render one region; intermediate files are deleted on success.

    Without `extract_path` the latest Geofabrik extract is downloaded into `work_dir` and
    deleted after filtering. A given `extract_path` is read in place and never deleted.
    The filter must not omit referenced objects, so osmium reads the input from disk.
    """
    validate_region(region)
    work_dir.mkdir(parents=True, exist_ok=True)
    filtered = work_dir / "marked-trails-filtered.osm.pbf"
    pmtiles = work_dir / PMTILES_NAME
    count_file = work_dir / MARKED_TRAIL_COUNT_NAME
    tmp_dir = work_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if extract_path is None:
        extract = work_dir / "extract.osm.pbf"
        download_extract(region, extract)
    else:
        extract = extract_path
    osm_data_date = _osm_data_date(tools, extract)
    _run(
        [
            tools.osmium,
            "tags-filter",
            "--overwrite",
            "-o",
            str(filtered),
            str(extract),
            FILTER_EXPRESSION,
        ],
        tools.tool_timeout_seconds,
    )
    if extract_path is None:
        extract.unlink()

    _run(
        [
            tools.java,
            PLANETILER_HEAP_OPTION,
            "-cp",
            tools.planetiler_classpath,
            PLANETILER_MAIN_CLASS,
            f"--osm_path={filtered}",
            f"--output={pmtiles}",
            f"--tmpdir={tmp_dir}",
            f"--minzoom={TILE_MIN_ZOOM}",
            f"--maxzoom={TILE_MAX_ZOOM}",
            f"--marked_trail_count_path={count_file}",
            "--force",
        ],
        tools.tool_timeout_seconds,
    )
    filtered.unlink()
    shutil.rmtree(tmp_dir, ignore_errors=True)
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


def _osm_data_date(tools: BuildTools, extract: Path) -> str | None:
    args = [
        tools.osmium,
        "fileinfo",
        "-g",
        "header.option.osmosis_replication_timestamp",
        str(extract),
    ]
    try:
        result = subprocess.run(  # noqa: S603 - argument list built from validated values, no shell
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=tools.tool_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildToolError(f"{args[0]} timed out after {exc.timeout} s") from exc
    value = result.stdout.strip()
    return value or None


def _run(args: list[str], timeout_seconds: float) -> None:
    """Run a tool, logging each output line as it arrives so long builds show progress."""
    tool = Path(args[0]).name
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
