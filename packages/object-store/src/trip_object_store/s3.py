"""S3-compatible `ObjectStore` (AWS S3, MinIO, RustFS)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from trip_object_store.atomic import staged_path
from trip_object_store.base import DEFAULT_CHUNK_BYTES, ObjectInfo
from trip_object_store.keys import normalize_key, normalize_prefix
from trip_object_store.ranges import InvalidRangeError, ObjectStream, parse_byte_range

_PART_BYTES = 8 * 1024 * 1024

# Classic uses file-backed parts for filename uploads; one request runs at a time, which bounds
# memory for large files on small hosts.
DEFAULT_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=_PART_BYTES,
    multipart_chunksize=_PART_BYTES,
    use_threads=False,
    preferred_transfer_client="classic",
)


def is_not_found_error(exc: ClientError) -> bool:
    """Return whether a boto3 error means the object is missing.

    Matches the `NoSuchKey`, `404` and `NotFound` codes and any HTTP 404 status, because
    S3-compatible servers answer HEAD requests with non-standard codes. `AccessDenied` is
    never a not-found error.
    """
    err = exc.response.get("Error", {})
    code = str(err.get("Code", ""))
    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "404", "NotFound"} or status_code == 404


def _is_invalid_range_error(exc: ClientError) -> bool:
    err = exc.response.get("Error", {})
    code = str(err.get("Code", ""))
    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code == "InvalidRange" or status_code == 416


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_key_prefix(key_prefix: str) -> str:
    clean = normalize_prefix(key_prefix.strip()).strip("/")
    return f"{clean}/" if clean else ""


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class S3ObjectStore:
    """S3-compatible object store; static keys or the default credential chain.

    `client` and `bucket` are public as an escape hatch for operations the interface does
    not cover (ETag sync, same-bucket copies, credentials for other libraries). Pass a
    prebuilt `client` or the endpoint, credential, region and botocore config arguments,
    never both. Empty or blank strings count as unset, so `region=""` falls back to the
    botocore region chain. `key_prefix` is prepended to every key and stripped from
    listings. Permission and server errors propagate as `ClientError`; failed uploads raise
    `boto3.exceptions.S3UploadFailedError`.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        region: str | None = None,
        key_prefix: str = "",
        client: Any | None = None,
        botocore_config: Config | None = None,
        transfer_config: TransferConfig | None = None,
    ) -> None:
        endpoint = _normalize_optional_str(endpoint_url)
        access_key = _normalize_optional_str(access_key_id)
        secret_key = _normalize_optional_str(secret_access_key)
        token = _normalize_optional_str(session_token)
        region_name = _normalize_optional_str(region)
        self.bucket = bucket
        self.key_prefix = _normalize_key_prefix(key_prefix)
        self.transfer_config = transfer_config or DEFAULT_TRANSFER_CONFIG
        if client is not None:
            if any(
                v is not None for v in (endpoint, access_key, secret_key, token, region_name)
            ) or (botocore_config is not None):
                raise ValueError(
                    "client cannot be combined with endpoint_url, credentials, region "
                    "or botocore_config"
                )
            self.client: Any = client
            return
        extra: dict[str, Any] = {}
        if token is not None:
            extra["aws_session_token"] = token
        if botocore_config is not None:
            extra["config"] = botocore_config
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            **extra,
        )

    def _full_key(self, key: str) -> str:
        return f"{self.key_prefix}{normalize_key(key)}"

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._full_key(key),
            "Body": data,
            "ContentType": content_type,
        }
        if cache_control:
            args["CacheControl"] = cache_control
        self.client.put_object(**args)

    def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        """Upload a local file, using bounded multipart transfers when needed."""
        extra_args: dict[str, Any] = {"ContentType": content_type}
        if cache_control:
            extra_args["CacheControl"] = cache_control
        full_key = self._full_key(key)
        self.client.upload_file(
            str(source), self.bucket, full_key, ExtraArgs=extra_args, Config=self.transfer_config
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._full_key(key))
        except ClientError as exc:
            if is_not_found_error(exc):
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            raise
        body_stream = resp["Body"]
        try:
            body: bytes = body_stream.read()
            return body
        finally:
            body_stream.close()

    def get_file(self, key: str, dest: Path) -> None:
        full_key = self._full_key(key)
        try:
            with staged_path(dest) as tmp:
                self.client.download_file(
                    self.bucket, full_key, str(tmp), Config=self.transfer_config
                )
        except ClientError as exc:
            if is_not_found_error(exc):
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            raise

    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_BYTES,
    ) -> ObjectStream:
        full_key = self._full_key(key)
        total_size = 0
        byte_range: tuple[int, int] | None = None
        if range_header:
            try:
                head = self.client.head_object(Bucket=self.bucket, Key=full_key)
            except ClientError as exc:
                if is_not_found_error(exc):
                    raise FileNotFoundError(f"Object not found: {key!r}") from exc
                raise
            total_size = int(head.get("ContentLength", 0))
            byte_range = parse_byte_range(range_header, total_size)

        get_kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": full_key}
        if byte_range is not None:
            start, end = byte_range
            get_kwargs["Range"] = f"bytes={start}-{end}"
            is_partial = True
        else:
            start = 0
            end = 0
            is_partial = False

        try:
            resp = self.client.get_object(**get_kwargs)
        except ClientError as exc:
            if is_not_found_error(exc):
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            if _is_invalid_range_error(exc):
                raise InvalidRangeError(total_size) from exc
            raise

        resp_length = int(resp.get("ContentLength", 0))
        if is_partial:
            content_length = resp_length
        else:
            total_size = resp_length
            content_length = resp_length
            end = max(total_size - 1, 0)

        body_stream = resp["Body"]

        def _iter_chunks() -> Iterator[bytes]:
            try:
                for chunk in body_stream.iter_chunks(chunk_size=chunk_size):
                    if chunk:
                        yield chunk
            finally:
                body_stream.close()

        return ObjectStream(
            chunks=_iter_chunks(),
            total_size=total_size,
            content_length=content_length,
            start=start,
            end=end,
            is_partial=is_partial,
        )

    def copy(
        self,
        src_key: str,
        dst_key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Server-side copy.

        Giving `content_type` or `cache_control` switches to `MetadataDirective=REPLACE`,
        which drops all other source metadata, including a content type that is not passed
        again.
        """
        extra_args: dict[str, Any] | None = None
        if content_type or cache_control:
            extra_args = {"MetadataDirective": "REPLACE"}
            if content_type:
                extra_args["ContentType"] = content_type
            if cache_control:
                extra_args["CacheControl"] = cache_control
        src = self._full_key(src_key)
        dst = self._full_key(dst_key)
        try:
            self.client.copy(
                {"Bucket": self.bucket, "Key": src}, self.bucket, dst, ExtraArgs=extra_args
            )
        except ClientError as exc:
            if is_not_found_error(exc):
                raise FileNotFoundError(f"Object not found: {src_key!r}") from exc
            raise

    def delete(self, key: str) -> None:
        full_key = self._full_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=full_key)
        except ClientError as exc:
            if is_not_found_error(exc):
                return  # Some S3-compatible servers answer NoSuchKey instead of 204.
            raise

    def stat(self, key: str) -> ObjectInfo:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=self._full_key(key))
        except ClientError as exc:
            if is_not_found_error(exc):
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            raise
        etag = head.get("ETag")
        return ObjectInfo(
            key=normalize_key(key),
            size=int(head.get("ContentLength", 0)),
            last_modified=_as_utc(head.get("LastModified")),
            etag=str(etag) if etag is not None else None,
        )

    def exists(self, key: str) -> bool:
        try:
            self.stat(key)
        except FileNotFoundError:
            return False
        return True

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        full_prefix = f"{self.key_prefix}{normalize_prefix(prefix)}"
        paginator = self.client.get_paginator("list_objects_v2")
        page_kwargs: dict[str, Any] = {"Bucket": self.bucket}
        if full_prefix:
            page_kwargs["Prefix"] = full_prefix
        for page in paginator.paginate(**page_kwargs):
            for obj in page.get("Contents") or ():
                key = str(obj["Key"]).removeprefix(self.key_prefix)
                if not key:
                    continue
                etag = obj.get("ETag")
                yield ObjectInfo(
                    key=key,
                    size=int(obj.get("Size", 0)),
                    last_modified=_as_utc(obj.get("LastModified")),
                    etag=str(etag) if etag is not None else None,
                )
