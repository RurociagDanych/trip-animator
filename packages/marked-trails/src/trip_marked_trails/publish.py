"""Publication of Marked Trails builds: immutable build prefixes and the `current.json` pointer."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime
from pathlib import Path

from trip_object_store import ObjectStore

from trip_marked_trails.pipeline import PMTILES_NAME, BuildArtifact

PMTILES_CONTENT_TYPE = "application/vnd.pmtiles"
POINTER_KEY = "current.json"
POINTER_SCHEMA_VERSION = 1
BUILD_CACHE_CONTROL = "public, max-age=31536000, immutable"
POINTER_CACHE_CONTROL = "no-cache, no-store, must-revalidate"
BUILDS_PREFIX = "builds/"
DEFAULT_SIZE_BUDGET_BYTES = 1024**3
MAX_RELATIVE_CHANGE_PERCENT = 20

logger = logging.getLogger(__name__)


BUILD_ID_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}(-[2-9]|-[1-9]\d+)?")


class PointerError(Exception):
    """`current.json` does not describe a build that is safe to keep or to prune around."""


class ValidationError(Exception):
    """A built file failed the checks that guard the switch of `current.json`."""


@dataclass(frozen=True, slots=True)
class CurrentPointer:
    """Contents of `current.json`: the active build and the previous one for rollback.

    Fields added after schema version 1 have defaults, so older pointers still load.
    """

    region: str
    current_build_id: str
    previous_build_id: str | None
    pmtiles_key: str
    size_bytes: int
    sha256: str
    osm_data_date: str | None
    built_at: str
    marked_trail_count: int | None = None


def build_pmtiles_key(build_id: str) -> str:
    """Return the store key of the PMTiles file of `build_id`."""
    return f"{BUILDS_PREFIX}{build_id}/{PMTILES_NAME}"


def read_pointer(store: ObjectStore) -> CurrentPointer | None:
    """Return the published pointer, or None when nothing has been published yet.

    Unknown keys are ignored, so a pointer written by a newer version still loads.
    """
    if not store.exists(POINTER_KEY):
        return None
    data = json.loads(store.get_bytes(POINTER_KEY))
    if data.get("schema_version") != POINTER_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported {POINTER_KEY} schema_version: {data.get('schema_version')!r}"
        )
    known = {field.name for field in fields(CurrentPointer)}
    return CurrentPointer(**{key: value for key, value in data.items() if key in known})


def allocate_build_id(
    store: ObjectStore, build_date: date, pointer: CurrentPointer | None = None
) -> str:
    """Return a build id for `build_date` that no build or pointer has used.

    The first build of a day is the bare date, later ones get a numeric suffix one above the
    highest seen in the store and in the pointer. Ids of deleted builds are never reused, so
    a cached immutable object can never be shadowed by different content.
    """
    base = build_date.isoformat()
    seen = [*_store_build_ids(store)]
    if pointer is not None:
        seen += [pointer.current_build_id, pointer.previous_build_id or ""]
    highest = 0
    for build_id in seen:
        if build_id == base:
            highest = max(highest, 1)
        elif build_id.startswith(f"{base}-") and build_id[len(base) + 1 :].isdecimal():
            highest = max(highest, int(build_id[len(base) + 1 :]))
    return base if highest == 0 else f"{base}-{highest + 1}"


def _store_build_ids(store: ObjectStore) -> set[str]:
    return {info.key.split("/")[1] for info in store.iter_objects(BUILDS_PREFIX)}


def check_pointer_before_prune(store: ObjectStore, pointer: CurrentPointer) -> None:
    """Raise `PointerError` unless the pointer names a build that exists in the store.

    Pruning keeps only the build the pointer names, so a typo or a stale key would delete the
    live build.
    """
    if not BUILD_ID_PATTERN.fullmatch(pointer.current_build_id):
        raise PointerError(f"current_build_id {pointer.current_build_id!r} is not a build id")
    expected = build_pmtiles_key(pointer.current_build_id)
    if pointer.pmtiles_key != expected:
        raise PointerError(
            f"pmtiles_key {pointer.pmtiles_key!r} does not match current_build_id "
            f"{pointer.current_build_id!r} (expected {expected!r})"
        )
    if not store.exists(expected):
        raise PointerError(f"The current build object {expected!r} is not in the store")


def delete_superseded_builds(store: ObjectStore, pointer: CurrentPointer | None) -> list[str]:
    """Delete every build except the current one and return the deleted keys.

    Run before a new build starts, so the store never holds more than two builds. Without a
    pointer nothing is deleted: the store has no published build to protect or to keep. An
    invalid pointer raises `PointerError` before anything is deleted.
    """
    if pointer is None:
        return []
    check_pointer_before_prune(store, pointer)
    keep = f"{BUILDS_PREFIX}{pointer.current_build_id}/"
    stale = [
        info.key for info in store.iter_objects(BUILDS_PREFIX) if not info.key.startswith(keep)
    ]
    for key in stale:
        store.delete(key)
    return stale


def validate_build(
    artifact: BuildArtifact, current: CurrentPointer | None, size_budget_bytes: int
) -> None:
    """Raise `ValidationError` unless the build fits the budget and is close to the current build.

    Size and trail count must be within `MAX_RELATIVE_CHANGE_PERCENT` of the current build.
    Without a current build only the budget applies, and a current build without a recorded
    trail count skips the count check. An empty file or a build without trails is always
    rejected, as is a budget of zero or below.
    """
    if size_budget_bytes <= 0:
        raise ValidationError(f"Size budget must be positive, got {size_budget_bytes}")
    size = artifact.pmtiles_path.stat().st_size
    if size <= 0:
        raise ValidationError("Build file is empty")
    if artifact.marked_trail_count <= 0:
        raise ValidationError(f"Build has {artifact.marked_trail_count} Marked Trail relations")
    if size > size_budget_bytes:
        raise ValidationError(f"Build is {size} bytes, over the budget of {size_budget_bytes}")
    if current is None:
        return
    _check_within_change_limit("size", size, current.size_bytes)
    if current.marked_trail_count is not None:
        _check_within_change_limit(
            "Marked Trail count", artifact.marked_trail_count, current.marked_trail_count
        )


def _check_within_change_limit(name: str, new: int, current: int) -> None:
    if abs(new - current) * 100 > current * MAX_RELATIVE_CHANGE_PERCENT:
        raise ValidationError(
            f"{name} {new} differs from the current build's {current} by more than "
            f"{MAX_RELATIVE_CHANGE_PERCENT}%"
        )


def publish_build(
    store: ObjectStore,
    *,
    region: str,
    build_date: date,
    artifact: BuildArtifact,
    previous: CurrentPointer | None,
    size_budget_bytes: int = DEFAULT_SIZE_BUDGET_BYTES,
) -> CurrentPointer:
    """Validate a built PMTiles file, upload it under a new prefix, then switch `current.json`.

    `previous` is the pointer read once by the caller. Validation runs on the local file before
    the upload, so a rejected build never reaches the store. A failed or short upload removes
    its partial object. Once the pointer write starts the new build is never deleted: the
    write may have reached the store, and a pointer naming a deleted build would break readers.
    """
    validate_build(artifact, previous, size_budget_bytes)
    size = artifact.pmtiles_path.stat().st_size
    sha256 = _sha256_file(artifact.pmtiles_path)
    build_id = allocate_build_id(store, build_date, previous)
    key = build_pmtiles_key(build_id)
    try:
        store.put_file(
            key,
            artifact.pmtiles_path,
            content_type=PMTILES_CONTENT_TYPE,
            cache_control=BUILD_CACHE_CONTROL,
        )
        stored_size = store.stat(key).size
        if stored_size != size:
            raise ValidationError(f"Uploaded object has {stored_size} bytes, expected {size}")
    except BaseException:
        try:
            store.delete(key)
        except Exception:
            logger.exception("Could not delete the partial upload %s", key)
        raise
    pointer = CurrentPointer(
        region=region,
        current_build_id=build_id,
        previous_build_id=previous.current_build_id if previous else None,
        pmtiles_key=key,
        size_bytes=size,
        sha256=sha256,
        osm_data_date=artifact.osm_data_date,
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
        marked_trail_count=artifact.marked_trail_count,
    )
    payload = {"schema_version": POINTER_SCHEMA_VERSION, **asdict(pointer)}
    store.put_bytes(
        POINTER_KEY,
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        content_type="application/json",
        cache_control=POINTER_CACHE_CONTROL,
    )
    return pointer


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
