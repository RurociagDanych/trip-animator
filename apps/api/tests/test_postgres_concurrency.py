from __future__ import annotations

import base64
import os
import socket
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
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
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from trip_api.config import Settings
from trip_api.db import (
    AuditEvent,
    OAuthConnection,
    OAuthState,
    Project,
    ProjectPhoto,
    RateLimitRecord,
    RenderJob,
    SessionRecord,
    Stage,
    User,
    create_db_engine,
    create_session_factory,
)
from trip_api.main import create_app
from trip_api.rate_limit import check_rate_limit
from trip_api.security import hash_password
from trip_api.storage import LocalObjectStore

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
    """Provision an isolated temporary PostgreSQL database for a single test."""
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


def _create_pg_user(sf: sessionmaker[Session], email: str, password: str = _DEFAULT_PW) -> User:
    with sf() as db:
        user = User(
            email=email.lower(),
            display_name=email.split("@", 1)[0],
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
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
        stage_checks = {c["name"] for c in insp.get_check_constraints("stages")}
        assert {
            "ck_stages_source_type",
            "ck_stages_source_format",
            "ck_stages_activity_type",
        } <= stage_checks
        audit_fks = {fk["name"] for fk in insp.get_foreign_keys("audit_events")}
        assert "fk_audit_events_project_id_projects" in audit_fks
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
        assert expected_tables <= set(inspect(engine_reup).get_table_names())
        with engine_reup.connect() as conn:
            ver = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert ver == "0004_etl_chunks_ingest"
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

    # Both clients observe revision 1 before writing.
    assert client_a.get(f"/api/v1/projects/{proj_id}").json()["revision"] == 1
    assert client_b.get(f"/api/v1/projects/{proj_id}").json()["revision"] == 1

    # First writer wins and advances revision to 2.
    winner = client_a.patch(
        f"/api/v1/projects/{proj_id}",
        json={"revision": 1, "title": "Winner Title"},
        headers={"X-CSRF-Token": csrf_a},
    )
    assert winner.status_code == 200
    assert winner.json()["revision"] == 2
    assert winner.json()["title"] == "Winner Title"

    # Second writer using stale revision 1 fails with 409 Conflict.
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

    # Deleting project removes stages/render_jobs via ON DELETE CASCADE and keeps audit_events
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
        # Prior project events had FK ON DELETE SET NULL triggered by PostgreSQL
        assert any(a.event_type == "project.created" and a.project_id is None for a in audits)


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

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(249, 115, 22)).save(buf, format="JPEG")
    jpg_bytes = buf.getvalue()

    workers = 5
    barrier = Barrier(workers)

    def _upload(idx: int) -> int:
        c = TestClient(app, cookies=cookies)
        barrier.wait(timeout=10.0)
        resp = c.post(
            f"/api/v1/projects/{proj_id}/photos",
            files={"file": (f"race_{idx}.jpg", jpg_bytes, "image/jpeg")},
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

    # Rejected uploads rolled back their stored blobs; only the single winner's 2 WebP files remain
    uploaded_webp_files = list(Path(store.root_dir).rglob("*.webp"))
    assert len(uploaded_webp_files) == 2


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
