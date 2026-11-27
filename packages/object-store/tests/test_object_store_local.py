"""LocalObjectStore behavior."""

from __future__ import annotations

import os
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from trip_object_store import LocalObjectStore, UnsafeKeyError


def test_blocks_path_traversal(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("bronze/test.gpx", b"<gpx/>")
    assert store.get_bytes("bronze/test.gpx") == b"<gpx/>"
    with pytest.raises(ValueError):
        store.put_bytes("../escape.txt", b"bad")
    with pytest.raises(ValueError):
        store.get_bytes("../../etc/passwd")
    with pytest.raises(ValueError):
        store.put_bytes("", b"nope")
    store.delete("bronze/never-existed.gpx")


def test_symlink_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    store = LocalObjectStore(tmp_path / "root")
    (store.root_dir / "link").symlink_to(outside)
    with pytest.raises(UnsafeKeyError, match="escapes storage root"):
        store.get_bytes("link/secret.txt")
    assert list(store.iter_objects()) == []


def test_missing_object_raises_file_not_found(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.get_bytes("nope.bin")
    with pytest.raises(FileNotFoundError):
        store.stat("nope.bin")
    with pytest.raises(FileNotFoundError):
        store.open_stream("nope.bin")
    with pytest.raises(FileNotFoundError):
        store.copy("nope.bin", "other.bin")
    with pytest.raises(FileNotFoundError):
        store.get_file("nope.bin", tmp_path / "dest.bin")
    assert not (tmp_path / "dest.bin").exists()


def test_streaming_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from trip_object_store import InvalidRangeError

    store = LocalObjectStore(tmp_path)
    store.put_bytes("empty.bin", b"")
    empty_stream = store.open_stream("empty.bin")
    assert empty_stream.total_size == 0
    assert empty_stream.content_length == 0
    assert list(empty_stream.chunks) == []

    store.put_bytes("sample.bin", b"0123456789ABCDEF")
    full = store.open_stream("sample.bin")
    assert full.is_partial is False
    assert (full.start, full.end, full.total_size) == (0, 15, 16)
    assert b"".join(full.chunks) == b"0123456789ABCDEF"

    suffix_stream = store.open_stream("sample.bin", range_header="bytes=-5", chunk_size=2)
    assert suffix_stream.is_partial is True
    assert suffix_stream.start == 11
    assert suffix_stream.end == 15
    assert b"".join(suffix_stream.chunks) == b"BCDEF"

    open_end_stream = store.open_stream("sample.bin", range_header="bytes=10-", chunk_size=4)
    assert open_end_stream.start == 10
    assert open_end_stream.end == 15
    assert b"".join(open_end_stream.chunks) == b"ABCDEF"

    with pytest.raises(InvalidRangeError):
        store.open_stream("sample.bin", range_header="bytes=100-200")

    orig_open = Path.open

    class _ExplodingHandle:
        def __init__(self, real_fh: Any) -> None:
            self._real = real_fh
            self.closed = False

        def seek(self, _offset: int) -> int:
            raise OSError("seek failed")

        def close(self) -> None:
            self.closed = True
            self._real.close()

    handles: list[_ExplodingHandle] = []

    def _patched_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        fh = _ExplodingHandle(orig_open(self, *args, **kwargs))
        handles.append(fh)
        return fh

    monkeypatch.setattr(Path, "open", _patched_open)
    with pytest.raises(OSError, match="seek failed"):
        store.open_stream("sample.bin", range_header="bytes=5-10")
    assert handles and handles[0].closed is True


def test_copy_creates_independent_object_with_fresh_mtime(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("outputs/src.mp4", b"mp4-binary-payload", content_type="video/mp4")
    old = time.time() - 86_400
    os.utime(store.resolve_existing_path("outputs/src.mp4"), (old, old))

    store.copy("outputs/src.mp4", "outputs/nested/dst.mp4", content_type="video/mp4")

    assert store.get_bytes("outputs/nested/dst.mp4") == b"mp4-binary-payload"
    assert store.get_bytes("outputs/src.mp4") == b"mp4-binary-payload"
    dst_mtime = store.resolve_existing_path("outputs/nested/dst.mp4").stat().st_mtime
    assert dst_mtime > old + 3_600
    with pytest.raises(FileNotFoundError):
        store.copy("outputs/missing.mp4", "outputs/dst2.mp4")
    with pytest.raises(ValueError, match="Unsafe object key"):
        store.copy("outputs/src.mp4", "../escape.mp4")


def test_put_bytes_overwrites_and_ignores_metadata(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("a/b.txt", b"one", content_type="text/plain", cache_control="no-cache")
    store.put_bytes("a/b.txt", b"two")
    assert store.get_bytes("a/b.txt") == b"two"


@pytest.mark.parametrize("existing", [False, True])
def test_put_bytes_is_atomic_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    store = LocalObjectStore(tmp_path)
    if existing:
        store.put_bytes("outputs/test.bin", b"previous")

    def fail_write(self: Path, data: bytes) -> int:
        self.open("wb").write(data[:2])
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail_write)
    with pytest.raises(OSError, match="disk full"):
        store.put_bytes("outputs/test.bin", b"new content")
    monkeypatch.undo()

    names = sorted(p.name for p in (store.root_dir / "outputs").iterdir())
    assert names == (["test.bin"] if existing else [])
    if existing:
        assert store.get_bytes("outputs/test.bin") == b"previous"


@pytest.mark.parametrize("key", ["", "../escape.mp4", "outputs/../../escape.mp4"])
def test_put_file_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(ValueError, match="Unsafe object key"):
        store.put_file(key, source)

    assert not list(store.root_dir.iterdir())
    assert source.read_bytes() == b"source"


def test_put_file_copies_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    store = LocalObjectStore(tmp_path / "objects")
    store.put_file("outputs/new/video.mp4", source, content_type="video/mp4")
    assert store.get_bytes("outputs/new/video.mp4") == b"video"
    assert source.read_bytes() == b"video"


def test_put_file_missing_source_does_not_create_destination_parent(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")

    with pytest.raises(FileNotFoundError):
        store.put_file("outputs/new/video.mp4", tmp_path / "missing.mp4")

    assert not list(store.root_dir.iterdir())


@pytest.mark.parametrize("existing", [False, True])
def test_put_file_does_not_publish_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"full source")
    store = LocalObjectStore(tmp_path / "objects")
    key = "outputs/test.mp4"
    if existing:
        store.put_bytes(key, b"previous export")

    def fail_copy(src: Path, dst: Path) -> None:
        assert src == source
        dst.write_bytes(b"partial copy")
        raise OSError("disk full")

    monkeypatch.setattr("trip_object_store.local.shutil.copyfile", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        store.put_file(key, source, content_type="video/mp4")

    assert source.read_bytes() == b"full source"
    assert sorted(path.name for path in (store.root_dir / "outputs").iterdir()) == (
        ["test.mp4"] if existing else []
    )
    if existing:
        assert store.get_bytes(key) == b"previous export"


def test_get_file_replaces_destination_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("silver/s1.parquet", b"parquet")
    dest = tmp_path / "out" / "s1.parquet"
    store.get_file("silver/s1.parquet", dest)
    assert dest.read_bytes() == b"parquet"

    def fail_copy(src: Path, dst: Path) -> None:
        dst.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr("trip_object_store.local.shutil.copyfile", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        store.get_file("silver/s1.parquet", dest)
    assert dest.read_bytes() == b"parquet"
    assert [p.name for p in dest.parent.iterdir()] == ["s1.parquet"]


def test_stat_and_exists(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("/test/file.txt", b"hello")
    info = store.stat("test/file.txt")
    assert info.key == "test/file.txt"
    assert info.size == 5
    assert info.etag is None
    assert info.last_modified is not None and info.last_modified.tzinfo == UTC

    assert store.exists("test/file.txt")
    assert not store.exists("test/missing.txt")
    assert not store.exists("test")
    with pytest.raises(UnsafeKeyError):
        store.exists("")
    with pytest.raises(UnsafeKeyError):
        store.exists("/")
    with pytest.raises(UnsafeKeyError):
        store.exists("../escape")


def test_delete_removes_object_and_ignores_missing(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("a.txt", b"x")
    store.delete("a.txt")
    store.delete("a.txt")
    assert not store.exists("a.txt")


def test_iter_objects_is_sorted_filtered_and_skips_temp_files(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("silver/p1/s2.parquet", b"p2")
    store.put_bytes("silver/p1/s1.parquet", b"p1")
    store.put_bytes("bronze/p1/b1.gpx", b"b1")
    leftover = store.root_dir / "silver" / "p1" / f".s1.parquet.{'a' * 32}.tmp"
    leftover.write_bytes(b"partial")
    (store.root_dir / "silver" / "empty-dir").mkdir()

    silver = list(store.iter_objects("silver/"))
    assert [o.key for o in silver] == ["silver/p1/s1.parquet", "silver/p1/s2.parquet"]
    assert [o.size for o in silver] == [2, 2]
    for obj in silver:
        assert obj.last_modified is not None and obj.last_modified.tzinfo == UTC
    assert [o.key for o in store.iter_objects()] == [
        "bronze/p1/b1.gpx",
        "silver/p1/s1.parquet",
        "silver/p1/s2.parquet",
    ]
    assert [o.key for o in store.iter_objects("/silver/p1/s2")] == ["silver/p1/s2.parquet"]
    with pytest.raises(UnsafeKeyError, match="Unsafe object prefix"):
        list(store.iter_objects("../x"))


def test_regular_dotfiles_are_listed(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes(".hidden", b"x")
    store.put_bytes("a/.keep.tmp", b"x")
    assert [o.key for o in store.iter_objects()] == [".hidden", "a/.keep.tmp"]
