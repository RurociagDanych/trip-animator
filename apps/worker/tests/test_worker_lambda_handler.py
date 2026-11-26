from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
from trip_telemetry import ManifestStage, RenderManifest, RenderSpecV2
from trip_worker.encoder import ProbeResult
from trip_worker.lambda_handler import handle_event
from trip_worker.local_spa import LocalSpaServer
from trip_worker.storage import LocalObjectStore


def _make_manifest(job_id: uuid.UUID, *, duration_s: float = 10.0, fps: int = 30) -> RenderManifest:
    return RenderManifest(
        job_id=str(job_id),
        project_id=str(uuid.uuid4()),
        project_title="Lambda Test Trip",
        spec=RenderSpecV2(resolution="1280x720", fps=fps, duration_seconds=duration_s),
        width=1280,
        height=720,
        fps=fps,
        duration_s=duration_s,
        total_frames=round(duration_s * fps),
        is_preview=False,
        stages=[
            ManifestStage(
                id=str(uuid.uuid4()),
                name="Day 1",
                day_index=1,
                stage_order=0,
                color="#ff5500",
                distance_m=1000.0,
                elevation_gain_m=50.0,
                moving_seconds=300.0,
                elapsed_seconds=300.0,
                has_timestamps=True,
                points=[],
            )
        ],
    )


def test_local_spa_server_serves_assets_and_spa_fallback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA_OK</html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok');", encoding="utf-8")

    with LocalSpaServer(dist) as srv:
        assert srv.base_url.startswith("http://127.0.0.1:")
        resp = httpx.get(f"{srv.base_url}/render/1234", timeout=5)
        assert resp.status_code == 200
        assert b"SPA_OK" in resp.content
        resp_js = httpx.get(f"{srv.base_url}/assets/app.js", timeout=5)
        assert resp_js.status_code == 200
        assert b"console.log" in resp_js.content


def test_lambda_handler_single_and_chunked_workflow_with_signed_callbacks(
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path / "staging")
    callback_requests: list[tuple[str, dict[str, str], bytes]] = []

    def _fake_post(url: str, headers: dict[str, str], body: bytes) -> None:
        callback_requests.append((url, headers, body))

    def _fake_render(
        manifest: RenderManifest,
        output_path: Path,
        settings: Any,
        **kwargs: Any,
    ) -> int:
        _ = (manifest, settings)
        frame_start = int(kwargs.get("frame_start", 0))
        frame_end = int(kwargs.get("frame_end") or manifest.total_frames)
        output_path.write_bytes(f"mp4:{frame_start}-{frame_end}".encode())
        return frame_end - frame_start

    def _fake_verify(output_mp4_path: Path, **kwargs: Any) -> ProbeResult:
        _ = (output_mp4_path, kwargs)
        return ProbeResult(
            codec_name="h264",
            pix_fmt="yuv420p",
            width=1280,
            height=720,
            duration_s=10.0,
            nb_frames=300,
        )

    def _fake_thumb(input_mp4: Path, out_thumb: Path, **kwargs: Any) -> Path:
        _ = (input_mp4, kwargs)
        out_thumb.write_bytes(b"webp-thumb")
        return out_thumb

    def _fake_concat(chunk_paths: list[Path], out_mp4: Path, **kwargs: Any) -> Path:
        _ = kwargs
        joined = b"|".join(p.read_bytes() for p in chunk_paths)
        out_mp4.write_bytes(joined)
        return out_mp4

    secret = "lambda-callback-secret-32-bytes-minimum"

    short_job = uuid.uuid4()
    short_manifest = _make_manifest(short_job, duration_s=10.0, fps=30)
    short_key = f"staging/{short_job}/manifest.json"
    store.put_bytes(short_key, short_manifest.model_dump_json().encode("utf-8"))

    prep_short = handle_event(
        {
            "action": "prepare",
            "job_id": str(short_job),
            "manifest_key": short_key,
            "photo_keys": {},
            "output_key": f"staging/{short_job}/output.mp4",
            "thumbnail_key": f"staging/{short_job}/output.webp",
            "chunk_threshold_frames": 300,
            "chunk_size_frames": 200,
            "preroll_frames": 15,
            "callback_url": "https://trips.example.com/api/v1/internal/render-callbacks",
        },
        store=store,
        callback_secret=secret,
        callback_post_fn=_fake_post,
    )
    assert prep_short["mode"] == "single"
    assert prep_short["total_frames"] == 300

    single_res = handle_event(
        {**prep_short, "action": "render_single"},
        store=store,
        render_fn=_fake_render,
        verify_fn=_fake_verify,
        thumb_fn=_fake_thumb,
        callback_secret=secret,
        callback_post_fn=_fake_post,
    )
    assert single_res["output_key"] == f"staging/{short_job}/output.mp4"
    assert store.get_bytes(f"staging/{short_job}/output.mp4") == b"mp4:0-300"
    assert store.get_bytes(f"staging/{short_job}/output.webp") == b"webp-thumb"
    assert len(callback_requests) == 1
    cb_url, cb_headers, cb_body = callback_requests[0]
    assert cb_url.endswith("/api/v1/internal/render-callbacks")
    ts = cb_headers["X-Trip-Timestamp"]
    expected_sig = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            f"{ts}.".encode("ascii") + cb_body,
            hashlib.sha256,
        ).hexdigest()
    )
    assert cb_headers["X-Trip-Signature"] == expected_sig
    assert json.loads(cb_body)["event"] == "succeeded"

    long_job = uuid.uuid4()
    long_manifest = _make_manifest(long_job, duration_s=20.0, fps=30)
    long_key = f"staging/{long_job}/manifest.json"
    store.put_bytes(long_key, long_manifest.model_dump_json().encode("utf-8"))

    prep_long = handle_event(
        {
            "action": "prepare",
            "job_id": str(long_job),
            "manifest_key": long_key,
            "photo_keys": {},
            "output_key": f"staging/{long_job}/output.mp4",
            "thumbnail_key": f"staging/{long_job}/output.webp",
            "chunk_threshold_frames": 300,
            "chunk_size_frames": 200,
            "preroll_frames": 15,
            "callback_url": "https://trips.example.com/api/v1/internal/render-callbacks",
        },
        store=store,
        callback_secret=secret,
        callback_post_fn=_fake_post,
    )
    assert prep_long["mode"] == "chunked"
    assert prep_long["total_chunks"] == 3
    assert prep_long["chunks"][0] == {
        "chunk_index": 0,
        "start_frame": 0,
        "end_frame": 200,
        "preroll_frames": 0,
    }
    assert prep_long["chunks"][1]["preroll_frames"] == 15

    chunk_results = []
    for chunk in prep_long["chunks"]:
        chunk_res = handle_event(
            {
                "action": "render_chunk",
                "job_id": str(long_job),
                "staging_prefix": f"staging/{long_job}",
                "manifest_key": long_key,
                "photo_keys": {},
                "total_chunks": prep_long["total_chunks"],
                "callback_url": prep_long["callback_url"],
                **chunk,
            },
            store=store,
            render_fn=_fake_render,
            callback_secret=secret,
            callback_post_fn=_fake_post,
        )
        chunk_results.append(chunk_res)

    final_res = handle_event(
        {
            "action": "finalize",
            "job_id": str(long_job),
            "manifest_key": long_key,
            "output_key": f"staging/{long_job}/output.mp4",
            "thumbnail_key": f"staging/{long_job}/output.webp",
            "chunk_results": chunk_results,
            "callback_url": prep_long["callback_url"],
        },
        store=store,
        concat_fn=_fake_concat,
        verify_fn=_fake_verify,
        thumb_fn=_fake_thumb,
        callback_secret=secret,
        callback_post_fn=_fake_post,
    )
    assert final_res["output_key"] == f"staging/{long_job}/output.mp4"
    assert store.get_bytes(f"staging/{long_job}/output.mp4") == b"mp4:0-200|mp4:200-400|mp4:400-600"

    fail_res = handle_event(
        {
            "action": "report_failure",
            "job_id": str(long_job),
            "callback_url": prep_long["callback_url"],
            "error_detail": {"Error": "States.TaskFailed", "Cause": "Out of memory"},
        },
        callback_secret=secret,
        callback_post_fn=_fake_post,
    )
    assert fail_res["reported"] is True
    assert json.loads(callback_requests[-1][2])["event"] == "failed"
    assert "Out of memory" in json.loads(callback_requests[-1][2])["error_message"]
