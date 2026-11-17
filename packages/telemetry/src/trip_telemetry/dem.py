"""Offline-safe DEM elevation backfill using Terrarium RGB PNG tiles or custom providers."""

from __future__ import annotations

import ipaddress
import math
import socket
import struct
import urllib.request
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import numpy as np

from trip_telemetry.schemas import IngestWarning, ParsedTrack, TrackPoint

DEFAULT_TERRARIUM_URL_TEMPLATE = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
)
DEFAULT_DEM_ZOOM = 12
_DEFAULT_ALLOWED_DEM_HOSTS = frozenset({"s3.amazonaws.com"})


class ElevationProvider(Protocol):
    """Protocol for sampling elevation in meters at WGS84 (lat, lon) coordinates."""

    def sample(self, lats: Sequence[float], lons: Sequence[float]) -> list[float | None]: ...


def lat_lon_to_tile_pixel(
    lat: float,
    lon: float,
    zoom: int = DEFAULT_DEM_ZOOM,
    tile_size: int = 256,
) -> tuple[int, int, int, int]:
    """Convert WGS84 (lat, lon) to WebMercator (tile_x, tile_y, px, py)."""
    clamped_lat = max(min(lat, 85.05112878), -85.05112878)
    wrapped_lon = ((lon + 180.0) % 360.0) - 180.0
    n = 1 << zoom
    x_float = (wrapped_lon + 180.0) / 360.0 * n
    lat_rad = math.radians(clamped_lat)
    y_float = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n

    tile_x = max(0, min(n - 1, int(math.floor(x_float))))
    tile_y = max(0, min(n - 1, int(math.floor(y_float))))
    px = max(0, min(tile_size - 1, int(math.floor((x_float - tile_x) * tile_size))))
    py = max(0, min(tile_size - 1, int(math.floor((y_float - tile_y) * tile_size))))
    return tile_x, tile_y, px, py


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_rgb_png(png_bytes: bytes) -> np.ndarray:
    """Decode an 8-bit RGB/RGBA PNG image into a (height, width, 3) uint8 NumPy array."""
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Invalid PNG signature")

    offset = 8
    width = 0
    height = 0
    bit_depth = 0
    color_type = 0
    idat_chunks: list[bytes] = []

    while offset + 8 <= len(png_bytes):
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        if data_end + 4 > len(png_bytes):
            raise ValueError("Truncated PNG chunk")
        chunk_data = png_bytes[data_start:data_end]
        offset = data_end + 4

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (
                bit_depth != 8
                or color_type not in (2, 6)
                or comp != 0
                or filt != 0
                or interlace != 0
            ):
                raise ValueError(
                    f"Unsupported PNG format (depth={bit_depth}, "
                    f"color={color_type}, interlace={interlace})"
                )
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if width <= 0 or height <= 0 or not idat_chunks:
        raise ValueError("PNG missing IHDR or IDAT payload")

    channels = 3 if color_type == 2 else 4
    stride = width * channels
    raw = zlib.decompress(b"".join(idat_chunks))
    expected_len = height * (1 + stride)
    if len(raw) != expected_len:
        raise ValueError(f"Unexpected decompressed PNG size: {len(raw)} != {expected_len}")

    img = np.zeros((height, stride), dtype=np.uint8)
    raw_view = memoryview(raw)
    prev_row = np.zeros(stride, dtype=np.int32)

    for y in range(height):
        row_start = y * (1 + stride)
        filter_type = raw_view[row_start]
        scanline = np.frombuffer(raw_view[row_start + 1 : row_start + 1 + stride], dtype=np.uint8)
        recon = scanline.astype(np.int32)

        if filter_type == 0:
            pass
        elif filter_type == 1:
            for x in range(channels, stride):
                recon[x] = (recon[x] + recon[x - channels]) & 0xFF
        elif filter_type == 2:
            recon = (recon + prev_row) & 0xFF
        elif filter_type == 3:
            for x in range(stride):
                left = int(recon[x - channels]) if x >= channels else 0
                up = int(prev_row[x])
                recon[x] = (recon[x] + ((left + up) >> 1)) & 0xFF
        elif filter_type == 4:
            for x in range(stride):
                left = int(recon[x - channels]) if x >= channels else 0
                up = int(prev_row[x])
                up_left = int(prev_row[x - channels]) if x >= channels else 0
                recon[x] = (recon[x] + _paeth_predictor(left, up, up_left)) & 0xFF
        else:
            raise ValueError(f"Unsupported PNG filter type: {filter_type}")

        img[y] = recon.astype(np.uint8)
        prev_row = recon

    reshaped = img.reshape((height, width, channels))
    return reshaped[:, :, :3]


def terrarium_rgb_to_meters(r: int, g: int, b: int) -> float:
    """Decode Mapzen/AWS Terrarium RGB pixel values into elevation meters."""
    return (float(r) * 256.0 + float(g) + float(b) / 256.0) - 32768.0


def _validate_dem_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Blocked non-HTTPS DEM URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host or host not in allowed_hosts:
        raise ValueError(f"DEM host {host!r} is not in allowlist")
    for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"Blocked non-public DEM IP address {ip}")


class TerrariumDemProvider:
    """Samples elevation from local or SSRF-guarded remote Terrarium PNG tiles."""

    def __init__(
        self,
        *,
        local_dir: Path | None = None,
        zoom: int = DEFAULT_DEM_ZOOM,
        tile_fetcher: Callable[[int, int, int], bytes | None] | None = None,
        allow_remote: bool = False,
        url_template: str = DEFAULT_TERRARIUM_URL_TEMPLATE,
        allowed_hosts: frozenset[str] = _DEFAULT_ALLOWED_DEM_HOSTS,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.local_dir = local_dir
        self.zoom = zoom
        self.tile_fetcher = tile_fetcher
        self.allow_remote = allow_remote
        self.url_template = url_template
        self.allowed_hosts = allowed_hosts
        self.timeout_seconds = timeout_seconds
        self._cache: dict[tuple[int, int, int], np.ndarray | None] = {}

    def _load_tile(self, z: int, x: int, y: int) -> np.ndarray | None:
        key = (z, x, y)
        if key in self._cache:
            return self._cache[key]

        raw_png: bytes | None = None
        if self.local_dir is not None:
            candidate = (self.local_dir / str(z) / str(x) / f"{y}.png").resolve()
            if candidate.is_relative_to(self.local_dir.resolve()) and candidate.is_file():
                raw_png = candidate.read_bytes()

        if raw_png is None and self.tile_fetcher is not None:
            raw_png = self.tile_fetcher(z, x, y)

        if raw_png is None and self.allow_remote:
            url = self.url_template.format(z=z, x=x, y=y)
            try:
                _validate_dem_url(url, self.allowed_hosts)
                req = urllib.request.Request(url, headers={"User-Agent": "trip-animator/2.0"})  # noqa: S310
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:  # noqa: S310
                    if getattr(resp, "status", 200) == 200:
                        raw_png = resp.read(2 * 1024 * 1024)
            except Exception:
                raw_png = None

        if raw_png is None:
            self._cache[key] = None
            return None

        try:
            decoded = decode_rgb_png(raw_png)
        except Exception:
            self._cache[key] = None
            return None

        self._cache[key] = decoded
        return decoded

    def sample(self, lats: Sequence[float], lons: Sequence[float]) -> list[float | None]:
        results: list[float | None] = []
        for lat, lon in zip(lats, lons, strict=True):
            if not (math.isfinite(lat) and math.isfinite(lon)):
                results.append(None)
                continue
            tx, ty, px, py = lat_lon_to_tile_pixel(lat, lon, zoom=self.zoom)
            tile = self._load_tile(self.zoom, tx, ty)
            if tile is None:
                results.append(None)
                continue
            h, w, _ = tile.shape
            px_c = max(0, min(w - 1, px))
            py_c = max(0, min(h - 1, py))
            r, g, b = int(tile[py_c, px_c, 0]), int(tile[py_c, px_c, 1]), int(tile[py_c, px_c, 2])
            elev = terrarium_rgb_to_meters(r, g, b)
            results.append(round(elev, 2) if math.isfinite(elev) else None)
        return results


def backfill_track_elevation(
    track: ParsedTrack,
    provider: ElevationProvider | None = None,
    *,
    local_dem_dir: Path | None = None,
    allow_remote: bool = False,
    force: bool = False,
) -> ParsedTrack:
    """Backfill missing TrackPoint elevations from a DEM provider, failing safe offline."""
    if not track.points:
        return track
    if track.has_elevation and all(p.elev_m is not None for p in track.points) and not force:
        return track

    resolved_provider = provider
    if resolved_provider is None:
        if local_dem_dir is None and not allow_remote:
            return track
        resolved_provider = TerrariumDemProvider(
            local_dir=local_dem_dir,
            allow_remote=allow_remote,
        )

    target_indices = [idx for idx, pt in enumerate(track.points) if force or pt.elev_m is None]
    if not target_indices:
        return track

    sampled = resolved_provider.sample(
        [track.points[i].lat for i in target_indices],
        [track.points[i].lon for i in target_indices],
    )
    if not any(val is not None for val in sampled):
        return track

    updated_points: list[TrackPoint] = list(track.points)
    filled_count = 0
    for idx, val in zip(target_indices, sampled, strict=True):
        if val is not None and math.isfinite(val):
            updated_points[idx] = updated_points[idx].model_copy(update={"elev_m": float(val)})
            filled_count += 1

    if filled_count == 0:
        return track

    elev_count = sum(1 for p in updated_points if p.elev_m is not None)
    new_warnings = [
        w for w in track.warnings if w.code not in ("MISSING_ELEVATION", "PARTIAL_ELEVATION")
    ]
    if elev_count < len(updated_points):
        new_warnings.append(
            IngestWarning(
                code="PARTIAL_ELEVATION",
                message="Some trackpoints lack elevation data",
                count=1,
            )
        )
    new_warnings.append(
        IngestWarning(
            code="DEM_ELEVATION_BACKFILLED",
            message="Missing elevation values backfilled from DEM terrain tiles",
            count=filled_count,
        )
    )

    return track.model_copy(
        update={
            "points": updated_points,
            "has_elevation": elev_count > 0,
            "warnings": new_warnings,
        }
    )
