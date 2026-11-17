"""Unified multi-format track parser registry for GPX, Garmin/Wahoo FIT, and TCX."""

from __future__ import annotations

import io
import math
import multiprocessing
import queue as queue_mod
from datetime import UTC, datetime
from multiprocessing.context import SpawnProcess
from typing import Any, Literal

import defusedxml.ElementTree as DefusedET
import fitdecode  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException

from trip_telemetry.gpx_parser import (
    DEFAULT_SYNTHETIC_SPEED_MPS,
    MAX_GPX_BYTES,
    MAX_POINTS,
    MAX_SEGMENTS,
    MAX_XML_DEPTH,
    PARSE_TIMEOUT_SECONDS,
    GpxParseError,
    _local_name,
    _parse_optional_float,
    _parse_optional_int,
    _parse_timestamp,
    _synthesize_uniform_timestamps,
    compute_unwrapped_bounds,
    haversine_distance_m,
    parse_gpx_bytes,
)
from trip_telemetry.schemas import IngestWarning, ParsedTrack, TrackPoint

TrackFormat = Literal["gpx", "fit", "tcx"]
MAX_TRACK_BYTES = MAX_GPX_BYTES
_SEMICIRCLES_TO_DEG = 180.0 / float(1 << 31)


class TrackParseError(GpxParseError):
    """Raised when GPX, FIT, or TCX input violates security, schema, or resource bounds."""


def has_fit_magic_bytes(raw_bytes: bytes) -> bool:
    """Return True if raw_bytes matches the Garmin/ANT+ FIT binary header signature."""
    return len(raw_bytes) >= 12 and raw_bytes[0] in (12, 14) and raw_bytes[8:12] == b".FIT"


def detect_track_format(
    raw_bytes: bytes,
    *,
    format_hint: str | None = None,
    filename: str | None = None,
) -> TrackFormat:
    """Detect whether raw_bytes represents a FIT, TCX, or GPX track payload."""
    if not raw_bytes:
        raise TrackParseError("Track payload is empty")
    if len(raw_bytes) > MAX_TRACK_BYTES:
        raise TrackParseError(f"Track payload exceeds maximum size of {MAX_TRACK_BYTES} bytes")

    if has_fit_magic_bytes(raw_bytes):
        return "fit"

    trimmed = raw_bytes.lstrip(b"\xef\xbb\xbf \t\r\n")
    head = trimmed[:4096].lower()
    if b"<trainingcenterdatabase" in head:
        return "tcx"
    if b"<gpx" in head:
        return "gpx"

    if format_hint:
        hint = format_hint.strip().lower().lstrip(".")
        if hint in ("gpx", "fit", "tcx"):
            return hint  # type: ignore[return-value]

    if filename:
        lower_name = filename.strip().lower()
        if lower_name.endswith(".tcx") and head.startswith(b"<?xml"):
            return "tcx"
        if lower_name.endswith(".gpx") and head.startswith(b"<?xml"):
            return "gpx"

    raise TrackParseError("Uploaded file is not a supported GPX, FIT, or TCX track")


def _fit_coord_to_deg(field: Any) -> float | None:
    if field is None or field.value is None:
        return None
    try:
        raw = float(field.value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw):
        return None
    units = str(getattr(field, "units", "") or "").lower()
    if units == "semicircles" or abs(raw) > 360.0:
        return raw * _SEMICIRCLES_TO_DEG
    return raw


def _fit_optional_float(msg: Any, *names: str) -> float | None:
    for name in names:
        if msg.has_field(name):
            val = msg.get_value(name, fallback=None)
            if val is not None and not isinstance(val, bool):
                try:
                    num = float(val)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(num):
                    return num
    return None


def _fit_optional_int(msg: Any, *names: str) -> int | None:
    val = _fit_optional_float(msg, *names)
    if val is None:
        return None
    return int(round(val))


def parse_fit_bytes(
    raw_bytes: bytes,
    *,
    synthesize_timestamps: bool = False,
    synthetic_speed_mps: float = DEFAULT_SYNTHETIC_SPEED_MPS,
) -> ParsedTrack:
    """Parse a binary Garmin/ANT+ FIT file into a normalized ParsedTrack."""
    if not raw_bytes:
        raise TrackParseError("FIT payload is empty")
    if len(raw_bytes) > MAX_TRACK_BYTES:
        raise TrackParseError(f"FIT payload exceeds maximum size of {MAX_TRACK_BYTES} bytes")
    if not has_fit_magic_bytes(raw_bytes):
        raise TrackParseError("Payload does not have a valid FIT header signature")

    points: list[TrackPoint] = []
    warnings: dict[str, IngestWarning] = {}

    def warn(code: str, message: str) -> None:
        if code in warnings:
            warnings[code].count += 1
        else:
            warnings[code] = IngestWarning(code=code, message=message, count=1)

    track_name = ""
    sport_parts: list[str] = []
    segment_idx = 0
    needs_new_segment = False
    prev_lat_lon: tuple[float, float] | None = None
    prev_ts: datetime | None = None
    non_monotonic_ts = False

    try:
        with fitdecode.FitReader(
            io.BytesIO(raw_bytes),
            check_crc=fitdecode.CrcCheck.ENABLED,
            error_handling=fitdecode.ErrorHandling.RAISE,
        ) as reader:
            for frame in reader:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                msg_name = frame.name
                if msg_name in ("sport", "session"):
                    sport_val = frame.get_value("sport", fallback=None)
                    sub_sport = frame.get_value("sub_sport", fallback=None)
                    if isinstance(sport_val, str) and sport_val.strip():
                        sport_parts.append(sport_val.strip())
                    if (
                        isinstance(sub_sport, str)
                        and sub_sport.strip()
                        and sub_sport.strip().lower() != "generic"
                    ):
                        sport_parts.append(sub_sport.strip())
                    if not track_name:
                        raw_title = frame.get_value("name", fallback=None)
                        if isinstance(raw_title, str) and raw_title.strip():
                            track_name = raw_title.strip()[:200]
                elif msg_name == "workout" and not track_name:
                    wkt_name = frame.get_value("wkt_name", fallback=None)
                    if isinstance(wkt_name, str) and wkt_name.strip():
                        track_name = wkt_name.strip()[:200]
                elif msg_name == "event":
                    ev = str(frame.get_value("event", fallback="") or "").lower()
                    ev_type = str(frame.get_value("event_type", fallback="") or "").lower()
                    if ev == "timer" and ev_type in ("stop", "stop_all") and points:
                        needs_new_segment = True
                elif msg_name == "record":
                    lat_field = (
                        frame.get_field("position_lat") if frame.has_field("position_lat") else None
                    )
                    lon_field = (
                        frame.get_field("position_long")
                        if frame.has_field("position_long")
                        else None
                    )
                    if lat_field is None and lon_field is None:
                        continue
                    lat = _fit_coord_to_deg(lat_field)
                    lon = _fit_coord_to_deg(lon_field)
                    if (
                        lat is None
                        or lon is None
                        or not (-90.0 <= lat <= 90.0)
                        or not (-180.0 <= lon <= 180.0)
                    ):
                        warn("INVALID_COORD", "Dropped FIT record with invalid coordinates")
                        continue

                    if needs_new_segment:
                        segment_idx += 1
                        if segment_idx >= MAX_SEGMENTS:
                            raise TrackParseError(f"Segment count exceeded {MAX_SEGMENTS}")
                        prev_lat_lon = None
                        needs_new_segment = False

                    if prev_lat_lon == (lat, lon):
                        warn("DUPLICATE_POINT", "Collapsed consecutive duplicate coordinate")
                        continue

                    if len(points) >= MAX_POINTS:
                        raise TrackParseError(f"Point count exceeded {MAX_POINTS}")

                    ts_raw = frame.get_value("timestamp", fallback=None)
                    ts_utc: datetime | None = None
                    if isinstance(ts_raw, datetime):
                        ts_utc = (
                            ts_raw.replace(tzinfo=UTC)
                            if ts_raw.tzinfo is None
                            else ts_raw.astimezone(UTC)
                        )
                    if ts_utc is not None and prev_ts is not None and ts_utc < prev_ts:
                        non_monotonic_ts = True
                    if ts_utc is not None:
                        prev_ts = ts_utc

                    elev_m = _fit_optional_float(frame, "enhanced_altitude", "altitude")
                    hr_raw = _fit_optional_int(frame, "heart_rate")
                    hr_bpm = hr_raw if (hr_raw is not None and 0 <= hr_raw <= 260) else None
                    cad_raw = _fit_optional_int(frame, "cadence")
                    cadence = cad_raw if (cad_raw is not None and 0 <= cad_raw <= 300) else None
                    watts_raw = _fit_optional_int(frame, "power")
                    watts = (
                        watts_raw if (watts_raw is not None and 0 <= watts_raw <= 3000) else None
                    )
                    temp_raw = _fit_optional_float(frame, "temperature")
                    temp_c = (
                        temp_raw if (temp_raw is not None and -80.0 <= temp_raw <= 80.0) else None
                    )
                    speed_raw = _fit_optional_float(frame, "enhanced_speed", "speed")
                    vel_smooth = speed_raw if (speed_raw is not None and speed_raw >= 0.0) else None

                    prev_lat_lon = (lat, lon)
                    points.append(
                        TrackPoint(
                            lat=lat,
                            lon=lon,
                            elev_m=elev_m,
                            ts_utc=ts_utc,
                            hr_bpm=hr_bpm,
                            cadence=cadence,
                            watts=watts,
                            temp_c=temp_c,
                            velocity_smooth=vel_smooth,
                            segment_idx=segment_idx,
                        )
                    )
    except TrackParseError:
        raise
    except Exception as exc:
        raise TrackParseError(f"Malformed FIT binary payload: {type(exc).__name__}") from exc

    if not points:
        raise TrackParseError("FIT file contains zero valid GPS trackpoints")

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
    activity_hint = " ".join(dict.fromkeys(sport_parts))[:64]

    return ParsedTrack(
        name=track_name,
        activity_type_hint=activity_hint,
        source_format="fit",
        points=points,
        warnings=list(warnings.values()),
        point_count=len(points),
        segment_count=segment_count,
        has_timestamps=has_timestamps,
        has_elevation=has_elevation,
        is_distance_only=is_distance_only,
        crosses_antimeridian=crosses_antimeridian,
        bounds=bounds,
        velocity_smooth=[p.velocity_smooth for p in points],
    )


def parse_tcx_bytes(
    raw_bytes: bytes,
    *,
    synthesize_timestamps: bool = False,
    synthetic_speed_mps: float = DEFAULT_SYNTHETIC_SPEED_MPS,
) -> ParsedTrack:
    """Parse a Garmin Training Center XML (TCX) payload safely using defusedxml."""
    if not raw_bytes:
        raise TrackParseError("TCX payload is empty")
    if len(raw_bytes) > MAX_TRACK_BYTES:
        raise TrackParseError(f"TCX payload exceeds maximum size of {MAX_TRACK_BYTES} bytes")

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
    activity_count = 0
    track_name = ""
    activity_hint = ""
    segment_idx = 0
    pending_track_break = False
    force_activity_break = False

    in_trackpoint = False
    cur_lat: float | None = None
    cur_lon: float | None = None
    cur_ele: float | None = None
    cur_ts: datetime | None = None
    cur_hr: int | None = None
    cur_cad: int | None = None
    cur_watts: int | None = None
    cur_speed: float | None = None
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
                    raise TrackParseError(f"XML nesting depth exceeded {MAX_XML_DEPTH}")
                tag_stack.append(tag)

                if tag == "activity":
                    activity_count += 1
                    if not activity_hint:
                        sport_attr = elem.attrib.get("Sport") or elem.attrib.get("sport") or ""
                        activity_hint = sport_attr.strip()[:64]
                    if activity_count > 1 and points:
                        force_activity_break = True
                elif tag == "track":
                    if points:
                        pending_track_break = True
                elif tag == "trackpoint":
                    in_trackpoint = True
                    cur_lat = None
                    cur_lon = None
                    cur_ele = None
                    cur_ts = None
                    cur_hr = None
                    cur_cad = None
                    cur_watts = None
                    cur_speed = None
            else:
                parent_tag = tag_stack[-2] if len(tag_stack) >= 2 else ""
                if not in_trackpoint:
                    if (
                        tag == "notes"
                        and parent_tag == "activity"
                        and elem.text
                        and elem.text.strip()
                    ):
                        track_name = elem.text.strip()[:200]
                    elif tag == "id" and parent_tag == "activity" and not track_name and elem.text:
                        track_name = elem.text.strip()[:200]
                else:
                    if tag == "latitudedegrees":
                        cur_lat = _parse_optional_float(elem.text)
                    elif tag == "longitudedegrees":
                        cur_lon = _parse_optional_float(elem.text)
                    elif tag == "altitudemeters":
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
                    elif tag == "value" and parent_tag == "heartratebpm":
                        val = _parse_optional_int(elem.text)
                        if val is not None and 0 <= val <= 260:
                            cur_hr = val
                    elif tag in ("cadence", "runcadence"):
                        val = _parse_optional_int(elem.text)
                        if val is not None and 0 <= val <= 300:
                            cur_cad = val
                    elif tag in ("watts", "power"):
                        val = _parse_optional_int(elem.text)
                        if val is not None and 0 <= val <= 3000:
                            cur_watts = val
                    elif tag == "speed":
                        spd = _parse_optional_float(elem.text)
                        if spd is not None and spd >= 0.0:
                            cur_speed = spd
                    elif tag == "trackpoint":
                        in_trackpoint = False
                        if cur_lat is None and cur_lon is None:
                            pass
                        elif (
                            cur_lat is None
                            or cur_lon is None
                            or not (-90.0 <= cur_lat <= 90.0)
                            or not (-180.0 <= cur_lon <= 180.0)
                        ):
                            warn("INVALID_COORD", "Dropped trackpoint with invalid coordinates")
                        elif prev_lat_lon == (cur_lat, cur_lon) and not force_activity_break:
                            warn("DUPLICATE_POINT", "Collapsed consecutive duplicate coordinate")
                        else:
                            if len(points) >= MAX_POINTS:
                                raise TrackParseError(f"Point count exceeded {MAX_POINTS}")

                            if points and (force_activity_break or pending_track_break):
                                last_pt = points[-1]
                                gap_dist = haversine_distance_m(
                                    last_pt.lat, last_pt.lon, cur_lat, cur_lon
                                )
                                gap_time = (
                                    (cur_ts - last_pt.ts_utc).total_seconds()
                                    if (cur_ts is not None and last_pt.ts_utc is not None)
                                    else 0.0
                                )
                                if force_activity_break or gap_dist >= 200.0 or gap_time >= 120.0:
                                    segment_idx += 1
                                    if segment_idx >= MAX_SEGMENTS:
                                        raise TrackParseError(
                                            f"Segment count exceeded {MAX_SEGMENTS}"
                                        )
                            force_activity_break = False
                            pending_track_break = False

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
                                    velocity_smooth=cur_speed,
                                    segment_idx=segment_idx,
                                )
                            )

                elem.clear()
                if tag_stack:
                    tag_stack.pop()
                depth -= 1
    except DefusedXmlException as exc:
        raise TrackParseError(f"Blocked unsafe XML construct: {type(exc).__name__}") from exc
    except TrackParseError:
        raise
    except Exception as exc:
        raise TrackParseError(f"Malformed TCX XML: {type(exc).__name__}") from exc

    if not points:
        raise TrackParseError("TCX contains zero valid trackpoints")

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
        activity_type_hint=activity_hint,
        source_format="tcx",
        points=points,
        warnings=list(warnings.values()),
        point_count=len(points),
        segment_count=segment_count,
        has_timestamps=has_timestamps,
        has_elevation=has_elevation,
        is_distance_only=is_distance_only,
        crosses_antimeridian=crosses_antimeridian,
        bounds=bounds,
        velocity_smooth=[p.velocity_smooth for p in points],
    )


def parse_track_bytes(
    raw_bytes: bytes,
    *,
    format_hint: str | None = None,
    filename: str | None = None,
    stitch_tracks: bool = True,
    synthesize_timestamps: bool = False,
    synthetic_speed_mps: float = DEFAULT_SYNTHETIC_SPEED_MPS,
) -> ParsedTrack:
    """Detect track format (GPX, FIT, TCX) and parse into a normalized ParsedTrack."""
    fmt = detect_track_format(raw_bytes, format_hint=format_hint, filename=filename)
    if fmt == "fit":
        return parse_fit_bytes(
            raw_bytes,
            synthesize_timestamps=synthesize_timestamps,
            synthetic_speed_mps=synthetic_speed_mps,
        )
    if fmt == "tcx":
        return parse_tcx_bytes(
            raw_bytes,
            synthesize_timestamps=synthesize_timestamps,
            synthetic_speed_mps=synthetic_speed_mps,
        )
    return parse_gpx_bytes(
        raw_bytes,
        synthesize_timestamps=synthesize_timestamps,
        synthetic_speed_mps=synthetic_speed_mps,
        stitch_tracks=stitch_tracks,
    )


def _subprocess_track_worker(
    raw_bytes: bytes,
    format_hint: str | None,
    filename: str | None,
    stitch_tracks: bool,
    synthesize_timestamps: bool,
    queue: multiprocessing.Queue[tuple[bool, object]],
) -> None:
    try:
        parsed = parse_track_bytes(
            raw_bytes,
            format_hint=format_hint,
            filename=filename,
            stitch_tracks=stitch_tracks,
            synthesize_timestamps=synthesize_timestamps,
        )
        queue.put((True, parsed.model_dump(mode="python")))
    except Exception as exc:
        queue.put((False, str(exc)))


def parse_track_with_timeout(
    raw_bytes: bytes,
    timeout_seconds: float = PARSE_TIMEOUT_SECONDS,
    *,
    format_hint: str | None = None,
    filename: str | None = None,
    stitch_tracks: bool = True,
    synthesize_timestamps: bool = False,
) -> ParsedTrack:
    """Execute GPX/FIT/TCX parsing in an isolated child process with a wall-clock timeout."""
    if not raw_bytes:
        raise TrackParseError("Track payload is empty")
    if len(raw_bytes) > MAX_TRACK_BYTES:
        raise TrackParseError(f"Track payload exceeds maximum size of {MAX_TRACK_BYTES} bytes")

    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue[tuple[bool, object]] = ctx.Queue(maxsize=1)
    proc: SpawnProcess = ctx.Process(
        target=_subprocess_track_worker,
        args=(raw_bytes, format_hint, filename, stitch_tracks, synthesize_timestamps, queue),
    )
    proc.start()

    try:
        ok, payload = queue.get(timeout=timeout_seconds)
    except queue_mod.Empty as exc:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=1.0)
            raise TrackParseError(
                f"Track parsing exceeded wall-clock limit of {timeout_seconds}s"
            ) from exc
        raise TrackParseError("Track parser process terminated without output") from exc
    finally:
        queue.close()
        queue.join_thread()

    proc.join(timeout=2.0)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=1.0)

    if not ok:
        raise TrackParseError(str(payload))
    return ParsedTrack.model_validate(payload)
