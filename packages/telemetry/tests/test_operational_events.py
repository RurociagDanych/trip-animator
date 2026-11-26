from __future__ import annotations

import json
import socket
from threading import Event
from uuid import UUID

import pytest
from trip_telemetry import operational_events


def _drain_udp(sock: socket.socket, expected: int) -> list[dict[str, str | int]]:
    sock.settimeout(2.0)
    messages: list[dict[str, str | int]] = []
    for _ in range(expected):
        raw, _addr = sock.recvfrom(4096)
        prefix = b"<14>1 - - trip-animator - - - "
        assert raw.startswith(prefix)
        messages.append(json.loads(raw[len(prefix) :].decode()))
    return messages


def test_operational_events_include_low_cardinality_level_and_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, str | int]]] = []
    monkeypatch.setattr(
        operational_events._api, "emit", lambda fields: emitted.append(("api", fields))
    )
    monkeypatch.setattr(
        operational_events._worker, "emit", lambda fields: emitted.append(("worker", fields))
    )
    monkeypatch.setattr(
        operational_events._ingest, "emit", lambda fields: emitted.append(("ingest", fields))
    )

    job_id = UUID(int=42)
    operational_events.record_api_failure("/api/v1/projects/{project_id}", 503)
    operational_events.record_api_failure(None, 500)
    operational_events.record_render_outcome(job_id, "succeeded")
    operational_events.record_render_outcome(job_id, "cancelled")
    operational_events.record_render_outcome(job_id, "failed")
    operational_events.record_render_outcome(job_id, "unexpected-value")
    operational_events.record_ingest_outcome(claimed=3, succeeded=2, ignored=1, failed=0)
    operational_events.record_ingest_outcome(
        claimed=2, succeeded=0, ignored=0, failed=1, deferred=1
    )
    operational_events.record_ingest_error("OperationalError")
    operational_events.record_service_started("api")
    operational_events.record_service_heartbeat("ingest")

    assert emitted == [
        (
            "api",
            {
                "level": "error",
                "event": "http_server_error",
                "route": "/api/v1/projects/{project_id}",
                "status": 503,
            },
        ),
        (
            "api",
            {
                "level": "error",
                "event": "http_server_error",
                "route": "unmatched",
                "status": 500,
            },
        ),
        (
            "worker",
            {
                "level": "info",
                "event": "render_attempt_finished",
                "job_id": str(job_id),
                "outcome": "succeeded",
            },
        ),
        (
            "worker",
            {
                "level": "warn",
                "event": "render_attempt_finished",
                "job_id": str(job_id),
                "outcome": "cancelled",
            },
        ),
        (
            "worker",
            {
                "level": "error",
                "event": "render_attempt_finished",
                "job_id": str(job_id),
                "outcome": "failed",
            },
        ),
        (
            "worker",
            {
                "level": "error",
                "event": "render_attempt_finished",
                "job_id": str(job_id),
                "outcome": "other",
            },
        ),
        (
            "ingest",
            {
                "level": "info",
                "event": "ingest_batch_finished",
                "claimed": 3,
                "succeeded": 2,
                "ignored": 1,
                "failed": 0,
                "deferred": 0,
            },
        ),
        (
            "ingest",
            {
                "level": "error",
                "event": "ingest_batch_finished",
                "claimed": 2,
                "succeeded": 0,
                "ignored": 0,
                "failed": 1,
                "deferred": 1,
            },
        ),
        (
            "ingest",
            {
                "level": "error",
                "event": "ingest_loop_error",
                "error_type": "OperationalError",
            },
        ),
        ("api", {"level": "info", "event": "service_started"}),
        ("ingest", {"level": "info", "event": "service_heartbeat"}),
    ]


def test_start_service_heartbeat_skips_when_collector_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPERATIONAL_EVENTS_HOST", raising=False)
    emitted: list[dict[str, str | int]] = []
    monkeypatch.setattr(operational_events._api, "emit", emitted.append)

    stop = operational_events.start_service_heartbeat("api", interval_seconds=1.0)
    stop.set()
    assert emitted == []


def test_sender_delivers_rfc5424_udp_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        port = receiver.getsockname()[1]
        sender = operational_events._Sender(port)
        monkeypatch.setenv("OPERATIONAL_EVENTS_HOST", "127.0.0.1")
        monkeypatch.setitem(operational_events._SENDERS, "ingest", sender)

        stop = Event()
        operational_events.start_service_heartbeat("ingest", interval_seconds=60.0, stop_event=stop)
        operational_events.record_service_heartbeat("ingest")
        try:
            payloads = _drain_udp(receiver, 2)
        finally:
            stop.set()

    assert payloads == [
        {"level": "info", "event": "service_started"},
        {"level": "info", "event": "service_heartbeat"},
    ]
