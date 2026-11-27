from __future__ import annotations

import base64
import hashlib
import os
import socket
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from trip_api.config import Settings
from trip_api.db import (
    AuditEvent,
    CreditLedgerEntry,
    IngestJob,
    OAuthConnection,
    OAuthState,
    Project,
    ProjectPhoto,
    RateLimitRecord,
    RenderJob,
    SessionRecord,
    Stage,
    User,
    VaultActivity,
    VaultSyncState,
    create_db_engine,
    create_session_factory,
)
from trip_api.ingest import process_pending_ingest_jobs
from trip_api.main import create_app
from trip_api.rate_limit import check_rate_limit
from trip_api.security import encrypt_secret, hash_password
from trip_api.storage import LocalObjectStore
from trip_api.vault_sync import upsert_strava_activity_to_vault
from trip_telemetry import build_silver_dataframe, parse_track_bytes

pytestmark = pytest.mark.postgres

_DEFAULT_TEST_POSTGRES_URL = "postgresql://trip:trip@127.0.0.1:55432/trip_test"
_API_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_PW = "correct-horse-battery-staple"


def _base_postgres_url() -> str:
    return os.environ.get("TEST_POSTGRES_URL", _DEFAULT_TEST_POSTGRES_URL)


def _to_psycopg_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    if url.startswith("postgres://"):
        return "postgresql://" + url.removeprefix("postgres://")
    return url


def _to_sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


def _replace_db_name(url: str, db_name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))


def _make_alembic_config(sa_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sa_url)
    return cfg


def _fake_public_getaddrinfo(
    *args: Any, **kwargs: Any
) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("151.101.1.69", 443),
        )
    ]


@pytest.fixture
def pg_temp_db_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    base_url = _base_postgres_url()
    admin_psycopg_url = _to_psycopg_url(base_url)
    temp_db_name = f"trip_api_test_{uuid.uuid4().hex[:12]}"

    try:
        admin_conn = psycopg.connect(admin_psycopg_url, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL test database is unreachable: {exc}")

    try:
        admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(temp_db_name)))
    finally:
        admin_conn.close()

    temp_sa_url = _to_sqlalchemy_url(_replace_db_name(base_url, temp_db_name))
    monkeypatch.setenv("DATABASE_URL", temp_sa_url)
    try:
        yield temp_sa_url
    finally:
        with psycopg.connect(admin_psycopg_url, connect_timeout=3, autocommit=True) as cleanup:
            cleanup.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(temp_db_name)
                )
            )


@pytest.fixture
def pg_app_factory(
    pg_temp_db_url: str,
    tmp_path: Path,
) -> Iterator[Any]:
    created_engines: list[Engine] = []

    def _build(
        **setting_overrides: Any,
    ) -> tuple[FastAPI, Settings, Engine, sessionmaker[Session], LocalObjectStore]:
        cfg_alembic = _make_alembic_config(pg_temp_db_url)
        command.upgrade(cfg_alembic, "head")

        store_dir = tmp_path / f"pg_objects_{len(created_engines)}"
        defaults: dict[str, Any] = {
            "app_env": "test",
            "dev_auth_enabled": False,
            "database_url": pg_temp_db_url,
            "session_secret": "postgres-test-session-secret-min-32-chars",
            "token_encryption_key": base64.urlsafe_b64encode(b"p" * 32).decode("ascii"),
            "local_object_store_dir": str(store_dir),
            "strava_client_id": "12345",
            "strava_client_secret": "strava-secret-xyz",
            "strava_verify_token": "webhook-verify-token-secret",
            "rate_limit_login_max": 20,
            "rate_limit_render_max": 20,
        }
        defaults.update(setting_overrides)
        settings = Settings(**defaults)
        engine = create_db_engine(pg_temp_db_url)
        created_engines.append(engine)
        sf = create_session_factory(engine)
        store = LocalObjectStore(store_dir)
        app = create_app(settings, engine=engine, session_factory=sf, object_store=store)
        return app, settings, engine, sf, store

    yield _build

    for eng in created_engines:
        eng.dispose()


def _create_pg_user(
    sf: sessionmaker[Session],
    email: str,
    password: str = _DEFAULT_PW,
    *,
    verified: bool = True,
    initial_credits: int = 10_000,
) -> User:
    with sf() as db:
        user = User(
            email=email.lower(),
            display_name=email.split("@", 1)[0],
            password_hash=hash_password(password),
            is_active=True,
            email_verified_at=datetime.now(UTC) if verified else None,
        )
        db.add(user)
        db.flush()
        if initial_credits > 0:
            db.add(
                CreditLedgerEntry(
                    user_id=user.id,
                    delta=initial_credits,
                    entry_type="admin_adjustment",
                    note="PG test credit seed",
                )
            )
        db.commit()
        db.refresh(user)
        return user


def _login_pg_client(
    app: FastAPI, email: str, password: str = _DEFAULT_PW
) -> tuple[TestClient, str]:
    client = TestClient(app)
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return client, str(resp.json()["csrf_token"])


def test_postgres_alembic_upgrade_downgrade_upgrade_roundtrip(pg_temp_db_url: str) -> None:
    cfg = _make_alembic_config(pg_temp_db_url)
    expected_tables = {
        "users",
        "sessions",
        "email_verification_tokens",
        "password_reset_tokens",
        "oauth_states",
        "oauth_connections",
        "projects",
        "stages",
        "project_photos",
        "render_jobs",
        "render_job_chunks",
        "ingest_jobs",
        "audit_events",
        "rate_limits",
        "vault_activities",
        "vault_activity_tiles",
        "vault_sync_states",
        "vault_gears",
        "library_views",
    }

    command.upgrade(cfg, "head")
    command.check(cfg)

    engine = create_db_engine(pg_temp_db_url)
    try:
        insp = inspect(engine)
        assert expected_tables <= set(insp.get_table_names())
        project_cols = {c["name"]: c for c in insp.get_columns("projects")}
        assert {"share_enabled", "share_slug", "share_created_at"} <= set(project_cols)
        assert "JSONB" in str(project_cols["settings_json"]["type"]).upper()
        view_cols = {c["name"]: c for c in insp.get_columns("library_views")}
        assert "JSONB" in str(view_cols["rules"]["type"]).upper()
        assert {"hidden_at", "disconnected_at"} <= {
            c["name"] for c in insp.get_columns("vault_activities")
        }
        stage_checks = {c["name"] for c in insp.get_check_constraints("stages")}
        assert {
            "ck_stages_source_type",
            "ck_stages_source_format",
            "ck_stages_activity_type",
        } <= stage_checks
        stage_cols = {c["name"] for c in insp.get_columns("stages")}
        assert "vault_activity_id" in stage_cols
        stage_fks = {fk["name"] for fk in insp.get_foreign_keys("stages")}
        assert "fk_stages_vault_activity_id_vault_activities" in stage_fks
        audit_fks = {fk["name"] for fk in insp.get_foreign_keys("audit_events")}
        assert "fk_audit_events_project_id_projects" in audit_fks
        with engine.connect() as conn:
            gin_idx = conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'vault_activities' "
                    "AND indexname = 'ix_vault_activities_name_tsv'"
                )
            ).scalar_one_or_none()
            assert gin_idx == "ix_vault_activities_name_tsv"
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine_down = create_db_engine(pg_temp_db_url)
    try:
        remaining = set(inspect(engine_down).get_table_names())
        assert expected_tables.isdisjoint(remaining)
    finally:
        engine_down.dispose()

    command.upgrade(cfg, "head")
    command.check(cfg)

    engine_reup = create_db_engine(pg_temp_db_url)
    try:
        insp_reup = inspect(engine_reup)
        assert expected_tables <= set(insp_reup.get_table_names())
        assert {"render_backend", "external_execution_id", "remote_dispatched_at"} <= {
            c["name"] for c in insp_reup.get_columns("render_jobs")
        }
        assert {
            "user_identities",
            "credit_vouchers",
            "credit_voucher_redemptions",
            "credit_ledger_entries",
        } <= set(insp_reup.get_table_names())
        with engine_reup.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0015_render_credits_vouchers"
    finally:
        engine_reup.dispose()


def test_postgres_concurrent_oauth_pkce_state_replay_only_one_succeeds(
    pg_app_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    app, _settings, _engine, sf, _store = pg_app_factory()

    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                    "refresh_token": "pg-strava-refresh",
                    "access_token": "pg-strava-access",
                    "scope": "read,activity:read_all",
                    "athlete": {"id": 990011},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )

    user = _create_pg_user(sf, "pkce-pg@example.com")
    client, _ = _login_pg_client(app, "pkce-pg@example.com")
    cookies = dict(client.cookies)
    state = str(client.get("/api/v1/strava/connect").json()["state"])

    workers = 6
    barrier = Barrier(workers)

    def _hit_callback(_idx: int) -> int:
        thread_client = TestClient(app, cookies=cookies)
        barrier.wait(timeout=10.0)
        resp = thread_client.get(f"/api/v1/strava/callback?code=code-pg&state={state}")
        return int(resp.status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = sorted(pool.map(_hit_callback, range(workers)))

    assert statuses.count(200) == 1
    assert statuses.count(400) == workers - 1
    assert token_calls == 1

    with sf() as db:
        state_row = db.scalar(select(OAuthState).where(OAuthState.user_id == user.id))
        assert state_row is not None
        assert state_row.consumed_at is not None
        conn_rows = db.scalars(
            select(OAuthConnection).where(OAuthConnection.user_id == user.id)
        ).all()
        assert len(conn_rows) == 1
        assert conn_rows[0].external_athlete_id == "990011"


def test_postgres_concurrent_first_dev_login_integrity_error_race(pg_app_factory: Any) -> None:
    app, _settings, _engine, sf, _store = pg_app_factory(
        app_env="development",
        dev_auth_enabled=True,
    )

    workers = 5
    barrier = Barrier(workers)

    def _hit_dev_login(_idx: int) -> tuple[int, str]:
        thread_client = TestClient(app)
        barrier.wait(timeout=10.0)
        resp = thread_client.post("/api/v1/auth/dev-login")
        email = str(resp.json()["user"]["email"]) if resp.status_code == 200 else ""
        return int(resp.status_code), email

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_hit_dev_login, range(workers)))

    assert all(status == 200 for status, _ in results), results
    assert all(email == "dev@trip-animator.local" for _, email in results)

    with sf() as db:
        user_count = db.scalar(
            select(func.count()).select_from(User).where(User.email == "dev@trip-animator.local")
        )
        session_count = db.scalar(select(func.count()).select_from(SessionRecord))
        assert user_count == 1
        assert session_count == workers


def test_postgres_optimistic_concurrency_revision_409_conflict(pg_app_factory: Any) -> None:
    app, _settings, _engine, sf, _store = pg_app_factory()
    _create_pg_user(sf, "occ-pg@example.com")
    client_a, csrf_a = _login_pg_client(app, "occ-pg@example.com")
    client_b = TestClient(app, cookies=dict(client_a.cookies))

    created = client_a.post(
        "/api/v1/projects",
        json={"title": "Initial Route"},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert created.status_code == 201
    proj_id = created.json()["id"]
    assert created.json()["revision"] == 1

    assert client_a.get(f"/api/v1/projects/{proj_id}").json()["revision"] == 1
    assert client_b.get(f"/api/v1/projects/{proj_id}").json()["revision"] == 1

    winner = client_a.patch(
        f"/api/v1/projects/{proj_id}",
        json={"revision": 1, "title": "Winner Title"},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert winner.status_code == 200
    assert winner.json()["revision"] == 2
    assert winner.json()["title"] == "Winner Title"

    stale = client_b.patch(
        f"/api/v1/projects/{proj_id}",
        json={"revision": 1, "title": "Stale Overwrite"},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert stale.status_code == 409
    assert "Revision conflict" in stale.json()["detail"]

    latest = client_a.get(f"/api/v1/projects/{proj_id}").json()
    assert latest["revision"] == 2
    assert latest["title"] == "Winner Title"


def test_postgres_rate_limiter_row_lock_and_concurrent_burst(pg_app_factory: Any) -> None:
    app, _settings, _engine, sf, _store = pg_app_factory()

    workers = 6
    limit = 3
    barrier = Barrier(workers)

    def _consume(_idx: int) -> str:
        from fastapi import HTTPException

        with sf() as db:
            barrier.wait(timeout=10.0)
            try:
                check_rate_limit(
                    db,
                    bucket_key="pg-burst-bucket",
                    limit=limit,
                    window_seconds=60,
                )
                return "ok"
            except HTTPException as exc:
                assert exc.status_code == 429
                return "limited"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(_consume, range(workers)))

    assert outcomes.count("ok") == limit
    assert outcomes.count("limited") == workers - limit

    with sf() as db:
        row = db.scalar(
            select(RateLimitRecord).where(RateLimitRecord.bucket_key == "pg-burst-bucket")
        )
        assert row is not None
        assert row.request_count == limit


def test_postgres_concurrent_render_idempotency_and_project_delete_audit_set_null(
    pg_app_factory: Any,
    sample_gpx: bytes,
) -> None:
    app, _settings, _engine, sf, _store = pg_app_factory()
    _create_pg_user(sf, "render-pg@example.com")
    client, csrf = _login_pg_client(app, "render-pg@example.com")
    cookies = dict(client.cookies)

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "PG Cascade Project"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    up = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("pg_stage.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert up.status_code == 201

    workers = 4
    barrier = Barrier(workers)

    def _submit_render(_idx: int) -> tuple[int, str]:
        c = TestClient(app, cookies=cookies)
        barrier.wait(timeout=10.0)
        r = c.post(
            f"/api/v1/projects/{proj_id}/renders",
            json={"resolution": "1280x720", "fps": 30},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "pg-idem-key-1234"},
        )
        return int(r.status_code), str(r.json()["id"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        render_results = list(pool.map(_submit_render, range(workers)))

    assert all(code == 202 for code, _ in render_results)
    assert len({jid for _, jid in render_results}) == 1

    del_resp = client.delete(
        f"/api/v1/projects/{proj_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert del_resp.status_code == 204

    with sf() as db:
        assert db.scalar(select(func.count()).select_from(Project)) == 0
        assert db.scalar(select(func.count()).select_from(Stage)) == 0
        assert db.scalar(select(func.count()).select_from(RenderJob)) == 0
        audits = db.scalars(select(AuditEvent)).all()
        assert any(a.event_type == "project.deleted" and a.project_id is None for a in audits)
        assert any(a.event_type == "project.created" and a.project_id is None for a in audits)


def test_postgres_concurrent_render_credit_debit_prevents_overdraft(
    pg_app_factory: Any,
    sample_gpx: bytes,
) -> None:
    app, _settings, _engine, sf, _store = pg_app_factory(max_active_renders_per_user=10)
    _create_pg_user(sf, "credit-race@example.com", initial_credits=100)
    client, csrf = _login_pg_client(app, "credit-race@example.com")
    cookies = dict(client.cookies)

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Credit Race Project"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    up = client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("race_stage.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )
    assert up.status_code == 201

    workers = 4
    barrier = Barrier(workers)

    def _submit_distinct(idx: int) -> int:
        c = TestClient(app, cookies=cookies)
        barrier.wait(timeout=10.0)
        r = c.post(
            f"/api/v1/projects/{proj_id}/renders",
            json={
                "resolution": "1920x1080",
                "fps": 30,
                "duration_seconds": 60.0,
                "intro_hold_seconds": 0.0,
                "outro_hold_seconds": 0.0,
                "terrain_3d": False,
                "show_photos": False,
            },
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"pg-credit-race-{idx:04d}"},
        )
        return int(r.status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(_submit_distinct, range(workers)))

    assert statuses.count(202) == 1
    assert statuses.count(402) == workers - 1
    assert client.get("/api/v1/credits").json()["balance"] == 0


def test_postgres_concurrent_voucher_redemption_respects_max_redemptions(
    pg_app_factory: Any,
) -> None:
    from trip_api.credits import create_voucher_record

    app, _settings, _engine, sf, _store = pg_app_factory()
    with sf() as db:
        voucher = create_voucher_record(
            db,
            credits_amount=200,
            max_redemptions=2,
            code="PG-RACE-VOUCHER",
        )

    workers = 4
    clients: list[tuple[dict[str, str], str]] = []
    for idx in range(workers):
        email = f"voucher-race-{idx}@example.com"
        _create_pg_user(sf, email, initial_credits=0)
        c, csrf = _login_pg_client(app, email)
        clients.append((dict(c.cookies), csrf))

    barrier = Barrier(workers)

    def _redeem(idx: int) -> int:
        cookies, csrf = clients[idx]
        c = TestClient(app, cookies=cookies)
        barrier.wait(timeout=10.0)
        r = c.post(
            "/api/v1/credits/redeem",
            headers={"X-CSRF-Token": csrf},
            json={"code": voucher.code},
        )
        return int(r.status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(_redeem, range(workers)))

    assert statuses.count(200) == 2
    assert statuses.count(400) == workers - 2


def test_postgres_concurrent_photo_upload_quota_for_update(
    pg_app_factory: Any,
    tmp_path: Path,
) -> None:
    import io

    from PIL import Image
    from trip_api.routers.photos import MAX_PHOTOS_PER_PROJECT

    app, _settings, _engine, sf, store = pg_app_factory(rate_limit_photo_max=50)
    user = _create_pg_user(sf, "photo-pg@example.com")
    client, csrf = _login_pg_client(app, "photo-pg@example.com")
    cookies = dict(client.cookies)

    proj_id = uuid.UUID(
        client.post(
            "/api/v1/projects",
            json={"title": "PG Photo Quota"},
            headers={"X-CSRF-Token": csrf},
        ).json()["id"]
    )

    with sf() as db:
        for idx in range(MAX_PHOTOS_PER_PROJECT - 1):
            pid = uuid.uuid4()
            db.add(
                ProjectPhoto(
                    id=pid,
                    project_id=proj_id,
                    source_type="upload",
                    object_key=f"photos/{proj_id}/{pid}.webp",
                    thumb_object_key=f"photos/{proj_id}/{pid}_thumb.webp",
                    mime_type="image/webp",
                    width=32,
                    height=32,
                    byte_size=128,
                    caption=f"seed-{idx}",
                    match_method="unmatched",
                    exif_offset_s=0,
                    enabled=True,
                )
            )
        db.commit()
    assert user.id is not None

    workers = 5
    barrier = Barrier(workers)

    def _upload(idx: int) -> int:
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), color=(249, 115, 22 + idx * 20)).save(buf, format="JPEG")
        c = TestClient(app, cookies=cookies)
        barrier.wait(timeout=10.0)
        resp = c.post(
            f"/api/v1/projects/{proj_id}/photos",
            files={"file": (f"race_{idx}.jpg", buf.getvalue(), "image/jpeg")},
            headers={"X-CSRF-Token": csrf},
        )
        return int(resp.status_code)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = sorted(pool.map(_upload, range(workers)))

    assert statuses.count(201) == 1
    assert statuses.count(422) == workers - 1

    with sf() as db:
        total_photos = db.scalar(
            select(func.count()).select_from(ProjectPhoto).where(ProjectPhoto.project_id == proj_id)
        )
        assert total_photos == MAX_PHOTOS_PER_PROJECT

    uploaded_webp_files = list(Path(store.root_dir).rglob("*.webp"))
    assert len(uploaded_webp_files) == 2


def test_postgres_concurrent_duplicate_photo_upload_returns_conflict_and_cleans_loser(
    pg_app_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from PIL import Image

    app, _settings, engine, sf, store = pg_app_factory(rate_limit_photo_max=50)
    assert engine.dialect.name == "postgresql"
    user = _create_pg_user(sf, "photo-duplicate-pg@example.com")
    client, csrf = _login_pg_client(app, "photo-duplicate-pg@example.com")
    cookies = dict(client.cookies)
    project_id = uuid.UUID(
        client.post(
            "/api/v1/projects",
            json={"title": "PG Duplicate Photo"},
            headers={"X-CSRF-Token": csrf},
        ).json()["id"]
    )
    assert user.id is not None

    image = io.BytesIO()
    Image.new("RGB", (32, 32), color=(24, 128, 232)).save(image, format="JPEG")
    jpeg_bytes = image.getvalue()
    put_barrier = Barrier(2)
    original_put_bytes = store.put_bytes

    def synchronized_put_bytes(
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        original_put_bytes(key, data, content_type=content_type)
        if key.endswith(".webp") and not key.endswith("_thumb.webp"):
            put_barrier.wait(timeout=10.0)

    monkeypatch.setattr(store, "put_bytes", synchronized_put_bytes)

    def _upload(filename: str) -> int:
        thread_client = TestClient(app, cookies=cookies)
        response = thread_client.post(
            f"/api/v1/projects/{project_id}/photos",
            files={"file": (filename, jpeg_bytes, "image/jpeg")},
            headers={"X-CSRF-Token": csrf},
        )
        return int(response.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(_upload, ("duplicate-a.jpg", "duplicate-b.jpg")))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 1

    with sf() as db:
        photos = db.scalars(select(ProjectPhoto).where(ProjectPhoto.project_id == project_id)).all()
        assert len(photos) == 1
        persisted_keys = {photos[0].object_key, photos[0].thumb_object_key}

    object_files = {
        path.relative_to(store.root_dir).as_posix()
        for path in Path(store.root_dir).rglob("*")
        if path.is_file()
    }
    assert object_files == persisted_keys


def test_postgres_activity_search_backfills_names_and_preserves_literal_filters(
    pg_app_factory: Any,
    pg_temp_db_url: str,
) -> None:
    app, _settings, engine, sf, _store = pg_app_factory()
    user = _create_pg_user(sf, "search-backfill@example.com")
    other = _create_pg_user(sf, "search-isolation@example.com")
    now = datetime.now(UTC)
    first_id = uuid.uuid4()
    with sf() as db:
        for index, (owner_id, name) in enumerate(
            [
                (user.id, "Koło"),
                (user.id, "Koło12"),
                (user.id, "100%_!"),
                (user.id, "ß" * 200),
                (other.id, "Koło"),
            ]
        ):
            db.add(
                VaultActivity(
                    id=first_id if index == 0 else uuid.uuid4(),
                    user_id=owner_id,
                    source_provider="strava",
                    external_activity_id=str(uuid.uuid4()),
                    name=name,
                    started_at=now,
                    local_date=now.date(),
                    has_gps=True,
                    summary_polyline="_p~iF~ps|U_ulLnnqC_mqNvxq`@",
                )
            )
        db.commit()

    cfg = _make_alembic_config(pg_temp_db_url)
    command.downgrade(cfg, "0010_project_photo_content_hash")
    assert "search_name" not in {
        column["name"] for column in inspect(engine).get_columns("vault_activities")
    }
    command.upgrade(cfg, "head")
    with sf() as db:
        assert (
            db.scalar(select(VaultActivity.search_name).where(VaultActivity.name == "ß" * 200))
            == "ss" * 200
        )

    client, _csrf = _login_pg_client(app, "search-backfill@example.com")
    for term in ("kolo", "KOŁO", "koło"):
        listing = client.get("/api/v1/vault/activities", params={"name": term, "limit": 1}).json()
        assert listing["total"] == 2
        assert len(listing["items"]) == 1
        assert listing["summary"]["activity_count"] == 2
        second = client.get(
            "/api/v1/vault/activities", params={"name": term, "limit": 1, "offset": 1}
        ).json()
        assert second["items"][0]["id"] != listing["items"][0]["id"]
        assert (
            client.get("/api/v1/vault/activities/map", params={"name": term}).json()["total"] == 2
        )
    for term in ("%", "_", "!", "ss" * 80):
        assert client.get("/api/v1/vault/activities", params={"name": term}).json()["total"] == 1

    with sf() as db:
        activity = db.get(VaultActivity, first_id)
        assert activity is not None
        activity.name = "Żółw"
        db.commit()
    assert client.get("/api/v1/vault/activities", params={"name": "zolw"}).json()["total"] == 1
    assert client.get("/api/v1/vault/activities", params={"name": "kolo"}).json()["total"] == 1


def test_postgres_catalog_preserves_strava_sport_from_import(pg_app_factory: Any) -> None:
    app, _settings, _engine, sf, _store = pg_app_factory()
    user = _create_pg_user(sf, "provider-sport-pg@example.com")
    now = datetime.now(UTC)
    with sf() as db:
        for item in (
            {"id": 1, "name": "Morning dance", "sport_type": "Dance", "type": "Workout"},
            {"id": 2, "name": "Afternoon ride", "type": "Ride"},
        ):
            upsert_strava_activity_to_vault(db, user_id=user.id, item=item, now=now)
        db.commit()

    client, _csrf = _login_pg_client(app, "provider-sport-pg@example.com")
    base = "/api/v1/vault/activities"
    assert client.get(f"{base}/sports").json() == ["Dance", "Ride"]
    response = client.get(base, params={"sport": "Dance"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [item["activity_type"] for item in body["items"]] == ["Dance"]
    assert [item["activity_type"] for item in body["by_sport"]] == ["Dance"]


def test_postgres_concurrent_ingest_parsed_track_unique_stage_order(
    pg_app_factory: Any,
    sample_gpx: bytes,
) -> None:
    from trip_api.services import ingest_parsed_track
    from trip_telemetry import parse_gpx_bytes

    _app, _settings, _engine, sf, store = pg_app_factory()
    user = _create_pg_user(sf, "stage-order-pg@example.com")
    with sf() as db:
        project = Project(owner_user_id=user.id, title="PG Stage Order")
        db.add(project)
        db.commit()
        db.refresh(project)
        project_id = project.id

    parsed = parse_gpx_bytes(sample_gpx)
    workers = 6
    barrier = Barrier(workers)

    def _ingest(idx: int) -> int:
        with sf() as db:
            proj = db.get(Project, project_id)
            assert proj is not None
            barrier.wait(timeout=10.0)
            stage = ingest_parsed_track(
                db=db,
                store=store,
                project=proj,
                parsed=parsed,
                raw_bronze_bytes=sample_gpx,
                bronze_ext="gpx",
                bronze_content_type="application/gpx+xml",
                source_type="gpx",
                stage_name=f"Stage {idx}",
                day_index=1,
                auto_align_photos=False,
            )
            return int(stage.stage_order)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        orders = sorted(pool.map(_ingest, range(workers)))

    assert orders == list(range(workers))


def test_postgres_concurrent_strava_token_refresh_for_update(
    pg_app_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from threading import Lock

    from trip_api.routers.strava import _get_valid_access_token, _oauth_aad
    from trip_api.security import encrypt_secret

    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    _app, settings, _engine, sf, _store = pg_app_factory()
    user = _create_pg_user(sf, "strava-refresh-pg@example.com")

    key = settings.decoded_encryption_key
    aad = _oauth_aad(user.id)
    with sf() as db:
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="554433",
                access_token_enc=encrypt_secret("expired-access", key, aad=aad),
                refresh_token_enc=encrypt_secret("old-refresh", key, aad=aad),
                expires_at=datetime.now(UTC) - timedelta(minutes=5),
                scope="read,activity:read_all",
            )
        )
        db.commit()

    refresh_lock = Lock()
    refresh_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            with refresh_lock:
                refresh_calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                    "refresh_token": "fresh-pg-refresh",
                    "access_token": "fresh-pg-access",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client_factory = lambda: httpx.AsyncClient(transport=transport, follow_redirects=False)  # noqa: E731

    workers = 6
    barrier = Barrier(workers)

    def _refresh(_idx: int) -> str:
        with sf() as db:
            barrier.wait(timeout=10.0)
            return asyncio.run(
                _get_valid_access_token(
                    db=db,
                    settings=settings,
                    user_id=user.id,
                    client_factory=client_factory,
                )
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        tokens = list(pool.map(_refresh, range(workers)))

    assert tokens == ["fresh-pg-access"] * workers
    assert refresh_calls["n"] == 1


def test_postgres_concurrent_vault_sync_claims_queued_state_once(
    pg_app_factory: Any,
) -> None:
    from trip_api.vault_sync import process_pending_vault_syncs

    _app, settings, _engine, sf, _store = pg_app_factory()
    user = _create_pg_user(sf, "vault-sync-pg@example.com")
    aad = f"oauth:{user.id}:strava".encode()
    with sf() as db:
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="771122",
                access_token_enc=encrypt_secret(
                    "vault-sync-access", settings.decoded_encryption_key, aad=aad
                ),
                refresh_token_enc=encrypt_secret(
                    "vault-sync-refresh", settings.decoded_encryption_key, aad=aad
                ),
                expires_at=datetime.now(UTC) + timedelta(days=365),
                scope="read,activity:read_all",
            )
        )
        db.add(
            VaultSyncState(
                user_id=user.id,
                provider="strava",
                status="queued",
                phase="discovering_pages",
            )
        )
        db.commit()

    request_entered = Event()
    release_request = Event()
    request_lock = Lock()
    provider_fetches = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_fetches
        if _request.url.path.endswith("/athlete/activities"):
            with request_lock:
                provider_fetches += 1
        request_entered.set()
        if not release_request.wait(timeout=10.0):
            raise TimeoutError("Vault sync provider request was not released")
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client_factory = lambda: httpx.Client(transport=transport, follow_redirects=False)  # noqa: E731

    def _run_worker() -> dict[str, int]:
        with sf() as db:
            return process_pending_vault_syncs(db, settings, http_client_factory=client_factory)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first_worker = pool.submit(_run_worker)
        try:
            assert request_entered.wait(timeout=10.0), "first worker did not reach provider fetch"
            with sf() as db:
                second_stats = process_pending_vault_syncs(
                    db, settings, http_client_factory=client_factory
                )
            assert second_stats["claimed"] == 0
        finally:
            release_request.set()
        first_stats = first_worker.result(timeout=15.0)

    assert first_stats["claimed"] == 1
    assert first_stats["completed"] == 1
    assert second_stats["completed"] == 0
    assert provider_fetches == 1

    with sf() as db:
        state = db.scalar(
            select(VaultSyncState).where(
                VaultSyncState.user_id == user.id,
                VaultSyncState.provider == "strava",
            )
        )
        assert state is not None
        assert state.status == "completed"
        assert state.lease_expires_at is None


@pytest.mark.parametrize("reuse_path", ["create", "append"])
def test_postgres_library_reuse_does_not_lock_source_during_artifact_copy(
    reuse_path: str,
    pg_app_factory: Any,
    pg_temp_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _settings, engine, sf, store = pg_app_factory()
    assert engine.dialect.name == "postgresql"
    user = _create_pg_user(sf, f"library-race-{reuse_path}@example.com")
    activity_id = uuid.uuid4()
    source_project_id = uuid.uuid4()
    source_stage_id = uuid.uuid4()
    bronze_key = f"bronze/{source_project_id}/{source_stage_id}/original.gpx"
    silver_key = f"silver/{source_project_id}/{source_stage_id}.parquet"
    original_bronze = (
        b'<gpx version="1.1" creator="local-test"><trk><trkseg>'
        b'<trkpt lat="52.0" lon="21.0"><time>2026-01-01T10:00:00Z</time></trkpt>'
        b'<trkpt lat="52.01" lon="21.01"><time>2026-01-01T10:01:00Z</time></trkpt>'
        b'<trkpt lat="52.02" lon="21.02"><time>2026-01-01T10:02:00Z</time></trkpt>'
        b"</trkseg></trk></gpx>"
    )
    frame, _ = build_silver_dataframe(
        parse_track_bytes(original_bronze, format_hint="gpx"),
        trip_id=str(source_project_id),
        stage_id=str(source_stage_id),
    )
    buffer = BytesIO()
    frame.write_parquet(buffer)
    original_silver = buffer.getvalue()
    store.put_bytes(bronze_key, original_bronze)
    store.put_bytes(silver_key, original_silver)
    with sf() as db:
        source_project = Project(
            id=source_project_id, owner_user_id=user.id, title="Canonical source"
        )
        activity = VaultActivity(
            id=activity_id,
            user_id=user.id,
            source_provider="strava",
            external_activity_id=str(activity_id),
            name="Concurrent ride",
            activity_type="cycling",
            started_at=datetime.now(UTC),
            local_date=datetime.now(UTC).date(),
            bronze_object_key=bronze_key,
            has_gps=True,
            tile_precision="full_stream",
        )
        source_stage = Stage(
            id=source_stage_id,
            project_id=source_project_id,
            source_type="strava",
            source_format="gpx",
            activity_type="cycling",
            vault_activity_id=activity_id,
            name="Concurrent ride",
            stage_order=0,
            day_index=1,
            bronze_object_key=bronze_key,
            silver_object_key=silver_key,
            bronze_sha256=hashlib.sha256(original_bronze).hexdigest(),
            point_count=3,
            distance_m=1234.0,
            elevation_gain_m=20.0,
            elevation_loss_m=2.0,
            moving_seconds=300.0,
            elapsed_seconds=320.0,
            has_timestamps=True,
            warnings_json=[],
        )
        db.add_all([source_project, activity])
        db.flush()
        db.add(source_stage)
        db.flush()
        activity.source_stage_id = source_stage.id
        if reuse_path == "append":
            target = Project(owner_user_id=user.id, title="Append target")
            db.add(target)
            db.flush()
            target_project_id = target.id
        else:
            target_project_id = source_project_id
        db.commit()

    client, csrf = _login_pg_client(app, user.email)
    copy_started = Event()
    allow_copy_to_finish = Event()
    request_backend_pid: list[int] = []

    def capture_request_backend(
        conn: Any, _cursor: Any, _statement: str, _parameters: Any, _context: Any, _many: bool
    ) -> None:
        if not request_backend_pid:
            raw_connection = conn.connection.driver_connection
            request_backend_pid.append(int(raw_connection.info.backend_pid))

    original_put = store.put_bytes

    def paused_put(
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        original_put(key, data, content_type=content_type)
        if key.startswith(("bronze/", "silver/")) and not copy_started.is_set():
            copy_started.set()
            if not allow_copy_to_finish.wait(timeout=10.0):
                raise TimeoutError("Test did not release artifact copy")

    monkeypatch.setattr(store, "put_bytes", paused_put)
    event.listen(engine, "before_cursor_execute", capture_request_backend)
    endpoint = (
        "/api/v1/library/projects"
        if reuse_path == "create"
        else f"/api/v1/library/projects/{target_project_id}/activities"
    )
    payload = (
        {"title": "Concurrent reuse", "activity_ids": [str(activity_id)]}
        if reuse_path == "create"
        else {"activity_ids": [str(activity_id)]}
    )
    before_counts: tuple[int, int]
    with sf() as db:
        before_counts = (
            int(db.scalar(select(func.count()).select_from(Project)) or 0),
            int(db.scalar(select(func.count()).select_from(Stage)) or 0),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, endpoint, json=payload, headers={"X-CSRF-Token": csrf})
        try:
            assert copy_started.wait(timeout=10.0), "HTTP request did not reach artifact copy"
            assert request_backend_pid
            with psycopg.connect(_to_psycopg_url(pg_temp_db_url), connect_timeout=3) as concurrent:
                concurrent_pid = int(concurrent.info.backend_pid)
                assert concurrent_pid != request_backend_pid[0]
                concurrent.execute("SET LOCAL lock_timeout = '1s'")
                concurrent.execute(
                    "UPDATE vault_activities SET disconnected_at = now() WHERE id = %s",
                    (activity_id,),
                )
                concurrent.commit()
        finally:
            allow_copy_to_finish.set()
        response = request.result(timeout=15.0)
    event.remove(engine, "before_cursor_execute", capture_request_backend)

    assert response.status_code == 409, response.text
    assert store.get_bytes(bronze_key) == original_bronze
    assert store.get_bytes(silver_key) == original_silver
    stored_keys = {
        path.relative_to(store.root_dir).as_posix()
        for path in Path(store.root_dir).rglob("*")
        if path.is_file()
    }
    assert stored_keys == {bronze_key, silver_key}
    with sf() as db:
        after_counts = (
            int(db.scalar(select(func.count()).select_from(Project)) or 0),
            int(db.scalar(select(func.count()).select_from(Stage)) or 0),
        )
        assert after_counts == before_counts
        activity = db.get(VaultActivity, activity_id)
        assert activity is not None and activity.disconnected_at is not None


@pytest.mark.parametrize("invalidation", [None, "disconnect", "delete", "oauth", "deactivate"])
def test_postgres_enrichment_revalidates_after_stream_io_and_preserves_project_edits(
    pg_app_factory: Any, monkeypatch: pytest.MonkeyPatch, invalidation: str | None
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    _, settings, _, sf, store = pg_app_factory()
    user = _create_pg_user(sf, "enrichment-race@example.com")
    now = datetime.now(UTC)
    summary = {
        "id": 8801,
        "name": "Original ride",
        "sport_type": "Ride",
        "start_date": "2026-06-01T08:00:00Z",
        "start_latlng": [46.5, 11.8],
        "end_latlng": [46.52, 11.82],
        "map": {"summary_polyline": ""},
        "distance": 4200,
        "moving_time": 120,
        "elapsed_time": 120,
    }
    with sf() as db:
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="777888",
                access_token_enc=encrypt_secret(
                    "synthetic-access",
                    settings.decoded_encryption_key,
                    aad=f"oauth:{user.id}:strava".encode(),
                ),
                refresh_token_enc=encrypt_secret(
                    "synthetic-refresh",
                    settings.decoded_encryption_key,
                    aad=f"oauth:{user.id}:strava".encode(),
                ),
                expires_at=now + timedelta(hours=1),
                scope="read,activity:read_all",
            )
        )
        activity = upsert_strava_activity_to_vault(
            db, user_id=user.id, item=summary, now=now
        ).vault_activity
        activity_id = activity.id
        project = Project(owner_user_id=user.id, title="QA — Edited project", settings_json={})
        db.add(project)
        db.flush()
        stage = Stage(
            project_id=project.id,
            source_type="strava",
            source_format="strava",
            activity_type="cycling",
            strava_activity_id=8801,
            vault_activity_id=activity_id,
            name="Edited project geometry",
            stage_order=0,
            day_index=1,
            bronze_object_key="bronze/edited.json",
            silver_object_key="silver/edited.parquet",
            point_count=2,
            distance_m=90,
            trim_start_s=30,
            trim_end_s=60,
        )
        db.add(stage)
        db.flush()
        stage_id = stage.id
        activity.source_stage_id = stage_id
        db.add(
            IngestJob(
                provider="strava",
                event_type="update",
                external_owner_id="777888",
                external_object_id=8801,
                status="queued",
                payload_json={
                    "kind": "library_enrichment",
                    "object_type": "activity",
                    "owner_user_id": str(user.id),
                    "catalog_activity_id": str(activity_id),
                },
            )
        )
        db.commit()
    store.put_bytes("bronze/edited.json", b"preserved project source")
    store.put_bytes("silver/edited.parquet", b"preserved project edits")

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/streams"):
            return httpx.Response(200, json=summary)
        if invalidation is not None:
            with sf() as concurrent:
                concurrent.execute(text("SET LOCAL lock_timeout = '1s'"))
                row = concurrent.get(VaultActivity, activity_id)
                assert row is not None
                if invalidation == "disconnect":
                    row.disconnected_at = now
                elif invalidation == "delete":
                    concurrent.delete(row)
                elif invalidation == "deactivate":
                    concurrent.get(User, user.id).is_active = False
                else:
                    concurrent.delete(concurrent.scalar(select(OAuthConnection)))
                concurrent.commit()
        return httpx.Response(
            200,
            json={
                "latlng": {"data": [[46.5, 11.8], [46.51, 11.81], [46.52, 11.82]]},
                "time": {"data": [0, 60, 120]},
                "altitude": {"data": [1000, 1010, 1020]},
            },
        )

    with sf() as db:
        result = process_pending_ingest_jobs(
            db,
            store,
            settings,
            http_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert result["failed"] == 0
        assert result["succeeded" if invalidation is None else "ignored"] == 1
        assert db.scalar(select(func.count()).select_from(Project)) == 1
        assert db.scalar(select(func.count()).select_from(Stage)) == 1
        edited = db.get(Stage, stage_id)
        assert edited is not None and edited.distance_m == 90
        assert (edited.trim_start_s, edited.trim_end_s) == (30, 60)
        original = db.get(VaultActivity, activity_id)
        if invalidation is None:
            assert original is not None and original.source_stage_id is None
            assert original.silver_object_key != edited.silver_object_key
            assert original.tile_precision == "full_stream"
            assert store.get_bytes(original.bronze_object_key)
            assert store.get_bytes(original.silver_object_key)
        elif invalidation == "delete":
            assert original is None
            assert db.scalar(select(VaultActivity.id)) is None
        else:
            assert original is not None and original.silver_object_key is None
    assert store.get_bytes("bronze/edited.json") == b"preserved project source"
    assert store.get_bytes("silver/edited.parquet") == b"preserved project edits"
    if invalidation is not None:
        assert {
            str(path.relative_to(store.root_dir))
            for path in Path(store.root_dir).rglob("*")
            if path.is_file()
        } == {"bronze/edited.json", "silver/edited.parquet"}


def test_postgres_shared_render_event_hub_fans_out_pg_notify(pg_app_factory: Any) -> None:
    import asyncio
    import json

    from trip_api.render_events import RenderEventHub

    _app, settings, _engine, sf, _store = pg_app_factory()

    async def _exercise() -> None:
        hub = RenderEventHub(settings.database_url)
        job_id = uuid.uuid4()
        other_job_id = uuid.uuid4()
        try:
            async with (
                hub.subscribe(job_id) as q1,
                hub.subscribe(job_id) as q2,
                hub.subscribe(other_job_id) as q3,
            ):
                await asyncio.sleep(0.25)
                payload = json.dumps(
                    {"job_id": str(job_id), "status": "running", "progress_pct": 55}
                )
                with sf() as db:
                    db.execute(text("SELECT pg_notify('render_job_events', :p)"), {"p": payload})
                    db.commit()

                msg1 = await asyncio.wait_for(q1.get(), timeout=3.0)
                msg2 = await asyncio.wait_for(q2.get(), timeout=3.0)
                assert msg1["job_id"] == str(job_id)
                assert msg1["progress_pct"] == 55
                assert msg2 == msg1
                assert q3.empty()
        finally:
            await hub.close()

    asyncio.run(_exercise())
