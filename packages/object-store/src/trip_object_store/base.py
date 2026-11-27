"""The `ObjectStore` protocol and the `ObjectInfo` record shared by all backends."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from trip_object_store.ranges import ObjectStream

DEFAULT_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Metadata of one stored object."""

    key: str
    size: int
    last_modified: datetime | None
    etag: str | None


class ObjectStore(Protocol):
    """Backend-neutral object storage contract.

    Writes are atomic and last-writer-wins. A missing object raises `FileNotFoundError`
    from `get_bytes`, `get_file`, `open_stream`, `copy` (source) and `stat`; `delete` and
    `exists` never raise for a missing key. Unsafe keys raise `UnsafeKeyError`. Permission
    and server errors propagate unchanged.
    """

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        """Store `data` under `key`; metadata arguments are ignored by the local backend."""
        ...

    def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        """Publish the completed file synchronously without consuming the source."""
        ...

    def get_bytes(self, key: str) -> bytes:
        """Return the whole object in memory."""
        ...

    def get_file(self, key: str, dest: Path) -> None:
        """Stream the object into `dest`, replacing it atomically."""
        ...

    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_BYTES,
    ) -> ObjectStream:
        """Open a chunked read; raises `InvalidRangeError` for an unsatisfiable range."""
        ...

    def copy(
        self,
        src_key: str,
        dst_key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Copy an object inside the store, giving the destination a fresh modification time."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object; a missing key is not an error."""
        ...

    def stat(self, key: str) -> ObjectInfo:
        """Return object metadata."""
        ...

    def exists(self, key: str) -> bool:
        """Return whether the object exists."""
        ...

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Yield objects whose key starts with `prefix`, in lexicographic key order."""
        ...
