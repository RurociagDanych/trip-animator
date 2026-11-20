from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from trip_airflow.vault_sync_scheduler import discover_queue_and_process_vault_syncs
from trip_api.db import OAuthConnection, Project, VaultActivity, VaultSyncState
from trip_api.security import encrypt_secret

if TYPE_CHECKING:
    from conftest import AirflowTaskHarness


def fake_public_getaddrinfo(
    *args: Any, **kwargs: Any
) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
    _ = args, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.69", 443))]


def test_scheduler_runs_real_vault_processor_and_keeps_payload_safe(
    airflow_harness: AirflowTaskHarness,
    monkeypatch: Any,
) -> None:
    user = airflow_harness.create_user("vault-worker@example.com")
    key = airflow_harness.settings.decoded_encryption_key
    with airflow_harness.session_factory() as db:
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="9001",
                access_token_enc=encrypt_secret(
                    "access-token-secret", key, aad=f"oauth:{user.id}:strava".encode()
                ),
                refresh_token_enc=encrypt_secret(
                    "refresh-token-secret", key, aad=f"oauth:{user.id}:strava".encode()
                ),
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                scope="read,activity:read_all",
            )
        )
        db.commit()

    requests: list[str] = []

    def strava_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.headers.get("Authorization") != "Bearer access-token-secret":
            return httpx.Response(401)
        if request.url.path == "/api/v3/athlete":
            return httpx.Response(200, json={"id": 9001, "bikes": [], "shoes": []})
        if request.url.path == "/api/v3/athletes/9001/stats":
            return httpx.Response(200, json={"all_ride_totals": {"count": 1}})
        if request.url.path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7001,
                        "name": "Indoor ride without GPS",
                        "type": "VirtualRide",
                        "sport_type": "VirtualRide",
                        "start_date": "2025-04-01T12:00:00Z",
                        "start_latlng": [],
                        "end_latlng": [],
                        "distance": 12000,
                        "moving_time": 1800,
                    }
                ],
            )
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(socket, "getaddrinfo", fake_public_getaddrinfo)
    transport = httpx.MockTransport(strava_handler)
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    result = discover_queue_and_process_vault_syncs(
        airflow_harness.session_factory,
        airflow_harness.settings,
        now=now,
        max_pages_per_sync=2,
        http_client_factory=lambda: httpx.Client(transport=transport, follow_redirects=False),
    )

    assert result == {
        "connections_discovered": 1,
        "backfills_queued": 1,
        "incrementals_queued": 0,
        "syncs_claimed": 1,
        "syncs_completed": 1,
        "syncs_rate_limited": 0,
        "syncs_requeued": 0,
        "syncs_failed": 0,
    }
    assert requests == [
        "/api/v3/athlete",
        "/api/v3/athletes/9001/stats",
        "/api/v3/athlete/activities",
    ]
    assert "access-token-secret" not in json.dumps(result)
    assert "refresh-token-secret" not in json.dumps(result)

    with airflow_harness.session_factory() as db:
        state = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
        activity = db.scalar(select(VaultActivity).where(VaultActivity.user_id == user.id))
        project = db.scalar(select(Project).where(Project.owner_user_id == user.id))
        assert state is not None
        assert state.mode == "full_backfill"
        assert state.status == "completed"
        assert state.backfill_completed_at is not None
        assert state.total_scanned == 1
        assert state.total_upserted == 1
        assert state.total_skipped_no_gps == 1
        assert activity is not None
        assert activity.external_activity_id == "7001"
        assert activity.name == "Indoor ride without GPS"
        assert activity.start_lat is None
        assert project is None


def test_scheduler_does_not_reset_active_or_rate_limited_progress(
    airflow_harness: AirflowTaskHarness,
) -> None:
    running_user = airflow_harness.create_user("running@example.com")
    limited_user = airflow_harness.create_user("limited@example.com")
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    with airflow_harness.session_factory() as db:
        for user, status, retry_at in (
            (running_user, "running", None),
            (limited_user, "rate_limited", now + timedelta(hours=1)),
        ):
            db.add(
                OAuthConnection(
                    user_id=user.id,
                    provider="strava",
                    external_athlete_id=str(user.id),
                    access_token_enc="encrypted-access",
                    refresh_token_enc="encrypted-refresh",
                    expires_at=now + timedelta(hours=2),
                    scope="read",
                )
            )
            db.add(
                VaultSyncState(
                    user_id=user.id,
                    provider="strava",
                    status=status,
                    phase="importing_activities" if status == "running" else "rate_limit_paused",
                    mode="full_backfill",
                    started_at=now - timedelta(hours=1),
                    backfill_cursor_before_epoch=1_700_000_000,
                    current_page=7,
                    total_scanned=321,
                    total_upserted=300,
                    total_skipped_no_gps=11,
                    next_retry_at=retry_at,
                    lease_expires_at=(now + timedelta(hours=1)) if status == "running" else None,
                )
            )
        db.commit()

    result = discover_queue_and_process_vault_syncs(
        airflow_harness.session_factory,
        airflow_harness.settings,
        now=now,
    )

    assert result["backfills_queued"] == 0
    assert result["incrementals_queued"] == 0
    assert result["syncs_claimed"] == 0
    with airflow_harness.session_factory() as db:
        states = list(db.scalars(select(VaultSyncState).order_by(VaultSyncState.user_id)).all())
        assert len(states) == 2
        for state in states:
            assert state.status in {"running", "rate_limited"}
            assert state.mode == "full_backfill"
            assert state.current_page == 7
            assert state.total_scanned == 321
            assert state.total_upserted == 300
            assert state.total_skipped_no_gps == 11
            assert state.backfill_cursor_before_epoch == 1_700_000_000
