"""ObjectStore abstraction with traversal-safe Local FS and MinIO/S3 implementations."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Protocol

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from trip_worker.config import WorkerSettings

_MP4_PART_BYTES = 8 * 1024 * 1024
# Classic uses file-backed parts for filename uploads; one request runs at a time.
_FILE_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=_MP4_PART_BYTES,
    multipart_chunksize=_MP4_PART_BYTES,
    use_threads=False,
    preferred_transfer_client="classic",
)


class ObjectStore(Protocol):
    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None: ...
    def put_file(
        self, key: str, source: Path, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Publish the completed file synchronously without consuming the source."""
        ...

    def get_bytes(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalObjectStore:
    """Local filesystem object store preventing directory traversal."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        clean = key.lstrip("/")
        if not clean or ".." in Path(clean).parts:
            raise ValueError(f"Unsafe object key: {key!r}")
        resolved = (self.root_dir / clean).resolve()
        if not resolved.is_relative_to(self.root_dir):
            raise ValueError(f"Object key escapes storage root: {key!r}")
        return resolved

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        _ = content_type
        target = self._safe_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def put_file(
        self, key: str, source: Path, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Replace the destination only after the full copy has been written."""
        _ = content_type
        target = self._safe_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stage on the destination filesystem so replacement is atomic.
        with tempfile.TemporaryDirectory(dir=target.parent) as staging_dir:
            staged = Path(staging_dir) / target.name
            shutil.copyfile(source, staged)
            staged.replace(target)

    def get_bytes(self, key: str) -> bytes:
        target = self._safe_path(key)
        return target.read_bytes()

    def resolve_existing_path(self, key: str) -> Path:
        target = self._safe_path(key)
        if not target.exists():
            raise FileNotFoundError(f"Object not found: {key!r}")
        return target

    def delete(self, key: str) -> None:
        target = self._safe_path(key)
        if target.exists():
            target.unlink()


class S3ObjectStore:
    """S3/MinIO object store adapter."""

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        bucket: str,
        region: str,
    ) -> None:
        self.bucket = bucket
        resolved_endpoint = endpoint_url.strip() if isinstance(endpoint_url, str) else endpoint_url
        resolved_access_key = (
            access_key_id.strip() if isinstance(access_key_id, str) else access_key_id
        )
        resolved_secret_key = (
            secret_access_key.strip() if isinstance(secret_access_key, str) else secret_access_key
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=resolved_endpoint or None,
            aws_access_key_id=resolved_access_key or None,
            aws_secret_access_key=resolved_secret_key or None,
            region_name=region,
        )

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def put_file(
        self, key: str, source: Path, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Upload a filename with a bounded, synchronous multipart transfer."""
        self.client.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
            Config=_FILE_TRANSFER_CONFIG,
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}:
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            raise
        body: bytes = resp["Body"].read()
        return body

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def build_object_store(settings: WorkerSettings) -> ObjectStore:
    """Construct the configured ObjectStore backend for the worker."""
    if settings.object_store_backend == "s3":
        return S3ObjectStore(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
        )
    return LocalObjectStore(Path(settings.local_object_store_dir))


def materialize_parquet_path(
    store: ObjectStore,
    silver_object_key: str,
    tmp_dir: Path,
    stage_id: str,
) -> Path:
    """Return a local filesystem path to a stage's Silver Parquet object."""
    if isinstance(store, LocalObjectStore):
        return store.resolve_existing_path(silver_object_key)
    target = tmp_dir / f"{stage_id}.parquet"
    target.write_bytes(store.get_bytes(silver_object_key))
    return target
