"""Tests for the Airflow storage helpers on the shared ObjectStore backends."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from conftest import FakeObjectStore
from trip_airflow.storage_helpers import (
    list_object_keys,
    list_objects_with_mtime,
    object_exists,
)
from trip_object_store import LocalObjectStore, S3ObjectStore, UnsafeKeyError


class _FakePaginator:
    def __init__(self, contents: list[dict[str, Any]]) -> None:
        self.contents = contents

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        prefix = kwargs.get("Prefix", "")
        return [{"Contents": [obj for obj in self.contents if obj["Key"].startswith(prefix)]}]


class _FakeS3Client:
    def __init__(self, contents: list[dict[str, Any]]) -> None:
        self.contents = contents

    def get_paginator(self, op_name: str) -> _FakePaginator:
        assert op_name == "list_objects_v2"
        return _FakePaginator(self.contents)

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        _ = Bucket
        if Key == "s3/found.mp4":
            return {"ContentLength": 100}
        if Key == "s3/not_found.mp4":
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        if Key == "s3/no_such_key.mp4":
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
                "HeadObject",
            )
        raise ClientError({"Error": {"Code": "500", "Message": "Internal Error"}}, "HeadObject")


def test_list_objects_with_mtime_local_backend(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "local_store")
    store.put_bytes("silver/p1/s2.parquet", b"p2")
    store.put_bytes("silver/p1/s1.parquet", b"p1")
    store.put_bytes("bronze/p1/b1.gpx", b"b1")

    mtimes = list_objects_with_mtime(store, prefix="silver/")
    assert list(mtimes) == ["silver/p1/s1.parquet", "silver/p1/s2.parquet"]
    for mt in mtimes.values():
        assert isinstance(mt, datetime)
        assert mt.tzinfo == UTC
    assert list_object_keys(store, prefix="/silver/") == list(mtimes)


def test_list_objects_with_mtime_s3_backend_sorts_and_normalizes_to_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    client = _FakeS3Client(
        [
            {"Key": "silver/b.parquet", "Size": 2, "ETag": '"b"', "LastModified": naive},
            {"Key": "silver/a.parquet", "Size": 1, "ETag": '"a"'},
            {"Key": "bronze/c.gpx", "Size": 3, "ETag": '"c"', "LastModified": naive},
        ]
    )
    store = S3ObjectStore(bucket="test-bucket", client=client)

    mtimes = list_objects_with_mtime(store, prefix="silver/")
    assert mtimes == {
        "silver/a.parquet": None,
        "silver/b.parquet": naive.replace(tzinfo=UTC),
    }
    assert list_object_keys(store) == ["bronze/c.gpx", "silver/a.parquet", "silver/b.parquet"]


def test_list_objects_with_mtime_fake_store() -> None:
    fake = FakeObjectStore()
    fake.put_bytes("outputs/vid1.mp4", b"video")
    fake.put_bytes("outputs/thumb1.jpg", b"thumb")
    fake_mtimes = list_objects_with_mtime(fake, prefix="outputs/")
    assert list(fake_mtimes) == ["outputs/thumb1.jpg", "outputs/vid1.mp4"]
    for mt in fake_mtimes.values():
        assert isinstance(mt, datetime)


@pytest.mark.parametrize("unsafe", ["..", "../etc/passwd", "silver/../../etc", "/../test"])
def test_list_objects_with_mtime_rejects_traversing_prefix(tmp_path: Path, unsafe: str) -> None:
    store = LocalObjectStore(tmp_path / "obj_store")
    with pytest.raises(UnsafeKeyError, match="Unsafe object prefix"):
        list_objects_with_mtime(store, prefix=unsafe)


def test_object_exists_local_and_fake_backends(tmp_path: Path) -> None:
    local_store = LocalObjectStore(tmp_path / "obj_store")
    local_store.put_bytes("test/file.txt", b"hello")
    assert object_exists(local_store, "test/file.txt")
    assert not object_exists(local_store, "test/missing.txt")

    fake = FakeObjectStore()
    fake.put_bytes("data/file.csv", b"csv")
    assert object_exists(fake, "data/file.csv")
    assert not object_exists(fake, "data/missing.csv")


def test_object_exists_s3_backend_maps_not_found_and_raises_other_errors() -> None:
    s3_store = S3ObjectStore(bucket="test-bucket", client=_FakeS3Client([]))

    assert object_exists(s3_store, "s3/found.mp4") is True
    assert object_exists(s3_store, "s3/not_found.mp4") is False
    assert object_exists(s3_store, "s3/no_such_key.mp4") is False
    with pytest.raises(ClientError):
        object_exists(s3_store, "s3/server_error.mp4")


@pytest.mark.parametrize("unsafe", ["../escape", "", "/"])
def test_object_exists_rejects_unsafe_keys(tmp_path: Path, unsafe: str) -> None:
    local_store = LocalObjectStore(tmp_path / "obj_store")
    s3_store = S3ObjectStore(bucket="test-bucket", client=_FakeS3Client([]))
    for store in (local_store, s3_store):
        with pytest.raises(UnsafeKeyError, match="Unsafe object key"):
            object_exists(store, unsafe)
