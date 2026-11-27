"""build_object_store and materialize."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import boto3
import pytest
from trip_object_store import (
    LocalObjectStore,
    ObjectStoreConfig,
    S3ObjectStore,
    build_object_store,
    materialize,
)


def test_build_local_store(tmp_path: Path) -> None:
    store = build_object_store(
        ObjectStoreConfig(backend="local", local_dir=tmp_path / "objects", key_prefix="ignored")
    )
    assert isinstance(store, LocalObjectStore)
    assert store.root_dir == (tmp_path / "objects").resolve()
    store.put_bytes("a.txt", b"x")
    assert (tmp_path / "objects" / "a.txt").read_bytes() == b"x"


def test_build_s3_store_passes_the_config_to_boto3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_boto_client(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(boto3, "client", fake_boto_client)
    store = build_object_store(
        ObjectStoreConfig(
            backend="s3",
            local_dir=tmp_path,
            s3_bucket="prod-bucket",
            s3_endpoint_url="",
            s3_access_key_id="",
            s3_secret_access_key="   ",
            s3_region="eu-central-1",
            key_prefix="trails",
        )
    )
    assert isinstance(store, S3ObjectStore)
    assert store.bucket == "prod-bucket"
    assert store.key_prefix == "trails/"
    assert calls == [
        (
            ("s3",),
            {
                "endpoint_url": None,
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "region_name": "eu-central-1",
            },
        )
    ]


def test_build_s3_store_requires_a_bucket(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="s3_bucket"):
        build_object_store(ObjectStoreConfig(backend="s3", local_dir=tmp_path, s3_bucket=" "))


def test_build_rejects_unknown_backend(tmp_path: Path) -> None:
    config = ObjectStoreConfig(backend="gcs", local_dir=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown object store backend"):
        build_object_store(config)


def test_materialize_local_returns_the_existing_path_without_copying(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("silver/s1.parquet", b"parquet")
    dest = tmp_path / "tmp" / "s1.parquet"
    path = materialize(store, "silver/s1.parquet", dest)
    assert path == store.resolve_existing_path("silver/s1.parquet")
    assert not dest.exists()
    with pytest.raises(FileNotFoundError):
        materialize(store, "silver/missing.parquet", dest)


def test_materialize_downloads_for_other_stores(tmp_path: Path) -> None:
    class FakeStore:
        def get_file(self, key: str, dest: Path) -> None:
            dest.write_bytes(f"data:{key}".encode())

    dest = tmp_path / "s1.parquet"
    path = materialize(FakeStore(), "silver/s1.parquet", dest)  # type: ignore[arg-type]
    assert path == dest
    assert dest.read_bytes() == b"data:silver/s1.parquet"
