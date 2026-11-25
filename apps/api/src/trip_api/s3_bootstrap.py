from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError

ABORT_INCOMPLETE_MULTIPART_RULE_ID = "abort-incomplete-multipart-uploads"
DEFAULT_ABORT_MULTIPART_DAYS = 1

_MISSING_BUCKET_CODES = frozenset({"404", "NoSuchBucket", "NotFound"})
_MISSING_LIFECYCLE_CODES = frozenset({"404", "NoSuchLifecycleConfiguration"})
_ALLOWED_ABORT_RULE_KEYS = frozenset(
    {"ID", "Status", "Filter", "Prefix", "AbortIncompleteMultipartUpload"}
)


class S3BootstrapClient(Protocol):
    def head_bucket(self, *, Bucket: str) -> Any: ...
    def create_bucket(self, *, Bucket: str) -> Any: ...
    def get_bucket_lifecycle_configuration(self, *, Bucket: str) -> Mapping[str, Any]: ...
    def put_bucket_lifecycle_configuration(
        self, *, Bucket: str, LifecycleConfiguration: Mapping[str, Any]
    ) -> Any: ...


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _normalize_optional_env(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _matches_abort_rule(
    rule: Mapping[str, Any], *, rule_id: str, abort_multipart_days: int
) -> bool:
    if rule.get("ID") != rule_id or rule.get("Status") != "Enabled":
        return False
    if rule.get("AbortIncompleteMultipartUpload") != {"DaysAfterInitiation": abort_multipart_days}:
        return False
    if not set(rule.keys()).issubset(_ALLOWED_ABORT_RULE_KEYS):
        return False
    if rule.get("Prefix", "") != "":
        return False
    return rule.get("Filter") in (None, {}, {"Prefix": ""})


def ensure_bucket_and_multipart_lifecycle(
    client: S3BootstrapClient,
    bucket: str,
    *,
    abort_multipart_days: int = DEFAULT_ABORT_MULTIPART_DAYS,
    rule_id: str = ABORT_INCOMPLETE_MULTIPART_RULE_ID,
) -> bool:
    cleaned_bucket = bucket.strip()
    if not cleaned_bucket:
        raise ValueError("S3 bucket name must not be empty")
    if abort_multipart_days < 1:
        raise ValueError("abort_multipart_days must be >= 1")

    try:
        client.head_bucket(Bucket=cleaned_bucket)
    except ClientError as exc:
        if _client_error_code(exc) in _MISSING_BUCKET_CODES:
            client.create_bucket(Bucket=cleaned_bucket)
        else:
            raise

    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=cleaned_bucket)
        existing_rules = list(response.get("Rules", []))
    except ClientError as exc:
        if _client_error_code(exc) in _MISSING_LIFECYCLE_CODES:
            existing_rules = []
        else:
            raise

    matching_id_rules = [rule for rule in existing_rules if rule.get("ID") == rule_id]
    if len(matching_id_rules) == 1 and _matches_abort_rule(
        matching_id_rules[0],
        rule_id=rule_id,
        abort_multipart_days=abort_multipart_days,
    ):
        return False

    desired_rule: dict[str, Any] = {
        "ID": rule_id,
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": abort_multipart_days},
    }
    merged_rules = [rule for rule in existing_rules if rule.get("ID") != rule_id]
    merged_rules.append(desired_rule)
    client.put_bucket_lifecycle_configuration(
        Bucket=cleaned_bucket,
        LifecycleConfiguration={"Rules": merged_rules},
    )
    return True


def bootstrap_from_env(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    bucket = (env.get("S3_BUCKET") or "").strip()
    if not bucket:
        raise ValueError("S3_BUCKET environment variable is required")

    raw_days = _normalize_optional_env(env.get("S3_ABORT_MULTIPART_DAYS"))
    abort_days = DEFAULT_ABORT_MULTIPART_DAYS if raw_days is None else int(raw_days)

    client = boto3.client(
        "s3",
        endpoint_url=_normalize_optional_env(env.get("S3_ENDPOINT_URL")),
        aws_access_key_id=_normalize_optional_env(env.get("S3_ACCESS_KEY_ID")),
        aws_secret_access_key=_normalize_optional_env(env.get("S3_SECRET_ACCESS_KEY")),
        region_name=_normalize_optional_env(env.get("S3_REGION")) or "us-east-1",
    )
    return ensure_bucket_and_multipart_lifecycle(
        client,
        bucket,
        abort_multipart_days=abort_days,
    )


def main() -> int:
    bootstrap_from_env()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
