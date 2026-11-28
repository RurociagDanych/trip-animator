from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import boto3
from botocore.exceptions import ClientError
from trip_telemetry import (
    RenderBundle,
    sign_render_callback,
    verify_render_callback_signature,
)

from trip_api.config import Settings

logger = logging.getLogger(__name__)

RenderBackendName = Literal["local", "aws_sfn_lambda"]
RemoteExecutionState = Literal["queued", "running", "succeeded", "failed", "cancelled"]

_PATH_RE = re.compile(r"(?:/[\w.\-]+){2,}")
_CRED_URL_RE = re.compile(r"://[^/\s@]+@")

__all__ = [
    "AwsSfnLambdaBackend",
    "LocalRenderBackend",
    "RemoteExecutionRef",
    "RemoteExecutionState",
    "RemoteExecutionStatus",
    "RemoteSubmitRequest",
    "RenderBackend",
    "RenderBackendName",
    "build_render_backend",
    "sanitize_remote_error",
    "sign_render_callback",
    "verify_render_callback_signature",
]


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def sanitize_remote_error(raw: object, *, fallback: str = "Remote render failed") -> str:
    text = str(raw or "").strip()
    if not text:
        return fallback
    first_line = text.splitlines()[0].strip()
    redacted = _CRED_URL_RE.sub("://[redacted]@", first_line)
    redacted = _PATH_RE.sub("[path]", redacted)
    return redacted[:300] or fallback


@dataclass(frozen=True, slots=True)
class RemoteSubmitRequest:
    job_id: uuid.UUID
    bundle: RenderBundle
    callback_url: str | None = None
    chunk_threshold_frames: int = 300
    chunk_size_frames: int = 200
    preroll_frames: int = 15


@dataclass(frozen=True, slots=True)
class RemoteExecutionRef:
    execution_id: str
    staging_prefix: str


@dataclass(frozen=True, slots=True)
class RemoteExecutionStatus:
    state: RemoteExecutionState
    progress_pct: int | None = None
    output_key: str | None = None
    thumbnail_key: str | None = None
    error_message: str | None = None
    probe: dict[str, Any] | None = None


class RenderBackend(Protocol):
    name: str
    is_remote: bool

    def submit(self, request: RemoteSubmitRequest) -> RemoteExecutionRef: ...
    def get_status(self, execution_ref: str) -> RemoteExecutionStatus: ...
    def cancel(self, execution_ref: str) -> None: ...
    def download_output(self, execution_ref: str, key: str, dest: Path) -> None: ...
    def cleanup_staging(self, job_id: uuid.UUID) -> None: ...


class LocalRenderBackend:
    name: str = "local"
    is_remote: bool = False

    def submit(self, request: RemoteSubmitRequest) -> RemoteExecutionRef:
        raise RuntimeError(f"LocalRenderBackend does not support remote submit ({request.job_id})")

    def get_status(self, execution_ref: str) -> RemoteExecutionStatus:
        raise RuntimeError(f"LocalRenderBackend does not support get_status ({execution_ref})")

    def cancel(self, execution_ref: str) -> None:
        _ = execution_ref

    def download_output(self, execution_ref: str, key: str, dest: Path) -> None:
        raise RuntimeError(
            f"LocalRenderBackend does not support download_output ({execution_ref}: {key}, {dest})"
        )

    def cleanup_staging(self, job_id: uuid.UUID) -> None:
        _ = job_id


class AwsSfnLambdaBackend:
    name: str = "aws_sfn_lambda"
    is_remote: bool = True

    def __init__(
        self,
        *,
        state_machine_arn: str,
        staging_bucket: str,
        region: str,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        endpoint_url: str | None = None,
        s3_client: Any = None,
        sfn_client: Any = None,
    ) -> None:
        self.state_machine_arn = state_machine_arn.strip()
        self.staging_bucket = staging_bucket.strip()
        self.region = region.strip()
        client_kwargs: dict[str, Any] = {
            "region_name": _normalize_optional_str(region),
            "aws_access_key_id": _normalize_optional_str(access_key_id),
            "aws_secret_access_key": _normalize_optional_str(secret_access_key),
            "endpoint_url": _normalize_optional_str(endpoint_url),
        }
        self._s3 = s3_client if s3_client is not None else boto3.client("s3", **client_kwargs)
        self._sfn = (
            sfn_client if sfn_client is not None else boto3.client("stepfunctions", **client_kwargs)
        )

    def submit(self, request: RemoteSubmitRequest) -> RemoteExecutionRef:
        prefix = f"staging/{request.job_id}"
        manifest_key = f"{prefix}/manifest.json"
        self._s3.put_object(
            Bucket=self.staging_bucket,
            Key=manifest_key,
            Body=request.bundle.manifest.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )
        photo_keys: dict[str, str] = {}
        for photo_id, data in request.bundle.photo_assets.items():
            photo_key = f"{prefix}/photos/{photo_id}.bin"
            self._s3.put_object(
                Bucket=self.staging_bucket,
                Key=photo_key,
                Body=data,
                ContentType="application/octet-stream",
            )
            photo_keys[photo_id] = photo_key

        execution_name = f"trip-{request.job_id.hex[:24]}-{uuid.uuid4().hex[:8]}"
        sfn_input: dict[str, Any] = {
            "job_id": str(request.job_id),
            "staging_bucket": self.staging_bucket,
            "staging_prefix": prefix,
            "manifest_key": manifest_key,
            "photo_keys": photo_keys,
            "output_key": f"{prefix}/output.mp4",
            "thumbnail_key": f"{prefix}/output.webp",
            "chunk_threshold_frames": request.chunk_threshold_frames,
            "chunk_size_frames": request.chunk_size_frames,
            "preroll_frames": request.preroll_frames,
            "callback_url": request.callback_url or "",
        }
        resp = self._sfn.start_execution(
            stateMachineArn=self.state_machine_arn,
            name=execution_name,
            input=json.dumps(sfn_input, separators=(",", ":")),
        )
        return RemoteExecutionRef(
            execution_id=str(resp["executionArn"]),
            staging_prefix=prefix,
        )

    def get_status(self, execution_ref: str) -> RemoteExecutionStatus:
        resp = self._sfn.describe_execution(executionArn=execution_ref)
        raw_status = str(resp.get("status") or "").upper()
        if raw_status == "RUNNING":
            return RemoteExecutionStatus(state="running")
        if raw_status == "SUCCEEDED":
            parsed: dict[str, Any] = {}
            raw_output = resp.get("output")
            if isinstance(raw_output, str) and raw_output.strip():
                try:
                    loaded = json.loads(raw_output)
                    if isinstance(loaded, dict):
                        parsed = loaded
                except json.JSONDecodeError:
                    parsed = {}
            output_key = parsed.get("output_key")
            thumbnail_key = parsed.get("thumbnail_key")
            probe = parsed.get("probe") if isinstance(parsed.get("probe"), dict) else None
            return RemoteExecutionStatus(
                state="succeeded",
                progress_pct=100,
                output_key=str(output_key) if output_key else None,
                thumbnail_key=str(thumbnail_key) if thumbnail_key else None,
                probe=probe,
            )
        if raw_status == "ABORTED":
            return RemoteExecutionStatus(state="cancelled")
        err_code = str(resp.get("error") or f"Remote execution {raw_status}")
        return RemoteExecutionStatus(
            state="failed",
            error_message=sanitize_remote_error(
                err_code, fallback=f"Remote execution {raw_status}"
            ),
        )

    def cancel(self, execution_ref: str) -> None:
        try:
            self._sfn.stop_execution(executionArn=execution_ref, cause="Cancelled by user")
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"ExecutionDoesNotExist", "ExecutionAlreadyStopped"}:
                return
            raise

    def download_output(self, execution_ref: str, key: str, dest: Path) -> None:
        _ = execution_ref
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._s3.download_file(self.staging_bucket, key, str(dest))

    def cleanup_staging(self, job_id: uuid.UUID) -> None:
        prefix = f"staging/{job_id}/"
        try:
            resp = self._s3.list_objects_v2(Bucket=self.staging_bucket, Prefix=prefix)
            contents = resp.get("Contents") or []
            keys = [{"Key": str(item["Key"])} for item in contents if "Key" in item]
            if keys:
                self._s3.delete_objects(
                    Bucket=self.staging_bucket,
                    Delete={"Objects": keys, "Quiet": True},
                )
        except Exception as exc:
            logger.warning("Best-effort staging cleanup failed for job %s: %s", job_id, exc)


def build_render_backend(settings: Settings) -> RenderBackend:
    if settings.render_backend == "aws_sfn_lambda":
        return AwsSfnLambdaBackend(
            state_machine_arn=settings.render_aws_state_machine_arn,
            staging_bucket=settings.render_aws_staging_bucket,
            region=settings.aws_region,
            access_key_id=settings.aws_access_key_id,
            secret_access_key=settings.aws_secret_access_key,
            endpoint_url=settings.render_aws_endpoint_url,
        )
    return LocalRenderBackend()
