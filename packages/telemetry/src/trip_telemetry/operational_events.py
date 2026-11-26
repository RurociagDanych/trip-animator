import json
import socket
from os import getenv
from queue import Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from uuid import UUID

_VALID_RENDER_OUTCOMES = frozenset(
    {"succeeded", "failed", "cancelled", "lost_lease", "queued", "error"}
)
_WARN_RENDER_OUTCOMES = frozenset({"cancelled", "lost_lease", "queued"})


class _Sender:
    def __init__(self, port: int) -> None:
        self._port = port
        self._queue: Queue[tuple[str, bytes]] = Queue(maxsize=128)
        self._lock = Lock()
        self._started = False

    def _run(self) -> None:
        retry_at = 0.0
        while True:
            host, packet = self._queue.get()
            if monotonic() < retry_at:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.setblocking(False)
                    client.sendto(packet, (host, self._port))
            except OSError:
                retry_at = monotonic() + 5

    def emit(self, fields: dict[str, str | int]) -> None:
        collector = getenv("OPERATIONAL_EVENTS_HOST")
        if not collector:
            return
        if not self._started:
            with self._lock:
                if not self._started:
                    try:
                        Thread(
                            target=self._run,
                            name=f"operational-events-{self._port}",
                            daemon=True,
                        ).start()
                    except RuntimeError:
                        return
                    self._started = True
        packet = b"<14>1 - - trip-animator - - - " + json.dumps(fields).encode()
        try:
            self._queue.put_nowait((collector, packet))
        except Full:
            pass


_api = _Sender(1514)
_worker = _Sender(1515)
_ingest = _Sender(1516)
_SENDERS = {"api": _api, "worker": _worker, "ingest": _ingest}


def record_api_failure(route_template: str | None, status: int) -> None:
    _api.emit(
        {
            "level": "error",
            "event": "http_server_error",
            "route": route_template or "unmatched",
            "status": status,
        }
    )


def record_render_outcome(job_id: UUID, outcome: str) -> None:
    if outcome not in _VALID_RENDER_OUTCOMES:
        outcome = "other"
    if outcome == "succeeded":
        level = "info"
    elif outcome in _WARN_RENDER_OUTCOMES:
        level = "warn"
    else:
        level = "error"
    _worker.emit(
        {
            "level": level,
            "event": "render_attempt_finished",
            "job_id": str(job_id),
            "outcome": outcome,
        }
    )


def record_ingest_outcome(
    *,
    claimed: int,
    succeeded: int,
    ignored: int,
    failed: int,
    deferred: int = 0,
) -> None:
    level = "error" if failed > 0 else "info"
    _ingest.emit(
        {
            "level": level,
            "event": "ingest_batch_finished",
            "claimed": claimed,
            "succeeded": succeeded,
            "ignored": ignored,
            "failed": failed,
            "deferred": deferred,
        }
    )


def record_ingest_error(error_type: str) -> None:
    safe_type = error_type.strip()[:64] or "Exception"
    _ingest.emit(
        {
            "level": "error",
            "event": "ingest_loop_error",
            "error_type": safe_type,
        }
    )


def record_service_started(service: str) -> None:
    sender = _SENDERS.get(service)
    if sender is not None:
        sender.emit({"level": "info", "event": "service_started"})


def record_service_heartbeat(service: str) -> None:
    sender = _SENDERS.get(service)
    if sender is not None:
        sender.emit({"level": "info", "event": "service_heartbeat"})


def start_service_heartbeat(
    service: str,
    *,
    interval_seconds: float = 300.0,
    stop_event: Event | None = None,
) -> Event:
    stop = stop_event if stop_event is not None else Event()
    if service not in _SENDERS or not getenv("OPERATIONAL_EVENTS_HOST"):
        return stop
    record_service_started(service)
    wait_seconds = max(1.0, interval_seconds)

    def _loop() -> None:
        while not stop.wait(wait_seconds):
            record_service_heartbeat(service)

    try:
        Thread(
            target=_loop,
            name=f"operational-heartbeat-{service}",
            daemon=True,
        ).start()
    except RuntimeError:
        pass
    return stop
