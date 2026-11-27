"""Sorted ObjectStore listing and existence helpers for the Airflow tasks."""

from __future__ import annotations

from datetime import datetime

from trip_object_store import ObjectStore


def list_objects_with_mtime(
    store: ObjectStore,
    prefix: str = "",
) -> dict[str, datetime | None]:
    """Map keys under ``prefix`` to their UTC modification time, sorted by key.

    Raises `trip_object_store.UnsafeKeyError` for a traversing or malformed prefix.
    """
    objects = sorted(store.iter_objects(prefix), key=lambda info: info.key)
    return {info.key: info.last_modified for info in objects}


def list_object_keys(store: ObjectStore, prefix: str = "") -> list[str]:
    """List sorted object keys under ``prefix``."""
    return sorted(info.key for info in store.iter_objects(prefix))


def object_exists(store: ObjectStore, key: str) -> bool:
    """Return True if ``key`` exists in ``store``; raise `UnsafeKeyError` for an unsafe key."""
    return store.exists(key)
