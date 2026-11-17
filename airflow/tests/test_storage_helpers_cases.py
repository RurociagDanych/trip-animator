"""Comprehensive tests for storage helpers: traversal safety, existence, and multi-backend."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import FakeObjectStore
from trip_airflow.storage_helpers import (
    _normalize_prefix,
    list_objects_with_mtime,
    object_exists,
)
from trip_api.storage import LocalObjectStore, S3ObjectStore


class MinimalKeysStore:
    """Store implementing only .list_keys(prefix) without mtime."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = sorted(keys)

    def list_keys(self, prefix: str = "") -> list[str]:
        return [k for k in self.keys if not prefix or k.startswith(prefix)]

    def get_bytes(self, key: str) -> bytes:
        if key in self.keys:
            return b"content"
        raise FileNotFoundError(key)


class CustomExistsStore:
    """Store implementing .exists(key)."""

    def __init__(self, existing: set[str]) -> None:
        self.existing = existing

    def exists(self, key: str) -> bool:
        return key in self.existing


class UnsupportedStore:
    """Store that does not implement any expected interface."""

    pass


def test_normalize_prefix_safety() -> None:
    """Verify traversal attempts are rejected and leading slashes normalized."""
    assert _normalize_prefix("") == ""
    assert _normalize_prefix("/silver/stage") == "silver/stage"
    assert _normalize_prefix("silver/stage/") == "silver/stage/"

    for unsafe in ["..", "../etc/passwd", "silver/../../etc", "/../test"]:
        with pytest.raises(ValueError, match="Unsafe object prefix"):
            _normalize_prefix(unsafe)


def test_list_objects_with_mtime_backends(tmp_path: Path) -> None:
    """Test list_objects_with_mtime across Local, Fake, minimal, and unsupported stores."""
    # 1. LocalObjectStore
    local_dir = tmp_path / "local_store"
    local_dir.mkdir()
    store = LocalObjectStore(local_dir)
    store.put_bytes("silver/p1/s1.parquet", b"p1")
    store.put_bytes("silver/p1/s2.parquet", b"p2")
    store.put_bytes("bronze/p1/b1.gpx", b"b1")

    mtimes = list_objects_with_mtime(store, prefix="silver/")
    assert sorted(mtimes.keys()) == ["silver/p1/s1.parquet", "silver/p1/s2.parquet"]
    for mt in mtimes.values():
        assert isinstance(mt, datetime)
        assert mt.tzinfo == UTC

    # 2. FakeObjectStore
    fake = FakeObjectStore()
    fake.put_bytes("outputs/vid1.mp4", b"video")
    fake.put_bytes("outputs/thumb1.jpg", b"thumb")
    fake_mtimes = list_objects_with_mtime(fake, prefix="outputs/")
    assert sorted(fake_mtimes.keys()) == ["outputs/thumb1.jpg", "outputs/vid1.mp4"]
    for mt in fake_mtimes.values():
        assert isinstance(mt, datetime)

    # 3. MinimalKeysStore (no mtime support)
    min_store = MinimalKeysStore(["silver/a.parquet", "silver/b.parquet", "bronze/c.gpx"])
    min_mtimes = list_objects_with_mtime(min_store, prefix="silver/")
    assert min_mtimes == {"silver/a.parquet": None, "silver/b.parquet": None}

    # 4. Unsupported store raises TypeError
    with pytest.raises(TypeError, match="Unsupported ObjectStore"):
        list_objects_with_mtime(UnsupportedStore(), prefix="")


def test_object_exists_backends(tmp_path: Path, monkeypatch: Any) -> None:
    """Test object_exists across Local, Fake, CustomExists, Minimal, and S3 stores."""
    # 1. LocalObjectStore
    local_store = LocalObjectStore(tmp_path / "obj_store")
    local_store.put_bytes("test/file.txt", b"hello")
    assert object_exists(local_store, "test/file.txt")
    assert not object_exists(local_store, "test/missing.txt")

    # 2. FakeObjectStore
    fake = FakeObjectStore()
    fake.put_bytes("data/file.csv", b"csv")
    assert object_exists(fake, "data/file.csv")
    assert not object_exists(fake, "data/missing.csv")

    # 3. CustomExistsStore
    custom = CustomExistsStore({"item1", "item2"})
    assert object_exists(custom, "item1")
    assert not object_exists(custom, "item3")

    # 4. MinimalKeysStore fallback
    min_store = MinimalKeysStore(["a/b/c"])
    assert object_exists(min_store, "a/b/c")
    assert not object_exists(min_store, "a/b/missing")

    # 5. S3ObjectStore mock with ClientError
    class MockS3Client:
        def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
            if Key == "s3/found.mp4":
                return {"ContentLength": 100}
            if Key == "s3/not_found.mp4":
                raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
            if Key == "s3/no_such_key.mp4":
                raise ClientError(
                    {
                        "Error": {
                            "Code": "NoSuchKey",
                            "Message": "The specified key does not exist.",
                        }
                    },
                    "HeadObject",
                )
            raise ClientError({"Error": {"Code": "500", "Message": "Internal Error"}}, "HeadObject")

    s3_store = S3ObjectStore.__new__(S3ObjectStore)
    s3_store.bucket = "test-bucket"
    s3_store.client = MockS3Client()

    assert object_exists(s3_store, "s3/found.mp4") is True
    assert object_exists(s3_store, "s3/not_found.mp4") is False
    assert object_exists(s3_store, "s3/no_such_key.mp4") is False

    with pytest.raises(ClientError):
        object_exists(s3_store, "s3/server_error.mp4")

    # 6. Unsafe keys raise ValueError
    with pytest.raises(ValueError, match="Unsafe object prefix"):
        object_exists(local_store, "../escape")

    with pytest.raises(ValueError, match="Unsafe object key"):
        object_exists(local_store, "")
    with pytest.raises(ValueError, match="Unsafe object key"):
        object_exists(local_store, "/")
