"""Render worker configuration."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Typed environment settings for the deterministic Playwright + FFmpeg render worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = (
        "postgresql+psycopg://trip_app:dev-postgres-password-change-me@localhost:5432/trip_animator"
    )
    render_base_url: str = "http://localhost:5173"
    map_tile_allowlist_raw: str = Field(
        default="https://tiles.openfreemap.org",
        alias="MAP_TILE_ALLOWLIST",
    )

    object_store_backend: Literal["local", "s3"] = "local"
    local_object_store_dir: str = "./data/objects"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "trip-animator"
    s3_region: str = "us-east-1"

    worker_concurrency: int = Field(default=1, ge=1, le=32, alias="WORKER_CONCURRENCY")
    tile_cache_dir: str | None = None
    tile_cache_max_bytes: int = Field(default=512 * 1024 * 1024, ge=0)
    chunk_threshold_frames: int = Field(default=1800, ge=1)
    chunk_size_frames: int = Field(default=300, ge=1)
    chunk_preroll_frames: int = Field(default=15, ge=0)

    lease_seconds: int = Field(default=120, ge=10, le=3600)
    lease_heartbeat_interval_seconds: float = Field(default=10.0, gt=0.0, le=600.0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    poll_interval_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    lease_renew_interval_frames: int = Field(default=5, ge=1, le=300)
    output_ttl_hours: int = Field(default=168, ge=1, le=8760)
    frame_format: Literal["png", "jpeg"] = "jpeg"
    render_ready_timeout_ms: int = Field(default=30_000, ge=1000, le=300_000)
    render_frame_timeout_ms: int = Field(default=60_000, ge=500, le=120_000)
    ffmpeg_timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)
    ffprobe_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_manifest_points_per_stage: int = Field(default=12_000, ge=100, le=500_000)
    max_total_manifest_points: int = Field(default=36_000, ge=500, le=1_000_000)
    chromium_no_sandbox: bool = False

    @property
    def resolved_tile_cache_dir(self) -> Path | None:
        """Return resolved Path for tile_cache_dir when configured and non-empty."""
        if self.tile_cache_dir is None:
            return None
        cleaned = self.tile_cache_dir.strip()
        return Path(cleaned) if cleaned else None

    @property
    def psycopg_dsn(self) -> str:
        """Return a libpq-compatible PostgreSQL connection string for psycopg."""
        if self.database_url.startswith("postgresql+psycopg://"):
            return "postgresql://" + self.database_url.removeprefix("postgresql+psycopg://")
        return self.database_url

    @property
    def allowed_external_hosts(self) -> frozenset[str]:
        hosts: set[str] = set()
        for item in self.map_tile_allowlist_raw.split(","):
            cleaned = item.strip()
            if not cleaned:
                continue
            # Handle bare IPv6 literals (with optional path) before URL parsing.
            if "://" not in cleaned and not cleaned.startswith("[") and cleaned.count(":") > 1:
                bare_host = cleaned.split("/", 1)[0]
                try:
                    hosts.add(ipaddress.ip_address(bare_host).compressed.lower())
                    continue
                except ValueError:
                    pass
            candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
            try:
                hostname = urlparse(candidate).hostname
            except ValueError:
                hostname = None
            if hostname:
                normalized = hostname.strip("[]").lower()
                try:
                    normalized = ipaddress.ip_address(normalized).compressed.lower()
                except ValueError:
                    pass
                hosts.add(normalized)
        return frozenset(hosts)
