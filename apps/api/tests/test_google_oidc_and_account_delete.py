from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select
from trip_api.config import Settings
from trip_api.db import (
    AuditEvent,
    OAuthConnection,
    Project,
    ProjectPhoto,
    RenderJob,
    RenderJobChunk,
    SessionRecord,
    Stage,
    User,
    UserIdentity,
    VaultActivity,
)
from trip_api.library_view import LibraryView
from trip_api.security import encrypt_secret
from trip_api.services import (
    stage_chart_cache_key,
    stage_events_cache_key,
    stage_lod_cache_key,
    stage_timeline_cache_key,
)

if TYPE_CHECKING:
    from conftest import HarnessFactory

_TEST_GOOGLE_CLIENT_ID = "test-google-client-id"
_TEST_GOOGLE_CLIENT_SECRET = "test-google-client-secret"
_TEST_GOOGLE_REDIRECT_URI = "http://localhost:5173/auth/google/callback"
_TEST_KID = "test-key-1"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_to_b64url(val: int) -> str:
    length = max(1, (val.bit_length() + 7) // 8)
    return _b64url(val.to_bytes(length, "big"))


def _make_rsa_keypair() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": _TEST_KID,
                "use": "sig",
                "alg": "RS256",
                "n": _int_to_b64url(pub_numbers.n),
                "e": _int_to_b64url(pub_numbers.e),
            }
        ]
    }
    return private_key, jwks


def _sign_id_token(
    private_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    *,
    kid: str = _TEST_KID,
    alg: str = "RS256",
) -> str:
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url(sig)}"


def _install_google_mock(
    app: Any,
    *,
    jwks: dict[str, Any],
    id_token_factory: Any,
    captured_token_forms: list[dict[str, str]] | None = None,
    token_status: int = 200,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com" and request.url.path == "/oauth2/v3/certs":
            return httpx.Response(200, json=jwks)
        if request.url.host == "oauth2.googleapis.com" and request.url.path == "/token":
            parsed = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
            if captured_token_forms is not None:
                captured_token_forms.append(parsed)
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": "test-access-token",
                    "token_type": "Bearer",
                    "id_token": id_token_factory(parsed),
                },
            )
        return httpx.Response(404, json={"error": "unexpected_url"})

    transport = httpx.MockTransport(_handler)
    app.state.google_http_client_factory = lambda: httpx.AsyncClient(transport=transport)


def _google_settings_overrides(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "google_auth_client_id": _TEST_GOOGLE_CLIENT_ID,
        "google_auth_client_secret": _TEST_GOOGLE_CLIENT_SECRET,
        "google_auth_redirect_uri": _TEST_GOOGLE_REDIRECT_URI,
    }
    base.update(extra)
    return base


def test_settings_rejects_half_configured_google_auth() -> None:
    base_kwargs: dict[str, Any] = {
        "app_env": "test",
        "session_secret": "test-session-secret-minimum-32-characters-long",
        "token_encryption_key": base64.urlsafe_b64encode(b"0123456789abcdef" * 2).decode("ascii"),
    }
    with pytest.raises(ValueError, match="GOOGLE_AUTH_CLIENT_ID"):
        Settings(**base_kwargs, google_auth_client_id=_TEST_GOOGLE_CLIENT_ID)

    with pytest.raises(ValueError, match="GOOGLE_AUTH_CLIENT_SECRET"):
        Settings(**base_kwargs, google_auth_client_secret=_TEST_GOOGLE_CLIENT_SECRET)

    with pytest.raises(ValueError, match="GOOGLE_AUTH_REDIRECT_URI"):
        Settings(
            **base_kwargs,
            google_auth_client_id=_TEST_GOOGLE_CLIENT_ID,
            google_auth_client_secret=_TEST_GOOGLE_CLIENT_SECRET,
            google_auth_redirect_uri="   ",
        )

    with pytest.raises(ValueError, match="https://"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            registration_enabled=False,
            database_url="postgresql+psycopg://trip_app:strong-prod-password-123@db:5432/trip",
            session_secret="prod-session-secret-minimum-32-characters-long",
            token_encryption_key=base64.urlsafe_b64encode(b"0123456789abcdef" * 2).decode("ascii"),
            google_auth_client_id=_TEST_GOOGLE_CLIENT_ID,
            google_auth_client_secret=_TEST_GOOGLE_CLIENT_SECRET,
            google_auth_redirect_uri="http://localhost:5173/auth/google/callback",
        )

    cfg = Settings(
        **base_kwargs,
        google_auth_client_id=_TEST_GOOGLE_CLIENT_ID,
        google_auth_client_secret=_TEST_GOOGLE_CLIENT_SECRET,
    )
    assert cfg.google_auth_enabled is True
    assert cfg.google_client_id == ""


def test_public_config_exposes_google_auth_enabled_without_secret(
    make_harness: HarnessFactory,
) -> None:
    disabled_harness = make_harness()
    with TestClient(disabled_harness.app) as client:
        resp = client.get("/api/v1/public-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["google_auth_enabled"] is False
        assert "google_auth_client_secret" not in data

    enabled_harness = make_harness(**_google_settings_overrides())
    with TestClient(enabled_harness.app) as client:
        resp = client.get("/api/v1/public-config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["google_auth_enabled"] is True
        assert _TEST_GOOGLE_CLIENT_SECRET not in resp.text


def test_google_oidc_creates_new_passwordless_user_and_returns_existing_by_subject(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(**_google_settings_overrides())
    private_key, jwks = _make_rsa_keypair()
    captured_forms: list[dict[str, str]] = []
    expected_nonce: list[str] = []

    def _make_token(_form: dict[str, str]) -> str:
        now = int(datetime.now(UTC).timestamp())
        return _sign_id_token(
            private_key,
            {
                "iss": "https://accounts.google.com",
                "aud": _TEST_GOOGLE_CLIENT_ID,
                "sub": "google-sub-1001",
                "email": "Alpine.Rider@Example.com",
                "email_verified": True,
                "name": "Alpine Rider",
                "nonce": expected_nonce[-1],
                "iat": now,
                "exp": now + 300,
            },
        )

    _install_google_mock(
        harness.app,
        jwks=jwks,
        id_token_factory=_make_token,
        captured_token_forms=captured_forms,
    )

    with TestClient(harness.app) as client:
        start_resp = client.get("/api/v1/auth/google/start")
        assert start_resp.status_code == 200
        authorize_url = start_resp.json()["authorize_url"]
        parsed = urlparse(authorize_url)
        assert parsed.netloc == "accounts.google.com"
        qs = parse_qs(parsed.query)
        assert qs["client_id"] == [_TEST_GOOGLE_CLIENT_ID]
        assert qs["redirect_uri"] == [_TEST_GOOGLE_REDIRECT_URI]
        assert qs["code_challenge_method"] == ["S256"]
        state = qs["state"][0]
        expected_nonce.append(qs["nonce"][0])

        cb_resp = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "auth-code-1", "state": state},
        )
        assert cb_resp.status_code == 200, cb_resp.text
        payload = cb_resp.json()
        assert payload["user"]["email"] == "alpine.rider@example.com"
        assert payload["user"]["display_name"] == "Alpine Rider"
        assert payload["user"]["has_password"] is False
        assert len(captured_forms) == 1
        assert captured_forms[0]["code"] == "auth-code-1"
        assert "code_verifier" in captured_forms[0]

        me_resp = client.get("/api/v1/auth/me")
        assert me_resp.status_code == 200
        assert me_resp.json()["user"]["has_password"] is False

        login_resp = client.post(
            "/api/v1/auth/login",
            json={"email": "alpine.rider@example.com", "password": "any-password-here"},
        )
        assert login_resp.status_code == 401

        replay_resp = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "auth-code-1", "state": state},
        )
        assert replay_resp.status_code == 400

    with harness.session_factory() as db:
        users = list(db.scalars(select(User)).all())
        assert len(users) == 1
        assert users[0].password_hash is None
        assert users[0].email_verified_at is not None
        identities = list(db.scalars(select(UserIdentity)).all())
        assert len(identities) == 1
        assert identities[0].provider == "google"
        assert identities[0].provider_subject == "google-sub-1001"
        user_id = users[0].id

    harness.settings.registration_enabled = False
    with TestClient(harness.app) as returning_client:
        start_resp2 = returning_client.get("/api/v1/auth/google/start")
        assert start_resp2.status_code == 200
        qs2 = parse_qs(urlparse(start_resp2.json()["authorize_url"]).query)
        expected_nonce.append(qs2["nonce"][0])
        cb_resp2 = returning_client.post(
            "/api/v1/auth/google/callback",
            json={"code": "auth-code-2", "state": qs2["state"][0]},
        )
        assert cb_resp2.status_code == 200
        assert cb_resp2.json()["user"]["id"] == str(user_id)


def test_google_oidc_auto_links_verified_account_and_blocks_unverified_account(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(**_google_settings_overrides())
    verified_user = harness.create_user("verified@example.com", verified=True)
    harness.create_user("unverified@example.com", verified=False)
    private_key, jwks = _make_rsa_keypair()
    current_claims: dict[str, Any] = {}

    def _make_token(_form: dict[str, str]) -> str:
        now = int(datetime.now(UTC).timestamp())
        return _sign_id_token(
            private_key,
            {
                "iss": "https://accounts.google.com",
                "aud": _TEST_GOOGLE_CLIENT_ID,
                "iat": now,
                "exp": now + 300,
                **current_claims,
            },
        )

    _install_google_mock(harness.app, jwks=jwks, id_token_factory=_make_token)

    with TestClient(harness.app) as client:
        start1 = client.get("/api/v1/auth/google/start")
        qs1 = parse_qs(urlparse(start1.json()["authorize_url"]).query)
        current_claims.clear()
        current_claims.update(
            {
                "sub": "google-sub-verified",
                "email": "verified@example.com",
                "email_verified": True,
                "name": "Verified Rider",
                "nonce": qs1["nonce"][0],
            }
        )
        cb1 = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "c1", "state": qs1["state"][0]},
        )
        assert cb1.status_code == 200, cb1.text
        assert cb1.json()["user"]["id"] == str(verified_user.id)
        assert cb1.json()["user"]["has_password"] is True

        start2 = client.get("/api/v1/auth/google/start")
        qs2 = parse_qs(urlparse(start2.json()["authorize_url"]).query)
        current_claims.clear()
        current_claims.update(
            {
                "sub": "google-sub-unverified",
                "email": "unverified@example.com",
                "email_verified": True,
                "name": "Unverified Target",
                "nonce": qs2["nonce"][0],
            }
        )
        cb2 = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "c2", "state": qs2["state"][0]},
        )
        assert cb2.status_code == 409
        assert "unverified" in cb2.json()["detail"].lower()

    with harness.session_factory() as db:
        identities = list(db.scalars(select(UserIdentity)).all())
        assert len(identities) == 1
        assert identities[0].user_id == verified_user.id


def test_google_oidc_rejects_bad_state_nonce_unverified_email_and_disabled_registration(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(**_google_settings_overrides(registration_enabled=False))
    private_key, jwks = _make_rsa_keypair()
    current_claims: dict[str, Any] = {}

    def _make_token(_form: dict[str, str]) -> str:
        now = int(datetime.now(UTC).timestamp())
        return _sign_id_token(
            private_key,
            {
                "iss": "https://accounts.google.com",
                "aud": _TEST_GOOGLE_CLIENT_ID,
                "iat": now,
                "exp": now + 300,
                **current_claims,
            },
        )

    _install_google_mock(harness.app, jwks=jwks, id_token_factory=_make_token)

    with TestClient(harness.app) as client:
        no_cookie = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "abc", "state": "missing-cookie-state"},
        )
        assert no_cookie.status_code == 400

        s1 = client.get("/api/v1/auth/google/start")
        qs1 = parse_qs(urlparse(s1.json()["authorize_url"]).query)
        bad_state = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "abc", "state": "wrong-state-\u00e9"},
        )
        assert bad_state.status_code == 400

        s2 = client.get("/api/v1/auth/google/start")
        qs2 = parse_qs(urlparse(s2.json()["authorize_url"]).query)
        current_claims.clear()
        current_claims.update(
            {
                "sub": "google-sub-bad-nonce",
                "email": "nonce@example.com",
                "email_verified": True,
                "nonce": "wrong-nonce",
            }
        )
        bad_nonce = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "abc", "state": qs2["state"][0]},
        )
        assert bad_nonce.status_code == 400

        s3 = client.get("/api/v1/auth/google/start")
        qs3 = parse_qs(urlparse(s3.json()["authorize_url"]).query)
        current_claims.clear()
        current_claims.update(
            {
                "sub": "google-sub-unverified-google",
                "email": "unverified-google@example.com",
                "email_verified": False,
                "nonce": qs3["nonce"][0],
            }
        )
        unverified_email = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "abc", "state": qs3["state"][0]},
        )
        assert unverified_email.status_code == 400

        s4 = client.get("/api/v1/auth/google/start")
        qs4 = parse_qs(urlparse(s4.json()["authorize_url"]).query)
        current_claims.clear()
        current_claims.update(
            {
                "sub": "google-sub-new-blocked",
                "email": "blocked-new@example.com",
                "email_verified": True,
                "nonce": qs4["nonce"][0],
            }
        )
        disabled_reg = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "abc", "state": qs4["state"][0]},
        )
        assert disabled_reg.status_code == 403
        assert qs1["state"][0] != qs2["state"][0]


def test_set_password_for_google_only_user_revokes_other_sessions(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(**_google_settings_overrides())
    private_key, jwks = _make_rsa_keypair()
    nonce_box: list[str] = []

    def _make_token(_form: dict[str, str]) -> str:
        now = int(datetime.now(UTC).timestamp())
        return _sign_id_token(
            private_key,
            {
                "iss": "https://accounts.google.com",
                "aud": _TEST_GOOGLE_CLIENT_ID,
                "sub": "google-sub-setpw",
                "email": "google-only@example.com",
                "email_verified": True,
                "name": "Google Only",
                "nonce": nonce_box[-1],
                "iat": now,
                "exp": now + 300,
            },
        )

    _install_google_mock(harness.app, jwks=jwks, id_token_factory=_make_token)

    client1 = TestClient(harness.app)
    client2 = TestClient(harness.app)
    try:
        s1 = client1.get("/api/v1/auth/google/start")
        qs1 = parse_qs(urlparse(s1.json()["authorize_url"]).query)
        nonce_box.append(qs1["nonce"][0])
        cb1 = client1.post(
            "/api/v1/auth/google/callback",
            json={"code": "c1", "state": qs1["state"][0]},
        )
        assert cb1.status_code == 200
        csrf1 = cb1.json()["csrf_token"]

        s2 = client2.get("/api/v1/auth/google/start")
        qs2 = parse_qs(urlparse(s2.json()["authorize_url"]).query)
        nonce_box.append(qs2["nonce"][0])
        cb2 = client2.post(
            "/api/v1/auth/google/callback",
            json={"code": "c2", "state": qs2["state"][0]},
        )
        assert cb2.status_code == 200

        bad_change = client1.post(
            "/api/v1/auth/change-password",
            headers={"X-CSRF-Token": csrf1},
            json={
                "current_password": "not-set-yet-password",
                "new_password": "brand-new-password-123",
            },
        )
        assert bad_change.status_code == 400

        set_resp = client1.post(
            "/api/v1/auth/set-password",
            headers={"X-CSRF-Token": csrf1},
            json={"new_password": "brand-new-password-123"},
        )
        assert set_resp.status_code == 200, set_resp.text
        assert set_resp.json() == {"ok": True, "revoked_sessions_count": 1}

        assert client2.get("/api/v1/auth/me").status_code == 401
        me1 = client1.get("/api/v1/auth/me")
        assert me1.status_code == 200
        assert me1.json()["user"]["has_password"] is True

        second_set = client1.post(
            "/api/v1/auth/set-password",
            headers={"X-CSRF-Token": csrf1},
            json={"new_password": "another-password-12345"},
        )
        assert second_set.status_code == 400

        pw_client, _ = harness.login_client("google-only@example.com", "brand-new-password-123")
        try:
            assert pw_client.get("/api/v1/auth/me").status_code == 200
        finally:
            pw_client.close()
    finally:
        client1.close()
        client2.close()


def test_delete_account_with_password_purges_db_objects_and_deauthorizes_strava(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness()
    user = harness.create_user("delete-me@example.com", "correct-horse-battery-staple")
    other_user = harness.create_user("keep-me@example.com", "correct-horse-battery-staple")

    deauth_tokens: list[str] = []

    def _strava_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/deauthorize":
            form = parse_qs(request.content.decode("utf-8"))
            deauth_tokens.append(form.get("access_token", [""])[0])
            return httpx.Response(200, json={"access_token": "revoked"})
        return httpx.Response(404)

    transport = httpx.MockTransport(_strava_handler)
    harness.app.state.http_client_factory = lambda: httpx.AsyncClient(transport=transport)

    object_keys: list[str] = []
    other_key = "bronze/other-user/keep.gpx"
    harness.store.put_bytes(other_key, b"keep")

    with harness.session_factory() as db:
        proj = Project(owner_user_id=user.id, title="Delete Trip", settings_json={})
        db.add(proj)
        db.flush()

        stage_id = uuid.uuid4()
        bronze_key = f"bronze/{proj.id}/{stage_id}/raw.gpx"
        silver_key = f"silver/{proj.id}/{stage_id}.parquet"
        lod_key = stage_lod_cache_key(proj.id, stage_id)
        chart_key = stage_chart_cache_key(proj.id, stage_id)
        timeline_key = stage_timeline_cache_key(proj.id, stage_id)
        events_key = stage_events_cache_key(proj.id, stage_id)
        db.add(
            Stage(
                id=stage_id,
                project_id=proj.id,
                source_type="gpx",
                name="Stage 1",
                stage_order=0,
                day_index=1,
                bronze_object_key=bronze_key,
                silver_object_key=silver_key,
                point_count=10,
                distance_m=1000.0,
            )
        )

        photo_key = f"photos/{proj.id}/photo1.webp"
        thumb_key = f"photos/{proj.id}/photo1_thumb.webp"
        db.add(
            ProjectPhoto(
                project_id=proj.id,
                object_key=photo_key,
                thumb_object_key=thumb_key,
                width=100,
                height=100,
                byte_size=12,
            )
        )

        render_out_key = f"renders/{proj.id}/out.mp4"
        render_thumb_key = f"renders/{proj.id}/out.webp"
        db.add(
            RenderJob(
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="idem-del-1",
                idempotency_hash="hash-del-1",
                spec_json={},
                status="succeeded",
                output_object_key=render_out_key,
                thumbnail_key=render_thumb_key,
            )
        )

        vault_bronze = f"vault/{user.id}/act1.gpx"
        vault_silver = f"vault/{user.id}/act1.parquet"
        db.add(
            VaultActivity(
                user_id=user.id,
                source_provider="upload",
                external_activity_id="ext-1",
                name="Vault Ride",
                started_at=datetime.now(UTC),
                local_date=date(2026, 6, 1),
                bronze_object_key=vault_bronze,
                silver_object_key=vault_silver,
            )
        )

        key = harness.settings.decoded_encryption_key
        aad = f"oauth:{user.id}:strava".encode()
        db.add(
            OAuthConnection(
                user_id=user.id,
                provider="strava",
                external_athlete_id="999001",
                access_token_enc=encrypt_secret("strava-live-access-token", key, aad=aad),
                refresh_token_enc=encrypt_secret("strava-live-refresh-token", key, aad=aad),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scope="read,activity:read_all",
            )
        )
        db.commit()

        object_keys.extend(
            [
                bronze_key,
                silver_key,
                lod_key,
                chart_key,
                timeline_key,
                events_key,
                photo_key,
                thumb_key,
                render_out_key,
                render_thumb_key,
                vault_bronze,
                vault_silver,
            ]
        )

    for obj_key in object_keys:
        harness.store.put_bytes(obj_key, b"payload")

    client, csrf = harness.login_client("delete-me@example.com", "correct-horse-battery-staple")
    try:
        no_csrf = client.request(
            "DELETE",
            "/api/v1/auth/me",
            json={"current_password": "correct-horse-battery-staple"},
        )
        assert no_csrf.status_code == 403

        wrong_pw = client.request(
            "DELETE",
            "/api/v1/auth/me",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": "wrong-password-attempt"},
        )
        assert wrong_pw.status_code == 400

        del_resp = client.request(
            "DELETE",
            "/api/v1/auth/me",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": "correct-horse-battery-staple"},
        )
        assert del_resp.status_code == 204, del_resp.text
        assert client.get("/api/v1/auth/me").status_code == 401
    finally:
        client.close()

    assert deauth_tokens == ["strava-live-access-token"]
    for obj_key in object_keys:
        with pytest.raises(FileNotFoundError):
            harness.store.get_bytes(obj_key)
    assert harness.store.get_bytes(other_key) == b"keep"

    with harness.session_factory() as db:
        assert db.get(User, user.id) is None
        assert db.get(User, other_user.id) is not None
        assert db.scalars(select(Project).where(Project.owner_user_id == user.id)).all() == []
        assert db.scalars(select(Stage)).all() == []
        assert db.scalars(select(ProjectPhoto)).all() == []
        assert db.scalars(select(RenderJob)).all() == []
        assert db.scalars(select(VaultActivity)).all() == []
        assert db.scalars(select(OAuthConnection)).all() == []
        assert db.scalars(select(SessionRecord).where(SessionRecord.user_id == user.id)).all() == []
        assert db.scalars(select(AuditEvent).where(AuditEvent.user_id == user.id)).all() == []


def test_delete_account_for_google_only_user_requires_delete_confirmation(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(**_google_settings_overrides())
    private_key, jwks = _make_rsa_keypair()
    nonce_box: list[str] = []

    def _make_token(_form: dict[str, str]) -> str:
        now = int(datetime.now(UTC).timestamp())
        return _sign_id_token(
            private_key,
            {
                "iss": "https://accounts.google.com",
                "aud": _TEST_GOOGLE_CLIENT_ID,
                "sub": "google-sub-delete",
                "email": "google-delete@example.com",
                "email_verified": True,
                "name": "Google Delete",
                "nonce": nonce_box[-1],
                "iat": now,
                "exp": now + 300,
            },
        )

    _install_google_mock(harness.app, jwks=jwks, id_token_factory=_make_token)

    with TestClient(harness.app) as client:
        s1 = client.get("/api/v1/auth/google/start")
        qs1 = parse_qs(urlparse(s1.json()["authorize_url"]).query)
        nonce_box.append(qs1["nonce"][0])
        cb1 = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "c1", "state": qs1["state"][0]},
        )
        assert cb1.status_code == 200
        csrf = cb1.json()["csrf_token"]
        user_id = uuid.UUID(cb1.json()["user"]["id"])

        wrong_confirm = client.request(
            "DELETE",
            "/api/v1/auth/me",
            headers={"X-CSRF-Token": csrf},
            json={"confirmation": "nope"},
        )
        assert wrong_confirm.status_code == 400

        ok_del = client.request(
            "DELETE",
            "/api/v1/auth/me",
            headers={"X-CSRF-Token": csrf},
            json={"confirmation": "DELETE"},
        )
        assert ok_del.status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401

    with harness.session_factory() as db:
        assert db.get(User, user_id) is None
        assert db.scalars(select(UserIdentity)).all() == []


def test_google_oidc_verifies_jwt_claims_signature_alg_and_rotates_jwks(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness(**_google_settings_overrides())
    key1, jwks1 = _make_rsa_keypair()
    key2, jwks2 = _make_rsa_keypair()
    jwks2["keys"][0]["kid"] = "rotated-key-2"

    active_jwks: dict[str, Any] = jwks1
    jwks_calls = 0
    token_box: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_calls
        if request.url.host == "www.googleapis.com" and request.url.path == "/oauth2/v3/certs":
            jwks_calls += 1
            return httpx.Response(200, json=active_jwks)
        if request.url.host == "oauth2.googleapis.com" and request.url.path == "/token":
            return httpx.Response(
                200,
                json={"access_token": "test-tok", "id_token": token_box[-1]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)
    harness.app.state.google_http_client_factory = lambda: httpx.AsyncClient(transport=transport)

    with TestClient(harness.app) as client:

        def _run_callback(
            claims_override: dict[str, Any],
            *,
            signing_key: rsa.RSAPrivateKey = key1,
            kid: str = _TEST_KID,
            alg: str = "RS256",
        ) -> Any:
            s = client.get("/api/v1/auth/google/start")
            qs = parse_qs(urlparse(s.json()["authorize_url"]).query)
            now = int(datetime.now(UTC).timestamp())
            claims: dict[str, Any] = {
                "iss": "https://accounts.google.com",
                "aud": _TEST_GOOGLE_CLIENT_ID,
                "sub": "google-sub-jwt-test",
                "email": "jwt-test@example.com",
                "email_verified": True,
                "name": "JWT Tester",
                "nonce": qs["nonce"][0],
                "iat": now,
                "exp": now + 300,
            }
            claims.update(claims_override)
            token_box.append(_sign_id_token(signing_key, claims, kid=kid, alg=alg))
            return client.post(
                "/api/v1/auth/google/callback",
                json={"code": "code-jwt", "state": qs["state"][0]},
            )

        assert _run_callback({"iss": "https://evil.example.com"}).status_code == 400
        assert _run_callback({"aud": "wrong-client-id"}).status_code == 400
        assert _run_callback({"exp": int(datetime.now(UTC).timestamp()) - 120}).status_code == 400
        assert _run_callback({}, alg="none").status_code == 400
        assert _run_callback({}, signing_key=key2, kid=_TEST_KID).status_code == 400

        active_jwks = jwks2
        ok_rotated = _run_callback({}, signing_key=key2, kid="rotated-key-2")
        assert ok_rotated.status_code == 200, ok_rotated.text
        assert jwks_calls >= 2


def test_delete_account_cleans_chunked_manifest_remote_backend_and_library_views(
    make_harness: HarnessFactory,
) -> None:
    harness = make_harness()
    user = harness.create_user("remote-del@example.com", "correct-horse-battery-staple")

    cancelled_refs: list[str] = []
    cleaned_job_ids: list[uuid.UUID] = []

    class _FakeRemoteBackend:
        name = "aws_sfn_lambda"
        is_remote = True

        def cancel(self, execution_ref: str) -> None:
            cancelled_refs.append(execution_ref)

        def cleanup_staging(self, job_id: uuid.UUID) -> None:
            cleaned_job_ids.append(job_id)

    harness.app.state.render_backend = _FakeRemoteBackend()

    with harness.session_factory() as db:
        proj = Project(owner_user_id=user.id, title="Remote Trip", settings_json={})
        db.add(proj)
        db.flush()

        job_id = uuid.uuid4()
        manifest_key = f"outputs/manifests/{job_id}.json"
        chunk_key = f"outputs/chunks/{job_id}/0000.mp4"
        db.add(
            RenderJob(
                id=job_id,
                project_id=proj.id,
                owner_user_id=user.id,
                idempotency_key="idem-remote-1",
                idempotency_hash="hash-remote-1",
                spec_json={},
                status="running",
                is_chunked=True,
                render_backend="aws_sfn_lambda",
                external_execution_id="arn:aws:states:eu-central-1:000000000000:execution:test:1",
            )
        )
        db.add(
            RenderJobChunk(
                render_job_id=job_id,
                chunk_index=0,
                start_frame=0,
                end_frame=100,
                status="succeeded",
                chunk_object_key=chunk_key,
            )
        )
        db.add(
            LibraryView(
                user_id=user.id,
                name="My Saved View",
                rules={"sport": ["cycling"]},
            )
        )
        db.commit()

    harness.store.put_bytes(manifest_key, b"{}")
    harness.store.put_bytes(chunk_key, b"chunk")

    client, csrf = harness.login_client("remote-del@example.com", "correct-horse-battery-staple")
    try:
        resp = client.request(
            "DELETE",
            "/api/v1/auth/me",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": "correct-horse-battery-staple"},
        )
        assert resp.status_code == 204, resp.text
    finally:
        client.close()

    assert cancelled_refs == ["arn:aws:states:eu-central-1:000000000000:execution:test:1"]
    assert cleaned_job_ids == [job_id]
    with pytest.raises(FileNotFoundError):
        harness.store.get_bytes(manifest_key)
    with pytest.raises(FileNotFoundError):
        harness.store.get_bytes(chunk_key)
    with harness.session_factory() as db:
        assert db.scalars(select(LibraryView).where(LibraryView.user_id == user.id)).all() == []
