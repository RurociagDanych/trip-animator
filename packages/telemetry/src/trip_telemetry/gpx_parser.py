"""Hardened streaming GPX parser using defusedxml with strict resource bounds."""

from __future__ import annotations

import io
import math
import multiprocessing
import queue as queue_mod
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from multiprocessing.context import SpawnProcess

import defusedxml.ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from trip_telemetry.schemas import IngestWarning, ParsedTrack, TrackPoint

MAX_GPX_BYTES = 25 * 1024 * 1024  # 25 MiB
MAX_XML_DEPTH = 64
MAX_TRACKS = 20
MAX_SEGMENTS = 1_000
MAX_POINTS = 500_000
PARSE_TIMEOUT_SECONDS = 15.0
EARTH_RADIUS_M = 6_371_000.0
DEFAULT_SYNTHETIC_SPEED_MPS = 4.0


class GpxParseError(ValueError):
    """Raised when GPX input violates XML security policy, schema, or bounds."""


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    return tag.lower()


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        val = float(raw.strip())
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        val = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    return int(val)


def _parse_timestamp(raw: str | None) -> datetime | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle Haversine distance in meters between two WGS84 points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlon = lon2 - lon1
    while dlon > 180.0:
        dlon -= 360.0
    while dlon < -180.0:
        dlon += 360.0
    dlambda = math.radians(dlon)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    a_clamped = min(max(a, 0.0), 1.0)
    return 2.0 * EARTH_RADIUS_M * math.atan2(math.sqrt(a_clamped), math.sqrt(1.0 - a_clamped))


def unwrap_longitudes(lons: Sequence[float]) -> tuple[list[float], bool]:
    """Unwrap consecutive longitudes across the +-180 antimeridian.

    Returns (unwrapped_lons, crosses_antimeridian).
    """
    if not lons:
        return [], False
    unwrapped = [float(lons[0])]
    crosses = False
    for idx in range(1, len(lons)):
        raw_delta = float(lons[idx]) - float(lons[idx - 1])
        delta = raw_delta
        if delta > 180.0:
            delta -= 360.0
            crosses = True
        elif delta < -180.0:
            delta += 360.0
            crosses = True
        unwrapped.append(unwrapped[-1] + delta)
    return unwrapped, crosses


def compute_unwrapped_bounds(
    points: Sequence[TrackPoint],
) -> tuple[tuple[float, float, float, float], bool]:
    """Compute (min_lon, min_lat, max_lon, max_lat) with antimeridian unwrapping."""
    if not points:
        raise ValueError("Cannot compute bounds for empty point sequence")
    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    unwrapped_lons, crosses = unwrap_longitudes(lons)
    return (
        min(unwrapped_lons),
        min(lats),
        max(unwrapped_lons),
        max(lats),
    ), crosses


def _synthesize_uniform_timestamps(
    points: list[TrackPoint],
    speed_mps: float = DEFAULT_SYNTHETIC_SPEED_MPS,
) -> list[TrackPoint]:
    """Fill missing timestamps uniformly by cumulative distance along segments."""
    if not points:
        return points

    safe_speed = max(speed_mps, 0.5)
    cum_dist = [0.0] * len(points)
    for i in range(1, len(points)):
        if points[i].segment_idx == points[i - 1].segment_idx:
            cum_dist[i] = cum_dist[i - 1] + haversine_distance_m(
                points[i - 1].lat,
                points[i - 1].lon,
                points[i].lat,
                points[i].lon,
            )
        else:
            cum_dist[i] = cum_dist[i - 1]

    first_existing = next((p.ts_utc for p in points if p.ts_utc is not None), None)
    base_ts = first_existing or datetime(1970, 1, 1, tzinfo=UTC)

    updated: list[TrackPoint] = []
    prev_ts: datetime | None = None
    for idx, pt in enumerate(points):
        if pt.ts_utc is not None:
            ts = pt.ts_utc
        elif idx == 0:
            ts = base_ts
        else:
            dd = cum_dist[idx] - cum_dist[idx - 1]
            dt = max(dd / safe_speed, 1.0)
            anchor = prev_ts or base_ts
            ts = anchor + timedelta(seconds=dt)
        prev_ts = ts
        updated.append(pt.model_copy(update={"ts_utc": ts}))
    return updated


def parse_gpx_bytes(
    raw_bytes: bytes,
    *,
    synthesize_timestamps: bool = False,
    synthetic_speed_mps: float = DEFAULT_SYNTHETIC_SPEED_MPS,
    stitch_tracks: bool = False,
) -> ParsedTrack:
    """Parse a GPX payload safely without DTD/XXE expansion or unbounded memory growth."""
    if not raw_bytes:
        raise GpxParseError("GPX payload is empty")
    if len(raw_bytes) > MAX_GPX_BYTES:
        raise GpxParseError(f"GPX payload exceeds maximum size of {MAX_GPX_BYTES} bytes")

    stream = io.BytesIO(raw_bytes)
    points: list[TrackPoint] = []
    warnings: dict[str, IngestWarning] = {}

    def warn(code: str, message: str) -> None:
        if code in warnings:
            warnings[code].count += 1
        else:
            warnings[code] = IngestWarning(code=code, message=message, count=1)

    depth = 0
    tag_stack: list[str] = []
    track_count = 0
    active_track_idx = -1
    track_name = ""
    track_type_hint = ""
    segment_idx = -1
    in_active_track = False
    in_trkpt = False
    cur_lat: float | None = None
    cur_lon: float | None = None
    cur_ele: float | None = None
    cur_ts: datetime | None = None
    cur_hr: int | None = None
    cur_cad: int | None = None
    cur_watts: int | None = None
    prev_lat_lon: tuple[float, float] | None = None
    prev_ts: datetime | None = None
    non_monotonic_ts = False

    try:
        context = DefusedET.iterparse(
            stream,
            events=("start", "end"),
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
        for event, elem in context:
            tag = _local_name(elem.tag)
            if event == "start":
                depth += 1
                if depth > MAX_XML_DEPTH:
                    raise GpxParseError(f"XML nesting depth exceeded {MAX_XML_DEPTH}")
                tag_stack.append(tag)

                if tag == "trk":
                    track_count += 1
                    if track_count > MAX_TRACKS:
                        raise GpxParseError(f"Track count exceeded {MAX_TRACKS}")
                    if active_track_idx == -1:
                        active_track_idx = track_count
                        in_active_track = True
                    elif stitch_tracks:
                        active_track_idx = track_count
                        in_active_track = True
                        prev_lat_lon = None
                        warn(
                            "STITCHED_TRACKS",
                            "Stitched multiple <trk> elements into a segmented track",
                        )
                    else:
                        in_active_track = False
                        warn(
                            "MULTIPLE_TRACKS",
                            "Additional <trk> elements ignored beyond first track",
                        )
                elif tag == "trkseg" and in_active_track:
                    segment_idx += 1
                    prev_lat_lon = None
                    if segment_idx >= MAX_SEGMENTS:
                        raise GpxParseError(f"Segment count exceeded {MAX_SEGMENTS}")
                elif tag == "trkpt" and in_active_track:
                    in_trkpt = True
                    cur_lat = _parse_optional_float(elem.attrib.get("lat"))
                    cur_lon = _parse_optional_float(elem.attrib.get("lon"))
                    cur_ele = None
                    cur_ts = None
                    cur_hr = None
                    cur_cad = None
                    cur_watts = None
            else:
                parent_tag = tag_stack[-2] if len(tag_stack) >= 2 else ""
                if in_active_track:
                    if (
                        tag == "name"
                        and parent_tag == "trk"
                        and not in_trkpt
                        and not track_name
                        and elem.text
                    ):
                        track_name = elem.text.strip()[:200]
                    elif (
                        tag == "type"
                        and parent_tag == "trk"
                        and not in_trkpt
                        and not track_type_hint
                        and elem.text
                    ):
                        track_type_hint = elem.text.strip()[:64]
                    elif in_trkpt:
                        if tag == "ele":
                            parsed_ele = _parse_optional_float(elem.text)
                            if parsed_ele is None and elem.text and elem.text.strip():
                                warn(
                                    "INVALID_ELEVATION",
                                    "Ignored trackpoint elevation with non-finite or invalid value",
                                )
                            cur_ele = parsed_ele
                        elif tag == "time":
                            parsed_ts = _parse_timestamp(elem.text)
                            if parsed_ts is None and elem.text and elem.text.strip():
                                warn(
                                    "INVALID_TIMESTAMP",
                                    "Ignored trackpoint timestamp with invalid ISO-8601 format",
                                )
                            cur_ts = parsed_ts
                        elif tag in ("hr", "heartrate"):
                            val = _parse_optional_int(elem.text)
                            if val is not None and 0 <= val <= 260:
                                cur_hr = val
                        elif tag in ("cad", "cadence"):
                            val = _parse_optional_int(elem.text)
                            if val is not None and 0 <= val <= 300:
                                cur_cad = val
                        elif tag in ("power", "watts"):
                            val = _parse_optional_int(elem.text)
                            if val is not None and 0 <= val <= 3000:
                                cur_watts = val
                        elif tag == "trkpt":
                            in_trkpt = False
                            if (
                                cur_lat is None
                                or cur_lon is None
                                or not (-90.0 <= cur_lat <= 90.0)
                                or not (-180.0 <= cur_lon <= 180.0)
                            ):
                                warn("INVALID_COORD", "Dropped trackpoint with invalid coordinates")
                            elif prev_lat_lon == (cur_lat, cur_lon):
                                warn(
                                    "DUPLICATE_POINT",
                                    "Collapsed consecutive duplicate coordinate",
                                )
                            else:
                                if len(points) >= MAX_POINTS:
                                    raise GpxParseError(f"Point count exceeded {MAX_POINTS}")
                                if cur_ts is not None and prev_ts is not None and cur_ts < prev_ts:
                                    non_monotonic_ts = True
                                if cur_ts is not None:
                                    prev_ts = cur_ts
                                prev_lat_lon = (cur_lat, cur_lon)
                                points.append(
                                    TrackPoint(
                                        lat=cur_lat,
                                        lon=cur_lon,
                                        elev_m=cur_ele,
                                        ts_utc=cur_ts,
                                        hr_bpm=cur_hr,
                                        cadence=cur_cad,
                                        watts=cur_watts,
                                        segment_idx=max(segment_idx, 0),
                                    )
                                )
                    elif tag == "trk":
                        in_active_track = False

                elem.clear()
                if tag_stack:
                    tag_stack.pop()
                depth -= 1
    except DefusedXmlException as exc:
        raise GpxParseError(f"Blocked unsafe XML construct: {type(exc).__name__}") from exc
    except GpxParseError:
        raise
    except Exception as exc:
        raise GpxParseError(f"Malformed GPX XML: {type(exc).__name__}") from exc

    if not points:
        raise GpxParseError("GPX contains zero valid trackpoints")

    elev_present_count = sum(1 for pt in points if pt.elev_m is not None)
    has_elevation = elev_present_count > 0
    if elev_present_count == 0:
        warn("MISSING_ELEVATION", "All trackpoints lack elevation data")
    elif elev_present_count < len(points):
        warn("PARTIAL_ELEVATION", "Some trackpoints lack elevation data")

    has_timestamps = all(pt.ts_utc is not None for pt in points)
    is_distance_only = not has_timestamps
    if not has_timestamps:
        if synthesize_timestamps:
            points = _synthesize_uniform_timestamps(points, speed_mps=synthetic_speed_mps)
            warn(
                "SYNTHETIC_TIMESTAMPS",
                "Missing timestamps filled with synthetic uniform fallback; distance timeline used",
            )
        else:
            warn(
                "MISSING_TIMESTAMPS",
                "Some or all trackpoints lack timestamps; distance timeline used",
            )
    if non_monotonic_ts:
        warn("NON_MONOTONIC_TIME", "Trackpoint timestamps are not strictly monotonic")

    bounds, crosses_antimeridian = compute_unwrapped_bounds(points)
    segment_count = len({pt.segment_idx for pt in points})

    return ParsedTrack(
        name=track_name,
        activity_type_hint=track_type_hint,
        source_format="gpx",
        points=points,
        warnings=list(warnings.values()),
        point_count=len(points),
        segment_count=segment_count,
        has_timestamps=has_timestamps,
        has_elevation=has_elevation,
        is_distance_only=is_distance_only,
        crosses_antimeridian=crosses_antimeridian,
        bounds=bounds,
    )


def _subprocess_parse_worker(
    raw_bytes: bytes,
    synthesize_timestamps: bool,
    queue: multiprocessing.Queue[tuple[bool, object]],
    stitch_tracks: bool = False,
) -> None:
    try:
        parsed = parse_gpx_bytes(
            raw_bytes,
            synthesize_timestamps=synthesize_timestamps,
            stitch_tracks=stitch_tracks,
        )
        queue.put((True, parsed.model_dump(mode="python")))
    except Exception as exc:
        queue.put((False, str(exc)))


def parse_gpx_with_timeout(
    raw_bytes: bytes,
    timeout_seconds: float = PARSE_TIMEOUT_SECONDS,
    *,
    synthesize_timestamps: bool = False,
    stitch_tracks: bool = False,
) -> ParsedTrack:
    """Execute GPX parsing in an isolated child process with a strict wall-clock timeout."""
    if len(raw_bytes) > MAX_GPX_BYTES:
        raise GpxParseError(f"GPX payload exceeds maximum size of {MAX_GPX_BYTES} bytes")

    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[tuple[bool, object]] = ctx.Queue(maxsize=1)
    proc: SpawnProcess = ctx.Process(
        target=_subprocess_parse_worker,
        args=(raw_bytes, synthesize_timestamps, queue, stitch_tracks),
    )
    proc.start()

    try:
        # Drain queue before proc.join() so payloads >64 KiB cannot deadlock the pipe feeder.
        ok, payload = queue.get(timeout=timeout_seconds)
    except queue_mod.Empty as exc:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=1.0)
            raise GpxParseError(
                f"GPX parsing exceeded wall-clock limit of {timeout_seconds}s"
            ) from exc
        raise GpxParseError("GPX parser process terminated without output") from exc
    finally:
        queue.close()
        queue.join_thread()

    proc.join(timeout=2.0)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=1.0)

    if not ok:
        raise GpxParseError(str(payload))
    return ParsedTrack.model_validate(payload)
