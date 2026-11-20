"""Pure deterministic Web Mercator tile explorer engine and polyline codec.

Provides Google Encoded Polyline (1e-5) encoding/decoding, Web Mercator slippy-tile
projections, Amanatides-Woo 2D DDA supercover rasterization with antimeridian and
GPS-jump guards, sparse Max Square DP, iterative Max Cluster BFS, global + local
Max Square Expansion Advisor, and chronological first-visit tile attribution.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

from trip_telemetry.gpx_parser import haversine_distance_m

DEFAULT_EXPLORER_ZOOM: Final[int] = 14
MICRO_EXPLORER_ZOOM: Final[int] = 17
VALID_EXPLORER_ZOOMS: Final[frozenset[int]] = frozenset(
    {DEFAULT_EXPLORER_ZOOM, MICRO_EXPLORER_ZOOM}
)
MAX_MERCATOR_LAT: Final[float] = 85.05112877980659
MAX_SEGMENT_INTERPOLATION_M: Final[float] = 5_000.0

_MIN_ZOOM: Final[int] = 0
_MAX_ZOOM: Final[int] = 20
_TILE_KEY_COORD_MASK: Final[int] = (1 << 20) - 1
_POLYLINE_SCALE: Final[float] = 1e5
_EPS: Final[float] = 1e-12
_MAX_DENSE_GRID_CELLS: Final[int] = 4_000_000
_MAX_SPARSE_VOTE_OPS: Final[int] = 4_000_000


@dataclass(frozen=True, slots=True)
class MaxSquareResult:
    """Result of sparse dynamic-programming Max Square detection."""

    size: int
    anchors: tuple[tuple[int, int], ...]
    tiles: frozenset[tuple[int, int]]


@dataclass(frozen=True, slots=True)
class MaxClusterResult:
    """Result of 4-neighbor cluster core and largest connected component analysis."""

    cluster_tiles: frozenset[tuple[int, int]]
    max_cluster_tiles: frozenset[tuple[int, int]]
    size: int


@dataclass(frozen=True, slots=True)
class MissingTile:
    """Single missing gap tile required to complete a candidate Max Square expansion."""

    x: int
    y: int
    center_lat: float
    center_lon: float


@dataclass(frozen=True, slots=True)
class ExpansionCandidate:
    """Candidate (N+1)x(N+1) square window ranked by minimum missing tiles."""

    id: str
    direction: str
    anchor_x: int
    anchor_y: int
    size: int
    missing_count: int
    overlaps_current_max_square: bool
    missing_tiles: tuple[MissingTile, ...]


@dataclass(frozen=True, slots=True)
class ExpansionAdvisorResult:
    """Top Max Square expansion candidates plus next-tier (N+2)x(N+2) best gap count."""

    target_size: int
    candidates: tuple[ExpansionCandidate, ...]
    next_tier_target_size: int
    next_tier_best_missing_count: int


def _validate_zoom(zoom: int) -> int:
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not (_MIN_ZOOM <= zoom <= _MAX_ZOOM):
        raise ValueError(
            f"zoom must be an integer between {_MIN_ZOOM} and {_MAX_ZOOM}, got {zoom!r}"
        )
    return zoom


def _validate_lat_lon(lat: float, lon: float) -> tuple[float, float]:
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError(f"lat and lon must be finite numbers, got ({lat!r}, {lon!r})")
    return float(lat), float(lon)


def _validate_tile_xy(x: int, y: int, zoom: int) -> int:
    _validate_zoom(zoom)
    grid_size = 1 << zoom
    if isinstance(x, bool) or not isinstance(x, int) or not (0 <= x < grid_size):
        raise ValueError(f"tile x must be in [0, {grid_size - 1}] for zoom {zoom}, got {x!r}")
    if isinstance(y, bool) or not isinstance(y, int) or not (0 <= y < grid_size):
        raise ValueError(f"tile y must be in [0, {grid_size - 1}] for zoom {zoom}, got {y!r}")
    return grid_size


def _normalize_lon(lon: float) -> float:
    wrapped = ((lon + 180.0) % 360.0) - 180.0
    if wrapped >= 180.0:
        return -180.0
    return wrapped


def _clamp_lat(lat: float) -> float:
    return max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))


def _lat_to_tile_y_float(lat: float, grid_size: float, upper_bound: float) -> float:
    lat_rad = math.radians(_clamp_lat(lat))
    yf = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) * 0.5 * grid_size
    return min(max(yf, 0.0), upper_bound)


def _decode_signed_varint(encoded: str, index: int) -> tuple[int, int]:
    result = 0
    shift = 0
    length = len(encoded)
    while True:
        if index >= length:
            raise ValueError("Truncated encoded polyline sequence")
        chunk = ord(encoded[index]) - 63
        index += 1
        if chunk < 0 or chunk > 63:
            raise ValueError(f"Invalid encoded polyline character at index {index - 1}")
        if shift > 35:
            raise ValueError("Malformed encoded polyline varint exceeds maximum bit width")
        result |= (chunk & 0x1F) << shift
        shift += 5
        if chunk < 0x20:
            break
    delta = ~(result >> 1) if (result & 1) else (result >> 1)
    return delta, index


def _encode_signed_varint(value: int, out: list[str]) -> None:
    shifted = (~(value << 1)) if value < 0 else (value << 1)
    while shifted >= 0x20:
        out.append(chr((0x20 | (shifted & 0x1F)) + 63))
        shifted >>= 5
    out.append(chr(shifted + 63))


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google Encoded Polyline (1e-5 precision) string into ``(lat, lon)`` tuples."""
    if not encoded:
        return []
    points: list[tuple[float, float]] = []
    index = 0
    length = len(encoded)
    lat_e5 = 0
    lon_e5 = 0
    while index < length:
        dlat, index = _decode_signed_varint(encoded, index)
        dlon, index = _decode_signed_varint(encoded, index)
        lat_e5 += dlat
        lon_e5 += dlon
        points.append((lat_e5 / _POLYLINE_SCALE, lon_e5 / _POLYLINE_SCALE))
    return points


def encode_polyline(points: Sequence[tuple[float, float]]) -> str:
    """Encode ``(lat, lon)`` coordinate pairs into a Google Encoded Polyline (1e-5) string."""
    if not points:
        return ""
    out: list[str] = []
    prev_lat_e5 = 0
    prev_lon_e5 = 0
    for lat, lon in points:
        lat_f, lon_f = _validate_lat_lon(lat, lon)
        lat_e5 = round(lat_f * _POLYLINE_SCALE)
        lon_e5 = round(lon_f * _POLYLINE_SCALE)
        _encode_signed_varint(lat_e5 - prev_lat_e5, out)
        _encode_signed_varint(lon_e5 - prev_lon_e5, out)
        prev_lat_e5 = lat_e5
        prev_lon_e5 = lon_e5
    return "".join(out)


def latlon_to_tile_float(
    lat: float,
    lon: float,
    zoom: int = DEFAULT_EXPLORER_ZOOM,
) -> tuple[float, float]:
    """Project WGS84 ``(lat, lon)`` to continuous Web Mercator tile coordinates ``(xf, yf)``."""
    _validate_zoom(zoom)
    lat_f, lon_f = _validate_lat_lon(lat, lon)
    grid_size = float(1 << zoom)
    upper_bound = math.nextafter(grid_size, 0.0)
    lon_n = _normalize_lon(lon_f)
    xf = min(max(((lon_n + 180.0) / 360.0) * grid_size, 0.0), upper_bound)
    yf = _lat_to_tile_y_float(lat_f, grid_size, upper_bound)
    return xf, yf


def lat_lon_to_tile(
    lat: float,
    lon: float,
    zoom: int = DEFAULT_EXPLORER_ZOOM,
) -> tuple[int, int]:
    """Project WGS84 ``(lat, lon)`` to discrete Web Mercator slippy-tile indices ``(x, y)``."""
    xf, yf = latlon_to_tile_float(lat, lon, zoom)
    max_idx = (1 << zoom) - 1
    return (
        min(max(int(math.floor(xf)), 0), max_idx),
        min(max(int(math.floor(yf)), 0), max_idx),
    )


def tile_bounds_wgs84(
    x: int,
    y: int,
    zoom: int = DEFAULT_EXPLORER_ZOOM,
) -> tuple[float, float, float, float]:
    """Return WGS84 bounding box ``(west, south, east, north)`` for slippy tile ``(x, y, zoom)``."""
    grid_size = float(_validate_tile_xy(x, y, zoom))
    west = (x / grid_size) * 360.0 - 180.0
    east = ((x + 1) / grid_size) * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / grid_size))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / grid_size))))
    return west, south, east, north


def tile_center_wgs84(
    x: int,
    y: int,
    zoom: int = DEFAULT_EXPLORER_ZOOM,
) -> tuple[float, float]:
    """Return WGS84 ``(center_lat, center_lon)`` at the Mercator center of tile ``(x, y, zoom)``."""
    grid_size = float(_validate_tile_xy(x, y, zoom))
    center_lon = ((x + 0.5) / grid_size) * 360.0 - 180.0
    center_lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 0.5) / grid_size))))
    return center_lat, center_lon


def tile_key(zoom: int, x: int, y: int) -> int:
    """Pack ``(zoom, x, y)`` into a deterministic 64-bit integer key."""
    _validate_tile_xy(x, y, zoom)
    return (zoom << 40) | (x << 20) | y


def unpack_tile_key(key: int) -> tuple[int, int, int]:
    """Unpack a 64-bit tile key into ``(zoom, x, y)`` with strict range validation."""
    if isinstance(key, bool) or not isinstance(key, int) or key < 0:
        raise ValueError(f"tile key must be a non-negative integer, got {key!r}")
    zoom = key >> 40
    x = (key >> 20) & _TILE_KEY_COORD_MASK
    y = key & _TILE_KEY_COORD_MASK
    _validate_tile_xy(x, y, zoom)
    return zoom, x, y


def _rasterize_tile_float_segment(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    max_idx: int,
    out: set[tuple[int, int]],
) -> None:
    """Run drift-free canonical Amanatides-Woo 2D DDA in continuous tile coordinates."""
    # Canonical endpoint ordering guarantees exact bitwise symmetry for (A, B) vs (B, A).
    if (x0, y0) > (x1, y1):
        x0, y0, x1, y1 = x1, y1, x0, y0

    ix = min(max(int(math.floor(x0)), 0), max_idx)
    iy = min(max(int(math.floor(y0)), 0), max_idx)
    tx = min(max(int(math.floor(x1)), 0), max_idx)
    ty = min(max(int(math.floor(y1)), 0), max_idx)

    out.add((ix, iy))
    if ix == tx and iy == ty:
        return

    dx = x1 - x0
    dy = y1 - y0
    step_x = 1 if tx > ix else 0
    step_y = 1 if ty > iy else (-1 if ty < iy else 0)

    while ix != tx or iy != ty:
        if ix != tx and dx != 0.0:
            t_next_x = ((ix + 1) - x0) / dx
        else:
            t_next_x = math.inf

        if iy != ty and dy != 0.0:
            next_y_line = (iy + 1) if step_y > 0 else iy
            t_next_y = (next_y_line - y0) / dy
        else:
            t_next_y = math.inf

        if abs(t_next_x - t_next_y) <= _EPS and ix != tx and iy != ty:
            # Exact grid vertex corner intersection: step diagonally without false side tiles.
            ix += step_x
            iy += step_y
        elif t_next_x < t_next_y:
            ix += step_x
        else:
            iy += step_y

        out.add((ix, iy))


def rasterize_segment_dda(
    lat0: float,
    lon0: float,
    lat1: float,
    lon1: float,
    zoom: int,
    out: set[tuple[int, int]],
) -> None:
    """Rasterize a geodesic segment into Web Mercator tiles via 2D DDA supercover traversal.

    Enforces ``MAX_SEGMENT_INTERPOLATION_M`` before rasterization (emitting only endpoint
    tiles on GPS jumps/pauses) and splits antimeridian crossings cleanly at +/-180 deg.
    """
    _validate_zoom(zoom)
    lat0_f, lon0_f = _validate_lat_lon(lat0, lon0)
    lat1_f, lon1_f = _validate_lat_lon(lat1, lon1)

    lon0_n = _normalize_lon(lon0_f)
    lon1_n = _normalize_lon(lon1_f)
    lat0_c = _clamp_lat(lat0_f)
    lat1_c = _clamp_lat(lat1_f)

    grid_size = float(1 << zoom)
    max_idx = (1 << zoom) - 1
    upper_bound = math.nextafter(grid_size, 0.0)

    x0 = min(max(((lon0_n + 180.0) / 360.0) * grid_size, 0.0), upper_bound)
    y0 = _lat_to_tile_y_float(lat0_c, grid_size, upper_bound)
    x1 = min(max(((lon1_n + 180.0) / 360.0) * grid_size, 0.0), upper_bound)
    y1 = _lat_to_tile_y_float(lat1_c, grid_size, upper_bound)

    if haversine_distance_m(lat0_f, lon0_n, lat1_f, lon1_n) > MAX_SEGMENT_INTERPOLATION_M:
        start_tile = (
            min(max(int(math.floor(x0)), 0), max_idx),
            min(max(int(math.floor(y0)), 0), max_idx),
        )
        end_tile = (
            min(max(int(math.floor(x1)), 0), max_idx),
            min(max(int(math.floor(y1)), 0), max_idx),
        )
        out.add(start_tile)
        out.add(end_tile)
        return

    if abs(lon1_n - lon0_n) > 180.0:
        # Order east-hemisphere (+lon) endpoint first so crossing latitude is bitwise symmetric.
        if lon0_n > lon1_n:
            lat_east, lon_east, x_east, y_east = lat0_c, lon0_n, x0, y0
            lat_west, lon_west, x_west, y_west = lat1_c, lon1_n, x1, y1
        else:
            lat_east, lon_east, x_east, y_east = lat1_c, lon1_n, x1, y1
            lat_west, lon_west, x_west, y_west = lat0_c, lon0_n, x0, y0

        dist_east = 180.0 - lon_east
        dist_west = lon_west + 180.0
        span = dist_east + dist_west
        lat_cross = (
            lat_east if span <= 0.0 else (dist_west * lat_east + dist_east * lat_west) / span
        )
        y_cross = _lat_to_tile_y_float(lat_cross, grid_size, upper_bound)

        _rasterize_tile_float_segment(x_east, y_east, upper_bound, y_cross, max_idx, out)
        _rasterize_tile_float_segment(0.0, y_cross, x_west, y_west, max_idx, out)
        return

    _rasterize_tile_float_segment(x0, y0, x1, y1, max_idx, out)


def rasterize_polyline_tiles(
    points_or_polyline: Sequence[tuple[float, float]] | str,
    zoom: int = DEFAULT_EXPLORER_ZOOM,
) -> set[tuple[int, int]]:
    """Rasterize either an encoded polyline string or ``(lat, lon)`` sequence into tiles."""
    _validate_zoom(zoom)
    points: Sequence[tuple[float, float]] = (
        decode_polyline(points_or_polyline)
        if isinstance(points_or_polyline, str)
        else points_or_polyline
    )
    out: set[tuple[int, int]] = set()
    if not points:
        return out
    if len(points) == 1:
        lat, lon = points[0]
        out.add(lat_lon_to_tile(lat, lon, zoom))
        return out
    for idx in range(len(points) - 1):
        lat0, lon0 = points[idx]
        lat1, lon1 = points[idx + 1]
        rasterize_segment_dda(lat0, lon0, lat1, lon1, zoom, out)
    return out


def rasterize_track_segments(
    segments: Sequence[Sequence[tuple[float, float]]],
    zoom: int = DEFAULT_EXPLORER_ZOOM,
) -> set[tuple[int, int]]:
    """Rasterize multi-segment tracks without interpolating across segment boundaries."""
    _validate_zoom(zoom)
    out: set[tuple[int, int]] = set()
    for segment in segments:
        if not segment:
            continue
        if len(segment) == 1:
            lat, lon = segment[0]
            out.add(lat_lon_to_tile(lat, lon, zoom))
            continue
        for idx in range(len(segment) - 1):
            lat0, lon0 = segment[idx]
            lat1, lon1 = segment[idx + 1]
            rasterize_segment_dda(lat0, lon0, lat1, lon1, zoom, out)
    return out


def _compute_max_square_dp(
    visited_set: set[tuple[int, int]],
) -> tuple[MaxSquareResult, dict[tuple[int, int], int]]:
    if not visited_set:
        return MaxSquareResult(size=0, anchors=(), tiles=frozenset()), {}

    dp: dict[tuple[int, int], int] = {}
    max_size = 0
    anchors_list: list[tuple[int, int]] = []

    for x, y in sorted(visited_set, key=lambda pt: (pt[1], pt[0])):
        side = 1 + min(
            dp.get((x - 1, y), 0),
            dp.get((x, y - 1), 0),
            dp.get((x - 1, y - 1), 0),
        )
        dp[(x, y)] = side
        if side > max_size:
            max_size = side
            anchors_list = [(x - side + 1, y - side + 1)]
        elif side == max_size:
            anchors_list.append((x - side + 1, y - side + 1))

    anchors = tuple(sorted(anchors_list))
    if max_size == 1:
        square_tiles = frozenset(visited_set)
    else:
        tiles_acc: set[tuple[int, int]] = set()
        for ax, ay in anchors:
            for dy in range(max_size):
                row_y = ay + dy
                for dx in range(max_size):
                    tiles_acc.add((ax + dx, row_y))
        square_tiles = frozenset(tiles_acc)

    return MaxSquareResult(size=max_size, anchors=anchors, tiles=square_tiles), dp


def compute_max_square(visited: Iterable[tuple[int, int]]) -> MaxSquareResult:
    """Compute the largest fully visited N x N square(s) using sparse O(T log T) DP."""
    result, _ = _compute_max_square_dp(set(visited))
    return result


def compute_max_cluster(visited: Iterable[tuple[int, int]]) -> MaxClusterResult:
    """Compute 4-neighbor cluster tiles and the largest 4-connected cluster via iterative BFS."""
    visited_set = set(visited)
    if not visited_set:
        return MaxClusterResult(
            cluster_tiles=frozenset(),
            max_cluster_tiles=frozenset(),
            size=0,
        )

    cluster_tiles = {
        (x, y)
        for x, y in visited_set
        if (x - 1, y) in visited_set
        and (x + 1, y) in visited_set
        and (x, y - 1) in visited_set
        and (x, y + 1) in visited_set
    }
    unseen = set(cluster_tiles)
    best_component: set[tuple[int, int]] = set()
    best_min_tile: tuple[int, int] | None = None

    while unseen:
        seed = unseen.pop()
        component: set[tuple[int, int]] = {seed}
        min_tile = seed
        queue: deque[tuple[int, int]] = deque((seed,))
        while queue:
            cx, cy = queue.popleft()
            for nb in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if nb in unseen:
                    unseen.remove(nb)
                    component.add(nb)
                    if nb < min_tile:
                        min_tile = nb
                    queue.append(nb)

        if len(component) > len(best_component) or (
            len(component) == len(best_component)
            and best_min_tile is not None
            and min_tile < best_min_tile
        ):
            best_component = component
            best_min_tile = min_tile

    return MaxClusterResult(
        cluster_tiles=frozenset(cluster_tiles),
        max_cluster_tiles=frozenset(best_component),
        size=len(best_component),
    )


def _partition_spatial_components(
    visited_set: set[tuple[int, int]],
    window_size: int,
) -> list[list[tuple[int, int]]]:
    """Group tiles into 8-connected coarse buckets of side ``window_size``.

    Any two tiles that can coexist inside the same ``window_size x window_size``
    window lie in identical or 8-adjacent buckets, so no optimal window can ever
    span multiple components.
    """
    buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for x, y in visited_set:
        key = (x // window_size, y // window_size)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = [(x, y)]
        else:
            bucket.append((x, y))

    unseen_buckets = set(buckets)
    components: list[list[tuple[int, int]]] = []
    while unseen_buckets:
        seed = unseen_buckets.pop()
        comp_tiles: list[tuple[int, int]] = list(buckets[seed])
        queue: deque[tuple[int, int]] = deque((seed,))
        while queue:
            bx, by = queue.popleft()
            for dx in (-1, 0, 1):
                nbx = bx + dx
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nb = (nbx, by + dy)
                    if nb in unseen_buckets:
                        unseen_buckets.remove(nb)
                        comp_tiles.extend(buckets[nb])
                        queue.append(nb)
        components.append(comp_tiles)

    components.sort(key=len, reverse=True)
    return components


def _overlaps_max_square(
    anchor_x: int,
    anchor_y: int,
    window_size: int,
    current_size: int,
    current_anchors: tuple[tuple[int, int], ...],
    missing_count: int,
) -> bool:
    if current_size <= 0 or not current_anchors:
        return False
    if current_size == 1:
        return missing_count < window_size * window_size
    ax_end = anchor_x + window_size
    ay_end = anchor_y + window_size
    return any(
        anchor_x < x0 + current_size
        and ax_end > x0
        and anchor_y < y0 + current_size
        and ay_end > y0
        for x0, y0 in current_anchors
    )


def _count_window_missing(
    visited_set: set[tuple[int, int]],
    anchor_x: int,
    anchor_y: int,
    window_size: int,
) -> int:
    missing = 0
    for dy in range(window_size):
        row_y = anchor_y + dy
        for dx in range(window_size):
            if (anchor_x + dx, row_y) not in visited_set:
                missing += 1
    return missing


def _find_best_windows(
    visited_set: set[tuple[int, int]],
    window_size: int,
    grid_size: int,
    max_candidates: int,
    *,
    current_size: int,
    current_anchors: tuple[tuple[int, int], ...],
    local_directions: dict[tuple[int, int], str],
    dp: dict[tuple[int, int], int],
    max_dense_grid_cells: int = _MAX_DENSE_GRID_CELLS,
) -> list[tuple[int, int, int, bool, str]]:
    """Find up to ``max_candidates`` optimal ``window_size x window_size`` windows.

    Returns sorted tuples ``(missing_count, anchor_x, anchor_y, overlaps, direction)``.
    """
    max_anchor = grid_size - window_size
    if max_anchor < 0 or not visited_set:
        return []

    target_area = window_size * window_size
    candidate_missing: dict[tuple[int, int], int] = {}

    # 1. Seed exact local directional shifts around current Max Square(s).
    for ax, ay in local_directions:
        candidate_missing[(ax, ay)] = _count_window_missing(visited_set, ax, ay, window_size)

    def _rank_key(item: tuple[int, int, int, bool, str]) -> tuple[int, bool, bool, int, int]:
        missing, ax, ay, overlaps, direction = item
        return (missing, not overlaps, direction == "GLOBAL", ax, ay)

    def _current_cutoff() -> int:
        if len(candidate_missing) < max_candidates:
            return target_area
        ranked_missing = sorted(candidate_missing.values())
        return ranked_missing[max_candidates - 1]

    # 2. Global optimal window solver across spatial proximity components.
    components = _partition_spatial_components(visited_set, window_size)
    for comp in components:
        cutoff = _current_cutoff()
        min_possible_missing = max(0, target_area - len(comp))
        if min_possible_missing > cutoff:
            continue

        xs = [pt[0] for pt in comp]
        ys = [pt[1] for pt in comp]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        ax_min = max(0, min_x - (window_size - 1))
        ax_max = min(max_anchor, max_x)
        ay_min = max(0, min_y - (window_size - 1))
        ay_max = min(max_anchor, max_y)
        if ax_min > ax_max or ay_min > ay_max:
            continue

        grid_w = (ax_max - ax_min) + window_size
        grid_h = (ay_max - ay_min) + window_size

        if grid_w * grid_h <= max_dense_grid_cells:
            prefix: NDArray[np.int32] = np.zeros((grid_h + 1, grid_w + 1), dtype=np.int32)
            x_idx = np.fromiter((pt[0] - ax_min + 1 for pt in comp), dtype=np.intp, count=len(comp))
            y_idx = np.fromiter((pt[1] - ay_min + 1 for pt in comp), dtype=np.intp, count=len(comp))
            prefix[y_idx, x_idx] = 1
            np.cumsum(prefix, axis=0, out=prefix)
            np.cumsum(prefix, axis=1, out=prefix)

            visited_counts = (
                prefix[window_size:, window_size:]
                - prefix[:-window_size, window_size:]
                - prefix[window_size:, :-window_size]
                + prefix[:-window_size, :-window_size]
            )
            missing_grid = target_area - visited_counts
            flat = missing_grid.ravel()
            k_index = min(max_candidates - 1, flat.size - 1)
            kth_value = int(np.partition(flat, k_index)[k_index])
            threshold = min(cutoff, kth_value)

            # Collect all anchors strictly better than threshold (guaranteed < max_candidates).
            strict_ys, strict_xs = np.nonzero(missing_grid < threshold)
            for dy_i, dx_i in zip(strict_ys.tolist(), strict_xs.tolist(), strict=True):
                ax = ax_min + dx_i
                ay = ay_min + dy_i
                candidate_missing[(ax, ay)] = int(missing_grid[dy_i, dx_i])

            # Collect tie-level anchors at `threshold` with exact tie-breaking and bounded work.
            tie_ys, tie_xs = np.nonzero(missing_grid == threshold)
            tie_count = len(tie_xs)
            if tie_count > 0:
                if tie_count <= 256:
                    for dy_i, dx_i in zip(tie_ys.tolist(), tie_xs.tolist(), strict=True):
                        ax = ax_min + dx_i
                        ay = ay_min + dy_i
                        candidate_missing[(ax, ay)] = threshold
                else:
                    # Prioritize local shifts and overlapping anchors deterministically by (ax, ay).
                    tie_anchors_x = tie_xs + ax_min
                    tie_anchors_y = tie_ys + ay_min
                    order = np.lexsort((tie_anchors_y, tie_anchors_x))
                    sorted_ax = tie_anchors_x[order]
                    sorted_ay = tie_anchors_y[order]

                    overlap_picked = 0
                    non_overlap_picked = 0
                    for ax_i, ay_i in zip(sorted_ax.tolist(), sorted_ay.tolist(), strict=True):
                        overlaps = _overlaps_max_square(
                            ax_i, ay_i, window_size, current_size, current_anchors, threshold
                        )
                        if overlaps:
                            if overlap_picked < max_candidates:
                                candidate_missing[(ax_i, ay_i)] = threshold
                                overlap_picked += 1
                        elif non_overlap_picked < max_candidates:
                            candidate_missing[(ax_i, ay_i)] = threshold
                            non_overlap_picked += 1
                        if (
                            overlap_picked >= max_candidates
                            and non_overlap_picked >= max_candidates
                        ):
                            break
        elif len(comp) * target_area <= _MAX_SPARSE_VOTE_OPS:
            # Exact sparse vote counter for huge sparse bounding boxes (e.g. long diagonal routes).
            votes: dict[tuple[int, int], int] = {}
            for x, y in comp:
                start_ax = max(0, x - window_size + 1)
                end_ax = min(max_anchor, x)
                start_ay = max(0, y - window_size + 1)
                end_ay = min(max_anchor, y)
                for ay in range(start_ay, end_ay + 1):
                    for ax in range(start_ax, end_ax + 1):
                        key = (ax, ay)
                        votes[key] = votes.get(key, 0) + 1
            for (ax, ay), count in votes.items():
                missing = target_area - count
                if missing <= cutoff:
                    candidate_missing[(ax, ay)] = missing
        else:
            # Bounded high-DP sparse fallback for giant dense transcontinental components.
            min_dp = max(1, current_size - 3)
            seeded_anchors: set[tuple[int, int]] = set()
            for x, y in comp:
                if dp.get((x, y), 0) >= min_dp:
                    for dy in range(window_size):
                        ay = min(max(0, y - dy), max_anchor)
                        for dx in range(window_size):
                            ax = min(max(0, x - dx), max_anchor)
                            seeded_anchors.add((ax, ay))
            for ax, ay in seeded_anchors:
                if (ax, ay) not in candidate_missing:
                    candidate_missing[(ax, ay)] = _count_window_missing(
                        visited_set, ax, ay, window_size
                    )

    scored: list[tuple[int, int, int, bool, str]] = []
    for (ax, ay), missing in candidate_missing.items():
        direction = local_directions.get((ax, ay), "GLOBAL")
        overlaps = _overlaps_max_square(ax, ay, window_size, current_size, current_anchors, missing)
        scored.append((missing, ax, ay, overlaps, direction))

    scored.sort(key=_rank_key)
    return scored[:max_candidates]


def compute_max_square_expansion_advisor(
    visited: Iterable[tuple[int, int]],
    zoom: int = DEFAULT_EXPLORER_ZOOM,
    max_candidates: int = 5,
) -> ExpansionAdvisorResult:
    """Compute optimal ``(N+1)x(N+1)`` Max Square expansion candidates and ``(N+2)x(N+2)`` target.

    Evaluates cardinal/diagonal ``NW/NE/SW/SE`` shifts around all current Max Square anchors
    plus a global optimal window search across spatial proximity components using NumPy 2D
    summed-area prefix sums (with sparse fallback for bounding boxes > 4M cells).
    """
    _validate_zoom(zoom)
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or max_candidates < 1
    ):
        raise ValueError(f"max_candidates must be a positive integer, got {max_candidates!r}")

    grid_size = 1 << zoom
    visited_set = {(x, y) for x, y in visited if 0 <= x < grid_size and 0 <= y < grid_size}

    max_sq, dp = _compute_max_square_dp(visited_set)
    current_size = max_sq.size
    target_size = current_size + 1
    next_tier_target_size = current_size + 2

    if not visited_set:
        next_missing = (
            next_tier_target_size * next_tier_target_size
            if next_tier_target_size <= grid_size
            else 0
        )
        return ExpansionAdvisorResult(
            target_size=target_size,
            candidates=(),
            next_tier_target_size=next_tier_target_size,
            next_tier_best_missing_count=next_missing,
        )

    if target_size > grid_size:
        return ExpansionAdvisorResult(
            target_size=target_size,
            candidates=(),
            next_tier_target_size=next_tier_target_size,
            next_tier_best_missing_count=0,
        )

    max_anchor = grid_size - target_size
    local_directions: dict[tuple[int, int], str] = {}
    for x0, y0 in max_sq.anchors:
        for direction, raw_ax, raw_ay in (
            ("NW", x0 - 1, y0 - 1),
            ("NE", x0, y0 - 1),
            ("SW", x0 - 1, y0),
            ("SE", x0, y0),
        ):
            if 0 <= raw_ax <= max_anchor and 0 <= raw_ay <= max_anchor:
                local_directions.setdefault((raw_ax, raw_ay), direction)

    top_windows = _find_best_windows(
        visited_set,
        target_size,
        grid_size,
        max_candidates,
        current_size=current_size,
        current_anchors=max_sq.anchors,
        local_directions=local_directions,
        dp=dp,
    )

    candidates: list[ExpansionCandidate] = []
    for missing_count, ax, ay, overlaps, direction in top_windows:
        missing_tiles: list[MissingTile] = []
        for dy in range(target_size):
            ty = ay + dy
            for dx in range(target_size):
                tx = ax + dx
                if (tx, ty) not in visited_set:
                    clat, clon = tile_center_wgs84(tx, ty, zoom)
                    missing_tiles.append(MissingTile(x=tx, y=ty, center_lat=clat, center_lon=clon))
        candidates.append(
            ExpansionCandidate(
                id=f"{direction.lower()}-{ax}-{ay}",
                direction=direction,
                anchor_x=ax,
                anchor_y=ay,
                size=target_size,
                missing_count=missing_count,
                overlaps_current_max_square=overlaps,
                missing_tiles=tuple(missing_tiles),
            )
        )

    if next_tier_target_size > grid_size:
        next_tier_best_missing = 0
    else:
        next_max_anchor = grid_size - next_tier_target_size
        next_local: dict[tuple[int, int], str] = {}
        for x0, y0 in max_sq.anchors:
            for raw_ax in (x0 - 2, x0 - 1, x0):
                for raw_ay in (y0 - 2, y0 - 1, y0):
                    if 0 <= raw_ax <= next_max_anchor and 0 <= raw_ay <= next_max_anchor:
                        next_local.setdefault((raw_ax, raw_ay), "GLOBAL")
        next_windows = _find_best_windows(
            visited_set,
            next_tier_target_size,
            grid_size,
            1,
            current_size=current_size,
            current_anchors=max_sq.anchors,
            local_directions=next_local,
            dp=dp,
        )
        next_tier_best_missing = (
            next_windows[0][0] if next_windows else next_tier_target_size * next_tier_target_size
        )

    return ExpansionAdvisorResult(
        target_size=target_size,
        candidates=tuple(candidates),
        next_tier_target_size=next_tier_target_size,
        next_tier_best_missing_count=next_tier_best_missing,
    )


def compute_chronological_new_tile_counts[K: Hashable](
    ordered_activities: Sequence[tuple[K, Iterable[int | tuple[int, int]]]],
) -> dict[K, int]:
    """Compute first-visit new tile counts per activity preserving input order."""
    seen: set[int | tuple[int, int]] = set()
    counts: dict[K, int] = {}
    for activity_id, tiles in ordered_activities:
        new_count = 0
        for tile in tiles:
            if tile not in seen:
                seen.add(tile)
                new_count += 1
        counts[activity_id] = counts.get(activity_id, 0) + new_count
    return counts
