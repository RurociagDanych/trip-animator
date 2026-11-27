from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from trip_api.config import Settings
from trip_api.db import (
    Base,
    CreditLedgerEntry,
    User,
    create_db_engine,
    create_session_factory,
)
from trip_api.main import create_app
from trip_api.security import hash_password
from trip_object_store import LocalObjectStore

_DEFAULT_PW = "correct-horse-battery-staple"

SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="pytest" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
  <trk>
    <name>Synthetic Sample Stage</name>
    <trkseg>
      <trkpt lat="0.1000" lon="0.2000">
        <ele>1000.0</ele>
        <time>2000-01-01T08:00:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>130</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1015" lon="0.2015">
        <ele>1030.0</ele>
        <time>2000-01-01T08:01:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>145</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1030" lon="0.2030">
        <ele>1060.0</ele>
        <time>2000-01-01T08:02:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>152</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
      <trkpt lat="0.1045" lon="0.2045">
        <ele>1085.0</ele>
        <time>2000-01-01T08:03:00Z</time>
        <extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>160</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions>
      </trkpt>
    </trkseg>
  </trk>
</gpx>"""


@dataclass
class AppHarness:
    app: FastAPI
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    store: LocalObjectStore
    db_path: Path

    def create_user(
        self,
        email: str,
        password: str | None = None,
        *,
        verified: bool = True,
        initial_credits: int = 10_000,
    ) -> User:
        pw = password or _DEFAULT_PW
        with self.session_factory() as db:
            user = User(
                email=email.lower(),
                display_name=email.split("@", 1)[0],
                password_hash=hash_password(pw),
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
                        note="Test fixture credit seed",
                    )
                )
            db.commit()
            db.refresh(user)
            return user

    def login_client(self, email: str, password: str | None = None) -> tuple[TestClient, str]:
        pw = password or _DEFAULT_PW
        client = TestClient(self.app)
        resp = client.post("/api/v1/auth/login", json={"email": email, "password": pw})
        assert resp.status_code == 200, resp.text
        csrf_token = str(resp.json()["csrf_token"])
        return client, csrf_token


HarnessFactory = Callable[..., AppHarness]


@pytest.fixture
def make_harness(tmp_path: Path) -> Iterator[HarnessFactory]:
    created: list[AppHarness] = []

    def _factory(**setting_overrides: Any) -> AppHarness:
        idx = len(created)
        db_path = tmp_path / f"test_{idx}.db"
        store_dir = tmp_path / f"objects_{idx}"
        defaults: dict[str, Any] = {
            "app_env": "test",
            "dev_auth_enabled": False,
            "database_url": f"sqlite:///{db_path}",
            "session_secret": "test-session-secret-minimum-32-characters-long",
            "token_encryption_key": base64.urlsafe_b64encode(b"0123456789abcdef" * 2).decode(
                "ascii"
            ),
            "local_object_store_dir": str(store_dir),
            "strava_client_id": "12345",
            "strava_client_secret": "strava-secret-xyz",
            "strava_verify_token": "webhook-verify-token-secret",
            "rate_limit_login_max": 5,
            "rate_limit_gpx_max": 10,
            "rate_limit_render_max": 10,
        }
        defaults.update(setting_overrides)
        cfg = Settings(**defaults)
        engine = create_db_engine(cfg.database_url)
        Base.metadata.create_all(engine)
        sf = create_session_factory(engine)
        store = LocalObjectStore(store_dir)
        app = create_app(cfg, engine=engine, session_factory=sf, object_store=store)
        app.state.sse_poll_interval_s = 0.01
        app.state.sse_max_polls = 5
        harness = AppHarness(
            app=app,
            settings=cfg,
            engine=engine,
            session_factory=sf,
            store=store,
            db_path=db_path,
        )
        created.append(harness)
        return harness

    yield _factory

    for h in created:
        h.engine.dispose()


@pytest.fixture
def sample_gpx() -> bytes:
    return SAMPLE_GPX


@pytest.fixture
def harness(make_harness: HarnessFactory) -> AppHarness:
    return make_harness()
