from __future__ import annotations

import base64
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from trip_api.config import Settings
from trip_api.db import (
    Base,
    IngestJob,
    Project,
    RateLimitRecord,
    RenderJob,
    User,
    create_db_engine,
    create_session_factory,
)
from trip_api.main import create_app
from trip_api.rate_limit import check_rate_limit
from trip_api.security import (
    SsrfBlockedError,
    decrypt_secret,
    encrypt_secret,
    generate_opaque_token,
    generate_pkce_pair,
    hash_password,
    hash_session_token,
    validate_outbound_url,
    verify_csrf_token,
    verify_password,
)
from trip_api.storage import InvalidRangeError, LocalObjectStore, S3ObjectStore, parse_byte_range

if TYPE_CHECKING:
    from conftest import HarnessFactory


def _make_s3_client_error(
    code: str,
    *,
    message: str = "Error",
    http_status: int | None = None,
    op: str = "GetObject",
) -> ClientError:
    payload: dict[str, Any] = {"Error": {"Code": code, "Message": message}}
    if http_status is not None:
        payload["ResponseMetadata"] = {"HTTPStatusCode": http_status}
    return ClientError(payload, op)  # type: ignore[arg-type]


def test_production_guard_blocks_dev_auth_and_default_secrets() -> None:
    valid_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    prod_db_url = "postgresql+psycopg://trip_app:strong-prod-secret@db:5432/trip_animator"

    with pytest.raises(ValueError, match="DEV_AUTH_ENABLED"):
        Settings(
            app_env="production",
            dev_auth_enabled=True,
            database_url=prod_db_url,
            session_secret="x" * 40,
            token_encryption_key=valid_key,
        )

    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=prod_db_url,
            session_secret="x" * 40,
            token_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        )

    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=(
                "postgresql+psycopg://trip_app:"
                "dev-postgres-password-change-me@localhost:5432/trip_animator"
            ),
            session_secret="x" * 40,
            token_encryption_key=valid_key,
        )

    with pytest.raises(ValueError, match="S3_SECRET_ACCESS_KEY"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=prod_db_url,
            session_secret="x" * 40,
            token_encryption_key=valid_key,
            object_store_backend="s3",
            s3_access_key_id="minioadmin",
            s3_secret_access_key="minioadmin-change-me",
        )

    with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=prod_db_url,
            session_secret="x" * 40,
            token_encryption_key=valid_key,
            ALLOWED_ORIGINS="https://app.example.com,*",
        )


def test_settings_validators_and_security_edge_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    prod_db_url = "postgresql+psycopg://trip_app:strong-prod-secret@db:5432/trip_animator"

    cfg = Settings(
        app_env="test",
        strava_webhook_subscription_id="",
        MAP_TILE_ALLOWLIST="https://tiles.openfreemap.org/styles, http://TILE.EXAMPLE.COM:8080/v1 ",
    )
    assert cfg.strava_webhook_subscription_id is None
    assert cfg.map_tile_hosts == frozenset({"tiles.openfreemap.org", "tile.example.com"})

    with pytest.raises(ValueError, match="valid URL-safe base64"):
        Settings(app_env="production", session_secret="x" * 40, token_encryption_key="A")

    with pytest.raises(ValueError, match="exactly 32 bytes"):
        Settings(
            app_env="production",
            session_secret="x" * 40,
            token_encryption_key=base64.urlsafe_b64encode(b"short").decode("ascii"),
        )

    with pytest.raises(ValueError, match="SESSION_SECRET must be at least 32"):
        Settings(app_env="production", session_secret="short", token_encryption_key=valid_key)

    with pytest.raises(ValueError, match="Default development SESSION_SECRET"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=prod_db_url,
            session_secret="dev-only-" + ("x" * 32),
            token_encryption_key=valid_key,
        )

    with pytest.raises(ValueError, match="at least 12"):
        hash_password("too-short")
    with pytest.raises(ValueError, match="Unsupported encrypted secret version"):
        decrypt_secret("v2:abc", b"k" * 32)
    with pytest.raises(ValueError, match="Malformed encrypted payload"):
        decrypt_secret("v1:" + base64.urlsafe_b64encode(b"tiny").decode("ascii"), b"k" * 32)
    with pytest.raises(SsrfBlockedError, match="credentials"):
        validate_outbound_url(
            "https://user:pass@www.strava.com/api",
            allowed_hosts=frozenset({"www.strava.com"}),
            resolve_dns=False,
        )

    def _gai_fail(*_args: Any, **_kwargs: Any) -> Any:
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", _gai_fail)
    with pytest.raises(SsrfBlockedError, match="DNS resolution failed"):
        validate_outbound_url(
            "https://www.strava.com/api",
            allowed_hosts=frozenset({"www.strava.com"}),
            resolve_dns=True,
        )

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.40", 443))],
    )
    with pytest.raises(SsrfBlockedError, match="restricted IP"):
        validate_outbound_url(
            "https://www.strava.com/api",
            allowed_hosts=frozenset({"www.strava.com"}),
            resolve_dns=True,
        )


def test_argon2_csrf_pkce_and_aead_roundtrip() -> None:
    pw_hash = hash_password("correct-horse-battery-staple")
    assert verify_password(pw_hash, "correct-horse-battery-staple") is True
    assert verify_password(pw_hash, "wrong-password") is False

    token = generate_opaque_token()
    assert hash_session_token(token, "s" * 32) == hash_session_token(token, "s" * 32)
    assert verify_csrf_token("abc123", "abc123") is True
    assert verify_csrf_token("abc123", "abc124") is False

    verifier, challenge = generate_pkce_pair()
    assert len(verifier) >= 43
    assert "=" not in challenge

    key = b"0123456789abcdef0123456789abcdef"
    enc = encrypt_secret("strava-refresh-secret", key)
    assert enc.startswith("v1:")
    assert decrypt_secret(enc, key) == "strava-refresh-secret"


def test_ssrf_blocks_unlisted_and_private_ips() -> None:
    allowed = frozenset({"www.strava.com", "tiles.openfreemap.org"})
    with pytest.raises(SsrfBlockedError):
        validate_outbound_url(
            "http://www.strava.com/api/v3", allowed_hosts=allowed, resolve_dns=False
        )
    with pytest.raises(SsrfBlockedError):
        validate_outbound_url(
            "https://169.254.169.254/latest",
            allowed_hosts=frozenset({"169.254.169.254"}),
            resolve_dns=False,
        )
    with pytest.raises(SsrfBlockedError):
        validate_outbound_url(
            "https://evil.example.com/hook", allowed_hosts=allowed, resolve_dns=False
        )
    assert (
        validate_outbound_url(
            "https://tiles.openfreemap.org/styles/liberty",
            allowed_hosts=allowed,
            resolve_dns=False,
        )
        == "https://tiles.openfreemap.org/styles/liberty"
    )


def test_local_object_store_blocks_path_traversal(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_bytes("bronze/test.gpx", b"<gpx/>")
    assert store.get_bytes("bronze/test.gpx") == b"<gpx/>"
    with pytest.raises(ValueError):
        store.put_bytes("../escape.txt", b"bad")
    store.delete("bronze/never-existed.gpx")


def test_parse_byte_range_and_local_store_streaming_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert parse_byte_range("items=0-5", 100) is None
    assert parse_byte_range("bytes=0-1,2-3", 100) is None
    assert parse_byte_range("bytes=abc-def", 100) is None
    assert parse_byte_range("bytes=-abc", 100) is None
    assert parse_byte_range("bytes=0-xyz", 100) is None

    assert parse_byte_range("bytes=0-9", 26) == (0, 9)
    assert parse_byte_range("bytes=10-", 26) == (10, 25)
    assert parse_byte_range("bytes=-5", 26) == (21, 25)
    assert parse_byte_range("bytes=-999", 26) == (0, 25)
    assert parse_byte_range("bytes=20-999", 26) == (20, 25)

    for bad_range, size in (
        ("bytes=-0", 26),
        ("bytes=999-", 26),
        ("bytes=10-5", 26),
        ("bytes=0-0", 0),
        ("bytes=-5", 0),
    ):
        with pytest.raises(InvalidRangeError):
            parse_byte_range(bad_range, size)

    store = LocalObjectStore(tmp_path)
    store.put_bytes("empty.bin", b"")
    empty_stream = store.open_stream("empty.bin")
    assert empty_stream.total_size == 0
    assert empty_stream.content_length == 0
    assert list(empty_stream.chunks) == []

    store.put_bytes("sample.bin", b"0123456789ABCDEF")
    suffix_stream = store.open_stream("sample.bin", range_header="bytes=-5", chunk_size=2)
    assert suffix_stream.is_partial is True
    assert suffix_stream.start == 11
    assert suffix_stream.end == 15
    assert b"".join(suffix_stream.chunks) == b"BCDEF"

    open_end_stream = store.open_stream("sample.bin", range_header="bytes=10-", chunk_size=4)
    assert open_end_stream.start == 10
    assert open_end_stream.end == 15
    assert b"".join(open_end_stream.chunks) == b"ABCDEF"

    orig_open = Path.open

    class _ExplodingHandle:
        def __init__(self, real_fh: Any) -> None:
            self._real = real_fh
            self.closed = False

        def seek(self, _offset: int) -> int:
            raise OSError("seek failed")

        def close(self) -> None:
            self.closed = True
            self._real.close()

    handles: list[_ExplodingHandle] = []

    def _patched_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        fh = _ExplodingHandle(orig_open(self, *args, **kwargs))
        handles.append(fh)
        return fh

    monkeypatch.setattr(Path, "open", _patched_open)
    with pytest.raises(OSError, match="seek failed"):
        store.open_stream("sample.bin", range_header="bytes=5-10")
    assert handles and handles[0].closed is True


def test_health_and_security_headers() -> None:
    app = create_app(Settings(app_env="test", dev_auth_enabled=False))
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


def test_ready_checks_db_connectivity(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'ready.db'}"
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    sf = create_session_factory(engine)
    app = create_app(
        Settings(app_env="test", dev_auth_enabled=False, database_url=db_url),
        engine=engine,
        session_factory=sf,
    )
    client = TestClient(app)
    ok_resp = client.get("/ready")
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"status": "ready", "service": "trip-api", "database": "ok"}

    engine.dispose()
    bad_engine = create_db_engine("sqlite:////nonexistent_dir_xyz/missing.db")
    bad_app = create_app(
        Settings(app_env="test", dev_auth_enabled=False),
        engine=bad_engine,
        session_factory=create_session_factory(bad_engine),
    )
    bad_client = TestClient(bad_app)
    fail_resp = bad_client.get("/ready")
    assert fail_resp.status_code == 503
    assert fail_resp.json()["status"] == "not_ready"
    bad_engine.dispose()


def test_metrics_prometheus_exposition_gating_and_zero_pii(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'metrics.db'}"
    engine = create_db_engine(db_url)
    Base.metadata.create_all(engine)
    sf = create_session_factory(engine)
    try:
        with sf() as db:
            user = User(
                email="secret-pii@example.com",
                display_name="Secret Rider",
                password_hash="argon2-hash",
                is_active=True,
            )
            db.add(user)
            db.flush()
            proj = Project(
                owner_user_id=user.id,
                title="Confidential Tour Title",
                revision=1,
                settings_json={},
            )
            db.add(proj)
            db.flush()
            db.add(
                RenderJob(
                    project_id=proj.id,
                    owner_user_id=user.id,
                    idempotency_key="metrics-job-1",
                    idempotency_hash="m" * 64,
                    spec_json={"version": 2},
                    status="succeeded",
                    progress_pct=100,
                )
            )
            db.commit()

        gated_app = create_app(
            Settings(
                app_env="production",
                dev_auth_enabled=False,
                smtp_host="smtp.example.com",
                database_url=db_url,
                session_secret="prod-session-secret-minimum-32-characters-long",
                token_encryption_key=base64.urlsafe_b64encode(b"m" * 32).decode("ascii"),
                object_store_backend="local",
                metrics_token="super-secret-metrics-token",
            ),
            engine=engine,
            session_factory=sf,
        )
        client = TestClient(gated_app)
        assert client.get("/metrics").status_code == 401
        assert (
            client.get("/metrics", headers={"Authorization": "Bearer wrong-token"}).status_code
            == 403
        )
        ok = client.get(
            "/metrics",
            headers={"Authorization": "Bearer super-secret-metrics-token"},
        )
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("text/plain")
        body = ok.text
        assert "trip_api_up 1" in body
        assert "trip_db_up 1" in body
        assert "trip_projects_total 1" in body
        assert 'trip_render_jobs_total{status="queued"} 0' in body
        assert 'trip_render_jobs_total{status="succeeded"} 1' in body
        metric_name = "trip_render_queue_oldest_age_seconds"
        assert f"{metric_name} 0" in body.splitlines()
        assert "secret-pii@example.com" not in body
        assert "Confidential Tour Title" not in body

        with sf() as db:
            job = db.scalars(select(RenderJob)).one()
            job.created_at = datetime.now(UTC) - timedelta(hours=2)
            db.add_all(
                [
                    RenderJob(
                        project_id=job.project_id,
                        owner_user_id=job.owner_user_id,
                        idempotency_key=f"queued-{age}",
                        idempotency_hash="m" * 64,
                        spec_json={"version": 2},
                        status="queued",
                        created_at=datetime.now(UTC) - timedelta(seconds=age),
                    )
                    for age in (60, 120)
                ]
            )
            db.commit()

        queued = client.get(
            "/metrics", headers={"Authorization": "Bearer super-secret-metrics-token"}
        )
        assert queued.status_code == 200
        [age_value] = [
            line.split()[1]
            for line in queued.text.splitlines()
            if line.startswith(f"{metric_name} ")
        ]
        assert 120 <= float(age_value) < 150

        assert "\ntrip_ingest_queue_depth 0\n" in body
        assert "\ntrip_ingest_queue_oldest_age_seconds 0\n" in body
        with sf() as db:
            now = datetime.now(UTC)
            db.add_all(
                IngestJob(
                    event_type="create",
                    external_owner_id="private-owner",
                    external_object_id=object_id,
                    status=job_status,
                    created_at=now - timedelta(seconds=age),
                )
                for object_id, job_status, age in (
                    (1, "queued", 60),
                    (2, "queued", 120),
                    (3, "running", 3600),
                    (4, "failed", 7200),
                )
            )
            db.commit()
        populated = client.get(
            "/metrics", headers={"Authorization": "Bearer super-secret-metrics-token"}
        )
        values = dict(
            line.split(" ", 1)
            for line in populated.text.splitlines()
            if line.startswith("trip_ingest_")
        )
        assert values["trip_ingest_queue_depth"] == "2"
        assert 120 <= float(values["trip_ingest_queue_oldest_age_seconds"]) < 130
        assert "private-owner" not in populated.text

        prod_no_token = create_app(
            Settings(
                app_env="production",
                dev_auth_enabled=False,
                smtp_host="smtp.example.com",
                database_url=db_url,
                session_secret="prod-session-secret-minimum-32-characters-long",
                token_encryption_key=base64.urlsafe_b64encode(b"m" * 32).decode("ascii"),
                object_store_backend="local",
                metrics_token="",
            ),
            engine=engine,
            session_factory=sf,
        )
        assert TestClient(prod_no_token).get("/metrics").status_code == 403

        def _broken_sf() -> Any:
            class _BrokenCtx:
                def __enter__(self) -> Any:
                    raise SQLAlchemyError("db outage")

                def __exit__(self, *_args: Any) -> None:
                    return None

            return _BrokenCtx()

        broken_metrics_app = create_app(
            Settings(app_env="test", dev_auth_enabled=False, database_url=db_url),
            engine=engine,
            session_factory=_broken_sf,  # type: ignore[arg-type]
        )
        down_resp = TestClient(broken_metrics_app).get("/metrics")
        assert down_resp.status_code == 200
        assert "trip_db_up 0" in down_resp.text
        assert not any(line.startswith(f"{metric_name} ") for line in down_resp.text.splitlines())
        assert "\ntrip_ingest_" not in down_resp.text
    finally:
        engine.dispose()


def test_http_operational_events_use_route_template_and_only_report_server_errors(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_harness().app
    events: list[tuple[str | None, int]] = []

    def record(route: str | None, status: int) -> None:
        events.append((route, status))

    monkeypatch.setattr("trip_api.main.record_api_failure", record)

    @app.get("/_test_failure/{item}", status_code=503)
    def _unavailable(item: str) -> dict[str, str]:
        return {"item": item}

    @app.get("/_test_exception/{item}")
    def _exception(item: str) -> None:
        raise RuntimeError(item)

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health").status_code == 200
    assert client.get("/_test_missing").status_code == 404
    assert client.get("/_test_failure/private-item?trace=private-value").status_code == 503
    assert client.get("/_test_exception/private-item?trace=private-value").status_code == 500
    assert events == [("/_test_failure/{item}", 503), ("/_test_exception/{item}", 500)]


def test_http_metrics_use_route_templates_and_count_failures_once(
    make_harness: HarnessFactory,
) -> None:
    h = make_harness()

    @h.app.get("/_test_metrics/{item}")
    def _item(item: int) -> dict[str, int]:
        if item == 43:
            raise RuntimeError("private diagnostic")
        return {"item": item}

    with TestClient(h.app, raise_server_exceptions=False) as client:
        assert client.get("/_test_metrics/424242?key=private-query").status_code == 200
        assert client.get("/_test_metrics/private-invalid").status_code == 422
        assert client.get("/_test_metrics/43").status_code == 500
        assert client.get("/private-missing").status_code == 404
        assert client.get("/metrics").status_code == 200
        scrape = client.get("/metrics")

    assert scrape.status_code == 200
    from prometheus_client.parser import text_string_to_metric_families

    samples = {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(scrape.text)
        for sample in family.samples
    }
    counts = {
        labels: value
        for (name, labels), value in samples.items()
        if name == "trip_api_http_request_duration_seconds_count"
    }
    assert counts == {
        (("route", route), ("status_class", status_class)): 1
        for route, status_class in (
            ("/_test_metrics/{item}", "2xx"),
            ("/_test_metrics/{item}", "4xx"),
            ("/_test_metrics/{item}", "5xx"),
            ("unmatched", "4xx"),
        )
    }
    for labels in counts:
        assert samples["trip_api_http_request_duration_seconds_sum", labels] > 0
        assert (
            samples["trip_api_http_request_duration_seconds_bucket", (("le", "+Inf"), *labels)] == 1
        )
    for private in (
        "424242",
        "private-query",
        "private-invalid",
        "private-missing",
        "private diagnostic",
    ):
        assert private not in scrape.text


def test_unhandled_exception_handler_and_rate_limiter_branches(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness()

    @h.app.get("/_test_boom")
    def _boom() -> dict[str, str]:
        raise RuntimeError("sensitive internal stack trace")

    safe_client = TestClient(h.app, raise_server_exceptions=False)
    err = safe_client.get("/_test_boom")
    assert err.status_code == 500
    assert err.json() == {
        "type": "about:blank",
        "title": "Internal server error",
        "detail": "Internal server error",
        "status": 500,
    }
    assert "sensitive internal stack trace" not in err.text

    with h.session_factory() as db:
        with pytest.raises(ValueError, match="cost must be >= 1"):
            check_rate_limit(db, bucket_key="k", limit=5, window_seconds=60, cost=0)

        check_rate_limit(db, bucket_key="disabled", limit=0, window_seconds=60, cost=1)

        past = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=120)
        db.add(
            RateLimitRecord(
                bucket_key="reset-bucket",
                window_start=past,
                request_count=5,
                updated_at=past,
            )
        )
        db.commit()
        check_rate_limit(db, bucket_key="reset-bucket", limit=5, window_seconds=60, cost=2)
        refreshed = db.query(RateLimitRecord).filter_by(bucket_key="reset-bucket").one()
        assert refreshed.request_count == 2

        refreshed.window_start = datetime.now(UTC) - timedelta(seconds=120)
        db.commit()
        with pytest.raises(HTTPException) as exc_info:
            check_rate_limit(db, bucket_key="reset-bucket", limit=5, window_seconds=60, cost=6)
        assert exc_info.value.status_code == 429

        orig_commit = db.commit
        race_state = {"triggered": False}

        def _racing_commit() -> None:
            if not race_state["triggered"]:
                race_state["triggered"] = True
                with h.session_factory() as other_db:
                    now = datetime.now(UTC)
                    other_db.add(
                        RateLimitRecord(
                            bucket_key="race-bucket",
                            window_start=now,
                            request_count=1,
                            updated_at=now,
                        )
                    )
                    other_db.commit()
                raise IntegrityError("insert race", {}, Exception("unique"))
            orig_commit()

        monkeypatch.setattr(db, "commit", _racing_commit)
        check_rate_limit(db, bucket_key="race-bucket", limit=3, window_seconds=60, cost=1)
        monkeypatch.undo()
        row = db.query(RateLimitRecord).filter_by(bucket_key="race-bucket").one()
        assert row.request_count == 2


def test_production_s3_allows_iam_role_and_rejects_partial_keys() -> None:
    valid_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    prod_db_url = "postgresql+psycopg://trip_app:strong-prod-secret@db:5432/trip_animator"

    cfg = Settings(
        app_env="production",
        dev_auth_enabled=False,
        smtp_host="smtp.example.com",
        database_url=prod_db_url,
        session_secret="x" * 40,
        token_encryption_key=valid_key,
        object_store_backend="s3",
        s3_endpoint_url="https://s3.eu-central-1.amazonaws.com",
        s3_access_key_id="",
        s3_secret_access_key="",
        s3_bucket="prod-objects",
        s3_region="eu-central-1",
    )
    assert cfg.s3_access_key_id == ""
    assert cfg.s3_secret_access_key == ""

    with pytest.raises(ValueError, match="S3_ACCESS_KEY_ID"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=prod_db_url,
            session_secret="x" * 40,
            token_encryption_key=valid_key,
            object_store_backend="s3",
            s3_access_key_id="AKIAONLYKEY",
            s3_secret_access_key="",
        )

    with pytest.raises(ValueError, match="S3_SECRET_ACCESS_KEY"):
        Settings(
            app_env="production",
            dev_auth_enabled=False,
            smtp_host="smtp.example.com",
            database_url=prod_db_url,
            session_secret="x" * 40,
            token_encryption_key=valid_key,
            object_store_backend="s3",
            s3_access_key_id="",
            s3_secret_access_key="only-secret-no-key",
        )


def test_s3_object_store_normalizes_empty_creds_and_maps_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls: list[dict[str, Any]] = []
    put_calls: list[dict[str, Any]] = []

    class FakeBody:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.closed = False
            self.read_called = False

        def read(self) -> bytes:
            self.read_called = True
            return self._payload

        def iter_chunks(self, chunk_size: int = 65536) -> Any:
            for offset in range(0, len(self._payload), chunk_size):
                yield self._payload[offset : offset + chunk_size]

        def close(self) -> None:
            self.closed = True

    class FakeS3Client:
        def __init__(self) -> None:
            self.last_body: FakeBody | None = None

        def put_object(self, **kwargs: Any) -> None:
            put_calls.append(kwargs)

        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            key = str(kwargs["Key"])
            if key == "missing-nokey.mp4":
                raise _make_s3_client_error("NoSuchKey", message="Not found.")
            if key == "missing-404.mp4":
                raise _make_s3_client_error("404", http_status=404)
            if key == "boom-500.mp4":
                raise _make_s3_client_error("InternalError", http_status=500)
            if key == "invalid-range.mp4" and "Range" in kwargs:
                raise _make_s3_client_error("InvalidRange", http_status=416)

            data = b"0123456789ABCDEF"
            if "Range" in kwargs:
                rng = str(kwargs["Range"]).removeprefix("bytes=")
                start_s, end_s = rng.split("-", 1)
                sliced = data[int(start_s) : int(end_s) + 1]
                self.last_body = FakeBody(sliced)
                return {"Body": self.last_body, "ContentLength": len(sliced)}
            self.last_body = FakeBody(data)
            return {"Body": self.last_body, "ContentLength": len(data)}

        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            key = str(kwargs["Key"])
            if key == "missing-nokey.mp4":
                raise _make_s3_client_error("404", message="Not Found", op="HeadObject")
            if key == "boom-head.mp4":
                raise _make_s3_client_error("InternalError", http_status=500, op="HeadObject")
            return {"ContentLength": 16}

        def delete_object(self, **kwargs: Any) -> None:
            key = str(kwargs["Key"])
            if key == "missing-nokey.mp4":
                raise _make_s3_client_error("NoSuchKey", op="DeleteObject")
            if key == "boom-delete.mp4":
                raise _make_s3_client_error("InternalError", http_status=500, op="DeleteObject")

    fake_client = FakeS3Client()

    def fake_boto3_client(service_name: str, **kwargs: Any) -> FakeS3Client:
        assert service_name == "s3"
        client_calls.append(kwargs)
        return fake_client

    monkeypatch.setattr(boto3, "client", fake_boto3_client)

    store = S3ObjectStore(
        endpoint_url="",
        access_key_id="",
        secret_access_key="  ",
        bucket="trip-prod",
        region="",
    )
    assert client_calls == [
        {
            "endpoint_url": None,
            "aws_access_key_id": None,
            "aws_secret_access_key": None,
            "region_name": None,
        }
    ]

    store.put_bytes("video.mp4", b"0123456789ABCDEF", content_type="video/mp4")
    assert put_calls[0]["Bucket"] == "trip-prod"
    assert put_calls[0]["Key"] == "video.mp4"
    assert store.get_bytes("video.mp4") == b"0123456789ABCDEF"
    assert fake_client.last_body is not None and fake_client.last_body.closed is True

    with pytest.raises(FileNotFoundError):
        store.get_bytes("missing-nokey.mp4")
    with pytest.raises(FileNotFoundError):
        store.get_bytes("missing-404.mp4")
    with pytest.raises(ClientError):
        store.get_bytes("boom-500.mp4")

    with pytest.raises(FileNotFoundError):
        store.open_stream("missing-nokey.mp4")
    with pytest.raises(FileNotFoundError):
        store.open_stream("missing-nokey.mp4", range_header="bytes=0-3")
    with pytest.raises(ClientError):
        store.open_stream("boom-head.mp4", range_header="bytes=0-3")
    with pytest.raises(InvalidRangeError):
        store.open_stream("invalid-range.mp4", range_header="bytes=0-3")

    full_stream = store.open_stream("video.mp4", chunk_size=5)
    assert full_stream.total_size == 16
    assert full_stream.content_length == 16
    assert full_stream.is_partial is False
    chunks = list(full_stream.chunks)
    assert chunks == [b"01234", b"56789", b"ABCDE", b"F"]
    assert fake_client.last_body is not None
    assert fake_client.last_body.read_called is False
    assert fake_client.last_body.closed is True

    part_stream = store.open_stream("video.mp4", range_header="bytes=2-7", chunk_size=4)
    assert part_stream.is_partial is True
    assert part_stream.start == 2
    assert part_stream.end == 7
    assert part_stream.content_length == 6
    assert part_stream.total_size == 16
    assert b"".join(part_stream.chunks) == b"234567"

    with pytest.raises(InvalidRangeError):
        store.open_stream("video.mp4", range_header="bytes=100-200")

    store.delete("missing-nokey.mp4")
    with pytest.raises(ClientError):
        store.delete("boom-delete.mp4")


def test_dockerfile_api_does_not_trust_wildcard_forwarded_ips() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    dockerfile_path = repo_root / "infra" / "docker" / "Dockerfile.api"
    content = dockerfile_path.read_text(encoding="utf-8")
    assert '"--forwarded-allow-ips", "*"' not in content
    assert "--forwarded-allow-ips *" not in content
    assert 'FORWARDED_ALLOW_IPS="127.0.0.1,::1"' in content
