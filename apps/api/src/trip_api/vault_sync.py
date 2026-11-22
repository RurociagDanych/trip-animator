"""Resumable Strava Activity Vault sync engine, Z14 tile indexing, and Stage mirroring."""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import httpx
import polars as pl
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from trip_telemetry import (
    DEFAULT_ACTIVITY_TYPE,
    ParsedTrack,
    infer_activity_type_from_strava,
    normalize_activity_type,
    simplify_coord_tuples_m,
)
from trip_telemetry.tiles import (
    compute_chronological_new_tile_counts,
    decode_polyline,
    encode_polyline,
    rasterize_polyline_tiles,
    rasterize_track_segments,
    tile_key,
)

from trip_api.config import Settings
from trip_api.db import (
    OAuthConnection,
    Project,
    Stage,
    User,
    VaultActivity,
    VaultActivityTile,
    VaultGear,
    VaultSyncState,
)
from trip_api.deps import ensure_utc
from trip_api.schemas import (
    VaultRateLimitOut,
    VaultSyncMode,
    VaultSyncPhase,
    VaultSyncStatus,
    VaultSyncStatusOut,
)
from trip_api.security import (
    SsrfBlockedError,
    decrypt_secret,
    encrypt_secret,
    validate_outbound_url,
)
from trip_api.services import materialize_silver_parquet
from trip_api.storage import ObjectStore

logger = logging.getLogger(__name__)

VAULT_SYNC_PAGE_SIZE = 200
VAULT_SYNC_LEASE_SECONDS = 180
VAULT_SYNC_MAX_RETRIES = 5
VAULT_SYNC_INLINE_INCREMENTAL_MAX_PAGES = 2
VAULT_SYNC_STAGE_POLYLINE_MAX_POINTS = 300
VAULT_EXPLORER_ZOOM = 14

_INDOOR_VIRTUAL_SPORT_TYPES: frozenset[str] = frozenset(
    {
        "virtualride",
        "virtualrun",
        "virtualrow",
        "weighttraining",
        "workout",
        "yoga",
        "crossfit",
        "elliptical",
        "stairstepper",
        "pilates",
        "hiit",
        "tabletennis",
        "badminton",
        "squash",
        "racquetball",
    }
)


class VaultSyncUpstreamError(RuntimeError):
    """Raised when Strava returns an unexpected error or redirect during Vault sync."""


class VaultSyncTransientError(VaultSyncUpstreamError):
    """A provider or transport failure eligible for bounded durable retries."""


@dataclass(frozen=True, slots=True)
class RateLimitInfo:
    short_used: int | None = None
    short_limit: int | None = None
    daily_used: int | None = None
    daily_limit: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedStageVaultGeometry:
    """Pure CPU-computed polyline, Z14 tiles, bounds, and telemetry stats for a Stage."""

    summary_polyline: str | None
    tiles_z14: frozenset[tuple[int, int]]
    start_lat: float | None = None
    start_lon: float | None = None
    end_lat: float | None = None
    end_lon: float | None = None
    min_lat: float | None = None
    max_lat: float | None = None
    min_lon: float | None = None
    max_lon: float | None = None
    elev_high_m: float | None = None
    elev_low_m: float | None = None
    avg_hr_bpm: float | None = None
    max_hr_bpm: float | None = None
    avg_watts: float | None = None


@dataclass(frozen=True, slots=True)
class UpsertActivityResult:
    vault_activity: VaultActivity
    created: bool
    has_gps: bool
    tiles_computed: bool


def _oauth_aad(user_id: uuid.UUID) -> bytes:
    return f"oauth:{user_id}:strava".encode()


def _default_http_client() -> httpx.Client:
    return httpx.Client(timeout=20.0, follow_redirects=False)


def _opt_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return fval if math.isfinite(fval) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            fval = float(text)
            return fval if math.isfinite(fval) else None
        except ValueError:
            return None
    return None


def _opt_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _opt_str(value: object, max_len: int | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len] if max_len is not None else cleaned


def _parse_latlng_pair(raw: object) -> tuple[float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    lat = _opt_float(raw[0])
    lon = _opt_float(raw[1])
    if lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


def parse_filter_epoch(raw_value: str, *, end_of_day: bool) -> int:
    """Parse YYYY-MM-DD, ISO-8601 timestamp, or UNIX epoch seconds into non-negative epoch."""
    cleaned = raw_value.strip()
    if not cleaned:
        raise ValueError("Empty date filter value")
    if cleaned.isascii() and cleaned.isdigit():
        epoch = int(cleaned)
    elif len(cleaned) == 10:
        parsed_date = date.fromisoformat(cleaned)
        boundary = time(23, 59, 59, tzinfo=UTC) if end_of_day else time(0, 0, 0, tzinfo=UTC)
        epoch = int(datetime.combine(parsed_date, boundary).timestamp())
    else:
        normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        epoch = int(dt.astimezone(UTC).timestamp())
    if epoch < 0:
        raise ValueError("Date filter timestamp must be non-negative")
    return epoch


def parse_strava_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitInfo:
    """Parse Strava X-ReadRateLimit-* (preferred) or X-RateLimit-* headers."""
    lower = {k.lower(): v for k, v in headers.items()}
    usage_raw = lower.get("x-readratelimit-usage") or lower.get("x-ratelimit-usage")
    limit_raw = lower.get("x-readratelimit-limit") or lower.get("x-ratelimit-limit")
    if not usage_raw or not limit_raw:
        return RateLimitInfo()
    try:
        u_short, u_daily = (int(part.strip()) for part in usage_raw.split(",", 1))
        l_short, l_daily = (int(part.strip()) for part in limit_raw.split(",", 1))
        return RateLimitInfo(
            short_used=u_short,
            short_limit=l_short,
            daily_used=u_daily,
            daily_limit=l_daily,
        )
    except (ValueError, TypeError):
        return RateLimitInfo()


def compute_rate_limit_pause(
    headers: Mapping[str, str],
    status_code: int,
    now: datetime,
) -> tuple[bool, datetime | None, RateLimitInfo]:
    """Return `(should_pause, next_retry_at, rate_limit_info)` for a Strava HTTP response."""
    now_utc = ensure_utc(now)
    rl = parse_strava_rate_limit_headers(headers)

    daily_hit = (
        rl.daily_limit is not None
        and rl.daily_limit > 0
        and rl.daily_used is not None
        and rl.daily_used >= 0.95 * rl.daily_limit
    )
    short_hit = (
        rl.short_limit is not None
        and rl.short_limit > 0
        and rl.short_used is not None
        and rl.short_used >= 0.90 * rl.short_limit
    )

    if daily_hit:
        next_day = (now_utc + timedelta(days=1)).date()
        next_retry = datetime(next_day.year, next_day.month, next_day.day, 0, 1, 0, tzinfo=UTC)
        return True, next_retry, rl

    if status_code == 429 or short_hit:
        next_quarter = ((now_utc.minute // 15) + 1) * 15
        hour_floor = now_utc.replace(minute=0, second=0, microsecond=0)
        next_retry = hour_floor + timedelta(minutes=next_quarter, seconds=5)
        return True, next_retry, rl

    return False, None, rl


def _apply_rate_limit_info(state: VaultSyncState, rl: RateLimitInfo) -> None:
    if rl.short_used is not None:
        state.rate_limit_short_used = rl.short_used
    if rl.short_limit is not None:
        state.rate_limit_short_limit = rl.short_limit
    if rl.daily_used is not None:
        state.rate_limit_daily_used = rl.daily_used
    if rl.daily_limit is not None:
        state.rate_limit_daily_limit = rl.daily_limit


def _strava_vault_request(
    client_factory: Callable[[], httpx.Client],
    settings: Settings,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an SSRF-validated, redirect-disabled Strava HTTP request."""
    base = settings.strava_api_base_url.rstrip("/")
    url = f"{base}{path}"
    validate_outbound_url(
        url,
        allowed_hosts=settings.strava_allowed_hosts,
        resolve_dns=True,
    )
    with client_factory() as client:
        try:
            resp = client.request(method, url, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("Strava Vault HTTP request failed: %s", type(exc).__name__)
            raise VaultSyncTransientError("Strava upstream request failed") from exc

    if resp.status_code == 408 or 500 <= resp.status_code <= 599:
        raise VaultSyncTransientError(f"Strava upstream HTTP {resp.status_code}")
    if 300 <= resp.status_code < 400:
        raise VaultSyncUpstreamError("Unexpected redirect from Strava upstream")
    return resp


def _get_valid_access_token_unlocked(
    *,
    db: Session,
    conn: OAuthConnection,
    settings: Settings,
    client_factory: Callable[[], httpx.Client],
    now: datetime,
) -> str:
    """Return a valid Strava access token without holding Postgres locks across HTTP I/O."""
    key = settings.decoded_encryption_key
    aad = _oauth_aad(conn.user_id)
    now_utc = ensure_utc(now)

    if ensure_utc(conn.expires_at) > now_utc + timedelta(seconds=60):
        return decrypt_secret(conn.access_token_enc, key, aad=aad)

    if not settings.strava_client_id or not settings.strava_client_secret:
        raise VaultSyncUpstreamError("Strava client credentials are not configured")

    refresh_token = decrypt_secret(conn.refresh_token_enc, key, aad=aad)
    resp = _strava_vault_request(
        client_factory,
        settings,
        "POST",
        "/oauth/token",
        data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    if resp.status_code != 200:
        raise VaultSyncUpstreamError(
            f"Failed to refresh Strava access token (status={resp.status_code})"
        )

    payload = resp.json()
    if (
        not isinstance(payload, dict)
        or "access_token" not in payload
        or "expires_at" not in payload
    ):
        raise VaultSyncUpstreamError("Malformed Strava token refresh response")

    new_access = str(payload["access_token"])
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    expires_epoch = int(payload["expires_at"])

    refreshed_conn = db.get(OAuthConnection, conn.id)
    if refreshed_conn is not None:
        refreshed_conn.access_token_enc = encrypt_secret(new_access, key, aad=aad)
        refreshed_conn.refresh_token_enc = encrypt_secret(new_refresh, key, aad=aad)
        refreshed_conn.expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC)
        db.commit()
    return new_access


def parse_strava_timestamps(
    item: Mapping[str, Any],
    *,
    fallback_now: datetime,
) -> tuple[datetime, datetime | None, date, str | None, int]:
    """Parse Strava UTC start_date and fake-Z start_date_local into exact temporal columns."""
    started_at: datetime | None = None
    raw_utc = _opt_str(item.get("start_date"))
    if raw_utc:
        try:
            normalized = raw_utc[:-1] + "+00:00" if raw_utc.endswith("Z") else raw_utc
            started_at = ensure_utc(datetime.fromisoformat(normalized))
        except ValueError:
            started_at = None
    if started_at is None:
        started_at = ensure_utc(fallback_now)

    started_at_local: datetime | None = None
    raw_local = _opt_str(item.get("start_date_local"))
    if raw_local:
        cleaned_local = raw_local[:-1] if raw_local.endswith("Z") else raw_local
        try:
            started_at_local = datetime.fromisoformat(cleaned_local).replace(tzinfo=None)
        except ValueError:
            started_at_local = None

    utc_offset_s = _opt_int(item.get("utc_offset")) or 0
    if started_at_local is None and utc_offset_s != 0:
        started_at_local = (started_at + timedelta(seconds=utc_offset_s)).replace(tzinfo=None)

    local_date = started_at_local.date() if started_at_local is not None else started_at.date()
    timezone_name = _opt_str(item.get("timezone"), 64)
    return started_at, started_at_local, local_date, timezone_name, utc_offset_s


def _replace_activity_z14_tiles(
    db: Session,
    *,
    vault_activity_id: uuid.UUID,
    user_id: uuid.UUID,
    tiles: Sequence[tuple[int, int]] | set[tuple[int, int]] | frozenset[tuple[int, int]],
) -> int:
    db.execute(
        delete(VaultActivityTile).where(
            VaultActivityTile.vault_activity_id == vault_activity_id,
            VaultActivityTile.zoom == VAULT_EXPLORER_ZOOM,
        )
    )
    unique_tiles = sorted(set(tiles))
    if unique_tiles:
        db.add_all(
            [
                VaultActivityTile(
                    vault_activity_id=vault_activity_id,
                    zoom=VAULT_EXPLORER_ZOOM,
                    tile_x=tx,
                    tile_y=ty,
                    user_id=user_id,
                    tile_key=tile_key(VAULT_EXPLORER_ZOOM, tx, ty),
                )
                for tx, ty in unique_tiles
            ]
        )
    return len(unique_tiles)


def recompute_user_chronological_new_tiles(
    db: Session,
    user_id: uuid.UUID,
) -> int:
    """Recompute chronological first-visit `new_tiles_z14_count` ordered by (started_at, id)."""
    db.flush()
    activities = list(
        db.scalars(
            select(VaultActivity)
            .where(VaultActivity.user_id == user_id)
            .order_by(VaultActivity.started_at.asc(), VaultActivity.id.asc())
        ).all()
    )
    if not activities:
        return 0

    tile_rows = db.execute(
        select(VaultActivityTile.vault_activity_id, VaultActivityTile.tile_key).where(
            VaultActivityTile.user_id == user_id,
            VaultActivityTile.zoom == VAULT_EXPLORER_ZOOM,
        )
    ).all()
    tiles_by_activity: dict[uuid.UUID, list[int]] = {}
    for act_id, t_key in tile_rows:
        tiles_by_activity.setdefault(act_id, []).append(int(t_key))

    ordered_input = [(act.id, tiles_by_activity.get(act.id, ())) for act in activities]
    counts = compute_chronological_new_tile_counts(ordered_input)

    updated = 0
    for act in activities:
        next_count = int(counts.get(act.id, 0))
        if act.new_tiles_z14_count != next_count:
            act.new_tiles_z14_count = next_count
            updated += 1
    db.flush()
    return updated


def get_or_create_vault_sync_state(
    db: Session,
    user_id: uuid.UUID,
    *,
    provider: str = "strava",
) -> VaultSyncState:
    state = db.scalar(
        select(VaultSyncState).where(
            VaultSyncState.user_id == user_id,
            VaultSyncState.provider == provider,
        )
    )
    if state is not None:
        return state
    state = VaultSyncState(user_id=user_id, provider=provider)
    db.add(state)
    db.flush()
    return state


def bump_user_vault_version(db: Session, user_id: uuid.UUID, *, provider: str = "strava") -> int:
    """Increment the user's VaultSyncState.vault_version and return the new version."""
    state = get_or_create_vault_sync_state(db, user_id, provider=provider)
    state.vault_version = int(state.vault_version or 1) + 1
    db.flush()
    return state.vault_version


def upsert_strava_gears(
    db: Session,
    *,
    user_id: uuid.UUID,
    athlete_payload: Mapping[str, Any],
    now: datetime,
) -> dict[str, str]:
    """Upsert bikes and shoes from Strava `/api/v3/athlete` into `vault_gears`."""
    existing_rows = list(
        db.scalars(
            select(VaultGear).where(
                VaultGear.user_id == user_id,
                VaultGear.provider == "strava",
            )
        ).all()
    )
    by_id = {row.gear_id: row for row in existing_rows}
    names_by_id: dict[str, str] = {row.gear_id: row.name for row in existing_rows}

    for key_name, gear_type in (("bikes", "bike"), ("shoes", "shoe")):
        raw_items = athlete_payload.get(key_name)
        if not isinstance(raw_items, list):
            continue
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            gear_id = _opt_str(entry.get("id"), 64)
            gear_name = _opt_str(entry.get("name"), 160)
            if not gear_id or not gear_name:
                continue
            distance_m = _opt_float(entry.get("distance")) or 0.0
            is_primary = bool(entry.get("primary"))
            existing = by_id.get(gear_id)
            if existing is None:
                row = VaultGear(
                    user_id=user_id,
                    provider="strava",
                    gear_id=gear_id,
                    name=gear_name,
                    gear_type=gear_type,
                    distance_m=distance_m,
                    is_primary=is_primary,
                    updated_at=ensure_utc(now),
                )
                db.add(row)
                by_id[gear_id] = row
            else:
                existing.name = gear_name
                existing.gear_type = gear_type
                existing.distance_m = distance_m
                existing.is_primary = is_primary
                existing.updated_at = ensure_utc(now)
            names_by_id[gear_id] = gear_name
    db.flush()
    return names_by_id


def upsert_strava_activity_to_vault(
    db: Session,
    *,
    user_id: uuid.UUID,
    item: Mapping[str, Any],
    now: datetime,
    gear_names_by_id: Mapping[str, str] | None = None,
    existing_by_ext_id: Mapping[str, VaultActivity] | None = None,
) -> UpsertActivityResult:
    """Upsert a single Strava Summary/Detail activity into `vault_activities` and Z14 tiles."""
    raw_id = item.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        raise ValueError("Strava activity missing valid integer id")

    strava_id = int(raw_id)
    ext_id = str(strava_id)
    now_utc = ensure_utc(now)

    name = _opt_str(item.get("name"), 200) or f"Strava {strava_id}"
    raw_sport = _opt_str(item.get("sport_type") or item.get("type"), 64) or ""
    activity_type = infer_activity_type_from_strava(raw_sport or None, name)
    started_at, started_at_local, local_date, timezone_name, utc_offset_s = parse_strava_timestamps(
        item, fallback_now=now_utc
    )

    is_indoor_virtual = (
        bool(item.get("trainer")) or raw_sport.lower() in _INDOOR_VIRTUAL_SPORT_TYPES
    )
    is_commute = bool(item.get("commute"))
    is_private = bool(item.get("private"))

    raw_map = item.get("map")
    map_obj: Mapping[str, Any] = raw_map if isinstance(raw_map, Mapping) else {}
    summary_polyline = _opt_str(map_obj.get("summary_polyline") or map_obj.get("polyline"))
    start_pair = _parse_latlng_pair(item.get("start_latlng"))
    end_pair = _parse_latlng_pair(item.get("end_latlng"))

    decoded_points: list[tuple[float, float]] = []
    if summary_polyline and not is_indoor_virtual:
        try:
            decoded_points = decode_polyline(summary_polyline)
        except Exception:
            decoded_points = []

    has_gps = bool(
        start_pair is not None and summary_polyline and decoded_points and not is_indoor_virtual
    )

    min_lat: float | None = None
    max_lat: float | None = None
    min_lon: float | None = None
    max_lon: float | None = None
    start_lat: float | None = start_pair[0] if start_pair else None
    start_lon: float | None = start_pair[1] if start_pair else None
    end_lat: float | None = end_pair[0] if end_pair else None
    end_lon: float | None = end_pair[1] if end_pair else None

    if has_gps and decoded_points:
        lats = [pt[0] for pt in decoded_points]
        lons = [pt[1] for pt in decoded_points]
        min_lat = min(lats)
        max_lat = max(lats)
        min_lon = min(lons)
        max_lon = max(lons)
        if start_lat is None:
            start_lat, start_lon = decoded_points[0]
        if end_lat is None:
            end_lat, end_lon = decoded_points[-1]
    elif not has_gps:
        summary_polyline = None
        start_lat = None
        start_lon = None
        end_lat = None
        end_lon = None

    gear_id = _opt_str(item.get("gear_id"), 64)
    raw_gear = item.get("gear")
    gear_obj: Mapping[str, Any] = raw_gear if isinstance(raw_gear, Mapping) else {}
    gear_name = _opt_str(gear_obj.get("name"), 120)
    if gear_name is None and gear_id and gear_names_by_id is not None:
        gear_name = _opt_str(gear_names_by_id.get(gear_id), 120)
    if gear_name is None and gear_id:
        gear_row = db.get(VaultGear, (user_id, "strava", gear_id))
        if gear_row is not None:
            gear_name = gear_row.name[:120]

    existing: VaultActivity | None
    if existing_by_ext_id is not None:
        existing = existing_by_ext_id.get(ext_id)
        if existing is not None:
            db.refresh(existing, with_for_update=True)
    else:
        existing = db.scalar(
            select(VaultActivity)
            .where(
                VaultActivity.user_id == user_id,
                VaultActivity.source_provider == "strava",
                VaultActivity.external_activity_id == ext_id,
            )
            .with_for_update()
        )

    if existing is not None and existing.disconnected_at is not None:
        return UpsertActivityResult(existing, False, existing.has_gps, False)

    raw_meta: dict[str, object] = {}
    # Preserve measurement presence separately from legacy non-null columns.
    # Missing provider fields are unknown; an explicitly reported zero is known.
    for metric_key in ("distance", "total_elevation_gain", "moving_time", "elapsed_time"):
        metric_value = _opt_float(item.get(metric_key))
        if metric_value is not None:
            raw_meta[metric_key] = metric_value
    for meta_key in (
        "device_name",
        "achievement_count",
        "kudos_count",
        "pr_count",
        "trainer",
        "manual",
        "workout_type",
        "location_country",
        "location_city",
    ):
        if meta_key in item and item[meta_key] is not None:
            raw_meta[meta_key] = item[meta_key]

    primary_country = _opt_str(item.get("location_country"), 64)
    created = existing is None
    if existing is None:
        act = VaultActivity(
            user_id=user_id,
            source_provider="strava",
            external_activity_id=ext_id,
            strava_activity_id=strava_id,
            name=name,
            raw_sport_type=raw_sport,
            activity_type=activity_type,
            started_at=started_at,
            started_at_local=started_at_local,
            local_date=local_date,
            timezone_name=timezone_name,
            utc_offset_s=utc_offset_s,
            distance_m=_opt_float(item.get("distance")) or 0.0,
            elevation_gain_m=_opt_float(item.get("total_elevation_gain")) or 0.0,
            elev_high_m=_opt_float(item.get("elev_high")),
            elev_low_m=_opt_float(item.get("elev_low")),
            moving_seconds=_opt_float(item.get("moving_time")) or 0.0,
            elapsed_seconds=_opt_float(item.get("elapsed_time")) or 0.0,
            avg_speed_mps=_opt_float(item.get("average_speed")),
            max_speed_mps=_opt_float(item.get("max_speed")),
            avg_hr_bpm=_opt_float(item.get("average_heartrate")),
            max_hr_bpm=_opt_float(item.get("max_heartrate")),
            avg_watts=_opt_float(item.get("average_watts")),
            weighted_watts=_opt_float(item.get("weighted_average_watts")),
            kilojoules=_opt_float(item.get("kilojoules")),
            avg_cadence=_opt_float(item.get("average_cadence")),
            suffer_score=_opt_float(item.get("suffer_score")),
            gear_id=gear_id,
            gear_name=gear_name,
            is_commute=is_commute,
            is_indoor_virtual=is_indoor_virtual,
            is_private=is_private,
            has_gps=has_gps,
            start_lat=start_lat,
            start_lon=start_lon,
            end_lat=end_lat,
            end_lon=end_lon,
            min_lat=min_lat,
            max_lat=max_lat,
            min_lon=min_lon,
            max_lon=max_lon,
            summary_polyline=summary_polyline,
            tile_precision="none",
            tile_count_z14=0,
            new_tiles_z14_count=0,
            primary_country=primary_country,
            raw_metadata_json=raw_meta,
            synced_at=now_utc,
        )
        db.add(act)
        db.flush()
    else:
        act = existing
        act.strava_activity_id = strava_id
        act.name = name
        act.raw_sport_type = raw_sport
        act.activity_type = activity_type
        act.started_at = started_at
        act.started_at_local = started_at_local
        act.local_date = local_date
        act.timezone_name = timezone_name
        act.utc_offset_s = utc_offset_s
        act.distance_m = _opt_float(item.get("distance")) or 0.0
        act.elevation_gain_m = _opt_float(item.get("total_elevation_gain")) or 0.0
        act.elev_high_m = _opt_float(item.get("elev_high"))
        act.elev_low_m = _opt_float(item.get("elev_low"))
        act.moving_seconds = _opt_float(item.get("moving_time")) or 0.0
        act.elapsed_seconds = _opt_float(item.get("elapsed_time")) or 0.0
        act.avg_speed_mps = _opt_float(item.get("average_speed"))
        act.max_speed_mps = _opt_float(item.get("max_speed"))
        act.avg_hr_bpm = _opt_float(item.get("average_heartrate"))
        act.max_hr_bpm = _opt_float(item.get("max_heartrate"))
        act.avg_watts = _opt_float(item.get("average_watts"))
        act.weighted_watts = _opt_float(item.get("weighted_average_watts"))
        act.kilojoules = _opt_float(item.get("kilojoules"))
        act.avg_cadence = _opt_float(item.get("average_cadence"))
        act.suffer_score = _opt_float(item.get("suffer_score"))
        act.gear_id = gear_id
        if gear_name is not None:
            act.gear_name = gear_name
        act.is_commute = is_commute
        act.is_indoor_virtual = is_indoor_virtual
        act.is_private = is_private
        if primary_country is not None:
            act.primary_country = primary_country
        act.raw_metadata_json = raw_meta
        act.synced_at = now_utc

        if act.tile_precision != "full_stream":
            act.has_gps = has_gps
            act.start_lat = start_lat
            act.start_lon = start_lon
            act.end_lat = end_lat
            act.end_lon = end_lon
            act.min_lat = min_lat
            act.max_lat = max_lat
            act.min_lon = min_lon
            act.max_lon = max_lon
            act.summary_polyline = summary_polyline

    tiles_computed = False
    if act.tile_precision == "full_stream":
        # Never downgrade an activity already upgraded from full telemetry streams.
        tiles_computed = False
    elif has_gps and decoded_points:
        z14_tiles = rasterize_polyline_tiles(decoded_points, zoom=VAULT_EXPLORER_ZOOM)
        act.tile_count_z14 = _replace_activity_z14_tiles(
            db,
            vault_activity_id=act.id,
            user_id=user_id,
            tiles=z14_tiles,
        )
        act.tile_precision = "summary_polyline"
        tiles_computed = True
    else:
        if act.tile_count_z14 > 0:
            _replace_activity_z14_tiles(
                db,
                vault_activity_id=act.id,
                user_id=user_id,
                tiles=(),
            )
        act.tile_precision = "none"
        act.tile_count_z14 = 0

    unlinked_stage_ids = list(
        db.scalars(
            select(Stage.id)
            .join(Project, Stage.project_id == Project.id)
            .where(
                Project.owner_user_id == user_id,
                Stage.strava_activity_id == strava_id,
                Stage.vault_activity_id.is_(None),
            )
        ).all()
    )
    if unlinked_stage_ids:
        db.execute(
            update(Stage).where(Stage.id.in_(unlinked_stage_ids)).values(vault_activity_id=act.id)
        )

    db.flush()
    return UpsertActivityResult(
        vault_activity=act,
        created=created,
        has_gps=bool(act.has_gps),
        tiles_computed=tiles_computed,
    )


def delete_strava_vault_activity(
    db: Session,
    *,
    user_id: uuid.UUID,
    strava_activity_id: int,
) -> int:
    """Delete matching Strava VaultActivity rows, cascade tiles cleanly, and recompute new tiles."""
    matching = list(
        db.scalars(
            select(VaultActivity).where(
                VaultActivity.user_id == user_id,
                VaultActivity.strava_activity_id == strava_activity_id,
                VaultActivity.disconnected_at.is_(None),
            )
        ).all()
    )
    if not matching:
        return 0

    ids = [row.id for row in matching]
    db.execute(update(Stage).where(Stage.vault_activity_id.in_(ids)).values(vault_activity_id=None))
    db.execute(delete(VaultActivityTile).where(VaultActivityTile.vault_activity_id.in_(ids)))
    for row in matching:
        db.delete(row)
    db.flush()

    recompute_user_chronological_new_tiles(db, user_id)
    bump_user_vault_version(db, user_id)
    return len(ids)


def _extract_estimated_total(stats_payload: Mapping[str, Any]) -> int | None:
    total = 0
    found = False
    for section_key in ("all_ride_totals", "all_run_totals", "all_swim_totals"):
        section = stats_payload.get(section_key)
        if isinstance(section, Mapping):
            count_val = _opt_int(section.get("count"))
            if count_val is not None and count_val >= 0:
                total += count_val
                found = True
    if found and total > 0:
        return total
    return None


def _run_strava_discovery(
    *,
    db: Session,
    settings: Settings,
    state: VaultSyncState,
    conn: OAuthConnection,
    access_token: str,
    client_factory: Callable[[], httpx.Client],
    now: datetime,
) -> bool:
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    now_utc = ensure_utc(now)

    state.status = "running"
    state.phase = "discovering_pages"
    state.lease_expires_at = now_utc + timedelta(seconds=VAULT_SYNC_LEASE_SECONDS)
    db.commit()

    athlete_resp = _strava_vault_request(
        client_factory,
        settings,
        "GET",
        "/api/v3/athlete",
        headers=auth_headers,
    )
    should_pause, next_retry, rl = compute_rate_limit_pause(
        athlete_resp.headers, athlete_resp.status_code, now_utc
    )
    _apply_rate_limit_info(state, rl)
    if athlete_resp.status_code == 200:
        athlete_json = athlete_resp.json()
        if isinstance(athlete_json, dict):
            upsert_strava_gears(
                db, user_id=state.user_id, athlete_payload=athlete_json, now=now_utc
            )
    if athlete_resp.status_code == 429 or should_pause:
        state.status = "rate_limited"
        state.phase = "rate_limit_paused"
        state.next_retry_at = next_retry
        state.lease_expires_at = None
        if athlete_resp.status_code == 429:
            state.last_error = "Strava rate limit (429) during discovery"
        db.commit()
        return False
    db.commit()

    if conn.external_athlete_id and state.mode == "full_backfill":
        stats_resp = _strava_vault_request(
            client_factory,
            settings,
            "GET",
            f"/api/v3/athletes/{conn.external_athlete_id}/stats",
            headers=auth_headers,
        )
        should_pause, next_retry, rl = compute_rate_limit_pause(
            stats_resp.headers, stats_resp.status_code, now_utc
        )
        _apply_rate_limit_info(state, rl)
        if stats_resp.status_code == 200:
            stats_json = stats_resp.json()
            if isinstance(stats_json, dict):
                est = _extract_estimated_total(stats_json)
                if est is not None:
                    state.estimated_total_activities = est
        if stats_resp.status_code == 429 or should_pause:
            state.status = "rate_limited"
            state.phase = "rate_limit_paused"
            state.next_retry_at = next_retry
            state.lease_expires_at = None
            if stats_resp.status_code == 429:
                state.last_error = "Strava rate limit (429) during discovery"
            db.commit()
            return False

    state.phase = "importing_activities"
    state.lease_expires_at = now_utc + timedelta(seconds=VAULT_SYNC_LEASE_SECONDS)
    db.commit()
    return True


def run_vault_sync_steps(
    db: Session,
    settings: Settings,
    state: VaultSyncState,
    conn: OAuthConnection,
    *,
    http_client_factory: Callable[[], httpx.Client] | None = None,
    max_pages: int | None = None,
    now: datetime | None = None,
    propagate_ssrf: bool = False,
) -> VaultSyncState:
    """Execute discovery + paginated resumable Strava sync commits with zero locks across HTTP."""
    client_factory = http_client_factory or _default_http_client
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)

    try:
        access_token = _get_valid_access_token_unlocked(
            db=db,
            conn=conn,
            settings=settings,
            client_factory=client_factory,
            now=current_time,
        )

        if state.current_page == 0 and state.phase in {
            "idle",
            "discovering_pages",
            "rate_limit_paused",
        }:
            proceed = _run_strava_discovery(
                db=db,
                settings=settings,
                state=state,
                conn=conn,
                access_token=access_token,
                client_factory=client_factory,
                now=current_time,
            )
            if not proceed:
                return state

        auth_headers = {"Authorization": f"Bearer {access_token}"}
        pages_fetched = 0

        while max_pages is None or pages_fetched < max_pages:
            state.status = "running"
            state.phase = "importing_activities"
            state.lease_expires_at = current_time + timedelta(seconds=VAULT_SYNC_LEASE_SECONDS)
            db.commit()

            params: dict[str, int] = {"per_page": VAULT_SYNC_PAGE_SIZE}
            if state.backfill_cursor_before_epoch is not None:
                params["before"] = int(state.backfill_cursor_before_epoch)
            if state.incremental_after_epoch is not None:
                params["after"] = int(state.incremental_after_epoch)

            resp = _strava_vault_request(
                client_factory,
                settings,
                "GET",
                "/api/v3/athlete/activities",
                params=params,
                headers=auth_headers,
            )

            should_pause, next_retry, rl = compute_rate_limit_pause(
                resp.headers, resp.status_code, current_time
            )
            _apply_rate_limit_info(state, rl)

            if resp.status_code == 429:
                state.status = "rate_limited"
                state.phase = "rate_limit_paused"
                state.next_retry_at = next_retry
                state.lease_expires_at = None
                state.last_error = "Strava rate limit (429); paused until quota reset"
                db.commit()
                return state

            if resp.status_code != 200:
                state.status = "failed"
                state.phase = "failed"
                state.lease_expires_at = None
                state.last_error = f"Strava activities fetch failed (status={resp.status_code})"
                db.commit()
                return state

            items = resp.json()
            if not isinstance(items, list):
                state.status = "failed"
                state.phase = "failed"
                state.lease_expires_at = None
                state.last_error = "Malformed Strava activities response payload"
                db.commit()
                return state

            gear_rows = list(
                db.scalars(
                    select(VaultGear).where(
                        VaultGear.user_id == state.user_id,
                        VaultGear.provider == "strava",
                    )
                ).all()
            )
            gear_names = {g.gear_id: g.name for g in gear_rows}

            page_ext_ids: list[str] = []
            for raw_item in items:
                if isinstance(raw_item, dict):
                    raw_id = raw_item.get("id")
                    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                        page_ext_ids.append(str(raw_id))

            existing_map: dict[str, VaultActivity] = {}
            if page_ext_ids:
                existing_rows = list(
                    db.scalars(
                        select(VaultActivity).where(
                            VaultActivity.user_id == state.user_id,
                            VaultActivity.source_provider == "strava",
                            VaultActivity.external_activity_id.in_(page_ext_ids),
                        )
                    ).all()
                )
                existing_map = {row.external_activity_id: row for row in existing_rows}

            page_min_epoch: int | None = None
            for raw_item in items:
                state.total_scanned += 1
                if not isinstance(raw_item, dict):
                    state.total_failed += 1
                    continue
                try:
                    upsert_res = upsert_strava_activity_to_vault(
                        db,
                        user_id=state.user_id,
                        item=raw_item,
                        now=current_time,
                        gear_names_by_id=gear_names,
                        existing_by_ext_id=existing_map,
                    )
                    existing_map[upsert_res.vault_activity.external_activity_id] = (
                        upsert_res.vault_activity
                    )
                    item_epoch = int(ensure_utc(upsert_res.vault_activity.started_at).timestamp())
                    if page_min_epoch is None or item_epoch < page_min_epoch:
                        page_min_epoch = item_epoch

                    if upsert_res.created:
                        state.total_upserted += 1
                    else:
                        state.total_skipped_existing += 1
                    if not upsert_res.has_gps:
                        state.total_skipped_no_gps += 1
                    if upsert_res.tiles_computed:
                        state.tiles_computed_count += 1
                except Exception as exc:
                    logger.warning("Skipping malformed Strava activity in Vault sync: %s", exc)
                    state.total_failed += 1

            pages_fetched += 1
            state.current_page += 1
            state.vault_version = int(state.vault_version or 1) + 1
            state.last_synced_at = current_time

            if page_min_epoch is not None:
                prev_cursor = state.backfill_cursor_before_epoch
                next_cursor = page_min_epoch - 1
                if prev_cursor is not None and next_cursor >= prev_cursor:
                    next_cursor = prev_cursor - 1
                state.backfill_cursor_before_epoch = max(0, next_cursor)

            reached_end = len(items) < VAULT_SYNC_PAGE_SIZE or len(items) == 0
            if (
                not reached_end
                and state.incremental_after_epoch is not None
                and page_min_epoch is not None
                and page_min_epoch <= state.incremental_after_epoch
            ):
                reached_end = True
            if not reached_end and state.backfill_cursor_before_epoch == 0:
                reached_end = True

            db.commit()

            if reached_end:
                state.phase = "computing_tiles"
                state.lease_expires_at = current_time + timedelta(seconds=VAULT_SYNC_LEASE_SECONDS)
                db.commit()

                recompute_user_chronological_new_tiles(db, state.user_id)

                latest_started = db.scalar(
                    select(func.max(VaultActivity.started_at)).where(
                        VaultActivity.user_id == state.user_id,
                        VaultActivity.source_provider == "strava",
                    )
                )
                if latest_started is not None:
                    state.incremental_after_epoch = int(ensure_utc(latest_started).timestamp())

                state.status = "completed"
                state.phase = "completed"
                state.last_synced_at = current_time
                state.last_error = None
                state.next_retry_at = None
                state.lease_expires_at = None
                state.vault_version = int(state.vault_version or 1) + 1
                if state.mode == "full_backfill":
                    state.backfill_completed_at = current_time
                    state.backfill_cursor_before_epoch = None
                db.commit()
                return state

            if should_pause:
                state.status = "rate_limited"
                state.phase = "rate_limit_paused"
                state.next_retry_at = next_retry
                state.lease_expires_at = None
                db.commit()
                return state

        # Inline page cap reached with more pages still remaining: hand off to background worker.
        state.status = "queued"
        state.phase = "importing_activities"
        state.lease_expires_at = None
        db.commit()
        return state
    except Exception as exc:
        db.rollback()
        refreshed = db.get(VaultSyncState, state.id)
        if refreshed is not None:
            if (
                isinstance(exc, VaultSyncTransientError)
                and refreshed.retry_count < VAULT_SYNC_MAX_RETRIES
            ):
                refreshed.retry_count += 1
                refreshed.status = "queued"
                refreshed.next_retry_at = current_time + timedelta(
                    seconds=min(60 * 2 ** (refreshed.retry_count - 1), 900)
                )
            else:
                refreshed.status = "failed"
                refreshed.phase = "failed"
                refreshed.next_retry_at = None
            refreshed.lease_expires_at = None
            refreshed.last_error = f"{type(exc).__name__}: {exc}"[:500]
            db.commit()
            if propagate_ssrf and isinstance(exc, SsrfBlockedError):
                raise
            return refreshed
        raise


def start_or_resume_strava_vault_sync(
    db: Session,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    mode: str = "incremental",
    after: str | None = None,
    before: str | None = None,
    inline_first_page: bool = True,
    http_client_factory: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> VaultSyncState:
    """Durably enqueue a Strava sync; provider requests belong to the worker only."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    resolved_mode: VaultSyncMode = (
        "full_backfill" if mode in {"full", "full_backfill"} else "incremental"
    )

    after_epoch = parse_filter_epoch(after, end_of_day=False) if after is not None else None
    before_epoch = parse_filter_epoch(before, end_of_day=True) if before is not None else None
    if after_epoch is not None and before_epoch is not None and after_epoch > before_epoch:
        raise ValueError("'after' filter must be <= 'before' filter")

    conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "strava",
        )
    )
    if conn is None:
        raise LookupError("Strava account is not connected")

    state = get_or_create_vault_sync_state(db, user_id, provider="strava")
    # Active/waiting work is authoritative: repeated requests must not erase its
    # cursor, progress, retry deadline, or lease.
    if state.status in {"queued", "running", "rate_limited"}:
        return state

    # Start a fresh run only after the previous run reached a terminal state.
    state.mode = resolved_mode
    state.status = "queued"
    state.phase = "discovering_pages"
    state.started_at = current_time
    state.retry_count = 0
    state.current_page = 0
    state.total_scanned = 0
    state.total_upserted = 0
    state.total_skipped_existing = 0
    state.total_skipped_no_gps = 0
    state.total_failed = 0
    state.tiles_computed_count = 0
    state.estimated_total_activities = None
    state.last_error = None
    state.next_retry_at = None
    state.lease_expires_at = None

    if resolved_mode == "full_backfill":
        state.backfill_completed_at = None
        state.backfill_cursor_before_epoch = (
            before_epoch if before_epoch is not None else int(current_time.timestamp())
        )
        state.incremental_after_epoch = after_epoch
    else:
        resolved_after = after_epoch
        if resolved_after is None:
            if state.incremental_after_epoch is not None:
                resolved_after = max(0, int(state.incremental_after_epoch) - 3600)
            else:
                latest_started = db.scalar(
                    select(func.max(VaultActivity.started_at)).where(
                        VaultActivity.user_id == user_id,
                        VaultActivity.source_provider == "strava",
                    )
                )
                if latest_started is not None:
                    resolved_after = max(0, int(ensure_utc(latest_started).timestamp()) - 3600)
        state.incremental_after_epoch = resolved_after
        state.backfill_cursor_before_epoch = (
            before_epoch if before_epoch is not None else int(current_time.timestamp())
        )

    db.commit()
    return state


def _eligible_vault_sync_condition(now: datetime) -> Any:
    return (
        (
            (VaultSyncState.status == "queued")
            & (VaultSyncState.next_retry_at.is_(None) | (VaultSyncState.next_retry_at <= now))
            & (VaultSyncState.lease_expires_at.is_(None) | (VaultSyncState.lease_expires_at <= now))
        )
        | (
            (VaultSyncState.status == "running")
            & VaultSyncState.lease_expires_at.is_not(None)
            & (VaultSyncState.lease_expires_at <= now)
        )
        | (
            (VaultSyncState.status == "rate_limited")
            & (VaultSyncState.next_retry_at.is_(None) | (VaultSyncState.next_retry_at <= now))
        )
    )


def count_pending_vault_syncs(
    db: Session,
    *,
    batch_size: int = 5,
    now: datetime | None = None,
) -> int:
    """Count eligible `VaultSyncState` rows ready for worker execution."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    stmt = (
        select(VaultSyncState.id)
        .where(_eligible_vault_sync_condition(current_time))
        .order_by(VaultSyncState.updated_at.asc())
        .limit(batch_size)
    )
    return len(list(db.scalars(stmt).all()))


def process_pending_vault_syncs(
    db: Session,
    settings: Settings,
    *,
    batch_size: int = 5,
    max_pages_per_sync: int | None = None,
    http_client_factory: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Claim and continue eligible queued/stale/rate-limited Vault sync states."""
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)
    lease_until = current_time + timedelta(seconds=VAULT_SYNC_LEASE_SECONDS)

    stmt = (
        select(VaultSyncState)
        .where(_eligible_vault_sync_condition(current_time))
        .order_by(VaultSyncState.updated_at.asc())
        .limit(batch_size)
    )
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    claimed_rows = list(db.scalars(stmt).all())
    claimed_ids: list[uuid.UUID] = []
    for row in claimed_rows:
        row.status = "running"
        row.lease_expires_at = lease_until
        if row.phase == "rate_limit_paused":
            row.phase = "discovering_pages" if row.current_page == 0 else "importing_activities"
        claimed_ids.append(row.id)
    # CRITICAL INVARIANT: Release FOR UPDATE SKIP LOCKED immediately before any HTTP calls.
    if claimed_rows:
        db.commit()

    completed = 0
    rate_limited = 0
    queued = 0
    failed = 0

    for sync_id in claimed_ids:
        state = db.get(VaultSyncState, sync_id)
        if state is None:
            continue
        conn = db.scalar(
            select(OAuthConnection)
            .join(User, OAuthConnection.user_id == User.id)
            .where(
                OAuthConnection.user_id == state.user_id,
                OAuthConnection.provider == state.provider,
                User.is_active.is_(True),
            )
        )
        if conn is None:
            state.status = "failed"
            state.phase = "failed"
            state.lease_expires_at = None
            state.last_error = "No active Strava OAuth connection for user"
            db.commit()
            failed += 1
            continue

        updated_state = run_vault_sync_steps(
            db,
            settings,
            state,
            conn,
            http_client_factory=http_client_factory,
            max_pages=max_pages_per_sync,
            now=current_time,
        )
        if updated_state.status == "completed":
            completed += 1
        elif updated_state.status == "rate_limited":
            rate_limited += 1
        elif updated_state.status == "queued":
            queued += 1
        else:
            failed += 1

    return {
        "claimed": len(claimed_ids),
        "completed": completed,
        "rate_limited": rate_limited,
        "queued": queued,
        "failed": failed,
    }


def _compute_progress_pct(state: VaultSyncState | None) -> float | None:
    if state is None or state.status == "idle":
        return None
    if state.status == "completed" or state.phase == "completed":
        return 100.0
    if state.phase == "discovering_pages":
        return 5.0
    if state.phase == "computing_tiles":
        return 95.0
    est = state.estimated_total_activities
    if est is not None and est > 0:
        ratio = min(1.0, float(state.total_scanned) / float(est))
        return round(min(99.0, 10.0 + 75.0 * ratio), 1)
    return None


def _compute_eta_seconds(state: VaultSyncState | None, now_utc: datetime) -> int | None:
    if state is None:
        return None
    if state.status not in {"running", "queued"} or state.phase != "importing_activities":
        return None
    est = state.estimated_total_activities
    if est is None or est <= state.total_scanned or state.total_scanned < 50:
        return None
    if state.started_at is None:
        return None
    elapsed_s = max(1.0, (now_utc - ensure_utc(state.started_at)).total_seconds())
    rate = float(state.total_scanned) / elapsed_s
    if rate <= 0.0:
        return None
    remaining = est - state.total_scanned
    return max(1, int(round(remaining / rate)))


def _format_phase_label(
    state: VaultSyncState | None,
    retry_in_seconds: int | None,
) -> str:
    if state is None or state.phase == "idle":
        return "Ready to sync"
    if state.phase == "discovering_pages":
        return "Discovering Strava history..."
    if state.phase == "importing_activities":
        page_num = max(1, int(state.current_page))
        if state.estimated_total_activities and state.estimated_total_activities > 0:
            return (
                f"Importing activities (page {page_num}) — "
                f"{state.total_scanned:,} / {state.estimated_total_activities:,}"
            )
        return f"Importing activities (page {page_num}) — {state.total_scanned:,} processed"
    if state.phase == "computing_tiles":
        if state.estimated_total_activities and state.estimated_total_activities > 0:
            return (
                f"Computing Z14 Explorer tiles "
                f"({state.tiles_computed_count:,} / {state.estimated_total_activities:,})..."
            )
        return f"Computing Z14 Explorer tiles ({state.tiles_computed_count:,})..."
    if state.phase == "rate_limit_paused":
        if retry_in_seconds is not None:
            mins, secs = divmod(retry_in_seconds, 60)
            return f"Strava rate limit pause — resumes in {mins:02d}:{secs:02d}"
        return "Strava rate limit pause — waiting for quota reset"
    if state.phase == "completed":
        return f"Sync complete — {state.total_upserted:,} imported"
    return state.last_error or "Sync failed"


def build_vault_sync_status(
    db: Session,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> VaultSyncStatusOut:
    """Assemble the full live transparent `VaultSyncStatusOut` snapshot for a user."""
    now_utc = ensure_utc(now) if now is not None else datetime.now(UTC)

    conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "strava",
        )
    )
    state = db.scalar(
        select(VaultSyncState).where(
            VaultSyncState.user_id == user_id,
            VaultSyncState.provider == "strava",
        )
    )

    total_activities = int(
        db.scalar(select(func.count(VaultActivity.id)).where(VaultActivity.user_id == user_id)) or 0
    )
    gps_activities = int(
        db.scalar(
            select(func.count(VaultActivity.id)).where(
                VaultActivity.user_id == user_id,
                VaultActivity.has_gps.is_(True),
            )
        )
        or 0
    )
    indoor_activities = int(
        db.scalar(
            select(func.count(VaultActivity.id)).where(
                VaultActivity.user_id == user_id,
                VaultActivity.is_indoor_virtual.is_(True),
            )
        )
        or 0
    )

    retry_in_seconds: int | None = None
    if state is not None and state.status == "rate_limited" and state.next_retry_at is not None:
        retry_in_seconds = max(0, int((ensure_utc(state.next_retry_at) - now_utc).total_seconds()))

    short_used = state.rate_limit_short_used if state is not None else None
    short_limit = state.rate_limit_short_limit if state is not None else None
    daily_used = state.rate_limit_daily_used if state is not None else None
    daily_limit = state.rate_limit_daily_limit if state is not None else None

    short_usage_str = (
        f"{short_used}/{short_limit}"
        if short_used is not None and short_limit is not None
        else None
    )
    daily_usage_str = (
        f"{daily_used}/{daily_limit}"
        if daily_used is not None and daily_limit is not None
        else None
    )

    webhook_active = bool(
        conn is not None
        and (settings.strava_verify_token or settings.strava_webhook_subscription_id)
    )

    return VaultSyncStatusOut(
        connected=conn is not None,
        athlete_id=conn.external_athlete_id if conn is not None else None,
        status=cast(VaultSyncStatus, state.status if state is not None else "idle"),
        phase=cast(VaultSyncPhase, state.phase if state is not None else "idle"),
        phase_label=_format_phase_label(state, retry_in_seconds),
        mode=cast(VaultSyncMode, state.mode if state is not None else "incremental"),
        progress_pct=_compute_progress_pct(state),
        current_page=int(state.current_page) if state is not None else 0,
        processed_activities=int(state.total_scanned) if state is not None else 0,
        estimated_total_activities=state.estimated_total_activities if state is not None else None,
        imported_count=int(state.total_upserted) if state is not None else 0,
        skipped_existing_count=int(state.total_skipped_existing) if state is not None else 0,
        skipped_no_gps_count=int(state.total_skipped_no_gps) if state is not None else 0,
        failed_count=int(state.total_failed) if state is not None else 0,
        tiles_computed_count=int(state.tiles_computed_count) if state is not None else 0,
        vault_version=int(state.vault_version) if state is not None else 1,
        total_activities=total_activities,
        gps_activities=gps_activities,
        indoor_activities=indoor_activities,
        started_at=ensure_utc(state.started_at) if state and state.started_at else None,
        backfill_completed_at=(
            ensure_utc(state.backfill_completed_at)
            if state and state.backfill_completed_at
            else None
        ),
        last_synced_at=ensure_utc(state.last_synced_at) if state and state.last_synced_at else None,
        next_retry_at=ensure_utc(state.next_retry_at) if state and state.next_retry_at else None,
        rate_limit_retry_in_seconds=retry_in_seconds,
        eta_seconds=_compute_eta_seconds(state, now_utc),
        rate_limit=VaultRateLimitOut(
            short_used=short_used,
            short_limit=short_limit,
            daily_used=daily_used,
            daily_limit=daily_limit,
        ),
        strava_short_usage=short_usage_str,
        strava_daily_usage=daily_usage_str,
        webhook_active=webhook_active,
        last_error=state.last_error if state is not None else None,
    )


def _downsample_to_summary_polyline(
    points_with_seg: Sequence[tuple[float, float, int]],
    *,
    max_points: int = VAULT_SYNC_STAGE_POLYLINE_MAX_POINTS,
) -> str | None:
    if not points_with_seg:
        return None
    simplified = list(points_with_seg)
    if len(simplified) > max_points:
        for tolerance_m in (15.0, 35.0, 75.0, 150.0, 300.0, 600.0):
            simplified = simplify_coord_tuples_m(simplified, tolerance_m)
            if len(simplified) <= max_points:
                break
    if len(simplified) > max_points:
        step = (len(simplified) - 1) / float(max_points - 1)
        indices = sorted(
            {min(len(simplified) - 1, int(round(i * step))) for i in range(max_points)}
        )
        simplified = [simplified[idx] for idx in indices]
    return str(encode_polyline([(pt[0], pt[1]) for pt in simplified]))


def _group_segments(
    points_with_seg: Sequence[tuple[float, float, int]],
) -> list[list[tuple[float, float]]]:
    segments: list[list[tuple[float, float]]] = []
    current_seg_idx: int | None = None
    current_pts: list[tuple[float, float]] = []
    for lat, lon, seg_idx in points_with_seg:
        if current_seg_idx is None or seg_idx != current_seg_idx:
            if current_pts:
                segments.append(current_pts)
            current_pts = [(lat, lon)]
            current_seg_idx = seg_idx
        else:
            current_pts.append((lat, lon))
    if current_pts:
        segments.append(current_pts)
    return segments


def _prepare_geometry_from_tuples(
    points_with_seg: Sequence[tuple[float, float, int]],
    *,
    elevations: Sequence[float] = (),
    heart_rates: Sequence[float] = (),
    watts: Sequence[float] = (),
) -> PreparedStageVaultGeometry:
    if not points_with_seg:
        return PreparedStageVaultGeometry(summary_polyline=None, tiles_z14=frozenset())

    segments = _group_segments(points_with_seg)
    tiles = frozenset(rasterize_track_segments(segments, zoom=VAULT_EXPLORER_ZOOM))
    poly = _downsample_to_summary_polyline(points_with_seg)
    lats = [p[0] for p in points_with_seg]
    lons = [p[1] for p in points_with_seg]

    return PreparedStageVaultGeometry(
        summary_polyline=poly,
        tiles_z14=tiles,
        start_lat=points_with_seg[0][0],
        start_lon=points_with_seg[0][1],
        end_lat=points_with_seg[-1][0],
        end_lon=points_with_seg[-1][1],
        min_lat=min(lats),
        max_lat=max(lats),
        min_lon=min(lons),
        max_lon=max(lons),
        elev_high_m=max(elevations) if elevations else None,
        elev_low_m=min(elevations) if elevations else None,
        avg_hr_bpm=(sum(heart_rates) / len(heart_rates)) if heart_rates else None,
        max_hr_bpm=max(heart_rates) if heart_rates else None,
        avg_watts=(sum(watts) / len(watts)) if watts else None,
    )


def prepare_vault_geometry_from_parsed_track(parsed: ParsedTrack) -> PreparedStageVaultGeometry:
    """Compute summary polyline and Z14 tiles from a `ParsedTrack` (pure CPU, no DB locks)."""
    pts: list[tuple[float, float, int]] = []
    elevs: list[float] = []
    hrs: list[float] = []
    watts: list[float] = []
    for pt in parsed.points:
        pts.append((float(pt.lat), float(pt.lon), int(pt.segment_idx)))
        if pt.elev_m is not None and math.isfinite(pt.elev_m):
            elevs.append(float(pt.elev_m))
        if pt.hr_bpm is not None and math.isfinite(pt.hr_bpm):
            hrs.append(float(pt.hr_bpm))
        if pt.watts is not None and math.isfinite(pt.watts):
            watts.append(float(pt.watts))
    return _prepare_geometry_from_tuples(pts, elevations=elevs, heart_rates=hrs, watts=watts)


def apply_strava_track_geometry(
    db: Session,
    activity: VaultActivity,
    geometry: PreparedStageVaultGeometry,
    *,
    now: datetime,
) -> None:
    """Replace canonical geometry from original provider streams without touching project stages."""
    if activity.source_provider != "strava":
        raise ValueError("Original Strava geometry requires a Strava catalog activity")
    for field in (
        "summary_polyline",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "min_lat",
        "max_lat",
        "min_lon",
        "max_lon",
        "elev_high_m",
        "elev_low_m",
        "avg_hr_bpm",
        "max_hr_bpm",
        "avg_watts",
    ):
        setattr(activity, field, getattr(geometry, field))
    activity.has_gps = bool(geometry.tiles_z14)
    activity.tile_precision = "full_stream" if activity.has_gps else "none"
    activity.tile_count_z14 = _replace_activity_z14_tiles(
        db, vault_activity_id=activity.id, user_id=activity.user_id, tiles=geometry.tiles_z14
    )
    activity.synced_at = ensure_utc(now)


def prepare_vault_geometry_from_dataframe(df: pl.DataFrame) -> PreparedStageVaultGeometry:
    """Compute summary polyline and Z14 tiles from a Silver Polars DataFrame (pure CPU)."""
    active = df.filter(~df.get_column("is_trimmed")) if "is_trimmed" in df.columns else df
    if active.height == 0:
        return PreparedStageVaultGeometry(summary_polyline=None, tiles_z14=frozenset())

    lats = active.get_column("lat").to_list()
    lons = active.get_column("lon").to_list()
    segs = (
        active.get_column("segment_idx").to_list()
        if "segment_idx" in active.columns
        else [0] * active.height
    )
    pts = [
        (float(lat), float(lon), int(seg))
        for lat, lon, seg in zip(lats, lons, segs, strict=True)
        if lat is not None and lon is not None
    ]
    elevs = (
        [
            float(v)
            for v in active.get_column("elev_m").drop_nulls().to_list()
            if math.isfinite(float(v))
        ]
        if "elev_m" in active.columns
        else []
    )
    hrs = (
        [
            float(v)
            for v in active.get_column("hr_bpm").drop_nulls().to_list()
            if math.isfinite(float(v))
        ]
        if "hr_bpm" in active.columns
        else []
    )
    watts = (
        [
            float(v)
            for v in active.get_column("watts").drop_nulls().to_list()
            if math.isfinite(float(v))
        ]
        if "watts" in active.columns
        else []
    )
    return _prepare_geometry_from_tuples(pts, elevations=elevs, heart_rates=hrs, watts=watts)


def mirror_stage_to_vault(
    db: Session,
    *,
    user_id: uuid.UUID,
    stage: Stage,
    geometry: PreparedStageVaultGeometry | None = None,
    trusted_provider_geometry: bool = False,
    strava_activity_json: Mapping[str, Any] | None = None,
    recompute_new_tiles: bool = True,
    now: datetime | None = None,
) -> VaultActivity:
    """Mirror or upgrade a `Stage` into `vault_activities` and link `stage.vault_activity_id`."""
    now_utc = ensure_utc(now) if now is not None else datetime.now(UTC)

    vault_act: VaultActivity | None = None
    if stage.vault_activity_id is not None:
        vault_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.id == stage.vault_activity_id,
                VaultActivity.user_id == user_id,
            )
        )
    if vault_act is None and stage.strava_activity_id is not None:
        vault_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == user_id,
                VaultActivity.source_provider == "strava",
                VaultActivity.external_activity_id == str(stage.strava_activity_id),
            )
        )
    if vault_act is None and stage.strava_activity_id is None:
        vault_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == user_id,
                VaultActivity.source_provider == "stage",
                VaultActivity.external_activity_id == str(stage.id),
            )
        )
    if vault_act is None and stage.strava_activity_id is None:
        vault_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == user_id,
                VaultActivity.source_stage_id == stage.id,
            )
        )

    activity_existed = vault_act is not None
    # If Strava activity JSON is supplied, upsert metadata first (preserves local_date/gear).
    if strava_activity_json is not None and stage.strava_activity_id is not None:
        upsert_res = upsert_strava_activity_to_vault(
            db,
            user_id=user_id,
            item=strava_activity_json,
            now=now_utc,
        )
        vault_act = upsert_res.vault_activity

    if vault_act is not None and vault_act.disconnected_at is not None:
        stage.vault_activity_id = vault_act.id
        return vault_act

    geom = geometry or PreparedStageVaultGeometry(summary_polyline=None, tiles_z14=frozenset())
    # An already existing library activity is canonical source data. A project
    # stage may be trimmed, renamed, or split, so never mirror its metrics or
    # geometry back over that canonical record. A supplied provider track is
    # trusted source data and may upgrade canonical geometry independently.
    if activity_existed and vault_act is not None:
        if (trusted_provider_geometry or strava_activity_json is not None) and geom.tiles_z14:
            vault_act.has_gps = True
            if geom.summary_polyline is not None:
                vault_act.summary_polyline = geom.summary_polyline
            for field in (
                "start_lat",
                "start_lon",
                "end_lat",
                "end_lon",
                "min_lat",
                "max_lat",
                "min_lon",
                "max_lon",
                "elev_high_m",
                "elev_low_m",
                "avg_hr_bpm",
                "max_hr_bpm",
                "avg_watts",
            ):
                value = getattr(geom, field)
                if value is not None:
                    setattr(vault_act, field, value)
            vault_act.tile_count_z14 = _replace_activity_z14_tiles(
                db,
                vault_activity_id=vault_act.id,
                user_id=user_id,
                tiles=geom.tiles_z14,
            )
            vault_act.tile_precision = "full_stream"
            vault_act.synced_at = now_utc
        # A catalog-first Strava activity can be missing its local reusable
        # source pointer. Attach only an untrimmed, unsplit Strava import from
        # the same owner; project edits/copies are not canonical provenance.
        if (
            vault_act.source_stage_id is None
            and vault_act.user_id == user_id
            and vault_act.source_provider == "strava"
            and stage.source_type == "strava"
            and stage.strava_activity_id is not None
            and stage.external_source_id == str(stage.strava_activity_id)
            and vault_act.strava_activity_id == stage.strava_activity_id
            and vault_act.external_activity_id == str(stage.strava_activity_id)
            and strava_activity_json is not None
            and str(strava_activity_json.get("id")) == str(stage.strava_activity_id)
            and len(stage.bronze_sha256 or "") == 64
            and all(char in "0123456789abcdef" for char in stage.bronze_sha256 or "")
            and bool(stage.bronze_object_key)
            and bool(stage.silver_object_key)
            and stage.trim_start_s is None
            and stage.trim_end_s is None
            and not any(
                isinstance(warning, dict)
                and warning.get("code") == "vault_canonical_excluded_split_segment"
                for warning in (stage.warnings_json or [])
            )
            and (project := db.get(Project, stage.project_id)) is not None
            and project.owner_user_id == user_id
        ):
            vault_act.source_stage_id = stage.id
        stage.vault_activity_id = vault_act.id
        db.flush()
        if recompute_new_tiles:
            recompute_user_chronological_new_tiles(db, user_id)
            bump_user_vault_version(db, user_id)
        return vault_act

    resolved_started_at = (
        ensure_utc(stage.started_at)
        if stage.started_at is not None
        else (ensure_utc(stage.created_at) if stage.created_at is not None else now_utc)
    )
    resolved_activity_type = normalize_activity_type(stage.activity_type, DEFAULT_ACTIVITY_TYPE)

    has_gps = bool(geom.tiles_z14 or geom.summary_polyline)
    avg_speed_mps = (
        float(stage.distance_m) / float(stage.moving_seconds)
        if stage.moving_seconds and stage.moving_seconds > 0
        else None
    )

    if vault_act is None:
        source_provider = "strava" if stage.strava_activity_id is not None else "stage"
        external_id = (
            str(stage.strava_activity_id) if stage.strava_activity_id is not None else str(stage.id)
        )
        vault_act = VaultActivity(
            user_id=user_id,
            source_provider=source_provider,
            external_activity_id=external_id,
            strava_activity_id=stage.strava_activity_id,
            source_stage_id=stage.id,
            name=stage.name[:200],
            raw_sport_type=stage.source_format or stage.source_type,
            activity_type=resolved_activity_type,
            started_at=resolved_started_at,
            started_at_local=resolved_started_at.replace(tzinfo=None),
            local_date=resolved_started_at.date(),
            timezone_name=None,
            utc_offset_s=0,
            distance_m=float(stage.distance_m or 0.0),
            elevation_gain_m=float(stage.elevation_gain_m or 0.0),
            elevation_loss_m=float(stage.elevation_loss_m or 0.0),
            elev_high_m=geom.elev_high_m,
            elev_low_m=geom.elev_low_m,
            moving_seconds=float(stage.moving_seconds or 0.0),
            elapsed_seconds=float(stage.elapsed_seconds or 0.0),
            avg_speed_mps=avg_speed_mps,
            avg_hr_bpm=geom.avg_hr_bpm,
            max_hr_bpm=geom.max_hr_bpm,
            avg_watts=geom.avg_watts,
            has_gps=has_gps,
            start_lat=geom.start_lat,
            start_lon=geom.start_lon,
            end_lat=geom.end_lat,
            end_lon=geom.end_lon,
            min_lat=geom.min_lat if geom.min_lat is not None else stage.min_lat,
            max_lat=geom.max_lat if geom.max_lat is not None else stage.max_lat,
            min_lon=geom.min_lon if geom.min_lon is not None else stage.min_lon,
            max_lon=geom.max_lon if geom.max_lon is not None else stage.max_lon,
            summary_polyline=geom.summary_polyline,
            tile_precision="full_stream" if geom.tiles_z14 else "none",
            tile_count_z14=0,
            new_tiles_z14_count=0,
            bronze_object_key=stage.bronze_object_key,
            raw_metadata_json={
                "source_format": stage.source_format,
                "distance": float(stage.distance_m) if stage.distance_m is not None else None,
                "total_elevation_gain": (
                    float(stage.elevation_gain_m) if stage.elevation_gain_m is not None else None
                ),
                "moving_time": (
                    float(stage.moving_seconds) if stage.moving_seconds is not None else None
                ),
                "elapsed_time": (
                    float(stage.elapsed_seconds) if stage.elapsed_seconds is not None else None
                ),
            },
            synced_at=now_utc,
        )
        db.add(vault_act)
        db.flush()
    else:
        if vault_act.source_provider == "stage":
            vault_act.name = stage.name[:200]
            vault_act.activity_type = resolved_activity_type
            vault_act.started_at = resolved_started_at
            vault_act.started_at_local = resolved_started_at.replace(tzinfo=None)
            vault_act.local_date = resolved_started_at.date()
            vault_act.distance_m = float(stage.distance_m or 0.0)
            vault_act.elevation_gain_m = float(stage.elevation_gain_m or 0.0)
            vault_act.elevation_loss_m = float(stage.elevation_loss_m or 0.0)
            vault_act.moving_seconds = float(stage.moving_seconds or 0.0)
            vault_act.elapsed_seconds = float(stage.elapsed_seconds or 0.0)
            vault_act.avg_speed_mps = avg_speed_mps
            vault_act.bronze_object_key = stage.bronze_object_key
        if vault_act.source_stage_id is None:
            vault_act.source_stage_id = stage.id
        if geom.elev_high_m is not None:
            vault_act.elev_high_m = geom.elev_high_m
        if geom.elev_low_m is not None:
            vault_act.elev_low_m = geom.elev_low_m
        if geom.avg_hr_bpm is not None:
            vault_act.avg_hr_bpm = geom.avg_hr_bpm
        if geom.max_hr_bpm is not None:
            vault_act.max_hr_bpm = geom.max_hr_bpm
        if geom.avg_watts is not None:
            vault_act.avg_watts = geom.avg_watts
        if geom.summary_polyline is not None:
            vault_act.summary_polyline = geom.summary_polyline
        if geom.start_lat is not None:
            vault_act.start_lat = geom.start_lat
            vault_act.start_lon = geom.start_lon
        if geom.end_lat is not None:
            vault_act.end_lat = geom.end_lat
            vault_act.end_lon = geom.end_lon
        if geom.min_lat is not None:
            vault_act.min_lat = geom.min_lat
            vault_act.max_lat = geom.max_lat
            vault_act.min_lon = geom.min_lon
            vault_act.max_lon = geom.max_lon
        vault_act.synced_at = now_utc

    if geom.tiles_z14:
        vault_act.has_gps = True
        vault_act.tile_count_z14 = _replace_activity_z14_tiles(
            db,
            vault_activity_id=vault_act.id,
            user_id=user_id,
            tiles=geom.tiles_z14,
        )
        vault_act.tile_precision = "full_stream"
    elif vault_act.source_provider == "stage":
        _replace_activity_z14_tiles(
            db,
            vault_activity_id=vault_act.id,
            user_id=user_id,
            tiles=(),
        )
        vault_act.tile_precision = "none"
        vault_act.tile_count_z14 = 0

    stage.vault_activity_id = vault_act.id
    db.flush()

    if recompute_new_tiles:
        recompute_user_chronological_new_tiles(db, user_id)
        bump_user_vault_version(db, user_id)

    return vault_act


def backfill_user_stages_to_vault(
    db: Session,
    store: ObjectStore,
    user_id: uuid.UUID,
) -> dict[str, int]:
    """Idempotently mirror unlinked user `Stage` rows into `vault_activities` lock-free."""
    stages = list(
        db.scalars(
            select(Stage)
            .join(Project, Stage.project_id == Project.id)
            .where(Project.owner_user_id == user_id)
            .order_by(Stage.created_at.asc(), Stage.id.asc())
        ).all()
    )

    scanned_stages = len(stages)
    skipped_stages = 0
    failed_stages = 0
    prepared_items: list[tuple[uuid.UUID, PreparedStageVaultGeometry]] = []

    existing_vault_ids = set(
        db.scalars(select(VaultActivity.id).where(VaultActivity.user_id == user_id)).all()
    )

    # Phase 1: Read Silver Parquets from ObjectStore outside any DB write lock.
    for st in stages:
        if any(
            warning.get("code") == "vault_canonical_excluded_split_segment"
            for warning in (st.warnings_json or [])
            if isinstance(warning, dict)
        ):
            skipped_stages += 1
            continue
        if st.vault_activity_id is not None and st.vault_activity_id in existing_vault_ids:
            skipped_stages += 1
            continue
        if st.trim_start_s is not None or st.trim_end_s is not None:
            skipped_stages += 1
            continue
        try:
            with materialize_silver_parquet(store, st) as parquet_path:
                df = pl.read_parquet(parquet_path)
            geom = prepare_vault_geometry_from_dataframe(df)
            prepared_items.append((st.id, geom))
        except Exception as exc:
            logger.warning("Failed preparing Vault geometry for stage %s: %s", st.id, exc)
            failed_stages += 1

    mirrored_stages = 0
    linked_stages = 0
    now_utc = datetime.now(UTC)
    for stage_id, geom in prepared_items:
        stage_row = db.get(Stage, stage_id)
        if stage_row is None:
            continue
        existed_before = (
            stage_row.strava_activity_id is not None
            and db.scalar(
                select(VaultActivity.id).where(
                    VaultActivity.user_id == user_id,
                    VaultActivity.source_provider == "strava",
                    VaultActivity.external_activity_id == str(stage_row.strava_activity_id),
                )
            )
            is not None
        )
        mirror_stage_to_vault(
            db,
            user_id=user_id,
            stage=stage_row,
            geometry=geom,
            recompute_new_tiles=False,
            now=now_utc,
        )
        linked_stages += 1
        if not existed_before:
            mirrored_stages += 1

    if linked_stages > 0:
        recompute_user_chronological_new_tiles(db, user_id)
        vault_version = bump_user_vault_version(db, user_id)
        db.commit()
    else:
        state = db.scalar(
            select(VaultSyncState).where(
                VaultSyncState.user_id == user_id,
                VaultSyncState.provider == "strava",
            )
        )
        vault_version = int(state.vault_version) if state is not None else 1

    return {
        "scanned_stages": scanned_stages,
        "mirrored_stages": mirrored_stages,
        "linked_stages": linked_stages,
        "skipped_stages": skipped_stages,
        "failed_stages": failed_stages,
        "vault_version": vault_version,
    }
