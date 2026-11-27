"""Construction of the trail store: local directory for development, S3 for production."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trip_object_store import LocalObjectStore, ObjectStore, S3ObjectStore

DEFAULT_S3_PREFIX = "marked-trails"


@dataclass(frozen=True, slots=True)
class TrailStoreSettings:
    """Where the trail store lives.

    The S3 backend takes credentials from the default AWS chain, never from settings, so no
    secret reaches the command line. `s3_prefix` must match the prefix the Airflow publisher
    policy allows.
    """

    backend: str = "local"
    store_dir: Path | None = None
    s3_bucket: str = ""
    s3_prefix: str = DEFAULT_S3_PREFIX
    s3_region: str | None = None


def build_trail_store(settings: TrailStoreSettings, *, s3_client: Any | None = None) -> ObjectStore:
    """Return the store named by `settings.backend`; raises `ValueError` if it is incomplete.

    `s3_client` replaces the boto3 client in tests.
    """
    if settings.backend == "local":
        if settings.store_dir is None:
            raise ValueError("store_dir is required for the local backend")
        return LocalObjectStore(settings.store_dir)
    if settings.backend == "s3":
        if not settings.s3_bucket.strip():
            raise ValueError("s3_bucket is required for the s3 backend")
        if s3_client is not None:
            return S3ObjectStore(
                bucket=settings.s3_bucket, key_prefix=settings.s3_prefix, client=s3_client
            )
        return S3ObjectStore(
            bucket=settings.s3_bucket, key_prefix=settings.s3_prefix, region=settings.s3_region
        )
    raise ValueError(f"Unknown trail store backend: {settings.backend!r}")
