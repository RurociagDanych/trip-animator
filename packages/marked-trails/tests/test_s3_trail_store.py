import json
import os
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from trip_marked_trails.cli import main
from trip_marked_trails.pipeline import BuildArtifact
from trip_marked_trails.trail_store import (
    ListingS3ObjectStore,
    TrailStoreSettings,
    build_trail_store,
)
from trip_object_store import LocalObjectStore

BUCKET = "trail-bucket"
PAYLOAD = b"PMTiles-fake-build-payload"


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.extra_args: dict[str, dict[str, Any]] = {}
        self.listing_error: ClientError | None = None
        self.truncate_uploads = False
        self.fail_uploads = False
        self.head_calls = 0

    def get_paginator(self, operation: str) -> "FakePaginator":
        assert operation == "list_objects_v2"
        return FakePaginator(self)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        if self.listing_error is not None:
            raise self.listing_error
        prefix = kwargs.get("Prefix", "")
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        keys = keys[: kwargs.get("MaxKeys", len(keys))]
        return {"Contents": [{"Key": key, "Size": len(self.objects[key])} for key in keys]}

    def delete_object(self, **kwargs: Any) -> None:
        assert kwargs["Bucket"] == BUCKET
        self.objects.pop(kwargs["Key"], None)

    def put_object(self, **kwargs: Any) -> None:
        key = kwargs["Key"]
        self.objects[key] = kwargs["Body"]
        self.extra_args[key] = {
            "ContentType": kwargs["ContentType"],
            "CacheControl": kwargs.get("CacheControl"),
        }

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs: Any) -> None:
        assert bucket == BUCKET
        data = Path(filename).read_bytes()
        self.objects[key] = data[: len(data) // 2] if self.truncate_uploads else data
        self.extra_args[key] = dict(kwargs["ExtraArgs"])
        if self.fail_uploads:
            raise RuntimeError("connection reset during upload")

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls += 1
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[key]), "ETag": '"etag"'}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        data = self.objects[key]

        class Body:
            def read(self) -> bytes:
                return data

            def close(self) -> None:
                return None

        return {"Body": Body()}


class FakePaginator:
    def __init__(self, client: FakeS3Client) -> None:
        self.client = client

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [self.client.list_objects_v2(**kwargs)]


def access_denied(operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "Access Denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        operation,
    )


def fake_build(region: str, work_dir: Path) -> BuildArtifact:
    work_dir.mkdir(parents=True, exist_ok=True)
    pmtiles = work_dir / "fake.pmtiles"
    pmtiles.write_bytes(PAYLOAD)
    return BuildArtifact(
        pmtiles_path=pmtiles, osm_data_date="2026-11-26T20:21:03Z", marked_trail_count=3
    )


def publish_to_s3(tmp_path: Path, client: FakeS3Client, *extra: str) -> int:
    return main(
        [
            "build-publish",
            "--region",
            "europe/andorra",
            "--work-dir",
            str(tmp_path / "work"),
            "--build-date",
            "2026-11-27",
            *extra,
        ],
        build_step=fake_build,
        s3_client=client,
    )


def test_s3_publish_writes_builds_and_pointer_under_the_prefix(tmp_path: Path) -> None:
    client = FakeS3Client()

    code = publish_to_s3(
        tmp_path, client, "--store", "s3", "--s3-bucket", BUCKET, "--s3-prefix", "marked-trails"
    )

    assert code == 0
    assert sorted(client.objects) == [
        "marked-trails/builds/2026-11-27/marked-trails.pmtiles",
        "marked-trails/current.json",
    ]
    pointer = json.loads(client.objects["marked-trails/current.json"])
    assert pointer["current_build_id"] == "2026-11-27"


def test_s3_publish_sets_content_types_and_cache_control(tmp_path: Path) -> None:
    client = FakeS3Client()

    publish_to_s3(tmp_path, client, "--store", "s3", "--s3-bucket", BUCKET)

    build = client.extra_args["marked-trails/builds/2026-11-27/marked-trails.pmtiles"]
    assert build["ContentType"] == "application/vnd.pmtiles"
    assert build["CacheControl"] == "public, max-age=31536000, immutable"
    pointer = client.extra_args["marked-trails/current.json"]
    assert pointer["ContentType"] == "application/json"
    assert "no-cache" in pointer["CacheControl"]


def test_s3_publish_keeps_the_previous_build_pointer_chain(tmp_path: Path) -> None:
    client = FakeS3Client()
    args = ("--store", "s3", "--s3-bucket", BUCKET)

    publish_to_s3(tmp_path, client, *args)
    publish_to_s3(tmp_path, client, *args)

    pointer = json.loads(client.objects["marked-trails/current.json"])
    assert pointer["current_build_id"] == "2026-11-27-2"
    assert pointer["previous_build_id"] == "2026-11-27"


def test_s3_store_is_selected_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_STORE", "s3")
    monkeypatch.setenv("MARKED_TRAILS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("MARKED_TRAILS_S3_PREFIX", "trails-v2")
    client = FakeS3Client()

    code = main(
        ["build-publish", "--region", "europe/andorra", "--work-dir", str(tmp_path / "work")],
        build_step=fake_build,
        s3_client=client,
    )

    assert code == 0
    assert "trails-v2/current.json" in client.objects


def test_local_store_still_requires_a_store_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKED_TRAILS_STORE", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(
            ["build-publish", "--region", "europe/andorra", "--work-dir", str(tmp_path / "w")],
            build_step=fake_build,
        )

    assert exc.value.code == 2


def test_s3_store_requires_a_bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARKED_TRAILS_S3_BUCKET", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "build-publish",
                "--region",
                "europe/andorra",
                "--work-dir",
                str(tmp_path / "w"),
                "--store",
                "s3",
            ],
            build_step=fake_build,
            s3_client=FakeS3Client(),
        )

    assert exc.value.code == 2


def test_build_trail_store_returns_local_and_s3_stores(tmp_path: Path) -> None:
    local = build_trail_store(TrailStoreSettings(backend="local", store_dir=tmp_path / "s"))
    remote = build_trail_store(
        TrailStoreSettings(backend="s3", s3_bucket=BUCKET, s3_prefix="/a/b/"),
        s3_client=FakeS3Client(),
    )

    assert isinstance(local, LocalObjectStore)
    assert isinstance(remote, ListingS3ObjectStore)
    assert remote.key_prefix == "a/b/"


def test_build_trail_store_rejects_an_unknown_backend() -> None:
    with pytest.raises(ValueError, match="backend"):
        build_trail_store(TrailStoreSettings(backend="gcs"))


S3_ARGS = ("--store", "s3", "--s3-bucket", BUCKET)
OLD_PMTILES = "marked-trails/builds/{}/marked-trails.pmtiles"


def test_s3_publish_prunes_builds_older_than_the_current_one(tmp_path: Path) -> None:
    client = FakeS3Client()
    for date in ("2026-11-01", "2026-11-02", "2026-11-03"):
        code = main(
            [
                "build-publish",
                "--region",
                "europe/andorra",
                "--work-dir",
                str(tmp_path / "work"),
                "--build-date",
                date,
                *S3_ARGS,
            ],
            build_step=fake_build,
            s3_client=client,
        )
        assert code == 0

    assert sorted(client.objects) == [
        OLD_PMTILES.format("2026-11-02"),
        OLD_PMTILES.format("2026-11-03"),
        "marked-trails/current.json",
    ]


def test_s3_publish_removes_a_short_upload_and_keeps_the_pointer(tmp_path: Path) -> None:
    client = FakeS3Client()
    assert publish_to_s3(tmp_path, client, *S3_ARGS) == 0
    pointer_before = client.objects["marked-trails/current.json"]
    client.truncate_uploads = True

    code = publish_to_s3(tmp_path, client, *S3_ARGS)

    assert code == 1
    assert client.objects["marked-trails/current.json"] == pointer_before
    assert sorted(client.objects) == [
        OLD_PMTILES.format("2026-11-27"),
        "marked-trails/current.json",
    ]


def test_s3_publish_removes_a_partial_object_after_a_failed_upload(tmp_path: Path) -> None:
    client = FakeS3Client()
    client.fail_uploads = True

    code = publish_to_s3(tmp_path, client, *S3_ARGS)

    assert code == 1
    assert client.objects == {}


def test_access_denied_on_the_existence_check_fails_the_publish(tmp_path: Path) -> None:
    client = FakeS3Client()
    assert publish_to_s3(tmp_path, client, *S3_ARGS) == 0
    pointer_before = client.objects["marked-trails/current.json"]
    client.listing_error = access_denied("ListObjectsV2")

    code = publish_to_s3(tmp_path, client, *S3_ARGS)

    assert code == 1
    assert client.objects["marked-trails/current.json"] == pointer_before


def test_s3_existence_check_lists_the_full_key_and_never_sends_head(tmp_path: Path) -> None:
    client = FakeS3Client()
    client.objects["marked-trails/builds/2026-11-01/marked-trails.pmtiles.bak"] = b"x"
    store = build_trail_store(TrailStoreSettings(backend="s3", s3_bucket=BUCKET), s3_client=client)

    assert not store.exists("builds/2026-11-01/marked-trails.pmtiles")
    client.objects["marked-trails/builds/2026-11-01/marked-trails.pmtiles"] = b"y"
    assert store.exists("builds/2026-11-01/marked-trails.pmtiles")
    assert client.head_calls == 0


@pytest.mark.parametrize(
    "extra",
    [
        ("--store", "gcs"),
        ("--store", "s3", "--s3-bucket", BUCKET, "--s3-prefix", " / "),
        ("--store", "s3", "--s3-bucket", BUCKET, "--store-dir", "/tmp/unused"),
    ],
    ids=["unknown-store", "empty-prefix", "store-dir-with-s3"],
)
def test_invalid_store_settings_exit_with_a_usage_error(
    tmp_path: Path, extra: tuple[str, ...]
) -> None:
    client = FakeS3Client()

    with pytest.raises(SystemExit) as exc:
        publish_to_s3(tmp_path, client, *extra)

    assert exc.value.code == 2
    assert client.objects == {}


def capture_boto_client(
    monkeypatch: pytest.MonkeyPatch, client: FakeS3Client
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_boto_client(*_args: Any, **kwargs: Any) -> FakeS3Client:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(boto3, "client", fake_boto_client)
    return calls


def s3_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MARKED_TRAILS_AWS_ACCESS_KEY_ID",
        "MARKED_TRAILS_AWS_SECRET_ACCESS_KEY",
        "MARKED_TRAILS_AWS_SESSION_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MARKED_TRAILS_STORE", "s3")
    monkeypatch.setenv("MARKED_TRAILS_S3_BUCKET", BUCKET)


def publish_without_injected_client(tmp_path: Path) -> int:
    return main(
        [
            "build-publish",
            "--region",
            "europe/andorra",
            "--work-dir",
            str(tmp_path / "work"),
            "--build-date",
            "2026-11-27",
        ],
        build_step=fake_build,
    )


def test_dedicated_credentials_go_only_to_the_trail_store_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3_environment(monkeypatch)
    monkeypatch.setenv("MARKED_TRAILS_AWS_ACCESS_KEY_ID", "AKIATRAILS")
    monkeypatch.setenv("MARKED_TRAILS_AWS_SECRET_ACCESS_KEY", "trail-secret")
    monkeypatch.setenv("MARKED_TRAILS_AWS_SESSION_TOKEN", "trail-token")
    client = FakeS3Client()
    calls = capture_boto_client(monkeypatch, client)

    assert publish_without_injected_client(tmp_path) == 0

    assert len(calls) == 1
    assert calls[0]["aws_access_key_id"] == "AKIATRAILS"
    assert calls[0]["aws_secret_access_key"] == "trail-secret"
    assert calls[0]["aws_session_token"] == "trail-token"
    assert "marked-trails/current.json" in client.objects
    assert "AWS_ACCESS_KEY_ID" not in os.environ


def test_without_dedicated_credentials_the_default_aws_chain_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3_environment(monkeypatch)
    calls = capture_boto_client(monkeypatch, FakeS3Client())

    assert publish_without_injected_client(tmp_path) == 0

    assert calls[0]["aws_access_key_id"] is None
    assert calls[0]["aws_secret_access_key"] is None
    assert "aws_session_token" not in calls[0]


@pytest.mark.parametrize(
    "present",
    [
        {"MARKED_TRAILS_AWS_ACCESS_KEY_ID": "AKIATRAILS"},
        {"MARKED_TRAILS_AWS_SECRET_ACCESS_KEY": "trail-secret"},
        {"MARKED_TRAILS_AWS_SESSION_TOKEN": "trail-token"},
    ],
)
def test_incomplete_dedicated_credentials_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, present: dict[str, str]
) -> None:
    s3_environment(monkeypatch)
    for name, value in present.items():
        monkeypatch.setenv(name, value)
    calls = capture_boto_client(monkeypatch, FakeS3Client())

    with pytest.raises(SystemExit) as exc:
        publish_without_injected_client(tmp_path)

    assert exc.value.code == 2
    assert calls == []


def test_credentials_are_not_part_of_the_settings_repr() -> None:
    settings = TrailStoreSettings(
        backend="s3",
        s3_bucket=BUCKET,
        s3_access_key_id="AKIATRAILS",
        s3_secret_access_key="trail-secret",
        s3_session_token="trail-token",
    )

    assert "trail-secret" not in repr(settings)
    assert "trail-token" not in repr(settings)
    assert "AKIATRAILS" not in repr(settings)
