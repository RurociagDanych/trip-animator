"""Verify bucket bootstrap and its standalone entry point."""

from __future__ import annotations

import subprocess  # noqa: S404  # Exercise the Compose entry point in a fresh interpreter.
import sys
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from trip_api.s3_bootstrap import (
    ABORT_INCOMPLETE_MULTIPART_RULE_ID,
    bootstrap_from_env,
    ensure_bucket_and_multipart_lifecycle,
)


def _make_stub_client() -> Any:
    return boto3.client(
        "s3",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


def test_bootstrap_cli_validates_s3_without_api_settings(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603  # The executable and arguments are fixed.
        [sys.executable, "-m", "trip_api.s3_bootstrap"],
        env={"APP_ENV": "production"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "S3_BUCKET environment variable is required" in result.stderr


@pytest.mark.parametrize("missing_code", ["404", "NoSuchBucket", "NotFound"])
def test_creates_missing_bucket_and_sets_multipart_lifecycle_rule(missing_code: str) -> None:
    client = _make_stub_client()
    expected_rule = {
        "ID": ABORT_INCOMPLETE_MULTIPART_RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }

    with Stubber(client) as stub:
        stub.add_client_error(
            "head_bucket",
            service_error_code=missing_code,
            expected_params={"Bucket": "trip-animator"},
        )
        stub.add_response(
            "create_bucket",
            {},
            expected_params={"Bucket": "trip-animator"},
        )
        stub.add_client_error(
            "get_bucket_lifecycle_configuration",
            service_error_code="NoSuchLifecycleConfiguration",
            expected_params={"Bucket": "trip-animator"},
        )
        stub.add_response(
            "put_bucket_lifecycle_configuration",
            {},
            expected_params={
                "Bucket": "trip-animator",
                "LifecycleConfiguration": {"Rules": [expected_rule]},
            },
        )

        updated = ensure_bucket_and_multipart_lifecycle(client, "trip-animator")
        assert updated is True


def test_is_idempotent_when_matching_rule_already_exists() -> None:
    client = _make_stub_client()
    existing_rule = {
        "ID": ABORT_INCOMPLETE_MULTIPART_RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }

    with Stubber(client) as stub:
        stub.add_response("head_bucket", {}, expected_params={"Bucket": "trip-animator"})
        stub.add_response(
            "get_bucket_lifecycle_configuration",
            {"Rules": [existing_rule]},
            expected_params={"Bucket": "trip-animator"},
        )

        updated = ensure_bucket_and_multipart_lifecycle(client, "trip-animator")
        assert updated is False


def test_preserves_unrelated_rules_and_updates_stale_managed_rule() -> None:
    client = _make_stub_client()
    custom_rule = {
        "ID": "custom-retention",
        "Status": "Enabled",
        "Filter": {"Prefix": "custom/"},
        "Expiration": {"Days": 30},
    }
    stale_rule = {
        "ID": ABORT_INCOMPLETE_MULTIPART_RULE_ID,
        "Status": "Disabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    }
    desired_rule = {
        "ID": ABORT_INCOMPLETE_MULTIPART_RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }

    with Stubber(client) as stub:
        stub.add_response("head_bucket", {}, expected_params={"Bucket": "trip-animator"})
        stub.add_response(
            "get_bucket_lifecycle_configuration",
            {"Rules": [custom_rule, stale_rule]},
            expected_params={"Bucket": "trip-animator"},
        )
        stub.add_response(
            "put_bucket_lifecycle_configuration",
            {},
            expected_params={
                "Bucket": "trip-animator",
                "LifecycleConfiguration": {"Rules": [custom_rule, desired_rule]},
            },
        )

        updated = ensure_bucket_and_multipart_lifecycle(client, "trip-animator")
        assert updated is True


@pytest.mark.parametrize("operation", ["head_bucket", "get_bucket_lifecycle_configuration"])
def test_propagates_non_missing_s3_errors(operation: str) -> None:
    client = _make_stub_client()

    with Stubber(client) as stub:
        if operation == "head_bucket":
            stub.add_client_error(
                "head_bucket",
                service_error_code="AccessDenied",
                expected_params={"Bucket": "trip-animator"},
            )
        else:
            stub.add_response("head_bucket", {}, expected_params={"Bucket": "trip-animator"})
            stub.add_client_error(
                "get_bucket_lifecycle_configuration",
                service_error_code="AccessDenied",
                expected_params={"Bucket": "trip-animator"},
            )

        with pytest.raises(ClientError):
            ensure_bucket_and_multipart_lifecycle(client, "trip-animator")


def test_bootstrap_from_env_builds_client_and_applies_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_client_kwargs: list[dict[str, Any]] = []
    client = _make_stub_client()

    def fake_client(service: str, **kwargs: Any) -> Any:
        assert service == "s3"
        recorded_client_kwargs.append(kwargs)
        return client

    monkeypatch.setattr("trip_api.s3_bootstrap.boto3.client", fake_client)

    with Stubber(client) as stub:
        stub.add_response("head_bucket", {}, expected_params={"Bucket": "trip-animator"})
        stub.add_client_error(
            "get_bucket_lifecycle_configuration",
            service_error_code="NoSuchLifecycleConfiguration",
            expected_params={"Bucket": "trip-animator"},
        )
        stub.add_response(
            "put_bucket_lifecycle_configuration",
            {},
            expected_params={
                "Bucket": "trip-animator",
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "ID": ABORT_INCOMPLETE_MULTIPART_RULE_ID,
                            "Status": "Enabled",
                            "Filter": {"Prefix": ""},
                            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2},
                        }
                    ]
                },
            },
        )

        updated = bootstrap_from_env(
            {
                "S3_ENDPOINT_URL": " http://minio:9000 ",
                "S3_ACCESS_KEY_ID": "   ",
                "S3_SECRET_ACCESS_KEY": "",
                "S3_BUCKET": " trip-animator ",
                "S3_REGION": "eu-central-1",
                "S3_ABORT_MULTIPART_DAYS": "2",
            }
        )
        assert updated is True

    assert recorded_client_kwargs == [
        {
            "endpoint_url": "http://minio:9000",
            "aws_access_key_id": None,
            "aws_secret_access_key": None,
            "region_name": "eu-central-1",
        }
    ]


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"S3_BUCKET": "   "},
        {"S3_BUCKET": "trip-animator", "S3_ABORT_MULTIPART_DAYS": "0"},
    ],
)
def test_bootstrap_from_env_rejects_invalid_inputs(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        bootstrap_from_env(env)
