"""Bounded canonical summary geometry and explicit geographic selection."""

from __future__ import annotations

import math
from itertools import pairwise

from trip_telemetry.tiles import decode_polyline

Bounds = tuple[float, float, float, float]
MAX_ENCODED_LENGTH = 40_000
MAX_TRACK_POINTS = 10_000
MAX_RESPONSE_POINTS = 100_000
MAX_EVALUATION_CHARS = 2_000_000


def map_coordinates(points: list[tuple[float, float]]) -> list[list[float]]:
    """Unwrap longitude for short rendered segments across the dateline."""
    coordinates: list[list[float]] = []
    for lat, lon in points:
        if coordinates:
            previous = coordinates[-1][0]
            lon += 360 * round((previous - lon) / 360)
        coordinates.append([lon, lat])
    return coordinates


def overview_points(points: list[tuple[float, float]], limit: int) -> list[tuple[float, float]]:
    """Sample a track deterministically while retaining both endpoints."""
    if len(points) <= limit:
        return points
    return [points[index * (len(points) - 1) // (limit - 1)] for index in range(limit)]


def parse_area(value: str) -> Bounds:
    try:
        west, south, east, north = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise ValueError("area must contain west,south,east,north") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)) or not (
        -180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= north <= 90
    ):
        raise ValueError("area coordinates are invalid")
    return west, south, east, north


def canonical_points(has_gps: bool, encoded: str | None) -> list[tuple[float, float]] | None:
    if not has_gps or not encoded or len(encoded) > MAX_ENCODED_LENGTH:
        return None
    try:
        points = decode_polyline(encoded)
    except (ValueError, IndexError, OverflowError):
        return None
    if not 2 <= len(points) <= MAX_TRACK_POINTS or any(
        not (-90 <= lat <= 90 and -180 <= lon <= 180) for lat, lon in points
    ):
        return None
    return points


def _segment_hits(x: float, y: float, end_x: float, end_y: float, bounds: Bounds) -> bool:
    """Liang-Barsky clipping, including a segment touching the boundary."""
    west, south, east, north = bounds
    dx, dy = end_x - x, end_y - y
    low, high = 0.0, 1.0
    for p, q in ((-dx, x - west), (dx, east - x), (-dy, y - south), (dy, north - y)):
        if p == 0:
            if q < 0:
                return False
        elif p < 0:
            low = max(low, q / p)
        else:
            high = min(high, q / p)
        if low > high:
            return False
    return True


def intersects_area(points: list[tuple[float, float]], bounds: Bounds) -> bool:
    """Match routes touching the area, including across the dateline."""
    west, south, east, north = bounds
    intervals = [(west, east)] if west <= east else [(west, 180), (-180, east)]
    rectangles = [
        (left + shift, south, right + shift, north)
        for left, right in intervals
        for shift in (-360, 0, 360)
    ]
    for (lat, lon), (end_lat, end_lon) in pairwise(points):
        if (lat < south and end_lat < south) or (lat > north and end_lat > north):
            continue
        # Unwrap the shortest segment so a dateline crossing does not cross Greenwich.
        if end_lon - lon > 180:
            end_lon -= 360
        elif end_lon - lon < -180:
            end_lon += 360
        for rectangle in rectangles:
            left, _, right, _ = rectangle
            if (lon < left and end_lon < left) or (lon > right and end_lon > right):
                continue
            if _segment_hits(lon, lat, end_lon, end_lat, rectangle):
                return True
    return False
