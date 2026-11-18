from __future__ import annotations

import base64
import getpass
import io
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from starlette.requests import Request
from trip_api.cli import main as cli_main
from trip_api.db import (
    AuditEvent,
    EmailVerificationToken,
    OAuthConnection,
    PasswordResetToken,
    RateLimitRecord,
    RenderJob,
    SessionRecord,
    User,
    create_db_engine,
)
from trip_api.deps import get_client_ip
from trip_api.main import create_app
from trip_api.security import hash_password, hash_session_token, verify_password
from trip_api.storage import LocalObjectStore
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

if TYPE_CHECKING:
    from conftest import AppHarness, HarnessFactory


def test_alembic_initial_migration_and_schema_parity(tmp_path: Path) -> None:
    db_file = tmp_path / "alembic_check.db"
    db_url = f"sqlite:///{db_file}"
    api_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Upgrade to 0004 first to verify 0005 backfills existing users' email_verified_at.
    command.upgrade(cfg, "0004_etl_chunks_ingest")
    pre_engine = create_db_engine(db_url)
    try:
        with pre_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, password_hash, is_active) "
                    "VALUES (:id, :email, :name, :pw, :active)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "email": "legacy@example.com",
                    "name": "Legacy Rider",
                    "pw": "argon2-placeholder",
                    "active": True,
                },
            )
    finally:
        pre_engine.dispose()

    command.upgrade(cfg, "head")
    command.check(cfg)

    engine = create_db_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "users",
            "sessions",
            "email_verification_tokens",
            "password_reset_tokens",
            "oauth_states",
            "oauth_connections",
            "projects",
            "stages",
            "render_jobs",
            "audit_events",
            "rate_limits",
        }.issubset(tables)
        with engine.connect() as conn:
            verified_at = conn.execute(
                text("SELECT email_verified_at FROM users WHERE email = 'legacy@example.com'")
            ).scalar_one()
            assert verified_at is not None
    finally:
        engine.dispose()


def test_cli_create_user_argon2id_and_duplicate_rejection(tmp_path: Path) -> None:
    db_file = tmp_path / "cli.db"
    db_url = f"sqlite:///{db_file}"
    api_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    rc = cli_main(
        [
            "create-user",
            "--email",
            "Rider@Example.com",
            "--password",
            "correct-horse-battery-staple",
            "--display-name",
            "Fictional Rider",
            "--database-url",
            db_url,
        ]
    )
    assert rc == 0

    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(select(User)).mappings().one()
            assert row["email"] == "rider@example.com"
            assert row["display_name"] == "Fictional Rider"
            assert row["email_verified_at"] is not None
            assert str(row["password_hash"]).startswith("$argon2id$")
            assert verify_password(str(row["password_hash"]), "correct-horse-battery-staple")
    finally:
        engine.dispose()

    dup_rc = cli_main(
        [
            "create-user",
            "--email",
            "rider@example.com",
            "--password",
            "another-valid-password",
            "--database-url",
            db_url,
        ]
    )
    assert dup_rc == 1


def test_cli_create_user_login_parity_and_input_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from trip_api.config import Settings
    from trip_api.db import create_session_factory

    db_file = tmp_path / "cli_parity.db"
    db_url = f"sqlite:///{db_file}"
    api_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")

    # Invalid email and short password fail cleanly with exit code 1
    assert (
        cli_main(
            [
                "create-user",
                "--email",
                "invalid-email-no-at",
                "--password",
                "valid-password-123",
                "--database-url",
                db_url,
            ]
        )
        == 1
    )
    assert "Invalid email address" in capsys.readouterr().err

    assert (
        cli_main(
            [
                "create-user",
                "--email",
                "short@example.com",
                "--password",
                "too-short",
                "--database-url",
                db_url,
            ]
        )
        == 1
    )
    assert "at least 12 characters" in capsys.readouterr().err

    # 1) Standard --password CLI flag with mixed-case email and padded display name
    assert (
        cli_main(
            [
                "create-user",
                "--email",
                "  MixEd.User@Example.COM  ",
                "--password",
                "cli-created-pass-1234",
                "--display-name",
                "  Mountain Goat  ",
                "--database-url",
                db_url,
            ]
        )
        == 0
    )

    # 2) --password-stdin mode
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-secret-pass-999\n"))
    assert (
        cli_main(
            [
                "create-user",
                "--email",
                "stdin@example.com",
                "--password-stdin",
                "--database-url",
                db_url,
            ]
        )
        == 0
    )

    # 3) Interactive getpass fallback
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": "getpass-secret-pass-777")
    assert (
        cli_main(
            [
                "create-user",
                "--email",
                "prompt@example.com",
                "--database-url",
                db_url,
            ]
        )
        == 0
    )

    # Verify HTTP POST /api/v1/auth/login parity against the exact same database
    engine = create_db_engine(db_url)
    try:
        sf = create_session_factory(engine)
        settings = Settings(
            app_env="test",
            dev_auth_enabled=False,
            database_url=db_url,
            session_secret="cli-parity-session-secret-min-32-chars",
            token_encryption_key=base64.urlsafe_b64encode(b"c" * 32).decode("ascii"),
            local_object_store_dir=str(tmp_path / "objects"),
        )
        app = create_app(
            settings,
            engine=engine,
            session_factory=sf,
            object_store=LocalObjectStore(tmp_path / "objects"),
        )
        for login_email, pw, expected_email, expected_name in (
            (
                "MIXED.USER@EXAMPLE.COM",
                "cli-created-pass-1234",
                "mixed.user@example.com",
                "Mountain Goat",
            ),
            ("stdin@example.com", "stdin-secret-pass-999", "stdin@example.com", "stdin"),
            ("prompt@example.com", "getpass-secret-pass-777", "prompt@example.com", "prompt"),
        ):
            client = TestClient(app)
            login_resp = client.post(
                "/api/v1/auth/login",
                json={"email": login_email, "password": pw},
            )
            assert login_resp.status_code == 200, login_resp.text
            body = login_resp.json()
            assert body["user"]["email"] == expected_email
            assert body["user"]["display_name"] == expected_name
            me_resp = client.get("/api/v1/auth/me")
            assert me_resp.status_code == 200
            assert me_resp.json()["user"]["email"] == expected_email
    finally:
        engine.dispose()


def test_login_session_cookie_csrf_and_logout(harness: AppHarness) -> None:
    harness.create_user("alice@example.com", "alice-secret-password-123")
    client = TestClient(harness.app)

    # Unknown email exercises constant-time dummy Argon2 check
    unknown_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "wrong-password"},
    )
    assert unknown_resp.status_code == 401

    bad_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "wrong-password"},
    )
    assert bad_resp.status_code == 401

    login_resp = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "alice-secret-password-123"},
    )
    assert login_resp.status_code == 200
    data = login_resp.json()
    csrf = data["csrf_token"]
    assert data["user"]["email"] == "alice@example.com"
    assert "trip_session" in client.cookies

    raw_cookie = client.cookies.get("trip_session")
    assert raw_cookie is not None
    expected_hash = hash_session_token(raw_cookie, harness.settings.session_secret)
    with harness.session_factory() as db:
        session_row = db.scalar(select(SessionRecord))
        assert session_row is not None
        assert session_row.token_hash == expected_hash
        assert session_row.token_hash != raw_cookie

    set_cookie_hdr = login_resp.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie_hdr
    assert "samesite=lax" in set_cookie_hdr

    me_resp = client.get("/api/v1/auth/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["user"]["email"] == "alice@example.com"

    # Mutating route without CSRF header must fail with 403
    no_csrf_logout = client.post("/api/v1/auth/logout")
    assert no_csrf_logout.status_code == 403

    bad_csrf_logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "wrong-token"})
    assert bad_csrf_logout.status_code == 403

    ok_logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert ok_logout.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_tampered_cookies_inactive_user_and_prod_cookie_flags(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness()
    user = h.create_user("expiry@example.com", "expiry-secret-password-123")
    client, _csrf = h.login_client("expiry@example.com", "expiry-secret-password-123")
    raw_cookie = client.cookies.get("trip_session")
    assert raw_cookie is not None

    # Tampered cookies are rejected with 401
    tampered = raw_cookie[:-1] + ("A" if raw_cookie[-1] != "A" else "B")
    for bad_val in (tampered, "completely-forged-token", ""):
        bad_c = TestClient(h.app, cookies={"trip_session": bad_val})
        assert bad_c.get("/api/v1/auth/me").status_code == 401

    # Expired session returns 401 and deletes the expired SessionRecord from DB
    with h.session_factory() as db:
        sess = db.scalar(select(SessionRecord).where(SessionRecord.user_id == user.id))
        assert sess is not None
        sess_id = sess.id
        sess.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()

    assert client.get("/api/v1/auth/me").status_code == 401
    with h.session_factory() as db:
        assert db.get(SessionRecord, sess_id) is None

    # Inactive user cannot use existing session or log in again
    client2, _ = h.login_client("expiry@example.com", "expiry-secret-password-123")
    with h.session_factory() as db:
        db_user = db.get(User, user.id)
        assert db_user is not None
        db_user.is_active = False
        db.commit()

    assert client2.get("/api/v1/auth/me").status_code == 401
    assert (
        TestClient(h.app)
        .post(
            "/api/v1/auth/login",
            json={"email": "expiry@example.com", "password": "expiry-secret-password-123"},
        )
        .status_code
        == 401
    )

    # Production environment sets Secure flag on session cookie
    prod_h = make_harness(
        app_env="production",
        dev_auth_enabled=False,
        session_secret="prod-cookie-secret-minimum-32-characters",
        token_encryption_key=base64.urlsafe_b64encode(b"p" * 32).decode("ascii"),
    )
    prod_h.create_user("prod@example.com", "prod-secret-password-123")
    prod_login = TestClient(prod_h.app).post(
        "/api/v1/auth/login",
        json={"email": "prod@example.com", "password": "prod-secret-password-123"},
    )
    assert prod_login.status_code == 200
    cookie_hdr = prod_login.headers.get("set-cookie", "").lower()
    assert "secure" in cookie_hdr
    assert "httponly" in cookie_hdr


def test_dev_login_disabled_in_prod_and_allowed_in_dev(make_harness: HarnessFactory) -> None:
    prod_harness = make_harness(
        app_env="production",
        dev_auth_enabled=False,
        session_secret="prod-session-secret-minimum-32-characters-long",
        token_encryption_key=base64.urlsafe_b64encode(b"z" * 32).decode("ascii"),
    )
    prod_client = TestClient(prod_harness.app)
    assert prod_client.post("/api/v1/auth/dev-login").status_code == 403
    assert prod_client.get("/api/v1/public-config").json()["dev_auth_enabled"] is False

    dev_harness = make_harness(app_env="development", dev_auth_enabled=True)
    dev_client = TestClient(dev_harness.app)
    resp = dev_client.post("/api/v1/auth/dev-login")
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "dev@trip-animator.local"
    assert dev_client.get("/api/v1/auth/me").status_code == 200
    with dev_harness.session_factory() as db:
        dev_user = db.scalar(select(User).where(User.email == "dev@trip-animator.local"))
        assert dev_user is not None
        assert dev_user.email_verified_at is not None
        dev_user.email_verified_at = None
        db.commit()
    assert TestClient(dev_harness.app).post("/api/v1/auth/dev-login").status_code == 200
    with dev_harness.session_factory() as db:
        dev_user = db.scalar(select(User).where(User.email == "dev@trip-animator.local"))
        assert dev_user is not None
        assert dev_user.email_verified_at is not None


def test_login_rate_limiting_enforced_in_db(make_harness: HarnessFactory) -> None:
    h = make_harness(rate_limit_login_max=3, rate_limit_login_window_s=60)
    h.create_user("bob@example.com", "bob-secret-password-123")
    client = TestClient(h.app)

    for _ in range(3):
        r = client.post(
            "/api/v1/auth/login",
            json={"email": "bob@example.com", "password": "wrong-password"},
        )
        assert r.status_code == 401

    limited = client.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": "wrong-password"},
    )
    assert limited.status_code == 429
    assert "retry-after" in limited.headers
    assert limited.json()["status"] == 429


def test_dev_login_handles_concurrent_first_login_integrity_race(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trip_api.routers.auth as auth_router

    # 1. Deterministic IntegrityError race simulation on fresh DB
    h1 = make_harness(app_env="development", dev_auth_enabled=True)

    def racing_hash_password(password: str) -> str:
        # Insert the dev user from a concurrent session before the handler commits.
        with h1.session_factory() as other_db:
            if other_db.scalar(select(User).where(User.email == "dev@trip-animator.local")) is None:
                other_db.add(
                    User(
                        email="dev@trip-animator.local",
                        display_name="Developer",
                        password_hash=hash_password("concurrent-winner-pw"),
                        is_active=True,
                    )
                )
                other_db.commit()
        return hash_password(password)

    monkeypatch.setattr(auth_router, "hash_password", racing_hash_password)
    c1 = TestClient(h1.app)
    resp = c1.post("/api/v1/auth/dev-login")
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == "dev@trip-animator.local"
    assert c1.get("/api/v1/auth/me").status_code == 200

    with h1.session_factory() as db:
        count = db.scalar(
            select(func.count()).select_from(User).where(User.email == "dev@trip-animator.local")
        )
        assert count == 1

    # 2. Real multi-threaded concurrent first dev-login requests on a fresh DB
    monkeypatch.undo()
    h2 = make_harness(app_env="development", dev_auth_enabled=True)
    barrier = Barrier(2)

    def _call_dev_login(_idx: int) -> int:
        client = TestClient(h2.app)
        barrier.wait(timeout=5.0)
        return int(client.post("/api/v1/auth/dev-login").status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(_call_dev_login, [0, 1]))
    assert statuses == [200, 200]
    with h2.session_factory() as db:
        count2 = db.scalar(
            select(func.count()).select_from(User).where(User.email == "dev@trip-animator.local")
        )
        assert count2 == 1


def test_full_idor_cross_user_and_unauthenticated_matrix(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    h = make_harness(rate_limit_gpx_max=20, rate_limit_render_max=20)
    alice_user = h.create_user("alice-idor@example.com")
    h.create_user("bob-idor@example.com")
    alice, alice_csrf = h.login_client("alice-idor@example.com")
    bob, bob_csrf = h.login_client("bob-idor@example.com")
    anon = TestClient(h.app)

    proj_a1 = alice.post(
        "/api/v1/projects",
        json={"title": "Alice Private Trip"},
        headers={"X-CSRF-Token": alice_csrf},
    ).json()["id"]
    proj_a2 = alice.post(
        "/api/v1/projects",
        json={"title": "Alice Empty Trip"},
        headers={"X-CSRF-Token": alice_csrf},
    ).json()["id"]

    stage_a1 = alice.post(
        f"/api/v1/projects/{proj_a1}/gpx",
        files={"file": ("stage.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": alice_csrf},
    ).json()["id"]

    alice.post(
        f"/api/v1/projects/{proj_a1}/share",
        headers={"X-CSRF-Token": alice_csrf},
    )

    queued_job_id = alice.post(
        f"/api/v1/projects/{proj_a1}/renders",
        json={"resolution": "1280x720", "fps": 30},
        headers={"X-CSRF-Token": alice_csrf, "Idempotency-Key": "alice-queued-1"},
    ).json()["id"]

    done_job_id = uuid.uuid4()
    h.store.put_bytes(f"outputs/{done_job_id}.mp4", b"ALICE-MP4", content_type="video/mp4")
    h.store.put_bytes(f"outputs/{done_job_id}.webp", b"ALICE-WEBP", content_type="image/webp")
    with h.session_factory() as db:
        db.add(
            RenderJob(
                id=done_job_id,
                project_id=uuid.UUID(proj_a1),
                owner_user_id=alice_user.id,
                idempotency_key="alice-done-1",
                idempotency_hash="a" * 64,
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
                output_object_key=f"outputs/{done_job_id}.mp4",
                thumbnail_key=f"outputs/{done_job_id}.webp",
            )
        )
        db.add(
            OAuthConnection(
                user_id=alice_user.id,
                provider="strava",
                external_athlete_id="123000",
                access_token_enc="v1:a",
                refresh_token_enc="v1:b",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scope="read",
            )
        )
        db.commit()

    alice_state = str(alice.get("/api/v1/strava/connect").json()["state"])
    alice_sessions = alice.get("/api/v1/auth/sessions").json()
    assert len(alice_sessions) == 1
    alice_session_id = str(alice_sessions[0]["id"])

    # 1. Unauthenticated requests must receive 401 across all protected routes
    unauth_requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/api/v1/auth/me", None),
        ("GET", "/api/v1/auth/sessions", None),
        ("DELETE", f"/api/v1/auth/sessions/{alice_session_id}", None),
        ("POST", "/api/v1/auth/logout", None),
        ("POST", "/api/v1/auth/logout-all", None),
        (
            "POST",
            "/api/v1/auth/change-password",
            {"json": {"current_password": "x", "new_password": "new-password-1234"}},
        ),
        ("GET", "/api/v1/projects", None),
        ("POST", "/api/v1/projects", {"json": {"title": "No Auth"}}),
        ("GET", f"/api/v1/projects/{proj_a1}", None),
        ("PATCH", f"/api/v1/projects/{proj_a1}", {"json": {"revision": 1, "title": "X"}}),
        ("DELETE", f"/api/v1/projects/{proj_a1}", None),
        ("POST", f"/api/v1/projects/{proj_a1}/share", None),
        ("DELETE", f"/api/v1/projects/{proj_a1}/share", None),
        ("GET", f"/api/v1/projects/{proj_a1}/audit", None),
        ("GET", f"/api/v1/projects/{proj_a1}/export?format=gpx", None),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/gpx",
            {"files": {"file": ("s.gpx", sample_gpx, "application/gpx+xml")}},
        ),
        ("PATCH", f"/api/v1/projects/{proj_a1}/stages/{stage_a1}", {"json": {"name": "X"}}),
        ("DELETE", f"/api/v1/projects/{proj_a1}/stages/{stage_a1}", None),
        ("POST", f"/api/v1/projects/{proj_a1}/stages/reorder", {"json": {"stage_ids": [stage_a1]}}),
        ("GET", f"/api/v1/projects/{proj_a1}/telemetry/chart", None),
        ("GET", f"/api/v1/projects/{proj_a1}/telemetry/geometry", None),
        ("GET", f"/api/v1/projects/{proj_a1}/stages/{stage_a1}/geometry", None),
        ("GET", f"/api/v1/projects/{proj_a1}/kpis", None),
        ("GET", f"/api/v1/projects/{proj_a1}/analytics/highlights", None),
        ("GET", "/api/v1/analytics/career", None),
        ("GET", "/api/v1/analytics/heatmap", None),
        ("GET", "/api/v1/strava/connect", None),
        ("GET", f"/api/v1/strava/callback?code=c&state={alice_state}", None),
        ("POST", "/api/v1/strava/disconnect", None),
        ("GET", "/api/v1/strava/activities", None),
        ("POST", f"/api/v1/projects/{proj_a1}/import-strava", {"json": {"activity_id": 1}}),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/import-strava/batch",
            {"json": {"items": [{"activity_id": 1}]}},
        ),
        ("POST", f"/api/v1/projects/{proj_a1}/renders", {"json": {"resolution": "1280x720"}}),
        ("GET", f"/api/v1/projects/{proj_a1}/renders", None),
        ("GET", f"/api/v1/renders/{queued_job_id}", None),
        ("GET", f"/api/v1/renders/{queued_job_id}/events", None),
        ("POST", f"/api/v1/renders/{queued_job_id}/cancel", None),
        ("GET", f"/api/v1/renders/{done_job_id}/download", None),
        ("GET", f"/api/v1/renders/{done_job_id}/thumbnail", None),
    ]
    for method, path, kwargs in unauth_requests:
        resp = anon.request(method, path, **(kwargs or {}))
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}: {resp.text}"

    # 2. Bob (authenticated with his own CSRF) cannot list, view, or mutate Alice's resources
    assert bob.get("/api/v1/projects").json() == []
    bob_headers = {"X-CSRF-Token": bob_csrf}
    bob_idor_matrix: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", f"/api/v1/projects/{proj_a1}", None),
        (
            "PATCH",
            f"/api/v1/projects/{proj_a1}",
            {"json": {"revision": 1, "title": "Hijacked"}, "headers": bob_headers},
        ),
        ("DELETE", f"/api/v1/projects/{proj_a1}", {"headers": bob_headers}),
        ("POST", f"/api/v1/projects/{proj_a1}/share", {"headers": bob_headers}),
        ("DELETE", f"/api/v1/projects/{proj_a1}/share", {"headers": bob_headers}),
        ("GET", f"/api/v1/projects/{proj_a1}/audit", None),
        ("GET", f"/api/v1/projects/{proj_a1}/export?format=gpx", None),
        ("GET", f"/api/v1/projects/{proj_a1}/export?format=geojson", None),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/gpx",
            {
                "files": {"file": ("s.gpx", sample_gpx, "application/gpx+xml")},
                "headers": bob_headers,
            },
        ),
        (
            "PATCH",
            f"/api/v1/projects/{proj_a1}/stages/{stage_a1}",
            {"json": {"name": "Hijacked"}, "headers": bob_headers},
        ),
        ("DELETE", f"/api/v1/projects/{proj_a1}/stages/{stage_a1}", {"headers": bob_headers}),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/stages/reorder",
            {"json": {"stage_ids": [stage_a1]}, "headers": bob_headers},
        ),
        ("GET", f"/api/v1/projects/{proj_a1}/telemetry/chart", None),
        ("GET", f"/api/v1/projects/{proj_a1}/telemetry/geometry", None),
        ("GET", f"/api/v1/projects/{proj_a1}/stages/{stage_a1}/geometry", None),
        ("GET", f"/api/v1/projects/{proj_a1}/kpis", None),
        ("GET", f"/api/v1/projects/{proj_a1}/analytics/highlights", None),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/import-strava",
            {"json": {"activity_id": 99}, "headers": bob_headers},
        ),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/import-strava/batch",
            {"json": {"items": [{"activity_id": 99}]}, "headers": bob_headers},
        ),
        (
            "POST",
            f"/api/v1/projects/{proj_a1}/renders",
            {
                "json": {"resolution": "1280x720"},
                "headers": {**bob_headers, "Idempotency-Key": "bob-hijack-key-1"},
            },
        ),
        ("GET", f"/api/v1/projects/{proj_a1}/renders", None),
        ("GET", f"/api/v1/renders/{queued_job_id}", None),
        ("GET", f"/api/v1/renders/{queued_job_id}/events", None),
        ("POST", f"/api/v1/renders/{queued_job_id}/cancel", {"headers": bob_headers}),
        ("GET", f"/api/v1/renders/{done_job_id}/download", None),
        ("GET", f"/api/v1/renders/{done_job_id}/thumbnail", None),
    ]
    for method, path, kwargs in bob_idor_matrix:
        resp = bob.request(method, path, **(kwargs or {}))
        assert resp.status_code == 404, f"Bob {method} {path} returned {resp.status_code}"

    # Account-wide analytics are authenticated and scoped to Bob's empty account.
    career = bob.get("/api/v1/analytics/career")
    assert career.status_code == 200, career.text
    assert career.json()["total_projects"] == 0
    heatmap = bob.get("/api/v1/analytics/heatmap")
    assert heatmap.status_code == 200, heatmap.text
    assert heatmap.json()["trips"] == []

    # Bob cannot hijack Alice's OAuth state or revoke Alice's session
    assert bob.get(f"/api/v1/strava/callback?code=c&state={alice_state}").status_code == 400
    assert (
        bob.delete(
            f"/api/v1/auth/sessions/{alice_session_id}",
            headers=bob_headers,
        ).status_code
        == 404
    )

    # 3. Cross-project stage IDOR (even within Alice's own account)
    assert (
        alice.patch(
            f"/api/v1/projects/{proj_a2}/stages/{stage_a1}",
            json={"name": "Cross Project"},
            headers={"X-CSRF-Token": alice_csrf},
        ).status_code
        == 404
    )
    assert (
        alice.delete(
            f"/api/v1/projects/{proj_a2}/stages/{stage_a1}",
            headers={"X-CSRF-Token": alice_csrf},
        ).status_code
        == 404
    )
    assert alice.get(f"/api/v1/projects/{proj_a2}/stages/{stage_a1}/geometry").status_code == 404
    assert (
        alice.get(f"/api/v1/projects/{proj_a2}/telemetry/chart?stage_id={stage_a1}").status_code
        == 404
    )
    assert (
        alice.get(f"/api/v1/projects/{proj_a2}/telemetry/geometry?stage_id={stage_a1}").status_code
        == 404
    )

    # Alice's project and stage remain intact
    alice_detail = alice.get(f"/api/v1/projects/{proj_a1}").json()
    assert alice_detail["title"] == "Alice Private Trip"
    assert len(alice_detail["stages"]) == 1
    assert alice_detail["share_enabled"] is True


def test_csrf_and_cors_origin_enforcement(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
) -> None:
    h = make_harness(ALLOWED_ORIGINS="http://localhost:5173,https://app.trip-animator.example")
    h.create_user("csrf-alice@example.com")
    h.create_user("csrf-bob@example.com")
    alice, alice_csrf = h.login_client("csrf-alice@example.com")
    _, bob_csrf = h.login_client("csrf-bob@example.com")

    proj_id = alice.post(
        "/api/v1/projects",
        json={"title": "CSRF Project"},
        headers={"X-CSRF-Token": alice_csrf},
    ).json()["id"]
    stage_id = alice.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": alice_csrf},
    ).json()["id"]
    job_id = alice.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={"resolution": "1280x720"},
        headers={"X-CSRF-Token": alice_csrf, "Idempotency-Key": "csrf-render-key-1"},
    ).json()["id"]

    mutating_routes: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", "/api/v1/projects", {"json": {"title": "No CSRF"}}),
        ("PATCH", f"/api/v1/projects/{proj_id}", {"json": {"revision": 1, "title": "No CSRF"}}),
        ("DELETE", f"/api/v1/projects/{proj_id}", None),
        ("POST", f"/api/v1/projects/{proj_id}/share", None),
        ("DELETE", f"/api/v1/projects/{proj_id}/share", None),
        (
            "POST",
            f"/api/v1/projects/{proj_id}/gpx",
            {"files": {"file": ("s.gpx", sample_gpx, "application/gpx+xml")}},
        ),
        ("PATCH", f"/api/v1/projects/{proj_id}/stages/{stage_id}", {"json": {"name": "No CSRF"}}),
        ("DELETE", f"/api/v1/projects/{proj_id}/stages/{stage_id}", None),
        ("POST", f"/api/v1/projects/{proj_id}/stages/reorder", {"json": {"stage_ids": [stage_id]}}),
        ("POST", f"/api/v1/projects/{proj_id}/import-strava", {"json": {"activity_id": 1}}),
        (
            "POST",
            f"/api/v1/projects/{proj_id}/import-strava/batch",
            {"json": {"items": [{"activity_id": 1}]}},
        ),
        ("POST", f"/api/v1/projects/{proj_id}/renders", {"json": {"resolution": "1280x720"}}),
        ("POST", f"/api/v1/renders/{job_id}/cancel", None),
        ("POST", "/api/v1/strava/disconnect", None),
        ("POST", "/api/v1/auth/logout", None),
        ("POST", "/api/v1/auth/logout-all", None),
        ("DELETE", f"/api/v1/auth/sessions/{uuid.uuid4()}", None),
        (
            "POST",
            "/api/v1/auth/change-password",
            {"json": {"current_password": "x", "new_password": "new-password-1234"}},
        ),
    ]
    for method, path, kwargs in mutating_routes:
        missing = alice.request(method, path, **(kwargs or {}))
        assert missing.status_code == 403, f"Missing CSRF {method} {path}: {missing.status_code}"
        cross_user = alice.request(
            method,
            path,
            headers={"X-CSRF-Token": bob_csrf},
            **(kwargs or {}),
        )
        assert cross_user.status_code == 403, f"Bob CSRF {method} {path}: {cross_user.status_code}"

    # CORS preflight and simple origin checks
    anon = TestClient(h.app)
    allowed_preflight = anon.options(
        "/api/v1/projects",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-CSRF-Token, Idempotency-Key",
        },
    )
    assert allowed_preflight.status_code == 200
    assert allowed_preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert allowed_preflight.headers["access-control-allow-credentials"] == "true"

    evil_preflight = anon.options(
        "/api/v1/projects",
        headers={
            "Origin": "https://evil.attacker.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, X-CSRF-Token",
        },
    )
    assert "access-control-allow-origin" not in evil_preflight.headers

    evil_get = anon.get(
        "/api/v1/public-config",
        headers={"Origin": "https://evil.attacker.example"},
    )
    assert evil_get.status_code == 200
    assert "access-control-allow-origin" not in evil_get.headers


async def test_x_forwarded_for_spoofing_vs_trusted_forwarded_allow_ips(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness(
        rate_limit_login_max=3,
        rate_limit_login_window_s=60,
        rate_limit_share_max=2,
        rate_limit_share_window_s=60,
        rate_limit_strava_webhook_max=2,
        rate_limit_strava_webhook_window_s=60,
    )
    h.create_user("xff@example.com", "xff-secret-password-123")
    # Uses 1 of 3 login tokens for client IP "testclient"
    owner, csrf = h.login_client("xff@example.com", "xff-secret-password-123")
    proj_id = owner.post(
        "/api/v1/projects",
        json={"title": "XFF Share"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    slug = owner.post(
        f"/api/v1/projects/{proj_id}/share",
        headers={"X-CSRF-Token": csrf},
    ).json()["share_slug"]

    # Direct bare request with missing client resolves to "unknown"
    bare_req = Request({"type": "http", "headers": [], "client": None})
    assert get_client_ip(bare_req) == "unknown"

    # 1. Direct app ignores spoofed X-Forwarded-For on login and share
    direct_client = TestClient(h.app)
    for idx in range(2):
        r = direct_client.post(
            "/api/v1/auth/login",
            json={"email": "xff@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": f"10.0.0.{idx + 1}"},
        )
        assert r.status_code == 401
    assert (
        direct_client.post(
            "/api/v1/auth/login",
            json={"email": "xff@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": "10.0.0.99"},
        ).status_code
        == 429
    )

    for idx in range(2):
        assert (
            direct_client.get(
                f"/api/v1/share/{slug}",
                headers={"X-Forwarded-For": f"172.16.0.{idx + 1}"},
            ).status_code
            == 200
        )
    assert (
        direct_client.get(
            f"/api/v1/share/{slug}",
            headers={"X-Forwarded-For": "172.16.0.99"},
        ).status_code
        == 429
    )

    # 2. ProxyHeadersMiddleware with FORWARDED_ALLOW_IPS="127.0.0.1,::1":
    #    - Untrusted peer cannot spoof X-Forwarded-For
    #    - Trusted proxy (127.0.0.1) uses rightmost untrusted hop, blocking leftmost spoofing
    proxied_app: Any = ProxyHeadersMiddleware(cast(Any, h.app), trusted_hosts=["127.0.0.1", "::1"])

    untrusted_transport = httpx.ASGITransport(app=proxied_app, client=("203.0.113.77", 54321))
    async with httpx.AsyncClient(
        transport=untrusted_transport, base_url="http://test"
    ) as untrusted:
        for spoof in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
            res = await untrusted.post(
                "/api/v1/auth/login",
                json={"email": "xff@example.com", "password": "wrong-password"},
                headers={"X-Forwarded-For": spoof},
            )
            assert res.status_code == 401
        blocked = await untrusted.post(
            "/api/v1/auth/login",
            json={"email": "xff@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": "198.51.100.4"},
        )
        assert blocked.status_code == 429

    trusted_transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=trusted_transport, base_url="http://test") as trusted:
        # Leftmost spoofed IPs still map to the rightmost untrusted client IP (198.51.100.50)
        for spoofed_left in ("1.1.1.1", "8.8.8.8", "4.2.2.1"):
            res = await trusted.post(
                "/api/v1/auth/login",
                json={"email": "xff@example.com", "password": "wrong-password"},
                headers={"X-Forwarded-For": f"{spoofed_left}, 198.51.100.50"},
            )
            assert res.status_code == 401
        blocked_spoof = await trusted.post(
            "/api/v1/auth/login",
            json={"email": "xff@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": "9.9.9.9, 198.51.100.50"},
        )
        assert blocked_spoof.status_code == 429

        # Distinct real client IP behind trusted proxy gets an independent rate-limit bucket
        other_client_res = await trusted.post(
            "/api/v1/auth/login",
            json={"email": "xff@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": "198.51.100.51"},
        )
        assert other_client_res.status_code == 401


def test_create_db_engine_applies_pool_params_on_postgres_and_skips_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trip_api import db as db_mod
    from trip_api.config import Settings

    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_create_engine(url: str, **kwargs: Any) -> Any:
        recorded.append((url, kwargs))
        return object()

    monkeypatch.setattr(db_mod, "create_engine", fake_create_engine)

    pg_url = "postgresql+psycopg://trip_app:secret@localhost:5432/trip_animator"
    custom_cfg = Settings(
        db_pool_size=12,
        db_max_overflow=24,
        db_pool_timeout_s=45,
        db_pool_recycle_s=900,
    )
    db_mod.create_db_engine(pg_url, settings=custom_cfg)
    db_mod.create_db_engine(pg_url)
    db_mod.create_db_engine("sqlite:///tmp/test_pool.db", settings=custom_cfg)

    assert len(recorded) == 3

    _, pg_custom_kw = recorded[0]
    assert pg_custom_kw["pool_size"] == 12
    assert pg_custom_kw["max_overflow"] == 24
    assert pg_custom_kw["pool_timeout"] == 45
    assert pg_custom_kw["pool_recycle"] == 900
    assert pg_custom_kw["pool_pre_ping"] is True
    assert pg_custom_kw["connect_args"] == {}

    _, pg_default_kw = recorded[1]
    assert pg_default_kw["pool_size"] == 10
    assert pg_default_kw["max_overflow"] == 20
    assert pg_default_kw["pool_timeout"] == 30
    assert pg_default_kw["pool_recycle"] == 1800
    assert pg_default_kw["pool_pre_ping"] is True

    _, sqlite_kw = recorded[2]
    assert sqlite_kw["pool_pre_ping"] is False
    assert sqlite_kw["connect_args"] == {"check_same_thread": False}
    for pool_key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
        assert pool_key not in sqlite_kw


async def test_login_dual_ip_and_normalized_account_rate_limit(
    make_harness: HarnessFactory,
) -> None:
    import hashlib

    from trip_api.db import RateLimitRecord

    h = make_harness(
        rate_limit_login_max=20,
        rate_limit_login_account_max=3,
        rate_limit_login_window_s=60,
    )
    h.create_user("target.user@example.com", "target-secret-password-123")
    h.create_user("other.user@example.com", "other-secret-password-123")

    proxied_app: Any = ProxyHeadersMiddleware(cast(Any, h.app), trusted_hosts=["127.0.0.1"])
    transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for idx, email_variant in enumerate(
            (
                "  TARGET.USER@Example.COM ",
                "target.user@example.com",
                "Target.User@example.com",
            )
        ):
            res = await client.post(
                "/api/v1/auth/login",
                json={"email": email_variant, "password": "wrong-password"},
                headers={"X-Forwarded-For": f"198.51.100.{idx + 1}"},
            )
            assert res.status_code == 401

        # Rotating to a fresh IP still hits the normalized account rate limit -> 429
        acct_blocked = await client.post(
            "/api/v1/auth/login",
            json={"email": "target.user@example.com", "password": "target-secret-password-123"},
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert acct_blocked.status_code == 429
        assert "retry-after" in acct_blocked.headers

        # Another account from the same IP is unaffected
        other_ok = await client.post(
            "/api/v1/auth/login",
            json={"email": "other.user@example.com", "password": "other-secret-password-123"},
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert other_ok.status_code == 200

    expected_hash = hashlib.sha256(b"target.user@example.com").hexdigest()[:32]
    with h.session_factory() as db:
        keys = [r.bucket_key for r in db.scalars(select(RateLimitRecord)).all()]
    assert f"login:acct:{expected_hash}" in keys
    assert any(k.startswith("login:ip:") for k in keys)
    assert all("target.user@example.com" not in k for k in keys)


def test_public_config_registration_enabled_flag(make_harness: HarnessFactory) -> None:
    default_h = make_harness()
    default_client = TestClient(default_h.app)
    default_cfg = default_client.get("/api/v1/public-config")
    assert default_cfg.status_code == 200
    assert default_cfg.json()["registration_enabled"] is False

    blank_h = make_harness(registration_invite_code=" ,  , ")
    blank_cfg = TestClient(blank_h.app).get("/api/v1/public-config")
    assert blank_cfg.status_code == 200
    assert blank_cfg.json()["registration_enabled"] is False

    enabled_h = make_harness(registration_invite_code="alpine-2026, summit-99")
    enabled_cfg = TestClient(enabled_h.app).get("/api/v1/public-config")
    assert enabled_cfg.status_code == 200
    assert enabled_cfg.json()["registration_enabled"] is True


def test_register_invite_code_lifecycle_validation_and_conflicts(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trip_api.routers.auth as auth_router

    sent_emails: list[tuple[str, str, str]] = []

    def _capture_email(
        _settings: Any, recipient_email: str, display_name: str, verify_url: str
    ) -> None:
        sent_emails.append((recipient_email, display_name, verify_url))

    monkeypatch.setattr(auth_router, "send_verification_email", _capture_email)

    # 1. Registration disabled by default -> 403
    disabled_h = make_harness()
    disabled_client = TestClient(disabled_h.app)
    disabled_resp = disabled_client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "display_name": "New Rider",
            "password": "valid-password-1234",
            "invite_code": "any-code",
        },
    )
    assert disabled_resp.status_code == 403
    assert disabled_resp.json()["detail"] == "Account registration is disabled"

    # 2. Single invite code: wrong code -> 403, short password -> 422, bad email -> 422
    single_h = make_harness(
        registration_invite_code="alpine-2026",
        public_base_url="https://trips.example.com/",
    )
    single_client = TestClient(single_h.app)

    wrong_code = single_client.post(
        "/api/v1/auth/register",
        json={
            "email": "rider1@example.com",
            "display_name": "Rider One",
            "password": "valid-password-1234",
            "invite_code": "wrong-code",
        },
    )
    assert wrong_code.status_code == 403
    assert wrong_code.json()["detail"] == "Invalid registration invite code"

    short_pw = single_client.post(
        "/api/v1/auth/register",
        json={
            "email": "rider1@example.com",
            "display_name": "Rider One",
            "password": "short-pw",
            "invite_code": "alpine-2026",
        },
    )
    assert short_pw.status_code == 422

    for bad_email in ("not-an-email", "@example.com", "rider@"):
        invalid_email_resp = single_client.post(
            "/api/v1/auth/register",
            json={
                "email": bad_email,
                "display_name": "Rider One",
                "password": "valid-password-1234",
                "invite_code": "alpine-2026",
            },
        )
        assert invalid_email_resp.status_code == 422

    # Valid registration -> 201 RegisterResponse, NO session cookie, verification token + email sent
    ok_resp = single_client.post(
        "/api/v1/auth/register",
        json={
            "email": "  Rider.One@Example.COM  ",
            "display_name": "  Alpine Explorer  ",
            "password": "alpine-secret-password-123",
            "invite_code": "alpine-2026",
        },
    )
    assert ok_resp.status_code == 201, ok_resp.text
    body = ok_resp.json()
    assert body["status"] == "verification_required"
    assert body["email"] == "rider.one@example.com"
    assert "verify" in body["message"].lower()
    assert "trip_session" not in single_client.cookies
    assert single_client.get("/api/v1/auth/me").status_code == 401

    assert len(sent_emails) == 1
    rec_email, rec_name, verify_url = sent_emails[0]
    assert rec_email == "rider.one@example.com"
    assert rec_name == "Alpine Explorer"
    assert verify_url.startswith("https://trips.example.com/verify-email?token=")
    raw_token = verify_url.split("token=", 1)[1]

    with single_h.session_factory() as db:
        user_row = db.scalar(select(User).where(User.email == "rider.one@example.com"))
        assert user_row is not None
        assert user_row.email_verified_at is None
        token_row = db.scalar(
            select(EmailVerificationToken).where(EmailVerificationToken.user_id == user_row.id)
        )
        assert token_row is not None
        assert token_row.used_at is None
        assert token_row.token_hash != raw_token
        audit_events = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "auth.register")
        ).all()
        assert len(audit_events) == 1
        assert audit_events[0].user_id == user_row.id
        assert audit_events[0].metadata_json == {"verification_required": True}

    # Login before verification is blocked with 403
    unverified_login = single_client.post(
        "/api/v1/auth/login",
        json={"email": "rider.one@example.com", "password": "alpine-secret-password-123"},
    )
    assert unverified_login.status_code == 403
    assert "not verified" in unverified_login.json()["detail"].lower()
    with single_h.session_factory() as db:
        unverified_audits = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "auth.login_unverified")
        ).all()
        assert len(unverified_audits) == 1

    # Verify email with valid token -> 200 + session cookie + CSRF + /me + login works
    verify_resp = single_client.post("/api/v1/auth/verify-email", json={"token": raw_token})
    assert verify_resp.status_code == 200, verify_resp.text
    verify_body = verify_resp.json()
    assert verify_body["user"]["email"] == "rider.one@example.com"
    assert verify_body["user"]["display_name"] == "Alpine Explorer"
    assert "trip_session" in single_client.cookies
    csrf_header = verify_resp.headers.get("x-csrf-token")
    assert csrf_header is not None
    assert csrf_header == verify_body["csrf_token"]

    me_resp = single_client.get("/api/v1/auth/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["user"]["email"] == "rider.one@example.com"

    with single_h.session_factory() as db:
        verified_user = db.scalar(select(User).where(User.email == "rider.one@example.com"))
        assert verified_user is not None
        assert verified_user.email_verified_at is not None
        used_tok = db.scalar(
            select(EmailVerificationToken).where(EmailVerificationToken.user_id == verified_user.id)
        )
        assert used_tok is not None
        assert used_tok.used_at is not None

    # Reusing the already-consumed token fails with 400
    reuse_resp = TestClient(single_h.app).post(
        "/api/v1/auth/verify-email", json={"token": raw_token}
    )
    assert reuse_resp.status_code == 400

    # Password login now succeeds
    post_verify_login = TestClient(single_h.app).post(
        "/api/v1/auth/login",
        json={"email": "rider.one@example.com", "password": "alpine-secret-password-123"},
    )
    assert post_verify_login.status_code == 200

    # Duplicate email (normalized) -> 409
    dup_resp = TestClient(single_h.app).post(
        "/api/v1/auth/register",
        json={
            "email": "rider.one@example.com",
            "display_name": "Duplicate",
            "password": "another-valid-password-123",
            "invite_code": "alpine-2026",
        },
    )
    assert dup_resp.status_code == 409
    assert dup_resp.json()["detail"] == "An account with this email already exists"

    # 3. Comma-separated invite codes accept any listed code
    multi_h = make_harness(registration_invite_code="first-code, second-code , third-code")
    for idx, code in enumerate(("first-code", "second-code", "third-code"), start=1):
        c = TestClient(multi_h.app)
        resp = c.post(
            "/api/v1/auth/register",
            json={
                "email": f"multi{idx}@example.com",
                "display_name": f"Multi {idx}",
                "password": "multi-code-password-123",
                "invite_code": code,
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "verification_required"
        tok = sent_emails[-1][2].split("token=", 1)[1]
        assert c.post("/api/v1/auth/verify-email", json={"token": tok}).status_code == 200
        assert c.get("/api/v1/auth/me").status_code == 200


async def test_register_ip_and_account_rate_limiting(
    make_harness: HarnessFactory,
) -> None:
    import hashlib

    # 1. Per-IP rate limit
    ip_h = make_harness(
        registration_invite_code="alpine-2026",
        rate_limit_register_max=2,
        rate_limit_register_account_max=10,
        rate_limit_register_window_s=60,
    )
    ip_client = TestClient(ip_h.app)
    for idx in range(2):
        res = ip_client.post(
            "/api/v1/auth/register",
            json={
                "email": f"ip-limit-{idx}@example.com",
                "display_name": "IP Test",
                "password": "valid-password-1234",
                "invite_code": "wrong-code",
            },
        )
        assert res.status_code == 403

    ip_blocked = ip_client.post(
        "/api/v1/auth/register",
        json={
            "email": "ip-limit-2@example.com",
            "display_name": "IP Test",
            "password": "valid-password-1234",
            "invite_code": "alpine-2026",
        },
    )
    assert ip_blocked.status_code == 429
    assert "retry-after" in ip_blocked.headers

    # 2. Per-account normalized rate limit across distinct IPs
    acct_h = make_harness(
        registration_invite_code="alpine-2026",
        rate_limit_register_max=20,
        rate_limit_register_account_max=2,
        rate_limit_register_window_s=60,
    )
    proxied_app: Any = ProxyHeadersMiddleware(cast(Any, acct_h.app), trusted_hosts=["127.0.0.1"])
    transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for idx, email_variant in enumerate(("  TARGET@Example.COM ", "target@example.com")):
            r = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email_variant,
                    "display_name": "Target",
                    "password": "valid-password-1234",
                    "invite_code": "wrong-code",
                },
                headers={"X-Forwarded-For": f"198.51.100.{idx + 1}"},
            )
            assert r.status_code == 403

        acct_blocked = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "target@example.com",
                "display_name": "Target",
                "password": "valid-password-1234",
                "invite_code": "alpine-2026",
            },
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert acct_blocked.status_code == 429
        assert "retry-after" in acct_blocked.headers

    expected_hash = hashlib.sha256(b"target@example.com").hexdigest()[:32]
    with acct_h.session_factory() as db:
        keys = [r.bucket_key for r in db.scalars(select(RateLimitRecord)).all()]
    assert f"register:acct:{expected_hash}" in keys
    assert any(k.startswith("register:ip:") for k in keys)


def test_compose_and_env_example_expose_invite_and_caddy_variables() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    compose_text = (repo_root / "infra" / "compose.yaml").read_text(encoding="utf-8")
    env_example_text = (repo_root / ".env.example").read_text(encoding="utf-8")

    expected_vars = (
        "REGISTRATION_INVITE_CODE",
        "CADDY_HTTP_PUBLISH",
        "CADDY_HTTPS_PUBLISH",
        "DOMAIN",
        "COMPOSE_ALLOWED_ORIGINS",
        "PUBLIC_BASE_URL",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "SMTP_USE_TLS",
        "EMAIL_VERIFICATION_TTL_HOURS",
    )
    for var_name in expected_vars:
        assert var_name in compose_text, f"{var_name} missing from infra/compose.yaml"
        assert var_name in env_example_text, f"{var_name} missing from .env.example"


def test_email_verification_token_expiry_invalid_and_inactive_user(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trip_api.routers.auth as auth_router

    sent_urls: list[str] = []
    monkeypatch.setattr(
        auth_router,
        "send_verification_email",
        lambda _s, _to, _name, url: sent_urls.append(url),
    )

    h = make_harness(registration_invite_code="alpine-2026", rate_limit_verify_email_max=3)
    client = TestClient(h.app)

    # Unknown or short malformed token -> uniform 400 (not 422)
    assert client.post("/api/v1/auth/verify-email", json={"token": "bad"}).status_code == 400
    unknown_resp = client.post(
        "/api/v1/auth/verify-email",
        json={"token": "missing-token"},
    )
    assert unknown_resp.status_code == 400

    # Register an account, expire its token, verify -> 400
    reg_resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "expiring@example.com",
            "display_name": "Expiring User",
            "password": "valid-password-1234",
            "invite_code": "alpine-2026",
        },
    )
    assert reg_resp.status_code == 201
    tok_expired = sent_urls[-1].split("token=", 1)[1]
    with h.session_factory() as db:
        tok_row = db.scalar(select(EmailVerificationToken))
        assert tok_row is not None
        tok_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

    # Second attempt hits verify-email endpoint (allowed, limit=2), fails with 400 due to expiry
    assert client.post("/api/v1/auth/verify-email", json={"token": tok_expired}).status_code == 400

    # Third attempt hits per-IP rate limit -> 429
    limited = client.post("/api/v1/auth/verify-email", json={"token": tok_expired})
    assert limited.status_code == 429
    assert "retry-after" in limited.headers

    # Inactive user cannot verify even with a fresh unexpired token
    h2 = make_harness(registration_invite_code="alpine-2026")
    c2 = TestClient(h2.app)
    assert (
        c2.post(
            "/api/v1/auth/register",
            json={
                "email": "disabled@example.com",
                "display_name": "Disabled User",
                "password": "valid-password-1234",
                "invite_code": "alpine-2026",
            },
        ).status_code
        == 201
    )
    tok_inactive = sent_urls[-1].split("token=", 1)[1]
    with h2.session_factory() as db:
        u = db.scalar(select(User).where(User.email == "disabled@example.com"))
        assert u is not None
        u.is_active = False
        db.commit()
    assert c2.post("/api/v1/auth/verify-email", json={"token": tok_inactive}).status_code == 400


async def test_resend_verification_anti_enumeration_replacement_and_rate_limits(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trip_api.routers.auth as auth_router

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_router,
        "send_verification_email",
        lambda _s, to, _name, url: sent.append((to, url)),
    )

    h = make_harness(
        registration_invite_code="alpine-2026",
        rate_limit_resend_verification_max=10,
        rate_limit_resend_verification_account_max=2,
        rate_limit_resend_verification_window_s=60,
    )
    h.create_user("verified@example.com", verified=True)
    inactive = h.create_user("inactive@example.com", verified=False)
    with h.session_factory() as db:
        u = db.get(User, inactive.id)
        assert u is not None
        u.is_active = False
        db.commit()

    client = TestClient(h.app)
    assert (
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "pending@example.com",
                "display_name": "Pending User",
                "password": "valid-password-1234",
                "invite_code": "alpine-2026",
            },
        ).status_code
        == 201
    )
    first_token = sent[-1][1].split("token=", 1)[1]

    # Unknown, already-verified, and inactive emails all return 202 without sending email
    sent_before = len(sent)
    for addr in ("nobody@example.com", "verified@example.com", "inactive@example.com"):
        r = client.post("/api/v1/auth/resend-verification", json={"email": addr})
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"
    assert len(sent) == sent_before

    # Resend for unverified user replaces old unused token and delivers a working new token
    resend_resp = client.post(
        "/api/v1/auth/resend-verification", json={"email": "  PENDING@Example.COM "}
    )
    assert resend_resp.status_code == 202
    assert len(sent) == sent_before + 1
    second_token = sent[-1][1].split("token=", 1)[1]
    assert second_token != first_token

    with h.session_factory() as db:
        tokens = db.scalars(select(EmailVerificationToken)).all()
        assert len(tokens) == 1

    assert client.post("/api/v1/auth/verify-email", json={"token": first_token}).status_code == 400
    assert client.post("/api/v1/auth/verify-email", json={"token": second_token}).status_code == 200

    # Account rate limit across distinct client IPs -> 429
    proxied_app: Any = ProxyHeadersMiddleware(cast(Any, h.app), trusted_hosts=["127.0.0.1"])
    transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        r1 = await async_client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "ratelimit@example.com"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        r2 = await async_client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "  RATELIMIT@Example.COM "},
            headers={"X-Forwarded-For": "198.51.100.2"},
        )
        r3 = await async_client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "ratelimit@example.com"},
            headers={"X-Forwarded-For": "198.51.100.3"},
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r3.status_code == 429
        assert "retry-after" in r3.headers


def test_email_module_build_url_and_smtp_transports(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    import smtplib

    from trip_api.config import Settings
    from trip_api.email import build_verification_url, send_verification_email

    url = build_verification_url("https://app.example.com/", "tok+abc/def=1")
    assert url == "https://app.example.com/verify-email?token=tok%2Babc%2Fdef%3D1"

    # 1. Dev log fallback when smtp_host is empty
    dev_cfg = Settings(app_env="test", smtp_host="")
    with caplog.at_level(logging.INFO, logger="trip_api.email"):
        send_verification_email(dev_cfg, "rider@example.com", "Rider", url)
    assert any(
        "SMTP not configured; email verification link for rider@example.com" in rec.message
        and url in rec.message
        for rec in caplog.records
    )

    # 2. Mocked smtplib.SMTP (STARTTLS + login) and smtplib.SMTP_SSL (port 465)
    calls: list[dict[str, Any]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 15) -> None:
            self.record: dict[str, Any] = {
                "kind": "SMTP",
                "host": host,
                "port": port,
                "timeout": timeout,
                "starttls": False,
                "login": None,
                "message": None,
            }
            calls.append(self.record)

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def starttls(self) -> None:
            self.record["starttls"] = True

        def login(self, user: str, password: str) -> None:
            self.record["login"] = (user, password)

        def send_message(self, msg: Any) -> None:
            self.record["message"] = msg

    class FakeSMTPSSL(FakeSMTP):
        def __init__(self, host: str, port: int, timeout: float = 15) -> None:
            super().__init__(host, port, timeout=timeout)
            self.record["kind"] = "SMTP_SSL"

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)

    starttls_cfg = Settings(
        app_env="test",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="mailer",
        smtp_password="smtp-pass",
        smtp_from="no-reply@example.com",
        smtp_use_tls=True,
        email_verification_ttl_hours=24,
    )
    send_verification_email(starttls_cfg, "alice@example.com", "Alice <A>", url)
    assert len(calls) == 1
    assert calls[0]["kind"] == "SMTP"
    assert calls[0]["host"] == "smtp.example.com"
    assert calls[0]["port"] == 587
    assert calls[0]["starttls"] is True
    assert calls[0]["login"] == ("mailer", "smtp-pass")
    msg = calls[0]["message"]
    assert msg["Subject"] == "Verify your Trip Animator email address"
    assert msg["From"] == "no-reply@example.com"
    assert msg["To"] == "alice@example.com"
    plain_part = msg.get_body(preferencelist=("plain",)).get_content()
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    assert url in plain_part
    assert "24 hours" in plain_part
    assert "Alice &lt;A&gt;" in html_part

    ssl_cfg = Settings(
        app_env="test",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_user="ssl-user",
        smtp_password="ssl-pass",
        smtp_use_tls=True,
    )
    send_verification_email(ssl_cfg, "bob@example.com", "Bob", url)
    assert len(calls) == 2
    assert calls[1]["kind"] == "SMTP_SSL"
    assert calls[1]["port"] == 465
    assert calls[1]["login"] == ("ssl-user", "ssl-pass")

    plain_cfg = Settings(
        app_env="test",
        smtp_host="mailhog.local",
        smtp_port=1025,
        smtp_user="",
        smtp_password="",
        smtp_use_tls=False,
    )
    send_verification_email(plain_cfg, "carol@example.com", "", url)
    assert len(calls) == 3
    assert calls[2]["kind"] == "SMTP"
    assert calls[2]["starttls"] is False
    assert calls[2]["login"] is None

    # 3. Password reset URL builder, dev log fallback, and SMTP delivery
    from trip_api.email import build_password_reset_url, send_password_reset_email

    reset_url = build_password_reset_url("https://app.example.com/", "rst+abc/def=1")
    assert reset_url == "https://app.example.com/reset-password?token=rst%2Babc%2Fdef%3D1"

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="trip_api.email"):
        send_password_reset_email(dev_cfg, "rider@example.com", "Rider", reset_url)
    assert any(
        "SMTP not configured; password reset link for rider@example.com" in rec.message
        and reset_url in rec.message
        for rec in caplog.records
    )

    reset_cfg = Settings(
        app_env="test",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="mailer",
        smtp_password="smtp-pass",
        smtp_from="no-reply@example.com",
        smtp_use_tls=True,
        password_reset_ttl_minutes=45,
    )
    send_password_reset_email(reset_cfg, "alice@example.com", "Alice <A>", reset_url)
    assert len(calls) == 4
    reset_msg = calls[3]["message"]
    assert reset_msg["Subject"] == "Reset your Trip Animator password"
    reset_plain = reset_msg.get_body(preferencelist=("plain",)).get_content()
    reset_html = reset_msg.get_body(preferencelist=("html",)).get_content()
    assert reset_url in reset_plain
    assert "45 minutes" in reset_plain
    assert "Alice &lt;A&gt;" in reset_html


async def test_forgot_password_and_reset_password_lifecycle_and_rate_limits(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    import trip_api.routers.auth as auth_router

    sent_resets: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        auth_router,
        "send_password_reset_email",
        lambda _s, to, name, url: sent_resets.append((to, name, url)),
    )

    h = make_harness(
        public_base_url="https://trips.example.com/",
        rate_limit_forgot_password_max=20,
        rate_limit_forgot_password_account_max=3,
        rate_limit_forgot_password_window_s=60,
        rate_limit_reset_password_max=20,
        rate_limit_reset_password_window_s=60,
    )
    alice = h.create_user("alice.reset@example.com", "alice-old-password-123")
    unverified = h.create_user("unverified.reset@example.com", "unverified-pw-1234", verified=False)
    inactive = h.create_user("inactive.reset@example.com", "inactive-pw-1234")
    with h.session_factory() as db:
        inactive_row = db.get(User, inactive.id)
        assert inactive_row is not None
        inactive_row.is_active = False
        db.commit()

    # Alice has two active sessions before resetting her password
    sess1, _ = h.login_client("alice.reset@example.com", "alice-old-password-123")
    sess2, _ = h.login_client("alice.reset@example.com", "alice-old-password-123")
    assert sess1.get("/api/v1/auth/me").status_code == 200
    assert sess2.get("/api/v1/auth/me").status_code == 200

    anon = TestClient(h.app)

    # Anti-enumeration: unknown and inactive emails return 202 without sending emails
    for addr in ("nobody@example.com", "inactive.reset@example.com"):
        r = anon.post("/api/v1/auth/forgot-password", json={"email": addr})
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"
    assert len(sent_resets) == 0

    # Active user gets a reset email; requesting again replaces the prior unused token
    r1 = anon.post("/api/v1/auth/forgot-password", json={"email": "  ALICE.RESET@Example.COM "})
    assert r1.status_code == 202
    assert len(sent_resets) == 1
    first_token = sent_resets[0][2].split("token=", 1)[1]

    r2 = anon.post("/api/v1/auth/forgot-password", json={"email": "alice.reset@example.com"})
    assert r2.status_code == 202
    assert len(sent_resets) == 2
    second_token = sent_resets[1][2].split("token=", 1)[1]
    assert second_token != first_token

    with h.session_factory() as db:
        rows = db.scalars(
            select(PasswordResetToken).where(PasswordResetToken.user_id == alice.id)
        ).all()
        assert len(rows) == 1
        assert rows[0].token_hash != second_token

    # Short malformed token -> 400; replaced first token -> 400
    assert (
        anon.post(
            "/api/v1/auth/reset-password",
            json={"token": "bad", "new_password": "alice-new-password-123"},
        ).status_code
        == 400
    )
    assert (
        anon.post(
            "/api/v1/auth/reset-password",
            json={"token": first_token, "new_password": "alice-new-password-123"},
        ).status_code
        == 400
    )

    # Valid reset updates password, marks token used, revokes ALL sessions, logs audit event
    reset_ok = anon.post(
        "/api/v1/auth/reset-password",
        json={"token": second_token, "new_password": "alice-new-password-123"},
    )
    assert reset_ok.status_code == 200, reset_ok.text
    assert reset_ok.json()["ok"] is True

    # Both prior sessions are immediately invalidated
    assert sess1.get("/api/v1/auth/me").status_code == 401
    assert sess2.get("/api/v1/auth/me").status_code == 401

    # Token cannot be reused
    assert (
        anon.post(
            "/api/v1/auth/reset-password",
            json={"token": second_token, "new_password": "another-new-password-123"},
        ).status_code
        == 400
    )

    # Old password fails, new password logs in
    assert (
        anon.post(
            "/api/v1/auth/login",
            json={"email": "alice.reset@example.com", "password": "alice-old-password-123"},
        ).status_code
        == 401
    )
    assert (
        anon.post(
            "/api/v1/auth/login",
            json={"email": "alice.reset@example.com", "password": "alice-new-password-123"},
        ).status_code
        == 200
    )

    with h.session_factory() as db:
        audits = db.scalars(
            select(AuditEvent).where(
                AuditEvent.user_id == alice.id,
                AuditEvent.event_type == "auth.password_reset",
            )
        ).all()
        assert len(audits) == 1

    # Unverified active user who resets via email link becomes verified
    assert (
        anon.post(
            "/api/v1/auth/forgot-password",
            json={"email": "unverified.reset@example.com"},
        ).status_code
        == 202
    )
    unverified_tok = sent_resets[-1][2].split("token=", 1)[1]
    assert (
        anon.post(
            "/api/v1/auth/reset-password",
            json={"token": unverified_tok, "new_password": "unverified-new-pw-1234"},
        ).status_code
        == 200
    )
    with h.session_factory() as db:
        u_refreshed = db.get(User, unverified.id)
        assert u_refreshed is not None
        assert u_refreshed.email_verified_at is not None

    # Expired token returns 400
    assert (
        anon.post(
            "/api/v1/auth/forgot-password",
            json={"email": "unverified.reset@example.com"},
        ).status_code
        == 202
    )
    exp_tok = sent_resets[-1][2].split("token=", 1)[1]
    with h.session_factory() as db:
        exp_row = db.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == unverified.id,
                PasswordResetToken.used_at.is_(None),
            )
        )
        assert exp_row is not None
        exp_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    assert (
        anon.post(
            "/api/v1/auth/reset-password",
            json={"token": exp_tok, "new_password": "expired-new-pw-1234"},
        ).status_code
        == 400
    )

    # Inactive user cannot reset password even with an unexpired token
    assert (
        anon.post(
            "/api/v1/auth/forgot-password",
            json={"email": "unverified.reset@example.com"},
        ).status_code
        == 202
    )
    inact_tok = sent_resets[-1][2].split("token=", 1)[1]
    with h.session_factory() as db:
        u_row = db.get(User, unverified.id)
        assert u_row is not None
        u_row.is_active = False
        db.commit()
    assert (
        anon.post(
            "/api/v1/auth/reset-password",
            json={"token": inact_tok, "new_password": "inactive-new-pw-1234"},
        ).status_code
        == 400
    )

    # Use a fresh harness for reset-password rate limit check so prior attempts do not interfere
    h_rl = make_harness(
        rate_limit_forgot_password_max=10,
        rate_limit_forgot_password_account_max=2,
        rate_limit_forgot_password_window_s=60,
        rate_limit_reset_password_max=2,
        rate_limit_reset_password_window_s=60,
    )

    # Rate limit checks on forgot-password (per-account across IPs) and reset-password (per-IP)
    proxied_app: Any = ProxyHeadersMiddleware(cast(Any, h_rl.app), trusted_hosts=["127.0.0.1"])
    transport = httpx.ASGITransport(app=proxied_app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        for idx in range(2):
            res = await async_client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "  TARGET.RESET@Example.COM "},
                headers={"X-Forwarded-For": f"198.51.100.{idx + 10}"},
            )
            assert res.status_code == 202
        blocked = await async_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "target.reset@example.com"},
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert blocked.status_code == 429
        assert "retry-after" in blocked.headers

        for _ in range(2):
            r_bad = await async_client.post(
                "/api/v1/auth/reset-password",
                json={"token": "invalid", "new_password": "valid-password-1234"},
                headers={"X-Forwarded-For": "198.51.100.55"},
            )
            assert r_bad.status_code == 400
        r_blocked = await async_client.post(
            "/api/v1/auth/reset-password",
            json={"token": "invalid", "new_password": "valid-password-1234"},
            headers={"X-Forwarded-For": "198.51.100.55"},
        )
        assert r_blocked.status_code == 429
        assert "retry-after" in r_blocked.headers

    expected_hash = hashlib.sha256(b"target.reset@example.com").hexdigest()[:32]
    with h_rl.session_factory() as db:
        keys = [r.bucket_key for r in db.scalars(select(RateLimitRecord)).all()]
    assert f"forgot_password:acct:{expected_hash}" in keys


def test_change_password_and_session_management_endpoints(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness(rate_limit_login_max=20)
    alice = h.create_user("alice.sessions@example.com", "alice-initial-pass-123")
    h.create_user("bob.sessions@example.com", "bob-initial-pass-123")

    alice_c1, csrf1 = h.login_client("alice.sessions@example.com", "alice-initial-pass-123")
    alice_c2, _csrf2 = h.login_client("alice.sessions@example.com", "alice-initial-pass-123")
    alice_c3, csrf3 = h.login_client("alice.sessions@example.com", "alice-initial-pass-123")
    bob_c, _bob_csrf = h.login_client("bob.sessions@example.com", "bob-initial-pass-123")

    # Insert an expired session and an unused PasswordResetToken for Alice
    with h.session_factory() as db:
        db.add(
            SessionRecord(
                user_id=alice.id,
                token_hash="expired-session-hash-" + ("0" * 43),
                csrf_token="expired-csrf",
                expires_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        db.add(
            PasswordResetToken(
                user_id=alice.id,
                token_hash="unused-reset-hash-" + ("1" * 46),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        db.commit()

    # GET /sessions returns only non-expired sessions, current session first
    sessions_resp = alice_c1.get("/api/v1/auth/sessions")
    assert sessions_resp.status_code == 200
    sessions_data = sessions_resp.json()
    assert len(sessions_data) == 3
    assert sessions_data[0]["is_current"] is True
    assert all(item["is_current"] is False for item in sessions_data[1:])

    # Wrong current password -> 400
    wrong_cp = alice_c1.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong-password", "new_password": "alice-updated-pass-123"},
        headers={"X-CSRF-Token": csrf1},
    )
    assert wrong_cp.status_code == 400
    assert wrong_cp.json()["detail"] == "Current password is incorrect"

    # Reusing current password -> 400
    same_cp = alice_c1.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "alice-initial-pass-123",
            "new_password": "alice-initial-pass-123",
        },
        headers={"X-CSRF-Token": csrf1},
    )
    assert same_cp.status_code == 400
    assert same_cp.json()["detail"] == "New password must be different from your current password"

    # Valid change-password revokes other sessions & unused reset tokens, keeps current alive
    ok_cp = alice_c1.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": "alice-initial-pass-123",
            "new_password": "alice-updated-pass-123",
        },
        headers={"X-CSRF-Token": csrf1},
    )
    assert ok_cp.status_code == 200
    assert ok_cp.json() == {"ok": True, "revoked_sessions_count": 2}

    assert alice_c1.get("/api/v1/auth/me").status_code == 200
    assert alice_c2.get("/api/v1/auth/me").status_code == 401
    assert alice_c3.get("/api/v1/auth/me").status_code == 401
    assert bob_c.get("/api/v1/auth/me").status_code == 200

    with h.session_factory() as db:
        assert (
            db.scalars(
                select(PasswordResetToken).where(PasswordResetToken.user_id == alice.id)
            ).all()
            == []
        )

    # New session with updated password, then test DELETE /sessions/{id} and POST /logout-all
    alice_c4, csrf4 = h.login_client("alice.sessions@example.com", "alice-updated-pass-123")
    alice_c5, _csrf5 = h.login_client("alice.sessions@example.com", "alice-updated-pass-123")

    list_after = alice_c1.get("/api/v1/auth/sessions").json()
    assert len(list_after) == 3
    other_ids = [item["id"] for item in list_after if not item["is_current"]]
    assert len(other_ids) == 2

    # Revoke one specific other session
    del_other = alice_c1.delete(
        f"/api/v1/auth/sessions/{other_ids[0]}",
        headers={"X-CSRF-Token": csrf1},
    )
    assert del_other.status_code == 200
    assert del_other.json() == {"ok": True}
    assert len(alice_c1.get("/api/v1/auth/sessions").json()) == 2

    # Nonexistent session -> 404
    assert (
        alice_c1.delete(
            f"/api/v1/auth/sessions/{uuid.uuid4()}",
            headers={"X-CSRF-Token": csrf1},
        ).status_code
        == 404
    )

    # Revoking own current session via DELETE /sessions/{current_id} clears cookie
    current_id = list_after[0]["id"]
    del_self = alice_c1.delete(
        f"/api/v1/auth/sessions/{current_id}",
        headers={"X-CSRF-Token": csrf1},
    )
    assert del_self.status_code == 200
    assert alice_c1.get("/api/v1/auth/me").status_code == 401

    # Re-authenticate and test POST /logout-all revokes all remaining sessions for Alice only
    alice_c6, csrf6 = h.login_client("alice.sessions@example.com", "alice-updated-pass-123")
    active_before_all = alice_c6.get("/api/v1/auth/sessions").json()
    expected_revoked = len(active_before_all)
    assert expected_revoked >= 2

    logout_all_resp = alice_c6.post(
        "/api/v1/auth/logout-all",
        headers={"X-CSRF-Token": csrf6},
    )
    assert logout_all_resp.status_code == 200
    assert logout_all_resp.json() == {"ok": True, "revoked_sessions_count": expected_revoked}
    assert alice_c4.get("/api/v1/auth/me").status_code == 401
    assert alice_c5.get("/api/v1/auth/me").status_code == 401
    assert alice_c6.get("/api/v1/auth/me").status_code == 401
    assert bob_c.get("/api/v1/auth/me").status_code == 200

    with h.session_factory() as db:
        event_types = set(
            db.scalars(select(AuditEvent.event_type).where(AuditEvent.user_id == alice.id)).all()
        )
        assert {
            "auth.password_changed",
            "auth.session_revoked",
            "auth.sessions_revoked",
        }.issubset(event_types)
