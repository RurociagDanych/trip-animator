# trip-object-store

Shared `ObjectStore` interface with a traversal-safe Local filesystem backend and an S3-compatible backend (AWS S3, MinIO, RustFS).

## Public API

- `ObjectStore`: `typing.Protocol` with `put_bytes`, `put_file`, `get_bytes`, `get_file`, `open_stream`, `copy`, `delete`, `stat`, `exists`, `iter_objects`.
- `LocalObjectStore`: directory-backed store; `resolve_existing_path(key)` returns the real file path.
- `S3ObjectStore`: S3-compatible store; `.client` and `.bucket` are public escape hatches.
- `ObjectStoreConfig`, `build_object_store(config)`: pick a backend from plain values; no env var reading.
- `materialize(store, key, dest)`: local path for an object (no copy for `LocalObjectStore`).
- `ObjectInfo`, `ObjectStream`: metadata and chunked-read records.
- `parse_byte_range`, `InvalidRangeError`: HTTP `Range` parsing.
- `UnsafeKeyError` (`ValueError`), `is_not_found_error`, `DEFAULT_TRANSFER_CONFIG`.

## Semantics

- Writes are atomic and last-writer-wins. Local writes go through a hidden sibling temp file and `os.replace`; `iter_objects` skips those files.
- A missing object raises `FileNotFoundError` from `get_bytes`, `get_file`, `open_stream`, `copy` (source) and `stat`. `delete` and `exists` never raise for a missing key. `AccessDenied` and other errors stay `ClientError`.
- Keys are validated on both backends: leading `/` is stripped; empty keys, `..` segments, backslashes and NUL raise `UnsafeKeyError`.
- `key_prefix` applies to `S3ObjectStore` only: it is prepended to every key and stripped from `iter_objects` results. The Local backend is scoped by its root directory.
- `S3ObjectStore` takes either a prebuilt `client` or endpoint, credential, region and `botocore_config` arguments, not both. Empty strings count as unset.
- `put_file` and `get_file` stream with a bounded multipart `TransferConfig` (8 MiB, no threads).
- The package has no logging, no retry loop and no environment access; callers own those.

## Usage

```python
from pathlib import Path

import boto3
from trip_object_store import LocalObjectStore, S3ObjectStore

local = LocalObjectStore(Path("data/objects"))
local.put_bytes("outputs/a.json", b"{}", content_type="application/json")
print([o.key for o in local.iter_objects("outputs/")])

s3 = S3ObjectStore(
    bucket="trip-prod",
    client=boto3.client("s3", region_name="eu-central-1"),
    key_prefix="trails",
)
s3.put_file("v1/trails.pmtiles", Path("trails.pmtiles"), cache_control="public, max-age=31536000")
```

## Tests

```bash
uv run pytest packages/object-store/tests -q
uv run mypy packages/object-store/src packages/object-store/tests
```
