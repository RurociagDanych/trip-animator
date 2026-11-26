"""Application settings with fail-closed production security invariants."""

from __future__ import annotations

import base64
import os
import secrets
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_TOKEN_KEY = "A" * 43 + "="


class Settings(BaseSettings):
    """Typed environment settings for the Trip Animator API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **values: Any) -> None:
        if (
            values.get("app_env") == "production"
            and "s3_secret_access_key" not in values
            and "S3_SECRET_ACCESS_KEY" not in os.environ
            and values.get("object_store_backend") != "s3"
        ):
            values["s3_secret_access_key"] = ""
        super().__init__(**values)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    dev_auth_enabled: bool = True
    registration_invite_code: str = ""
    public_base_url: str = "http://localhost:5173"

    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@trip-animator.local"
    smtp_use_tls: bool = True
    email_verification_ttl_hours: int = Field(default=24, ge=1, le=168)
    password_reset_ttl_minutes: int = Field(default=60, ge=5, le=1440)

    database_url: str = (
        "postgresql+psycopg://trip_app:dev-postgres-password-change-me@localhost:5432/trip_animator"
    )
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_timeout_s: int = Field(default=30, ge=1, le=300)
    db_pool_recycle_s: int = Field(default=1800, ge=60, le=86400)

    session_secret: str = ""
    session_cookie_name: str = "trip_session"
    session_ttl_hours: int = 168
    token_encryption_key: str = ""
    allowed_origins_raw: str = Field(
        default="http://localhost:5173,http://localhost:8080",
        alias="ALLOWED_ORIGINS",
    )

    object_store_backend: Literal["local", "s3"] = "local"
    local_object_store_dir: str = "./data/objects"
    gold_output_dir: str = "./data/gold"
    gold_s3_key: str = "gold/trip_analytics.duckdb"
    gold_sync_interval_s: float = Field(default=60.0, ge=0.0, le=3600.0)
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "trip-animator"
    s3_region: str = "us-east-1"

    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost:5173/strava/callback"
    strava_verify_token: str = ""
    strava_webhook_subscription_id: int | None = Field(default=None, ge=1)
    strava_api_base_url: str = "https://www.strava.com"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:5173/google-photos/callback"

    render_retention_days: int = Field(default=7, ge=1, le=3650)
    render_backend: Literal["local", "aws_sfn_lambda"] = "local"
    render_aws_region: str = "eu-central-1"
    render_aws_state_machine_arn: str = ""
    render_aws_staging_bucket: str = ""
    render_aws_access_key_id: str = ""
    render_aws_secret_access_key: str = ""
    render_aws_endpoint_url: str = ""
    render_callback_base_url: str = ""
    render_callback_secret: str = ""
    render_remote_chunk_threshold_frames: int = Field(default=300, ge=1, le=100_000)
    render_remote_chunk_size_frames: int = Field(default=200, ge=1, le=10_000)
    render_remote_preroll_frames: int = Field(default=15, ge=0, le=300)
    render_remote_dispatch_interval_s: float = Field(default=2.0, ge=0.0, le=300.0)
    render_remote_max_attempts: int = Field(default=3, ge=1, le=20)
    rate_limit_retention_hours: int = Field(default=24, ge=1, le=8760)
    metrics_token: str = ""

    rate_limit_login_max: int = 10
    rate_limit_login_account_max: int = 10
    rate_limit_login_window_s: int = 60
    rate_limit_register_max: int = 10
    rate_limit_register_account_max: int = 5
    rate_limit_register_window_s: int = 60
    rate_limit_verify_email_max: int = 20
    rate_limit_verify_email_window_s: int = 60
    rate_limit_resend_verification_max: int = 5
    rate_limit_resend_verification_account_max: int = 3
    rate_limit_resend_verification_window_s: int = 60
    rate_limit_forgot_password_max: int = 5
    rate_limit_forgot_password_account_max: int = 3
    rate_limit_forgot_password_window_s: int = 60
    rate_limit_reset_password_max: int = 10
    rate_limit_reset_password_window_s: int = 60
    rate_limit_gpx_max: int = 20
    rate_limit_gpx_window_s: int = 60
    rate_limit_photo_max: int = 60
    rate_limit_photo_window_s: int = 60
    rate_limit_strava_import_max: int = 60
    rate_limit_strava_import_window_s: int = 60
    rate_limit_strava_webhook_max: int = 60
    rate_limit_strava_webhook_window_s: int = 60
    rate_limit_share_max: int = 120
    rate_limit_share_window_s: int = 60
    rate_limit_render_max: int = 10
    rate_limit_render_window_s: int = 60

    @field_validator("strava_webhook_subscription_id", mode="before")
    @classmethod
    def _empty_subscription_id_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    map_tile_allowlist_raw: str = Field(
        default="https://tiles.openfreemap.org",
        alias="MAP_TILE_ALLOWLIST",
    )

    @property
    def registration_invite_codes(self) -> tuple[str, ...]:
        return tuple(
            code.strip() for code in self.registration_invite_code.split(",") if code.strip()
        )

    @property
    def registration_enabled(self) -> bool:
        return len(self.registration_invite_codes) > 0

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins_raw.split(",") if o.strip()]

    @property
    def map_tile_hosts(self) -> frozenset[str]:
        hosts: set[str] = set()
        for item in self.map_tile_allowlist_raw.split(","):
            cleaned = item.strip()
            if "://" in cleaned:
                cleaned = cleaned.split("://", 1)[1]
            cleaned = cleaned.split("/", 1)[0].split(":", 1)[0].lower()
            if cleaned:
                hosts.add(cleaned)
        return frozenset(hosts)

    @property
    def strava_allowed_hosts(self) -> frozenset[str]:
        return frozenset({"www.strava.com"})

    @property
    def decoded_encryption_key(self) -> bytes:
        try:
            raw = base64.urlsafe_b64decode(self.token_encryption_key.encode("ascii"))
        except Exception as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be valid URL-safe base64") from exc
        if len(raw) != 32:
            raise ValueError("TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return raw

    @model_validator(mode="after")
    def enforce_security_invariants(self) -> Settings:
        if self.app_env != "production":
            if not self.session_secret:
                self.session_secret = secrets.token_urlsafe(32)
            if not self.token_encryption_key:
                self.token_encryption_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

        _ = self.decoded_encryption_key
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")

        if self.app_env == "production":
            if self.dev_auth_enabled:
                raise ValueError("DEV_AUTH_ENABLED must be false in production")
            if self.session_secret.startswith("dev-only-"):
                raise ValueError("Default development SESSION_SECRET is forbidden in production")
            if self.token_encryption_key == _PLACEHOLDER_TOKEN_KEY:
                raise ValueError("Default TOKEN_ENCRYPTION_KEY is forbidden in production")
            if "dev-postgres-password-change-me" in self.database_url:
                raise ValueError(
                    "Default development DATABASE_URL password is forbidden in production"
                )
            if "minioadmin-change-me" in self.s3_secret_access_key:
                raise ValueError("Default S3_SECRET_ACCESS_KEY is forbidden in production")
            if "*" in self.allowed_origins:
                raise ValueError("Wildcard ALLOWED_ORIGINS ('*') is forbidden in production")
            has_s3_key = bool(self.s3_access_key_id.strip())
            has_s3_secret = bool(self.s3_secret_access_key.strip())
            if self.object_store_backend == "s3" and (has_s3_key != has_s3_secret):
                raise ValueError(
                    "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must both be set or both omitted "
                    "for IAM role credentials when OBJECT_STORE_BACKEND=s3"
                )
            callback_base = self.render_callback_base_url.strip()
            callback_secret = self.render_callback_secret.strip()
            if callback_secret and len(callback_secret) < 32:
                raise ValueError(
                    "RENDER_CALLBACK_SECRET must be at least 32 characters in production"
                )
            if callback_base:
                if not callback_base.startswith("https://"):
                    raise ValueError("RENDER_CALLBACK_BASE_URL must use https:// in production")
                if len(callback_secret) < 32:
                    raise ValueError(
                        "RENDER_CALLBACK_SECRET must be at least 32 characters in production "
                        "when RENDER_CALLBACK_BASE_URL is set"
                    )
            if self.render_backend == "aws_sfn_lambda":
                if not self.render_aws_state_machine_arn.strip():
                    raise ValueError(
                        "RENDER_AWS_STATE_MACHINE_ARN is required in production when "
                        "RENDER_BACKEND=aws_sfn_lambda"
                    )
                if not self.render_aws_staging_bucket.strip():
                    raise ValueError(
                        "RENDER_AWS_STAGING_BUCKET is required in production when "
                        "RENDER_BACKEND=aws_sfn_lambda"
                    )
                has_render_key = bool(self.render_aws_access_key_id.strip())
                has_render_secret = bool(self.render_aws_secret_access_key.strip())
                if has_render_key != has_render_secret:
                    raise ValueError(
                        "RENDER_AWS_ACCESS_KEY_ID and RENDER_AWS_SECRET_ACCESS_KEY must both be "
                        "set or both omitted for IAM role credentials"
                    )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
