from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from trip_api.config import Settings
from trip_api.db import AuditEvent, EmailVerificationToken, PasswordResetToken, User

if TYPE_CHECKING:
    from conftest import HarnessFactory


def _prod_base_kwargs() -> dict[str, Any]:
    return {
        "app_env": "production",
        "dev_auth_enabled": False,
        "database_url": "postgresql+psycopg://trip_app:strong-prod-secret@db:5432/trip_animator",
        "session_secret": "x" * 40,
        "token_encryption_key": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
    }


def test_settings_open_registration_defaults_and_production_fail_closed() -> None:
    dev_cfg = Settings(app_env="test")
    assert dev_cfg.registration_enabled is True
    assert dev_cfg.turnstile_enabled is False

    with pytest.raises(ValueError, match="SMTP_HOST"):
        Settings(**_prod_base_kwargs(), registration_enabled=True, smtp_host="")

    disabled_prod = Settings(**_prod_base_kwargs(), registration_enabled=False, smtp_host="")
    assert disabled_prod.registration_enabled is False

    enabled_prod = Settings(
        **_prod_base_kwargs(),
        registration_enabled=True,
        smtp_host="smtp.example.com",
    )
    assert enabled_prod.registration_enabled is True

    with pytest.raises(ValueError, match="TURNSTILE_SITE_KEY"):
        Settings(app_env="test", turnstile_site_key="site-only", turnstile_secret_key="")

    with pytest.raises(ValueError, match="TURNSTILE_SITE_KEY"):
        Settings(app_env="test", turnstile_site_key="", turnstile_secret_key="secret-only")

    both_turnstile = Settings(
        app_env="test",
        turnstile_site_key="0x4AAAA-site",
        turnstile_secret_key="0x4AAAA-secret",
    )
    assert both_turnstile.turnstile_enabled is True


def test_public_config_exposes_open_registration_and_turnstile_site_key(
    make_harness: HarnessFactory,
) -> None:
    default_h = make_harness()
    cfg = TestClient(default_h.app).get("/api/v1/public-config").json()
    assert cfg["registration_enabled"] is True
    assert cfg["turnstile_site_key"] is None

    disabled_h = make_harness(registration_enabled=False)
    disabled_cfg = TestClient(disabled_h.app).get("/api/v1/public-config").json()
    assert disabled_cfg["registration_enabled"] is False

    turnstile_h = make_harness(
        turnstile_site_key="0x4AAAA-public-site-key",
        turnstile_secret_key="test-turnstile-secret",
    )
    ts_cfg = TestClient(turnstile_h.app).get("/api/v1/public-config").json()
    assert ts_cfg["turnstile_site_key"] == "0x4AAAA-public-site-key"
    assert "test-turnstile-secret" not in str(ts_cfg)


def test_open_registration_anti_enumeration_and_background_email(
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

    h = make_harness(public_base_url="https://trips.example.com/")
    client = TestClient(h.app)

    new_resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "  New.Rider@Example.COM ",
            "display_name": "New Rider",
            "password": "open-signup-password-123",
        },
    )
    assert new_resp.status_code == 202, new_resp.text
    new_body = new_resp.json()
    assert new_body["status"] == "verification_required"
    assert new_body["email"] == "new.rider@example.com"
    assert len(sent_emails) == 1
    first_token = sent_emails[-1][2].split("token=", 1)[1]

    unverified_dup = client.post(
        "/api/v1/auth/register",
        json={
            "email": "new.rider@example.com",
            "display_name": "Attacker Name",
            "password": "attacker-different-password-123",
        },
    )
    assert unverified_dup.status_code == 202
    assert unverified_dup.json() == new_body
    assert len(sent_emails) == 2
    second_token = sent_emails[-1][2].split("token=", 1)[1]
    assert second_token != first_token

    with h.session_factory() as db:
        users = db.scalars(select(User).where(User.email == "new.rider@example.com")).all()
        assert len(users) == 1
        assert users[0].display_name == "New Rider"

    verify_resp = client.post("/api/v1/auth/verify-email", json={"token": second_token})
    assert verify_resp.status_code == 200

    assert (
        TestClient(h.app)
        .post(
            "/api/v1/auth/login",
            json={"email": "new.rider@example.com", "password": "open-signup-password-123"},
        )
        .status_code
        == 200
    )

    verified_dup = TestClient(h.app).post(
        "/api/v1/auth/register",
        json={
            "email": "new.rider@example.com",
            "display_name": "Duplicate Attempt",
            "password": "another-valid-password-123",
        },
    )
    assert verified_dup.status_code == 202
    assert verified_dup.json() == new_body
    assert len(sent_emails) == 2

    with h.session_factory() as db:
        ignored_audits = db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "auth.register_existing_ignored")
        ).all()
        assert len(ignored_audits) == 1


def test_background_smtp_failure_does_not_fail_registration_or_leak_tokens(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import trip_api.routers.auth as auth_router

    def _boom(_settings: Any, _to: str, _name: str, _url: str) -> None:
        raise RuntimeError("smtp-connection-refused-secret-password")

    monkeypatch.setattr(auth_router, "send_verification_email", _boom)

    h = make_harness()
    client = TestClient(h.app)
    with caplog.at_level(logging.ERROR, logger="trip_api.routers.auth"):
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": "smtpfail@example.com",
                "display_name": "SMTP Fail",
                "password": "valid-password-1234",
            },
        )
    assert resp.status_code == 202
    with h.session_factory() as db:
        tok = db.scalar(select(EmailVerificationToken))
        assert tok is not None
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in log_text
    assert "smtp-connection-refused-secret-password" not in log_text
    assert "token=" not in log_text


def test_turnstile_verification_on_register_and_forgot_password(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trip_api.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "send_verification_email", lambda *_args: None)
    monkeypatch.setattr(auth_router, "send_password_reset_email", lambda *_args: None)

    received_forms: list[dict[str, str]] = []
    mock_result: dict[str, Any] = {"status_code": 200, "json": {"success": True}}

    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        form_data = dict(httpx.QueryParams(request.content.decode("utf-8")))
        received_forms.append(form_data)
        return httpx.Response(mock_result["status_code"], json=mock_result["json"])

    h = make_harness(
        turnstile_site_key="0x4AAAA-site",
        turnstile_secret_key="0x4AAAA-secret",
    )
    h.app.state.turnstile_http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(_handler)
    )
    h.create_user("existing@example.com", verified=True)
    client = TestClient(h.app)

    missing_reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "ts1@example.com",
            "display_name": "TS One",
            "password": "valid-password-1234",
        },
    )
    assert missing_reg.status_code == 400
    assert "challenge" in missing_reg.json()["detail"].lower()

    mock_result["json"] = {"success": False, "error-codes": ["invalid-input-response"]}
    bad_reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "ts1@example.com",
            "display_name": "TS One",
            "password": "valid-password-1234",
            "turnstile_token": "bad-token",
        },
    )
    assert bad_reg.status_code == 400
    assert received_forms[-1]["secret"] == "0x4AAAA-secret"
    assert received_forms[-1]["response"] == "bad-token"

    mock_result["status_code"] = 502
    upstream_err = client.post(
        "/api/v1/auth/register",
        json={
            "email": "ts1@example.com",
            "display_name": "TS One",
            "password": "valid-password-1234",
            "turnstile_token": "some-token",
        },
    )
    assert upstream_err.status_code == 503

    mock_result["status_code"] = 200
    mock_result["json"] = {"success": True}
    ok_reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "ts1@example.com",
            "display_name": "TS One",
            "password": "valid-password-1234",
            "turnstile_token": "good-token",
        },
    )
    assert ok_reg.status_code == 202

    missing_fp = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "existing@example.com"},
    )
    assert missing_fp.status_code == 400

    ok_fp = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "existing@example.com", "turnstile_token": "good-fp-token"},
    )
    assert ok_fp.status_code == 202
    with h.session_factory() as db:
        reset_tok = db.scalar(select(PasswordResetToken))
        assert reset_tok is not None
