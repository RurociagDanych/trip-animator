from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from conftest import SAMPLE_GPX, AppHarness, HarnessFactory
from fastapi.testclient import TestClient
from sqlalchemy import select
from trip_api.config import Settings
from trip_api.db import RenderJob
from trip_api.main import create_app
from trip_api.remote_renders import process_remote_renders_once
from trip_api.render_backends import (
    AwsSfnLambdaBackend,
    RemoteExecutionRef,
    RemoteExecutionStatus,
    RemoteSubmitRequest,
    build_render_backend,
    sign_render_callback,
)


class _FakeRemoteBackend:
    name = "aws_sfn_lambda"
    is_remote = True

    def __init__(self) -> None:
        self.submitted: list[RemoteSubmitRequest] = []
        self.cancelled: list[str] = []
        self.cleaned_jobs: list[uuid.UUID] = []
        self.statuses: dict[str, RemoteExecutionStatus] = {}
        self.artifacts: dict[str, bytes] = {}
        self.fail_submit: Exception | None = None

    def submit(self, request: RemoteSubmitRequest) -> RemoteExecutionRef:
        if self.fail_submit is not None:
            raise self.fail_submit
        self.submitted.append(request)
        exec_id = f"arn:aws:states:eu-central-1:123456789012:execution:render:{request.job_id}"
        self.statuses[exec_id] = RemoteExecutionStatus(state="running", progress_pct=10)
        return RemoteExecutionRef(
            execution_id=exec_id,
            staging_prefix=f"staging/{request.job_id}",
        )

    def get_status(self, execution_ref: str) -> RemoteExecutionStatus:
        return self.statuses[execution_ref]

    def cancel(self, execution_ref: str) -> None:
        self.cancelled.append(execution_ref)
        self.statuses[execution_ref] = RemoteExecutionStatus(state="cancelled")

    def download_output(self, key: str, dest: Path) -> None:
        if key not in self.artifacts:
            raise FileNotFoundError(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.artifacts[key])

    def cleanup_staging(self, job_id: uuid.UUID) -> None:
        self.cleaned_jobs.append(job_id)


def _create_project_with_stage(client: TestClient, csrf: str) -> str:
    proj = client.post(
        "/api/v1/projects",
        json={"title": "Remote Render Trip", "description": "Test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert proj.status_code == 201, proj.text
    project_id = str(proj.json()["id"])

    upload = client.post(
        f"/api/v1/projects/{project_id}/gpx",
        files={"file": ("day1.gpx", SAMPLE_GPX, "application/gpx+xml")},
        data={"name": "Day 1", "day_index": "1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 201, upload.text
    return project_id


def test_production_settings_fail_closed_for_remote_render_backend() -> None:
    base_prod: dict[str, Any] = {
        "app_env": "production",
        "dev_auth_enabled": False,
        "database_url": "postgresql+psycopg://trip:strong-pw@db.internal:5432/trip",
        "session_secret": "x" * 48,
        "token_encryption_key": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        "allowed_origins_raw": "https://trips.example.com",
        "render_backend": "aws_sfn_lambda",
    }

    with pytest.raises(ValueError, match="RENDER_AWS_STATE_MACHINE_ARN"):
        Settings(**base_prod)

    with pytest.raises(ValueError, match="RENDER_AWS_STAGING_BUCKET"):
        Settings(
            **base_prod,
            render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
        )

    with pytest.raises(ValueError, match="RENDER_CALLBACK_SECRET"):
        Settings(
            **base_prod,
            render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
            render_aws_staging_bucket="trip-staging",
            render_callback_base_url="https://trips.example.com",
            render_callback_secret="short",
        )

    valid = Settings(
        **base_prod,
        render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
        render_aws_staging_bucket="trip-staging",
        render_callback_base_url="https://trips.example.com",
        render_callback_secret="s" * 40,
    )
    assert valid.render_backend == "aws_sfn_lambda"
    assert build_render_backend(valid).name == "aws_sfn_lambda"


def test_outbox_dispatch_reconcile_and_copy_back_to_primary_store(
    make_harness: HarnessFactory,
) -> None:
    backend = _FakeRemoteBackend()
    harness: AppHarness = make_harness(
        render_backend="aws_sfn_lambda",
        render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
        render_aws_staging_bucket="trip-staging",
        render_remote_dispatch_interval_s=0.0,
    )
    harness.app = create_app(
        harness.settings,
        engine=harness.engine,
        session_factory=harness.session_factory,
        object_store=harness.store,
        render_backend=backend,
    )
    harness.create_user("rider@example.com")
    client, csrf = harness.login_client("rider@example.com")
    project_id = _create_project_with_stage(client, csrf)

    resp = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json={"resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "remote-render-key-001"},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["id"])
    assert resp.json()["status"] == "queued"

    report1 = process_remote_renders_once(
        harness.session_factory,
        harness.store,
        harness.settings,
        backend,
    )
    assert report1.dispatched == 1
    assert len(backend.submitted) == 1
    assert backend.submitted[0].job_id == job_id
    assert backend.submitted[0].bundle.manifest.total_frames == 300

    with harness.session_factory() as db:
        job = db.scalar(select(RenderJob).where(RenderJob.id == job_id))
        assert job is not None
        assert job.status == "running"
        assert job.render_backend == "aws_sfn_lambda"
        assert job.external_execution_id is not None
        exec_id = job.external_execution_id

    output_key = f"staging/{job_id}/output.mp4"
    thumb_key = f"staging/{job_id}/output.webp"
    backend.artifacts[output_key] = b"fake-mp4-video-payload"
    backend.artifacts[thumb_key] = b"fake-webp-thumb-payload"
    backend.statuses[exec_id] = RemoteExecutionStatus(
        state="succeeded",
        progress_pct=100,
        output_key=output_key,
        thumbnail_key=thumb_key,
        probe={"width": 1280, "height": 720, "duration_s": 10.0},
    )

    report2 = process_remote_renders_once(
        harness.session_factory,
        harness.store,
        harness.settings,
        backend,
    )
    assert report2.reconciled_succeeded == 1
    assert harness.store.get_bytes(f"outputs/{job_id}.mp4") == b"fake-mp4-video-payload"
    assert harness.store.get_bytes(f"outputs/{job_id}.webp") == b"fake-webp-thumb-payload"
    assert job_id in backend.cleaned_jobs

    job_resp = client.get(f"/api/v1/renders/{job_id}")
    assert job_resp.status_code == 200
    assert job_resp.json()["status"] == "succeeded"
    assert job_resp.json()["progress_pct"] == 100


def test_signed_hmac_callbacks_and_cancellation_protection(
    make_harness: HarnessFactory,
) -> None:
    secret = "callback-hmac-secret-at-least-32-characters"
    backend = _FakeRemoteBackend()
    harness = make_harness(
        render_backend="aws_sfn_lambda",
        render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
        render_aws_staging_bucket="trip-staging",
        render_callback_base_url="https://trips.example.com",
        render_callback_secret=secret,
        render_remote_dispatch_interval_s=0.0,
    )
    harness.app = create_app(
        harness.settings,
        engine=harness.engine,
        session_factory=harness.session_factory,
        object_store=harness.store,
        render_backend=backend,
    )
    harness.create_user("rider@example.com")
    client, csrf = harness.login_client("rider@example.com")
    project_id = _create_project_with_stage(client, csrf)

    resp = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json={"resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "remote-callback-key-001"},
    )
    job_id = uuid.UUID(resp.json()["id"])

    process_remote_renders_once(harness.session_factory, harness.store, harness.settings, backend)

    body_bytes = json.dumps({"job_id": str(job_id), "event": "running", "progress_pct": 55}).encode(
        "utf-8"
    )
    now_ts = int(time.time())

    bad_sig = client.post(
        "/api/v1/internal/render-callbacks",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Trip-Timestamp": str(now_ts),
            "X-Trip-Signature": "sha256=deadbeef",
        },
    )
    assert bad_sig.status_code == 401

    stale_ts = now_ts - 600
    stale_resp = client.post(
        "/api/v1/internal/render-callbacks",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Trip-Timestamp": str(stale_ts),
            "X-Trip-Signature": sign_render_callback(secret, stale_ts, body_bytes),
        },
    )
    assert stale_resp.status_code == 401

    good_resp = client.post(
        "/api/v1/internal/render-callbacks",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Trip-Timestamp": str(now_ts),
            "X-Trip-Signature": sign_render_callback(secret, now_ts, body_bytes),
        },
    )
    assert good_resp.status_code == 200, good_resp.text
    assert client.get(f"/api/v1/renders/{job_id}").json()["progress_pct"] == 55

    cancel_resp = client.post(
        f"/api/v1/renders/{job_id}/cancel",
        headers={"X-CSRF-Token": csrf},
    )
    assert cancel_resp.status_code == 200
    assert len(backend.cancelled) == 1

    late_ok = json.dumps(
        {
            "job_id": str(job_id),
            "event": "succeeded",
            "output_key": f"staging/{job_id}/output.mp4",
            "thumbnail_key": f"staging/{job_id}/output.webp",
        }
    ).encode("utf-8")
    late_resp = client.post(
        "/api/v1/internal/render-callbacks",
        content=late_ok,
        headers={
            "Content-Type": "application/json",
            "X-Trip-Timestamp": str(now_ts),
            "X-Trip-Signature": sign_render_callback(secret, now_ts, late_ok),
        },
    )
    assert late_resp.status_code == 200
    assert client.get(f"/api/v1/renders/{job_id}").json()["status"] == "cancelled"


def test_aws_sfn_lambda_backend_stages_bundle_and_maps_sfn_states(tmp_path: Path) -> None:
    s3_objects: dict[tuple[str, str], bytes] = {}
    started_executions: list[dict[str, Any]] = []
    stopped_executions: list[str] = []

    class _StubS3:
        def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> None:
            _ = ContentType
            s3_objects[(Bucket, Key)] = Body

        def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
            Path(Filename).write_bytes(s3_objects[(Bucket, Key)])

        def list_objects_v2(self, *, Bucket: str, Prefix: str) -> dict[str, Any]:
            keys = [{"Key": k} for (b, k) in s3_objects if b == Bucket and k.startswith(Prefix)]
            return {"Contents": keys}

        def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> None:
            for item in Delete.get("Objects", []):
                s3_objects.pop((Bucket, item["Key"]), None)

    class _StubSfn:
        def __init__(self) -> None:
            self.describe_response: dict[str, Any] = {"status": "RUNNING"}

        def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> dict[str, Any]:
            started_executions.append(
                {"arn": stateMachineArn, "name": name, "input": json.loads(input)}
            )
            return {"executionArn": f"arn:aws:states:eu-central-1:123:execution:trip:{name}"}

        def describe_execution(self, *, executionArn: str) -> dict[str, Any]:
            _ = executionArn
            return self.describe_response

        def stop_execution(self, *, executionArn: str, cause: str = "") -> None:
            _ = cause
            stopped_executions.append(executionArn)

    sfn = _StubSfn()
    backend = AwsSfnLambdaBackend(
        state_machine_arn="arn:aws:states:eu-central-1:123:stateMachine:trip",
        staging_bucket="trip-staging",
        region="eu-central-1",
        s3_client=_StubS3(),
        sfn_client=sfn,
    )

    from trip_telemetry import RenderBundle, RenderManifest, RenderSpecV2

    job_id = uuid.uuid4()
    manifest = RenderManifest(
        job_id=str(job_id),
        project_id=str(uuid.uuid4()),
        project_title="Test",
        spec=RenderSpecV2(resolution="1280x720", fps=30, duration_seconds=10.0),
        width=1280,
        height=720,
        fps=30,
        duration_s=10.0,
        total_frames=300,
        is_preview=False,
        stages=[],
    )
    ref = backend.submit(
        RemoteSubmitRequest(
            job_id=job_id,
            bundle=RenderBundle(manifest=manifest, photo_assets={"p1": b"img-bytes"}),
            callback_url="https://trips.example.com/api/v1/internal/render-callbacks",
            chunk_threshold_frames=300,
            chunk_size_frames=200,
            preroll_frames=15,
        )
    )
    assert ("trip-staging", f"staging/{job_id}/manifest.json") in s3_objects
    assert ("trip-staging", f"staging/{job_id}/photos/p1.bin") in s3_objects
    assert len(started_executions) == 1
    sfn_input = started_executions[0]["input"]
    assert sfn_input["job_id"] == str(job_id)
    assert "secret" not in json.dumps(sfn_input).lower()

    assert backend.get_status(ref.execution_id).state == "running"

    s3_objects[("trip-staging", f"staging/{job_id}/output.mp4")] = b"mp4-ok"
    sfn.describe_response = {
        "status": "SUCCEEDED",
        "output": json.dumps(
            {
                "output_key": f"staging/{job_id}/output.mp4",
                "thumbnail_key": f"staging/{job_id}/output.webp",
            }
        ),
    }
    st = backend.get_status(ref.execution_id)
    assert st.state == "succeeded"
    assert st.output_key == f"staging/{job_id}/output.mp4"

    dest = tmp_path / "out.mp4"
    backend.download_output(st.output_key, dest)
    assert dest.read_bytes() == b"mp4-ok"

    backend.cancel(ref.execution_id)
    assert stopped_executions == [ref.execution_id]

    backend.cleanup_staging(job_id)
    assert not any(k.startswith(f"staging/{job_id}/") for (_, k) in s3_objects)


def test_outbox_retry_exhaustion_and_callback_key_guard(
    make_harness: HarnessFactory,
) -> None:
    secret = "callback-hmac-secret-at-least-32-characters"
    backend = _FakeRemoteBackend()
    backend.fail_submit = RuntimeError("SFN throttled")
    harness = make_harness(
        render_backend="aws_sfn_lambda",
        render_aws_state_machine_arn="arn:aws:states:eu-central-1:123456789012:stateMachine:trip",
        render_aws_staging_bucket="trip-staging",
        render_callback_secret=secret,
        render_remote_max_attempts=2,
        render_remote_dispatch_interval_s=0.0,
    )
    harness.app = create_app(
        harness.settings,
        engine=harness.engine,
        session_factory=harness.session_factory,
        object_store=harness.store,
        render_backend=backend,
    )
    harness.create_user("rider@example.com")
    client, csrf = harness.login_client("rider@example.com")
    project_id = _create_project_with_stage(client, csrf)

    resp = client.post(
        f"/api/v1/projects/{project_id}/renders",
        json={"resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "retry-exhaust-key-001"},
    )
    job_id = uuid.UUID(resp.json()["id"])

    r1 = process_remote_renders_once(
        harness.session_factory, harness.store, harness.settings, backend
    )
    assert r1.dispatch_failed == 1
    assert client.get(f"/api/v1/renders/{job_id}").json()["status"] == "queued"

    r2 = process_remote_renders_once(
        harness.session_factory, harness.store, harness.settings, backend
    )
    assert r2.dispatch_failed == 1
    assert client.get(f"/api/v1/renders/{job_id}").json()["status"] == "failed"

    now_ts = int(time.time())
    bad_key_body = json.dumps(
        {
            "job_id": str(job_id),
            "event": "succeeded",
            "output_key": "staging/other-job/output.mp4",
        }
    ).encode("utf-8")
    bad_key_resp = client.post(
        "/api/v1/internal/render-callbacks",
        content=bad_key_body,
        headers={
            "Content-Type": "application/json",
            "X-Trip-Timestamp": str(now_ts),
            "X-Trip-Signature": sign_render_callback(secret, now_ts, bad_key_body),
        },
    )
    assert bad_key_resp.status_code == 400
