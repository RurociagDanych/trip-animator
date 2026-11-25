from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from trip_worker import (
    EncoderError,
    LocalObjectStore,
    S3ObjectStore,
    StreamingVideoEncoder,
    WorkerSettings,
    build_ffmpeg_concat_args,
    build_ffmpeg_encode_args,
    build_ffmpeg_pipe_encode_args,
    build_ffmpeg_thumbnail_args,
    build_ffprobe_args,
    build_object_store,
    concat_mp4_chunks,
    extract_thumbnail,
    is_render_request_allowed,
    is_render_request_allowed_async,
    probe_video,
    resolve_and_validate_external_target_async,
    verify_mp4_output,
)


def test_playwright_ssrf_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = frozenset({"tiles.openfreemap.org"})
    origin = "http://web:8080"

    assert is_render_request_allowed(
        "http://web:8080/render/job-1",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert is_render_request_allowed(
        "http://caddy/render/job-1",
        render_origin="http://caddy:80",
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert is_render_request_allowed(
        "data:image/png;base64,AAAA",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert is_render_request_allowed(
        "blob:http://web:8080/1234",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert is_render_request_allowed(
        "https://tiles.openfreemap.org/styles/liberty",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "http://tiles.openfreemap.org/styles/liberty",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "http://169.254.169.254/latest/meta-data",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "http://api:8000/api/v1/renders",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "file:///etc/passwd",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "https://evil.example.com/script.js",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "https://user:pass@tiles.openfreemap.org/styles/liberty",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "https://127.0.0.1/tile.pbf",
        render_origin=origin,
        allowed_external_hosts=frozenset({"127.0.0.1"}),
        resolve_dns=False,
    )

    # DNS rebinding protection: allowlisted host resolving to private IP must be blocked
    monkeypatch.setattr(
        "trip_worker.ssrf.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("10.0.0.5", 443))],
    )
    assert not is_render_request_allowed(
        "https://tiles.openfreemap.org/styles/liberty",
        render_origin=origin,
        allowed_external_hosts=allowed,
        resolve_dns=True,
    )


def test_ffmpeg_constant_args_and_validation() -> None:
    args = build_ffmpeg_encode_args(
        frame_pattern_path=Path("/tmp/render/frame_%06d.png"),
        output_mp4_path=Path("/tmp/render/out.mp4"),
        fps=30,
        width=1920,
        height=1080,
    )
    assert args[0] == "ffmpeg"
    assert "-c:v" in args and "libx264" in args
    assert "-pix_fmt" in args and "yuv420p" in args
    assert "-movflags" in args and "+faststart" in args
    assert "-an" in args

    pipe_args = build_ffmpeg_pipe_encode_args(
        output_mp4_path=Path("/tmp/render/out.mp4"),
        fps=15,
        width=1080,
        height=1920,
        frame_format="png",
    )
    assert pipe_args[:6] == ["ffmpeg", "-y", "-f", "image2pipe", "-vcodec", "png"]
    assert "pipe:0" in pipe_args
    assert "libx264" in pipe_args and "yuv420p" in pipe_args and "+faststart" in pipe_args

    with pytest.raises(ValueError):
        build_ffmpeg_encode_args(
            frame_pattern_path=Path("/tmp/f_%06d.png"),
            output_mp4_path=Path("/tmp/out.mp4"),
            fps=24,
            width=1920,
            height=1080,
        )

    with pytest.raises(ValueError):
        build_ffmpeg_pipe_encode_args(
            output_mp4_path=Path("-flag-injection.mp4"),
            fps=30,
            width=1920,
            height=1080,
        )

    with pytest.raises(ValueError):
        build_ffmpeg_pipe_encode_args(
            output_mp4_path=Path("/tmp/odd.mp4"),
            fps=30,
            width=1921,
            height=1080,
        )

    thumb_args = build_ffmpeg_thumbnail_args(
        input_mp4_path=Path("/tmp/render/out.mp4"),
        output_thumb_path=Path("/tmp/render/thumb.webp"),
        seek_seconds=0.5,
        width=640,
    )
    assert thumb_args[0] == "ffmpeg"
    assert "libwebp" in thumb_args
    assert "-vframes" in thumb_args and "1" in thumb_args

    with pytest.raises(ValueError):
        build_ffmpeg_thumbnail_args(
            input_mp4_path=Path("/tmp/render/out.mp4"),
            output_thumb_path=Path("/tmp/render/thumb.exe"),
        )

    probe = build_ffprobe_args(Path("/tmp/render/out.mp4"))
    assert probe[0] == "ffprobe"


def test_streaming_encoder_probe_and_thumbnail_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_mp4 = tmp_path / "render.mp4"
    thumb_webp = tmp_path / "thumb.webp"
    written_chunks: list[bytes] = []

    class FakeStdin:
        def write(self, data: bytes) -> int:
            written_chunks.append(data)
            return len(data)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProc:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            assert kwargs.get("shell") is False
            assert args[0] == "ffmpeg"
            self.stdin = FakeStdin()
            self.stderr = None

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            out_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            return 0

        def poll(self) -> int | None:
            return 0

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", FakeProc)

    encoder = StreamingVideoEncoder(
        output_mp4_path=out_mp4,
        fps=30,
        width=1280,
        height=720,
    )
    encoder.write_frame(b"PNG_FRAME_0")
    encoder.write_frame(b"PNG_FRAME_1")
    finished = encoder.finish(timeout_seconds=10.0)
    assert finished == out_mp4
    assert written_chunks == [b"PNG_FRAME_0", b"PNG_FRAME_1"]

    probe_json = json.dumps(
        {
            "streams": [
                {
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 1280,
                    "height": 720,
                    "nb_frames": "60",
                    "duration": "2.000",
                }
            ],
            "format": {"duration": "2.000"},
        }
    )

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("shell") is False
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, stdout=probe_json, stderr="")
        if args[0] == "ffmpeg":
            Path(args[-1]).write_bytes(b"RIFF....WEBP")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected binary: {args[0]}")

    monkeypatch.setattr("trip_worker.encoder.subprocess.run", fake_run)

    probed = probe_video(out_mp4)
    assert probed.codec_name == "h264"
    assert probed.pix_fmt == "yuv420p"
    assert probed.nb_frames == 60

    verified = verify_mp4_output(
        out_mp4,
        expected_width=1280,
        expected_height=720,
        expected_duration_s=2.0,
    )
    assert verified.width == 1280

    with pytest.raises(EncoderError, match="Resolution mismatch"):
        verify_mp4_output(out_mp4, expected_width=1920, expected_height=1080)

    thumb_result = extract_thumbnail(out_mp4, thumb_webp)
    assert thumb_result == thumb_webp
    assert thumb_webp.read_bytes().startswith(b"RIFF")


def test_local_object_store_and_settings_dsn(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("outputs/test.mp4", b"video-bytes")
    assert store.get_bytes("outputs/test.mp4") == b"video-bytes"

    with pytest.raises(ValueError):
        store.get_bytes("../../etc/passwd")

    with pytest.raises(ValueError):
        store.put_bytes("", b"nope")

    settings = WorkerSettings(
        database_url="postgresql+psycopg://u:p@localhost:5432/db",
        MAP_TILE_ALLOWLIST=(
            "https://tiles.openfreemap.org, "
            "http://tiles.internal:8080/styles, "
            "https://[2001:db8::1]:8443/tiles, "
            "[2001:0db8::2], "
            "2001:db8::3, "
            "2001:db8::4/tiles"
        ),
    )
    assert settings.psycopg_dsn == "postgresql://u:p@localhost:5432/db"
    assert settings.allowed_external_hosts == frozenset(
        {
            "tiles.openfreemap.org",
            "tiles.internal",
            "2001:db8::1",
            "2001:db8::2",
            "2001:db8::3",
            "2001:db8::4",
        }
    )


def test_ssrf_blocks_caddy_api_paths_and_handles_ipv6() -> None:
    allowed = frozenset({"2606:4700:4700::1111", "2001:db8::1"})
    caddy_origin = "http://caddy"

    assert is_render_request_allowed(
        "http://caddy/render/job-1",
        render_origin=caddy_origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    assert is_render_request_allowed(
        "http://caddy/assets/index.js",
        render_origin=caddy_origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    for blocked_url in (
        "http://caddy/api/v1/projects",
        "http://caddy/api",
        "http://caddy/api/",
        "http://caddy/render/../api/v1/projects",
        "http://caddy//api/v1/projects",
        "http://caddy/%61pi/v1/projects",
        "http://caddy/API/v1/renders",
    ):
        assert not is_render_request_allowed(
            blocked_url,
            render_origin=caddy_origin,
            allowed_external_hosts=allowed,
            resolve_dns=False,
        )

    assert is_render_request_allowed(
        "https://[2606:4700:4700::1111]:8443/tile.pbf",
        render_origin=caddy_origin,
        allowed_external_hosts=allowed,
        resolve_dns=False,
    )
    for restricted_v6 in (
        "https://[2001:db8::1]/tile.pbf",
        "https://[::1]/tile.pbf",
        "https://[fe80::1]/tile.pbf",
        "https://[fc00::1]/tile.pbf",
        "https://[::ffff:127.0.0.1]/tile.pbf",
    ):
        assert not is_render_request_allowed(
            restricted_v6,
            render_origin=caddy_origin,
            allowed_external_hosts=frozenset({"::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1"}),
            resolve_dns=False,
        )


async def test_ssrf_async_dns_uses_loop_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    calls: list[tuple[str, Any, int]] = []

    async def fake_getaddrinfo(
        host: Any, port: Any, *, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[Any]:
        _ = family, proto, flags
        calls.append((str(host), port, type))
        if host == "rebind.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 443, 0, 0))]
        if host == "broken.openfreemap.org":
            raise socket.gaierror("DNS failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    allowed = frozenset(
        {"tiles.openfreemap.org", "rebind.openfreemap.org", "broken.openfreemap.org"}
    )
    assert await is_render_request_allowed_async(
        "https://tiles.openfreemap.org/styles/liberty",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
        resolve_dns=True,
    )
    assert not await is_render_request_allowed_async(
        "https://rebind.openfreemap.org/styles/liberty",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
        resolve_dns=True,
    )
    assert not await is_render_request_allowed_async(
        "https://broken.openfreemap.org/styles/liberty",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
        resolve_dns=True,
    )
    assert len(calls) == 3


@pytest.mark.parametrize("existing", [False, True])
def test_local_put_file_does_not_publish_partial_copy(
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

    monkeypatch.setattr("trip_worker.storage.shutil.copyfile", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        store.put_file(key, source, content_type="video/mp4")

    assert source.read_bytes() == b"full source"
    assert sorted(path.name for path in (store.root_dir / "outputs").iterdir()) == (
        ["test.mp4"] if existing else []
    )
    if existing:
        assert store.get_bytes(key) == b"previous export"


def test_s3_object_store_passes_none_for_empty_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_boto_client(*args: Any, **kwargs: Any) -> object:
        recorded_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr("trip_worker.storage.boto3.client", fake_boto_client)

    settings = WorkerSettings(
        object_store_backend="s3",
        s3_endpoint_url="",
        s3_access_key_id="",
        s3_secret_access_key="   ",
        s3_bucket="prod-bucket",
        s3_region="eu-central-1",
    )
    store = build_object_store(settings)
    assert isinstance(store, S3ObjectStore)
    assert recorded_calls[-1][0] == ("s3",)
    assert recorded_calls[-1][1] == {
        "endpoint_url": None,
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "region_name": "eu-central-1",
    }

    S3ObjectStore(
        endpoint_url="http://minio:9000",
        access_key_id="minioadmin",
        secret_access_key="miniosecret",
        bucket="local-bucket",
        region="us-east-1",
    )
    assert recorded_calls[-1][1] == {
        "endpoint_url": "http://minio:9000",
        "aws_access_key_id": "minioadmin",
        "aws_secret_access_key": "miniosecret",
        "region_name": "us-east-1",
    }


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "AccessDenied"])
def test_s3_get_bytes_distinguishes_a_missing_object_from_access_denial(code: str) -> None:
    from botocore.exceptions import ClientError
    from botocore.stub import Stubber

    store = S3ObjectStore(
        access_key_id="test", secret_access_key="test", bucket="test-bucket", region="us-east-1"
    )
    with Stubber(store.client) as stub:
        stub.add_client_error("get_object", service_error_code=code)
        expected = ClientError if code == "AccessDenied" else FileNotFoundError
        with pytest.raises(expected):
            store.get_bytes("photos/a.webp")


def test_ssrf_ipv4_ipv6_bypass_vectors_url_normalization_and_ipv6_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = frozenset({"tiles.openfreemap.org", "[2606:4700:4700::1111]:8443"})
    origin = "http://caddy"

    for bypass_url in (
        "http://127.0.0.1/render/job-1",
        "https://127.0.0.1/tile.pbf",
        "http://0.0.0.0/render/job-1",
        "https://0.0.0.0/tile.pbf",
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/api/v1/projects",
        "https://100.64.0.1/tile.pbf",
        "http://[::1]/render/job-1",
        "https://[::1]/tile.pbf",
        "https://[::ffff:127.0.0.1]/tile.pbf",
        "https://[fc00::1]/tile.pbf",
        "https://[fe80::1]/tile.pbf",
        "https://[fe80::1%25eth0]/tile.pbf",
        "https://[::]/tile.pbf",
        "https://10.0.0.1/tile.pbf",
        "https://172.16.0.1/tile.pbf",
        "https://192.168.1.1/tile.pbf",
        "https://224.0.0.1/tile.pbf",
    ):
        assert not is_render_request_allowed(
            bypass_url,
            render_origin=origin,
            allowed_external_hosts=allowed,
            resolve_dns=False,
        ), f"Expected SSRF block for {bypass_url}"

    # Restricted IPs must remain blocked even if accidentally added to allowed_external_hosts.
    poisoned_allowlist = frozenset(
        {
            "127.0.0.1",
            "0.0.0.0",  # noqa: S104
            "169.254.169.254",
            "::1",
            "::ffff:127.0.0.1",
            "fc00::1",
            "fe80::1",
            "::",
            "224.0.0.1",
        }
    )
    for host in poisoned_allowlist:
        bracketed = f"[{host}]" if ":" in host else host
        assert not is_render_request_allowed(
            f"https://{bracketed}/tile.pbf",
            render_origin=origin,
            allowed_external_hosts=poisoned_allowlist,
            resolve_dns=False,
        )

    for normalized_bypass in (
        "http://caddy/api/../api/v1/projects",
        "http://caddy/assets/../../api/v1/projects",
        "http://caddy/render/../api",
        "http://caddy/%2e%2e/api/v1/projects",
        "https://tiles.openfreemap.org@127.0.0.1/tile.pbf",
        "http://caddy@127.0.0.1/render/job-1",
        "https://127.0.0.1@tiles.openfreemap.org/tile.pbf",
        "http://user@caddy/render/job-1",
        "http://caddy:99999/render/job-1",
        "https://tiles.openfreemap.org:99999/tile.pbf",
    ):
        assert not is_render_request_allowed(
            normalized_bypass,
            render_origin=origin,
            allowed_external_hosts=allowed,
            resolve_dns=False,
        ), f"Expected URL normalization/userinfo block for {normalized_bypass}"

    settings = WorkerSettings(
        MAP_TILE_ALLOWLIST="https://[2606:4700:4700::1111]:8443/tiles, [2606:4700:4700::1112]:443"
    )
    assert settings.allowed_external_hosts == frozenset(
        {"2606:4700:4700::1111", "2606:4700:4700::1112"}
    )
    assert is_render_request_allowed(
        "https://[2606:4700:4700::1111]:8443/tiles/1.pbf",
        render_origin=origin,
        allowed_external_hosts=settings.allowed_external_hosts,
        resolve_dns=False,
    )
    assert is_render_request_allowed(
        "https://[2606:4700:4700::1112]/tiles/1.pbf",
        render_origin=origin,
        allowed_external_hosts=settings.allowed_external_hosts,
        resolve_dns=False,
    )

    v6_origin = "http://[2606:4700:4700::1111]:8080"
    assert is_render_request_allowed(
        "http://[2606:4700:4700::1111]:8080/render/job-1",
        render_origin=v6_origin,
        allowed_external_hosts=frozenset(),
        resolve_dns=False,
    )
    assert not is_render_request_allowed(
        "http://[2606:4700:4700::1111]:8080/api/../api/v1/projects",
        render_origin=v6_origin,
        allowed_external_hosts=frozenset(),
        resolve_dns=False,
    )

    monkeypatch.setattr(
        "trip_worker.ssrf.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert is_render_request_allowed(
        "https://tiles.openfreemap.org/styles/liberty",
        render_origin=origin,
        allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
        resolve_dns=True,
    )

    def _raise_nxdomain(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr("trip_worker.ssrf.socket.getaddrinfo", _raise_nxdomain)
    assert not is_render_request_allowed(
        "https://tiles.openfreemap.org/styles/liberty",
        render_origin=origin,
        allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
        resolve_dns=True,
    )


async def test_ssrf_async_dns_nxdomain_and_ipv6_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(
        host: Any, port: Any, *, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[Any]:
        _ = port, family, type, proto, flags
        if host == "nxdomain.openfreemap.org":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host == "v4mapped.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0))]
        if host == "linklocal.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 443, 0, 0))]
        if host == "ula.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fc00::1", 443, 0, 0))]
        if host == "cgnat-or-v6.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    allowed = frozenset(
        {
            "nxdomain.openfreemap.org",
            "v4mapped.openfreemap.org",
            "linklocal.openfreemap.org",
            "ula.openfreemap.org",
            "cgnat-or-v6.openfreemap.org",
            "meta.openfreemap.org",
        }
    )
    for host in allowed:
        assert not await is_render_request_allowed_async(
            f"https://{host}/styles/liberty",
            render_origin="http://caddy",
            allowed_external_hosts=allowed,
            resolve_dns=True,
        ), f"Expected async DNS block for {host}"


def test_streaming_encoder_nonzero_exit_stderr_capture_and_timeout_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    class _FakeStdin:
        def __init__(self, *, broken: bool = False) -> None:
            self.broken = broken
            self.closed = False

        def write(self, data: bytes) -> int:
            if self.broken:
                raise BrokenPipeError("broken pipe")
            return len(data)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class _ErrorProc:
        def __init__(self, _args: list[str], **_kwargs: Any) -> None:
            self.stdin = _FakeStdin()
            self.stderr = io.BytesIO(b"Error line 1: invalid frame\nError line 2: libx264 failed\n")
            self.killed = False
            self.terminated = False

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            return 1

        def poll(self) -> int | None:
            return 1

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", _ErrorProc)
    enc_err = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "fail.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    enc_err.start()
    enc_err.write_frame(b"PNG_BYTES")
    with pytest.raises(EncoderError, match="exited with code 1") as exc_info:
        enc_err.finish(timeout_seconds=5.0)
    assert "Error line 1: invalid frame" in str(exc_info.value)
    assert "Error line 2: libx264 failed" in str(exc_info.value)

    class _BrokenPipeProc:
        def __init__(self, _args: list[str], **_kwargs: Any) -> None:
            self.stdin = _FakeStdin(broken=True)
            self.stderr = io.BytesIO(b"ffmpeg pipe died\n")

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            return 1

        def poll(self) -> int | None:
            return 1

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", _BrokenPipeProc)
    enc_pipe = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "broken.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    enc_pipe.start()
    assert enc_pipe._stderr_thread is not None
    enc_pipe._stderr_thread.join(timeout=2.0)
    with pytest.raises(EncoderError, match="ffmpeg pipe died"):
        enc_pipe.write_frame(b"FRAME")

    timeout_state: dict[str, Any] = {"killed": False, "waits": 0}

    class _TimeoutProc:
        def __init__(self, args: list[str], **_kwargs: Any) -> None:
            self.args = args
            self.stdin = _FakeStdin()
            self.stderr = io.BytesIO(b"encoding stalled\n")

        def wait(self, timeout: float | None = None) -> int:
            timeout_state["waits"] += 1
            if timeout_state["waits"] == 1:
                raise subprocess.TimeoutExpired(self.args, timeout or 1.0)
            return -9

        def kill(self) -> None:
            timeout_state["killed"] = True

        def poll(self) -> int | None:
            return -9 if timeout_state["killed"] else None

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", _TimeoutProc)
    enc_timeout = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "timeout.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    enc_timeout.start()
    with pytest.raises(EncoderError, match="exceeded timeout"):
        enc_timeout.finish(timeout_seconds=1.5)
    assert timeout_state["killed"] is True
    assert timeout_state["waits"] == 2
    assert enc_timeout._stderr_thread is not None
    assert not enc_timeout._stderr_thread.is_alive()

    abort_state: dict[str, Any] = {"terminated": False, "killed": False, "waits": 0}

    class _StubbornProc:
        def __init__(self, args: list[str], **_kwargs: Any) -> None:
            self.args = args
            self.stdin = _FakeStdin()
            self.stderr = io.BytesIO(b"")

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            abort_state["terminated"] = True

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            abort_state["waits"] += 1
            if abort_state["waits"] == 1:
                raise subprocess.TimeoutExpired(self.args, 5.0)
            return -9

        def kill(self) -> None:
            abort_state["killed"] = True

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", _StubbornProc)
    enc_abort = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "abort.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    enc_abort.start()
    enc_abort.abort()
    assert abort_state == {"terminated": True, "killed": True, "waits": 2}
    enc_abort.abort()

    enc_unstarted = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "unstarted.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    with pytest.raises(EncoderError, match="never started"):
        enc_unstarted.finish()

    class _EmptyOutProc:
        def __init__(self, _args: list[str], **_kwargs: Any) -> None:
            self.stdin = _FakeStdin()
            self.stderr = None

        def wait(self, timeout: float | None = None) -> int:
            _ = timeout
            return 0

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", _EmptyOutProc)
    enc_empty = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "missing.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    enc_empty.start()
    with pytest.raises(EncoderError, match="empty frame"):
        enc_empty.write_frame(b"")
    with pytest.raises(EncoderError, match="empty or missing MP4"):
        enc_empty.finish()
    assert enc_empty.finish() == tmp_path / "missing.mp4"

    def _raise_oserror(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("ffmpeg binary not found")

    monkeypatch.setattr("trip_worker.encoder.subprocess.Popen", _raise_oserror)
    enc_missing_bin = StreamingVideoEncoder(
        output_mp4_path=tmp_path / "nobin.mp4",
        fps=30,
        width=1280,
        height=720,
    )
    with pytest.raises(EncoderError, match="Failed to launch ffmpeg"):
        enc_missing_bin.start()


def test_probe_verify_and_thumbnail_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mp4 = tmp_path / "v.mp4"
    thumb = tmp_path / "t.png"

    with pytest.raises(ValueError, match="Unsupported dimensions"):
        build_ffmpeg_encode_args(
            frame_pattern_path=tmp_path / "f_%06d.png",
            output_mp4_path=mp4,
            fps=30,
            width=4096,
            height=1080,
        )
    with pytest.raises(ValueError, match="seek_seconds"):
        build_ffmpeg_thumbnail_args(
            input_mp4_path=mp4,
            output_thumb_path=thumb,
            seek_seconds=-1.0,
        )
    with pytest.raises(ValueError, match="Unsupported thumbnail width"):
        build_ffmpeg_thumbnail_args(
            input_mp4_path=mp4,
            output_thumb_path=thumb,
            width=641,
        )
    png_args = build_ffmpeg_thumbnail_args(input_mp4_path=mp4, output_thumb_path=thumb)
    assert "png" in png_args

    def _timeout_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, 30.0)

    monkeypatch.setattr("trip_worker.encoder.subprocess.run", _timeout_run)
    with pytest.raises(EncoderError, match="ffprobe execution failed"):
        probe_video(mp4)
    with pytest.raises(EncoderError, match="Thumbnail ffmpeg execution failed"):
        extract_thumbnail(mp4, thumb)

    monkeypatch.setattr(
        "trip_worker.encoder.subprocess.run",
        lambda args, **_kw: subprocess.CompletedProcess(args, 2, stdout="", stderr="bad moov atom"),
    )
    with pytest.raises(EncoderError, match="bad moov atom"):
        probe_video(mp4)

    monkeypatch.setattr(
        "trip_worker.encoder.subprocess.run",
        lambda args, **_kw: subprocess.CompletedProcess(args, 0, stdout="{not-json", stderr=""),
    )
    with pytest.raises(EncoderError, match="invalid JSON"):
        probe_video(mp4)

    monkeypatch.setattr(
        "trip_worker.encoder.subprocess.run",
        lambda args, **_kw: subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"streams": []}), stderr=""
        ),
    )
    with pytest.raises(EncoderError, match="no video streams"):
        probe_video(mp4)

    def _mock_probe_payload(
        *, codec: str = "h264", pix_fmt: str = "yuv420p", duration: str = "2.0"
    ) -> None:
        payload = json.dumps(
            {
                "streams": [
                    {
                        "codec_name": codec,
                        "pix_fmt": pix_fmt,
                        "width": 1280,
                        "height": 720,
                        "duration": duration,
                    }
                ],
                "format": {"duration": duration},
            }
        )
        monkeypatch.setattr(
            "trip_worker.encoder.subprocess.run",
            lambda args, **_kw: subprocess.CompletedProcess(args, 0, stdout=payload, stderr=""),
        )

    _mock_probe_payload(codec="vp9")
    with pytest.raises(EncoderError, match="Expected h264"):
        verify_mp4_output(mp4, expected_width=1280, expected_height=720)

    _mock_probe_payload(pix_fmt="yuv444p")
    with pytest.raises(EncoderError, match="Expected yuv420p"):
        verify_mp4_output(mp4, expected_width=1280, expected_height=720)

    _mock_probe_payload(duration="not-a-float")
    with pytest.raises(EncoderError, match="duration too short"):
        verify_mp4_output(mp4, expected_width=1280, expected_height=720)

    _mock_probe_payload(duration="10.0")
    with pytest.raises(EncoderError, match="Duration mismatch"):
        verify_mp4_output(
            mp4,
            expected_width=1280,
            expected_height=720,
            expected_duration_s=2.0,
            duration_tolerance_s=1.0,
        )

    seen_seeks: list[str] = []

    def _fail_first_thumb(args: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        ss_idx = args.index("-ss")
        seen_seeks.append(args[ss_idx + 1])
        if len(seen_seeks) == 1:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="seek past eof")
        Path(args[-1]).write_bytes(b"PNG_OK")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("trip_worker.encoder.subprocess.run", _fail_first_thumb)
    assert extract_thumbnail(mp4, thumb, seek_seconds=0.5) == thumb
    assert seen_seeks == ["0.500", "0.000"]

    monkeypatch.setattr(
        "trip_worker.encoder.subprocess.run",
        lambda args, **_kw: subprocess.CompletedProcess(args, 1, stdout="", stderr="ffmpeg error"),
    )
    with pytest.raises(EncoderError, match="Thumbnail extraction failed: ffmpeg error"):
        extract_thumbnail(mp4, thumb, seek_seconds=0.5)


def test_worker_settings_chunk_tile_cache_and_gop_concat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("TILE_CACHE_DIR", str(tmp_path / "tiles"))
    cfg = WorkerSettings()
    assert cfg.worker_concurrency == 4
    assert cfg.resolved_tile_cache_dir == (tmp_path / "tiles")
    assert cfg.tile_cache_max_bytes == 512 * 1024 * 1024
    assert cfg.chunk_threshold_frames == 1800
    assert cfg.chunk_size_frames == 300
    assert cfg.chunk_preroll_frames == 15

    cfg_direct = WorkerSettings(worker_concurrency=2, tile_cache_dir="  ")
    assert cfg_direct.worker_concurrency == 2
    assert cfg_direct.resolved_tile_cache_dir is None

    gop_args = build_ffmpeg_pipe_encode_args(
        output_mp4_path=tmp_path / "chunk.mp4",
        fps=30,
        width=1280,
        height=720,
        gop_aligned=True,
    )
    assert "-g" in gop_args and "30" in gop_args
    assert "-keyint_min" in gop_args
    assert "-sc_threshold" in gop_args and "0" in gop_args

    concat_args = build_ffmpeg_concat_args(
        concat_list_path=tmp_path / "list.txt",
        output_mp4_path=tmp_path / "final.mp4",
    )
    assert concat_args[:7] == ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i"]
    assert "-c" in concat_args and "copy" in concat_args
    assert "+faststart" in concat_args

    c1 = tmp_path / "c1.mp4"
    c2 = tmp_path / "c2.mp4"
    c1.write_bytes(b"CHUNK1")
    c2.write_bytes(b"CHUNK2")
    out = tmp_path / "assembled.mp4"

    seen_lists: list[str] = []

    def _fake_concat_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("shell") is False
        list_idx = args.index("-i") + 1
        seen_lists.append(Path(args[list_idx]).read_text(encoding="utf-8"))
        Path(args[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42FINAL")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("trip_worker.encoder.subprocess.run", _fake_concat_run)
    assert concat_mp4_chunks([c1, c2], out) == out
    assert str(c1.resolve()) in seen_lists[0]
    assert str(c2.resolve()) in seen_lists[0]
    assert out.read_bytes().endswith(b"FINAL")

    with pytest.raises(ValueError, match="at least one chunk"):
        concat_mp4_chunks([], out)
    with pytest.raises(EncoderError, match="Missing or empty MP4 chunk"):
        concat_mp4_chunks([tmp_path / "missing.mp4"], out)


async def test_resolve_and_validate_external_target_and_ip_pinned_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import ssl

    import httpcore
    import httpx
    from trip_worker.ssrf import (
        PinnedSsrfAsyncTransport,
        SsrfBlockedConnectError,
        SsrfPinnedNetworkBackend,
        pin_external_target,
    )

    loop = asyncio.get_running_loop()
    dns_calls: list[str] = []

    async def fake_getaddrinfo(host: Any, _port: Any, **_kwargs: Any) -> list[Any]:
        dns_calls.append(str(host))
        if host == "mixed.openfreemap.org":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    allowed = frozenset({"tiles.openfreemap.org", "mixed.openfreemap.org"})

    d_data = await resolve_and_validate_external_target_async(
        "data:image/png;base64,AAAA",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
    )
    assert d_data.kind == "data_or_blob" and d_data.target is None

    d_origin = await resolve_and_validate_external_target_async(
        "http://caddy/render/job-1",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
    )
    assert d_origin.kind == "render_origin" and d_origin.target is None

    d_api = await resolve_and_validate_external_target_async(
        "http://caddy/api/v1/projects",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
    )
    assert d_api.kind == "blocked"

    d_mixed = await resolve_and_validate_external_target_async(
        "https://mixed.openfreemap.org/tile.pbf",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
    )
    assert d_mixed.kind == "blocked"

    d_ok = await resolve_and_validate_external_target_async(
        "https://tiles.openfreemap.org/styles/liberty",
        render_origin="http://caddy",
        allowed_external_hosts=allowed,
    )
    assert d_ok.kind == "external_https"
    assert d_ok.target is not None
    assert d_ok.target.host == "tiles.openfreemap.org"
    assert d_ok.target.port == 443
    assert d_ok.target.resolved_ip == "93.184.216.34"

    class FakeSock:
        def __init__(self, peer_ip: str) -> None:
            self._peer_ip = peer_ip

        def getpeername(self) -> tuple[str, int]:
            return (self._peer_ip, 443)

    class FakeStream(httpcore.AsyncNetworkStream):
        def __init__(self, peer_ip: str | None) -> None:
            self.peer_ip = peer_ip
            self.closed = False
            self.tls_server_hostname: str | None = None
            self.reads = 0

        async def read(
            self,
            max_bytes: int,
            timeout: float | None = None,  # noqa: ASYNC109
        ) -> bytes:
            _ = max_bytes, timeout
            self.reads += 1
            if self.reads == 1:
                return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok"
            return b""

        async def write(
            self,
            buffer: bytes,
            timeout: float | None = None,  # noqa: ASYNC109
        ) -> None:
            _ = buffer, timeout

        async def aclose(self) -> None:
            self.closed = True

        async def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,  # noqa: ASYNC109
        ) -> httpcore.AsyncNetworkStream:
            _ = ssl_context, timeout
            self.tls_server_hostname = server_hostname
            return self

        def get_extra_info(self, info: str) -> Any:
            if info == "socket" and self.peer_ip is not None:
                return FakeSock(self.peer_ip)
            return None

    class FakeInnerBackend(httpcore.AsyncNetworkBackend):
        def __init__(self, peer_ip: str | None) -> None:
            self.peer_ip = peer_ip
            self.connect_calls: list[tuple[str, int]] = []
            self.last_stream: FakeStream | None = None

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            _ = timeout, local_address, socket_options
            self.connect_calls.append((host, port))
            stream = FakeStream(self.peer_ip)
            self.last_stream = stream
            return stream

        async def connect_unix_socket(
            self, *_args: Any, **_kwargs: Any
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("unix socket must not be called")

        async def sleep(self, seconds: float) -> None:
            _ = seconds

    dns_calls_before = len(dns_calls)
    inner_ok = FakeInnerBackend("93.184.216.34")
    transport_ok = PinnedSsrfAsyncTransport(network_backend=inner_ok, resolve_dns=True)
    async with httpx.AsyncClient(transport=transport_ok, follow_redirects=False) as client:
        with pin_external_target(d_ok.target):
            resp = await client.get("https://tiles.openfreemap.org/styles/liberty")
    assert resp.status_code == 200
    assert resp.text == "ok"
    # Connected directly to pinned IP without a second DNS lookup
    assert len(dns_calls) == dns_calls_before
    assert inner_ok.connect_calls == [("93.184.216.34", 443)]
    assert inner_ok.last_stream is not None
    assert inner_ok.last_stream.tls_server_hostname == "tiles.openfreemap.org"

    # If the connected peer IP is restricted (e.g. link-local metadata), abort BEFORE TLS
    inner_bad_peer = FakeInnerBackend("169.254.169.254")
    transport_bad_peer = PinnedSsrfAsyncTransport(network_backend=inner_bad_peer, resolve_dns=True)
    async with httpx.AsyncClient(transport=transport_bad_peer, follow_redirects=False) as client:
        with pin_external_target(d_ok.target):
            with pytest.raises(httpx.ConnectError, match="restricted connected peer IP"):
                await client.get("https://tiles.openfreemap.org/styles/liberty")
    assert inner_bad_peer.last_stream is not None
    assert inner_bad_peer.last_stream.closed is True
    assert inner_bad_peer.last_stream.tls_server_hostname is None

    # Direct backend checks: missing peer IP fails closed, unix socket fails closed
    backend_no_peer = SsrfPinnedNetworkBackend(
        inner=FakeInnerBackend(None),
        resolve_dns=False,
        require_verified_peer=True,
    )
    with pytest.raises(SsrfBlockedConnectError, match="Unable to verify"):
        await backend_no_peer.connect_tcp("93.184.216.34", 443)
    with pytest.raises(SsrfBlockedConnectError, match="Unix domain sockets"):
        await backend_no_peer.connect_unix_socket("/var/run/docker.sock")
