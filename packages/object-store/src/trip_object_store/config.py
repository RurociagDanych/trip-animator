"""Backend selection: `ObjectStoreConfig`, `build_object_store` and `materialize`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trip_object_store.base import ObjectStore
from trip_object_store.local import LocalObjectStore
from trip_object_store.s3 import S3ObjectStore


@dataclass(frozen=True, slots=True)
class ObjectStoreConfig:
    """Plain configuration for `build_object_store`; callers map their own settings onto it.

    `key_prefix` applies to the S3 backend only; the local backend is already scoped by
    `local_dir`.
    """

    backend: Literal["local", "s3"]
    local_dir: Path
    s3_bucket: str = ""
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str | None = None
    key_prefix: str = ""


def build_object_store(config: ObjectStoreConfig) -> ObjectStore:
    """Construct the backend named by `config.backend`; raises `ValueError` if it is invalid."""
    if config.backend == "local":
        return LocalObjectStore(config.local_dir)
    if config.backend == "s3":
        if not config.s3_bucket.strip():
            raise ValueError("s3_bucket is required for the s3 backend")
        return S3ObjectStore(
            bucket=config.s3_bucket,
            endpoint_url=config.s3_endpoint_url,
            access_key_id=config.s3_access_key_id,
            secret_access_key=config.s3_secret_access_key,
            region=config.s3_region,
            key_prefix=config.key_prefix,
        )
    raise ValueError(f"Unknown object store backend: {config.backend!r}")


def materialize(store: ObjectStore, key: str, dest: Path) -> Path:
    """Return a local path holding the object.

    A `LocalObjectStore` returns the existing file without copying, so callers must not
    modify it. Other stores download the object to `dest` and return `dest`.
    """
    if isinstance(store, LocalObjectStore):
        return store.resolve_existing_path(key)
    store.get_file(key, dest)
    return dest
