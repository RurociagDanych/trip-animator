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
    return f"builds/{build_id}/{PMTILES_NAME}"


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


def publish_build(
    store: ObjectStore,
    *,
    region: str,
    build_date: date,
    artifact: BuildArtifact,
) -> CurrentPointer:
    """Publish a built PMTiles file under a new prefix, then switch `current.json` to it.

    The pointer is written last, so a failure before it leaves the previously published
    build active.
    """
    previous = read_pointer(store)
    build_id = allocate_build_id(store, build_date)
    key = build_pmtiles_key(build_id)
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
