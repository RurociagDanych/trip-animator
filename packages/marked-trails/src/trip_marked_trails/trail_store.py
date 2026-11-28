"""Construction of the trail store: local directory for development, S3 for production."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trip_object_store import LocalObjectStore, ObjectStore, S3ObjectStore

DEFAULT_S3_PREFIX = "marked-trails"


class ListingS3ObjectStore(S3ObjectStore):
    """S3 store whose `exists` lists the key instead of sending a HEAD request.

    S3 answers a HEAD for a missing key with 403 when the caller lacks `s3:ListBucket`
    (https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html). The publisher
    policy grants `s3:ListBucket` only under the trail prefix; whether such a conditioned grant
    turns that 403 into 404 is unverified, so `exists` lists the full key instead, which stays
    inside the grant. Permission errors propagate.
    """

    def exists(self, key: str) -> bool:
        full_key = self._full_key(key)
        response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=full_key, MaxKeys=1)
        return any(obj.get("Key") == full_key for obj in response.get("Contents") or ())


@dataclass(frozen=True, slots=True)
class TrailStoreSettings:
    """Where the trail store lives.

    Credentials are never command line arguments. Without `s3_access_key_id` the S3 backend
    uses the default AWS chain; with it, the keys (and the optional session token) go only to
    this store's client, so they are not visible to other AWS clients in the process.
    `s3_prefix` must match the prefix the Airflow publisher policy allows.
    """

    backend: str = "local"
    store_dir: Path | None = None
    s3_bucket: str = ""
    s3_prefix: str = DEFAULT_S3_PREFIX
    s3_region: str | None = None
    s3_access_key_id: str = field(default="", repr=False)
    s3_secret_access_key: str = field(default="", repr=False)
    s3_session_token: str = field(default="", repr=False)


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
            return ListingS3ObjectStore(
                bucket=settings.s3_bucket, key_prefix=settings.s3_prefix, client=s3_client
            )
        has_keys = bool(settings.s3_access_key_id.strip() or settings.s3_secret_access_key.strip())
        if has_keys and not (
            settings.s3_access_key_id.strip() and settings.s3_secret_access_key.strip()
        ):
            raise ValueError("s3_access_key_id and s3_secret_access_key must be set together")
        if settings.s3_session_token.strip() and not has_keys:
            raise ValueError("s3_session_token needs s3_access_key_id and s3_secret_access_key")
        return ListingS3ObjectStore(
            bucket=settings.s3_bucket,
            key_prefix=settings.s3_prefix,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            session_token=settings.s3_session_token,
        )
    raise ValueError(f"Unknown trail store backend: {settings.backend!r}")
