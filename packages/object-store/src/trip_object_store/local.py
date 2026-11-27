"""Traversal-safe local filesystem `ObjectStore`."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from trip_object_store.atomic import is_temp_name, staged_path
from trip_object_store.base import DEFAULT_CHUNK_BYTES, ObjectInfo
from trip_object_store.keys import UnsafeKeyError, normalize_key, normalize_prefix
from trip_object_store.ranges import ObjectStream, parse_byte_range


class LocalObjectStore:
    """Object store backed by a directory; keys map to relative file paths under `root_dir`."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        resolved = (self.root_dir / normalize_key(key)).resolve()
        if not resolved.is_relative_to(self.root_dir):
            raise UnsafeKeyError(f"Object key escapes storage root: {key!r}")
        return resolved

    def resolve_existing_path(self, key: str) -> Path:
        """Return the filesystem path of an existing object; raises `FileNotFoundError`."""
        target = self._safe_path(key)
        if not target.is_file():
            raise FileNotFoundError(f"Object not found: {key!r}")
        return target

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        _ = (content_type, cache_control)
        with staged_path(self._safe_path(key)) as tmp:
            tmp.write_bytes(data)

    def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        _ = (content_type, cache_control)
        target = self._safe_path(key)
        source.stat()  # Reject an already missing source before creating destination directories.
        with staged_path(target) as tmp:
            shutil.copyfile(source, tmp)

    def get_bytes(self, key: str) -> bytes:
        return self.resolve_existing_path(key).read_bytes()

    def get_file(self, key: str, dest: Path) -> None:
        source = self.resolve_existing_path(key)
        with staged_path(dest) as tmp:
            shutil.copyfile(source, tmp)

    def open_stream(
        self,
        key: str,
        *,
        range_header: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_BYTES,
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

    def copy(
        self,
        src_key: str,
        dst_key: str,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        _ = (content_type, cache_control)
        src = self.resolve_existing_path(src_key)
        dst = self._safe_path(dst_key)
        # copyfile, not copy2: the copy must get a fresh mtime like an S3 CopyObject.
        with staged_path(dst) as tmp:
            shutil.copyfile(src, tmp)

    def delete(self, key: str) -> None:
        self._safe_path(key).unlink(missing_ok=True)

    def stat(self, key: str) -> ObjectInfo:
        path = self.resolve_existing_path(key)
        return self._info(normalize_key(key), path)

    def exists(self, key: str) -> bool:
        try:
            self.resolve_existing_path(key)
        except FileNotFoundError:
            return False
        return True

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        clean_prefix = normalize_prefix(prefix)
        keys: dict[str, Path] = {}
        for path in self.root_dir.rglob("*"):
            if is_temp_name(path.name) or not path.is_file():
                continue
            if not path.resolve().is_relative_to(self.root_dir):
                continue
            key = path.relative_to(self.root_dir).as_posix()
            if key.startswith(clean_prefix):
                keys[key] = path
        for key in sorted(keys):
            try:
                yield self._info(key, keys[key])
            except FileNotFoundError:
                continue  # Deleted while listing.

    @staticmethod
    def _info(key: str, path: Path) -> ObjectInfo:
        st = path.stat()
        return ObjectInfo(
            key=key,
            size=st.st_size,
            last_modified=datetime.fromtimestamp(st.st_mtime, tz=UTC),
            etag=None,
        )
