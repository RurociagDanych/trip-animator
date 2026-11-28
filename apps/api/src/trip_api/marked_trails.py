"""Pin the Marked Trails build of a render when it is enqueued."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from trip_telemetry import MARKED_TRAILS_PIN_KEY, MarkedTrailsPin

from trip_api.config import Settings

logger = logging.getLogger(__name__)

POINTER_TIMEOUT_S = 3.0
_POINTER_MAX_BYTES = 64 * 1024
_PMTILES_KEY = re.compile(r"^builds/\d{4}-\d{2}-\d{2}(-\d+)?/marked-trails\.pmtiles$")


def _build_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False)


def resolve_marked_trails_base_url(settings: Settings) -> str:
    """Return the absolute trail base URL without a trailing slash, or "" when off.

    An https base stays as is. A root-relative dev base is resolved against the render base
    URL, the origin the worker loads the render page from.
    """
    base = settings.marked_trails_base_url.strip().rstrip("/")
    if base.startswith("/"):
        return f"{settings.render_base_url.strip().rstrip('/')}{base}"
    return base


def _parse_pointer(raw: Any, base_url: str) -> MarkedTrailsPin | None:
    if not isinstance(raw, dict):
        return None
    build_id = raw.get("current_build_id")
    key = raw.get("pmtiles_key")
    if not isinstance(build_id, str) or not build_id:
        return None
    if not isinstance(key, str) or not _PMTILES_KEY.fullmatch(key):
        return None
    osm_data_date = raw.get("osm_data_date")
    return MarkedTrailsPin(
        build_id=build_id,
        pmtiles_key=key,
        osm_data_date=osm_data_date if isinstance(osm_data_date, str) else None,
        base_url=base_url,
    )


def fetch_marked_trails_pin(settings: Settings) -> MarkedTrailsPin | None:
    """Read `current.json` and return the build to pin, or None when there is no trail data.

    Blocking network call with a short timeout. Callers must not hold a database row lock.
    Any failure (timeout, HTTP error, invalid pointer) means no trail data, so a render is
    never blocked by the trail store.
    """
    base_url = resolve_marked_trails_base_url(settings)
    if not base_url:
        return None
    url = f"{base_url}/current.json"
    try:
        with (
            _build_client(POINTER_TIMEOUT_S) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                logger.info("Marked Trails pointer not usable status=%d", response.status_code)
                return None
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > _POINTER_MAX_BYTES:
                    logger.warning("Marked Trails pointer is larger than the limit")
                    return None
        return _parse_pointer(json.loads(body), base_url)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Marked Trails pointer unavailable error_type=%s", type(exc).__name__)
        return None


def spec_json_with_pin(
    spec_json: dict[str, object], pin: MarkedTrailsPin | None
) -> dict[str, object]:
    """Return the stored render spec with the pin, replacing anything a client sent."""
    stored = {k: v for k, v in spec_json.items() if k != MARKED_TRAILS_PIN_KEY}
    if pin is not None:
        stored[MARKED_TRAILS_PIN_KEY] = pin.model_dump(mode="json")
    return stored
