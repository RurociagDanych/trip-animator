"""Builds a parent Geofabrik region one child extract at a time to bound disk use."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from trip_marked_trails.pipeline import (
    GEOFABRIK_BASE_URL,
    PLANETILER_TMP_NAME,
    BuildArtifact,
    BuildToolError,
    BuildTools,
    Downloader,
    SubprocessToolRunner,
    ToolRunner,
    build_tiles,
    download_extract,
    filter_extract,
    log_disk_usage,
    remove_build_scratch,
    run_build,
    validate_region,
)

GEOFABRIK_INDEX_URL = f"{GEOFABRIK_BASE_URL}/index-v1.json"
SCRATCH_DIR_NAME = "regional"
MERGED_HISTORY_NAME = "marked-trails-merged.osh.pbf"
MERGED_NAME = "marked-trails-merged.osm.pbf"
MAX_SNAPSHOT_SPREAD = timedelta(days=2)
# One merge holds memory per input (48 to 90 MiB each, measured), so 56 inputs exceeded the 3 GiB
# cap while 8 peaked below 1 GiB.
MERGE_BATCH_SIZE = 8
_PBF_SUFFIX = "-latest.osm.pbf"

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _FilteredChild:
    path: Path
    data_date: str | None


def _oldest_date(region: str, fetched: list[_FilteredChild]) -> str | None:
    dates = sorted((child.data_date for child in fetched if child.data_date), key=_parse_date)
    if not dates:
        return None
    spread = _parse_date(dates[-1]) - _parse_date(dates[0])
    logger.info(
        "Extracts of %s are dated %s to %s, a spread of %s", region, dates[0], dates[-1], spread
    )
    if spread > MAX_SNAPSHOT_SPREAD:
        logger.warning(
            "Extracts of %s are %s apart, more than %s; the oldest date is published",
            region,
            spread,
            MAX_SNAPSHOT_SPREAD,
        )
    return dates[0]


def _parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _valid_date(child: str, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        _parse_date(value)
    except ValueError:
        logger.warning("Ignoring the malformed data date %r of %s", value, child)
        return None
    return value


def _merge_history(
    paths: list[Path], history: Path, scratch: Path, tools: BuildTools, runner: ToolRunner
) -> None:
    level = 0
    while len(paths) > MERGE_BATCH_SIZE:
        merged: list[Path] = []
        for start in range(0, len(paths), MERGE_BATCH_SIZE):
            output = scratch / f"merge-{level}-{start // MERGE_BATCH_SIZE:03d}.osh.pbf"
            _merge(paths[start : start + MERGE_BATCH_SIZE], output, tools, runner)
            log_disk_usage(scratch.parent, f"merge batch {level}-{start // MERGE_BATCH_SIZE}")
            merged.append(output)
        paths = merged
        level += 1
    _merge(paths, history, tools, runner)


def _merge(inputs: list[Path], output: Path, tools: BuildTools, runner: ToolRunner) -> None:
    runner.run(
        [
            tools.osmium,
            "merge",
            "--with-history",
            "--overwrite",
            "-o",
            str(output),
            *(str(path) for path in inputs),
        ]
    )
    for path in inputs:
        path.unlink()


def list_child_regions(region: str, *, client: httpx.Client | None = None) -> list[str]:
    """Return the Geofabrik regions to build below `region`, or an empty list for a leaf.

    These are the direct children with a PBF extract, except those named in the parent's
    `special_regions` property of the index (extracts that overlap their siblings, such as
    `alps` and `dach` below `europe`). Raises `BuildToolError` when the index does not know
    `region`.
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
    parent = known[region]
    special = parent.get("special_regions")
    excluded = set(special) if isinstance(special, list) else set()
    children = {
        path
        for path, properties in known.items()
        if properties.get("parent") == parent["id"] and properties["id"] not in excluded
    }
    return sorted(children)


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

    Children are downloaded at different times, so two extracts can hold different versions of
    the same relation. The filtered files are merged with `osmium merge --with-history`, at
    most `MERGE_BATCH_SIZE` inputs at a time and then the intermediate results the same way,
    to bound memory. `osmium time-filter` runs once on the final result and keeps only the
    newest version of each object, so one relation with its current members survives. An
    object deleted between two snapshots can survive from the older extract; this is accepted.
    The `osmosis_replication_timestamp` of every child is logged right after its download; a
    malformed one is logged and ignored. `osm_data_date` is the oldest valid one, and a spread
    of more than `MAX_SNAPSHOT_SPREAD` is only logged as a warning. Scratch files and
    Planetiler's temporary files live under `work_dir` and are removed on success and on
    failure, and stale ones of a killed run are removed at the start; only the PMTiles file of
    the result stays.
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
    shutil.rmtree(work_dir / PLANETILER_TMP_NAME, ignore_errors=True)
    scratch.mkdir(parents=True)

    def fetch(position: int) -> _FilteredChild:
        child = children[position]
        logger.info("Processing %s (%d of %d)", child, position + 1, len(children))
        extract = scratch / f"{position + 1:03d}-extract.osm.pbf"
        filtered = scratch / f"{position + 1:03d}-filtered.osm.pbf"
        download(child, extract)
        try:
            date = _valid_date(child, filter_extract(extract, filtered, tools, runner))
        finally:
            extract.unlink(missing_ok=True)
        logger.info("%s has data date %s", child, date)
        log_disk_usage(work_dir, f"filter of {child}")
        return _FilteredChild(filtered, date)

    succeeded = False
    try:
        fetched = [fetch(position) for position in range(len(children))]
        history = scratch / MERGED_HISTORY_NAME
        _merge_history([child.path for child in fetched], history, scratch, tools, runner)
        log_disk_usage(work_dir, "merge")
        merged = scratch / MERGED_NAME
        runner.run([tools.osmium, "time-filter", "--overwrite", "-o", str(merged), str(history)])
        history.unlink()
        log_disk_usage(work_dir, "time-filter")
        artifact = build_tiles(merged, work_dir, _oldest_date(region, fetched), tools, runner)
        succeeded = True
        return artifact
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        remove_build_scratch(work_dir, keep_outputs=succeeded)
