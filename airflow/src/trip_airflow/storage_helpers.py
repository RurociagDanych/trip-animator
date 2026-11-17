"""Uniform, traversal-safe ObjectStore enumeration and existence helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from trip_api.storage import LocalObjectStore, ObjectStore, S3ObjectStore


def _normalize_prefix(prefix: str) -> str:
    clean = prefix.lstrip("/")
    if clean and ".." in Path(clean).parts:
        raise ValueError(f"Unsafe object prefix: {prefix!r}")
    return clean


def list_objects_with_mtime(
    store: ObjectStore,
    prefix: str = "",
) -> dict[str, datetime | None]:
    """List keys matching ``prefix`` along with UTC modification timestamps when available."""
    clean_prefix = _normalize_prefix(prefix)

    if isinstance(store, LocalObjectStore):
        results: dict[str, datetime | None] = {}
        for path in store.root_dir.rglob("*"):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(store.root_dir):
                continue
            rel_key = path.relative_to(store.root_dir).as_posix()
            if clean_prefix and not rel_key.startswith(clean_prefix):
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            results[rel_key] = mtime
        return dict(sorted(results.items()))

    if isinstance(store, S3ObjectStore):
        results = {}
        paginator = store.client.get_paginator("list_objects_v2")
        page_kwargs: dict[str, Any] = {"Bucket": store.bucket}
        if clean_prefix:
            page_kwargs["Prefix"] = clean_prefix
        for page in paginator.paginate(**page_kwargs):
            for obj in page.get("Contents") or ():
                key = str(obj["Key"])
                last_mod = obj.get("LastModified")
                s3_mtime: datetime | None = None
                if isinstance(last_mod, datetime):
                    s3_mtime = (
                        last_mod.replace(tzinfo=UTC)
                        if last_mod.tzinfo is None
                        else last_mod.astimezone(UTC)
                    )
                results[key] = s3_mtime
        return dict(sorted(results.items()))

    list_keys_with_mtime_fn = getattr(store, "list_keys_with_mtime", None)
    if callable(list_keys_with_mtime_fn):
        raw_mapping = list_keys_with_mtime_fn(clean_prefix)
        return dict(sorted((str(k), v) for k, v in raw_mapping.items()))

    list_keys_fn = getattr(store, "list_keys", None)
    if callable(list_keys_fn):
        keys = sorted(str(k) for k in list_keys_fn(clean_prefix))
        return {k: None for k in keys}

    raise TypeError(
        f"Unsupported ObjectStore {type(store).__name__}: expected LocalObjectStore, "
        "S3ObjectStore, or .list_keys(prefix)"
    )


def list_object_keys(store: ObjectStore, prefix: str = "") -> list[str]:
    """List sorted object keys matching ``prefix`` across Local, S3, and test fake stores."""
    return list(list_objects_with_mtime(store, prefix=prefix).keys())


def object_exists(store: ObjectStore, key: str) -> bool:
    """Return True if ``key`` exists in ``store``."""
    clean_key = _normalize_prefix(key)
    if not clean_key:
        raise ValueError(f"Unsafe object key: {key!r}")

    if isinstance(store, LocalObjectStore):
        try:
            store.resolve_existing_path(clean_key)
        except FileNotFoundError:
            return False
        return True

    if isinstance(store, S3ObjectStore):
        try:
            store.client.head_object(Bucket=store.bucket, Key=clean_key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    exists_fn = getattr(store, "exists", None)
    if callable(exists_fn):
        return bool(exists_fn(clean_key))

    list_keys_fn = getattr(store, "list_keys", None)
    if callable(list_keys_fn):
        return clean_key in set(str(k) for k in list_keys_fn(clean_key))

    try:
        store.get_bytes(clean_key)
    except (FileNotFoundError, KeyError):
        return False
    return True
