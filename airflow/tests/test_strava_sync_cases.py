"""Comprehensive unit and integration tests for Strava incremental sync in Airflow."""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select
from trip_airflow.strava_sync import (
    StravaUpstreamError,
    summarize_strava_sync,
    sync_strava_connection,
)
from trip_api.db import AuditEvent, OAuthConnection, Project, Stage
from trip_api.security import decrypt_secret, encrypt_secret
from trip_api.services import stage_lod_cache_key

if TYPE_CHECKING:
    from conftest import AirflowTaskHarness

from conftest import fake_public_getaddrinfo


def _create_strava_connection(
    harness: AirflowTaskHarness,
    user_id: uuid.UUID,
    *,
    access_token: str = "initial-access-token",  # noqa: S107
    refresh_token: str = "initial-refresh-token",  # noqa: S107
    expires_at: datetime | None = None,
    athlete_id: str = "athlete-100",
) -> uuid.UUID:
    key = harness.settings.decoded_encryption_key
    aad = f"oauth:{user_id}:strava".encode()
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=2)

    with harness.session_factory() as db:
        conn = OAuthConnection(
            user_id=user_id,
            provider="strava",
            external_athlete_id=athlete_id,
            access_token_enc=encrypt_secret(access_token, key, aad=aad),
            refresh_token_enc=encrypt_secret(refresh_token, key, aad=aad),
            expires_at=expires_at,
            scope="read,activity:read_all",
        )
        db.add(conn)
        db.commit()
        return conn.id


def _sample_valid_streams() -> dict[str, Any]:
    return {
        "latlng": {"data": [[0.12, 0.4], [0.125, 0.405], [0.13, 0.41]]},
        "time": {"data": [0, 60, 120]},
        "altitude": {"data": [1200.0, 1250.0, 1310.0]},
        "heartrate": {"data": [135, 142, 150]},
    }


def test_strava_sync_429_too_many_requests_on_listing_raises(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("rate_limit_list@example.com")
    conn_id = _create_strava_connection(airflow_harness, user.id)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/athlete/activities":
            return httpx.Response(
                429,
                headers={"Retry-After": "900"},
                json={
                    "message": "Rate Limit Exceeded",
                    "errors": [{"field": "rate", "code": "exceeded"}],
                },
            )
        return httpx.Response(404)

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with airflow_harness.session_factory() as db:
        with pytest.raises(StravaUpstreamError, match=r"status=429"):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=airflow_harness.settings,
                connection_id=conn_id,
                user_id=user.id,
                http_client_factory=client_factory,
            )


def test_strava_sync_429_too_many_requests_on_token_refresh_raises(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("rate_limit_token@example.com")
    conn_id = _create_strava_connection(
        airflow_harness,
        user.id,
        expires_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                429,
                headers={"Retry-After": "60"},
                json={"message": "Too Many Requests", "errors": []},
            )
        return httpx.Response(404)

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with airflow_harness.session_factory() as db:
        with pytest.raises(StravaUpstreamError, match=r"status=429"):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=airflow_harness.settings,
                connection_id=conn_id,
                user_id=user.id,
                http_client_factory=client_factory,
            )


def test_strava_sync_429_on_activity_detail_and_streams_resilient(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("rate_limit_items@example.com")
    conn_id = _create_strava_connection(airflow_harness, user.id)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {"id": 401, "name": "Detail 429", "start_latlng": [0.1, 0.2]},
                    {"id": 402, "name": "Streams 429", "start_latlng": [0.3, 0.3]},
                ],
            )
        if path == "/api/v3/activities/401":
            return httpx.Response(
                429,
                headers={"Retry-After": "300"},
                json={"message": "Rate limit exceeded"},
            )
        if path == "/api/v3/activities/402":
            return httpx.Response(
                200,
                json={
                    "id": 402,
                    "name": "Streams 429",
                    "sport_type": "Ride",
                    "start_date": "2001-07-02T10:00:00Z",
                },
            )
        if path == "/api/v3/activities/402/streams":
            return httpx.Response(
                429,
                headers={"Retry-After": "300"},
                json={"message": "Rate limit exceeded on streams"},
            )
        return httpx.Response(404)

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with airflow_harness.session_factory() as db:
        result = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
        )

    assert result == {
        "connection_id": str(conn_id),
        "fetched": 2,
        "skipped_duplicate": 0,
        "skipped_no_gps": 0,
        "ingested_count": 0,
        "failed_count": 2,
    }


def test_strava_sync_expired_token_refresh_lifecycle_and_errors(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("token_refresh_flow@example.com")
    now = datetime.now(UTC)
    key = airflow_harness.settings.decoded_encryption_key

    conn_id = _create_strava_connection(
        airflow_harness,
        user.id,
        access_token="old-acc",
        refresh_token="old-ref",
        expires_at=now - timedelta(seconds=10),
    )

    new_expiry_epoch = int((now + timedelta(hours=6)).timestamp())

    def refresh_ok_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            body = dict(urllib_parse_qsl=request.read().decode())
            assert "client_id" in body["urllib_parse_qsl"]
            assert "refresh_token=old-ref" in body["urllib_parse_qsl"]
            return httpx.Response(
                200,
                json={
                    "access_token": "brand-new-acc",
                    "refresh_token": "brand-new-ref",
                    "expires_at": new_expiry_epoch,
                },
            )
        if request.url.path == "/api/v3/athlete/activities":
            assert request.headers.get("Authorization") == "Bearer brand-new-acc"
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    def client_factory():
        return httpx.Client(
            transport=httpx.MockTransport(refresh_ok_handler), follow_redirects=False
        )

    with airflow_harness.session_factory() as db:
        res = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
            now=now,
        )
        assert res["fetched"] == 0

    with airflow_harness.session_factory() as db:
        conn = db.get(OAuthConnection, conn_id)
        assert conn is not None
        aad = f"oauth:{user.id}:strava".encode()
        assert decrypt_secret(conn.access_token_enc, key, aad=aad) == "brand-new-acc"
        assert decrypt_secret(conn.refresh_token_enc, key, aad=aad) == "brand-new-ref"
        assert conn.expires_at is not None
        assert int(conn.expires_at.replace(tzinfo=UTC).timestamp()) == new_expiry_epoch

    called_refresh = False

    def no_refresh_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_refresh
        if request.url.path == "/oauth/token":
            called_refresh = True
            return httpx.Response(500)
        if request.url.path == "/api/v3/athlete/activities":
            assert request.headers.get("Authorization") == "Bearer brand-new-acc"
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    with airflow_harness.session_factory() as db:
        sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(no_refresh_handler), follow_redirects=False
            ),
            now=now,
        )
    assert not called_refresh

    with airflow_harness.session_factory() as db:
        conn = db.get(OAuthConnection, conn_id)
        assert conn is not None
        conn.expires_at = now - timedelta(minutes=5)
        db.commit()

    def error_401_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(404)

    with airflow_harness.session_factory() as db:
        with pytest.raises(StravaUpstreamError, match="status=401"):
            sync_strava_connection(
                db=db,
                store=airflow_harness.store,
                settings=airflow_harness.settings,
                connection_id=conn_id,
                user_id=user.id,
                http_client_factory=lambda: httpx.Client(
                    transport=httpx.MockTransport(error_401_handler), follow_redirects=False
                ),
                now=now,
            )

    for malformed in [
        {"access_token": "ok-acc"},
        {"expires_at": 1234567890},
        "string-not-dict",
    ]:
        payload = malformed

        def malformed_handler(req: httpx.Request, p: Any = payload) -> httpx.Response:
            return httpx.Response(200, json=p)

        with airflow_harness.session_factory() as db:
            with pytest.raises(StravaUpstreamError, match="Malformed"):
                sync_strava_connection(
                    db=db,
                    store=airflow_harness.store,
                    settings=airflow_harness.settings,
                    connection_id=conn_id,
                    user_id=user.id,
                    http_client_factory=lambda: httpx.Client(
                        transport=httpx.MockTransport(malformed_handler), follow_redirects=False
                    ),
                    now=now,
                )

    unset_settings = airflow_harness.settings.model_copy(update={"strava_client_id": None})
    with airflow_harness.session_factory() as db:
        empty_res = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=unset_settings,
            connection_id=conn_id,
            user_id=user.id,
            now=now,
        )
        assert empty_res["fetched"] == 0
        assert empty_res["ingested_count"] == 0


def test_strava_sync_multi_page_pagination(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("pagination@example.com")
    conn_id = _create_strava_connection(airflow_harness, user.id)

    requested_pages: list[int] = []
    requested_after: list[int] = []

    def pagination_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/athlete/activities":
            page = int(request.url.params.get("page", 1))
            int(request.url.params.get("per_page", 2))
            after_val = int(request.url.params.get("after", 0))
            requested_pages.append(page)
            requested_after.append(after_val)

            if page == 1:
                return httpx.Response(
                    200,
                    json=[
                        {"id": 1001, "name": "Stage 1001", "start_latlng": [0.1, 0.2]},
                        {"id": 1002, "name": "Stage 1002", "start_latlng": [0.11, 0.21]},
                    ],
                )
            if page == 2:
                return httpx.Response(
                    200,
                    json=[
                        {"id": 1003, "name": "Stage 1003", "start_latlng": [0.12, 0.22]},
                        {"id": 1004, "name": "Stage 1004", "start_latlng": [0.13, 0.23]},
                    ],
                )
            if page == 3:
                return httpx.Response(
                    200,
                    json=[
                        {"id": 1005, "name": "Stage 1005", "start_latlng": [0.14, 0.24]},
                    ],
                )
            return httpx.Response(200, json=[])

        if path.startswith("/api/v3/activities/") and not path.endswith("/streams"):
            aid = int(path.split("/")[-1])
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": f"Stage {aid}",
                    "sport_type": "Ride",
                    "start_date": "2001-07-02T08:00:00Z",
                },
            )
        if path.endswith("/streams"):
            return httpx.Response(200, json=_sample_valid_streams())

        return httpx.Response(404)

    def client_factory():
        return httpx.Client(
            transport=httpx.MockTransport(pagination_handler), follow_redirects=False
        )

    with airflow_harness.session_factory() as db:
        res = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
            max_pages=5,
            per_page=2,
            lookback_days=10,
        )

    assert requested_pages == [1, 2, 3]
    assert res["fetched"] == 5
    assert res["ingested_count"] == 5

    expected_after = int((datetime.now(UTC) - timedelta(days=10)).timestamp())
    assert abs(requested_after[0] - expected_after) <= 10

    requested_pages.clear()
    requested_after.clear()

    with airflow_harness.session_factory() as db:
        latest_stage = db.scalar(select(Stage).where(Stage.strava_activity_id == 1005))
        assert latest_stage is not None
        assert latest_stage.started_at is not None
        stage_start_utc = (
            latest_stage.started_at.replace(tzinfo=UTC)
            if latest_stage.started_at.tzinfo is None
            else latest_stage.started_at
        )
        expected_recent_after = max(0, int((stage_start_utc - timedelta(hours=24)).timestamp()))

        sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
            max_pages=2,
            per_page=2,
        )

    assert abs(requested_after[0] - expected_recent_after) <= 5

    requested_pages.clear()
    with airflow_harness.session_factory() as db:
        user2 = airflow_harness.create_user("max_pages_ceiling@example.com")
        conn_id2 = _create_strava_connection(airflow_harness, user2.id)
        res_ceiling = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id2,
            user_id=user2.id,
            http_client_factory=client_factory,
            max_pages=2,
            per_page=2,
        )
    assert requested_pages == [1, 2]
    assert res_ceiling["fetched"] == 4


def test_strava_sync_idempotent_resync_and_duplicate_dedup(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("idempotency@example.com")
    conn_id = _create_strava_connection(airflow_harness, user.id)

    prior_proj = airflow_harness.create_project(user.id, "Prior Bike Tour")
    airflow_harness.ingest_sample_stage(prior_proj, name="Existing 7001", strava_activity_id=7001)

    detail_calls: list[int] = []

    def idempotency_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {"id": 7001, "name": "Already In DB", "start_latlng": [0.1, 0.2]},
                    {"id": 7002, "name": "Indoor Turbo", "start_latlng": None},
                    {"id": 7003, "name": "Empty LatLng", "start_latlng": []},
                    {"id": 7004, "name": "New Valid Ride", "start_latlng": [0.12, 0.22]},
                    {
                        "id": 7004,
                        "name": "Duplicate In Same Payload",
                        "start_latlng": [0.12, 0.22],
                    },
                ],
            )
        if path.startswith("/api/v3/activities/") and not path.endswith("/streams"):
            aid = int(path.split("/")[-1])
            detail_calls.append(aid)
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": f"Activity {aid}",
                    "sport_type": "Ride",
                    "start_date": "2001-07-02T10:00:00Z",
                },
            )
        if path.endswith("/streams"):
            return httpx.Response(200, json=_sample_valid_streams())
        return httpx.Response(404)

    def client_factory():
        return httpx.Client(
            transport=httpx.MockTransport(idempotency_handler), follow_redirects=False
        )

    with airflow_harness.session_factory() as db:
        res1 = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
        )

    assert res1 == {
        "connection_id": str(conn_id),
        "fetched": 5,
        "skipped_duplicate": 2,
        "skipped_no_gps": 2,
        "ingested_count": 1,
        "failed_count": 0,
    }
    assert detail_calls == [7004]

    detail_calls.clear()
    with airflow_harness.session_factory() as db:
        res2 = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
        )

    assert res2 == {
        "connection_id": str(conn_id),
        "fetched": 5,
        "skipped_duplicate": 3,
        "skipped_no_gps": 2,
        "ingested_count": 0,
        "failed_count": 0,
    }
    assert detail_calls == []


def test_strava_sync_malformed_empty_and_corrupt_streams(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("malformed_streams@example.com")
    conn_id = _create_strava_connection(airflow_harness, user.id)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {"id": 801, "name": "Valid First", "start_latlng": [0.1, 0.2]},
                    {"id": 802, "name": "Empty Stream Dict", "start_latlng": [0.11, 0.21]},
                    {"id": 803, "name": "Missing LatLng", "start_latlng": [0.12, 0.22]},
                    {"id": 804, "name": "Length Mismatch", "start_latlng": [0.13, 0.23]},
                    {"id": 805, "name": "Detail 404", "start_latlng": [0.14, 0.24]},
                    {"id": 806, "name": "Streams 500", "start_latlng": [0.15, 0.25]},
                    {"id": 807, "name": "Valid Last", "start_latlng": [0.16, 0.26]},
                ],
            )
        if path == "/api/v3/activities/805":
            return httpx.Response(404, json={"message": "Activity not found"})
        if path.startswith("/api/v3/activities/") and not path.endswith("/streams"):
            aid = int(path.split("/")[-1])
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": f"Activity {aid}",
                    "sport_type": "Ride",
                    "start_date": "2001-07-02T09:00:00Z",
                },
            )
        if path == "/api/v3/activities/801/streams" or path == "/api/v3/activities/807/streams":
            return httpx.Response(200, json=_sample_valid_streams())
        if path == "/api/v3/activities/802/streams":
            return httpx.Response(200, json={})
        if path == "/api/v3/activities/803/streams":
            return httpx.Response(200, json={"time": {"data": [0, 10, 20]}})
        if path == "/api/v3/activities/804/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[999.0, 999.0], [-999.0, -999.0]]},
                    "time": {"data": [0, 60]},
                },
            )
        if path == "/api/v3/activities/806/streams":
            return httpx.Response(500, json={"message": "Internal error"})

        return httpx.Response(404)

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with airflow_harness.session_factory() as db:
        res = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
        )

    assert res == {
        "connection_id": str(conn_id),
        "fetched": 7,
        "skipped_duplicate": 0,
        "skipped_no_gps": 0,
        "ingested_count": 2,
        "failed_count": 5,
    }

    with airflow_harness.session_factory() as db:
        valid_stages = list(
            db.scalars(
                select(Stage)
                .where(Stage.strava_activity_id.in_([801, 807]))
                .order_by(Stage.strava_activity_id.asc())
            ).all()
        )
        assert len(valid_stages) == 2
        for stg in valid_stages:
            assert stg.bronze_object_key and stg.silver_object_key
            assert airflow_harness.store.get_bytes(stg.bronze_object_key)
            assert airflow_harness.store.get_bytes(stg.silver_object_key)
            assert airflow_harness.store.get_bytes(stage_lod_cache_key(stg.project_id, stg.id))


def test_strava_sync_project_reuse_and_summary_aggregation(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    user = airflow_harness.create_user("project_reuse@example.com")
    conn_id = _create_strava_connection(airflow_harness, user.id)

    with airflow_harness.session_factory() as db:
        custom_proj = Project(
            owner_user_id=user.id,
            title="My Custom Strava Repository",
            settings_json={"strava_auto_sync": True, "theme": "dark"},
        )
        db.add(custom_proj)
        db.commit()
        custom_proj_id = custom_proj.id

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[{"id": 901, "name": "Reused Project Stage", "start_latlng": [0.1, 0.2]}],
            )
        if path == "/api/v3/activities/901":
            return httpx.Response(
                200,
                json={
                    "id": 901,
                    "name": "Reused Project Stage",
                    "sport_type": "Ride",
                    "start_date": "2001-07-02T12:00:00Z",
                },
            )
        if path == "/api/v3/activities/901/streams":
            return httpx.Response(200, json=_sample_valid_streams())
        return httpx.Response(404)

    def client_factory():
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    with airflow_harness.session_factory() as db:
        res = sync_strava_connection(
            db=db,
            store=airflow_harness.store,
            settings=airflow_harness.settings,
            connection_id=conn_id,
            user_id=user.id,
            http_client_factory=client_factory,
        )
        assert res["ingested_count"] == 1

        stage = db.scalar(select(Stage).where(Stage.strava_activity_id == 901))
        assert stage is not None
        assert stage.project_id == custom_proj_id

        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.project_id == custom_proj_id,
                AuditEvent.event_type == "stage.strava_auto_synced",
            )
        )
        assert audit is not None
        assert audit.metadata_json == {
            "stage_id": str(stage.id),
            "strava_activity_id": 901,
            "activity_type": stage.activity_type,
        }

    multi_results = [
        {
            "fetched": 10,
            "skipped_duplicate": 2,
            "skipped_no_gps": 1,
            "ingested_count": 6,
            "failed_count": 1,
        },
        {
            "fetched": 5,
            "skipped_duplicate": 1,
            "skipped_no_gps": 0,
            "ingested_count": 4,
            "failed_count": 0,
        },
    ]
    summary = summarize_strava_sync(multi_results)
    assert summary == {
        "connections_processed": 2,
        "fetched": 15,
        "skipped_duplicate": 3,
        "skipped_no_gps": 1,
        "ingested_count": 10,
        "failed_count": 1,
    }
