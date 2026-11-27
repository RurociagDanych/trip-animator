from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def _to_psycopg_dsn(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix("postgresql+psycopg://")
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url.removeprefix("postgres://")
    return database_url


class RenderEventHub:
    def __init__(
        self,
        database_url: str,
        *,
        reconnect_base_s: float = 0.5,
        reconnect_max_s: float = 5.0,
    ) -> None:
        self._database_url = database_url
        self._dsn = _to_psycopg_dsn(database_url)
        self._reconnect_base_s = reconnect_base_s
        self._reconnect_max_s = reconnect_max_s
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected_once = False

    @property
    def is_postgres(self) -> bool:
        return self._database_url.startswith("postgresql")

    @property
    def subscriber_count(self) -> int:
        return sum(len(queues) for queues in self._subscribers.values())

    def _enqueue_nonblocking(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        if queue.full():
            with suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with suppress(asyncio.QueueFull):
            queue.put_nowait(payload)

    def publish_local(self, job_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
        if job_id is None:
            for queues in list(self._subscribers.values()):
                for queue in list(queues):
                    self._enqueue_nonblocking(queue, payload)
            return
        for queue in list(self._subscribers.get(job_id, ())):
            self._enqueue_nonblocking(queue, payload)

    def _ensure_listener_started(self) -> None:
        if self._closed or not self.is_postgres:
            return
        if self._listener_task is None or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._listener_loop())

    async def _listener_loop(self) -> None:
        delay = self._reconnect_base_s
        while not self._closed:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._dsn,
                    autocommit=True,
                ) as aconn:
                    await aconn.execute("LISTEN render_job_events")
                    if self._connected_once:
                        self.publish_local(None, {"event": "reconnect"})
                    self._connected_once = True
                    delay = self._reconnect_base_s
                    async for notify in aconn.notifies():
                        if self._closed:
                            return
                        target_job_id: uuid.UUID | None = None
                        parsed_payload: dict[str, Any] = {"event": "notify"}
                        try:
                            msg = json.loads(notify.payload)
                            if isinstance(msg, dict):
                                parsed_payload = msg
                                raw_id = msg.get("job_id")
                                if isinstance(raw_id, str):
                                    target_job_id = uuid.UUID(raw_id)
                        except Exception:
                            target_job_id = None
                        self.publish_local(target_job_id, parsed_payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("RenderEventHub PG LISTEN reconnecting after error: %s", exc)
                self.publish_local(None, {"event": "reconnect"})
                await asyncio.sleep(delay)
                delay = min(self._reconnect_max_s, delay * 2.0)

    @asynccontextmanager
    async def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self._subscribers.setdefault(job_id, set()).add(queue)
        self._ensure_listener_started()
        try:
            yield queue
        finally:
            bucket = self._subscribers.get(job_id)
            if bucket is not None:
                bucket.discard(queue)
                if not bucket:
                    self._subscribers.pop(job_id, None)

    async def close(self) -> None:
        self._closed = True
        task = self._listener_task
        self._listener_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._subscribers.clear()
