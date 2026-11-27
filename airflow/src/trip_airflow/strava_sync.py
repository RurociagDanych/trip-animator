"""Pure-Python Strava incremental Bronze -> Silver synchronization tasks."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from trip_api.config import Settings
from trip_api.db import OAuthConnection, Project, Stage, User
from trip_api.deps import ensure_utc
from trip_api.security import (
    decrypt_secret,
    encrypt_secret,
    validate_outbound_url,
)
from trip_api.services import ingest_parsed_track, write_audit_event
from trip_object_store import ObjectStore
from trip_telemetry import infer_activity_type_from_strava, parse_strava_streams

logger = logging.getLogger(__name__)


class StravaUpstreamError(RuntimeError):
    """Raised when Strava upstream returns an unexpected status or redirect."""


def _oauth_aad(user_id: uuid.UUID) -> bytes:
    return f"oauth:{user_id}:strava".encode()


def _default_sync_http_client() -> httpx.Client:
    return httpx.Client(timeout=15.0, follow_redirects=False)


def _strava_sync_request(
    client_factory: Callable[[], httpx.Client],
    settings: Settings,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
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
            logger.warning("Strava HTTP request failed: %s", type(exc).__name__)
            raise StravaUpstreamError("Strava upstream request failed") from exc

    if 300 <= resp.status_code < 400:
        raise StravaUpstreamError("Unexpected redirect from Strava upstream")
    return resp


def _coerce_utc_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt: datetime = ensure_utc(datetime.fromisoformat(text))
        return dt
    dt_val: datetime = ensure_utc(value)
    return dt_val


def discover_active_strava_connections(db: Session) -> list[dict[str, str]]:
    """Return active Strava OAuth connection IDs + user IDs only (zero secrets in XCom)."""
    stmt = (
        select(OAuthConnection.id, OAuthConnection.user_id)
        .join(User, OAuthConnection.user_id == User.id)
        .where(
            OAuthConnection.provider == "strava",
            User.is_active.is_(True),
        )
        .order_by(OAuthConnection.updated_at.asc())
    )
    rows = db.execute(stmt).all()
    return [{"connection_id": str(conn_id), "user_id": str(user_id)} for conn_id, user_id in rows]


def _resolve_or_create_autosync_project(db: Session, user_id: uuid.UUID) -> Project:
    user_projects = list(
        db.scalars(
            select(Project)
            .where(Project.owner_user_id == user_id)
            .order_by(Project.created_at.asc())
        ).all()
    )
    for proj in user_projects:
        if (
            isinstance(proj.settings_json, dict)
            and proj.settings_json.get("strava_auto_sync") is True
        ):
            return proj
    for proj in user_projects:
        if proj.title == "Strava Auto-Sync":
            return proj

    project = Project(
        owner_user_id=user_id,
        title="Strava Auto-Sync",
        settings_json={"strava_auto_sync": True},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _refresh_access_token_if_needed(
    *,
    db: Session,
    conn: OAuthConnection,
    settings: Settings,
    user_id: uuid.UUID,
    client_factory: Callable[[], httpx.Client],
    current_time: datetime,
) -> str | None:
    key = settings.decoded_encryption_key
    aad = _oauth_aad(user_id)
    access_token: str = decrypt_secret(conn.access_token_enc, key, aad=aad)
    refresh_token: str = decrypt_secret(conn.refresh_token_enc, key, aad=aad)

    if ensure_utc(conn.expires_at) > current_time + timedelta(seconds=60):
        return access_token

    if not settings.strava_client_id or not settings.strava_client_secret:
        logger.warning(
            "Skipping Strava token refresh for connection %s: client credentials unset",
            conn.id,
        )
        return None

    resp = _strava_sync_request(
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
        raise StravaUpstreamError(
            f"Failed to refresh Strava access token (status={resp.status_code})"
        )

    payload = resp.json()
    if (
        not isinstance(payload, dict)
        or "access_token" not in payload
        or "expires_at" not in payload
    ):
        raise StravaUpstreamError("Malformed Strava token refresh response")

    new_access = str(payload["access_token"])
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    expires_epoch = int(payload["expires_at"])

    conn.access_token_enc = encrypt_secret(new_access, key, aad=aad)
    conn.refresh_token_enc = encrypt_secret(new_refresh, key, aad=aad)
    conn.expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC)
    db.commit()
    return new_access


def sync_strava_connection(
    *,
    db: Session,
    store: ObjectStore,
    settings: Settings,
    connection_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
    http_client_factory: Callable[[], httpx.Client] | None = None,
    now: datetime | None = None,
    lookback_days: int = 7,
    max_pages: int = 5,
    per_page: int = 50,
) -> dict[str, Any]:
    """Incrementally sync new Strava GPS activities for one active user connection."""
    conn_uuid = uuid.UUID(str(connection_id))
    user_uuid = uuid.UUID(str(user_id))
    client_factory = http_client_factory or _default_sync_http_client
    current_time = ensure_utc(now) if now is not None else datetime.now(UTC)

    conn = db.scalar(
        select(OAuthConnection)
        .join(User, OAuthConnection.user_id == User.id)
        .where(
            OAuthConnection.id == conn_uuid,
            OAuthConnection.user_id == user_uuid,
            OAuthConnection.provider == "strava",
            User.is_active.is_(True),
        )
    )
    if conn is None:
        return {
            "connection_id": str(conn_uuid),
            "fetched": 0,
            "skipped_duplicate": 0,
            "skipped_no_gps": 0,
            "ingested_count": 0,
            "failed_count": 0,
        }

    access_token = _refresh_access_token_if_needed(
        db=db,
        conn=conn,
        settings=settings,
        user_id=user_uuid,
        client_factory=client_factory,
        current_time=current_time,
    )
    if access_token is None:
        return {
            "connection_id": str(conn_uuid),
            "fetched": 0,
            "skipped_duplicate": 0,
            "skipped_no_gps": 0,
            "ingested_count": 0,
            "failed_count": 0,
        }

    latest_raw = db.scalar(
        select(func.max(Stage.started_at))
        .join(Project, Stage.project_id == Project.id)
        .where(
            Project.owner_user_id == user_uuid,
            Stage.source_type == "strava",
        )
    )
    latest_started_at = _coerce_utc_datetime(latest_raw)
    if latest_started_at is not None:
        after_dt = latest_started_at - timedelta(hours=24)
    else:
        after_dt = current_time - timedelta(days=lookback_days)
    after_epoch = max(0, int(after_dt.timestamp()))

    existing_ids: set[int] = {
        int(aid)
        for aid in db.scalars(
            select(Stage.strava_activity_id)
            .join(Project, Stage.project_id == Project.id)
            .where(
                Project.owner_user_id == user_uuid,
                Stage.strava_activity_id.is_not(None),
            )
        ).all()
        if aid is not None
    }

    auth_headers = {"Authorization": f"Bearer {access_token}"}
    listed_items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        list_resp = _strava_sync_request(
            client_factory,
            settings,
            "GET",
            "/api/v3/athlete/activities",
            params={"page": page, "per_page": per_page, "after": after_epoch},
            headers=auth_headers,
        )
        if list_resp.status_code != 200:
            raise StravaUpstreamError(
                f"Failed to list Strava activities (status={list_resp.status_code})"
            )
        page_payload = list_resp.json()
        if not isinstance(page_payload, list):
            raise StravaUpstreamError("Unexpected Strava activities payload")
        if not page_payload:
            break
        for entry in page_payload:
            if isinstance(entry, dict):
                listed_items.append(entry)
        if len(page_payload) < per_page:
            break

    fetched = len(listed_items)
    skipped_duplicate = 0
    skipped_no_gps = 0
    candidates: list[int] = []

    for item in listed_items:
        raw_id = item.get("id")
        if raw_id is None:
            continue
        activity_id = int(raw_id)
        if activity_id in existing_ids:
            skipped_duplicate += 1
            continue
        if not item.get("start_latlng"):
            skipped_no_gps += 1
            continue
        existing_ids.add(activity_id)
        candidates.append(activity_id)

    ingested_count = 0
    failed_count = 0

    if not candidates:
        return {
            "connection_id": str(conn_uuid),
            "fetched": fetched,
            "skipped_duplicate": skipped_duplicate,
            "skipped_no_gps": skipped_no_gps,
            "ingested_count": 0,
            "failed_count": 0,
        }

    target_project = _resolve_or_create_autosync_project(db, user_uuid)

    for activity_id in candidates:
        activity_resp = _strava_sync_request(
            client_factory,
            settings,
            "GET",
            f"/api/v3/activities/{activity_id}",
            headers=auth_headers,
        )
        if activity_resp.status_code != 200:
            failed_count += 1
            logger.warning(
                "Strava activity detail fetch failed for activity_id=%s status=%s",
                activity_id,
                activity_resp.status_code,
            )
            continue

        streams_resp = _strava_sync_request(
            client_factory,
            settings,
            "GET",
            f"/api/v3/activities/{activity_id}/streams",
            params={
                "keys": (
                    "latlng,time,altitude,heartrate,cadence,watts,"
                    "velocity_smooth,grade_smooth,moving,temp,distance"
                ),
                "key_by_type": "true",
            },
            headers=auth_headers,
        )
        if streams_resp.status_code != 200:
            failed_count += 1
            logger.warning(
                "Strava streams fetch failed for activity_id=%s status=%s",
                activity_id,
                streams_resp.status_code,
            )
            continue

        activity_json = activity_resp.json()
        streams_json = streams_resp.json()
        if not isinstance(activity_json, dict) or not isinstance(streams_json, dict):
            failed_count += 1
            continue

        try:
            parsed = parse_strava_streams(
                streams_json,
                start_date=activity_json.get("start_date"),
                name=str(activity_json.get("name") or "Strava Activity"),
            )
            raw_bronze = json.dumps(
                {"activity": activity_json, "streams": streams_json},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

            raw_sport = activity_json.get("sport_type") or activity_json.get("type")
            resolved_activity = infer_activity_type_from_strava(
                str(raw_sport) if raw_sport is not None else None,
                parsed.name,
            )
            stage = ingest_parsed_track(
                db=db,
                store=store,
                project=target_project,
                parsed=parsed,
                raw_bronze_bytes=raw_bronze,
                bronze_ext="json",
                bronze_content_type="application/json",
                source_type="strava",
                stage_name=parsed.name or f"Strava {activity_id}",
                day_index=1,
                activity_type=resolved_activity,
                external_source_id=str(activity_id),
                strava_activity_id=activity_id,
            )
            write_audit_event(
                db,
                user_id=user_uuid,
                project_id=target_project.id,
                event_type="stage.strava_auto_synced",
                metadata={
                    "stage_id": str(stage.id),
                    "strava_activity_id": activity_id,
                    "activity_type": stage.activity_type,
                },
            )
            db.commit()
            ingested_count += 1
        except ValueError as exc:
            db.rollback()
            failed_count += 1
            logger.warning(
                "Skipped Strava activity_id=%s during parsing/ingest: %s",
                activity_id,
                exc,
            )

    return {
        "connection_id": str(conn_uuid),
        "fetched": fetched,
        "skipped_duplicate": skipped_duplicate,
        "skipped_no_gps": skipped_no_gps,
        "ingested_count": ingested_count,
        "failed_count": failed_count,
    }


def summarize_strava_sync(results: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Aggregate per-connection Strava sync metrics into a single sanitized XCom summary."""
    connections_processed = 0
    fetched = 0
    skipped_duplicate = 0
    skipped_no_gps = 0
    ingested_count = 0
    failed_count = 0

    for item in results:
        connections_processed += 1
        fetched += int(item.get("fetched", 0))
        skipped_duplicate += int(item.get("skipped_duplicate", 0))
        skipped_no_gps += int(item.get("skipped_no_gps", 0))
        ingested_count += int(item.get("ingested_count", 0))
        failed_count += int(item.get("failed_count", 0))

    return {
        "connections_processed": connections_processed,
        "fetched": fetched,
        "skipped_duplicate": skipped_duplicate,
        "skipped_no_gps": skipped_no_gps,
        "ingested_count": ingested_count,
        "failed_count": failed_count,
    }
