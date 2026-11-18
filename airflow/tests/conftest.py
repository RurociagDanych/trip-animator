from __future__ import annotations

import base64
import socket
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from trip_api.config import Settings
from trip_api.db import Project, Stage, User, create_db_engine, create_session_factory
from trip_api.security import hash_password
from trip_api.services import ingest_parsed_track
from trip_api.storage import LocalObjectStore
from trip_telemetry import parse_gpx_bytes

# Constructed telemetry fixture; coordinates and timestamps are synthetic.
SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk>
    <name>Synthetic Sample Stage</name>
    <trkseg>
      <trkpt lat="0.1000" lon="0.2000">
        <ele>1200.0</ele>
        <time>2000-01-01T08:00:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>132</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1020" lon="0.2020">
        <ele>1240.0</ele>
        <time>2000-01-01T08:02:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>146</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1040" lon="0.2040">
        <ele>1285.0</ele>
        <time>2000-01-01T08:04:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>158</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
    </trkseg>
  </trk>
</gpx>"""


class FakeObjectStore:
    """In-memory ObjectStore fake implementing `.list_keys(prefix)` like S3."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.mtimes: dict[str, datetime] = {}

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        _ = content_type
        clean = key.lstrip("/")
        self.blobs[clean] = data
        self.mtimes[clean] = datetime.now(UTC)

    def get_bytes(self, key: str) -> bytes:
        clean = key.lstrip("/")
        if clean not in self.blobs:
            raise FileNotFoundError(clean)
        return self.blobs[clean]

    def copy(
        self, src_key: str, dst_key: str, *, content_type: str | None = None
    ) -> None:
        _ = content_type
        clean_dst = dst_key.lstrip("/")
        self.blobs[clean_dst] = self.get_bytes(src_key)
        self.mtimes[clean_dst] = datetime.now(UTC)

    def delete(self, key: str) -> None:
        clean = key.lstrip("/")
        self.blobs.pop(clean, None)
        self.mtimes.pop(clean, None)

    def list_keys(self, prefix: str = "") -> list[str]:
        clean = prefix.lstrip("/")
        return sorted(k for k in self.blobs if not clean or k.startswith(clean))

    def list_keys_with_mtime(self, prefix: str = "") -> dict[str, datetime | None]:
        clean = prefix.lstrip("/")
        return {
            k: self.mtimes.get(k)
            for k in sorted(self.blobs)
            if not clean or k.startswith(clean)
        }


def fake_public_getaddrinfo(
    *args: Any, **kwargs: Any
) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
    _ = args, kwargs
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.69", 443))]


@dataclass
class AirflowTaskHarness:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    store: LocalObjectStore
    db_path: Path
    gold_dir: Path

    def create_user(self, email: str, *, is_active: bool = True) -> User:
        with self.session_factory() as db:
            user = User(
                email=email.lower(),
                display_name=email.split("@", 1)[0],
                password_hash=hash_password("correct-horse-battery-staple"),
                is_active=is_active,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            return user

    def create_project(self, user_id: uuid.UUID, title: str = "Synthetic Tour") -> Project:
        with self.session_factory() as db:
            proj = Project(owner_user_id=user_id, title=title, settings_json={})
            db.add(proj)
            db.commit()
            db.refresh(proj)
            return proj

    def ingest_sample_stage(
        self,
        project: Project,
        *,
        name: str = "Synthetic Sample Stage",
        gpx_bytes: bytes = SAMPLE_GPX,
        strava_activity_id: int | None = None,
    ) -> Stage:
        parsed = parse_gpx_bytes(gpx_bytes)
        with self.session_factory() as db:
            db_proj = db.get(Project, project.id)
            assert db_proj is not None
            return ingest_parsed_track(
                db=db,
                store=self.store,
                project=db_proj,
                parsed=parsed,
                raw_bronze_bytes=gpx_bytes,
                bronze_ext="gpx",
                bronze_content_type="application/gpx+xml",
                source_type="strava" if strava_activity_id is not None else "gpx",
                stage_name=name,
                day_index=1,
                strava_activity_id=strava_activity_id,
            )


HarnessFactory = Callable[..., AirflowTaskHarness]


@pytest.fixture
def make_airflow_harness(tmp_path: Path, monkeypatch: Any) -> Iterator[HarnessFactory]:
    created: list[AirflowTaskHarness] = []
    repo_root = Path(__file__).resolve().parents[2]
    api_dir = repo_root / "apps" / "api"

    def _factory(**setting_overrides: Any) -> AirflowTaskHarness:
        idx = len(created)
        db_path = tmp_path / f"airflow_test_{idx}.db"
        store_dir = tmp_path / f"objects_{idx}"
        gold_dir = tmp_path / f"gold_{idx}"
        db_url = f"sqlite:///{db_path}"

        monkeypatch.setenv("DATABASE_URL", db_url)
        alembic_cfg = AlembicConfig(str(api_dir / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(api_dir / "alembic"))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)
        alembic_command.upgrade(alembic_cfg, "head")

        defaults: dict[str, Any] = {
            "app_env": "test",
            "dev_auth_enabled": False,
            "database_url": db_url,
            "session_secret": "test-session-secret-minimum-32-characters-long",
            "token_encryption_key": base64.urlsafe_b64encode(b"0123456789abcdef" * 2).decode(
                "ascii"
            ),
            "local_object_store_dir": str(store_dir),
            "strava_client_id": "12345",
            "strava_client_secret": "strava-secret-xyz",
        }
        defaults.update(setting_overrides)
        cfg = Settings(**defaults)
        engine = create_db_engine(cfg.database_url)
        sf = create_session_factory(engine)
        store = LocalObjectStore(store_dir)
        harness = AirflowTaskHarness(
            settings=cfg,
            engine=engine,
            session_factory=sf,
            store=store,
            db_path=db_path,
            gold_dir=gold_dir,
        )
        created.append(harness)
        return harness

    yield _factory

    for h in created:
        h.engine.dispose()


@pytest.fixture
def airflow_harness(make_airflow_harness: HarnessFactory) -> AirflowTaskHarness:
    return make_airflow_harness()
