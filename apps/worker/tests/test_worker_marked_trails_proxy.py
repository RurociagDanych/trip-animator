"""Range-aware forwarding of Marked Trails PMTiles requests through the pinned proxy."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from trip_worker import TileCache, make_ssrf_route_handler

PMTILES_URL = "https://trails.example.test/builds/2026-11-27/marked-trails.pmtiles"
EXPOSE = "Content-Range, ETag, Accept-Ranges"


class RangeRoute:
    def __init__(
        self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None
    ) -> None:
        self._headers = headers or {}
        self.request = type("Req", (), {"url": url, "method": method, "all_headers": self._all})()
        self.continued = False
        self.aborted_reason: str | None = None
        self.fulfilled: dict[str, Any] | None = None

    async def _all(self) -> dict[str, str]:
        return self._headers

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, error_code: str = "failed") -> None:
        self.aborted_reason = error_code

    async def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled = kwargs


@pytest_asyncio.fixture(autouse=True)
async def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def fake_getaddrinfo(_host: Any, _port: Any, **_kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)


def _handler(upstream: Any, client: httpx.AsyncClient, tile_cache: TileCache | None = None) -> Any:
    return make_ssrf_route_handler(
        render_origin="http://localhost:5173",
        allowed_external_hosts=frozenset({"trails.example.test"}),
        resolve_dns=True,
        tile_cache=tile_cache,
        http_client=client,
    )


def _fulfilled_headers(route: RangeRoute) -> dict[str, str]:
    assert route.fulfilled is not None
    return {k.lower(): v for k, v in route.fulfilled["headers"].items()}


async def test_range_request_is_forwarded_and_the_206_reaches_the_page_with_its_headers() -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            206,
            content=b"0123456789",
            headers={
                "content-type": "application/octet-stream",
                "content-range": "bytes 0-9/4096",
                "accept-ranges": "bytes",
                "etag": '"abc"',
                "cache-control": "public, max-age=31536000, immutable",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = RangeRoute(
            PMTILES_URL,
            headers={
                "range": "bytes=0-9",
                "if-range": '"abc"',
                "if-match": '"abc"',
                "if-none-match": '"zzz"',
                "cookie": "session=secret",
                "authorization": "Bearer secret",
                "origin": "http://localhost:5173",
            },
        )
        await _handler(upstream, client)(route)  # type: ignore[arg-type]

    sent = {k.lower(): v for k, v in seen[0].headers.items()}
    assert sent["range"] == "bytes=0-9"
    assert sent["if-range"] == '"abc"'
    assert sent["if-match"] == '"abc"'
    assert sent["if-none-match"] == '"zzz"'
    assert sent["host"] == "trails.example.test"
    assert "cookie" not in sent and "authorization" not in sent and "origin" not in sent

    assert route.continued is False
    assert route.fulfilled is not None
    assert route.fulfilled["status"] == 206
    assert route.fulfilled["body"] == b"0123456789"
    headers = _fulfilled_headers(route)
    assert headers["content-range"] == "bytes 0-9/4096"
    assert headers["content-length"] == "10"
    assert headers["accept-ranges"] == "bytes"
    assert headers["etag"] == '"abc"'
    assert headers["cache-control"] == "public, max-age=31536000, immutable"
    assert headers["access-control-expose-headers"] == EXPOSE


async def test_416_is_passed_through_with_its_content_range() -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(416, headers={"content-range": "bytes */4096"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = RangeRoute(PMTILES_URL, headers={"range": "bytes=9999-10000"})
        await _handler(upstream, client)(route)  # type: ignore[arg-type]

    assert route.fulfilled is not None and route.fulfilled["status"] == 416
    assert _fulfilled_headers(route)["content-range"] == "bytes */4096"


async def test_304_is_passed_through_instead_of_blocked_as_a_redirect() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"abc"'
        return httpx.Response(304, headers={"etag": '"abc"'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = RangeRoute(PMTILES_URL, headers={"if-none-match": '"abc"'})
        await _handler(upstream, client)(route)  # type: ignore[arg-type]

    assert route.aborted_reason is None
    assert route.fulfilled is not None and route.fulfilled["status"] == 304
    assert "content-length" not in _fulfilled_headers(route)
    assert _fulfilled_headers(route)["etag"] == '"abc"'


async def test_head_request_reports_the_upstream_length_without_a_body() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(
            200, headers={"content-length": "4096", "accept-ranges": "bytes", "etag": '"abc"'}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = RangeRoute(PMTILES_URL, method="HEAD")
        await _handler(upstream, client)(route)  # type: ignore[arg-type]

    assert route.fulfilled is not None
    assert route.fulfilled["body"] == b""
    assert _fulfilled_headers(route)["content-length"] == "4096"


async def test_range_requests_bypass_the_tile_cache_in_both_directions(tmp_path: Path) -> None:
    calls: list[str | None] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range"))
        if "range" in request.headers:
            return httpx.Response(206, content=b"PART", headers={"content-range": "bytes 0-3/8"})
        return httpx.Response(200, content=b"WHOLEFIL")

    cache = TileCache(tmp_path / "tiles")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        handler = _handler(upstream, client, cache)

        whole = RangeRoute(PMTILES_URL)
        await handler(whole)  # type: ignore[arg-type]
        ranged = RangeRoute(PMTILES_URL, headers={"range": "bytes=0-3"})
        await handler(ranged)  # type: ignore[arg-type]
        whole_again = RangeRoute(PMTILES_URL)
        await handler(whole_again)  # type: ignore[arg-type]
        ranged_again = RangeRoute(PMTILES_URL, headers={"Range": "bytes=0-3"})
        await handler(ranged_again)  # type: ignore[arg-type]

    assert calls == [None, "bytes=0-3", "bytes=0-3"]
    assert ranged.fulfilled is not None and ranged.fulfilled["body"] == b"PART"
    assert whole_again.fulfilled is not None and whole_again.fulfilled["body"] == b"WHOLEFIL"
    assert ranged_again.fulfilled is not None and ranged_again.fulfilled["status"] == 206
    assert cache.get(PMTILES_URL) == (b"WHOLEFIL", "application/octet-stream")


async def test_a_206_is_never_written_to_the_cache(tmp_path: Path) -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(206, content=b"PART", headers={"content-range": "bytes 0-3/8"})

    cache = TileCache(tmp_path / "tiles")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        await _handler(upstream, client, cache)(
            RangeRoute(PMTILES_URL, headers={"range": "bytes=0-3"})  # type: ignore[arg-type]
        )

    assert cache.get(PMTILES_URL) is None


async def test_non_range_tile_still_hits_the_cache_after_the_first_fetch(tmp_path: Path) -> None:
    calls = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"TILE", headers={"content-type": "image/png"})

    cache = TileCache(tmp_path / "tiles")
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        handler = _handler(upstream, client, cache)
        await handler(RangeRoute("https://trails.example.test/t.png"))  # type: ignore[arg-type]
        second = RangeRoute("https://trails.example.test/t.png")
        await handler(second)  # type: ignore[arg-type]

    assert calls == 1
    assert second.fulfilled is not None and second.fulfilled["body"] == b"TILE"


async def test_disallowed_host_is_still_blocked_even_with_a_range_header() -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        route = RangeRoute(
            "https://evil.example.test/marked-trails.pmtiles", headers={"range": "bytes=0-1"}
        )
        await _handler(upstream, client)(route)  # type: ignore[arg-type]

    assert route.aborted_reason == "blockedbyclient"
    assert route.continued is False


async def test_unreadable_page_headers_abort_instead_of_sending_a_ranged_read_whole() -> None:
    def upstream(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach upstream")

    class BrokenRoute(RangeRoute):
        async def _all(self) -> dict[str, str]:
            raise RuntimeError("page closed")

    broken = BrokenRoute(PMTILES_URL)
    broken.request.all_headers = broken._all  # type: ignore[attr-defined]
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        await _handler(upstream, client)(broken)  # type: ignore[arg-type]

    assert broken.aborted_reason == "failed"


async def test_page_javascript_reads_206_content_range_and_etag_in_real_chromium() -> None:
    """The route handler is driven by a real page, so the 206 and the exposed headers are real."""
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import async_playwright

    origin = "http://localhost:5173"
    payload = bytes(range(32))
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "range" not in request.headers:
            return httpx.Response(200, content=payload, headers={"etag": '"abc"'})
        return httpx.Response(
            206,
            content=payload[4:12],
            headers={
                "content-type": "application/octet-stream",
                "content-range": f"bytes 4-11/{len(payload)}",
                "accept-ranges": "bytes",
                "etag": '"abc"',
            },
        )

    async def serve_page(route: Any) -> None:
        await route.fulfill(
            status=200, content_type="text/html", body="<!doctype html><title>t</title>"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        handler = _handler(upstream, client)
        playwright = await async_playwright().start()
        try:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                if os.environ.get("CI"):
                    raise
                pytest.skip(f"Chromium cannot start in this environment: {exc}".splitlines()[0])
            try:
                page = await browser.new_page()
                await page.route(f"{origin}/**", serve_page)
                await page.route("https://trails.example.test/**", handler)
                await page.goto(f"{origin}/")
                result = await page.evaluate(
                    """async (url) => {
                        const ranged = await fetch(url, { headers: { Range: "bytes=4-11" } });
                        const body = new Uint8Array(await ranged.arrayBuffer());
                        return {
                            status: ranged.status,
                            contentRange: ranged.headers.get("Content-Range"),
                            etag: ranged.headers.get("ETag"),
                            length: body.length,
                            first: body[0],
                        };
                    }""",
                    PMTILES_URL,
                )
            finally:
                await browser.close()
        finally:
            await playwright.stop()

    assert result == {
        "status": 206,
        "contentRange": "bytes 4-11/32",
        "etag": '"abc"',
        "length": 8,
        "first": 4,
    }
    assert [r.headers.get("range") for r in seen] == ["bytes=4-11"]
