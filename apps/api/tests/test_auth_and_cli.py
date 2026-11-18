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
from sqlalchemy import func, inspect, select
from starlette.requests import Request
from trip_api.cli import main as cli_main
from trip_api.db import (
    AuditEvent,
    OAuthConnection,
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

    command.upgrade(cfg, "head")
    command.check(cfg)

    engine = create_db_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "users",
            "sessions",
            "oauth_states",
            "oauth_connections",
            "projects",
            "stages",
            "render_jobs",
            "audit_events",
            "rate_limits",
        }.issubset(tables)
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

    # 1. Unauthenticated requests must receive 401 across all protected routes
    unauth_requests: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/api/v1/auth/me", None),
        ("POST", "/api/v1/auth/logout", None),
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

    # Bob cannot hijack Alice's OAuth state
    assert bob.get(f"/api/v1/strava/callback?code=c&state={alice_state}").status_code == 400

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
) -> None:
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
    single_h = make_harness(registration_invite_code="alpine-2026")
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

    # Valid registration with single invite code -> 201 + cookie + CSRF + /me + audit event
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
    assert body["user"]["email"] == "rider.one@example.com"
    assert body["user"]["display_name"] == "Alpine Explorer"
    assert "trip_session" in single_client.cookies
    csrf_header = ok_resp.headers.get("x-csrf-token")
    assert csrf_header is not None
    assert csrf_header == body["csrf_token"]

    me_resp = single_client.get("/api/v1/auth/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["user"]["email"] == "rider.one@example.com"

    with single_h.session_factory() as db:
        audit_events = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "auth.register")
        ).all()
        assert len(audit_events) == 1
        assert str(audit_events[0].user_id) == body["user"]["id"]

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
    )
    for var_name in expected_vars:
        assert var_name in compose_text, f"{var_name} missing from infra/compose.yaml"
        assert var_name in env_example_text, f"{var_name} missing from .env.example"
