from __future__ import annotations

import socket
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, select, text
from trip_api.cli import run_process_ingest_jobs
from trip_api.db import (
    IngestJob,
    OAuthConnection,
    Project,
    Stage,
    VaultActivity,
    VaultActivityTile,
    VaultGear,
    VaultSyncState,
    create_db_engine,
)
from trip_api.ingest import process_pending_ingest_jobs, replay_outdated_silver_stages
from trip_api.security import encrypt_secret
from trip_api.vault_sync import (
    build_vault_sync_status,
    compute_rate_limit_pause,
    mirror_stage_to_vault,
    process_pending_vault_syncs,
    start_or_resume_strava_vault_sync,
    upsert_strava_activity_to_vault,
)
from trip_telemetry.tiles import encode_polyline

if TYPE_CHECKING:
    from conftest import AppHarness, HarnessFactory


def _fake_public_getaddrinfo(
    *args: Any, **kwargs: Any
) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.69", 443))]


def _seed_strava_oauth(
    harness: AppHarness,
    user_id: uuid.UUID,
    *,
    athlete_id: str = "41928374",
) -> None:
    enc_material = harness.settings.decoded_encryption_key
    aad = f"oauth:{user_id}:strava".encode()
    with harness.session_factory() as db:
        db.add(
            OAuthConnection(
                user_id=user_id,
                provider="strava",
                external_athlete_id=athlete_id,
                access_token_enc=encrypt_secret("demo-access", enc_material, aad=aad),
                refresh_token_enc=encrypt_secret("demo-refresh", enc_material, aad=aad),
                expires_at=datetime.now(UTC) + timedelta(hours=6),
                scope="read,activity:read_all",
            )
        )
        db.commit()


def _make_strava_summary(
    act_id: int,
    *,
    started_at_utc: datetime,
    name: str | None = None,
    sport_type: str = "Ride",
    points: list[tuple[float, float]] | None = None,
    trainer: bool = False,
    gear_id: str | None = "b100",
    utc_offset: int = 0,
    start_date_local: str | None = None,
) -> dict[str, Any]:
    pts = points if points is not None else [(46.50, 11.80), (46.51, 11.81), (46.52, 11.82)]
    has_pts = len(pts) > 0
    poly = encode_polyline(pts) if has_pts else ""
    local_str = start_date_local or (
        (started_at_utc + timedelta(seconds=utc_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {
        "id": act_id,
        "name": name or f"Ride {act_id}",
        "sport_type": sport_type,
        "type": sport_type,
        "start_date": started_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date_local": local_str,
        "timezone": "(GMT+02:00) Europe/Rome" if utc_offset else "(GMT+00:00) UTC",
        "utc_offset": utc_offset,
        "distance": 42500.0,
        "total_elevation_gain": 820.0,
        "elev_high": 1820.0,
        "elev_low": 1000.0,
        "moving_time": 5400,
        "elapsed_time": 5900,
        "average_speed": 7.87,
        "max_speed": 15.2,
        "average_heartrate": 148.0,
        "max_heartrate": 176.0,
        "average_watts": 225.0,
        "weighted_average_watts": 240.0,
        "kilojoules": 1215.0,
        "average_cadence": 88.0,
        "suffer_score": 92.0,
        "gear_id": gear_id,
        "commute": False,
        "trainer": trainer,
        "private": False,
        "start_latlng": [pts[0][0], pts[0][1]] if has_pts else [],
        "end_latlng": [pts[-1][0], pts[-1][1]] if has_pts else [],
        "map": {"summary_polyline": poly},
    }


def test_alembic_vault_sqlite_upgrade_downgrade_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "alembic_0007.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    api_dir = Path(__file__).resolve().parents[1]
    cfg = AlembicConfig(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    alembic_command.upgrade(cfg, "head")
    alembic_command.check(cfg)

    vault_tables = {
        "vault_activities",
        "vault_activity_tiles",
        "vault_sync_states",
        "vault_gears",
    }
    engine = create_db_engine(db_url)
    try:
        insp = inspect(engine)
        assert vault_tables <= set(insp.get_table_names())
        stage_cols = {c["name"] for c in insp.get_columns("stages")}
        assert "vault_activity_id" in stage_cols
        assert "retry_count" in {c["name"] for c in insp.get_columns("vault_sync_states")}
        with engine.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0008_vault_sync_retries"
    finally:
        engine.dispose()

    alembic_command.downgrade(cfg, "0006_password_reset_tokens")
    engine_down = create_db_engine(db_url)
    try:
        insp_down = inspect(engine_down)
        assert vault_tables.isdisjoint(set(insp_down.get_table_names()))
        assert "vault_activity_id" not in {c["name"] for c in insp_down.get_columns("stages")}
    finally:
        engine_down.dispose()

    alembic_command.upgrade(cfg, "head")
    alembic_command.check(cfg)


def test_full_backfill_resumable_cursor_live_progress_and_incremental_sync(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness()
    user = h.create_user("vault-rider@example.com")
    _seed_strava_oauth(h, user.id, athlete_id="41928374")
    client, csrf = h.login_client("vault-rider@example.com")

    base_dt = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
    page1_items = [
        _make_strava_summary(
            200000 + idx,
            started_at_utc=base_dt - timedelta(hours=idx + 1),
            name=f"Page 1 Ride {idx}",
            points=[(46.50 + (idx % 5) * 0.01, 11.80), (46.51 + (idx % 5) * 0.01, 11.82)],
            gear_id="b100",
        )
        for idx in range(200)
    ]
    page2_items = [
        _make_strava_summary(
            100003,
            started_at_utc=datetime(2026, 5, 10, 23, 30, 0, tzinfo=UTC),
            start_date_local="2026-05-11T01:30:00Z",
            utc_offset=7200,
            name="Midnight Local Shift Ride",
            points=[(45.00, 10.00), (45.02, 10.02)],
            gear_id="b100",
        ),
        _make_strava_summary(
            100002,
            started_at_utc=datetime(2026, 5, 9, 18, 0, 0, tzinfo=UTC),
            name="Indoor Trainer Session",
            sport_type="VirtualRide",
            points=[(46.50, 11.80), (46.51, 11.81)],
            trainer=True,
            gear_id=None,
        ),
        _make_strava_summary(
            100001,
            started_at_utc=datetime(2026, 5, 8, 9, 0, 0, tzinfo=UTC),
            name="Treadmill Walk",
            sport_type="Walk",
            points=[],
            gear_id="s200",
        ),
    ]

    seen_before_params: list[int] = []
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if req.url.path == "/api/v3/athlete":
            return httpx.Response(
                200,
                json={
                    "id": 41928374,
                    "bikes": [
                        {
                            "id": "b100",
                            "name": "Canyon Endurace",
                            "distance": 12400000.0,
                            "primary": True,
                        }
                    ],
                    "shoes": [{"id": "s200", "name": "Trail Runners", "distance": 540000.0}],
                },
                headers={"X-ReadRateLimit-Usage": "1,10", "X-ReadRateLimit-Limit": "100,1000"},
            )
        if req.url.path == "/api/v3/athletes/41928374/stats":
            return httpx.Response(
                200,
                json={
                    "all_ride_totals": {"count": 202},
                    "all_run_totals": {"count": 1},
                    "all_swim_totals": {"count": 0},
                },
                headers={"X-ReadRateLimit-Usage": "2,11", "X-ReadRateLimit-Limit": "100,1000"},
            )
        if req.url.path == "/api/v3/athlete/activities":
            before_val = int(req.url.params["before"])
            seen_before_params.append(before_val)
            assert int(req.url.params["per_page"]) == 200
            if len(seen_before_params) == 1:
                return httpx.Response(
                    200,
                    json=page1_items,
                    headers={"X-ReadRateLimit-Usage": "3,12", "X-ReadRateLimit-Limit": "100,1000"},
                )
            return httpx.Response(
                200,
                json=page2_items,
                headers={"X-ReadRateLimit-Usage": "4,13", "X-ReadRateLimit-Limit": "100,1000"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def sync_factory() -> httpx.Client:
        return httpx.Client(transport=transport, follow_redirects=False)

    h.app.state.sync_http_client_factory = sync_factory

    # HTTP only durably enqueues; the shared processor performs provider I/O.
    resp = client.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "full_backfill", "inline_first_page": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 202, resp.text
    body1 = resp.json()
    assert body1["connected"] is True
    assert body1["athlete_id"] == "41928374"
    assert body1["status"] == "queued"
    assert body1["phase"] == "discovering_pages"
    assert body1["current_page"] == 0
    assert body1["processed_activities"] == 0
    assert body1["imported_count"] == 0
    assert body1["rate_limit"] == {
        "short_used": None,
        "short_limit": None,
        "daily_used": None,
        "daily_limit": None,
    }
    assert seen_before_params == []
    assert body1["webhook_active"] is True
    v_after_enqueue = body1["vault_version"]

    status_mid = client.get("/api/v1/vault/sync/status")
    assert status_mid.status_code == 200
    assert status_mid.json()["processed_activities"] == 0

    with h.session_factory() as db:
        worker_stats = process_pending_vault_syncs(
            db,
            h.settings,
            batch_size=5,
            http_client_factory=sync_factory,
        )
    assert worker_stats == {
        "claimed": 1,
        "completed": 1,
        "rate_limited": 0,
        "queued": 0,
        "failed": 0,
    }
    assert len(seen_before_params) == 2
    assert seen_before_params[1] < seen_before_params[0]

    status_done = client.get("/api/v1/vault/sync/status").json()
    assert status_done["status"] == "completed"
    assert status_done["phase"] == "completed"
    assert status_done["progress_pct"] == 100.0
    assert status_done["processed_activities"] == 203
    assert status_done["imported_count"] == 203
    assert status_done["skipped_no_gps_count"] == 2
    assert status_done["tiles_computed_count"] == 201
    assert status_done["total_activities"] == 203
    assert status_done["gps_activities"] == 201
    assert status_done["indoor_activities"] == 1
    assert status_done["backfill_completed_at"] is not None
    assert status_done["vault_version"] > v_after_enqueue
    with h.session_factory() as db:
        gears = list(db.scalars(select(VaultGear).where(VaultGear.user_id == user.id)).all())
        assert {g.gear_id: g.name for g in gears} == {
            "b100": "Canyon Endurace",
            "s200": "Trail Runners",
        }
        assert db.scalars(select(Project).where(Project.owner_user_id == user.id)).all() == []
        assert (
            db.scalars(select(Stage).join(Project).where(Project.owner_user_id == user.id)).all()
            == []
        )

    # Exercise the actual provider ingestion -> catalog query boundary, not
    # only fixtures that prepopulate the catalog's measurement provenance.
    catalog = client.get("/api/v1/vault/activities", params={"min_distance_m": 1, "limit": 1})
    assert catalog.status_code == 200, catalog.text
    catalog_body = catalog.json()
    assert catalog_body["total"] == 203
    assert catalog_body["items"][0]["distance_m"] == 42500.0
    assert catalog_body["summary"]["distance_known_count"] == 203
    assert catalog_body["summary"]["distance_m"] == 203 * 42500.0

    with h.session_factory() as db:
        midnight_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == user.id,
                VaultActivity.strava_activity_id == 100003,
            )
        )
        assert midnight_act is not None
        assert midnight_act.local_date == date(2026, 5, 11)
        assert midnight_act.started_at_local == datetime(2026, 5, 11, 1, 30, 0)
        assert midnight_act.gear_name == "Canyon Endurace"
        assert midnight_act.tile_precision == "summary_polyline"
        assert midnight_act.tile_count_z14 > 0
        assert midnight_act.new_tiles_z14_count == midnight_act.tile_count_z14

        indoor_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == user.id,
                VaultActivity.strava_activity_id == 100002,
            )
        )
        assert indoor_act is not None
        assert indoor_act.has_gps is False
        assert indoor_act.is_indoor_virtual is True
        assert indoor_act.tile_precision == "none"
        assert indoor_act.tile_count_z14 == 0

        midnight_act.tile_precision = "full_stream"
        db.commit()

    def incremental_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v3/athlete":
            return httpx.Response(404)
        if req.url.path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    page2_items[0],
                    _make_strava_summary(
                        300001,
                        started_at_utc=base_dt + timedelta(hours=2),
                        name="Brand New Ride",
                        points=[(47.10, 12.10), (47.11, 12.12)],
                    ),
                ],
            )
        return httpx.Response(404)

    h.app.state.sync_http_client_factory = lambda: httpx.Client(
        transport=httpx.MockTransport(incremental_handler),
        follow_redirects=False,
    )
    inc_resp = client.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "incremental"},
        headers={"X-CSRF-Token": csrf},
    )
    assert inc_resp.status_code == 202, inc_resp.text
    inc_data = inc_resp.json()
    assert inc_data["status"] == "queued"
    with h.session_factory() as db:
        result = process_pending_vault_syncs(
            db,
            h.settings,
            http_client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(incremental_handler), follow_redirects=False
            ),
        )
        assert result["completed"] == 1
    inc_data = client.get("/api/v1/vault/sync/status").json()
    assert inc_data["status"] == "completed"
    assert inc_data["imported_count"] == 1
    assert inc_data["skipped_existing_count"] == 1

    with h.session_factory() as db:
        check_act = db.scalar(
            select(VaultActivity).where(
                VaultActivity.user_id == user.id,
                VaultActivity.strava_activity_id == 100003,
            )
        )
        assert check_act is not None
        assert check_act.tile_precision == "full_stream"


@pytest.mark.parametrize(
    ("active_status", "active_phase", "lease_expires_at", "next_retry_at"),
    [
        ("queued", "discovering_pages", None, None),
        ("running", "importing_activities", datetime(2026, 10, 1, 12, 5), None),
        ("rate_limited", "rate_limit_paused", None, datetime(2026, 10, 1, 12, 10)),
    ],
)
def test_repeated_sync_request_preserves_active_cursor_and_deadlines(
    make_harness: HarnessFactory,
    active_status: str,
    active_phase: str,
    lease_expires_at: datetime | None,
    next_retry_at: datetime | None,
) -> None:
    h = make_harness()
    user = h.create_user(f"repeat-{active_status}@example.com")
    _seed_strava_oauth(h, user.id)
    client, csrf = h.login_client(f"repeat-{active_status}@example.com")

    provider_calls = 0

    def should_not_call_provider() -> httpx.Client:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("sync request must only enqueue work")

    h.app.state.sync_http_client_factory = should_not_call_provider
    endpoint = "/api/v1/vault/sync/strava"
    headers = {"X-CSRF-Token": csrf}
    first = client.post(endpoint, json={"mode": "full_backfill"}, headers=headers)
    assert first.status_code == 202, first.text

    with h.session_factory() as db:
        state = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
        assert state is not None
        state.status = active_status
        state.phase = active_phase
        state.mode = "full_backfill"
        state.current_page = 7
        state.backfill_cursor_before_epoch = 1_782_000_000
        state.incremental_after_epoch = 1_781_000_000
        state.total_scanned = 123
        state.total_upserted = 45
        state.lease_expires_at = lease_expires_at
        state.next_retry_at = next_retry_at
        db.commit()

    repeated = client.post(endpoint, json={"mode": "incremental"}, headers=headers)
    assert repeated.status_code == 202, repeated.text
    assert provider_calls == 0

    with h.session_factory() as db:
        state = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
        assert state is not None
        assert state.status == active_status
        assert state.phase == active_phase
        assert state.mode == "full_backfill"
        assert state.current_page == 7
        assert state.backfill_cursor_before_epoch == 1_782_000_000
        assert state.incremental_after_epoch == 1_781_000_000
        assert state.total_scanned == 123
        assert state.total_upserted == 45
        assert state.lease_expires_at == lease_expires_at
        assert state.next_retry_at == next_retry_at


def test_vault_sync_rate_limit_90pct_proactive_and_429_pause_resume(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness()
    user = h.create_user("ratelimit@example.com")
    _seed_strava_oauth(h, user.id, athlete_id="555111")

    t0 = datetime(2026, 8, 1, 12, 10, 0, tzinfo=UTC)
    daily_pause, daily_retry, _ = compute_rate_limit_pause(
        {"X-ReadRateLimit-Usage": "20,950", "X-ReadRateLimit-Limit": "100,1000"},
        200,
        t0,
    )
    assert daily_pause is True
    assert daily_retry == datetime(2026, 8, 2, 0, 1, 0, tzinfo=UTC)

    page1 = [
        _make_strava_summary(
            500000 + i,
            started_at_utc=t0 - timedelta(minutes=i + 1),
            points=[(46.0 + (i % 3) * 0.01, 11.0), (46.01 + (i % 3) * 0.01, 11.01)],
        )
        for i in range(200)
    ]
    page2 = [
        _make_strava_summary(
            499001,
            started_at_utc=t0 - timedelta(days=2),
            points=[(46.20, 11.20), (46.21, 11.21)],
        )
    ]

    page_calls = 0

    def rl_handler(req: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        if req.url.path == "/api/v3/athlete":
            return httpx.Response(200, json={"id": 555111, "bikes": [], "shoes": []})
        if req.url.path.endswith("/stats"):
            return httpx.Response(200, json={"all_ride_totals": {"count": 201}})
        if req.url.path == "/api/v3/athlete/activities":
            page_calls += 1
            if page_calls == 1:
                return httpx.Response(
                    200,
                    json=page1,
                    headers={
                        "X-ReadRateLimit-Usage": "90,100",
                        "X-ReadRateLimit-Limit": "100,1000",
                    },
                )
            if page_calls == 2:
                return httpx.Response(
                    429,
                    json={"message": "Rate Limit Exceeded"},
                    headers={
                        "X-ReadRateLimit-Usage": "100,100",
                        "X-ReadRateLimit-Limit": "100,1000",
                    },
                )
            return httpx.Response(
                200,
                json=page2,
                headers={
                    "X-ReadRateLimit-Usage": "2,102",
                    "X-ReadRateLimit-Limit": "100,1000",
                },
            )
        return httpx.Response(404)

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(rl_handler), follow_redirects=False)

    with h.session_factory() as db:
        state = start_or_resume_strava_vault_sync(
            db,
            h.settings,
            user_id=user.id,
            mode="full_backfill",
            inline_first_page=True,
            http_client_factory=factory,
            now=t0,
        )
        assert state.status == "queued"
        assert state.phase == "discovering_pages"
        assert page_calls == 0
        first_run = process_pending_vault_syncs(
            db,
            h.settings,
            http_client_factory=factory,
            now=t0,
        )
        assert first_run["claimed"] == 1
        assert first_run["rate_limited"] == 1
        assert state.status == "rate_limited"
        assert state.phase == "rate_limit_paused"
        assert state.next_retry_at is not None
        assert state.next_retry_at.replace(tzinfo=UTC) == datetime(
            2026, 8, 1, 12, 15, 5, tzinfo=UTC
        )
        assert state.total_upserted == 200

        status_paused = build_vault_sync_status(
            db,
            h.settings,
            user_id=user.id,
            now=t0 + timedelta(seconds=65),
        )
        assert status_paused.rate_limit_retry_in_seconds == 240
        assert "resumes in 04:00" in status_paused.phase_label

        early = process_pending_vault_syncs(
            db,
            h.settings,
            http_client_factory=factory,
            now=datetime(2026, 8, 1, 12, 14, 0, tzinfo=UTC),
        )
        assert early["claimed"] == 0

        hit_429 = process_pending_vault_syncs(
            db,
            h.settings,
            http_client_factory=factory,
            now=datetime(2026, 8, 1, 12, 15, 10, tzinfo=UTC),
        )
        assert hit_429 == {
            "claimed": 1,
            "completed": 0,
            "rate_limited": 1,
            "queued": 0,
            "failed": 0,
        }
        st_429 = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
        assert st_429 is not None
        assert st_429.next_retry_at is not None
        assert st_429.next_retry_at.replace(tzinfo=UTC) == datetime(
            2026, 8, 1, 12, 30, 5, tzinfo=UTC
        )

        done = process_pending_vault_syncs(
            db,
            h.settings,
            http_client_factory=factory,
            now=datetime(2026, 8, 1, 12, 30, 10, tzinfo=UTC),
        )
        assert done == {
            "claimed": 1,
            "completed": 1,
            "rate_limited": 0,
            "queued": 0,
            "failed": 0,
        }
        assert (
            len(db.scalars(select(VaultActivity).where(VaultActivity.user_id == user.id)).all())
            == 201
        )


def test_webhook_decision_1a_vault_upsert_hydrated_stage_update_and_delete(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness()
    user = h.create_user("webhook-vault@example.com")
    _seed_strava_oauth(h, user.id, athlete_id="777888")

    requested_paths: list[str] = []
    poly = encode_polyline([(46.50, 11.80), (46.52, 11.82)])

    def wh_handler(req: httpx.Request) -> httpx.Response:
        requested_paths.append(req.url.path)
        if req.url.path == "/api/v3/activities/8001":
            return httpx.Response(
                200,
                json=_make_strava_summary(
                    8001,
                    started_at_utc=datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC),
                    name="Vault Only Webhook Ride",
                    points=[(46.50, 11.80), (46.52, 11.82)],
                ),
            )
        if req.url.path == "/api/v3/activities/8002":
            return httpx.Response(
                200,
                json={
                    **_make_strava_summary(
                        8002,
                        started_at_utc=datetime(2026, 6, 2, 8, 0, 0, tzinfo=UTC),
                        name="Hydrated Stage Renamed",
                        points=[(46.50, 11.80), (46.52, 11.82)],
                    ),
                    "map": {"summary_polyline": poly},
                },
            )
        if req.url.path == "/api/v3/activities/8002/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[46.50, 11.80], [46.51, 11.81], [46.52, 11.82]]},
                    "time": {"data": [0, 60, 120]},
                    "altitude": {"data": [1000.0, 1050.0, 1120.0]},
                },
            )
        return httpx.Response(404)

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(wh_handler), follow_redirects=False)

    with h.session_factory() as db:
        db.add(
            IngestJob(
                provider="strava",
                event_type="create",
                external_owner_id="777888",
                external_object_id=8001,
                payload_json={"object_type": "activity", "aspect_type": "create"},
                status="queued",
            )
        )
        db.commit()
        res1 = process_pending_ingest_jobs(db, h.store, h.settings, http_client_factory=factory)
        assert res1["succeeded"] == 1
        assert requested_paths == ["/api/v3/activities/8001"]
        assert db.scalars(select(Stage)).all() == []
        assert db.scalars(select(Project)).all() == []
        va_8001 = db.scalar(select(VaultActivity).where(VaultActivity.strava_activity_id == 8001))
        assert va_8001 is not None
        assert va_8001.tile_precision == "summary_polyline"
        assert va_8001.tile_count_z14 > 0
        assert va_8001.new_tiles_z14_count == va_8001.tile_count_z14

    with h.session_factory() as db:
        canonical_before_refresh = upsert_strava_activity_to_vault(
            db,
            user_id=user.id,
            item=_make_strava_summary(
                8002,
                started_at_utc=datetime(2026, 6, 2, 8, 0, 0, tzinfo=UTC),
                name="Provider Summary",
                points=[(46.50, 11.80), (46.52, 11.82)],
            ),
            now=datetime(2026, 6, 2, 8, 0, 0, tzinfo=UTC),
        ).vault_activity
        proj = Project(owner_user_id=user.id, title="Curated Alps", settings_json={})
        db.add(proj)
        db.flush()
        stage = Stage(
            project_id=proj.id,
            source_type="strava",
            source_format="strava",
            activity_type="cycling",
            external_source_id="8002",
            strava_activity_id=8002,
            name="Old Name",
            stage_order=0,
            day_index=1,
            bronze_object_key=f"bronze/{proj.id}/8002.json",
            silver_object_key=f"silver/{proj.id}/8002.parquet",
            point_count=3,
            distance_m=1000.0,
            trim_start_s=15.0,
            trim_end_s=105.0,
        )
        db.add(stage)
        db.add(
            IngestJob(
                provider="strava",
                event_type="update",
                external_owner_id="777888",
                external_object_id=8002,
                payload_json={"object_type": "activity", "aspect_type": "update"},
                status="queued",
            )
        )
        db.commit()
        stage_id = stage.id

        requested_paths.clear()
        res2 = process_pending_ingest_jobs(db, h.store, h.settings, http_client_factory=factory)
        assert res2["succeeded"] == 1
        assert requested_paths == ["/api/v3/activities/8002", "/api/v3/activities/8002/streams"]

        updated_stage = db.get(Stage, stage_id)
        assert updated_stage is not None
        assert updated_stage.name == "Old Name"
        assert updated_stage.distance_m == 1000.0
        assert updated_stage.trim_start_s == 15.0
        assert updated_stage.trim_end_s == 105.0
        assert updated_stage.vault_activity_id is not None

        va_8002 = db.get(VaultActivity, updated_stage.vault_activity_id)
        assert va_8002 is not None
        assert va_8002.tile_precision == "full_stream"
        assert va_8002.id == canonical_before_refresh.id
        assert va_8002.elev_high_m == 1120.0

        # Because 8001 (earlier) already visited those exact tiles, 8002 should have 0 new tiles
        assert va_8002.new_tiles_z14_count == 0

    with h.session_factory() as db:
        db.add(
            IngestJob(
                provider="strava",
                event_type="delete",
                external_owner_id="777888",
                external_object_id=8001,
                payload_json={"object_type": "activity", "aspect_type": "delete"},
                status="queued",
            )
        )
        db.commit()
        res3 = process_pending_ingest_jobs(db, h.store, h.settings, http_client_factory=factory)
        assert res3["succeeded"] == 1

        assert (
            db.scalar(select(VaultActivity).where(VaultActivity.strava_activity_id == 8001)) is None
        )
        va_8002_after = db.scalar(
            select(VaultActivity).where(VaultActivity.strava_activity_id == 8002)
        )
        assert va_8002_after is not None
        assert va_8002_after.new_tiles_z14_count == va_8002_after.tile_count_z14


def test_stage_upload_split_mirroring_and_backfill_stages(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    h = make_harness(rate_limit_gpx_max=50)
    h.create_user("alice-vault@example.com")
    h.create_user("bob-vault@example.com")
    alice_client, alice_csrf = h.login_client("alice-vault@example.com")
    bob_client, bob_csrf = h.login_client("bob-vault@example.com")

    proj_id = alice_client.post(
        "/api/v1/projects",
        json={"title": "Alpine GPX Trip"},
        headers={"X-CSRF-Token": alice_csrf},
    ).json()["id"]

    up = alice_client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("stage1.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert up.status_code == 201, up.text
    stage1_id = uuid.UUID(up.json()["id"])

    with h.session_factory() as db:
        st1 = db.get(Stage, stage1_id)
        assert st1 is not None
        assert st1.vault_activity_id is not None
        va1 = db.get(VaultActivity, st1.vault_activity_id)
        assert va1 is not None
        assert va1.source_provider == "stage"
        assert va1.tile_precision == "full_stream"
        assert va1.summary_polyline is not None
        assert va1.tile_count_z14 > 0
        assert va1.raw_metadata_json["distance"] == va1.distance_m
        assert va1.raw_metadata_json["total_elevation_gain"] == va1.elevation_gain_m
        catalog = alice_client.get(
            f"/api/v1/vault/activities?min_distance_m={va1.distance_m}"
        ).json()
        assert catalog["total"] == 1
        assert catalog["summary"]["distance_known_count"] == 1
        tiles_for_va1 = list(
            db.scalars(
                select(VaultActivityTile).where(VaultActivityTile.vault_activity_id == va1.id)
            ).all()
        )
        assert len(tiles_for_va1) == va1.tile_count_z14

    with h.session_factory() as db:
        original_va_id = db.get(Stage, stage1_id).vault_activity_id
        original_va = db.get(VaultActivity, original_va_id)
        original_snapshot = (
            original_va.name,
            original_va.distance_m,
            original_va.summary_polyline,
            original_va.tile_count_z14,
        )
    split_resp = alice_client.post(
        f"/api/v1/projects/{proj_id}/stages/{stage1_id}/split",
        json={"split_elapsed_s": 90.0, "part1_name": "Morning Climb", "part2_name": "Descent"},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert split_resp.status_code == 200, split_resp.text
    part2_id = uuid.UUID(split_resp.json()["part2"]["id"])

    with h.session_factory() as db:
        st_p1 = db.get(Stage, stage1_id)
        st_p2 = db.get(Stage, part2_id)
        assert st_p1 is not None and st_p2 is not None
        assert st_p1.vault_activity_id == original_va_id
        assert st_p2.vault_activity_id == original_va_id
        st_p1.name = "Edited Project Segment"
        st_p1.distance_m = 123.0
        # A Strava identifier alone does not make project-derived geometry
        # trusted provider data; replay/mirroring must preserve the canonical.
        st_p1.strava_activity_id = 123456
        project = db.get(Project, uuid.UUID(proj_id))
        assert project is not None
        mirror_stage_to_vault(db, user_id=project.owner_user_id, stage=st_p1)
        va_after = db.get(VaultActivity, original_va_id)
        assert va_after is not None
        assert (
            va_after.name,
            va_after.distance_m,
            va_after.summary_polyline,
            va_after.tile_count_z14,
        ) == original_snapshot
        db.commit()

    bob_bf = bob_client.post(
        "/api/v1/vault/backfill-stages",
        headers={"X-CSRF-Token": bob_csrf},
    )
    assert bob_bf.status_code == 200
    assert bob_bf.json()["scanned_stages"] == 0
    assert bob_bf.json()["mirrored_stages"] == 0

    bf1 = alice_client.post(
        "/api/v1/vault/backfill-stages",
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert bf1.status_code == 200, bf1.text
    bf1_data = bf1.json()
    assert bf1_data["scanned_stages"] == 2
    assert bf1_data["mirrored_stages"] == 0
    assert bf1_data["linked_stages"] == 0
    assert bf1_data["skipped_stages"] == 2
    assert bf1_data["failed_stages"] == 0

    # Replay rewrites project-stage metadata, but must retain split exclusion
    # provenance so a later backfill cannot promote either segment.
    with h.session_factory() as db:
        split_stages = list(
            db.scalars(select(Stage).where(Stage.project_id == uuid.UUID(proj_id))).all()
        )
        for split_stage in split_stages:
            split_stage.etl_version = 0
        db.commit()
        replay_stats = replay_outdated_silver_stages(db, h.store)
        assert replay_stats["replayed_count"] == 2
        assert replay_stats["failed_count"] == 0
        for split_stage in split_stages:
            db.refresh(split_stage)
            assert any(
                isinstance(warning, dict)
                and warning.get("code") == "vault_canonical_excluded_split_segment"
                for warning in (split_stage.warnings_json or [])
            )

    bf2 = alice_client.post(
        "/api/v1/vault/backfill-stages",
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert bf2.status_code == 200
    assert bf2.json()["scanned_stages"] == 2
    assert bf2.json()["skipped_stages"] == 2
    assert bf2.json()["mirrored_stages"] == 0

    with h.session_factory() as db:
        va_after_backfill = db.get(VaultActivity, original_va_id)
        assert va_after_backfill is not None
        assert (
            va_after_backfill.name,
            va_after_backfill.distance_m,
            va_after_backfill.summary_polyline,
            va_after_backfill.tile_count_z14,
        ) == original_snapshot

    assert bob_client.get("/api/v1/vault/sync/status").json()["total_activities"] == 0
    assert alice_client.get("/api/v1/vault/sync/status").json()["total_activities"] == 1

    # If a legacy split loses its canonical row/link, backfill must not
    # reinterpret either project segment as an independent source activity.
    with h.session_factory() as db:
        db.get(Stage, stage1_id).vault_activity_id = None
        db.get(Stage, part2_id).vault_activity_id = None
        db.delete(db.get(VaultActivity, original_va_id))
        db.commit()
    missing_source_backfill = alice_client.post(
        "/api/v1/vault/backfill-stages",
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert missing_source_backfill.status_code == 200, missing_source_backfill.text
    assert missing_source_backfill.json()["mirrored_stages"] == 0
    assert missing_source_backfill.json()["linked_stages"] == 0
    assert alice_client.get("/api/v1/vault/sync/status").json()["total_activities"] == 0


@pytest.mark.parametrize("failure", ["server_error", "timeout"])
def test_vault_sync_retries_transient_errors_without_resetting_progress(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness()
    user = h.create_user("retry-vault@example.com")
    _seed_strava_oauth(h, user.id)
    client, csrf = h.login_client("retry-vault@example.com")
    response = client.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "full_backfill"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 202
    now = datetime.now(UTC)
    with h.session_factory() as db:
        state = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
        assert state is not None
        state.current_page = 2
        state.phase = "importing_activities"
        state.total_scanned = 400
        state.backfill_cursor_before_epoch = 1_700_000_000
        db.commit()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["before"] == "1700000000"
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("Synthetic timeout", request=request)
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    with h.session_factory() as db:
        result = process_pending_vault_syncs(db, h.settings, http_client_factory=factory, now=now)
    assert result["queued"] == 1
    status = client.get("/api/v1/vault/sync/status").json()
    assert status["status"] == "queued"
    assert status["processed_activities"] == 400
    assert status["current_page"] == 2
    assert status["next_retry_at"] is not None
    repeated = client.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "incremental"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert repeated["next_retry_at"] == status["next_retry_at"]
    assert repeated["current_page"] == 2
    with h.session_factory() as db:
        early = process_pending_vault_syncs(
            db, h.settings, http_client_factory=factory, now=now + timedelta(seconds=59)
        )
    assert early["claimed"] == 0
    assert len(requests) == 1
    with h.session_factory() as db:
        recovered = process_pending_vault_syncs(
            db, h.settings, http_client_factory=factory, now=now + timedelta(seconds=60)
        )
    assert recovered["completed"] == 1
    status = client.get("/api/v1/vault/sync/status").json()
    assert status["processed_activities"] == 400
    assert status["next_retry_at"] is None
    assert status["last_error"] is None


@pytest.mark.parametrize("status_code", [503, 403])
def test_vault_sync_retry_budget_is_bounded_and_permanent_errors_fail(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness()
    user = h.create_user("retry-budget@example.com")
    _seed_strava_oauth(h, user.id)
    client, csrf = h.login_client("retry-budget@example.com")
    client.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "full_backfill"},
        headers={"X-CSRF-Token": csrf},
    )
    with h.session_factory() as db:
        state = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
        assert state is not None
        state.current_page = 1
        state.phase = "importing_activities"
        db.commit()

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status_code)))

    now = datetime.now(UTC)
    delays = [60, 120, 240, 480, 900] if status_code == 503 else []
    for delay in delays:
        with h.session_factory() as db:
            result = process_pending_vault_syncs(
                db, h.settings, http_client_factory=factory, now=now
            )
            assert result["queued"] == 1
            state = db.scalar(select(VaultSyncState).where(VaultSyncState.user_id == user.id))
            assert state is not None and state.next_retry_at is not None
            retry_at = state.next_retry_at.replace(tzinfo=UTC)
            assert retry_at == now + timedelta(seconds=delay)
            now = retry_at
    with h.session_factory() as db:
        result = process_pending_vault_syncs(db, h.settings, http_client_factory=factory, now=now)
    assert result["failed"] == 1
    status = client.get("/api/v1/vault/sync/status").json()
    assert status["status"] == "failed"
    assert status["next_retry_at"] is None
    with h.session_factory() as db:
        assert (
            process_pending_vault_syncs(
                db, h.settings, http_client_factory=factory, now=now + timedelta(days=1)
            )["claimed"]
            == 0
        )


def test_vault_sync_cli_worker_and_security_ssrf_csrf(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h_ssrf = make_harness(strava_api_base_url="http://169.254.169.254")
    u_ssrf = h_ssrf.create_user("ssrf-vault@example.com")
    _seed_strava_oauth(h_ssrf, u_ssrf.id, athlete_id="303030")
    c_ssrf, csrf_ssrf = h_ssrf.login_client("ssrf-vault@example.com")

    assert c_ssrf.post("/api/v1/vault/sync/strava", json={"mode": "incremental"}).status_code == 403
    # Enqueue performs no provider request, so outbound URL validation is deferred
    # to the worker and this request remains a durable queue operation.
    ssrf_resp = c_ssrf.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "incremental"},
        headers={"X-CSRF-Token": csrf_ssrf},
    )
    assert ssrf_resp.status_code == 202

    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness()
    u = h.create_user("cli-vault@example.com")
    _seed_strava_oauth(h, u.id, athlete_id="909090")
    c, csrf = h.login_client("cli-vault@example.com")

    q_resp = c.post(
        "/api/v1/vault/sync/strava",
        json={"mode": "full_backfill", "inline_first_page": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert q_resp.status_code == 202
    assert q_resp.json()["status"] == "queued"

    def cli_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v3/athlete":
            return httpx.Response(200, json={"id": 909090, "bikes": [], "shoes": []})
        if req.url.path.endswith("/stats"):
            return httpx.Response(200, json={"all_ride_totals": {"count": 1}})
        if req.url.path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    _make_strava_summary(
                        900001,
                        started_at_utc=datetime(2026, 9, 1, 7, 0, 0, tzinfo=UTC),
                        name="CLI Synced Ride",
                    )
                ],
            )
        return httpx.Response(404)

    db_url = f"sqlite:///{h.db_path}"
    dry = run_process_ingest_jobs(h.settings, dry_run=True, database_url=db_url)
    assert dry.vault_syncs_claimed == 0

    rep = run_process_ingest_jobs(
        h.settings,
        database_url=db_url,
        http_client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(cli_handler),
            follow_redirects=False,
        ),
    )
    assert rep.vault_syncs_claimed == 0
    assert rep.vault_syncs_completed == 0
    assert rep.vault_syncs_failed == 0
    assert c.get("/api/v1/vault/sync/status").json()["status"] == "queued"
