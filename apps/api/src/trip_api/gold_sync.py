"""Synchronize Gold DuckDB analytics database from S3 to local ephemeral storage."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from botocore.exceptions import ClientError

from trip_api.config import Settings
from trip_api.storage import ObjectStore, S3ObjectStore, _is_not_found_client_error

logger = logging.getLogger(__name__)

_STREAM_CHUNK_BYTES = 64 * 1024


@dataclass(slots=True)
class _GoldSyncState:
    last_check_mono: float
    cached_etag: str | None


_sync_lock = threading.Lock()
_sync_states: dict[tuple[str, str, Path], _GoldSyncState] = {}


def reset_gold_sync_cache() -> None:
    """Clear in-process Gold DuckDB ETag/TTL sync state (intended for tests)."""
    with _sync_lock:
        _sync_states.clear()


def _write_stream_to_file(body_stream: Any, dest_path: Path) -> None:
    iter_chunks = getattr(body_stream, "iter_chunks", None)
    with dest_path.open("wb") as fh:
        if callable(iter_chunks):
            for chunk in iter_chunks(chunk_size=_STREAM_CHUNK_BYTES):
                if chunk:
                    fh.write(chunk)
        else:
            data = body_stream.read()
            if data:
                fh.write(data)


def ensure_local_gold_duckdb(store: ObjectStore, settings: Settings) -> Path:
    """Return path to local Gold DuckDB, syncing from S3 on ETag changes when configured."""
    target_path = Path(settings.gold_output_dir) / "trip_analytics.duckdb"
    if settings.object_store_backend != "s3":
        return target_path

    if not isinstance(store, S3ObjectStore):
        s3_client = getattr(store, "client", None)
        s3_bucket = getattr(store, "bucket", None)
        if s3_client is None or not s3_bucket:
            return target_path
    else:
        s3_client = store.client
        s3_bucket = store.bucket

    if target_path.parent.exists() and not os.access(target_path.parent, os.W_OK):
        # Preserve read-only shared volume mounts (e.g., Docker Compose gold_data:/data/gold:ro).
        return target_path

    cache_key = (str(s3_bucket), settings.gold_s3_key, target_path)

    with _sync_lock:
        now = time.monotonic()
        state = _sync_states.get(cache_key)
        if (
            target_path.is_file()
            and settings.gold_sync_interval_s > 0.0
            and state is not None
            and (now - state.last_check_mono) < settings.gold_sync_interval_s
        ):
            return target_path

        try:
            head = s3_client.head_object(Bucket=str(s3_bucket), Key=settings.gold_s3_key)
        except ClientError as exc:
            if _is_not_found_client_error(exc):
                _sync_states[cache_key] = _GoldSyncState(
                    last_check_mono=time.monotonic(),
                    cached_etag=None,
                )
                return target_path
            logger.warning(
                "Gold DuckDB S3 head_object failed (%s); using local path if present",
                type(exc).__name__,
            )
            return target_path
        except Exception as exc:
            logger.warning(
                "Gold DuckDB S3 head_object failed (%s); using local path if present",
                type(exc).__name__,
            )
            return target_path

        remote_etag = str(head.get("ETag") or "").strip() or None
        if (
            target_path.is_file()
            and state is not None
            and remote_etag is not None
            and state.cached_etag == remote_etag
        ):
            state.last_check_mono = time.monotonic()
            return target_path

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.parent / f".trip_analytics.duckdb.tmp.{uuid.uuid4().hex}"
        body_stream: Any = None
        try:
            resp = s3_client.get_object(Bucket=str(s3_bucket), Key=settings.gold_s3_key)
            body_stream = resp["Body"]
            _write_stream_to_file(body_stream, tmp_path)

            con = duckdb.connect(str(tmp_path), read_only=True)
            try:
                con.execute("SELECT 1").fetchone()
            finally:
                con.close()

            os.replace(tmp_path, target_path)
            resolved_etag = remote_etag or (str(resp.get("ETag") or "").strip() or None)
            _sync_states[cache_key] = _GoldSyncState(
                last_check_mono=time.monotonic(),
                cached_etag=resolved_etag,
            )
        except ClientError as exc:
            if _is_not_found_client_error(exc):
                _sync_states[cache_key] = _GoldSyncState(
                    last_check_mono=time.monotonic(),
                    cached_etag=None,
                )
            else:
                logger.warning(
                    "Gold DuckDB S3 get_object failed (%s); serving stale local file if present",
                    type(exc).__name__,
                )
        except Exception as exc:
            logger.warning(
                "Gold DuckDB sync failed (%s); serving stale local file if present",
                type(exc).__name__,
            )
        finally:
            if body_stream is not None:
                close_fn = getattr(body_stream, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception as close_exc:
                        logger.debug(
                            "Ignoring Gold DuckDB S3 stream close error: %s",
                            type(close_exc).__name__,
                        )
            tmp_path.unlink(missing_ok=True)

    return target_path
