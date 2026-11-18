from __future__ import annotations

import base64
import hashlib
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from trip_api.db import (
    AuditEvent,
    IngestJob,
    OAuthConnection,
    OAuthState,
    Project,
    RenderJob,
    Stage,
)
from trip_api.ingest import _resolve_target_project_for_job, process_pending_ingest_jobs
from trip_api.routers.strava import _parse_strava_epoch
from trip_api.security import decrypt_secret, encrypt_secret
from trip_api.storage import S3ObjectStore

if TYPE_CHECKING:
    from conftest import AppHarness, HarnessFactory


def _fake_public_getaddrinfo(
    *args: Any, **kwargs: Any
) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("151.101.1.69", 443),
        )
    ]


def _seed_strava_connection(
    h: AppHarness,
    user_id: uuid.UUID,
    *,
    athlete_id: str = "555",
    expires_at: datetime | None = None,
) -> None:
    with h.session_factory() as db:
        aad = f"oauth:{user_id}:strava".encode()
        key = h.settings.decoded_encryption_key
        db.add(
            OAuthConnection(
                user_id=user_id,
                provider="strava",
                external_athlete_id=athlete_id,
                access_token_enc=encrypt_secret("acc", key, aad=aad),
                refresh_token_enc=encrypt_secret("ref", key, aad=aad),
                expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=2)),
                scope="read,activity:read_all",
            )
        )
        db.commit()


def test_strava_oauth_pkce_aead_activities_import_and_webhook(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        path = request.url.path
        if path == "/oauth/token":
            body_text = request.content.decode("utf-8")
            assert "code_verifier=" in body_text or "grant_type=refresh_token" in body_text
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                    "refresh_token": "strava-refresh-secret-999",
                    "access_token": "strava-access-secret-111",
                    "scope": "read,activity:read_all",
                    "athlete": {"id": 987654321},
                },
            )
        if path == "/api/v3/athlete/activities":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 424242,
                        "name": "Sample Morning Ride",
                        "sport_type": "Ride",
                        "start_date": "2001-01-02T07:00:00Z",
                        "distance": 45120.5,
                        "total_elevation_gain": 1820.0,
                        "moving_time": 7200,
                        "start_latlng": [0.1, 0.2],
                    }
                ],
            )
        if path == "/api/v3/activities/424242":
            return httpx.Response(
                200,
                json={
                    "id": 424242,
                    "name": "Sample Morning Ride",
                    "start_date": "2001-01-02T07:00:00Z",
                },
            )
        if path == "/api/v3/activities/424242/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.10, 0.20], [0.11, 0.21], [0.12, 0.22]]},
                    "time": {"data": [0, 60, 120]},
                    "altitude": {"data": [1500.0, 1540.0, 1590.0]},
                    "heartrate": {"data": [135, 148, 155]},
                    "cadence": {"data": [82, 85, 88]},
                    "watts": {"data": [220, 245, 260]},
                },
            )
        if path == "/oauth/deauthorize":
            return httpx.Response(200, json={"access_token": "revoked"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    harness.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )

    user = harness.create_user("strava@example.com")
    harness.create_user("other@example.com")
    client, csrf = harness.login_client("strava@example.com")
    other_client, _ = harness.login_client("other@example.com")

    # 1. Connect returns PKCE S256 authorize URL + state
    conn_resp = client.get("/api/v1/strava/connect")
    assert conn_resp.status_code == 200
    auth_url = conn_resp.json()["authorize_url"]
    state = conn_resp.json()["state"]
    qs = parse_qs(urlparse(auth_url).query)
    assert qs["code_challenge_method"] == ["S256"]
    assert len(qs["code_challenge"][0]) >= 43
    assert qs["state"] == [state]

    # 2. Other user cannot hijack callback with Alice's state
    hijack = other_client.get(f"/api/v1/strava/callback?code=abc&state={state}")
    assert hijack.status_code == 400

    # 3. Valid callback exchanges PKCE verifier and stores AEAD-encrypted tokens
    cb = client.get(f"/api/v1/strava/callback?code=auth-code-123&state={state}")
    assert cb.status_code == 200, cb.text
    assert cb.json()["connected"] is True
    assert cb.json()["athlete_id"] == "987654321"

    # Replaying the same state must fail (single-use)
    replay = client.get(f"/api/v1/strava/callback?code=auth-code-123&state={state}")
    assert replay.status_code == 400

    # Verify DB stores AEAD ciphertext, not plaintext
    with harness.session_factory() as db:
        row = db.scalar(select(OAuthConnection).where(OAuthConnection.user_id == user.id))
        assert row is not None
        assert row.access_token_enc.startswith("v1:")
        assert row.refresh_token_enc.startswith("v1:")
        assert "strava-access-secret-111" not in row.access_token_enc
        assert "strava-refresh-secret-999" not in row.refresh_token_enc
        aad = f"oauth:{user.id}:strava".encode()
        assert (
            decrypt_secret(row.access_token_enc, harness.settings.decoded_encryption_key, aad=aad)
            == "strava-access-secret-111"
        )

    # Verify PKCE challenge matches verifier sent to Strava
    token_req = next(r for r in captured_requests if r.url.path == "/oauth/token")
    sent_verifier = parse_qs(token_req.content.decode("utf-8"))["code_verifier"][0]
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(sent_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert expected_challenge == qs["code_challenge"][0]

    # 4. List activities & import Strava streams into a project
    acts = client.get("/api/v1/strava/activities")
    assert acts.status_code == 200
    assert acts.json()[0]["id"] == 424242

    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Strava Import Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    imp = client.post(
        f"/api/v1/projects/{proj_id}/import-strava",
        json={"activity_id": 424242, "day_index": 2},
        headers={"X-CSRF-Token": csrf},
    )
    assert imp.status_code == 201, imp.text
    stage = imp.json()
    assert stage["source_type"] == "strava"
    assert stage["strava_activity_id"] == 424242
    assert stage["day_index"] == 2
    assert stage["point_count"] == 3
    assert stage["elevation_gain_m"] == 90.0

    # 5. Webhook verification & deauthorization cleanup
    bad_wh = client.get(
        "/api/v1/strava/webhook?hub.mode=subscribe&hub.challenge=xyz&hub.verify_token=wrong"
    )
    assert bad_wh.status_code == 403
    ok_wh = client.get(
        "/api/v1/strava/webhook?hub.mode=subscribe&hub.challenge=xyz"
        "&hub.verify_token=webhook-verify-token-secret"
    )
    assert ok_wh.status_code == 200
    assert ok_wh.json() == {"hub.challenge": "xyz"}

    # Unauthenticated or wrong-token POST webhook must be rejected and preserve OAuthConnection
    unauth_wh = client.post(
        "/api/v1/strava/webhook",
        json={
            "object_type": "athlete",
            "aspect_type": "update",
            "owner_id": 987654321,
            "updates": {"authorized": "false"},
        },
    )
    assert unauth_wh.status_code == 403
    wrong_tok_wh = client.post(
        "/api/v1/strava/webhook",
        headers={"X-Strava-Verify-Token": "wrong-token"},
        json={
            "object_type": "athlete",
            "aspect_type": "update",
            "owner_id": 987654321,
            "updates": {"authorized": "false"},
        },
    )
    assert wrong_tok_wh.status_code == 403
    with harness.session_factory() as db:
        assert db.scalar(select(OAuthConnection)) is not None

    deauth_wh = client.post(
        "/api/v1/strava/webhook",
        headers={"X-Strava-Verify-Token": "webhook-verify-token-secret"},
        json={
            "object_type": "athlete",
            "aspect_type": "update",
            "owner_id": 987654321,
            "updates": {"authorized": "false"},
        },
    )
    assert deauth_wh.status_code == 200
    with harness.session_factory() as db:
        assert db.scalar(select(OAuthConnection)) is None

    # 6. Disconnect is idempotent
    disc = client.post("/api/v1/strava/disconnect", headers={"X-CSRF-Token": csrf})
    assert disc.status_code == 200
    assert disc.json()["connected"] is False


def test_strava_oauth_refresh_reconnect_error_paths_and_disconnect_resilience(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)

    # Unconfigured Strava -> 503 on /connect
    unconf = make_harness(strava_client_id="", strava_client_secret="")
    unconf.create_user("no-strava@example.com")
    c_unconf, _ = unconf.login_client("no-strava@example.com")
    assert c_unconf.get("/api/v1/strava/connect").status_code == 503

    h = make_harness()
    user = h.create_user("refresh@example.com")
    client, csrf = h.login_client("refresh@example.com")

    # Unconnected user cannot list activities -> 400
    assert client.get("/api/v1/strava/activities").status_code == 400

    # Callback error & missing parameter validation
    assert client.get("/api/v1/strava/callback?error=access_denied").status_code == 400
    assert client.get("/api/v1/strava/callback?code=only-code").status_code == 400

    # Upstream token exchange errors -> 502
    state1 = str(client.get("/api/v1/strava/connect").json()["state"])
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(500)),
        follow_redirects=False,
    )
    assert client.get(f"/api/v1/strava/callback?code=c&state={state1}").status_code == 502

    state2 = str(client.get("/api/v1/strava/connect").json()["state"])
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                json={"access_token": "a", "refresh_token": "r", "expires_at": 2000000000},
            )
        ),
        follow_redirects=False,
    )
    assert client.get(f"/api/v1/strava/callback?code=c&state={state2}").status_code == 502

    # Seed expired connection and test refresh failure -> 502, then refresh success -> 200
    _seed_strava_connection(
        h,
        user.id,
        athlete_id="666",
        expires_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(500)),
        follow_redirects=False,
    )
    assert client.get("/api/v1/strava/activities").status_code == 502

    def refresh_ok_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-access-tok",
                    "refresh_token": "fresh-refresh-tok",
                    "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                },
            )
        if request.url.path == "/api/v3/athlete/activities":
            assert request.headers["authorization"] == "Bearer fresh-access-tok"
            return httpx.Response(200, json=[])
        if request.url.path == "/oauth/deauthorize":
            raise httpx.ConnectError("upstream down")
        return httpx.Response(404)

    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(refresh_ok_handler),
        follow_redirects=False,
    )
    acts_ok = client.get("/api/v1/strava/activities")
    assert acts_ok.status_code == 200
    assert acts_ok.json() == []

    # Verify refreshed encrypted tokens persisted
    with h.session_factory() as db:
        conn = db.scalar(select(OAuthConnection).where(OAuthConnection.user_id == user.id))
        assert conn is not None
        aad = f"oauth:{user.id}:strava".encode()
        assert (
            decrypt_secret(conn.access_token_enc, h.settings.decoded_encryption_key, aad=aad)
            == "fresh-access-tok"
        )

    # Re-connecting overwrites existing OAuthConnection row cleanly
    state3 = str(client.get("/api/v1/strava/connect").json()["state"])
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: (
                httpx.Response(
                    200,
                    json={
                        "access_token": "reconnected-access",
                        "refresh_token": "reconnected-refresh",
                        "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                        "athlete": {"id": 666777},
                    },
                )
                if req.url.path == "/oauth/token"
                else httpx.Response(500)
            )
        ),
        follow_redirects=False,
    )
    reconn = client.get(f"/api/v1/strava/callback?code=c&state={state3}")
    assert reconn.status_code == 200
    assert reconn.json()["athlete_id"] == "666777"

    # Disconnect tolerates upstream deauthorize failure and still clears local DB connection
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(refresh_ok_handler),
        follow_redirects=False,
    )
    disc = client.post("/api/v1/strava/disconnect", headers={"X-CSRF-Token": csrf})
    assert disc.status_code == 200
    assert disc.json()["connected"] is False
    with h.session_factory() as db:
        assert db.scalar(select(OAuthConnection).where(OAuthConnection.user_id == user.id)) is None


def test_strava_ssrf_guard_blocks_metadata_and_redirects(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness(strava_api_base_url="https://169.254.169.254")
    user = h.create_user("ssrf@example.com")
    client, _ = h.login_client("ssrf@example.com")

    # /connect validates URL and refuses metadata IP
    assert client.get("/api/v1/strava/connect").status_code == 502

    # Also test redirect blocking during token exchange
    h2 = make_harness(strava_api_base_url="https://www.strava.com")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    user = h2.create_user("redir@example.com")
    client2, _csrf2 = h2.login_client("redir@example.com")
    transport = httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"Location": "http://127.0.0.1:8000/secret"})
    )
    h2.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )
    _seed_strava_connection(h2, user.id, athlete_id="101")

    resp = client2.get("/api/v1/strava/activities")
    assert resp.status_code == 502


def test_render_jobs_idempotency_sse_cancel_and_downloads(
    make_harness: HarnessFactory, sample_gpx: bytes
) -> None:
    h = make_harness(rate_limit_render_max=9, rate_limit_render_window_s=60)
    h.create_user("render@example.com")
    client, csrf = h.login_client("render@example.com")

    project_id = client.post(
        "/api/v1/projects",
        json={"title": "Render Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # Empty project cannot be rendered
    empty_r = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json={"resolution": "1280x720"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-key-empty-1"},
    )
    assert empty_r.status_code == 422

    # Upload a GPX stage
    stage_id = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("stage.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # Missing / invalid Idempotency-Key rejected
    assert (
        client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={"resolution": "1280x720"},
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={"resolution": "1280x720"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "short"},
        ).status_code
        == 400
    )

    # Foreign stage_colors key rejected
    assert (
        client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={"stage_colors": {str(uuid.uuid4()): "#FFFFFF"}},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "idem-key-bad-stage"},
        ).status_code
        == 422
    )

    spec_payload = {
        "resolution": "1280x720",
        "fps": 30,
        "map_style": "liberty",
        "stage_colors": {stage_id: "#F97316"},
        "stage_transitions": {stage_id: "bed"},
    }
    r1 = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json=spec_payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "render-req-0001"},
    )
    assert r1.status_code == 202, r1.text
    job_id = r1.json()["id"]
    assert r1.json()["status"] == "queued"

    # List & Get render jobs
    listed = client.get(f"/api/v1/projects/{project_id}/renders")
    assert listed.status_code == 200
    assert [j["id"] for j in listed.json()] == [job_id]
    fetched = client.get(f"/api/v1/renders/{job_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == job_id
    assert client.get(f"/api/v1/renders/{uuid.uuid4()}").status_code == 404

    # Idempotent replay with identical payload returns same job
    r2 = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json=spec_payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "render-req-0001"},
    )
    assert r2.status_code == 202
    assert r2.json()["id"] == job_id

    # Same Idempotency-Key with modified payload triggers 409 Conflict
    r_conflict = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json={**spec_payload, "fps": 60},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "render-req-0001"},
    )
    assert r_conflict.status_code == 409

    # Download before completion returns 409; missing thumbnail returns 404
    assert client.get(f"/api/v1/renders/{job_id}/download").status_code == 409
    assert client.get(f"/api/v1/renders/{job_id}/thumbnail").status_code == 404

    # SSE stream on non-terminal job loops across polls cleanly
    with client.stream("GET", f"/api/v1/renders/{job_id}/events") as sse_queued:
        assert sse_queued.status_code == 200
        queued_body = "".join(sse_queued.iter_text())
        assert '"status": "queued"' in queued_body

    # Cancel queued job; cancelling a terminal job returns 409
    c_resp = client.post(f"/api/v1/renders/{job_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert c_resp.status_code == 200
    assert c_resp.json()["status"] == "cancelled"
    assert (
        client.post(f"/api/v1/renders/{job_id}/cancel", headers={"X-CSRF-Token": csrf}).status_code
        == 409
    )

    # SSE stream emits cancelled terminal status and closes cleanly; unauthenticated rejected
    anon = TestClient(h.app)
    assert anon.get(f"/api/v1/renders/{job_id}/events").status_code == 401
    with client.stream("GET", f"/api/v1/renders/{job_id}/events") as sse:
        assert sse.status_code == 200
        body = "".join(sse.iter_text())
        assert "event: status" in body
        assert '"status": "cancelled"' in body

    # Simulate worker finishing another job and test download/thumbnail
    r3 = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json=spec_payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "render-req-0002"},
    )
    job2_id = r3.json()["id"]
    h.store.put_bytes(f"outputs/{job2_id}.mp4", b"FAKE-MP4-BYTES", content_type="video/mp4")
    h.store.put_bytes(f"outputs/{job2_id}.webp", b"FAKE-WEBP-BYTES", content_type="image/webp")
    with h.session_factory() as db:
        job2 = db.get(RenderJob, uuid.UUID(job2_id))
        assert job2 is not None
        job2.status = "succeeded"
        job2.progress_pct = 100
        job2.output_object_key = f"outputs/{job2_id}.mp4"
        job2.thumbnail_key = f"outputs/{job2_id}.webp"
        db.commit()

    dl = client.get(f"/api/v1/renders/{job2_id}/download")
    assert dl.status_code == 200
    assert dl.content == b"FAKE-MP4-BYTES"
    assert dl.headers["content-type"] == "video/mp4"

    th = client.get(f"/api/v1/renders/{job2_id}/thumbnail")
    assert th.status_code == 200
    assert th.content == b"FAKE-WEBP-BYTES"
    assert th.headers["content-type"] == "image/webp"

    # Rate limit on renders (max=9; 8 calls made so far -> 9th succeeds, 10th hits 429)
    r4 = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json=spec_payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "render-req-0003"},
    )
    assert r4.status_code == 202
    r5 = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json=spec_payload,
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "render-req-0004"},
    )
    assert r5.status_code == 429


def test_render_sse_deleted_job_and_idempotency_integrity_race(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness(rate_limit_render_max=20)
    user = h.create_user("renderrace@example.com")
    client, csrf = h.login_client("renderrace@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Race Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    client.post(
        f"/api/v1/projects/{proj_id}/gpx",
        files={"file": ("s.gpx", sample_gpx, "application/gpx+xml")},
        headers={"X-CSRF-Token": csrf},
    )

    # 1. SSE emits event: error if job disappears before poll executes
    from trip_api.routers import renders as renders_router

    job = client.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={"resolution": "1280x720"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "sse-disappear-1"},
    ).json()

    orig_get_owned_job = renders_router._get_owned_job

    def _delete_after_auth(db: Any, jid: uuid.UUID, uid: uuid.UUID) -> RenderJob:
        found = orig_get_owned_job(db, jid, uid)
        # Only delete during SSE pre-check by inspecting call stack or deleting on second check
        return found

    # Delete job inside a racing session wrapper on poll
    orig_sf = h.session_factory
    call_count = {"n": 0}

    def _deleting_sf() -> Any:
        call_count["n"] += 1
        if call_count["n"] == 2:
            with orig_sf() as cleanup_db:
                row = cleanup_db.get(RenderJob, uuid.UUID(job["id"]))
                if row is not None:
                    cleanup_db.delete(row)
                    cleanup_db.commit()
        return orig_sf()

    h.app.state.session_factory = _deleting_sf
    with client.stream("GET", f"/api/v1/renders/{job['id']}/events") as sse_err:
        assert sse_err.status_code == 200
        err_text = "".join(sse_err.iter_text())
        assert "event: error" in err_text
        assert "Render job not found" in err_text
    h.app.state.session_factory = orig_sf

    # 2. Render creation IntegrityError race recovery (same payload -> 202; mismatch -> 409)
    from sqlalchemy.orm import Session as OrmSession

    orig_commit = OrmSession.commit
    state = {"triggered": False}

    from trip_telemetry.schemas import RenderSpecV2

    def _racing_render_commit(self: OrmSession) -> None:
        if not state["triggered"] and any(
            isinstance(obj, RenderJob) and obj.idempotency_key == "race-render-key"
            for obj in self.new
        ):
            state["triggered"] = True
            with orig_sf() as other_db:
                other_db.add(
                    RenderJob(
                        project_id=uuid.UUID(proj_id),
                        owner_user_id=user.id,
                        idempotency_key="race-render-key",
                        idempotency_hash=renders_router._compute_idempotency_hash(
                            uuid.UUID(proj_id), RenderSpecV2(resolution="1280x720")
                        ),
                        spec_json={"resolution": "1280x720"},
                        status="queued",
                        progress_pct=0,
                        attempt_count=0,
                    )
                )
                other_db.commit()
            raise IntegrityError("duplicate idempotency", {}, Exception("uq"))
        orig_commit(self)

    monkeypatch.setattr(OrmSession, "commit", _racing_render_commit)
    same_race = client.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={"resolution": "1280x720"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "race-render-key"},
    )
    assert same_race.status_code == 202

    state["triggered"] = False
    with orig_sf() as db:
        row = db.scalar(select(RenderJob).where(RenderJob.idempotency_key == "race-render-key"))
        assert row is not None
        row.idempotency_hash = "different" * 7
        db.commit()
    # Force select to return None before commit by using fresh key with pre-seeded mismatch
    state["triggered"] = False

    def _racing_mismatch_commit(self: OrmSession) -> None:
        if not state["triggered"] and any(
            isinstance(obj, RenderJob) and obj.idempotency_key == "race-render-mismatch"
            for obj in self.new
        ):
            state["triggered"] = True
            with orig_sf() as other_db:
                other_db.add(
                    RenderJob(
                        project_id=uuid.UUID(proj_id),
                        owner_user_id=user.id,
                        idempotency_key="race-render-mismatch",
                        idempotency_hash="x" * 64,
                        spec_json={"resolution": "1920x1080"},
                        status="queued",
                        progress_pct=0,
                        attempt_count=0,
                    )
                )
                other_db.commit()
            raise IntegrityError("duplicate idempotency", {}, Exception("uq"))
        orig_commit(self)

    monkeypatch.setattr(OrmSession, "commit", _racing_mismatch_commit)
    diff_race = client.post(
        f"/api/v1/projects/{proj_id}/renders",
        json={"resolution": "1280x720"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "race-render-mismatch"},
    )
    assert diff_race.status_code == 409


def test_strava_webhook_subscription_id_fail_closed_and_rate_limit(
    make_harness: HarnessFactory,
) -> None:
    # 1. Fail-closed when neither verify_token nor subscription_id is configured
    unconfigured = make_harness(strava_verify_token="", strava_webhook_subscription_id=None)
    c_unconf = TestClient(unconfigured.app)
    assert (
        c_unconf.get(
            "/api/v1/strava/webhook?hub.mode=subscribe&hub.challenge=abc&hub.verify_token=any"
        ).status_code
        == 403
    )
    r_unconf = c_unconf.post(
        "/api/v1/strava/webhook",
        json={"object_type": "athlete", "aspect_type": "update", "owner_id": 1},
    )
    assert r_unconf.status_code == 403

    # 2. Verify-token-only mode accepts header or query parameter aliases
    tok_only = make_harness(
        strava_verify_token="only-token-secret",
        strava_webhook_subscription_id=None,
        rate_limit_strava_webhook_max=10,
    )
    c_tok = TestClient(tok_only.app)
    assert c_tok.post("/api/v1/strava/webhook", json={"object_type": "activity"}).status_code == 403
    for suffix in (
        "?verify_token=only-token-secret",
        "?hub.verify_token=only-token-secret",
        "?token=only-token-secret",
    ):
        assert (
            c_tok.post(
                f"/api/v1/strava/webhook{suffix}",
                json={"object_type": "activity", "aspect_type": "create"},
            ).status_code
            == 200
        )

    # 3. Both subscription_id and verify_token configured -> BOTH must match (no token bypass)
    sub_h = make_harness(
        strava_verify_token="verify-secret",
        strava_webhook_subscription_id=778899,
        rate_limit_strava_webhook_max=8,
        rate_limit_strava_webhook_window_s=60,
    )
    user = sub_h.create_user("subwh@example.com")
    other_user = sub_h.create_user("subwh-other@example.com")
    _seed_strava_connection(sub_h, user.id, athlete_id="444555")
    _seed_strava_connection(sub_h, other_user.id, athlete_id="999888")

    c_sub = TestClient(sub_h.app)
    # Missing subscription_id even with valid token -> 403 (req 1)
    assert (
        c_sub.post(
            "/api/v1/strava/webhook?verify_token=verify-secret",
            json={
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": 444555,
                "updates": {"authorized": "false"},
            },
        ).status_code
        == 403
    )
    # Boolean/wrong subscription_id -> 403 (req 2)
    assert (
        c_sub.post(
            "/api/v1/strava/webhook?verify_token=verify-secret",
            json={
                "subscription_id": True,
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": 444555,
                "updates": {"authorized": "false"},
            },
        ).status_code
        == 403
    )
    # Matching subscription_id with MISSING token -> 403 (req 3)
    assert (
        c_sub.post(
            "/api/v1/strava/webhook",
            json={
                "subscription_id": 778899,
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": 444555,
                "updates": {"authorized": "false"},
            },
        ).status_code
        == 403
    )
    # Matching subscription_id with wrong token -> 403 (req 4)
    assert (
        c_sub.post(
            "/api/v1/strava/webhook?verify_token=wrong",
            json={
                "subscription_id": 778899,
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": 444555,
                "updates": {"authorized": "false"},
            },
        ).status_code
        == 403
    )
    # Non-deauth event with matching subscription_id + valid token preserves connections (req 5)
    assert (
        c_sub.post(
            "/api/v1/strava/webhook?verify_token=verify-secret",
            json={
                "subscription_id": 778899,
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": 444555,
                "updates": {"authorized": "true"},
            },
        ).status_code
        == 200
    )
    # Boolean owner_id ignored safely with valid token (req 6)
    assert (
        c_sub.post(
            "/api/v1/strava/webhook?verify_token=verify-secret",
            json={
                "subscription_id": 778899,
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": True,
                "updates": {"authorized": "false"},
            },
        ).status_code
        == 200
    )
    # Matching subscription_id + valid token deauthorizes athlete 444555 only (req 7)
    ok_resp = c_sub.post(
        "/api/v1/strava/webhook",
        headers={"X-Strava-Verify-Token": "verify-secret"},
        json={
            "subscription_id": 778899,
            "object_type": "athlete",
            "aspect_type": "update",
            "owner_id": "444555",
            "updates": {"authorized": "FALSE"},
        },
    )
    assert ok_resp.status_code == 200
    with sub_h.session_factory() as db:
        remaining = db.scalars(select(OAuthConnection)).all()
        assert [c.external_athlete_id for c in remaining] == ["999888"]
        assert db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "strava.webhook_deauthorize")
        )

    # Matching subscription_id + valid token succeeds on req 8, then 9th hits rate limit -> 429
    assert (
        c_sub.post(
            "/api/v1/strava/webhook?verify_token=verify-secret",
            json={"subscription_id": 778899, "object_type": "activity", "aspect_type": "create"},
        ).status_code
        == 200
    )
    limited = c_sub.post(
        "/api/v1/strava/webhook?verify_token=verify-secret",
        json={"subscription_id": 778899, "object_type": "activity", "aspect_type": "create"},
    )
    assert limited.status_code == 429
    assert "retry-after" in limited.headers

    # 4. Subscription-ID-only mode (strava_verify_token=""): matching subscription_id succeeds
    sub_only = make_harness(
        strava_verify_token="",
        strava_webhook_subscription_id=556677,
        rate_limit_strava_webhook_max=10,
    )
    sub_only_user = sub_only.create_user("subonly@example.com")
    _seed_strava_connection(sub_only, sub_only_user.id, athlete_id="121212")
    c_sub_only = TestClient(sub_only.app)
    assert (
        c_sub_only.post(
            "/api/v1/strava/webhook",
            json={"subscription_id": 999999, "object_type": "activity", "aspect_type": "create"},
        ).status_code
        == 403
    )
    assert (
        c_sub_only.post(
            "/api/v1/strava/webhook",
            json={
                "subscription_id": 556677,
                "object_type": "athlete",
                "aspect_type": "update",
                "owner_id": 121212,
                "updates": {"authorized": "false"},
            },
        ).status_code
        == 200
    )
    with sub_only.session_factory() as db:
        assert db.scalars(select(OAuthConnection)).all() == []


def test_strava_batch_25_items_rate_limit_and_partial_404_429_handling(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness(rate_limit_strava_import_max=25, rate_limit_strava_import_window_s=60)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v3/activities/") and path.endswith("/streams"):
            aid = int(path.split("/")[4])
            if aid == 902:
                return httpx.Response(429, headers={"Retry-After": "15"})
            if aid == 903:
                # Non-GPS activity streams -> parse_strava_streams ValueError -> 422
                return httpx.Response(200, json={"time": {"data": [0, 60]}})
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.10, 0.20], [0.11, 0.21]]},
                    "time": {"data": [0, 60]},
                    "altitude": {"data": [1000.0, 1010.0]},
                },
            )
        if path.startswith("/api/v3/activities/"):
            aid = int(path.rsplit("/", 1)[-1])
            if aid == 901:
                return httpx.Response(404)
            if aid == 904:
                return httpx.Response(429, headers={"Retry-After": "30"})
            if aid == 905:
                return httpx.Response(200, json=["not-a-dict"])
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": f"Ride {aid}",
                    "sport_type": "Ride",
                    "start_date": "2001-07-01T08:00:00Z",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )

    user_full = h.create_user("batch25@example.com")
    _seed_strava_connection(h, user_full.id, athlete_id="250")
    c25, csrf25 = h.login_client("batch25@example.com")
    p25 = c25.post(
        "/api/v1/projects", json={"title": "Batch 25"}, headers={"X-CSRF-Token": csrf25}
    ).json()["id"]

    # Validation rejects 0 items and 26 items without deducting rate limit tokens
    assert (
        c25.post(
            f"/api/v1/projects/{p25}/import-strava/batch",
            json={"items": []},
            headers={"X-CSRF-Token": csrf25},
        ).status_code
        == 422
    )
    assert (
        c25.post(
            f"/api/v1/projects/{p25}/import-strava/batch",
            json={"items": [{"activity_id": i} for i in range(1, 27)]},
            headers={"X-CSRF-Token": csrf25},
        ).status_code
        == 422
    )

    # Full 25-item batch consumes exactly 25 tokens (next single import -> 429)
    full_batch = c25.post(
        f"/api/v1/projects/{p25}/import-strava/batch",
        json={"items": [{"activity_id": i} for i in range(1, 26)]},
        headers={"X-CSRF-Token": csrf25},
    )
    assert full_batch.status_code == 200, full_batch.text
    assert len(full_batch.json()["imported"]) == 25
    assert full_batch.json()["failed"] == []

    after_25 = c25.post(
        f"/api/v1/projects/{p25}/import-strava",
        json={"activity_id": 26},
        headers={"X-CSRF-Token": csrf25},
    )
    assert after_25.status_code == 429

    # Partial 404, 429 (both activity & streams), malformed JSON, and missing latlng handling
    user_partial = h.create_user("partial@example.com")
    _seed_strava_connection(h, user_partial.id, athlete_id="251")
    cp, csrfp = h.login_client("partial@example.com")
    pp = cp.post(
        "/api/v1/projects", json={"title": "Partial Mix"}, headers={"X-CSRF-Token": csrfp}
    ).json()["id"]

    mixed = cp.post(
        f"/api/v1/projects/{pp}/import-strava/batch",
        json={
            "items": [
                {"activity_id": 100},
                {"activity_id": 901},  # 404 activity
                {"activity_id": 902},  # 429 on streams
                {"activity_id": 903},  # 422 missing latlng
                {"activity_id": 904},  # 429 on activity
                {"activity_id": 905},  # 502 non-dict payload
                {"activity_id": 101},
            ]
        },
        headers={"X-CSRF-Token": csrfp},
    )
    assert mixed.status_code == 200, mixed.text
    m_body = mixed.json()
    assert [s["strava_activity_id"] for s in m_body["imported"]] == [100, 101]
    assert m_body["failed"] == [
        {"activity_id": 901, "detail": "Strava activity not found"},
        {"activity_id": 902, "detail": "Failed to fetch Strava activity streams"},
        {
            "activity_id": 903,
            "detail": "Strava streams missing non-empty 'latlng' stream",
        },
        {"activity_id": 904, "detail": "Failed to fetch Strava activity"},
        {"activity_id": 905, "detail": "Malformed Strava upstream payload"},
    ]

    # Batch where all items fail records no stage.strava_batch_imported audit event
    all_fail_proj = cp.post(
        "/api/v1/projects", json={"title": "All Fail"}, headers={"X-CSRF-Token": csrfp}
    ).json()["id"]
    all_fail = cp.post(
        f"/api/v1/projects/{all_fail_proj}/import-strava/batch",
        json={"items": [{"activity_id": 901}, {"activity_id": 904}]},
        headers={"X-CSRF-Token": csrfp},
    )
    assert all_fail.status_code == 200
    assert all_fail.json()["imported"] == []
    assert len(all_fail.json()["failed"]) == 2
    audits = cp.get(f"/api/v1/projects/{all_fail_proj}/audit").json()
    assert all(ev["event_type"] != "stage.strava_batch_imported" for ev in audits)


def test_strava_batch_import_consumes_proportional_rate_limit_tokens(
    make_harness: HarnessFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness(rate_limit_strava_import_max=5, rate_limit_strava_import_window_s=60)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v3/activities/") and path.endswith("/streams"):
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[0.10, 0.20], [0.11, 0.21]]},
                    "time": {"data": [0, 60]},
                    "altitude": {"data": [1000.0, 1010.0]},
                },
            )
        if path.startswith("/api/v3/activities/"):
            aid = int(path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json={
                    "id": aid,
                    "name": f"Ride {aid}",
                    "sport_type": "Ride",
                    "start_date": "2001-07-01T08:00:00Z",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )

    user = h.create_user("batchrl@example.com")
    _seed_strava_connection(h, user.id, athlete_id="333")

    client, csrf = h.login_client("batchrl@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Batch RL"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # 3 items consumes 3 of 5 tokens
    r1 = client.post(
        f"/api/v1/projects/{proj_id}/import-strava/batch",
        json={"items": [{"activity_id": 1}, {"activity_id": 2}, {"activity_id": 3}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200
    assert len(r1.json()["imported"]) == 3

    # Requesting 3 more (3 + 3 = 6 > 5) must fail with 429
    r2 = client.post(
        f"/api/v1/projects/{proj_id}/import-strava/batch",
        json={"items": [{"activity_id": 4}, {"activity_id": 5}, {"activity_id": 6}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 429

    # Requesting remaining 2 (3 + 2 = 5 <= 5) succeeds
    r3 = client.post(
        f"/api/v1/projects/{proj_id}/import-strava/batch",
        json={"items": [{"activity_id": 4}, {"activity_id": 5}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert r3.status_code == 200
    assert len(r3.json()["imported"]) == 2

    # Next single import (5 + 1 = 6 > 5) hits 429
    r4 = client.post(
        f"/api/v1/projects/{proj_id}/import-strava",
        json={"activity_id": 6},
        headers={"X-CSRF-Token": csrf},
    )
    assert r4.status_code == 429

    # A single batch larger than the entire bucket limit fails immediately on fresh bucket
    user2 = h.create_user("batchrl2@example.com")
    _seed_strava_connection(h, user2.id, athlete_id="334")
    c2, csrf2 = h.login_client("batchrl2@example.com")
    p2 = c2.post("/api/v1/projects", json={"title": "P2"}, headers={"X-CSRF-Token": csrf2}).json()[
        "id"
    ]
    too_big = c2.post(
        f"/api/v1/projects/{p2}/import-strava/batch",
        json={"items": [{"activity_id": idx} for idx in range(10, 16)]},
        headers={"X-CSRF-Token": csrf2},
    )
    assert too_big.status_code == 429


def test_strava_callback_atomic_state_consumption_race(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/oauth/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_at": int((datetime.now(UTC) + timedelta(hours=6)).timestamp()),
                    "refresh_token": "ref",
                    "access_token": "acc",
                    "scope": "read",
                    "athlete": {"id": 888},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    harness.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=transport, follow_redirects=False
    )

    harness.create_user("race@example.com")
    client, _ = harness.login_client("race@example.com")
    cookies = dict(client.cookies)
    state = client.get("/api/v1/strava/connect").json()["state"]

    barrier = Barrier(2)

    def _hit_callback(_idx: int) -> int:
        c = TestClient(harness.app, cookies=cookies)
        barrier.wait(timeout=5.0)
        resp = c.get(f"/api/v1/strava/callback?code=code-abc&state={state}")
        return int(resp.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(_hit_callback, [0, 1]))

    assert statuses == [200, 400]
    assert token_calls == 1
    with harness.session_factory() as db:
        row = db.scalar(select(OAuthState))
        assert row is not None
        assert row.consumed_at is not None


def test_strava_epoch_parsing_rejects_unicode_digits_and_negative_iso_dates(
    harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_inputs = (
        "",
        "   ",
        "²",
        "³",
        "①",
        "٠",
        "١٢٣",
        "５",
        "½",
        "Ⅷ",
        "-1",
        "-100",
        "-9999999999",
        "1960-01-01",
        "1969-12-31",
        "1969-12-31T23:59:59Z",
        "1970-01-01T00:00:00+02:00",
        "not-a-date",
        "2001-13-01",
        "2001-02-30",
        "2001-06-15T25:00:00Z",
        "123abc",
        "12.34",
        "NaN",
        "inf",
    )
    for bad in bad_inputs:
        with pytest.raises(HTTPException) as exc_info:
            _parse_strava_epoch(bad, end_of_day=False)
        assert exc_info.value.status_code == 422, f"Expected 422 for {bad!r}"

    assert _parse_strava_epoch("0", end_of_day=False) == 0
    assert _parse_strava_epoch("1970-01-01", end_of_day=False) == 0
    assert _parse_strava_epoch("1970-01-01", end_of_day=True) == 86399
    assert _parse_strava_epoch("  1700000000  ", end_of_day=False) == 1700000000
    assert _parse_strava_epoch("2001-06-15T12:00:00", end_of_day=False) == _parse_strava_epoch(
        "2001-06-15T14:00:00+02:00", end_of_day=False
    )

    # End-to-end HTTP check against GET /api/v1/strava/activities
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    user = harness.create_user("epoch@example.com")
    _seed_strava_connection(harness, user.id, athlete_id="777")

    client, _ = harness.login_client("epoch@example.com")
    for q in ("%C2%B2", "%D9%A1%D9%A2", "-100", "1960-01-01", "1969-12-31T23:59:59Z", "bad"):
        assert client.get(f"/api/v1/strava/activities?after={q}").status_code == 422
    assert client.get("/api/v1/strava/activities?before=1969-12-31T23:59:59Z").status_code == 422


@given(raw=st.text(max_size=64), end_of_day=st.booleans())
@hypothesis_settings(max_examples=250, deadline=None)
def test_parse_strava_epoch_hypothesis_fuzzing(raw: str, end_of_day: bool) -> None:
    try:
        epoch = _parse_strava_epoch(raw, end_of_day=end_of_day)
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        assert isinstance(epoch, int)
        assert epoch >= 0


def test_render_download_streaming_ranges_and_s3_404(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = make_harness()
    user = h.create_user("stream@example.com")
    client, csrf = h.login_client("stream@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Stream Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    job_id = uuid.uuid4()
    mp4_bytes = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    mp4_key = f"outputs/{job_id}.mp4"
    thumb_key = f"outputs/{job_id}.webp"
    h.store.put_bytes(mp4_key, mp4_bytes, content_type="video/mp4")

    with h.session_factory() as db:
        db.add(
            RenderJob(
                id=job_id,
                project_id=uuid.UUID(proj_id),
                owner_user_id=user.id,
                idempotency_key="stream-job-1",
                idempotency_hash="s" * 64,
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
                output_object_key=mp4_key,
                thumbnail_key=thumb_key,
            )
        )
        db.commit()

    # Full download streams with Content-Length & Accept-Ranges
    full = client.get(f"/api/v1/renders/{job_id}/download")
    assert full.status_code == 200
    assert full.content == mp4_bytes
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(mp4_bytes))

    # Closed range bytes=0-9
    r_0_9 = client.get(f"/api/v1/renders/{job_id}/download", headers={"Range": "bytes=0-9"})
    assert r_0_9.status_code == 206
    assert r_0_9.content == b"0123456789"
    assert r_0_9.headers["content-range"] == f"bytes 0-9/{len(mp4_bytes)}"
    assert r_0_9.headers["content-length"] == "10"

    # Open-ended range bytes=10-
    r_10 = client.get(f"/api/v1/renders/{job_id}/download", headers={"Range": "bytes=10-"})
    assert r_10.status_code == 206
    assert r_10.content == mp4_bytes[10:]
    assert r_10.headers["content-range"] == f"bytes 10-{len(mp4_bytes) - 1}/{len(mp4_bytes)}"

    # Suffix range bytes=-5
    r_suf = client.get(f"/api/v1/renders/{job_id}/download", headers={"Range": "bytes=-5"})
    assert r_suf.status_code == 206
    assert r_suf.content == mp4_bytes[-5:]
    assert r_suf.headers["content-range"] == (
        f"bytes {len(mp4_bytes) - 5}-{len(mp4_bytes) - 1}/{len(mp4_bytes)}"
    )

    # Unsatisfiable ranges return 416
    for bad_hdr in ("bytes=999999-", "bytes=10-5", "bytes=-0"):
        bad_range = client.get(f"/api/v1/renders/{job_id}/download", headers={"Range": bad_hdr})
        assert bad_range.status_code == 416
        assert bad_range.headers["content-range"] == f"bytes */{len(mp4_bytes)}"

    # S3ObjectStore missing object ClientError(NoSuchKey) maps cleanly to HTTP 404 (not 500)
    class MissingS3Client:
        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "Missing"}}, "GetObject")

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: MissingS3Client())
    s3_store = S3ObjectStore(
        endpoint_url="",
        access_key_id="",
        secret_access_key="",
        bucket="b",
        region="",
    )
    h.app.state.object_store = s3_store
    assert client.get(f"/api/v1/renders/{job_id}/download").status_code == 404
    assert client.get(f"/api/v1/renders/{job_id}/thumbnail").status_code == 404


def test_local_and_s3_object_store_copy_zero_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trip_api.storage import LocalObjectStore

    local = LocalObjectStore(tmp_path)
    local.put_bytes("outputs/src.mp4", b"mp4-binary-payload", content_type="video/mp4")
    local.copy("outputs/src.mp4", "outputs/nested/dst.mp4", content_type="video/mp4")
    assert local.get_bytes("outputs/nested/dst.mp4") == b"mp4-binary-payload"
    assert local.get_bytes("outputs/src.mp4") == b"mp4-binary-payload"

    with pytest.raises(FileNotFoundError):
        local.copy("outputs/missing.mp4", "outputs/dst2.mp4")
    with pytest.raises(ValueError, match="Unsafe object key"):
        local.copy("outputs/src.mp4", "../escape.mp4")

    s3_copy_calls: list[tuple[Any, ...]] = []

    class FakeS3CopyClient:
        def copy(
            self,
            copy_source: dict[str, str],
            bucket: str,
            key: str,
            ExtraArgs: dict[str, Any] | None = None,
        ) -> None:
            s3_copy_calls.append((copy_source, bucket, key, ExtraArgs))
            if copy_source["Key"] == "outputs/missing.mp4":
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "Missing"}},
                    "CopyObject",
                )

    monkeypatch.setattr(boto3, "client", lambda *_args, **_kwargs: FakeS3CopyClient())
    s3 = S3ObjectStore(
        endpoint_url="",
        access_key_id="",
        secret_access_key="",
        bucket="my-bucket",
        region="us-east-1",
    )
    s3.copy("outputs/a.mp4", "outputs/b.mp4", content_type="video/mp4")
    assert s3_copy_calls == [
        (
            {"Bucket": "my-bucket", "Key": "outputs/a.mp4"},
            "my-bucket",
            "outputs/b.mp4",
            {"ContentType": "video/mp4", "MetadataDirective": "REPLACE"},
        )
    ]
    with pytest.raises(FileNotFoundError):
        s3.copy("outputs/missing.mp4", "outputs/c.mp4")


def test_try_copy_cached_render_artifacts_uses_store_copy(tmp_path: Path) -> None:
    from trip_api.routers.renders import _try_copy_cached_render_artifacts
    from trip_api.storage import LocalObjectStore

    class CopySpyStore(LocalObjectStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.copy_calls: list[tuple[str, str, str | None]] = []

        def get_bytes(self, key: str) -> bytes:
            raise AssertionError(f"get_bytes should not be called during zero-copy: {key}")

        def copy(self, src_key: str, dst_key: str, *, content_type: str | None = None) -> None:
            self.copy_calls.append((src_key, dst_key, content_type))
            super().copy(src_key, dst_key, content_type=content_type)

    store = CopySpyStore(tmp_path)
    src_job_id = uuid.uuid4()
    new_job_id = uuid.uuid4()
    src_mp4 = f"outputs/{src_job_id}.mp4"
    src_webp = f"outputs/{src_job_id}.webp"
    store.put_bytes(src_mp4, b"MP4-DATA", content_type="video/mp4")
    store.put_bytes(src_webp, b"WEBP-DATA", content_type="image/webp")

    cached_job = RenderJob(
        id=src_job_id,
        project_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        idempotency_key="cache-src",
        idempotency_hash="c" * 64,
        spec_json={},
        status="succeeded",
        progress_pct=100,
        output_object_key=src_mp4,
        thumbnail_key=src_webp,
    )
    copied = _try_copy_cached_render_artifacts(store, cached_job, new_job_id)
    assert copied == (f"outputs/{new_job_id}.mp4", f"outputs/{new_job_id}.webp")
    assert store.copy_calls == [
        (src_mp4, f"outputs/{new_job_id}.mp4", "video/mp4"),
        (src_webp, f"outputs/{new_job_id}.webp", "image/webp"),
    ]
    assert store.resolve_existing_path(f"outputs/{new_job_id}.mp4").read_bytes() == b"MP4-DATA"
    assert store.resolve_existing_path(f"outputs/{new_job_id}.webp").read_bytes() == b"WEBP-DATA"

    missing_job = RenderJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        idempotency_key="cache-missing",
        idempotency_hash="d" * 64,
        spec_json={},
        status="succeeded",
        progress_pct=100,
        output_object_key="outputs/does-not-exist.mp4",
        thumbnail_key=None,
    )
    assert _try_copy_cached_render_artifacts(store, missing_job, uuid.uuid4()) is None


def test_stream_render_events_offloads_snapshot_fetch(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from trip_api.routers import renders as renders_router

    h = make_harness()
    user = h.create_user("sseoffload@example.com")
    client, csrf = h.login_client("sseoffload@example.com")
    proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Offload Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    job_id = uuid.uuid4()
    with h.session_factory() as db:
        db.add(
            RenderJob(
                id=job_id,
                project_id=uuid.UUID(proj_id),
                owner_user_id=user.id,
                idempotency_key="sse-offload-1",
                idempotency_hash="e" * 64,
                spec_json={"version": 2},
                status="succeeded",
                progress_pct=100,
            )
        )
        db.commit()

    offloaded_fns: list[Any] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloaded_fns.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(renders_router.asyncio, "to_thread", spy_to_thread)

    with client.stream("GET", f"/api/v1/renders/{job_id}/events") as sse:
        assert sse.status_code == 200
        body = "".join(sse.iter_text())
        assert '"status": "succeeded"' in body

    assert renders_router._fetch_job_snapshot in offloaded_fns


def test_strava_import_status_and_alias(make_harness: HarnessFactory) -> None:
    h = make_harness()
    user = h.create_user("status-user@example.com")
    other = h.create_user("status-other@example.com")
    client, csrf = h.login_client("status-user@example.com")

    # Disconnected state works on both /import-status and /status alias
    for path in ("/api/v1/strava/import-status", "/api/v1/strava/status"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.json() == {
            "connected": False,
            "athlete_id": None,
            "scope": None,
            "pending_jobs": 0,
            "queued_jobs": 0,
            "running_jobs": 0,
            "succeeded_jobs": 0,
            "failed_jobs": 0,
            "ignored_jobs": 0,
            "imported_stages_count": 0,
            "last_error": None,
        }

    # Seed Strava connection, stages (Strava + non-Strava), and IngestJobs across statuses
    _seed_strava_connection(h, user.id, athlete_id="77001")
    _seed_strava_connection(h, other.id, athlete_id="88002")

    proj_id = uuid.UUID(
        client.post(
            "/api/v1/projects",
            json={"title": "Status Trip"},
            headers={"X-CSRF-Token": csrf},
        ).json()["id"]
    )
    now = datetime.now(UTC)
    with h.session_factory() as db:
        db.add_all(
            [
                Stage(
                    project_id=proj_id,
                    source_type="strava",
                    source_format="strava",
                    activity_type="cycling",
                    external_source_id="1001",
                    strava_activity_id=1001,
                    name="Imported Ride 1",
                    stage_order=0,
                    day_index=1,
                    bronze_object_key="bronze/1001.json",
                    silver_object_key="silver/1001.parquet",
                    point_count=10,
                    distance_m=1000.0,
                ),
                Stage(
                    project_id=proj_id,
                    source_type="gpx",
                    source_format="gpx",
                    activity_type="cycling",
                    name="Local GPX Stage",
                    stage_order=1,
                    day_index=1,
                    bronze_object_key="bronze/local.gpx",
                    silver_object_key="silver/local.parquet",
                    point_count=10,
                    distance_m=1000.0,
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2001,
                    status="queued",
                    payload_json={"source": "strava_bulk_import"},
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2002,
                    status="queued",
                    payload_json={"source": "strava_bulk_import"},
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2003,
                    status="running",
                    payload_json={"source": "strava_bulk_import"},
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2004,
                    status="succeeded",
                    payload_json={"source": "strava_bulk_import"},
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2005,
                    status="ignored",
                    payload_json={"source": "strava_bulk_import"},
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2006,
                    status="failed",
                    error_message="older failure",
                    created_at=now - timedelta(minutes=10),
                    updated_at=now - timedelta(minutes=10),
                    payload_json={"source": "strava_bulk_import"},
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="77001",
                    external_object_id=2007,
                    status="failed",
                    error_message="latest failure reason",
                    created_at=now - timedelta(minutes=1),
                    updated_at=now - timedelta(minutes=1),
                    payload_json={"source": "strava_bulk_import"},
                ),
                # Another athlete's job must not be counted
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="88002",
                    external_object_id=9999,
                    status="failed",
                    error_message="other athlete error",
                    payload_json={},
                ),
            ]
        )
        db.commit()

    expected = {
        "connected": True,
        "athlete_id": "77001",
        "scope": "read,activity:read_all",
        "pending_jobs": 3,
        "queued_jobs": 2,
        "running_jobs": 1,
        "succeeded_jobs": 1,
        "failed_jobs": 2,
        "ignored_jobs": 1,
        "imported_stages_count": 1,
        "last_error": "latest failure reason",
    }
    assert client.get("/api/v1/strava/import-status").json() == expected
    assert client.get("/api/v1/strava/status").json() == expected


def test_strava_import_all_pagination_dedup_gps_bola_and_autosync(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness(rate_limit_strava_import_max=50)
    user = h.create_user("bulk-owner@example.com")
    h.create_user("bulk-other@example.com")
    client, csrf = h.login_client("bulk-owner@example.com")
    other_client, other_csrf = h.login_client("bulk-other@example.com")

    _seed_strava_connection(h, user.id, athlete_id="31415")

    other_proj_id = other_client.post(
        "/api/v1/projects",
        json={"title": "Other Private Trip"},
        headers={"X-CSRF-Token": other_csrf},
    ).json()["id"]

    # BOLA check: specifying another user's project_id returns 404 before calling Strava
    upstream_calls: list[httpx.Request] = []
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: (upstream_calls.append(req), httpx.Response(200, json=[]))[1]
        ),
        follow_redirects=False,
    )
    bola_resp = client.post(
        "/api/v1/strava/import-all",
        json={"project_id": other_proj_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert bola_resp.status_code == 404
    assert upstream_calls == []

    # Invalid date range -> 422
    bad_dates = client.post(
        "/api/v1/strava/import-all",
        json={"after": "2024-06-10", "before": "2024-06-01"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_dates.status_code == 422

    # Empty response without project_id must NOT create an empty Strava Auto-Sync project
    empty_resp = client.post(
        "/api/v1/strava/import-all",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert empty_resp.status_code == 200, empty_resp.text
    assert empty_resp.json() == {
        "queued_count": 0,
        "skipped_existing_count": 0,
        "skipped_no_gps_count": 0,
        "total_scanned": 0,
        "project_id": None,
        "project_title": None,
        "queued_activity_ids": [],
    }
    with h.session_factory() as db:
        assert db.scalars(select(Project).where(Project.owner_user_id == user.id)).all() == []

    # Seed an explicit project with an already-imported stage (activity 10001)
    # plus a stage identified via numeric external_source_id (activity 10002)
    # and an active running IngestJob (activity 10003)
    own_proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Alpine Tour"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    with h.session_factory() as db:
        db.add_all(
            [
                Stage(
                    project_id=uuid.UUID(own_proj_id),
                    source_type="strava",
                    source_format="strava",
                    activity_type="cycling",
                    external_source_id="10001",
                    strava_activity_id=10001,
                    name="Existing Strava Stage",
                    stage_order=0,
                    day_index=1,
                    bronze_object_key="bronze/10001.json",
                    silver_object_key="silver/10001.parquet",
                    point_count=5,
                    distance_m=500.0,
                ),
                Stage(
                    project_id=uuid.UUID(own_proj_id),
                    source_type="strava",
                    source_format="strava",
                    activity_type="cycling",
                    external_source_id="10002",
                    strava_activity_id=None,
                    name="Legacy Strava Stage",
                    stage_order=1,
                    day_index=1,
                    bronze_object_key="bronze/10002.json",
                    silver_object_key="silver/10002.parquet",
                    point_count=5,
                    distance_m=500.0,
                ),
                IngestJob(
                    provider="strava",
                    event_type="create",
                    external_owner_id="31415",
                    external_object_id=10003,
                    status="running",
                    payload_json={"source": "strava_bulk_import"},
                ),
            ]
        )
        db.commit()

    # Build 105 activities across two Strava pages (page 1 = 100, page 2 = 5)
    # cap max_activities = 103 so page 2 requests per_page=3 and stops at 103 scanned.
    page1_items = []
    for idx in range(1, 101):
        act_id = 10000 + idx
        page1_items.append(
            {
                "id": act_id,
                "name": f"Ride {act_id}",
                "sport_type": "Ride",
                "start_date": "2024-05-01T08:00:00Z",
                # Make activity 10004 and 10005 indoor (no GPS)
                "start_latlng": [] if act_id in (10004, 10005) else [46.0, 7.0],
            }
        )
    page2_items = [
        {
            "id": 10101,
            "name": "Ride 10101",
            "sport_type": "Ride",
            "start_date": "2024-05-02T08:00:00Z",
            "start_latlng": [46.1, 7.1],
        },
        {
            "id": 10102,
            "name": "Indoor 10102",
            "sport_type": "VirtualRide",
            "start_date": "2024-05-02T09:00:00Z",
            "start_latlng": None,
        },
        {
            "id": 10103,
            "name": "Ride 10103",
            "sport_type": "Run",
            "start_date": "2024-05-02T10:00:00Z",
            "start_latlng": [46.2, 7.2],
        },
        {
            "id": 10104,
            "name": "Ride 10104 beyond cap",
            "sport_type": "Ride",
            "start_date": "2024-05-02T11:00:00Z",
            "start_latlng": [46.3, 7.3],
        },
    ]

    page_queries: list[dict[str, str]] = []

    def multi_page_handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/athlete/activities"
        params = dict(req.url.params)
        page_queries.append(params)
        page_num = int(params["page"])
        if page_num == 1:
            return httpx.Response(200, json=page1_items)
        if page_num == 2:
            return httpx.Response(200, json=page2_items[: int(params["per_page"])])
        return httpx.Response(200, json=[])

    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(multi_page_handler),
        follow_redirects=False,
    )

    resp = client.post(
        "/api/v1/strava/import-all",
        json={
            "max_activities": 103,
            "after": "2024-05-01",
            "before": "2024-05-31",
            "gps_only": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 103 scanned: 3 existing (10001, 10002, 10003), 3 no-gps (10004, 10005, 10102) -> 97 queued
    assert data["total_scanned"] == 103
    assert data["skipped_existing_count"] == 3
    assert data["skipped_no_gps_count"] == 3
    assert data["queued_count"] == 97
    assert len(data["queued_activity_ids"]) == 97
    assert 10103 in data["queued_activity_ids"]
    assert 10104 not in data["queued_activity_ids"]
    assert data["project_title"] == "Strava Auto-Sync"
    assert data["project_id"] is not None

    # Verify pagination params and after/before epochs
    assert len(page_queries) == 2
    assert page_queries[0]["page"] == "1"
    assert page_queries[0]["per_page"] == "100"
    assert page_queries[1]["page"] == "2"
    assert page_queries[1]["per_page"] == "3"
    assert int(page_queries[0]["after"]) < int(page_queries[0]["before"])

    # Verify audit event was written
    with h.session_factory() as db:
        audit = db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.user_id == user.id,
                AuditEvent.event_type == "strava.bulk_import_queued",
            )
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        assert audit is not None
        assert audit.metadata_json["queued_count"] == 97
        assert audit.metadata_json["skipped_existing_count"] == 3
        assert audit.metadata_json["skipped_no_gps_count"] == 3

    # Re-running with same parameters is idempotent (all 97 newly queued are now skipped)
    second = client.post(
        "/api/v1/strava/import-all",
        json={"max_activities": 103, "gps_only": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 200
    assert second.json()["queued_count"] == 0
    assert second.json()["skipped_existing_count"] == 100
    assert second.json()["skipped_no_gps_count"] == 3
    assert second.json()["project_title"] == "Strava Auto-Sync"

    # Running with gps_only=False and explicit project_id queues the 3 indoor activities
    indoor_resp = client.post(
        "/api/v1/strava/import-all",
        json={"project_id": own_proj_id, "max_activities": 103, "gps_only": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert indoor_resp.status_code == 200
    assert indoor_resp.json()["queued_count"] == 3
    assert indoor_resp.json()["skipped_no_gps_count"] == 0
    assert indoor_resp.json()["project_id"] == own_proj_id
    assert indoor_resp.json()["project_title"] == "Alpine Tour"
    assert set(indoor_resp.json()["queued_activity_ids"]) == {10004, 10005, 10102}


def test_strava_import_all_rate_limit_and_ingest_processing(
    make_harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    h = make_harness(rate_limit_strava_import_max=50)
    user = h.create_user("bulk-worker@example.com")
    other = h.create_user("bulk-worker-other@example.com")
    client, csrf = h.login_client("bulk-worker@example.com")
    _seed_strava_connection(h, user.id, athlete_id="51515")

    explicit_proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Dolomites Week"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]

    # Page 1 returning 429 -> 429 response
    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(429)),
        follow_redirects=False,
    )
    rl_page1 = client.post(
        "/api/v1/strava/import-all",
        json={"project_id": explicit_proj_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert rl_page1.status_code == 429

    # Page 1 succeeds with 100 items, Page 2 returns 429 -> returns 200 with page 1 items queued
    page1_hundred = [
        {
            "id": 60000 + idx,
            "name": f"Page1 Ride {idx}",
            "sport_type": "Ride",
            "start_date": "2024-06-01T07:00:00Z",
            "start_latlng": [46.0, 11.0],
        }
        for idx in range(1, 101)
    ]
    seen_pages_429: list[int] = []

    def page2_429_handler(req: httpx.Request) -> httpx.Response:
        page_num = int(req.url.params["page"])
        seen_pages_429.append(page_num)
        if page_num == 1:
            return httpx.Response(200, json=page1_hundred)
        return httpx.Response(429)

    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(page2_429_handler),
        follow_redirects=False,
    )
    partial_429_resp = client.post(
        "/api/v1/strava/import-all",
        json={"project_id": explicit_proj_id, "max_activities": 150},
        headers={"X-CSRF-Token": csrf},
    )
    assert partial_429_resp.status_code == 200, partial_429_resp.text
    assert seen_pages_429 == [1, 2]
    assert partial_429_resp.json()["total_scanned"] == 100
    assert partial_429_resp.json()["queued_count"] == 100

    # Clear those 100 queued jobs before testing rate-limit header stop + ingest worker
    with h.session_factory() as db:
        for row in db.scalars(select(IngestJob)).all():
            db.delete(row)
        db.commit()

    # Rate-limit usage header >= 90% stops pagination cleanly after page 1
    page1_two_items = [
        {
            "id": 70001,
            "name": "Pass One",
            "sport_type": "Ride",
            "start_date": "2024-07-01T07:00:00Z",
            "start_latlng": [46.5, 11.8],
        },
        {
            "id": 70002,
            "name": "Pass Two",
            "sport_type": "Ride",
            "start_date": "2024-07-02T07:00:00Z",
            "start_latlng": [46.6, 11.9],
        },
    ]

    def rl_header_handler(req: httpx.Request) -> httpx.Response:
        page_num = int(req.url.params["page"])
        if page_num == 1:
            return httpx.Response(
                200,
                json=page1_two_items,
                headers={"X-RateLimit-Usage": "95,100", "X-RateLimit-Limit": "100,1000"},
            )
        raise AssertionError("Should not request page 2 after rate-limit header backpressure")

    h.app.state.http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(rl_header_handler),
        follow_redirects=False,
    )
    queued_resp = client.post(
        "/api/v1/strava/import-all",
        json={"project_id": explicit_proj_id, "max_activities": 2},
        headers={"X-CSRF-Token": csrf},
    )
    assert queued_resp.status_code == 200, queued_resp.text
    assert queued_resp.json()["queued_activity_ids"] == [70001, 70002]

    # Pre-seed activity 70002 as already imported into another owned project to verify
    # bulk import job skips upstream Strava HTTP completely for 70002
    other_own_proj_id = client.post(
        "/api/v1/projects",
        json={"title": "Already Imported Trip"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    with h.session_factory() as db:
        db.add(
            Stage(
                project_id=uuid.UUID(other_own_proj_id),
                source_type="strava",
                source_format="strava",
                activity_type="cycling",
                external_source_id="70002",
                strava_activity_id=70002,
                name="Pass Two Existing",
                stage_order=0,
                day_index=1,
                bronze_object_key="bronze/70002.json",
                silver_object_key="silver/70002.parquet",
                point_count=3,
                distance_m=1500.0,
            )
        )
        db.commit()

    fetched_paths: list[str] = []

    def sync_worker_handler(req: httpx.Request) -> httpx.Response:
        fetched_paths.append(req.url.path)
        if req.url.path == "/api/v3/activities/70001":
            return httpx.Response(
                200,
                json={
                    "id": 70001,
                    "name": "Pass One",
                    "sport_type": "Ride",
                    "start_date": "2024-07-01T07:00:00Z",
                    "start_latlng": [46.5, 11.8],
                },
            )
        if req.url.path == "/api/v3/activities/70001/streams":
            return httpx.Response(
                200,
                json={
                    "latlng": {"data": [[46.50, 11.80], [46.51, 11.81], [46.52, 11.82]]},
                    "time": {"data": [0, 60, 120]},
                    "altitude": {"data": [1600.0, 1650.0, 1710.0]},
                },
            )
        raise AssertionError(f"Unexpected Strava upstream request: {req.url.path}")

    with h.session_factory() as db:
        stats = process_pending_ingest_jobs(
            db,
            h.store,
            h.settings,
            http_client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(sync_worker_handler),
                follow_redirects=False,
            ),
        )
    assert stats["claimed"] == 2
    assert stats["succeeded"] == 2
    assert stats["failed"] == 0
    # Activity 70002 must NOT have triggered any Strava HTTP requests
    assert fetched_paths == ["/api/v3/activities/70001", "/api/v3/activities/70001/streams"]

    # Verify activity 70001 landed in explicit_proj_id ("Dolomites Week")
    with h.session_factory() as db:
        stage_70001 = db.scalar(select(Stage).where(Stage.strava_activity_id == 70001))
        assert stage_70001 is not None
        assert str(stage_70001.project_id) == explicit_proj_id

        # Verify _resolve_target_project_for_job rejects foreign project_id and invalid UUIDs
        other_proj = Project(owner_user_id=other.id, title="Foreign Trip", settings_json={})
        db.add(other_proj)
        db.commit()
        db.refresh(other_proj)

        fallback_foreign = _resolve_target_project_for_job(
            db, user.id, {"project_id": str(other_proj.id)}
        )
        assert fallback_foreign.title == "Strava Auto-Sync"
        assert fallback_foreign.owner_user_id == user.id

        fallback_bad_uuid = _resolve_target_project_for_job(
            db, user.id, {"project_id": "not-a-uuid"}
        )
        assert fallback_bad_uuid.id == fallback_foreign.id

        resolved_owned = _resolve_target_project_for_job(
            db, user.id, {"project_id": explicit_proj_id}
        )
        assert str(resolved_owned.id) == explicit_proj_id
