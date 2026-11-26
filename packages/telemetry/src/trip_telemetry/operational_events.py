"""Send reviewed operational events without coupling product work to the collector."""

import json
import socket
from os import getenv
from queue import Full, Queue
from threading import Lock, Thread
from time import monotonic
from uuid import UUID

_COLLECTOR = getenv("OPERATIONAL_EVENTS_HOST")


class _Sender:
    def __init__(self, port: int) -> None:
        self._port = port
        self._queue: Queue[bytes] = Queue(maxsize=128)
        self._lock = Lock()
        self._started = False

    def _run(self, host: str) -> None:
        retry_at = 0.0
        while True:
            packet = self._queue.get()
            if monotonic() < retry_at:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.setblocking(False)
                    client.sendto(packet, (host, self._port))
            except OSError:
                retry_at = monotonic() + 5

    def emit(self, fields: dict[str, str | int]) -> None:
        if not _COLLECTOR:
            return
        if not self._started:
            with self._lock:
                if not self._started:
                    try:
                        Thread(
                            target=self._run,
                            args=(_COLLECTOR,),
                            name="operational-events",
                            daemon=True,
                        ).start()
                    except RuntimeError:
                        return
                    self._started = True
        packet = b"<14>1 - - trip-animator - - - " + json.dumps(fields).encode()
        try:
            self._queue.put_nowait(packet)
        except Full:
            pass


_api = _Sender(1514)
_worker = _Sender(1515)


def record_api_failure(route_template: str | None, status: int) -> None:
    """Send a server error using only its registered route and numeric HTTP status."""
    _api.emit(
        {"event": "http_server_error", "route": route_template or "unmatched", "status": status}
    )


def record_render_outcome(job_id: UUID, outcome: str) -> None:
    """Send the outcome of a render attempt without the exception or render inputs."""
    if outcome not in {"succeeded", "failed", "cancelled", "lost_lease", "queued", "error"}:
        outcome = "other"
    _worker.emit({"event": "render_attempt_finished", "job_id": str(job_id), "outcome": outcome})
