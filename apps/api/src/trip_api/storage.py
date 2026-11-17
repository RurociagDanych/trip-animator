"""ObjectStore abstraction with traversal-safe Local FS and MinIO/S3 implementations."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

_STREAM_CHUNK_BYTES = 64 * 1024


class InvalidRangeError(ValueError):
    """Raised when an HTTP Range header cannot be satisfied for an object."""

    def __init__(self, total_size: int) -> None:
        super().__init__(f"Requested byte range is not satisfiable for size {total_size}")
        self.total_size = total_size


@dataclass(slots=True)
class ObjectStream:
    """Chunked object stream with byte-range metadata."""

    chunks: Iterator[bytes]
    total_size: int
    content_length: int
    start: int
    end: int
    is_partial: bool


def parse_byte_range(range_header: str, total_size: int) -> tuple[int, int] | None:
    """Parse an HTTP `Range: bytes=...` header into an inclusive `(start, end)` pair.

    Returns `None` when the header is missing, uses a non-bytes unit, or specifies a
    multi-range / syntactically unrecognized range so callers can serve a full 200 OK.
    Raises `InvalidRangeError` when a syntactically valid range cannot be satisfied.
    """
    value = range_header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].strip()
    if "," in spec or "-" not in spec:
        return None

    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    if start_str == "":
        if not (end_str.isascii() and end_str.isdigit()):
            return None
        suffix_len = int(end_str)
        if suffix_len == 0 or total_size == 0:
            raise InvalidRangeError(total_size)
        return max(total_size - suffix_len, 0), total_size - 1

    if not (start_str.isascii() and start_str.isdigit()):
        return None
    start = int(start_str)
    if total_size == 0 or start >= total_size:
        raise InvalidRangeError(total_size)

    if end_str == "":
        return start, total_size - 1
    if not (end_str.isascii() and end_str.isdigit()):
        return None
    end = int(end_str)
    if end < start:
        raise InvalidRangeError(total_size)
    return start, min(end, total_size - 1)


def _is_not_found_client_error(exc: ClientError) -> bool:
    err = exc.response.get("Error", {})
    code = str(err.get("Code", ""))
    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "404", "NotFound"} or status_code == 404


def _is_invalid_range_client_error(exc: ClientError) -> bool:
    err = exc.response.get("Error", {})
    code = str(err.get("Code", ""))
    status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code == "InvalidRange" or status_code == 416


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class ObjectStore(Protocol):
    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = _STREAM_CHUNK_BYTES,
    ) -> ObjectStream: ...
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

    def get_bytes(self, key: str) -> bytes:
        target = self._safe_path(key)
        return target.read_bytes()

    def resolve_existing_path(self, key: str) -> Path:
        target = self._safe_path(key)
        if not target.exists():
            raise FileNotFoundError(f"Object not found: {key!r}")
        return target

    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = _STREAM_CHUNK_BYTES,
    ) -> ObjectStream:
        target = self.resolve_existing_path(key)
        total_size = target.stat().st_size
        byte_range = parse_byte_range(range_header, total_size) if range_header else None

        if byte_range is not None:
            start, end = byte_range
            content_length = end - start + 1
            is_partial = True
        else:
            start = 0
            end = max(total_size - 1, 0)
            content_length = total_size
            is_partial = False

        fh = target.open("rb")
        try:
            if start > 0:
                fh.seek(start)
        except Exception:
            fh.close()
            raise

        def _iter_chunks() -> Iterator[bytes]:
            remaining = content_length
            try:
                while remaining > 0:
                    chunk = fh.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                fh.close()

        return ObjectStream(
            chunks=_iter_chunks(),
            total_size=total_size,
            content_length=content_length,
            start=start,
            end=end,
            is_partial=is_partial,
        )

    def delete(self, key: str) -> None:
        target = self._safe_path(key)
        if target.exists():
            target.unlink()


class S3ObjectStore:
    """S3/MinIO object store adapter supporting static keys and IAM role credential chains."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
        bucket: str,
        region: str | None,
    ) -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=_normalize_optional_str(endpoint_url),
            aws_access_key_id=_normalize_optional_str(access_key_id),
            aws_secret_access_key=_normalize_optional_str(secret_access_key),
            region_name=_normalize_optional_str(region),
        )

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get_bytes(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _is_not_found_client_error(exc):
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            raise
        body_stream = resp["Body"]
        try:
            body: bytes = body_stream.read()
            return body
        finally:
            body_stream.close()

    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = _STREAM_CHUNK_BYTES,
    ) -> ObjectStream:
        total_size = 0
        byte_range: tuple[int, int] | None = None
        if range_header:
            try:
                head = self.client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if _is_not_found_client_error(exc):
                    raise FileNotFoundError(f"Object not found: {key!r}") from exc
                raise
            total_size = int(head.get("ContentLength", 0))
            byte_range = parse_byte_range(range_header, total_size)

        get_kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
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
            if _is_not_found_client_error(exc):
                raise FileNotFoundError(f"Object not found: {key!r}") from exc
            if _is_invalid_range_client_error(exc):
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

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _is_not_found_client_error(exc):
                return
            raise
