"""Strava OAuth2 PKCE + state integration, webhook, activity listing, and stream import."""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from trip_telemetry import ActivityType, infer_activity_type_from_strava, parse_strava_streams

from trip_api.config import Settings
from trip_api.db import AuditEvent, IngestJob, OAuthConnection, OAuthState, Project, Stage
from trip_api.deps import (
    AppSettings,
    CsrfAuth,
    CurrentAuth,
    DbSession,
    StoreDep,
    ensure_utc,
    get_client_ip,
)
from trip_api.ingest import (
    _find_existing_autosync_project,
    _is_rate_limited,
    _resolve_or_create_autosync_project,
)
from trip_api.rate_limit import check_rate_limit
from trip_api.schemas import (
    StageOut,
    StravaActivityOut,
    StravaBatchImportFailure,
    StravaBatchImportOut,
    StravaBatchImportRequest,
    StravaCallbackOut,
    StravaConnectOut,
    StravaDisconnectOut,
    StravaImportAllOut,
    StravaImportAllRequest,
    StravaImportRequest,
    StravaImportStatusOut,
)
from trip_api.security import (
    SsrfBlockedError,
    decrypt_secret,
    encrypt_secret,
    generate_opaque_token,
    generate_pkce_pair,
    hash_session_token,
    validate_outbound_url,
)
from trip_api.services import (
    get_owned_project,
    ingest_parsed_track,
    stage_to_out,
    write_audit_event,
)
from trip_api.storage import ObjectStore

router = APIRouter(tags=["strava"])
logger = logging.getLogger(__name__)


def _oauth_aad(user_id: uuid.UUID) -> bytes:
    return f"oauth:{user_id}:strava".encode()


def _pkce_aad(user_id: uuid.UUID, session_id: uuid.UUID) -> bytes:
    return f"pkce:{user_id}:{session_id}".encode()


def _get_http_client_factory(request: Request) -> Callable[[], httpx.AsyncClient]:
    factory = getattr(request.app.state, "http_client_factory", None)
    if factory is not None:
        return cast(Callable[[], httpx.AsyncClient], factory)
    return lambda: httpx.AsyncClient(timeout=15.0, follow_redirects=False)


async def _strava_request(
    client_factory: Callable[[], httpx.AsyncClient],
    settings: Settings,
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    base = settings.strava_api_base_url.rstrip("/")
    url = f"{base}{path}"
    try:
        validate_outbound_url(url, allowed_hosts=settings.strava_allowed_hosts, resolve_dns=True)
    except SsrfBlockedError as exc:
        logger.warning("Blocked outbound Strava URL %s: %s", url, exc)
        raise HTTPException(status_code=502, detail="Blocked outbound integration URL") from exc

    async with client_factory() as client:
        try:
            resp = await client.request(method, url, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("Strava HTTP request failed: %s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Strava upstream request failed") from exc

    if 300 <= resp.status_code < 400:
        raise HTTPException(status_code=502, detail="Unexpected redirect from Strava upstream")
    return resp


async def _get_valid_access_token(
    *,
    db: Session,
    settings: Settings,
    user_id: uuid.UUID,
    client_factory: Callable[[], httpx.AsyncClient],
) -> str:
    conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == user_id,
            OAuthConnection.provider == "strava",
        )
    )
    if conn is None:
        raise HTTPException(status_code=400, detail="Strava account is not connected")

    key = settings.decoded_encryption_key
    aad = _oauth_aad(user_id)

    if ensure_utc(conn.expires_at) > datetime.now(UTC) + timedelta(seconds=60):
        return decrypt_secret(conn.access_token_enc, key, aad=aad)

    is_postgres = db.get_bind().dialect.name == "postgresql"
    stmt = (
        select(OAuthConnection)
        .where(OAuthConnection.id == conn.id)
        .execution_options(populate_existing=True)
    )
    if is_postgres:
        stmt = stmt.with_for_update()
    locked_conn = db.scalar(stmt)
    if locked_conn is None:
        raise HTTPException(status_code=400, detail="Strava account is not connected")

    if ensure_utc(locked_conn.expires_at) > datetime.now(UTC) + timedelta(seconds=60):
        return decrypt_secret(locked_conn.access_token_enc, key, aad=aad)

    refresh_token = decrypt_secret(locked_conn.refresh_token_enc, key, aad=aad)
    resp = await _strava_request(
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
        raise HTTPException(status_code=502, detail="Failed to refresh Strava access token")

    payload = resp.json()
    new_access = str(payload["access_token"])
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    expires_epoch = int(payload["expires_at"])

    locked_conn.access_token_enc = encrypt_secret(new_access, key, aad=aad)
    locked_conn.refresh_token_enc = encrypt_secret(new_refresh, key, aad=aad)
    locked_conn.expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC)
    db.commit()
    return new_access


@router.get("/api/v1/strava/connect", response_model=StravaConnectOut)
def strava_connect(
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
) -> StravaConnectOut:
    if not settings.strava_client_id or not settings.strava_client_secret:
        raise HTTPException(status_code=503, detail="Strava integration is not configured")

    verifier, challenge = generate_pkce_pair()
    raw_state = generate_opaque_token(32)
    state_hash = hash_session_token(raw_state, settings.session_secret)
    verifier_enc = encrypt_secret(
        verifier,
        settings.decoded_encryption_key,
        aad=_pkce_aad(auth.user.id, auth.session.id),
    )

    oauth_state = OAuthState(
        user_id=auth.user.id,
        session_id=auth.session.id,
        provider="strava",
        state_hash=state_hash,
        code_verifier_enc=verifier_enc,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db.add(oauth_state)
    db.commit()

    base = settings.strava_api_base_url.rstrip("/")
    params = urlencode(
        {
            "client_id": settings.strava_client_id,
            "redirect_uri": settings.strava_redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": "read,activity:read_all",
            "state": raw_state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    authorize_url = f"{base}/oauth/authorize?{params}"
    try:
        validate_outbound_url(
            authorize_url,
            allowed_hosts=settings.strava_allowed_hosts,
            resolve_dns=False,
        )
    except SsrfBlockedError as exc:
        raise HTTPException(status_code=502, detail="Blocked Strava authorization URL") from exc

    return StravaConnectOut(authorize_url=authorize_url, state=raw_state)


@router.get("/api/v1/strava/callback", response_model=StravaCallbackOut)
async def strava_callback(
    request: Request,
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> StravaCallbackOut:
    if error:
        raise HTTPException(status_code=400, detail="Strava authorization was denied")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code or state")

    now = datetime.now(UTC)
    state_hash = hash_session_token(state, settings.session_secret)
    consume_res = cast(
        CursorResult[Any],
        db.execute(
            update(OAuthState)
            .where(
                OAuthState.state_hash == state_hash,
                OAuthState.user_id == auth.user.id,
                OAuthState.session_id == auth.session.id,
                OAuthState.provider == "strava",
                OAuthState.consumed_at.is_(None),
                OAuthState.expires_at > now,
            )
            .values(consumed_at=now)
        ),
    )
    if (consume_res.rowcount or 0) != 1:
        db.rollback()
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    db.commit()

    oauth_state = db.scalar(
        select(OAuthState).where(
            OAuthState.state_hash == state_hash,
            OAuthState.user_id == auth.user.id,
            OAuthState.session_id == auth.session.id,
        )
    )
    if oauth_state is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    key = settings.decoded_encryption_key
    code_verifier = decrypt_secret(
        oauth_state.code_verifier_enc,
        key,
        aad=_pkce_aad(auth.user.id, auth.session.id),
    )

    client_factory = _get_http_client_factory(request)
    resp = await _strava_request(
        client_factory,
        settings,
        "POST",
        "/oauth/token",
        data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Strava token exchange failed")

    payload = resp.json()
    athlete_id = str((payload.get("athlete") or {}).get("id") or "")
    if not athlete_id:
        raise HTTPException(status_code=502, detail="Strava response missing athlete ID")

    aad = _oauth_aad(auth.user.id)
    access_enc = encrypt_secret(str(payload["access_token"]), key, aad=aad)
    refresh_enc = encrypt_secret(str(payload["refresh_token"]), key, aad=aad)
    expires_at = datetime.fromtimestamp(int(payload["expires_at"]), tz=UTC)
    scope = str(payload.get("scope") or "read,activity:read_all")[:256]

    existing = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == auth.user.id,
            OAuthConnection.provider == "strava",
        )
    )
    if existing is None:
        conn = OAuthConnection(
            user_id=auth.user.id,
            provider="strava",
            external_athlete_id=athlete_id,
            access_token_enc=access_enc,
            refresh_token_enc=refresh_enc,
            expires_at=expires_at,
            scope=scope,
        )
        db.add(conn)
    else:
        existing.external_athlete_id = athlete_id
        existing.access_token_enc = access_enc
        existing.refresh_token_enc = refresh_enc
        existing.expires_at = expires_at
        existing.scope = scope

    db.add(
        AuditEvent(
            user_id=auth.user.id,
            event_type="strava.connected",
            metadata_json={"athlete_id": athlete_id},
        )
    )
    db.commit()
    return StravaCallbackOut(connected=True, athlete_id=athlete_id, scope=scope)


@router.post("/api/v1/strava/disconnect", response_model=StravaDisconnectOut)
async def strava_disconnect(
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
) -> StravaDisconnectOut:
    conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == auth.user.id,
            OAuthConnection.provider == "strava",
        )
    )
    if conn is not None:
        try:
            token = decrypt_secret(
                conn.access_token_enc,
                settings.decoded_encryption_key,
                aad=_oauth_aad(auth.user.id),
            )
            await _strava_request(
                _get_http_client_factory(request),
                settings,
                "POST",
                "/oauth/deauthorize",
                data={"access_token": token},
            )
        except Exception:
            logger.warning("Best-effort Strava upstream deauthorization skipped or failed")
        db.delete(conn)
        db.add(AuditEvent(user_id=auth.user.id, event_type="strava.disconnected", metadata_json={}))
        db.commit()

    return StravaDisconnectOut(connected=False)


@router.get("/api/v1/strava/import-status", response_model=StravaImportStatusOut)
@router.get("/api/v1/strava/status", response_model=StravaImportStatusOut)
def strava_import_status(
    auth: CurrentAuth,
    db: DbSession,
) -> StravaImportStatusOut:
    conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == auth.user.id,
            OAuthConnection.provider == "strava",
        )
    )
    imported_stages_count = int(
        db.scalar(
            select(func.count(Stage.id))
            .join(Project, Stage.project_id == Project.id)
            .where(
                Project.owner_user_id == auth.user.id,
                (Stage.source_type == "strava") | Stage.strava_activity_id.is_not(None),
            )
        )
        or 0
    )

    if conn is None:
        return StravaImportStatusOut(
            connected=False,
            imported_stages_count=imported_stages_count,
        )

    status_rows = db.execute(
        select(IngestJob.status, func.count(IngestJob.id))
        .where(
            IngestJob.provider == "strava",
            IngestJob.external_owner_id == conn.external_athlete_id,
        )
        .group_by(IngestJob.status)
    ).all()
    counts = {str(status): int(cnt) for status, cnt in status_rows}
    queued_jobs = counts.get("queued", 0)
    running_jobs = counts.get("running", 0)
    succeeded_jobs = counts.get("succeeded", 0)
    failed_jobs = counts.get("failed", 0)
    ignored_jobs = counts.get("ignored", 0)

    last_error = db.scalar(
        select(IngestJob.error_message)
        .where(
            IngestJob.provider == "strava",
            IngestJob.external_owner_id == conn.external_athlete_id,
            IngestJob.status == "failed",
            IngestJob.error_message.is_not(None),
        )
        .order_by(IngestJob.updated_at.desc(), IngestJob.created_at.desc())
        .limit(1)
    )

    return StravaImportStatusOut(
        connected=True,
        athlete_id=conn.external_athlete_id,
        scope=conn.scope,
        pending_jobs=queued_jobs + running_jobs,
        queued_jobs=queued_jobs,
        running_jobs=running_jobs,
        succeeded_jobs=succeeded_jobs,
        failed_jobs=failed_jobs,
        ignored_jobs=ignored_jobs,
        imported_stages_count=imported_stages_count,
        last_error=last_error,
    )


@router.post("/api/v1/strava/import-all", response_model=StravaImportAllOut)
async def strava_import_all(
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    body: StravaImportAllRequest | None = None,
) -> StravaImportAllOut:
    req_body = body or StravaImportAllRequest()

    after_epoch = (
        _parse_strava_epoch(req_body.after, end_of_day=False)
        if req_body.after is not None
        else None
    )
    before_epoch = (
        _parse_strava_epoch(req_body.before, end_of_day=True)
        if req_body.before is not None
        else None
    )
    if after_epoch is not None and before_epoch is not None and after_epoch > before_epoch:
        raise HTTPException(status_code=422, detail="'after' filter must be <= 'before' filter")

    explicit_project = (
        get_owned_project(db, req_body.project_id, auth.user.id)
        if req_body.project_id is not None
        else None
    )

    check_rate_limit(
        db,
        bucket_key=f"strava_import:{auth.user.id}",
        limit=settings.rate_limit_strava_import_max,
        window_seconds=settings.rate_limit_strava_import_window_s,
    )

    client_factory = _get_http_client_factory(request)
    access_token = await _get_valid_access_token(
        db=db,
        settings=settings,
        user_id=auth.user.id,
        client_factory=client_factory,
    )
    conn = db.scalar(
        select(OAuthConnection).where(
            OAuthConnection.user_id == auth.user.id,
            OAuthConnection.provider == "strava",
        )
    )
    if conn is None:
        raise HTTPException(status_code=400, detail="Strava account is not connected")

    existing_activity_ids: set[int] = set()
    stage_rows = db.execute(
        select(Stage.strava_activity_id, Stage.external_source_id, Stage.source_type)
        .join(Project, Stage.project_id == Project.id)
        .where(
            Project.owner_user_id == auth.user.id,
            Stage.strava_activity_id.is_not(None) | (Stage.source_type == "strava"),
        )
    ).all()
    for strava_id, ext_id, source_type in stage_rows:
        if strava_id is not None:
            existing_activity_ids.add(int(strava_id))
        if (
            source_type == "strava"
            and isinstance(ext_id, str)
            and ext_id.isascii()
            and ext_id.isdigit()
        ):
            existing_activity_ids.add(int(ext_id))

    active_job_ids = db.scalars(
        select(IngestJob.external_object_id).where(
            IngestJob.provider == "strava",
            IngestJob.external_owner_id == conn.external_athlete_id,
            IngestJob.status.in_(("queued", "running")),
        )
    ).all()
    for job_obj_id in active_job_ids:
        existing_activity_ids.add(int(job_obj_id))

    total_scanned = 0
    skipped_existing_count = 0
    skipped_no_gps_count = 0
    to_queue: list[tuple[int, dict[str, Any]]] = []

    page = 1
    while total_scanned < req_body.max_activities:
        per_page = min(100, req_body.max_activities - total_scanned)
        params: dict[str, int] = {"page": page, "per_page": per_page}
        if after_epoch is not None:
            params["after"] = after_epoch
        if before_epoch is not None:
            params["before"] = before_epoch

        resp = await _strava_request(
            client_factory,
            settings,
            "GET",
            "/api/v3/athlete/activities",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code == 429:
            if page == 1:
                raise HTTPException(
                    status_code=429,
                    detail="Strava upstream rate limit exceeded",
                )
            break
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch Strava activities")

        items = resp.json()
        if not isinstance(items, list):
            raise HTTPException(status_code=502, detail="Failed to fetch Strava activities")

        page_scanned = 0
        for item in items:
            if total_scanned >= req_body.max_activities:
                break
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
                continue

            total_scanned += 1
            page_scanned += 1
            act_id = int(raw_id)

            if req_body.gps_only and not bool(item.get("start_latlng")):
                skipped_no_gps_count += 1
                continue
            if act_id in existing_activity_ids:
                skipped_existing_count += 1
                continue

            existing_activity_ids.add(act_id)
            to_queue.append((act_id, item))

        if len(items) < per_page or page_scanned == 0 or _is_rate_limited(resp.headers):
            break
        page += 1

    target_project: Project | None
    if explicit_project is not None:
        target_project = explicit_project
    elif to_queue:
        target_project = _resolve_or_create_autosync_project(db, auth.user.id)
    else:
        target_project = _find_existing_autosync_project(db, auth.user.id)

    queued_activity_ids: list[int] = []
    if to_queue:
        assert target_project is not None
        for act_id, item in to_queue:
            db.add(
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id=conn.external_athlete_id,
                    external_object_id=act_id,
                    status="queued",
                    payload_json={
                        "source": "strava_bulk_import",
                        "user_id": str(auth.user.id),
                        "project_id": str(target_project.id),
                        "name": str(item.get("name") or f"Strava {act_id}")[:200],
                        "sport_type": str(item.get("sport_type") or item.get("type") or "Workout"),
                        "start_date": str(item.get("start_date") or ""),
                    },
                )
            )
            queued_activity_ids.append(act_id)

    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=target_project.id if target_project is not None else None,
        event_type="strava.bulk_import_queued",
        metadata={
            "queued_count": len(queued_activity_ids),
            "skipped_existing_count": skipped_existing_count,
            "skipped_no_gps_count": skipped_no_gps_count,
            "total_scanned": total_scanned,
            "project_id": str(target_project.id) if target_project is not None else None,
        },
    )
    db.commit()

    return StravaImportAllOut(
        queued_count=len(queued_activity_ids),
        skipped_existing_count=skipped_existing_count,
        skipped_no_gps_count=skipped_no_gps_count,
        total_scanned=total_scanned,
        project_id=target_project.id if target_project is not None else None,
        project_title=target_project.title if target_project is not None else None,
        queued_activity_ids=queued_activity_ids,
    )


@router.get("/api/v1/strava/webhook")
def strava_webhook_verify(
    hub_mode: Annotated[str, Query(alias="hub.mode")],
    hub_challenge: Annotated[str, Query(alias="hub.challenge")],
    hub_verify_token: Annotated[str, Query(alias="hub.verify_token")],
    settings: AppSettings,
) -> dict[str, str]:
    expected = settings.strava_verify_token
    if (
        not expected
        or hub_mode != "subscribe"
        or not hmac.compare_digest(expected.encode("utf-8"), hub_verify_token.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    return {"hub.challenge": hub_challenge}


@router.post("/api/v1/strava/webhook")
def strava_webhook_event(
    payload: dict[str, Any],
    request: Request,
    db: DbSession,
    settings: AppSettings,
    verify_token: Annotated[str | None, Query()] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    token: Annotated[str | None, Query()] = None,
    x_strava_verify_token: Annotated[str | None, Header(alias="X-Strava-Verify-Token")] = None,
) -> dict[str, str]:
    client_ip = get_client_ip(request)
    check_rate_limit(
        db,
        bucket_key=f"strava_webhook:{client_ip}",
        limit=settings.rate_limit_strava_webhook_max,
        window_seconds=settings.rate_limit_strava_webhook_window_s,
    )

    expected_sub_id = settings.strava_webhook_subscription_id
    expected_token = settings.strava_verify_token
    if expected_sub_id is None and not expected_token:
        raise HTTPException(status_code=403, detail="Webhook verification is not configured")

    if expected_sub_id is not None:
        raw_sub_id = payload.get("subscription_id")
        if (
            not isinstance(raw_sub_id, int)
            or isinstance(raw_sub_id, bool)
            or raw_sub_id != expected_sub_id
        ):
            raise HTTPException(status_code=403, detail="Webhook subscription verification failed")

    provided_token = next(
        (
            candidate
            for candidate in (x_strava_verify_token, verify_token, hub_verify_token, token)
            if candidate is not None
        ),
        None,
    )
    if expected_token:
        if provided_token is None or not hmac.compare_digest(
            expected_token.encode("utf-8"), provided_token.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="Webhook verification failed")

    object_type = payload.get("object_type")
    aspect_type = payload.get("aspect_type")
    updates = payload.get("updates")
    owner_id = payload.get("owner_id")

    if (
        object_type == "athlete"
        and aspect_type == "update"
        and isinstance(updates, dict)
        and str(updates.get("authorized", "")).lower() == "false"
        and isinstance(owner_id, (int, str))
        and not isinstance(owner_id, bool)
        and str(owner_id).strip()
    ):
        athlete_id = str(owner_id).strip()
        db.execute(
            delete(OAuthConnection).where(
                OAuthConnection.provider == "strava",
                OAuthConnection.external_athlete_id == athlete_id,
            )
        )
        db.add(
            AuditEvent(
                user_id=None,
                event_type="strava.webhook_deauthorize",
                metadata_json={"athlete_id": athlete_id},
            )
        )
        db.commit()
    elif (
        object_type == "activity"
        and aspect_type in {"create", "update", "delete"}
        and isinstance(owner_id, (int, str))
        and not isinstance(owner_id, bool)
        and str(owner_id).strip()
        and isinstance(payload.get("object_id"), int)
        and not isinstance(payload.get("object_id"), bool)
        and int(payload["object_id"]) > 0
    ):
        db.add(
            IngestJob(
                provider="strava",
                event_type=str(aspect_type),
                external_owner_id=str(owner_id).strip(),
                external_object_id=int(payload["object_id"]),
                payload_json=dict(payload),
                status="queued",
            )
        )
        db.commit()

    return {"status": "ok"}


def _parse_strava_epoch(raw_value: str, *, end_of_day: bool) -> int:
    text = raw_value.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Empty date filter value")
    try:
        if text.isascii() and text.isdigit():
            epoch = int(text)
        elif len(text) == 10:
            parsed_date = date.fromisoformat(text)
            boundary_time = (
                time(23, 59, 59, tzinfo=UTC) if end_of_day else time(0, 0, 0, tzinfo=UTC)
            )
            epoch = int(datetime.combine(parsed_date, boundary_time).timestamp())
        else:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            epoch = int(dt.astimezone(UTC).timestamp())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid date filter; expected YYYY-MM-DD, ISO-8601, or UNIX epoch seconds",
        ) from exc
    if epoch < 0:
        raise HTTPException(status_code=422, detail="Date filter timestamp must be non-negative")
    return epoch


@router.get("/api/v1/strava/activities", response_model=list[StravaActivityOut])
async def strava_list_activities(
    request: Request,
    auth: CurrentAuth,
    db: DbSession,
    settings: AppSettings,
    page: Annotated[int, Query(ge=1, le=50)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 30,
    after: str | None = None,
    before: str | None = None,
) -> list[StravaActivityOut]:
    params: dict[str, int] = {"page": page, "per_page": per_page}
    after_epoch = _parse_strava_epoch(after, end_of_day=False) if after is not None else None
    before_epoch = _parse_strava_epoch(before, end_of_day=True) if before is not None else None
    if after_epoch is not None and before_epoch is not None and after_epoch > before_epoch:
        raise HTTPException(status_code=422, detail="'after' filter must be <= 'before' filter")
    if after_epoch is not None:
        params["after"] = after_epoch
    if before_epoch is not None:
        params["before"] = before_epoch

    client_factory = _get_http_client_factory(request)
    access_token = await _get_valid_access_token(
        db=db,
        settings=settings,
        user_id=auth.user.id,
        client_factory=client_factory,
    )
    resp = await _strava_request(
        client_factory,
        settings,
        "GET",
        "/api/v3/athlete/activities",
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch Strava activities")

    items = resp.json()
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="Unexpected Strava activities payload")

    return [
        StravaActivityOut(
            id=int(item["id"]),
            name=str(item.get("name") or "Activity"),
            sport_type=str(item.get("sport_type") or item.get("type") or "Workout"),
            start_date=str(item.get("start_date") or ""),
            distance_m=float(item.get("distance") or 0.0),
            elevation_gain_m=float(item.get("total_elevation_gain") or 0.0),
            moving_time_s=int(item.get("moving_time") or 0),
            has_gps=bool(item.get("start_latlng")),
            summary_polyline=(
                raw_poly
                if isinstance(
                    raw_poly := (
                        item.get("map", {}).get("summary_polyline")
                        if isinstance(item.get("map"), dict)
                        else None
                    ),
                    str,
                )
                and raw_poly
                else None
            ),
        )
        for item in items
    ]


async def _import_single_strava_activity(
    *,
    client_factory: Callable[[], httpx.AsyncClient],
    access_token: str,
    settings: Settings,
    db: Session,
    store: ObjectStore,
    project: Project,
    activity_id: int,
    day_index: int,
    custom_name: str | None,
    activity_type: ActivityType | None,
) -> Stage:
    existing_stage_id = db.scalar(
        select(Stage.id).where(
            Stage.project_id == project.id,
            Stage.strava_activity_id == activity_id,
        )
    )
    if existing_stage_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This Strava activity has already been imported into this project",
        )

    headers = {"Authorization": f"Bearer {access_token}"}

    activity_resp = await _strava_request(
        client_factory,
        settings,
        "GET",
        f"/api/v3/activities/{activity_id}",
        headers=headers,
    )
    if activity_resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Strava activity not found")
    if activity_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch Strava activity")

    streams_resp = await _strava_request(
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
        headers=headers,
    )
    if streams_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch Strava activity streams")

    activity_json = activity_resp.json()
    streams_json = streams_resp.json()
    if not isinstance(activity_json, dict) or not isinstance(streams_json, dict):
        raise HTTPException(status_code=502, detail="Malformed Strava upstream payload")

    try:
        parsed = parse_strava_streams(
            streams_json,
            start_date=activity_json.get("start_date"),
            name=str(activity_json.get("name") or "Strava Activity"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    raw_bronze = json.dumps(
        {"activity": activity_json, "streams": streams_json},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    stage_name = (custom_name.strip() if custom_name else "") or parsed.name
    raw_sport = activity_json.get("sport_type") or activity_json.get("type")
    resolved_activity = activity_type or infer_activity_type_from_strava(
        str(raw_sport) if raw_sport is not None else None,
        stage_name,
    )

    return ingest_parsed_track(
        db=db,
        store=store,
        project=project,
        parsed=parsed,
        raw_bronze_bytes=raw_bronze,
        bronze_ext="json",
        bronze_content_type="application/json",
        source_type="strava",
        source_format="strava",
        stage_name=stage_name,
        day_index=day_index,
        activity_type=resolved_activity,
        external_source_id=str(activity_id),
        strava_activity_id=activity_id,
    )


@router.post(
    "/api/v1/projects/{project_id}/import-strava",
    response_model=StageOut,
    status_code=201,
)
async def import_strava_activity(
    project_id: uuid.UUID,
    body: StravaImportRequest,
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
) -> StageOut:
    project = get_owned_project(db, project_id, auth.user.id)
    check_rate_limit(
        db,
        bucket_key=f"strava_import:{auth.user.id}",
        limit=settings.rate_limit_strava_import_max,
        window_seconds=settings.rate_limit_strava_import_window_s,
    )

    client_factory = _get_http_client_factory(request)
    access_token = await _get_valid_access_token(
        db=db,
        settings=settings,
        user_id=auth.user.id,
        client_factory=client_factory,
    )
    stage = await _import_single_strava_activity(
        client_factory=client_factory,
        access_token=access_token,
        settings=settings,
        db=db,
        store=store,
        project=project,
        activity_id=body.activity_id,
        day_index=body.day_index,
        custom_name=body.name,
        activity_type=body.activity_type,
    )
    write_audit_event(
        db,
        user_id=auth.user.id,
        project_id=project.id,
        event_type="stage.strava_imported",
        metadata={
            "stage_id": str(stage.id),
            "strava_activity_id": body.activity_id,
            "activity_type": stage.activity_type,
            "name": stage.name,
        },
    )
    db.commit()
    return stage_to_out(stage)


@router.post(
    "/api/v1/projects/{project_id}/import-strava/batch",
    response_model=StravaBatchImportOut,
)
async def import_strava_activities_batch(
    project_id: uuid.UUID,
    body: StravaBatchImportRequest,
    request: Request,
    auth: CsrfAuth,
    db: DbSession,
    settings: AppSettings,
    store: StoreDep,
) -> StravaBatchImportOut:
    project = get_owned_project(db, project_id, auth.user.id)
    check_rate_limit(
        db,
        bucket_key=f"strava_import:{auth.user.id}",
        limit=settings.rate_limit_strava_import_max,
        window_seconds=settings.rate_limit_strava_import_window_s,
        cost=len(body.items),
    )

    client_factory = _get_http_client_factory(request)
    access_token = await _get_valid_access_token(
        db=db,
        settings=settings,
        user_id=auth.user.id,
        client_factory=client_factory,
    )

    imported: list[StageOut] = []
    failed: list[StravaBatchImportFailure] = []
    seen_activity_ids: set[int] = set()
    for item in body.items:
        if item.activity_id in seen_activity_ids:
            failed.append(
                StravaBatchImportFailure(
                    activity_id=item.activity_id,
                    detail="Duplicate Strava activity in batch request",
                )
            )
            continue
        seen_activity_ids.add(item.activity_id)
        resolved_custom_name = item.name or item.custom_name
        try:
            stage = await _import_single_strava_activity(
                client_factory=client_factory,
                access_token=access_token,
                settings=settings,
                db=db,
                store=store,
                project=project,
                activity_id=item.activity_id,
                day_index=item.day_index,
                custom_name=resolved_custom_name,
                activity_type=item.activity_type,
            )
            imported.append(stage_to_out(stage))
        except HTTPException as exc:
            failed.append(
                StravaBatchImportFailure(
                    activity_id=item.activity_id,
                    detail=str(exc.detail),
                )
            )
        except Exception:
            failed.append(
                StravaBatchImportFailure(
                    activity_id=item.activity_id,
                    detail="Failed to import Strava activity",
                )
            )

    if imported:
        write_audit_event(
            db,
            user_id=auth.user.id,
            project_id=project.id,
            event_type="stage.strava_batch_imported",
            metadata={
                "imported_count": len(imported),
                "failed_count": len(failed),
                "activity_ids": [s.strava_activity_id for s in imported],
            },
        )
        db.commit()

    return StravaBatchImportOut(imported=imported, failed=failed)
