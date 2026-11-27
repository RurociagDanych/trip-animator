import json
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from trip_marked_trails.cli import main
from trip_marked_trails.pipeline import BuildArtifact
from trip_marked_trails.trail_store import TrailStoreSettings, build_trail_store
from trip_object_store import LocalObjectStore, S3ObjectStore

BUCKET = "trail-bucket"
PAYLOAD = b"PMTiles-fake-build-payload"


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.extra_args: dict[str, dict[str, Any]] = {}

    def put_object(self, **kwargs: Any) -> None:
        key = kwargs["Key"]
        self.objects[key] = kwargs["Body"]
        self.extra_args[key] = {
            "ContentType": kwargs["ContentType"],
            "CacheControl": kwargs.get("CacheControl"),
        }

    def upload_file(self, filename: str, bucket: str, key: str, **kwargs: Any) -> None:
        assert bucket == BUCKET
        self.objects[key] = Path(filename).read_bytes()
        self.extra_args[key] = dict(kwargs["ExtraArgs"])

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
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
    assert isinstance(remote, S3ObjectStore)
    assert remote.key_prefix == "a/b/"


def test_build_trail_store_rejects_an_unknown_backend() -> None:
    with pytest.raises(ValueError, match="backend"):
        build_trail_store(TrailStoreSettings(backend="gcs"))
