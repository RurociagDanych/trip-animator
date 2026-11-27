"""S3ObjectStore behavior against botocore Stubber and small fakes."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import boto3
import pytest
from boto3.exceptions import S3UploadFailedError
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from trip_object_store import (
    DEFAULT_TRANSFER_CONFIG,
    InvalidRangeError,
    S3ObjectStore,
    UnsafeKeyError,
    is_not_found_error,
)

BUCKET = "test-bucket"


def make_client() -> Any:
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def make_store(**kwargs: Any) -> S3ObjectStore:
    return S3ObjectStore(bucket=BUCKET, client=make_client(), **kwargs)


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def client_error(
    code: str, *, http_status: int | None = None, op: str = "GetObject"
) -> ClientError:
    payload: dict[str, Any] = {"Error": {"Code": code, "Message": "Error"}}
    if http_status is not None:
        payload["ResponseMetadata"] = {"HTTPStatusCode": http_status}
    return ClientError(payload, op)  # type: ignore[arg-type]


class FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.closed = False
        self.read_called = False

    def read(self) -> bytes:
        self.read_called = True
        return self._payload

    def iter_chunks(self, chunk_size: int = 65536) -> Any:
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    def __init__(self) -> None:
        self.last_body: FakeBody | None = None
        self.put_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs["Key"])
        if name == "missing-nokey.mp4":
            raise client_error("NoSuchKey")
        if name == "missing-404.mp4":
            raise client_error("SomethingElse", http_status=404)
        if name == "boom-500.mp4":
            raise client_error("InternalError", http_status=500)
        if name == "invalid-range.mp4" and "Range" in kwargs:
            raise client_error("InvalidRange", http_status=416)
        data = b"0123456789ABCDEF"
        if "Range" in kwargs:
            start_s, end_s = str(kwargs["Range"]).removeprefix("bytes=").split("-", 1)
            data = data[int(start_s) : int(end_s) + 1]
        self.last_body = FakeBody(data)
        return {"Body": self.last_body, "ContentLength": len(data)}

    def delete_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        name = str(kwargs["Key"])
        if name == "missing-nokey.mp4":
            raise client_error("404", op="HeadObject")
        if name == "boom-head.mp4":
            raise client_error("InternalError", http_status=500, op="HeadObject")
        return {"ContentLength": 16}


def test_constructor_normalizes_empty_values_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_boto_client(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(boto3, "client", fake_boto_client)
    S3ObjectStore(
        bucket="prod",
        endpoint_url="",
        access_key_id="",
        secret_access_key="  ",
        region="",
    )
    assert calls[-1] == (
        ("s3",),
        {
            "endpoint_url": None,
            "aws_access_key_id": None,
            "aws_secret_access_key": None,
            "region_name": None,
        },
    )

    S3ObjectStore(bucket="prod")
    assert calls[-1][1] == {
        "endpoint_url": None,
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "region_name": None,
    }

    S3ObjectStore(
        bucket="local",
        endpoint_url=" http://minio:9000 ",
        access_key_id="minioadmin",
        secret_access_key="miniosecret",
        region="us-east-1",
    )
    assert calls[-1][1] == {
        "endpoint_url": "http://minio:9000",
        "aws_access_key_id": "minioadmin",
        "aws_secret_access_key": "miniosecret",
        "region_name": "us-east-1",
    }


def test_botocore_config_is_passed_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_boto_client(*_args: Any, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(boto3, "client", fake_boto_client)
    config = Config(retries={"max_attempts": 2})
    S3ObjectStore(bucket="b", botocore_config=config)
    assert calls[-1]["config"] is config


@pytest.mark.parametrize(
    "extra",
    [
        {"endpoint_url": "http://minio:9000"},
        {"access_key_id": "k"},
        {"secret_access_key": "s"},
        {"region": "eu-west-1"},
        {"botocore_config": Config()},
    ],
)
def test_injected_client_excludes_connection_arguments(extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="client cannot be combined"):
        S3ObjectStore(bucket=BUCKET, client=make_client(), **extra)


def test_injected_client_accepts_blank_connection_arguments() -> None:
    client = make_client()
    store = S3ObjectStore(bucket=BUCKET, client=client, endpoint_url="", region=" ")
    assert store.client is client
    assert store.bucket == BUCKET


def test_default_transfer_config_is_bounded_and_classic() -> None:
    config = DEFAULT_TRANSFER_CONFIG
    assert config.multipart_threshold == config.multipart_chunksize == 8 * 1024 * 1024
    assert config.use_threads is False
    assert config.preferred_transfer_client == "classic"  # type: ignore[attr-defined]
    assert make_store().transfer_config is DEFAULT_TRANSFER_CONFIG
    custom = TransferConfig(use_threads=False)
    assert make_store(transfer_config=custom).transfer_config is custom


def test_fake_client_round_trip_maps_errors_and_closes_streams() -> None:
    fake = FakeS3Client()
    store = S3ObjectStore(bucket="trip-prod", client=fake)

    store.put_bytes("video.mp4", b"0123456789ABCDEF", content_type="video/mp4")
    assert fake.put_calls == [
        {
            "Bucket": "trip-prod",
            "Key": "video.mp4",
            "Body": b"0123456789ABCDEF",
            "ContentType": "video/mp4",
        }
    ]
    assert store.get_bytes("video.mp4") == b"0123456789ABCDEF"
    assert fake.last_body is not None and fake.last_body.closed is True

    with pytest.raises(FileNotFoundError):
        store.get_bytes("missing-nokey.mp4")
    with pytest.raises(ClientError):
        store.get_bytes("boom-500.mp4")

    with pytest.raises(FileNotFoundError):
        store.open_stream("missing-nokey.mp4")
    with pytest.raises(FileNotFoundError):
        store.open_stream("missing-nokey.mp4", range_header="bytes=0-3")
    with pytest.raises(ClientError):
        store.open_stream("boom-head.mp4", range_header="bytes=0-3")
    with pytest.raises(InvalidRangeError):
        store.open_stream("invalid-range.mp4", range_header="bytes=0-3")

    full_stream = store.open_stream("video.mp4", chunk_size=5)
    assert full_stream.total_size == 16
    assert full_stream.content_length == 16
    assert full_stream.is_partial is False
    assert list(full_stream.chunks) == [b"01234", b"56789", b"ABCDE", b"F"]
    assert fake.last_body is not None
    assert fake.last_body.read_called is False
    assert fake.last_body.closed is True

    part_stream = store.open_stream("video.mp4", range_header="bytes=2-7", chunk_size=4)
    assert part_stream.is_partial is True
    assert (part_stream.start, part_stream.end) == (2, 7)
    assert part_stream.content_length == 6
    assert part_stream.total_size == 16
    assert b"".join(part_stream.chunks) == b"234567"

    with pytest.raises(InvalidRangeError):
        store.open_stream("video.mp4", range_header="bytes=100-200")


def test_get_bytes_closes_the_body_even_when_read_fails() -> None:
    class ExplodingBody(FakeBody):
        def read(self) -> bytes:
            raise OSError("connection reset")

    exploding = ExplodingBody(b"")

    class Client(FakeS3Client):
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"Body": exploding}

    store = S3ObjectStore(bucket=BUCKET, client=Client())
    with pytest.raises(OSError, match="connection reset"):
        store.get_bytes("a")
    assert exploding.closed is True


@pytest.mark.parametrize(
    ("code", "status"),
    [("NoSuchKey", None), ("404", None), ("NotFound", None), ("Weird", 404)],
)
def test_get_bytes_maps_not_found(code: str, status: int | None) -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error("get_object", service_error_code=code, http_status_code=status or 400)
        with pytest.raises(FileNotFoundError):
            store.get_bytes("photos/a.webp")


def test_get_bytes_http_404_without_a_known_code_is_not_found() -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error(
            "get_object", service_error_code="UnexpectedServerCode", http_status_code=404
        )
        with pytest.raises(FileNotFoundError):
            store.get_bytes("photos/a.webp")


@pytest.mark.parametrize("code", ["AccessDenied", "InternalError"])
def test_get_bytes_does_not_hide_permission_or_server_errors(code: str) -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error(
            "get_object", service_error_code=code, http_status_code=403 if "Access" in code else 500
        )
        with pytest.raises(ClientError) as info:
            store.get_bytes("photos/a.webp")
    assert not isinstance(info.value, FileNotFoundError)
    assert info.value.response["Error"]["Code"] == code


def test_is_not_found_error() -> None:
    assert is_not_found_error(client_error("NoSuchKey"))
    assert is_not_found_error(client_error("404"))
    assert is_not_found_error(client_error("NotFound"))
    assert is_not_found_error(client_error("Other", http_status=404))
    assert not is_not_found_error(client_error("AccessDenied", http_status=403))
    assert not is_not_found_error(client_error("InternalError"))


def test_put_bytes_sends_content_type_and_cache_control() -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_response(
            "put_object",
            {},
            {"Bucket": BUCKET, "Key": "a.json", "Body": b"{}", "ContentType": "application/json"},
        )
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": "b.json",
                "Body": b"{}",
                "ContentType": "application/json",
                "CacheControl": "no-cache",
            },
        )
        store.put_bytes("a.json", b"{}", content_type="application/json")
        store.put_bytes("/b.json", b"{}", content_type="application/json", cache_control="no-cache")
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("size", "part_count", "fail_part"),
    [(8_388_607, 0, False), (8_388_608, 1, False), (8_388_609, 2, False), (8_388_609, 2, True)],
    ids=["below-threshold", "at-threshold", "two-parts", "abort-on-part-error"],
)
def test_put_file_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, part_count: int, fail_part: bool
) -> None:
    source = tmp_path / "render.mp4"
    with source.open("wb") as file:
        file.truncate(size)
    store = make_store()
    key = "outputs/render.mp4"
    object_args = {"Bucket": BUCKET, "Key": key}
    metadata = {"ContentType": "video/mp4", "ChecksumAlgorithm": "CRC32"}
    upload_args = {**object_args, "UploadId": "test-upload"}
    upload_file = store.client.upload_file

    def check_upload(filename: str, bucket: str, object_key: str, **kwargs: Any) -> None:
        assert isinstance(filename, str) and filename == str(source)
        assert (bucket, object_key) == (BUCKET, key)
        assert kwargs["ExtraArgs"] == {"ContentType": "video/mp4"}
        config = kwargs["Config"]
        assert config.multipart_threshold == config.multipart_chunksize == 8_388_608
        assert config.use_threads is False
        assert config.preferred_transfer_client == "classic"
        upload_file(filename, bucket, object_key, **kwargs)

    monkeypatch.setattr(store.client, "upload_file", check_upload)
    with Stubber(store.client) as stub:
        if part_count == 0:
            stub.add_response("put_object", {}, {**object_args, **metadata, "Body": ANY})
        else:
            stub.add_response(
                "create_multipart_upload", {"UploadId": "test-upload"}, {**object_args, **metadata}
            )
            parts = []
            for number in range(1, part_count + 1):
                part_args = {
                    **upload_args,
                    "PartNumber": number,
                    "Body": ANY,
                    "ChecksumAlgorithm": "CRC32",
                }
                if fail_part and number == part_count:
                    stub.add_client_error(
                        "upload_part",
                        "AccessDenied",
                        http_status_code=403,
                        expected_params=part_args,
                    )
                    stub.add_response("abort_multipart_upload", {}, upload_args)
                else:
                    etag = f"etag-{number}"
                    stub.add_response("upload_part", {"ETag": etag}, part_args)
                    parts.append({"ETag": etag, "PartNumber": number})
            if not fail_part:
                stub.add_response(
                    "complete_multipart_upload",
                    {},
                    {**upload_args, "MultipartUpload": {"Parts": parts}},
                )

        if fail_part:
            with pytest.raises(S3UploadFailedError, match="AccessDenied"):
                store.put_file(key, source, content_type="video/mp4")
        else:
            store.put_file(key, source, content_type="video/mp4")
        stub.assert_no_pending_responses()

    assert source.stat().st_size == size


def test_put_file_sends_cache_control_and_honors_a_custom_transfer_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "trails.pmtiles"
    source.write_bytes(b"tiles")
    custom = TransferConfig(use_threads=False, multipart_threshold=1024)
    store = make_store(transfer_config=custom)
    seen: dict[str, Any] = {}
    upload_file = store.client.upload_file

    def spy(filename: str, bucket: str, object_key: str, **kwargs: Any) -> None:
        seen.update(kwargs)
        upload_file(filename, bucket, object_key, **kwargs)

    monkeypatch.setattr(store.client, "upload_file", spy)
    with Stubber(store.client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": "trails/v1/t.pmtiles",
                "Body": ANY,
                "ContentType": "application/octet-stream",
                "CacheControl": "public, max-age=31536000, immutable",
                "ChecksumAlgorithm": "CRC32",
            },
        )
        store.put_file(
            "trails/v1/t.pmtiles", source, cache_control="public, max-age=31536000, immutable"
        )
        stub.assert_no_pending_responses()
    assert seen["ExtraArgs"] == {
        "ContentType": "application/octet-stream",
        "CacheControl": "public, max-age=31536000, immutable",
    }
    assert seen["Config"] is custom


def test_copy_sends_replace_directive_only_when_metadata_is_given() -> None:
    class Recorder(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[Any, ...]] = []

        def copy(self, src: dict[str, str], bucket: str, key: str, ExtraArgs: Any = None) -> None:
            self.calls.append((src, bucket, key, ExtraArgs))
            if src["Key"] == "outputs/missing.mp4":
                raise client_error("NoSuchKey", op="CopyObject")
            if src["Key"] == "outputs/denied.mp4":
                raise client_error("AccessDenied", http_status=403, op="CopyObject")

    client = Recorder()
    store = S3ObjectStore(bucket="my-bucket", client=client)
    store.copy("outputs/a.mp4", "outputs/b.mp4", content_type="video/mp4")
    store.copy("outputs/a.mp4", "outputs/c.mp4")
    store.copy("outputs/a.mp4", "outputs/d.mp4", cache_control="no-cache")
    store.copy("outputs/a.mp4", "outputs/e.mp4", content_type="video/mp4", cache_control="no-cache")
    source = {"Bucket": "my-bucket", "Key": "outputs/a.mp4"}
    assert client.calls == [
        (
            source,
            "my-bucket",
            "outputs/b.mp4",
            {"ContentType": "video/mp4", "MetadataDirective": "REPLACE"},
        ),
        (source, "my-bucket", "outputs/c.mp4", None),
        (
            source,
            "my-bucket",
            "outputs/d.mp4",
            {"CacheControl": "no-cache", "MetadataDirective": "REPLACE"},
        ),
        (
            source,
            "my-bucket",
            "outputs/e.mp4",
            {
                "ContentType": "video/mp4",
                "CacheControl": "no-cache",
                "MetadataDirective": "REPLACE",
            },
        ),
    ]
    with pytest.raises(FileNotFoundError):
        store.copy("outputs/missing.mp4", "outputs/f.mp4")
    with pytest.raises(ClientError):
        store.copy("outputs/denied.mp4", "outputs/f.mp4")
    with pytest.raises(UnsafeKeyError):
        store.copy("outputs/a.mp4", "../escape.mp4")


def test_copy_cache_control_reaches_copy_object() -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_response("head_object", {"ContentLength": 3})
        stub.add_response(
            "copy_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": "outputs/b.mp4",
                "CopySource": {"Bucket": BUCKET, "Key": "outputs/a.mp4"},
                "MetadataDirective": "REPLACE",
                "ContentType": "video/mp4",
                "CacheControl": "no-cache",
            },
        )
        store.copy(
            "outputs/a.mp4", "outputs/b.mp4", content_type="video/mp4", cache_control="no-cache"
        )
        stub.assert_no_pending_responses()


def test_copy_missing_source_maps_to_file_not_found() -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        with pytest.raises(FileNotFoundError):
            store.copy("outputs/missing.mp4", "outputs/b.mp4")


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_delete_ignores_missing_key_under_any_not_found_code(code: str) -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error("delete_object", service_error_code=code, http_status_code=404)
        store.delete("outputs/gone.mp4")
        stub.assert_no_pending_responses()


def test_delete_propagates_other_errors() -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error(
            "delete_object", service_error_code="AccessDenied", http_status_code=403
        )
        with pytest.raises(ClientError):
            store.delete("outputs/a.mp4")
    with Stubber(store.client) as stub:
        stub.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": "outputs/a.mp4"})
        store.delete("outputs/a.mp4")
        stub.assert_no_pending_responses()


def test_stat_and_exists() -> None:
    store = make_store()
    modified = datetime(2026, 11, 27, 10, 30, tzinfo=UTC)
    with Stubber(store.client) as stub:
        stub.add_response(
            "head_object",
            {"ContentLength": 42, "ETag": '"abc"', "LastModified": modified},
            {"Bucket": BUCKET, "Key": "test/file.txt"},
        )
        info = store.stat("/test/file.txt")
        assert (info.key, info.size, info.etag, info.last_modified) == (
            "test/file.txt",
            42,
            '"abc"',
            modified,
        )

        stub.add_response("head_object", {"ContentLength": 1})
        assert store.exists("test/file.txt") is True
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        assert store.exists("test/missing.txt") is False
        stub.add_client_error("head_object", service_error_code="NoSuchKey", http_status_code=404)
        assert store.exists("test/missing.txt") is False
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        with pytest.raises(FileNotFoundError):
            store.stat("test/missing.txt")
        stub.add_client_error("head_object", service_error_code="500", http_status_code=500)
        with pytest.raises(ClientError):
            store.exists("test/server_error.mp4")
        stub.add_client_error("head_object", service_error_code="403", http_status_code=403)
        with pytest.raises(ClientError):
            store.stat("test/denied.mp4")


def test_get_file_downloads_atomically(tmp_path: Path) -> None:
    store = make_store()
    dest = tmp_path / "nested" / "s1.parquet"
    with Stubber(store.client) as stub:
        stub.add_response("head_object", {"ContentLength": 7, "ETag": '"e"'})
        stub.add_response(
            "get_object",
            {"Body": body(b"parquet"), "ContentLength": 7, "ETag": '"e"'},
        )
        store.get_file("silver/s1.parquet", dest)
        stub.assert_no_pending_responses()
    assert dest.read_bytes() == b"parquet"
    assert [p.name for p in dest.parent.iterdir()] == ["s1.parquet"]


def test_get_file_missing_object_leaves_no_destination(tmp_path: Path) -> None:
    store = make_store()
    dest = tmp_path / "s1.parquet"
    with Stubber(store.client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        with pytest.raises(FileNotFoundError):
            store.get_file("silver/s1.parquet", dest)
    assert list(tmp_path.iterdir()) == []


def test_get_file_permission_error_propagates(tmp_path: Path) -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_client_error("head_object", service_error_code="403", http_status_code=403)
        with pytest.raises(ClientError):
            store.get_file("silver/s1.parquet", tmp_path / "s1.parquet")
    assert list(tmp_path.iterdir()) == []


def test_s3_keys_are_validated_like_local_keys() -> None:
    fake = FakeS3Client()
    store = S3ObjectStore(bucket=BUCKET, client=fake)
    for bad in ("", "/", "../x", "a/../b", "a\\b", "a\x00b"):
        with pytest.raises(UnsafeKeyError):
            store.put_bytes(bad, b"x")
        with pytest.raises(UnsafeKeyError):
            store.get_bytes(bad)
        with pytest.raises(UnsafeKeyError):
            store.delete(bad)
        with pytest.raises(UnsafeKeyError):
            store.stat(bad)
        with pytest.raises(UnsafeKeyError):
            store.exists(bad)
    with pytest.raises(UnsafeKeyError):
        store.copy("ok", "../bad")
    with pytest.raises(UnsafeKeyError):
        list(store.iter_objects("../bad"))
    with pytest.raises(UnsafeKeyError):
        S3ObjectStore(bucket=BUCKET, client=fake, key_prefix="../x")
    assert fake.put_calls == []


def test_key_prefix_round_trip_including_iter_objects() -> None:
    store = make_store(key_prefix="/trails//")
    assert store.key_prefix == "trails/"
    with Stubber(store.client) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": "trails/current.json",
                "Body": b"{}",
                "ContentType": "application/json",
            },
        )
        store.put_bytes("current.json", b"{}", content_type="application/json")

        stub.add_response(
            "get_object",
            {"Body": body(b"{}")},
            {"Bucket": BUCKET, "Key": "trails/current.json"},
        )
        assert store.get_bytes("/current.json") == b"{}"

        stub.add_response(
            "head_object", {"ContentLength": 2}, {"Bucket": BUCKET, "Key": "trails/current.json"}
        )
        info = store.stat("current.json")
        assert info.key == "current.json"

        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "tok",
                "Contents": [
                    {"Key": "trails/v1/a.pmtiles", "Size": 10, "ETag": '"1"'},
                    {"Key": "trails/v1/b.pmtiles", "Size": 20},
                ],
            },
            {"Bucket": BUCKET, "Prefix": "trails/v1/"},
        )
        stub.add_response(
            "list_objects_v2",
            {
                "IsTruncated": False,
                "Contents": [
                    {
                        "Key": "trails/v1/c.pmtiles",
                        "Size": 30,
                        "LastModified": datetime(2026, 1, 2, tzinfo=UTC),
                    },
                ],
            },
            {"Bucket": BUCKET, "Prefix": "trails/v1/", "ContinuationToken": "tok"},
        )
        listed = list(store.iter_objects("v1/"))
        stub.assert_no_pending_responses()

    assert [(o.key, o.size, o.etag) for o in listed] == [
        ("v1/a.pmtiles", 10, '"1"'),
        ("v1/b.pmtiles", 20, None),
        ("v1/c.pmtiles", 30, None),
    ]
    assert listed[0].last_modified is None
    assert listed[2].last_modified == datetime(2026, 1, 2, tzinfo=UTC)


def test_iter_objects_without_prefix_omits_prefix_argument_and_empty_response() -> None:
    store = make_store()
    with Stubber(store.client) as stub:
        stub.add_response("list_objects_v2", {"IsTruncated": False}, {"Bucket": BUCKET})
        assert list(store.iter_objects()) == []
        stub.assert_no_pending_responses()


def test_key_prefix_scopes_copy_and_delete() -> None:
    class Recorder(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.copies: list[tuple[Any, ...]] = []
            self.deletes: list[dict[str, Any]] = []

        def copy(self, src: dict[str, str], bucket: str, key: str, ExtraArgs: Any = None) -> None:
            self.copies.append((src, bucket, key))

        def delete_object(self, **kwargs: Any) -> None:
            self.deletes.append(kwargs)

    client = Recorder()
    store = S3ObjectStore(bucket="b", client=client, key_prefix="p")
    store.copy("a", "c/d")
    store.delete("a")
    assert client.copies == [({"Bucket": "b", "Key": "p/a"}, "b", "p/c/d")]
    assert client.deletes == [{"Bucket": "b", "Key": "p/a"}]


def test_open_stream_uses_the_prefixed_key() -> None:
    class Recorder(FakeS3Client):
        def __init__(self) -> None:
            super().__init__()
            self.keys: list[str] = []

        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            self.keys.append(kwargs["Key"])
            return super().get_object(**kwargs)

    client = Recorder()
    store = S3ObjectStore(bucket="b", client=client, key_prefix="p/")
    stream = store.open_stream("video.mp4")
    assert b"".join(stream.chunks) == b"0123456789ABCDEF"
    assert client.keys == ["p/video.mp4"]
