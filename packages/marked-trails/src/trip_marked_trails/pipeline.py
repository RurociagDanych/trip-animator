"""Builds one Marked Trails PMTiles file from a Geofabrik extract with osmium and Planetiler."""

from __future__ import annotations

import re
import shutil
import subprocess  # noqa: S404 - the CLI runs the osmium and Java tools it was packaged with
from dataclasses import dataclass
from pathlib import Path

import httpx

GEOFABRIK_BASE_URL = "https://download.geofabrik.de"
FILTER_EXPRESSION = "r/route=hiking,foot,bicycle,mtb"
PLANETILER_MAIN_CLASS = "com.tripanimator.markedtrails.MarkedTrailsMain"
PLANETILER_HEAP_OPTION = "-Xmx2g"
DEFAULT_PLANETILER_CLASSPATH = "/opt/marked-trails/classes:/app/libs/*"
DOWNLOAD_TIMEOUT_SECONDS = 3600.0
PMTILES_NAME = "marked-trails.pmtiles"
# Must match MarkedTrailsProfile, which narrows zooms per feature.
TILE_MIN_ZOOM = 5
TILE_MAX_ZOOM = 13
_REGION_PATTERN = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")
_STDERR_TAIL_CHARS = 2000


class BuildToolError(RuntimeError):
    """A download or build tool failed; the message carries the tail of its stderr."""


@dataclass(frozen=True, slots=True)
class BuildArtifact:
    """Result of a build step: the PMTiles file to publish and the OSM data date, if known."""

    pmtiles_path: Path
    osm_data_date: str | None


@dataclass(frozen=True, slots=True)
class BuildTools:
    """Executables and classpath used by the build, defaulting to the builder image layout."""

    osmium: str = "osmium"
    java: str = "java"
    planetiler_classpath: str = DEFAULT_PLANETILER_CLASSPATH


def validate_region(region: str) -> str:
    """Return the region unchanged if it is a Geofabrik path such as `europe/andorra`."""
    if not _REGION_PATTERN.fullmatch(region):
        raise ValueError(f"Invalid Geofabrik region: {region!r}")
    return region


def extract_url(region: str) -> str:
    return f"{GEOFABRIK_BASE_URL}/{validate_region(region)}-latest.osm.pbf"


def download_extract(region: str, dest: Path) -> None:
    """Stream the latest Geofabrik extract for `region` to `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    with (
        httpx.Client(follow_redirects=True, timeout=DOWNLOAD_TIMEOUT_SECONDS) as client,
        client.stream("GET", extract_url(region)) as response,
    ):
        response.raise_for_status()
        with partial.open("wb") as fh:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                fh.write(chunk)
    partial.replace(dest)


def run_build(region: str, work_dir: Path, tools: BuildTools) -> BuildArtifact:
    """Download, filter and render one region; intermediate extracts are deleted on success.

    The filter must not omit referenced objects, so osmium reads the input from disk.
    """
    validate_region(region)
    work_dir.mkdir(parents=True, exist_ok=True)
    extract = work_dir / "extract.osm.pbf"
    filtered = work_dir / "marked-trails-filtered.osm.pbf"
    pmtiles = work_dir / PMTILES_NAME
    tmp_dir = work_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    download_extract(region, extract)
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
        ]
    )
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
            "--force",
        ]
    )
    filtered.unlink()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if not pmtiles.is_file() or pmtiles.stat().st_size == 0:
        raise BuildToolError(f"Planetiler produced no PMTiles output at {pmtiles}")
    return BuildArtifact(pmtiles_path=pmtiles, osm_data_date=osm_data_date)


def _osm_data_date(tools: BuildTools, extract: Path) -> str | None:
    result = _run(
        [
            tools.osmium,
            "fileinfo",
            "-g",
            "header.option.osmosis_replication_timestamp",
            str(extract),
        ],
        check=False,
    )
    value = result.stdout.strip()
    return value or None


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - argument list built from validated values, no shell
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        tail = (result.stderr or "")[-_STDERR_TAIL_CHARS:]
        raise BuildToolError(f"{args[0]} exited with status {result.returncode}: {tail}")
    return result
