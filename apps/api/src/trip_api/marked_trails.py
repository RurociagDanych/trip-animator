"""Pin the Marked Trails build of a render when it is enqueued."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from trip_telemetry import MARKED_TRAILS_PIN_KEY, MarkedTrailsPin

from trip_api.config import Settings

logger = logging.getLogger(__name__)

POINTER_TIMEOUT_S = 3.0
PIN_CACHE_TTL_S = 30.0
_POINTER_MAX_BYTES = 64 * 1024

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, MarkedTrailsPin | None]] = {}


def _now() -> float:
    return time.monotonic()


def _build_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False)


def _is_plain_http_url(value: str) -> bool:
    """True for an http(s) URL with a host and without query or fragment."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(host)
        and not parsed.query
        and not parsed.fragment
        and "\\" not in value
    )


def configured_base(settings: Settings) -> str:
    """The configured trail base without a trailing slash, or "" when the overlay is off."""
    return settings.marked_trails_base_url.strip().rstrip("/")


def pointer_url(settings: Settings) -> str | None:
    """Absolute `current.json` URL for the API's own read, or None when there is none.

    `RENDER_BASE_URL` is used only here, to reach a root-relative dev base. The pin keeps the
    base as configured. An invalid value means no pin.
    """
    base = configured_base(settings)
    if not base:
        return None
    if base.startswith("/"):
        origin = settings.render_base_url.strip().rstrip("/")
        if not _is_plain_http_url(origin) or urlsplit(origin).path:
            logger.warning("RENDER_BASE_URL is not usable to resolve MARKED_TRAILS_BASE_URL")
            return None
        base = f"{origin}{base}"
    url = f"{base}/current.json"
    if not _is_plain_http_url(url):
        logger.warning("Marked Trails pointer URL is not usable")
        return None
    return url


def _parse_pointer(raw: Any, base_url: str) -> MarkedTrailsPin | None:
    if not isinstance(raw, dict):
        return None
    osm_data_date = raw.get("osm_data_date")
    try:
        return MarkedTrailsPin.model_validate(
            {
                "build_id": raw.get("current_build_id"),
                "pmtiles_key": raw.get("pmtiles_key"),
                "osm_data_date": osm_data_date if isinstance(osm_data_date, str) else None,
                "base_url": base_url,
            }
        )
    except ValidationError:
        logger.warning("Marked Trails pointer fails the pin constraints")
        return None


def _read_pointer(url: str) -> Any:
    """GET the pointer within one total deadline. Raises on any failure."""
    deadline = _now() + POINTER_TIMEOUT_S
    with (
        _build_client(POINTER_TIMEOUT_S) as client,
        client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response,
    ):
        if response.status_code != 200:
            logger.info("Marked Trails pointer not usable status=%d", response.status_code)
            raise ValueError("pointer status")
        body = bytearray()
        for chunk in response.iter_raw():
            body += chunk
            if len(body) > _POINTER_MAX_BYTES:
                logger.warning("Marked Trails pointer is larger than the limit")
                raise ValueError("pointer too large")
            if _now() > deadline:
                logger.warning("Marked Trails pointer read passed its deadline")
                raise ValueError("pointer deadline")
    return json.loads(bytes(body))


def fetch_marked_trails_pin(settings: Settings) -> MarkedTrailsPin | None:
    """Read `current.json` and return the build to pin, or None when there is no trail data.

    Blocking network call with a total deadline. Callers must not hold a database row lock.
    Any failure (timeout, HTTP error, invalid pointer or URL) means no trail data, so a render
    is never blocked by the trail store.
    """
    url = pointer_url(settings)
    if url is None:
        return None
    try:
        return _parse_pointer(_read_pointer(url), configured_base(settings))
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        logger.warning("Marked Trails pointer unavailable error_type=%s", type(exc).__name__)
        return None


def get_marked_trails_pin(settings: Settings) -> MarkedTrailsPin | None:
    """`fetch_marked_trails_pin` behind a short in-process cache.

    The quote and the enqueue of one render both read this, so they pin the same build and
    agree on the content hash and on whether a stored render can be reused. The cache holds
    failures too, so a broken store is not hit by every request.
    """
    url = pointer_url(settings)
    if url is None:
        return None
    key = f"{url}|{configured_base(settings)}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and _now() - hit[0] < PIN_CACHE_TTL_S:
            return hit[1]
    pin = fetch_marked_trails_pin(settings)
    with _cache_lock:
        _cache[key] = (_now(), pin)
    return pin


def clear_pin_cache() -> None:
    with _cache_lock:
        _cache.clear()


def spec_json_with_pin(
    spec_json: dict[str, object], pin: MarkedTrailsPin | None
) -> dict[str, object]:
    """Return the stored render spec with the pin, replacing anything a client sent."""
    stored = {k: v for k, v in spec_json.items() if k != MARKED_TRAILS_PIN_KEY}
    if pin is not None:
        stored[MARKED_TRAILS_PIN_KEY] = pin.model_dump(mode="json")
    return stored
