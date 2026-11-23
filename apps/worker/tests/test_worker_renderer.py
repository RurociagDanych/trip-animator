from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from trip_telemetry import RenderSpecV2
from trip_worker import (
    ManifestPoint,
    ManifestStage,
    RenderCancelledError,
    RenderError,
    RenderManifest,
    TileCache,
    WorkerSettings,
    build_chromium_launch_args,
    make_ssrf_route_handler,
    render_manifest_with_page,
)


def _sample_manifest(
    total_frames: int = 4,
    *,
    intro_hold_seconds: float = 0.0,
    outro_hold_seconds: float = 0.0,
) -> RenderManifest:
    return RenderManifest(
        job_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        project_title="Fictional Loop",
        spec=RenderSpecV2(
            resolution="1280x720",
            fps=30,
            duration_seconds=5.0,
            intro_hold_seconds=intro_hold_seconds,
            outro_hold_seconds=outro_hold_seconds,
        ),
        width=1280,
        height=720,
        fps=30,
        duration_s=5.0,
        total_frames=total_frames,
        is_preview=False,
        stages=[
            ManifestStage(
                id=str(uuid.uuid4()),
                name="Stage 1",
                day_index=1,
                stage_order=0,
                color="#F97316",
                transition="none",
                distance_m=1500.0,
                elevation_gain_m=120.0,
                moving_seconds=300.0,
                elapsed_seconds=320.0,
                has_timestamps=True,
                points=[
                    ManifestPoint(
                        lat=0.1,
                        lon=0.2,
                        lon_unwrapped=0.2,
                        elev_m=1400.0,
                        cum_dist_m=0.0,
                        speed_mps=5.0,
                        grade_pct=0.0,
                        elapsed_s=0.0,
                        ts_utc="2001-06-01T08:00:00Z",
                        segment_idx=0,
                    )
                ],
            )
        ],
    )


class FakeRoute:
    def __init__(self, url: str, *, method: str = "GET") -> None:
        self.request = type("Req", (), {"url": url, "method": method})()
        self.continued = False
        self.aborted_reason: str | None = None
        self.fulfilled: dict[str, Any] | None = None

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, error_code: str = "failed") -> None:
        self.aborted_reason = error_code

    async def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled = kwargs


class FakePage:
    def __init__(self, *, js_frame_count: int) -> None:
        self.js_frame_count = js_frame_count
        self.routes: list[tuple[str, Any]] = []
        self.init_scripts: list[str] = []
        self.visited_urls: list[str] = []
        self.frames_rendered: list[int] = []
        self.listeners: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.listeners[event] = handler

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.visited_urls.append(url)

    async def wait_for_function(self, _expr: str, **_kwargs: Any) -> None:
        return None

    async def evaluate(self, expr: str, arg: Any = None) -> Any:
        if "getFrameCount" in expr:
            return self.js_frame_count
        if "setFrame" in expr:
            self.frames_rendered.append(int(arg))
            return None
        return None

    async def screenshot(self, **kwargs: Any) -> bytes:
        assert kwargs.get("full_page") is False
        idx = self.frames_rendered[-1]
        return f"FRAME_{idx}".encode()


class FakeEncoder:
    def __init__(self) -> None:
        self.started = False
        self.finished = False
        self.aborted = False
        self.frames: list[bytes] = []

    def start(self) -> None:
        self.started = True

    def write_frame(self, frame_bytes: bytes) -> None:
        self.frames.append(frame_bytes)

    def finish(self, *, timeout_seconds: float = 300.0) -> Path:
        _ = timeout_seconds
        self.finished = True
        return Path("/tmp/out.mp4")

    def abort(self) -> None:
        self.aborted = True


async def test_ssrf_route_handler_blocks_unexpected_origins_and_caddy_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    import httpx

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    seen_external: list[tuple[str, str, str | None, str | None]] = []

    def _external_handler(request: httpx.Request) -> httpx.Response:
        seen_external.append(
            (
                request.method,
                str(request.url),
                request.headers.get("host"),
                request.extensions.get("sni_hostname"),
            )
        )
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_external_handler),
        follow_redirects=False,
    ) as client:
        handler = make_ssrf_route_handler(
            render_origin="http://caddy",
            allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
            resolve_dns=True,
            http_client=client,
        )

        ok_app = FakeRoute("http://caddy/assets/index.js")
        await handler(ok_app)  # type: ignore[arg-type]
        assert ok_app.continued is True

        ok_render = FakeRoute("http://caddy/render/job-1")
        await handler(ok_render)  # type: ignore[arg-type]
        assert ok_render.continued is True

        bad_caddy_api = FakeRoute("http://caddy/api/v1/projects")
        await handler(bad_caddy_api)  # type: ignore[arg-type]
        assert bad_caddy_api.aborted_reason == "blockedbyclient"

        ok_tiles = FakeRoute("https://tiles.openfreemap.org/styles/liberty")
        await handler(ok_tiles)  # type: ignore[arg-type]
        assert ok_tiles.continued is False
        assert ok_tiles.fulfilled is not None
        assert ok_tiles.fulfilled["status"] == 200
        assert seen_external == [
            (
                "GET",
                "https://tiles.openfreemap.org/styles/liberty",
                "tiles.openfreemap.org",
                "tiles.openfreemap.org",
            )
        ]

        bad_metadata = FakeRoute("http://169.254.169.254/latest/meta-data/")
        await handler(bad_metadata)  # type: ignore[arg-type]
        assert bad_metadata.aborted_reason == "blockedbyclient"

        bad_api = FakeRoute("http://api:8000/api/v1/projects")
        await handler(bad_api)  # type: ignore[arg-type]
        assert bad_api.aborted_reason == "blockedbyclient"


async def test_render_manifest_with_page_offloads_callbacks_and_finish_from_event_loop() -> None:
    import threading

    loop_thread_id = threading.get_ident()
    callback_threads: list[int] = []

    class ThreadTrackingEncoder(FakeEncoder):
        def finish(self, *, timeout_seconds: float = 300.0) -> Path:
            callback_threads.append(threading.get_ident())
            return super().finish(timeout_seconds=timeout_seconds)

    manifest = _sample_manifest(total_frames=2)
    settings = WorkerSettings(render_base_url="http://web:8080", lease_renew_interval_frames=1)
    page = FakePage(js_frame_count=2)
    encoder = ThreadTrackingEncoder()

    def _on_progress(_done: int, _total: int) -> bool:
        callback_threads.append(threading.get_ident())
        return True

    def _is_cancelled() -> bool:
        callback_threads.append(threading.get_ident())
        return False

    await render_manifest_with_page(
        page,  # type: ignore[arg-type]
        manifest,
        encoder,  # type: ignore[arg-type]
        settings,
        on_progress=_on_progress,
        is_cancelled=_is_cancelled,
        resolve_dns=False,
    )
    assert callback_threads
    assert all(tid != loop_thread_id for tid in callback_threads)


async def test_render_manifest_with_page_streams_frames_to_encoder() -> None:
    manifest = _sample_manifest(total_frames=5)
    settings = WorkerSettings(
        render_base_url="http://web:8080",
        lease_renew_interval_frames=2,
    )
    page = FakePage(js_frame_count=5)
    encoder = FakeEncoder()
    progress_calls: list[tuple[int, int]] = []

    def _on_progress(done: int, total: int) -> bool:
        progress_calls.append((done, total))
        return True

    count = await render_manifest_with_page(
        page,  # type: ignore[arg-type]
        manifest,
        encoder,  # type: ignore[arg-type]
        settings,
        on_progress=_on_progress,
        is_cancelled=lambda: False,
        resolve_dns=False,
    )

    assert count == 5
    assert page.routes[0][0] == "**/*"
    assert len(page.init_scripts) == 1
    assert "window.__TRIP_RENDER_MANIFEST__" in page.init_scripts[0]
    raw_arg = page.init_scripts[0].split("JSON.parse(", 1)[1].rsplit(");", 1)[0]
    parsed_manifest = json.loads(json.loads(raw_arg))
    assert parsed_manifest["job_id"] == manifest.job_id

    assert page.visited_urls == [f"http://web:8080/render/{manifest.job_id}"]
    assert page.frames_rendered == [0, 1, 2, 3, 4]
    assert encoder.started is True
    assert encoder.finished is True
    assert encoder.aborted is False
    assert encoder.frames == [b"FRAME_0", b"FRAME_1", b"FRAME_2", b"FRAME_3", b"FRAME_4"]
    assert progress_calls == [(2, 5), (4, 5), (5, 5)]


async def test_render_manifest_with_page_aborts_on_mid_render_cancellation() -> None:
    manifest = _sample_manifest(total_frames=6)
    settings = WorkerSettings(
        render_base_url="http://web:8080",
        lease_renew_interval_frames=2,
    )
    page = FakePage(js_frame_count=6)
    encoder = FakeEncoder()

    def _on_progress(done: int, _total: int) -> bool:
        return done < 2

    with pytest.raises(RenderCancelledError):
        await render_manifest_with_page(
            page,  # type: ignore[arg-type]
            manifest,
            encoder,  # type: ignore[arg-type]
            settings,
            on_progress=_on_progress,
            is_cancelled=lambda: False,
            resolve_dns=False,
        )

    assert encoder.aborted is True
    assert encoder.finished is False
    assert page.frames_rendered == [0, 1]


async def test_ssrf_route_handler_blocks_bypass_vectors_and_nxdomain_allows_ipv6_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    import httpx

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(
        host: Any, port: Any, *, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[Any]:
        _ = port, family, type, proto, flags
        if host == "nxdomain.openfreemap.org":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host == "rebind-v4mapped.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0))]
        if host == "2606:4700:4700::1111":
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 8443, 0, 0))
            ]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    settings = WorkerSettings(
        render_base_url="http://caddy",
        MAP_TILE_ALLOWLIST=(
            "https://[2606:4700:4700::1111]:8443/tiles, "
            "https://nxdomain.openfreemap.org, "
            "https://rebind-v4mapped.openfreemap.org"
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                headers={"content-type": "application/x-protobuf"},
                content=b"tile",
            )
        ),
        follow_redirects=False,
    ) as client:
        handler = make_ssrf_route_handler(
            render_origin=settings.render_base_url,
            allowed_external_hosts=settings.allowed_external_hosts,
            resolve_dns=True,
            http_client=client,
        )

        for blocked_url in (
            "http://127.0.0.1/render/job-1",
            "http://0.0.0.0/render/job-1",
            "http://169.254.169.254/latest/meta-data/",
            "https://100.64.0.1/tile.pbf",
            "https://[::1]/tile.pbf",
            "https://[::ffff:127.0.0.1]/tile.pbf",
            "https://[fc00::1]/tile.pbf",
            "https://[fe80::1]/tile.pbf",
            "http://caddy/api/../api/v1/projects",
            "http://caddy@127.0.0.1/render/job-1",
            "https://tiles.openfreemap.org@127.0.0.1/tile.pbf",
            "https://nxdomain.openfreemap.org/styles/liberty",
            "https://rebind-v4mapped.openfreemap.org/styles/liberty",
        ):
            route = FakeRoute(blocked_url)
            await handler(route)  # type: ignore[arg-type]
            assert route.aborted_reason == "blockedbyclient", f"Expected block for {blocked_url}"

        v6_route = FakeRoute("https://[2606:4700:4700::1111]:8443/tiles/0/0/0.pbf")
        await handler(v6_route)  # type: ignore[arg-type]
        assert v6_route.continued is False
        assert v6_route.fulfilled is not None
        assert v6_route.fulfilled["status"] == 200


async def test_render_manifest_with_page_png_cancellation_and_pageerror_handling() -> None:
    args_default = build_chromium_launch_args(WorkerSettings(chromium_no_sandbox=False))
    assert "--no-sandbox" not in args_default
    assert "--use-angle=swiftshader" in args_default
    assert "--disable-features=WebRtcHideLocalIpsWithMdns" in args_default
    assert "--enforce-webrtc-ip-permission-check" in args_default
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args_default
    args_no_sandbox = build_chromium_launch_args(WorkerSettings(chromium_no_sandbox=True))
    assert "--no-sandbox" in args_no_sandbox

    manifest = _sample_manifest(total_frames=2)
    page = FakePage(js_frame_count=2)
    encoder = FakeEncoder()
    with pytest.raises(RenderCancelledError, match="before start"):
        await render_manifest_with_page(
            page,  # type: ignore[arg-type]
            manifest,
            encoder,  # type: ignore[arg-type]
            WorkerSettings(),
            is_cancelled=lambda: True,
            resolve_dns=False,
        )
    assert encoder.started is False

    cancel_calls = 0

    def _cancel_on_second_check() -> bool:
        nonlocal cancel_calls
        cancel_calls += 1
        return cancel_calls >= 3

    encoder2 = FakeEncoder()
    page2 = FakePage(js_frame_count=4)
    with pytest.raises(RenderCancelledError, match="cancelled at frame 1/4"):
        await render_manifest_with_page(
            page2,  # type: ignore[arg-type]
            _sample_manifest(total_frames=4),
            encoder2,  # type: ignore[arg-type]
            WorkerSettings(lease_renew_interval_frames=1),
            is_cancelled=_cancel_on_second_check,
            resolve_dns=False,
        )
    assert encoder2.aborted is True

    seen_types: list[str] = []

    class _PngErrorPage(FakePage):
        async def screenshot(self, **kwargs: Any) -> bytes:
            seen_types.append(str(kwargs.get("type")))
            if len(seen_types) == 1:
                return b"PNG_0"
            self.listeners["pageerror"](RuntimeError("WebGL context lost"))
            raise RuntimeError("screenshot failed")

    encoder3 = FakeEncoder()
    page3 = _PngErrorPage(js_frame_count=2)
    with pytest.raises(RenderError, match="WebGL context lost"):
        await render_manifest_with_page(
            page3,  # type: ignore[arg-type]
            _sample_manifest(total_frames=2),
            encoder3,  # type: ignore[arg-type]
            WorkerSettings(frame_format="png"),
            resolve_dns=False,
        )
    assert seen_types == ["png", "png"]
    assert encoder3.aborted is True


async def test_render_manifest_with_page_preroll_and_hold_deduplication() -> None:
    manifest = _sample_manifest(
        total_frames=20,
        intro_hold_seconds=1.0,
        outro_hold_seconds=1.0,
    )
    settings = WorkerSettings(render_base_url="http://web:8080", lease_renew_interval_frames=5)
    page = FakePage(js_frame_count=20)
    encoder = FakeEncoder()

    written = await render_manifest_with_page(
        page,  # type: ignore[arg-type]
        manifest,
        encoder,  # type: ignore[arg-type]
        settings,
        frame_start=2,
        frame_end=19,
        preroll_frames=2,
        resolve_dns=False,
    )
    assert written == 17
    assert len(encoder.frames) == 17
    assert page.frames_rendered[:2] == [0, 1]
    assert len(page.frames_rendered) < 19


async def test_virtual_photo_route_and_ssrf_first_tile_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    import httpx

    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host: Any, _port: Any, **_kwargs: Any) -> list[Any]:
        if host == "rebind.openfreemap.org":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    cache_dir = tmp_path / "tiles"
    cache = TileCache(cache_dir, max_bytes=32)

    fetch_calls: list[str] = []

    def handler_transport(request: httpx.Request) -> httpx.Response:
        fetch_calls.append(str(request.url))
        if "redirect" in str(request.url):
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        if "big" in str(request.url):
            return httpx.Response(
                200, headers={"content-type": "application/x-protobuf"}, content=b"A" * 20
            )
        return httpx.Response(
            200, headers={"content-type": "application/json; charset=utf-8"}, content=b'{"ok":1}'
        )

    transport = httpx.MockTransport(handler_transport)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        handler = make_ssrf_route_handler(
            render_origin="http://caddy",
            allowed_external_hosts=frozenset({"tiles.openfreemap.org", "rebind.openfreemap.org"}),
            resolve_dns=True,
            photo_assets={"photo-1": b"RIFF\x10\x00\x00\x00WEBPVP8 "},
            tile_cache=cache,
            http_client=client,
        )

        photo_ok = FakeRoute("https://render-assets.local/photo-1")
        await handler(photo_ok)  # type: ignore[arg-type]
        assert photo_ok.fulfilled is not None
        assert photo_ok.fulfilled["status"] == 200
        assert photo_ok.fulfilled["content_type"] == "image/webp"

        photo_missing = FakeRoute("https://render-assets.local/unknown")
        await handler(photo_missing)  # type: ignore[arg-type]
        assert photo_missing.fulfilled is not None
        assert photo_missing.fulfilled["status"] == 404

        # SSRF runs BEFORE tile_cache: pre-seed cache for rebind host and verify block
        cache.put("https://rebind.openfreemap.org/tile.pbf", b"secret", "application/octet-stream")
        rebind_route = FakeRoute("https://rebind.openfreemap.org/tile.pbf")
        await handler(rebind_route)  # type: ignore[arg-type]
        assert rebind_route.aborted_reason == "blockedbyclient"

        tile_url = "https://tiles.openfreemap.org/styles/liberty"
        r1 = FakeRoute(tile_url)
        await handler(r1)  # type: ignore[arg-type]
        assert r1.fulfilled is not None and r1.fulfilled["status"] == 200
        assert r1.fulfilled["body"] == b'{"ok":1}'
        assert len(fetch_calls) == 1

        r2 = FakeRoute(tile_url)
        await handler(r2)  # type: ignore[arg-type]
        assert r2.fulfilled is not None and r2.fulfilled["status"] == 200
        assert r2.fulfilled["body"] == b'{"ok":1}'
        assert len(fetch_calls) == 1

        # Redirect response is aborted and never cached
        redir = FakeRoute("https://tiles.openfreemap.org/redirect")
        await handler(redir)  # type: ignore[arg-type]
        assert redir.aborted_reason == "blockedbyclient"

        for idx in range(3):
            big_route = FakeRoute(f"https://tiles.openfreemap.org/big/{idx}.pbf")
            await handler(big_route)  # type: ignore[arg-type]
        total_bin_bytes = sum(p.stat().st_size for p in cache_dir.rglob("*.bin"))
        assert total_bin_bytes <= 32


def test_warm_playwright_browser_reuses_browser_isolates_context_and_recycles_after_max_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trip_worker import renderer as renderer_mod
    from trip_worker.renderer import WarmPlaywrightHost, render_manifest_to_mp4

    launch_records: list[dict[str, Any]] = []
    context_records: list[dict[str, Any]] = []
    pw_stopped = {"count": 0}

    class FakeWarmContext:
        def __init__(self, browser_idx: int, ctx_idx: int) -> None:
            self.browser_idx = browser_idx
            self.ctx_idx = ctx_idx
            self.closed = False

        async def new_page(self) -> Any:
            return type(
                "FakeWarmPage",
                (),
                {"set_default_timeout": lambda *_a: None},
            )()

        async def close(self) -> None:
            self.closed = True

    class FakeWarmBrowser:
        def __init__(self, browser_idx: int) -> None:
            self.browser_idx = browser_idx
            self.connected = True
            self.closed = False
            self.contexts_created = 0

        def is_connected(self) -> bool:
            return self.connected and not self.closed

        async def new_context(self, **kwargs: Any) -> FakeWarmContext:
            self.contexts_created += 1
            ctx = FakeWarmContext(self.browser_idx, self.contexts_created)
            context_records.append(
                {
                    "browser_idx": self.browser_idx,
                    "ctx": ctx,
                    "viewport": kwargs.get("viewport"),
                }
            )
            return ctx

        async def close(self) -> None:
            self.closed = True
            self.connected = False

    class FakeChromium:
        def __init__(self) -> None:
            self.browsers: list[FakeWarmBrowser] = []

        async def launch(self, **kwargs: Any) -> FakeWarmBrowser:
            b = FakeWarmBrowser(len(self.browsers) + 1)
            self.browsers.append(b)
            launch_records.append({"browser_idx": b.browser_idx, "kwargs": kwargs, "browser": b})
            return b

    fake_chromium = FakeChromium()

    class FakePlaywright:
        chromium = fake_chromium

        async def stop(self) -> None:
            pw_stopped["count"] += 1

    class FakePlaywrightStarter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    monkeypatch.setattr(renderer_mod, "async_playwright", lambda: FakePlaywrightStarter())

    async def fake_render_with_page(
        _page: Any,
        manifest: RenderManifest,
        _encoder: Any,
        _settings: WorkerSettings,
        **_kwargs: Any,
    ) -> int:
        return manifest.total_frames

    monkeypatch.setattr(renderer_mod, "render_manifest_with_page", fake_render_with_page)

    settings = WorkerSettings(
        render_base_url="http://web:8080",
        worker_browser_max_renders=2,
    )
    manifest = _sample_manifest(4)

    with WarmPlaywrightHost(settings) as host:
        for idx in range(3):
            out_mp4 = tmp_path / f"out_{idx}.mp4"
            browser = host.run_coro(host.acquire_browser())
            host.run_coro(render_manifest_to_mp4(manifest, out_mp4, settings, browser=browser))

        assert len(launch_records) == 2
        assert launch_records[0]["browser"].closed is True
        assert launch_records[1]["browser"].closed is False
        assert len(context_records) == 3
        assert [c["browser_idx"] for c in context_records] == [1, 1, 2]
        assert all(c["ctx"].closed for c in context_records)
        assert host.render_count == 1

    assert launch_records[1]["browser"].closed is True
    assert pw_stopped["count"] == 1


def test_warm_playwright_browser_crash_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from trip_worker import renderer as renderer_mod
    from trip_worker import runner as runner_mod
    from trip_worker.renderer import WarmPlaywrightHost

    launched_browsers: list[Any] = []

    class FakeCrashBrowser:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.connected = True
            self.closed = False

        def is_connected(self) -> bool:
            return self.connected and not self.closed

        async def close(self) -> None:
            self.closed = True
            self.connected = False

    class FakeChromium:
        async def launch(self, **_kwargs: Any) -> FakeCrashBrowser:
            b = FakeCrashBrowser(len(launched_browsers) + 1)
            launched_browsers.append(b)
            return b

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakePlaywrightStarter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    monkeypatch.setattr(renderer_mod, "async_playwright", lambda: FakePlaywrightStarter())

    settings = WorkerSettings(render_base_url="http://web:8080", worker_browser_max_renders=50)
    manifest = _sample_manifest(4)
    attempt = {"n": 0}

    async def flaky_render(
        _manifest: RenderManifest,
        out_path: Path,
        _settings: WorkerSettings,
        *,
        browser: Any = None,
        **_kwargs: Any,
    ) -> Path:
        attempt["n"] += 1
        if attempt["n"] == 1:
            browser.connected = False
            raise RuntimeError("Target closed")
        await asyncio.to_thread(out_path.write_bytes, b"ok")
        return out_path

    with WarmPlaywrightHost(settings) as host:
        with pytest.raises(RuntimeError, match="Target closed"):
            runner_mod._execute_render(
                flaky_render,
                manifest,
                tmp_path / "fail.mp4",
                settings,
                browser_host=host,
            )
        assert launched_browsers[0].closed is True

        res = runner_mod._execute_render(
            flaky_render,
            manifest,
            tmp_path / "ok.mp4",
            settings,
            browser_host=host,
        )
        assert res == tmp_path / "ok.mp4"
        assert len(launched_browsers) == 2
        assert launched_browsers[1].is_connected() is True


async def test_ssrf_route_handler_external_without_tile_cache_blocks_redirects_and_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import socket

    import httpx

    loop = asyncio.get_running_loop()
    dns_round = {"n": 0}

    async def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
        dns_round["n"] += 1
        if dns_round["n"] == 3:
            # 3rd request simulates DNS rebinding to link-local metadata IP
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    seen_requests: list[tuple[str, str]] = []

    def transport_fn(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, str(request.url)))
        if "redir" in str(request.url):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"v":1}')

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport_fn),
        follow_redirects=False,
    ) as client:
        handler = make_ssrf_route_handler(
            render_origin="http://caddy",
            allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
            resolve_dns=True,
            tile_cache=None,
            http_client=client,
        )

        get_route = FakeRoute("https://tiles.openfreemap.org/styles/liberty", method="GET")
        await handler(get_route)  # type: ignore[arg-type]
        assert get_route.continued is False
        assert get_route.fulfilled is not None
        assert get_route.fulfilled["status"] == 200
        assert get_route.fulfilled["body"] == b'{"v":1}'

        head_route = FakeRoute("https://tiles.openfreemap.org/styles/liberty", method="HEAD")
        await handler(head_route)  # type: ignore[arg-type]
        assert head_route.continued is False
        assert head_route.fulfilled is not None
        assert head_route.fulfilled["status"] == 200
        assert head_route.fulfilled["body"] == b""

        rebind_route = FakeRoute("https://tiles.openfreemap.org/rebinding.pbf", method="GET")
        await handler(rebind_route)  # type: ignore[arg-type]
        assert rebind_route.aborted_reason == "blockedbyclient"
        assert rebind_route.continued is False

        redir_route = FakeRoute("https://tiles.openfreemap.org/redir", method="GET")
        await handler(redir_route)  # type: ignore[arg-type]
        assert redir_route.aborted_reason == "blockedbyclient"
        assert redir_route.continued is False

        post_route = FakeRoute("https://tiles.openfreemap.org/mutate", method="POST")
        await handler(post_route)  # type: ignore[arg-type]
        assert post_route.aborted_reason == "blockedbyclient"
        assert post_route.continued is False

    assert seen_requests == [
        ("GET", "https://tiles.openfreemap.org/styles/liberty"),
        ("HEAD", "https://tiles.openfreemap.org/styles/liberty"),
        ("GET", "https://tiles.openfreemap.org/redir"),
    ]


async def test_ssrf_route_handler_aborts_when_http_client_already_closed() -> None:
    import httpx

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"{}")),
        follow_redirects=False,
    )
    await client.aclose()
    handler = make_ssrf_route_handler(
        render_origin="http://caddy",
        allowed_external_hosts=frozenset({"tiles.openfreemap.org"}),
        resolve_dns=False,
        tile_cache=None,
        http_client=client,
    )

    route = FakeRoute("https://tiles.openfreemap.org/styles/liberty", method="GET")
    await handler(route)  # type: ignore[arg-type]

    assert route.aborted_reason == "failed"
    assert route.continued is False
    assert route.fulfilled is None
