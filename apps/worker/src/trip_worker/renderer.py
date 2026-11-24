"""Headless Playwright Chromium + MapLibre/Deck.gl deterministic frame capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser, Page, Playwright, Route, async_playwright

from trip_worker.config import WorkerSettings
from trip_worker.encoder import StreamingVideoEncoder
from trip_worker.logging_setup import elapsed_ms
from trip_worker.manifest import RenderManifest, compute_frame_render_spans
from trip_worker.ssrf import (
    ResolvedExternalTarget,
    build_external_request_kwargs,
    build_pinned_external_http_client,
    pin_external_target,
    resolve_and_validate_external_target_async,
)

logger = logging.getLogger(__name__)

# 300 frames is 10 s at 30 fps and equals the default chunk size, so a chunked render
# logs roughly one progress line per chunk.
PROGRESS_LOG_EVERY_FRAMES = 300


class RenderCancelledError(RuntimeError):
    """Raised when a render job is cancelled before or during frame capture."""


class RenderError(RuntimeError):
    """Raised when headless Chromium rendering fails."""


@runtime_checkable
class AsyncRouteHandler(Protocol):
    async def __call__(self, route: Route) -> None: ...


def _detect_image_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/webp"


class TileCache:
    """Bounded disk cache (`{sha256[:2]}/{sha256}.bin` + `.meta.json`) for HTTPS tiles."""

    def __init__(self, cache_dir: Path, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.cache_dir = cache_dir.resolve()
        self.max_bytes = max(0, int(max_bytes))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _entry_paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        subdir = self.cache_dir / digest[:2]
        return subdir / f"{digest}.bin", subdir / f"{digest}.meta.json"

    def get(self, url: str) -> tuple[bytes, str] | None:
        if self.max_bytes <= 0:
            return None
        bin_path, meta_path = self._entry_paths(url)
        with self._lock:
            if not bin_path.is_file() or not meta_path.is_file():
                return None
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict) or meta.get("url") != url:
                    return None
                data = bin_path.read_bytes()
                os.utime(bin_path, None)
                content_type = str(meta.get("content_type") or "application/octet-stream")
                return data, content_type
            except (OSError, json.JSONDecodeError, ValueError):
                return None

    def put(self, url: str, data: bytes, content_type: str) -> None:
        if self.max_bytes <= 0 or len(data) > self.max_bytes:
            return
        bin_path, meta_path = self._entry_paths(url)
        with self._lock:
            bin_path.parent.mkdir(parents=True, exist_ok=True)
            tid = threading.get_ident()
            tmp_bin = bin_path.with_name(f".{bin_path.name}.{tid}.tmp")
            tmp_meta = meta_path.with_name(f".{meta_path.name}.{tid}.tmp")
            meta_payload = json.dumps(
                {
                    "url": url,
                    "content_type": content_type,
                    "size": len(data),
                    "cached_at": time.time(),
                },
                separators=(",", ":"),
            )
            try:
                tmp_bin.write_bytes(data)
                os.replace(tmp_bin, bin_path)
                tmp_meta.write_text(meta_payload, encoding="utf-8")
                os.replace(tmp_meta, meta_path)
            except OSError:
                tmp_bin.unlink(missing_ok=True)
                tmp_meta.unlink(missing_ok=True)
                return
            self._evict_if_needed_locked()

    def _evict_if_needed_locked(self) -> None:
        entries: list[tuple[int, int, Path]] = []
        total_bytes = 0
        for bin_file in self.cache_dir.rglob("*.bin"):
            try:
                st = bin_file.stat()
            except OSError:
                continue
            total_bytes += st.st_size
            entries.append((st.st_mtime_ns, st.st_size, bin_file))

        if total_bytes <= self.max_bytes:
            return

        entries.sort(key=lambda item: (item[0], str(item[2])))
        for _mtime, size, bin_file in entries:
            if total_bytes <= self.max_bytes:
                break
            bin_file.unlink(missing_ok=True)
            bin_file.with_suffix(".meta.json").unlink(missing_ok=True)
            total_bytes -= size


def build_chromium_launch_args(settings: WorkerSettings) -> list[str]:
    """Return headless Chromium launch arguments for WebGL MapLibre/Deck.gl rendering."""
    args = [
        "--disable-dev-shm-usage",
        "--enable-webgl",
        "--ignore-gpu-blocklist",
        "--use-gl=angle",
        "--use-angle=vulkan" if settings.worker_gpu_enabled else "--use-angle=swiftshader",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        "--enforce-webrtc-ip-permission-check",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if settings.worker_gpu_enabled:
        args.append("--disable-software-rasterizer")
    if settings.chromium_no_sandbox:
        args.append("--no-sandbox")
    return args


async def _launch_chromium(playwright: Playwright, settings: WorkerSettings) -> Browser:
    browser = await playwright.chromium.launch(
        headless=True,
        args=build_chromium_launch_args(settings),
    )
    if not settings.worker_gpu_enabled:
        return browser

    try:
        page = await browser.new_page()
        try:
            renderer: str = await page.evaluate("""() => {
                const gl = document.createElement('canvas').getContext('webgl2');
                const info = gl?.getExtension('WEBGL_debug_renderer_info');
                return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : '';
            }""")
        finally:
            await page.close()
        backend = renderer.lower()
        if "vulkan" not in backend or any(
            name in backend for name in ("swiftshader", "llvmpipe", "lavapipe", "software")
        ):
            raise RenderError(
                "WORKER_GPU_ENABLED requires hardware Vulkan WebGL2; "
                f"Chromium reported renderer={renderer or 'unavailable'!r}. "
                "Check the render device, permissions and Vulkan driver."
            )
        logger.info("Worker GPU renderer: %s", renderer)
    except BaseException:
        await browser.close()
        raise
    return browser


def build_manifest_init_script(manifest: RenderManifest) -> str:
    """Return JS init script safely assigning `window.__TRIP_RENDER_MANIFEST__`."""
    raw_json = json.dumps(manifest.model_dump(mode="json"), separators=(",", ":"))
    escaped_json_string = json.dumps(raw_json)
    return f"window.__TRIP_RENDER_MANIFEST__ = JSON.parse({escaped_json_string});"


def make_ssrf_route_handler(
    *,
    render_origin: str,
    allowed_external_hosts: frozenset[str],
    resolve_dns: bool = True,
    photo_assets: Mapping[str, bytes] | None = None,
    tile_cache: TileCache | None = None,
    http_client: httpx.AsyncClient | None = None,
    fetch_failures: list[str] | None = None,
) -> AsyncRouteHandler:
    """Create a Playwright `page.route()` handler enforcing SSRF, virtual photos, and tile cache.

    When `fetch_failures` is given, the exception type name of each external fetch that
    raised is appended to it, so the caller can fail a render that lost map data.
    """
    owned_client: httpx.AsyncClient | None = None

    async def _fetch_external(
        url: str,
        *,
        method: str,
        target: ResolvedExternalTarget,
    ) -> httpx.Response:
        nonlocal owned_client
        client = http_client
        if client is None:
            if owned_client is None:
                owned_client = build_pinned_external_http_client(resolve_dns=resolve_dns)
            client = owned_client
        req_kwargs = build_external_request_kwargs(target)
        with pin_external_target(target):
            return await client.request(method, url, follow_redirects=False, **req_kwargs)

    async def _handle_route(route: Route) -> None:
        url = route.request.url
        parsed = urlparse(url)
        method = str(getattr(route.request, "method", "GET")).upper()

        if (
            photo_assets is not None
            and method == "GET"
            and parsed.scheme.lower() == "https"
            and (parsed.hostname or "").lower() == "render-assets.local"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
        ):
            asset_id = parsed.path.lstrip("/")
            asset_bytes = photo_assets.get(asset_id)
            if asset_bytes is not None:
                await route.fulfill(
                    status=200,
                    content_type=_detect_image_content_type(asset_bytes),
                    body=asset_bytes,
                )
            else:
                await route.fulfill(
                    status=404,
                    content_type="text/plain",
                    body=b"Photo asset not found",
                )
            return

        # Always evaluate SSRF policy and pin DNS BEFORE consulting tile_cache or fetching
        decision = await resolve_and_validate_external_target_async(
            url,
            render_origin=render_origin,
            allowed_external_hosts=allowed_external_hosts,
            resolve_dns=resolve_dns,
        )
        if decision.kind == "blocked":
            logger.warning(
                "Blocked disallowed Playwright request scheme=%s host=%s",
                parsed.scheme,
                parsed.hostname,
            )
            await route.abort("blockedbyclient")
            return

        if decision.kind in {"data_or_blob", "render_origin"}:
            await route.continue_()
            return

        # External HTTPS requests must NEVER call route.continue_() (even when tile_cache is None)
        if method not in {"GET", "HEAD"} or decision.target is None:
            logger.warning(
                "Blocked disallowed external HTTP method=%s host=%s",
                method,
                parsed.hostname,
            )
            await route.abort("blockedbyclient")
            return

        if tile_cache is not None and method == "GET":
            cached = await asyncio.to_thread(tile_cache.get, url)
            if cached is not None:
                body, content_type = cached
                await route.fulfill(status=200, content_type=content_type, body=body)
                return

        try:
            resp = await _fetch_external(url, method=method, target=decision.target)
        except Exception as exc:
            # Every route must end in fulfill or abort, so any failure of the external
            # fetch aborts here instead of propagating out of the callback.
            logger.warning(
                "External Playwright request failed error_type=%s host=%s",
                type(exc).__name__,
                parsed.hostname,
            )
            if fetch_failures is not None:
                fetch_failures.append(type(exc).__name__)
            try:
                await route.abort("failed")
            except Exception:
                logger.debug("Ignoring error aborting Playwright route", exc_info=True)
            return

        if 300 <= resp.status_code < 400:
            logger.warning(
                "Blocked redirect response status=%d for host=%s",
                resp.status_code,
                parsed.hostname,
            )
            await route.abort("blockedbyclient")
            return

        raw_ct = resp.headers.get("content-type", "application/octet-stream")
        content_type = raw_ct.split(";")[0].strip() or "application/octet-stream"
        if tile_cache is not None and method == "GET" and resp.status_code == 200:
            await asyncio.to_thread(tile_cache.put, url, resp.content, content_type)
        await route.fulfill(
            status=resp.status_code,
            content_type=content_type,
            body=b"" if method == "HEAD" else resp.content,
        )

    return _handle_route


class _TrackedRouteHandler:
    """Run each route callback as a task that the render scope can wait for."""

    def __init__(self, handler: AsyncRouteHandler) -> None:
        self._handler = handler
        self._tasks: set[asyncio.Task[None]] = set()

    async def __call__(self, route: Route) -> None:
        task = asyncio.ensure_future(self._handler(route))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await task

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


@asynccontextmanager
async def _external_route_scope(
    page: Page,
    route_handler: AsyncRouteHandler,
) -> AsyncIterator[AsyncRouteHandler]:
    """Yield a tracked route handler, then unroute it and wait for in-flight calls on exit.

    The pinned HTTP client used by the handler must be closed only after this scope exits.
    """
    tracked = _TrackedRouteHandler(route_handler)
    try:
        yield tracked
    finally:
        try:
            await page.unroute("**/*", tracked)
        except Exception:
            logger.debug("Ignoring error removing Playwright route", exc_info=True)
        await tracked.drain()


async def _open_render_page(
    page: Page,
    manifest: RenderManifest,
    settings: WorkerSettings,
    route_handler: AsyncRouteHandler,
) -> int:
    """Load the render harness and return the frame count reported by the browser."""
    await page.route("**/*", route_handler)
    await page.add_init_script(build_manifest_init_script(manifest))

    target_url = f"{settings.render_base_url.rstrip('/')}/render/{manifest.job_id}"
    await page.goto(
        target_url,
        wait_until="domcontentloaded",
        timeout=settings.render_ready_timeout_ms,
    )

    await page.wait_for_function(
        "() => Boolean(window.__TRIP_RENDERER__ && window.__TRIP_RENDERER__.ready)",
        timeout=settings.render_ready_timeout_ms,
    )

    js_frame_count = await page.evaluate(
        """async () => {
            const renderer = window.__TRIP_RENDERER__;
            if (!renderer) {
                throw new Error("window.__TRIP_RENDERER__ missing");
            }
            await renderer.ready;
            return typeof renderer.getFrameCount === "function"
                ? renderer.getFrameCount()
                : null;
        }"""
    )
    if isinstance(js_frame_count, (int, float)) and int(js_frame_count) > 0:
        return int(js_frame_count)
    return manifest.total_frames


async def probe_manifest_frame_count(
    manifest: RenderManifest,
    settings: WorkerSettings,
    *,
    browser: Browser | None = None,
    resolve_dns: bool = True,
) -> int:
    """Return the total frame count the browser schedule reports, without capturing frames.

    Cinematic photo stops add time that only the shared web schedule can compute, so the
    worker asks the same code that renders the frames instead of re-implementing it.
    """

    async def _probe(target: Browser) -> int:
        context = await target.new_context(
            viewport={"width": manifest.width, "height": manifest.height},
            device_scale_factor=1,
        )
        try:
            page = await context.new_page()
            page.set_default_timeout(settings.render_frame_timeout_ms)
            async with (
                build_pinned_external_http_client(resolve_dns=resolve_dns) as http_client,
                _external_route_scope(
                    page,
                    make_ssrf_route_handler(
                        render_origin=settings.render_base_url,
                        allowed_external_hosts=settings.allowed_external_hosts,
                        resolve_dns=resolve_dns,
                        photo_assets={},
                        http_client=http_client,
                    ),
                ) as route_handler,
            ):
                return await _open_render_page(page, manifest, settings, route_handler)
        finally:
            await context.close()

    if browser is not None:
        return await _probe(browser)
    async with async_playwright() as pw:
        launched = await _launch_chromium(pw, settings)
        try:
            return await _probe(launched)
        finally:
            await launched.close()


async def render_manifest_with_page(
    page: Page,
    manifest: RenderManifest,
    encoder: StreamingVideoEncoder,
    settings: WorkerSettings,
    *,
    on_progress: Callable[[int, int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    resolve_dns: bool = True,
    frame_start: int = 0,
    frame_end: int | None = None,
    preroll_frames: int = 0,
    photo_assets: Mapping[str, bytes] | None = None,
    tile_cache: TileCache | None = None,
) -> int:
    """Drive `/render/{job_id}` on an open Playwright Page and pipe frames into FFmpeg."""
    if is_cancelled is not None and await asyncio.to_thread(is_cancelled):
        raise RenderCancelledError(f"Render job {manifest.job_id} was cancelled before start")

    resolved_cache = tile_cache
    if resolved_cache is None and settings.resolved_tile_cache_dir is not None:
        resolved_cache = TileCache(
            settings.resolved_tile_cache_dir,
            max_bytes=settings.tile_cache_max_bytes,
        )

    page_errors: list[str] = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    external_fetch_failures: list[str] = []
    async with (
        build_pinned_external_http_client(resolve_dns=resolve_dns) as http_client,
        _external_route_scope(
            page,
            make_ssrf_route_handler(
                render_origin=settings.render_base_url,
                allowed_external_hosts=settings.allowed_external_hosts,
                resolve_dns=resolve_dns,
                photo_assets=photo_assets,
                tile_cache=resolved_cache,
                http_client=http_client,
                fetch_failures=external_fetch_failures,
            ),
        ) as route_handler,
    ):
        total_frames = await _open_render_page(page, manifest, settings, route_handler)

        eff_start = max(0, frame_start)
        eff_end = total_frames if frame_end is None else min(total_frames, frame_end)
        if eff_start >= eff_end:
            raise ValueError(
                f"Invalid frame span [{eff_start}, {eff_end}) for total_frames={total_frames}"
            )

        spans = compute_frame_render_spans(
            manifest,
            frame_start=eff_start,
            frame_end=eff_end,
            total_frames_override=total_frames,
        )
        chunk_frame_count = eff_end - eff_start

        capture_started = time.monotonic()
        encoder.start()
        frames_written = 0
        try:
            # Silent preroll settle before capturing [eff_start, eff_end)
            preroll_start = max(0, eff_start - max(0, preroll_frames))
            for pre_idx in range(preroll_start, eff_start):
                if pre_idx == preroll_start or pre_idx % settings.lease_renew_interval_frames == 0:
                    if is_cancelled is not None and await asyncio.to_thread(is_cancelled):
                        encoder.abort()
                        raise RenderCancelledError(
                            f"Render job {manifest.job_id} cancelled during preroll at {pre_idx}"
                        )
                await page.evaluate(
                    "async (idx) => { await window.__TRIP_RENDERER__.setFrame(idx); }",
                    pre_idx,
                )

            last_cancel_check_at = -settings.lease_renew_interval_frames
            last_progress_at = 0
            last_logged_at = 0
            for span in spans:
                if (
                    frames_written == 0
                    or (frames_written - last_cancel_check_at)
                    >= settings.lease_renew_interval_frames
                ):
                    last_cancel_check_at = frames_written
                    if is_cancelled is not None and await asyncio.to_thread(is_cancelled):
                        encoder.abort()
                        raise RenderCancelledError(
                            f"Render job {manifest.job_id} cancelled at frame "
                            f"{span.frame_idx}/{total_frames}"
                        )

                await page.evaluate(
                    "async (idx) => { await window.__TRIP_RENDERER__.setFrame(idx); }",
                    span.frame_idx,
                )

                if settings.frame_format == "jpeg":
                    frame_bytes = await page.screenshot(
                        type="jpeg",
                        quality=92,
                        full_page=False,
                        animations="disabled",
                    )
                else:
                    frame_bytes = await page.screenshot(
                        type="png",
                        full_page=False,
                        animations="disabled",
                    )

                for _ in range(span.repeat_count):
                    encoder.write_frame(frame_bytes)
                    frames_written += 1

                if frames_written - last_logged_at >= PROGRESS_LOG_EVERY_FRAMES:
                    last_logged_at = frames_written
                    logger.info(
                        "Render progress: %d/%d frames",
                        frames_written,
                        chunk_frame_count,
                        extra={
                            "event": "render_progress",
                            "job_id": str(manifest.job_id),
                            "stage": "frame_capture",
                        },
                    )

                is_last = frames_written >= chunk_frame_count
                if on_progress is not None and (
                    is_last
                    or (frames_written - last_progress_at) >= settings.lease_renew_interval_frames
                ):
                    last_progress_at = frames_written
                    still_running = await asyncio.to_thread(
                        on_progress, frames_written, chunk_frame_count
                    )
                    if not still_running:
                        encoder.abort()
                        raise RenderCancelledError(
                            f"Render job {manifest.job_id} cancelled during frame {frames_written}"
                        )

            logger.info(
                "Frame capture finished: %d frames",
                frames_written,
                extra={
                    "event": "frame_capture_finish",
                    "job_id": str(manifest.job_id),
                    "stage": "frame_capture",
                    "duration_ms": elapsed_ms(capture_started),
                },
            )
            encoder_started = time.monotonic()
            await asyncio.to_thread(encoder.finish, timeout_seconds=settings.ffmpeg_timeout_seconds)
            logger.info(
                "Encoder finished",
                extra={
                    "event": "encoder_finish",
                    "job_id": str(manifest.job_id),
                    "stage": "encode",
                    "duration_ms": elapsed_ms(encoder_started),
                },
            )
        except RenderCancelledError:
            encoder.abort()
            raise
        except Exception as exc:
            encoder.abort()
            err_suffix = f" (pageerror: {page_errors[0]})" if page_errors else ""
            raise RenderError(f"Frame capture failed: {exc}{err_suffix}") from exc

    if external_fetch_failures:
        failed_types = ", ".join(sorted(set(external_fetch_failures)))
        raise RenderError(
            f"External map request failed {len(external_fetch_failures)} time(s) "
            f"during render: {failed_types}"
        )

    return frames_written


class WarmPlaywrightHost:
    """Thread-affine persistent asyncio event loop and warm Chromium browser host."""

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._render_count: int = 0

    def __enter__(self) -> WarmPlaywrightHost:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def render_count(self) -> int:
        """Number of renders dispatched to the active Chromium instance."""
        return self._render_count

    def run_coro[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Execute an async coroutine on the worker thread's persistent event loop."""
        return self._loop.run_until_complete(coro)

    async def acquire_browser(self) -> Browser:
        """Return a connected Chromium Browser, launching or recycling when needed."""
        if self._browser is not None:
            needs_recycle = (
                not self._browser.is_connected()
                or self._render_count >= self._settings.worker_browser_max_renders
            )
            if needs_recycle:
                await self.reset_browser()

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        if self._browser is None:
            self._browser = await _launch_chromium(self._playwright, self._settings)
            self._render_count = 0

        self._render_count += 1
        return self._browser

    async def reset_browser(self) -> None:
        """Close the active Browser instance so the next render launches a fresh one."""
        browser = self._browser
        self._browser = None
        self._render_count = 0
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.debug("Ignoring error closing recycled/crashed Chromium", exc_info=True)

    async def aclose(self) -> None:
        """Shut down the active Chromium Browser and Playwright driver."""
        await self.reset_browser()
        pw = self._playwright
        self._playwright = None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                logger.debug("Ignoring error stopping Playwright", exc_info=True)

    def close(self) -> None:
        """Synchronously close browser resources and the owned event loop."""
        if self._loop.is_closed():
            return
        try:
            self._loop.run_until_complete(self.aclose())
        finally:
            self._loop.close()


async def _render_with_browser(
    browser: Browser,
    manifest: RenderManifest,
    encoder: StreamingVideoEncoder,
    settings: WorkerSettings,
    *,
    on_progress: Callable[[int, int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    frame_start: int = 0,
    frame_end: int | None = None,
    preroll_frames: int = 0,
    photo_assets: Mapping[str, bytes] | None = None,
    tile_cache: TileCache | None = None,
) -> None:
    context = await browser.new_context(
        viewport={"width": manifest.width, "height": manifest.height},
        device_scale_factor=1,
    )
    try:
        page = await context.new_page()
        page.set_default_timeout(settings.render_frame_timeout_ms)
        await render_manifest_with_page(
            page,
            manifest,
            encoder,
            settings,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            frame_start=frame_start,
            frame_end=frame_end,
            preroll_frames=preroll_frames,
            photo_assets=photo_assets,
            tile_cache=tile_cache,
        )
    finally:
        await context.close()


async def render_manifest_to_mp4(
    manifest: RenderManifest,
    output_mp4_path: Path,
    settings: WorkerSettings,
    *,
    on_progress: Callable[[int, int], bool] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    frame_start: int = 0,
    frame_end: int | None = None,
    preroll_frames: int = 0,
    photo_assets: Mapping[str, bytes] | None = None,
    tile_cache: TileCache | None = None,
    gop_aligned: bool = False,
    browser: Browser | None = None,
) -> Path:
    """Launch or reuse headless Chromium via Playwright, capture frames, and encode MP4."""
    if is_cancelled is not None and is_cancelled():
        raise RenderCancelledError(f"Render job {manifest.job_id} was cancelled before launch")

    use_gop = gop_aligned or frame_start > 0 or frame_end is not None
    encoder = StreamingVideoEncoder(
        output_mp4_path=output_mp4_path,
        fps=manifest.fps,
        width=manifest.width,
        height=manifest.height,
        frame_format=settings.frame_format,
        gop_aligned=use_gop,
    )

    if browser is not None:
        await _render_with_browser(
            browser,
            manifest,
            encoder,
            settings,
            on_progress=on_progress,
            is_cancelled=is_cancelled,
            frame_start=frame_start,
            frame_end=frame_end,
            preroll_frames=preroll_frames,
            photo_assets=photo_assets,
            tile_cache=tile_cache,
        )
        return output_mp4_path

    async with async_playwright() as pw:
        launched = await _launch_chromium(pw, settings)
        try:
            await _render_with_browser(
                launched,
                manifest,
                encoder,
                settings,
                on_progress=on_progress,
                is_cancelled=is_cancelled,
                frame_start=frame_start,
                frame_end=frame_end,
                preroll_frames=preroll_frames,
                photo_assets=photo_assets,
                tile_cache=tile_cache,
            )
        finally:
            await launched.close()

    return output_mp4_path
