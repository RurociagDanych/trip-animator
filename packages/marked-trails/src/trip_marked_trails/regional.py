"""Builds a parent Geofabrik region one child extract at a time to bound disk use."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
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
    read_replication_sequence,
    remove_build_scratch,
    run_build,
    validate_region,
)

GEOFABRIK_INDEX_URL = f"{GEOFABRIK_BASE_URL}/index-v1.json"
SCRATCH_DIR_NAME = "regional"
MERGED_NAME = "marked-trails-merged.osm.pbf"
# Geofabrik lists European Russia under russia, not under europe, so the europe parent alone
# would drop it. These are the federal districts west of the Urals; Kaliningrad is part of the
# northwestern one, and the districts east of them lie almost entirely in Asia.
EXTRA_CHILDREN: dict[str, tuple[str, ...]] = {
    "europe": (
        "russia/central-fed-district",
        "russia/northwestern-fed-district",
        "russia/south-fed-district",
        "russia/north-caucasus-fed-district",
        "russia/volga-fed-district",
    ),
}
_PBF_SUFFIX = "-latest.osm.pbf"

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _FilteredChild:
    path: Path
    sequence: int
    data_date: str | None


def list_child_regions(region: str, *, client: httpx.Client | None = None) -> list[str]:
    """Return the Geofabrik regions to build below `region`, or an empty list for a leaf.

    These are the direct children with a PBF extract, except those named in the parent's
    `special_regions` property of the index (extracts that overlap their siblings, such as
    `alps` and `dach` below `europe`), plus the `EXTRA_CHILDREN` of the region. Raises
    `BuildToolError` when the index does not know `region` or an extra child.
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
    for extra in EXTRA_CHILDREN.get(region, ()):
        if extra not in known:
            raise BuildToolError(
                f"Extra child {extra!r} of {region!r} is not in the Geofabrik index"
            )
        children.add(extra)
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

    Children are downloaded at different times. Merging extracts of different snapshots can
    keep an old version of a relation next to ways that a newer version dropped, so the
    `osmosis_replication_sequence_number` of every child must be equal. When it differs, each
    child older than the newest is downloaded and filtered once more; if the numbers still
    differ the build fails. Scratch files and Planetiler's temporary files live under
    `work_dir` and are removed on success and on failure, and stale ones of a killed run are
    removed at the start; only the PMTiles file of the result stays. `osm_data_date` is the
    oldest child's date, so the pointer never claims data fresher than its stalest part.
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
            sequence = read_replication_sequence(extract, tools, runner)
            if sequence is None or not sequence.isdigit():
                raise BuildToolError(f"{child} has no replication sequence number in its header")
            date = filter_extract(extract, filtered, tools, runner)
        finally:
            extract.unlink(missing_ok=True)
        return _FilteredChild(filtered, int(sequence), date)

    succeeded = False
    try:
        fetched = [fetch(position) for position in range(len(children))]
        newest = max(child.sequence for child in fetched)
        if any(child.sequence != newest for child in fetched):
            for position, child in enumerate(fetched):
                if child.sequence < newest:
                    logger.warning(
                        "%s is at replication sequence %d, the newest is %d; downloading it again",
                        children[position],
                        child.sequence,
                        newest,
                    )
                    child.path.unlink(missing_ok=True)
                    fetched[position] = fetch(position)
            if len({child.sequence for child in fetched}) != 1:
                numbers = ", ".join(
                    f"{name}={child.sequence}"
                    for name, child in zip(children, fetched, strict=True)
                )
                raise BuildToolError(
                    f"Extracts of {region} have different replication sequence numbers "
                    f"after downloading the older ones again: {numbers}"
                )
        merged = scratch / MERGED_NAME
        runner.run(
            [
                tools.osmium,
                "merge",
                "--overwrite",
                "-o",
                str(merged),
                *(str(child.path) for child in fetched),
            ]
        )
        for child in fetched:
            child.path.unlink()
        data_dates = [child.data_date for child in fetched if child.data_date]
        artifact = build_tiles(
            merged, work_dir, min(data_dates) if data_dates else None, tools, runner
        )
        succeeded = True
        return artifact
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        remove_build_scratch(work_dir, keep_outputs=succeeded)
