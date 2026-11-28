from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from trip_api import marked_trails
from trip_api.db import RenderJob

if TYPE_CHECKING:
    from conftest import AppHarness, HarnessFactory

_POINTER = {
    "region": "europe/andorra",
    "current_build_id": "2026-11-27",
    "previous_build_id": None,
    "pmtiles_key": "builds/2026-11-27/marked-trails.pmtiles",
    "size_bytes": 1234,
    "sha256": "0" * 64,
    "osm_data_date": "2026-11-20T00:00:00Z",
    "built_at": "2026-11-27T10:00:00Z",
}


@pytest.fixture(autouse=True)
def _fresh_pin_cache() -> Iterator[None]:
    marked_trails.clear_pin_cache()
    yield
    marked_trails.clear_pin_cache()


@dataclass
class _FakeStore:
    status: int = 200
    body: bytes = field(default_factory=lambda: json.dumps(_POINTER).encode())
    delay_s: float = 0.0
    location: str | None = None
    drip_bytes: int = 0
    drip_interval_s: float = 0.0
    requests: list[str] = field(default_factory=list)


@pytest.fixture
def fake_store() -> Iterator[tuple[_FakeStore, str]]:
    state = _FakeStore()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(self.path)
            time.sleep(state.delay_s)
            try:
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                if state.location is not None:
                    self.send_header("Location", state.location)
                self.send_header("Content-Length", str(len(state.body) + state.drip_bytes))
                self.end_headers()
                self.wfile.write(state.body)
                for _ in range(state.drip_bytes):
                    self.wfile.flush()
                    time.sleep(state.drip_interval_s)
                    self.wfile.write(b" ")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _harness(make_harness: HarnessFactory, render_base_url: str, **overrides: Any) -> AppHarness:
    return make_harness(
        marked_trails_base_url=overrides.pop("marked_trails_base_url", "/tiles/marked-trails"),
        render_base_url=render_base_url,
        rate_limit_render_max=20,
        **overrides,
    )


class _Project:
    """One user and project of a harness, so a test can enqueue several renders."""

    def __init__(self, h: AppHarness, sample_gpx: bytes) -> None:
        self.h = h
        h.create_user("pin@example.com")
        self.client, csrf = h.login_client("pin@example.com")
        self.headers = {"X-CSRF-Token": csrf}
        self.project_id = self.client.post(
            "/api/v1/projects", json={"title": "Pin"}, headers=self.headers
        ).json()["id"]
        uploaded = self.client.post(
            f"/api/v1/projects/{self.project_id}/gpx",
            files={"file": ("stage.gpx", sample_gpx, "application/gpx+xml")},
            headers=self.headers,
        )
        assert uploaded.status_code == 201, uploaded.text

    def enqueue(self, spec: dict[str, Any], key: str) -> httpx.Response:
        response = self.client.post(
            f"/api/v1/projects/{self.project_id}/renders",
            json=spec,
            headers={**self.headers, "Idempotency-Key": key},
        )
        assert response.status_code == 202, response.text
        return response

    def quote(self, spec: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"/api/v1/projects/{self.project_id}/renders/quote", json=spec)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def finish(self, job_id: str) -> None:
        with self.h.session_factory() as db:
            job = db.get(RenderJob, uuid.UUID(job_id))
            assert job is not None
            out_key = f"outputs/{job.id}.mp4"
            self.h.store.put_bytes(out_key, b"fake-mp4-bytes")
            job.status = "succeeded"
            job.progress_pct = 100
            job.output_object_key = out_key
            job.expires_at = datetime.now(UTC) + timedelta(days=7)
            db.commit()

    def job(self, job_id: str) -> RenderJob:
        with self.h.session_factory() as db:
            job = db.get(RenderJob, uuid.UUID(job_id))
            assert job is not None
            return job


def _enqueue(h: AppHarness, sample_gpx: bytes, spec: dict[str, Any], key: str) -> RenderJob:
    response = _Project(h, sample_gpx).enqueue(spec, key)
    with h.session_factory() as db:
        job = db.get(RenderJob, uuid.UUID(response.json()["id"]))
        assert job is not None
        return job


def test_enqueue_pins_the_current_build_and_keeps_a_dev_base_as_configured(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    h = _harness(make_harness, origin)

    job = _enqueue(h, sample_gpx, {"marked_trail_types": ["hiking"]}, "pin-dev-0001")

    assert state.requests == ["/tiles/marked-trails/current.json"]
    assert job.spec_json["marked_trails_pin"] == {
        "build_id": "2026-11-27",
        "pmtiles_key": "builds/2026-11-27/marked-trails.pmtiles",
        "osm_data_date": "2026-11-20T00:00:00Z",
        "base_url": "/tiles/marked-trails",
    }
    assert job.spec_json["marked_trail_types"] == ["hiking"]


def test_enqueue_keeps_an_https_base_unchanged(
    make_harness: HarnessFactory, sample_gpx: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(_POINTER).encode()))

    monkeypatch.setattr(
        marked_trails,
        "_build_client",
        lambda timeout_s: httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout_s),
    )
    h = _harness(
        make_harness,
        "http://localhost:5173",
        marked_trails_base_url="https://trails.example.test/pointer/",
    )

    job = _enqueue(h, sample_gpx, {"marked_trail_types": ["cycling", "mtb"]}, "pin-https-001")

    assert seen == ["https://trails.example.test/pointer/current.json"]
    pin = job.spec_json["marked_trails_pin"]
    assert isinstance(pin, dict)
    assert pin["base_url"] == "https://trails.example.test/pointer"


@pytest.mark.parametrize("status", [403, 404, 500])
def test_enqueue_without_a_usable_pointer_stores_no_trail_data_and_still_queues(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    status: int,
) -> None:
    state, origin = fake_store
    state.status = status
    h = _harness(make_harness, origin)

    job = _enqueue(h, sample_gpx, {"marked_trail_types": ["hiking"]}, "pin-none-0001")

    assert job.status == "queued"
    assert "marked_trails_pin" not in job.spec_json


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"current_build_id": "2026-11-27"}',
        b'{"current_build_id": "x", "pmtiles_key": "../../etc/passwd"}',
    ],
)
def test_enqueue_ignores_an_invalid_pointer(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    body: bytes,
) -> None:
    state, origin = fake_store
    state.body = body
    h = _harness(make_harness, origin)

    job = _enqueue(h, sample_gpx, {"marked_trail_types": ["hiking"]}, "pin-bad-00001")

    assert "marked_trails_pin" not in job.spec_json


def test_enqueue_with_empty_trail_types_never_contacts_the_store(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    h = _harness(make_harness, origin)

    job = _enqueue(h, sample_gpx, {}, "pin-empty-0001")

    assert state.requests == []
    assert "marked_trails_pin" not in job.spec_json


def test_enqueue_without_a_configured_base_url_stores_no_trail_data(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    h = _harness(make_harness, origin, marked_trails_base_url="")

    job = _enqueue(h, sample_gpx, {"marked_trail_types": ["hiking"]}, "pin-unconf-001")

    assert state.requests == []
    assert "marked_trails_pin" not in job.spec_json


def test_enqueue_gives_up_on_a_slow_store_within_the_timeout(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, origin = fake_store
    state.delay_s = 2.0
    monkeypatch.setattr(marked_trails, "POINTER_TIMEOUT_S", 0.2)
    h = _harness(make_harness, origin)

    started = time.monotonic()
    job = _enqueue(h, sample_gpx, {"marked_trail_types": ["hiking"]}, "pin-slow-00001")

    assert time.monotonic() - started < 1.8
    assert job.status == "queued"
    assert "marked_trails_pin" not in job.spec_json


def test_client_supplied_pin_in_the_render_spec_is_not_stored(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    state.status = 404
    h = _harness(make_harness, origin)
    forged = {
        "build_id": "2020-01-01",
        "pmtiles_key": "builds/2020-01-01/marked-trails.pmtiles",
        "base_url": "https://evil.example",
    }

    job = _enqueue(
        h,
        sample_gpx,
        {"marked_trail_types": ["hiking"], "marked_trails_pin": forged},
        "pin-forged-001",
    )

    assert "marked_trails_pin" not in job.spec_json


def test_remote_dispatch_sends_the_pin_in_the_bundle_manifest(
    make_harness: HarnessFactory, fake_store: tuple[_FakeStore, str]
) -> None:
    from conftest import SAMPLE_GPX
    from test_remote_render_backend import _FakeRemoteBackend
    from trip_api.main import create_app
    from trip_api.remote_renders import process_remote_renders_once

    _state, origin = fake_store
    backend = _FakeRemoteBackend()
    h = _harness(
        make_harness,
        origin,
        render_backend="aws_sfn_lambda",
        render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
        render_aws_staging_bucket="trip-staging",
        render_remote_dispatch_interval_s=0.0,
    )
    h.app = create_app(
        h.settings,
        engine=h.engine,
        session_factory=h.session_factory,
        object_store=h.store,
        render_backend=backend,
    )

    _enqueue(h, SAMPLE_GPX, {"marked_trail_types": ["hiking"]}, "pin-remote-001")
    report = process_remote_renders_once(h.session_factory, h.store, h.settings, backend)

    assert report.dispatched == 1
    pin = backend.submitted[0].bundle.manifest.marked_trails
    assert pin is not None
    assert pin.build_id == "2026-11-27"
    assert pin.base_url == "/tiles/marked-trails"


def _publish(state: _FakeStore, build_id: str) -> None:
    pointer = {
        **_POINTER,
        "current_build_id": build_id,
        "pmtiles_key": f"builds/{build_id}/marked-trails.pmtiles",
    }
    state.body = json.dumps(pointer).encode()


_TRAILS = {"marked_trail_types": ["hiking"]}


def _expire_pin_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    real = marked_trails._now
    monkeypatch.setattr(marked_trails, "_now", lambda: real() + 3600.0)


def test_render_made_before_a_new_build_is_not_reused_after_it(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, origin = fake_store
    project = _Project(_harness(make_harness, origin), sample_gpx)
    first = project.enqueue(_TRAILS, "reuse-old-0001").json()["id"]
    project.finish(first)

    again = project.enqueue(_TRAILS, "reuse-same-001").json()
    assert again["status"] == "succeeded"
    assert project.quote(_TRAILS)["cached"] is True

    _publish(state, "2026-11-28")
    _expire_pin_cache(monkeypatch)

    assert project.quote(_TRAILS)["cached"] is False
    fresh = project.enqueue(_TRAILS, "reuse-new-0001").json()
    assert fresh["status"] == "queued"
    assert fresh["credits_charged"] > 0
    stored = project.job(fresh["id"]).spec_json["marked_trails_pin"]
    assert isinstance(stored, dict)
    assert stored["build_id"] == "2026-11-28"


def test_render_with_no_pin_is_not_reused_once_a_build_exists(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, origin = fake_store
    state.status = 404
    project = _Project(_harness(make_harness, origin), sample_gpx)
    first = project.enqueue(_TRAILS, "nopin-first-001").json()["id"]
    assert "marked_trails_pin" not in project.job(first).spec_json
    project.finish(first)

    state.status = 200
    _expire_pin_cache(monkeypatch)

    assert project.quote(_TRAILS)["cached"] is False
    assert project.enqueue(_TRAILS, "nopin-second-01").json()["status"] == "queued"


def test_a_reused_render_stores_the_pin_on_the_copied_job(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    _state, origin = fake_store
    project = _Project(_harness(make_harness, origin), sample_gpx)
    project.finish(project.enqueue(_TRAILS, "copy-first-0001").json()["id"])

    copy = project.enqueue(_TRAILS, "copy-second-001").json()

    assert copy["status"] == "succeeded"
    stored = project.job(copy["id"]).spec_json["marked_trails_pin"]
    assert isinstance(stored, dict)
    assert stored["build_id"] == "2026-11-27"


def test_quote_and_enqueue_share_the_cached_pointer(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    project = _Project(_harness(make_harness, origin), sample_gpx)
    project.quote(_TRAILS)
    _publish(state, "2026-11-28")

    job = project.enqueue(_TRAILS, "share-cache-001").json()

    assert state.requests == ["/tiles/marked-trails/current.json"]
    stored = project.job(job["id"]).spec_json["marked_trails_pin"]
    assert isinstance(stored, dict)
    assert stored["build_id"] == "2026-11-27"


def test_a_spec_without_trails_never_reads_the_pointer_and_keeps_its_hash() -> None:
    import hashlib

    from trip_api.routers.renders import _compute_content_hash
    from trip_telemetry import MarkedTrailsPin
    from trip_telemetry.schemas import RenderSpecV2

    spec = RenderSpecV2()
    pin = MarkedTrailsPin(
        build_id="2026-11-27",
        pmtiles_key="builds/2026-11-27/marked-trails.pmtiles",
        base_url="https://trails.example.test",
    )
    legacy = hashlib.sha256(
        json.dumps(
            {"spec": spec.model_dump(mode="json"), "stages": [], "photos": []},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert _compute_content_hash(spec, [], [], pin) == legacy
    assert _compute_content_hash(spec, [], [], None) == legacy

    with_trails = RenderSpecV2(marked_trail_types=["hiking"])
    assert _compute_content_hash(with_trails, [], [], pin) != _compute_content_hash(
        with_trails, [], [], None
    )
    other = pin.model_copy(
        update={
            "build_id": "2026-11-28",
            "pmtiles_key": "builds/2026-11-28/marked-trails.pmtiles",
        }
    )
    assert _compute_content_hash(with_trails, [], [], pin) != _compute_content_hash(
        with_trails, [], [], other
    )


def test_the_pointer_is_read_before_the_credit_lock_is_taken(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trip_api.routers import renders

    calls: list[str] = []
    real_get, real_lock = renders.get_marked_trails_pin, renders.lock_user_for_credit_update

    def get_spy(settings: Any) -> Any:
        calls.append("pointer")
        return real_get(settings)

    def lock_spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("lock")
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(renders, "get_marked_trails_pin", get_spy)
    monkeypatch.setattr(renders, "lock_user_for_credit_update", lock_spy)
    _state, origin = fake_store

    _enqueue(_harness(make_harness, origin), sample_gpx, _TRAILS, "order-spy-0001")

    assert calls == ["pointer", "lock"]


def test_the_pointer_request_does_not_follow_redirects(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    state.status = 302
    state.location = "/elsewhere/current.json"
    h = _harness(make_harness, origin)

    job = _enqueue(h, sample_gpx, _TRAILS, "redirect-0001")

    assert state.requests == ["/tiles/marked-trails/current.json"]
    assert "marked_trails_pin" not in job.spec_json


def test_a_pointer_over_64_kib_is_ignored(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    state, origin = fake_store
    state.body = json.dumps({**_POINTER, "padding": "x" * 70_000}).encode()
    h = _harness(make_harness, origin)

    job = _enqueue(h, sample_gpx, _TRAILS, "too-large-0001")

    assert job.status == "queued"
    assert "marked_trails_pin" not in job.spec_json


def test_a_slow_trickling_pointer_hits_the_total_deadline(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, origin = fake_store
    state.drip_bytes = 40
    state.drip_interval_s = 0.1
    monkeypatch.setattr(marked_trails, "POINTER_TIMEOUT_S", 0.5)
    h = _harness(make_harness, origin)

    started = time.monotonic()
    job = _enqueue(h, sample_gpx, _TRAILS, "trickle-00001")

    assert time.monotonic() - started < 3.0
    assert "marked_trails_pin" not in job.spec_json


@pytest.mark.parametrize("render_base_url", ["ftp://web.example.test", "http://[::1", "not a url"])
def test_an_invalid_render_base_url_means_no_pin_not_a_server_error(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    render_base_url: str,
) -> None:
    state, _origin = fake_store
    h = _harness(make_harness, render_base_url)

    job = _enqueue(h, sample_gpx, _TRAILS, "bad-render-001")

    assert state.requests == []
    assert job.status == "queued"
    assert "marked_trails_pin" not in job.spec_json


def test_an_httpx_invalid_url_error_means_no_pin(
    make_harness: HarnessFactory, sample_gpx: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_timeout_s: float) -> httpx.Client:
        raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")

    monkeypatch.setattr(marked_trails, "_build_client", broken)
    h = _harness(make_harness, "http://localhost:5173")

    job = _enqueue(h, sample_gpx, _TRAILS, "invalid-url-001")

    assert job.status == "queued"
    assert "marked_trails_pin" not in job.spec_json


def test_the_pointer_request_asks_for_an_identity_encoding(
    make_harness: HarnessFactory, sample_gpx: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(_POINTER).encode()))

    monkeypatch.setattr(
        marked_trails,
        "_build_client",
        lambda timeout_s: httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout_s),
    )
    h = _harness(make_harness, "http://localhost:5173", marked_trails_base_url="https://t.example")

    _enqueue(h, sample_gpx, _TRAILS, "identity-0001")

    assert seen == ["identity"]


@pytest.mark.parametrize(
    "pointer",
    [
        {**_POINTER, "current_build_id": "2026-11-27/../x"},
        {**_POINTER, "pmtiles_key": "builds/2026-11-26/marked-trails.pmtiles"},
        {**_POINTER, "osm_data_date": "x" * 41},
        {**_POINTER, "osm_data_date": "yesterday"},
    ],
)
def test_a_pointer_that_fails_the_pin_constraints_is_ignored(
    make_harness: HarnessFactory,
    sample_gpx: bytes,
    fake_store: tuple[_FakeStore, str],
    pointer: dict[str, Any],
) -> None:
    state, origin = fake_store
    state.body = json.dumps(pointer).encode()

    job = _enqueue(_harness(make_harness, origin), sample_gpx, _TRAILS, "constraints-01")

    assert "marked_trails_pin" not in job.spec_json


def test_the_render_job_response_does_not_expose_the_pin(
    make_harness: HarnessFactory, sample_gpx: bytes, fake_store: tuple[_FakeStore, str]
) -> None:
    _state, origin = fake_store
    project = _Project(_harness(make_harness, origin), sample_gpx)

    created = project.enqueue(_TRAILS, "response-0001").json()
    fetched = project.client.get(f"/api/v1/renders/{created['id']}").json()

    assert "marked_trails_pin" not in created["spec"]
    assert "marked_trails_pin" not in fetched["spec"]
    assert created["spec"]["marked_trail_types"] == ["hiking"]
    assert "marked_trails_pin" in project.job(created["id"]).spec_json
