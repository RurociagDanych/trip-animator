"""Publication of Marked Trails builds: immutable build prefixes and the `current.json` pointer."""

from __future__ import annotations

import hashlib
import json
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


def allocate_build_id(store: ObjectStore, build_date: date) -> str:
    """Return the build date, with a `-N` suffix for later builds on the same day."""
    base = build_date.isoformat()
    candidate = base
    suffix = 2
    while store.exists(build_pmtiles_key(candidate)):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def delete_superseded_builds(store: ObjectStore, pointer: CurrentPointer | None) -> list[str]:
    """Delete every build except the current one and return the deleted keys.

    Run before a new build starts, so the store never holds more than two builds. Without a
    pointer nothing is deleted: the store has no published build to protect or to keep.
    """
    if pointer is None:
        return []
    keep = build_pmtiles_key(pointer.current_build_id).removesuffix(PMTILES_NAME)
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
    trail count skips the count check.
    """
    size = artifact.pmtiles_path.stat().st_size
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
    size_budget_bytes: int = DEFAULT_SIZE_BUDGET_BYTES,
) -> CurrentPointer:
    """Validate a built PMTiles file, upload it under a new prefix, then switch `current.json`.

    The file is validated before the upload, so a rejected build never reaches the store. The
    pointer is written last, so a failure at any point leaves the previously published build
    active; a failed upload removes its partial prefix.
    """
    previous = read_pointer(store)
    validate_build(artifact, previous, size_budget_bytes)
    build_id = allocate_build_id(store, build_date)
    key = build_pmtiles_key(build_id)
    try:
        return _upload_and_switch(store, previous, region, build_id, key, artifact)
    except Exception:
        store.delete(key)
        raise


def _upload_and_switch(
    store: ObjectStore,
    previous: CurrentPointer | None,
    region: str,
    build_id: str,
    key: str,
    artifact: BuildArtifact,
) -> CurrentPointer:
    store.put_file(
        key,
        artifact.pmtiles_path,
        content_type=PMTILES_CONTENT_TYPE,
        cache_control=BUILD_CACHE_CONTROL,
    )
    pointer = CurrentPointer(
        region=region,
        current_build_id=build_id,
        previous_build_id=previous.current_build_id if previous else None,
        pmtiles_key=key,
        size_bytes=artifact.pmtiles_path.stat().st_size,
        sha256=_sha256_file(artifact.pmtiles_path),
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
