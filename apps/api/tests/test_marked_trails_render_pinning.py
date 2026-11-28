from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select
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


@dataclass
class _FakeStore:
    status: int = 200
    body: bytes = field(default_factory=lambda: json.dumps(_POINTER).encode())
    delay_s: float = 0.0
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
                self.send_header("Content-Length", str(len(state.body)))
                self.end_headers()
                self.wfile.write(state.body)
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


def _enqueue(h: AppHarness, sample_gpx: bytes, spec: dict[str, Any], key: str) -> RenderJob:
    h.create_user("pin@example.com")
    client, csrf = h.login_client("pin@example.com")
    headers = {"X-CSRF-Token": csrf}
    project_id = client.post("/api/v1/projects", json={"title": "Pin"}, headers=headers).json()[
        "id"
    ]
    uploaded = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("stage.gpx", sample_gpx, "application/gpx+xml")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    response = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json=spec,
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    with h.session_factory() as db:
        return db.scalars(select(RenderJob)).one()


def test_enqueue_pins_the_current_build_with_a_dev_base_resolved_against_the_render_url(
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
        "base_url": f"{origin}/tiles/marked-trails",
    }
    assert job.spec_json["marked_trail_types"] == ["hiking"]


def test_enqueue_keeps_an_https_base_unchanged(
    make_harness: HarnessFactory, sample_gpx: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_POINTER)

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
    assert pin.base_url == f"{origin}/tiles/marked-trails"
