from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from trip_telemetry import RenderManifest, sign_render_callback

from trip_worker.config import WorkerSettings
from trip_worker.encoder import (
    ProbeResult,
    concat_mp4_chunks,
    extract_thumbnail,
    verify_mp4_output,
)
from trip_worker.local_spa import LocalSpaServer
from trip_worker.logging_setup import configure_logging
from trip_worker.renderer import probe_manifest_frame_count, render_manifest_to_mp4
from trip_worker.ssrf import (
    build_external_request_kwargs,
    build_pinned_external_http_client,
    pin_external_target,
    resolve_and_validate_external_target_async,
)
from trip_worker.storage import ObjectStore, S3ObjectStore, build_object_store

logger = logging.getLogger(__name__)

CallbackPostFn = Callable[[str, dict[str, str], bytes], None]

_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")
_CRED_URL_RE = re.compile(r"://[^/\s@]+@")


def _sanitize_error_text(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return "Remote render failed"
    first_line = text.splitlines()[0].strip()
    redacted = _CRED_URL_RE.sub("://[redacted]@", first_line)
    redacted = _PATH_RE.sub("[path]", redacted)
    return redacted[:300] or "Remote render failed"


async def _post_callback_pinned(url: str, headers: dict[str, str], body: bytes) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Render callback URL must use https:// with a valid hostname")
    host = parsed.hostname.strip().lower()
    decision = await resolve_and_validate_external_target_async(
        url,
        render_origin="https://callback-origin.invalid",
        allowed_external_hosts=frozenset({host}),
        resolve_dns=True,
    )
    if not decision.allowed or decision.target is None:
        raise ValueError("Render callback target rejected by SSRF guard")
    pinned_kwargs = build_external_request_kwargs(decision.target)
    merged_headers = {**pinned_kwargs["headers"], **headers}
    async with build_pinned_external_http_client(resolve_dns=True, timeout=15.0) as client:
        with pin_external_target(decision.target):
            resp = await client.post(
                url,
                headers=merged_headers,
                content=body,
                extensions=pinned_kwargs["extensions"],
            )
            resp.raise_for_status()


def _default_post_callback(url: str, headers: dict[str, str], body: bytes) -> None:
    asyncio.run(_post_callback_pinned(url, headers, body))


def _send_callback_best_effort(
    *,
    callback_url: str | None,
    callback_secret: str | None,
    payload: dict[str, Any],
    post_fn: CallbackPostFn | None = None,
) -> None:
    url = (callback_url or "").strip()
    secret = (
        callback_secret
        if callback_secret is not None
        else os.environ.get("RENDER_CALLBACK_SECRET", "")
    ).strip()
    if not url or not secret:
        return
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-Trip-Timestamp": str(ts),
        "X-Trip-Signature": sign_render_callback(secret, ts, raw_body),
    }
    sender = post_fn or _default_post_callback
    try:
        sender(url, headers, raw_body)
    except Exception as exc:
        logger.warning("Best-effort render callback to %s failed: %s", url, exc)


def _resolve_store(event: Mapping[str, Any], settings: WorkerSettings) -> ObjectStore:
    bucket = str(event.get("staging_bucket") or "").strip()
    if bucket:
        region = os.environ.get("AWS_REGION") or settings.s3_region
        return S3ObjectStore(
            endpoint_url=os.environ.get("RENDER_AWS_ENDPOINT_URL") or None,
            access_key_id=None,
            secret_access_key=None,
            bucket=bucket,
            region=region,
        )
    return build_object_store(settings)


def _load_manifest(store: ObjectStore, manifest_key: str) -> RenderManifest:
    raw = store.get_bytes(manifest_key)
    return RenderManifest.model_validate_json(raw.decode("utf-8"))


def _load_photos(store: ObjectStore, photo_keys: Mapping[str, Any] | None) -> dict[str, bytes]:
    assets: dict[str, bytes] = {}
    if not photo_keys:
        return assets
    for photo_id, obj_key in photo_keys.items():
        assets[str(photo_id)] = store.get_bytes(str(obj_key))
    return assets


def _default_frame_count(manifest: RenderManifest, settings: WorkerSettings) -> int:
    return asyncio.run(probe_manifest_frame_count(manifest, settings))


def _with_local_spa_if_configured(
    settings: WorkerSettings,
    stack: ExitStack,
) -> WorkerSettings:
    spa_dir = (settings.worker_local_spa_dir or "").strip()
    if not spa_dir:
        return settings
    server = stack.enter_context(LocalSpaServer(Path(spa_dir)))
    return settings.model_copy(update={"render_base_url": server.base_url})


def _probe_to_dict(probe: ProbeResult) -> dict[str, Any]:
    return {
        "codec_name": probe.codec_name,
        "pix_fmt": probe.pix_fmt,
        "width": probe.width,
        "height": probe.height,
        "duration_s": probe.duration_s,
        "nb_frames": probe.nb_frames,
    }


def _invoke_render_fn(render_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = render_fn(*args, **kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _verify_thumb_upload_and_notify(
    *,
    job_id: str,
    manifest: RenderManifest,
    mp4_path: Path,
    thumb_path: Path,
    output_key: str,
    thumbnail_key: str,
    settings: WorkerSettings,
    store: ObjectStore,
    verify_fn: Callable[..., ProbeResult],
    thumb_fn: Callable[..., Path],
    callback_url: str,
    callback_secret: str | None,
    callback_post_fn: CallbackPostFn | None,
) -> dict[str, Any]:
    probe = verify_fn(
        mp4_path,
        expected_width=manifest.width,
        expected_height=manifest.height,
        expected_duration_s=manifest.duration_s,
        duration_tolerance_s=max(1.5, manifest.duration_s * 0.25),
        timeout_seconds=settings.ffprobe_timeout_seconds,
    )
    thumb_fn(
        mp4_path,
        thumb_path,
        seek_seconds=min(0.5, manifest.duration_s * 0.25),
        width=min(640, manifest.width),
        timeout_seconds=settings.ffmpeg_timeout_seconds,
    )
    store.put_file(output_key, mp4_path, content_type="video/mp4")
    store.put_bytes(thumbnail_key, thumb_path.read_bytes(), content_type="image/webp")

    probe_dict = _probe_to_dict(probe)
    _send_callback_best_effort(
        callback_url=callback_url,
        callback_secret=callback_secret,
        payload={
            "job_id": job_id,
            "event": "succeeded",
            "progress_pct": 100,
            "output_key": output_key,
            "thumbnail_key": thumbnail_key,
            "probe": probe_dict,
        },
        post_fn=callback_post_fn,
    )
    return {
        "job_id": job_id,
        "output_key": output_key,
        "thumbnail_key": thumbnail_key,
        "probe": probe_dict,
    }


def _handle_prepare(
    event: Mapping[str, Any],
    *,
    store: ObjectStore,
    settings: WorkerSettings,
    frame_count_fn: Callable[[RenderManifest, WorkerSettings], int],
) -> dict[str, Any]:
    manifest_key = str(event["manifest_key"])
    manifest = _load_manifest(store, manifest_key)
    total_frames = manifest.total_frames

    if manifest.spec.photo_card_style == "cinematic_stop" and manifest.photos:
        with ExitStack() as stack:
            effective = _with_local_spa_if_configured(settings, stack)
            probed = int(frame_count_fn(manifest, effective))
        if probed > 0 and probed != total_frames:
            total_frames = probed
            manifest = manifest.model_copy(
                update={
                    "total_frames": total_frames,
                    "duration_s": round(total_frames / max(1, manifest.fps), 4),
                }
            )
            store.put_bytes(
                manifest_key,
                manifest.model_dump_json().encode("utf-8"),
                content_type="application/json",
            )

    threshold = max(1, int(event.get("chunk_threshold_frames", 300)))
    chunk_size = max(1, int(event.get("chunk_size_frames", 200)))
    preroll = max(0, int(event.get("preroll_frames", 15)))

    base = dict(event)
    if total_frames <= threshold:
        return {
            **base,
            "mode": "single",
            "total_frames": total_frames,
            "total_chunks": 1,
            "chunks": [],
        }

    chunks: list[dict[str, int]] = []
    chunk_idx = 0
    for start in range(0, total_frames, chunk_size):
        end = min(total_frames, start + chunk_size)
        chunks.append(
            {
                "chunk_index": chunk_idx,
                "start_frame": start,
                "end_frame": end,
                "preroll_frames": 0 if start == 0 else preroll,
            }
        )
        chunk_idx += 1

    return {
        **base,
        "mode": "chunked",
        "total_frames": total_frames,
        "total_chunks": len(chunks),
        "chunks": chunks,
    }


def _handle_render_single(
    event: Mapping[str, Any],
    *,
    store: ObjectStore,
    settings: WorkerSettings,
    render_fn: Callable[..., Any],
    verify_fn: Callable[..., ProbeResult],
    thumb_fn: Callable[..., Path],
    callback_secret: str | None,
    callback_post_fn: CallbackPostFn | None,
) -> dict[str, Any]:
    job_id = str(event["job_id"])
    manifest = _load_manifest(store, str(event["manifest_key"]))
    photo_assets = _load_photos(store, event.get("photo_keys"))

    with ExitStack() as stack:
        effective = _with_local_spa_if_configured(settings, stack)
        tmpdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="trip_lambda_")))
        mp4_path = tmpdir / f"{job_id}.mp4"
        thumb_path = tmpdir / f"{job_id}.webp"

        _invoke_render_fn(
            render_fn,
            manifest,
            mp4_path,
            effective,
            photo_assets=photo_assets,
        )
        return _verify_thumb_upload_and_notify(
            job_id=job_id,
            manifest=manifest,
            mp4_path=mp4_path,
            thumb_path=thumb_path,
            output_key=str(event["output_key"]),
            thumbnail_key=str(event["thumbnail_key"]),
            settings=effective,
            store=store,
            verify_fn=verify_fn,
            thumb_fn=thumb_fn,
            callback_url=str(event.get("callback_url") or ""),
            callback_secret=callback_secret,
            callback_post_fn=callback_post_fn,
        )


def _handle_render_chunk(
    event: Mapping[str, Any],
    *,
    store: ObjectStore,
    settings: WorkerSettings,
    render_fn: Callable[..., Any],
    callback_secret: str | None,
    callback_post_fn: CallbackPostFn | None,
) -> dict[str, Any]:
    job_id = str(event["job_id"])
    chunk_index = int(event["chunk_index"])
    start_frame = int(event["start_frame"])
    end_frame = int(event["end_frame"])
    preroll_frames = int(event.get("preroll_frames", 0))
    total_chunks = max(1, int(event.get("total_chunks", 1)))
    staging_prefix = str(event.get("staging_prefix") or f"staging/{job_id}").rstrip("/")
    chunk_key = f"{staging_prefix}/chunks/chunk_{chunk_index:04d}.mp4"

    manifest = _load_manifest(store, str(event["manifest_key"]))
    photo_assets = _load_photos(store, event.get("photo_keys"))

    with ExitStack() as stack:
        effective = _with_local_spa_if_configured(settings, stack)
        tmpdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="trip_chunk_")))
        chunk_mp4 = tmpdir / f"chunk_{chunk_index:04d}.mp4"
        _invoke_render_fn(
            render_fn,
            manifest,
            chunk_mp4,
            effective,
            frame_start=start_frame,
            frame_end=end_frame,
            preroll_frames=preroll_frames,
            photo_assets=photo_assets,
            gop_aligned=True,
        )
        store.put_file(chunk_key, chunk_mp4, content_type="video/mp4")

    _send_callback_best_effort(
        callback_url=str(event.get("callback_url") or ""),
        callback_secret=callback_secret,
        payload={
            "job_id": job_id,
            "event": "running",
            "chunk_done": 1,
            "total_chunks": total_chunks,
        },
        post_fn=callback_post_fn,
    )
    return {
        "chunk_index": chunk_index,
        "chunk_key": chunk_key,
    }


def _handle_finalize(
    event: Mapping[str, Any],
    *,
    store: ObjectStore,
    settings: WorkerSettings,
    concat_fn: Callable[..., Path],
    verify_fn: Callable[..., ProbeResult],
    thumb_fn: Callable[..., Path],
    callback_secret: str | None,
    callback_post_fn: CallbackPostFn | None,
) -> dict[str, Any]:
    job_id = str(event["job_id"])
    manifest = _load_manifest(store, str(event["manifest_key"]))
    raw_chunks = list(event.get("chunk_results") or [])
    ordered_chunks = sorted(raw_chunks, key=lambda item: int(item["chunk_index"]))
    if not ordered_chunks:
        raise ValueError("Cannot finalize remote render without chunk_results")

    with tempfile.TemporaryDirectory(prefix="trip_finalize_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        chunk_paths: list[Path] = []
        for item in ordered_chunks:
            idx = int(item["chunk_index"])
            key = str(item["chunk_key"])
            c_path = tmpdir / f"chunk_{idx:04d}.mp4"
            c_path.write_bytes(store.get_bytes(key))
            chunk_paths.append(c_path)

        mp4_path = tmpdir / f"{job_id}.mp4"
        thumb_path = tmpdir / f"{job_id}.webp"
        concat_fn(chunk_paths, mp4_path, timeout_seconds=settings.ffmpeg_timeout_seconds)
        return _verify_thumb_upload_and_notify(
            job_id=job_id,
            manifest=manifest,
            mp4_path=mp4_path,
            thumb_path=thumb_path,
            output_key=str(event["output_key"]),
            thumbnail_key=str(event["thumbnail_key"]),
            settings=settings,
            store=store,
            verify_fn=verify_fn,
            thumb_fn=thumb_fn,
            callback_url=str(event.get("callback_url") or ""),
            callback_secret=callback_secret,
            callback_post_fn=callback_post_fn,
        )


def _handle_report_failure(
    event: Mapping[str, Any],
    *,
    callback_secret: str | None,
    callback_post_fn: CallbackPostFn | None,
) -> dict[str, Any]:
    job_id = str(event.get("job_id") or "")
    raw_error = event.get("error_detail") or event.get("cause") or event.get("error")
    if isinstance(raw_error, Mapping):
        message = _sanitize_error_text(raw_error.get("Error") or "Remote render failed")
    else:
        message = _sanitize_error_text(raw_error)
    _send_callback_best_effort(
        callback_url=str(event.get("callback_url") or ""),
        callback_secret=callback_secret,
        payload={
            "job_id": job_id,
            "event": "failed",
            "error_message": message,
        },
        post_fn=callback_post_fn,
    )
    return {"job_id": job_id, "reported": True, "error_message": message}


def handle_event(
    event: Mapping[str, Any],
    *,
    store: ObjectStore | None = None,
    settings: WorkerSettings | None = None,
    render_fn: Callable[..., Any] = render_manifest_to_mp4,
    frame_count_fn: Callable[[RenderManifest, WorkerSettings], int] = _default_frame_count,
    concat_fn: Callable[..., Path] = concat_mp4_chunks,
    verify_fn: Callable[..., ProbeResult] = verify_mp4_output,
    thumb_fn: Callable[..., Path] = extract_thumbnail,
    callback_secret: str | None = None,
    callback_post_fn: CallbackPostFn | None = None,
) -> dict[str, Any]:
    cfg = settings or WorkerSettings()
    action = str(event.get("action") or "").strip()
    if action == "report_failure":
        return _handle_report_failure(
            event,
            callback_secret=callback_secret,
            callback_post_fn=callback_post_fn,
        )

    resolved_store = store or _resolve_store(event, cfg)

    if action == "prepare":
        return _handle_prepare(
            event,
            store=resolved_store,
            settings=cfg,
            frame_count_fn=frame_count_fn,
        )
    if action == "render_single":
        return _handle_render_single(
            event,
            store=resolved_store,
            settings=cfg,
            render_fn=render_fn,
            verify_fn=verify_fn,
            thumb_fn=thumb_fn,
            callback_secret=callback_secret,
            callback_post_fn=callback_post_fn,
        )
    if action == "render_chunk":
        return _handle_render_chunk(
            event,
            store=resolved_store,
            settings=cfg,
            render_fn=render_fn,
            callback_secret=callback_secret,
            callback_post_fn=callback_post_fn,
        )
    if action == "finalize":
        return _handle_finalize(
            event,
            store=resolved_store,
            settings=cfg,
            concat_fn=concat_fn,
            verify_fn=verify_fn,
            thumb_fn=thumb_fn,
            callback_secret=callback_secret,
            callback_post_fn=callback_post_fn,
        )
    raise ValueError(f"Unsupported Lambda render action: {action!r}")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    _ = context
    configure_logging()
    return handle_event(event)
