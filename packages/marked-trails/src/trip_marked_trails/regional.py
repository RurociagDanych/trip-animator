"""Builds a parent Geofabrik region one child extract at a time to bound disk use."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import httpx

from trip_marked_trails.pipeline import (
    GEOFABRIK_BASE_URL,
    BuildArtifact,
    BuildToolError,
    BuildTools,
    Downloader,
    SubprocessToolRunner,
    ToolRunner,
    build_tiles,
    download_extract,
    filter_extract,
    run_build,
    validate_region,
)

GEOFABRIK_INDEX_URL = f"{GEOFABRIK_BASE_URL}/index-v1.json"
SCRATCH_DIR_NAME = "regional"
MERGED_NAME = "marked-trails-merged.osm.pbf"
# Geofabrik also publishes extracts that cover several countries at once. Building them next to
# their countries would download and filter the same data twice.
AGGREGATE_EXTRACTS: dict[str, frozenset[str]] = {
    "europe": frozenset({"alps", "britain-and-ireland", "dach", "great-britain"}),
}
_PBF_SUFFIX = "-latest.osm.pbf"

logger = logging.getLogger(__name__)


def list_child_regions(region: str, *, client: httpx.Client | None = None) -> list[str]:
    """Return the Geofabrik regions directly below `region`, or an empty list for a leaf.

    Children without a PBF extract and the aggregate extracts in `AGGREGATE_EXTRACTS` are left
    out. Raises `BuildToolError` when the index does not know `region`.
    """
    validate_region(region)
    owned_client = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=120.0)
    try:
        response = http.get(GEOFABRIK_INDEX_URL)
        response.raise_for_status()
        features = response.json()["features"]
    finally:
        if owned_client:
            http.close()
    known: dict[str, dict[str, object]] = {}
    for item in features:
        properties = item["properties"]
        path = _region_path(properties)
        if path is not None:
            known[path] = properties
    if region not in known:
        raise BuildToolError(f"Region {region!r} is not in the Geofabrik index")
    parent_id = known[region]["id"]
    excluded = AGGREGATE_EXTRACTS.get(region, frozenset())
    return sorted(
        path
        for path, properties in known.items()
        if properties.get("parent") == parent_id and properties["id"] not in excluded
    )


def _region_path(properties: dict[str, object]) -> str | None:
    urls = properties.get("urls")
    pbf = urls.get("pbf") if isinstance(urls, dict) else None
    if not isinstance(pbf, str) or not pbf.startswith(f"{GEOFABRIK_BASE_URL}/"):
        return None
    path = pbf.removeprefix(f"{GEOFABRIK_BASE_URL}/")
    if not path.endswith(_PBF_SUFFIX) or "/" not in path:
        return None
    return path.rsplit("/", 1)[0]


def run_regional_build(
    region: str,
    work_dir: Path,
    tools: BuildTools,
    *,
    runner: ToolRunner | None = None,
    downloader: Downloader | None = None,
    index_client: httpx.Client | None = None,
) -> BuildArtifact:
    """Build `region`, processing each Geofabrik child extract on its own when it has children.

    For a parent region every child is downloaded (md5 verified), filtered with osmium and its
    extract deleted before the next child starts, so the disk holds one extract plus the small
    filtered files. The filtered files are then merged and built with one Planetiler run. A
    failure in any child raises and leaves nothing to publish. A leaf region is built directly.
    Scratch files live under `work_dir` and are removed on success and on failure; only the
    PMTiles file of the result stays. `osm_data_date` is the oldest child's date, so the pointer
    never claims data fresher than its stalest part.
    """
    validate_region(region)
    runner = runner or SubprocessToolRunner(tools)
    children = list_child_regions(region, client=index_client)
    if not children:
        logger.info("%s has no Geofabrik children; building it directly", region)
        return run_build(region, work_dir, tools, runner=runner, downloader=downloader)

    download = downloader or download_extract
    scratch = work_dir / SCRATCH_DIR_NAME
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    filtered_files: list[Path] = []
    data_dates: list[str] = []
    try:
        for position, child in enumerate(children, start=1):
            logger.info("Processing %s (%d of %d)", child, position, len(children))
            extract = scratch / f"{position:03d}-extract.osm.pbf"
            filtered = scratch / f"{position:03d}-filtered.osm.pbf"
            download(child, extract)
            try:
                date = filter_extract(extract, filtered, tools, runner)
            finally:
                extract.unlink(missing_ok=True)
            if date:
                data_dates.append(date)
            filtered_files.append(filtered)
        merged = scratch / MERGED_NAME
        runner.run(
            [
                tools.osmium,
                "merge",
                "--overwrite",
                "-o",
                str(merged),
                *(str(path) for path in filtered_files),
            ]
        )
        for path in filtered_files:
            path.unlink()
        return build_tiles(merged, work_dir, min(data_dates) if data_dates else None, tools, runner)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
