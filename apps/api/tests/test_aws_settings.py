from __future__ import annotations

import base64
from typing import Any

import pytest
from trip_api import render_backends
from trip_api.config import Settings
from trip_api.render_backends import build_render_backend

ARN = "arn:aws:states:eu-central-1:123456789012:stateMachine:trip"


def production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "dev_auth_enabled": False,
        "smtp_host": "smtp.example.com",
        "database_url": "postgresql+psycopg://trip:strong-pw@db.internal:5432/trip",
        "session_secret": "x" * 48,
        "token_encryption_key": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        "allowed_origins_raw": "https://trips.example.com",
        "render_backend": "aws_sfn_lambda",
        "render_aws_state_machine_arn": ARN,
        "render_aws_staging_bucket": "trip-staging",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture(autouse=True)
def clean_aws_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "RENDER_AWS_ACCESS_KEY_ID",
        "RENDER_AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "RENDER_AWS_REGION",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_read_the_standard_aws_key_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIARENDER")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "render-secret")

    settings = Settings(_env_file=None)

    assert settings.aws_access_key_id == "AKIARENDER"
    assert settings.aws_secret_access_key == "render-secret"


@pytest.mark.parametrize("name", ["RENDER_AWS_ACCESS_KEY_ID", "RENDER_AWS_SECRET_ACCESS_KEY"])
def test_old_render_key_variables_fail_at_startup_with_a_rename_hint(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "old-value")

    with pytest.raises(ValueError, match=f"{name}.*renamed.*{name.removeprefix('RENDER_')}"):
        Settings(_env_file=None)


def test_old_render_key_variable_in_the_env_file_fails_at_startup(tmp_path: Any) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RENDER_AWS_ACCESS_KEY_ID=old-value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="RENDER_AWS_ACCESS_KEY_ID"):
        Settings(_env_file=str(env_file))


def test_empty_old_render_key_variables_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_AWS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("RENDER_AWS_SECRET_ACCESS_KEY", "")

    Settings(_env_file=None)


@pytest.mark.parametrize("name", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"])
def test_only_one_aws_key_is_rejected_for_the_remote_render_backend(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "value")

    with pytest.raises(ValueError, match="AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"):
        production_settings()


def test_remote_render_backend_clients_use_the_aws_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIARENDER")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "render-secret")
    calls: list[dict[str, Any]] = []

    def fake_client(_service: str, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(render_backends.boto3, "client", fake_client)

    build_render_backend(production_settings())

    assert [call["aws_access_key_id"] for call in calls] == ["AKIARENDER", "AKIARENDER"]
    assert [call["aws_secret_access_key"] for call in calls] == ["render-secret"] * 2


def test_settings_read_the_standard_aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    assert Settings(_env_file=None).aws_region == "us-east-1"


def test_old_render_region_variable_fails_at_startup_with_a_rename_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RENDER_AWS_REGION", "eu-central-1")

    with pytest.raises(ValueError, match="RENDER_AWS_REGION.*renamed.*AWS_REGION"):
        Settings(_env_file=None)


def test_remote_render_backend_uses_the_aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    calls: list[dict[str, Any]] = []

    def fake_client(_service: str, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(render_backends.boto3, "client", fake_client)

    backend = build_render_backend(production_settings())

    assert [call["region_name"] for call in calls] == ["us-east-1", "us-east-1"]
    assert backend.region == "us-east-1"  # type: ignore[attr-defined]
