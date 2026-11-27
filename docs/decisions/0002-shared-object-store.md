# 0002 Shared object store package

> Purpose: Record how the diverged API and worker object stores become one shared package.
> Audience: contributors, reviewers
> Last reviewed: 2026-11-27

Sections:

- Context
- Decision
- Divergences
- Consequences

## Context

`apps/api/src/trip_api/storage.py` and `apps/worker/src/trip_worker/storage.py` each define
`ObjectStore`, `LocalObjectStore` and `S3ObjectStore`. The copies differ in 5 diff hunks
(348 changed lines). The API copy has ranged streaming and copy, the worker copy has bounded
file uploads, and both differ in S3 error handling. Airflow imports the API copy, and the
telemetry package duck-types a third, read-only variant. A future S3 backend for the Marked
Trails tile store (ADR 0001) should reuse the same code instead of adding a fourth copy.

## Decision

One package, `packages/object-store` (distribution `trip-object-store`, import
`trip_object_store`), owns the interface, both backends and the factory. Apps keep their
settings classes and env var names and map them to an `ObjectStoreConfig`.

The interface is a `typing.Protocol`, so existing structural test fakes stay valid:

| Method | Contract |
|---|---|
| `put_bytes(key, data, *, content_type, cache_control=None)` | Atomic, last writer wins. |
| `put_file(key, source, *, content_type, cache_control=None)` | Synchronous, bounded multipart on S3, source untouched, never partially visible. |
| `get_bytes(key)` / `get_file(key, dest)` | `FileNotFoundError` when missing; `get_file` streams to disk. |
| `open_stream(key, *, range_header=None, chunk_size=64 KiB)` | Returns `ObjectStream`; raises `InvalidRangeError` for unsatisfiable ranges. |
| `copy(src_key, dst_key, *, content_type=None, cache_control=None)` | `FileNotFoundError` for a missing source; S3 replaces metadata only when a value is given. |
| `delete(key)` | Idempotent; other errors propagate. |
| `stat(key)` / `exists(key)` | `ObjectInfo(key, size, last_modified, etag)`; `exists` never raises for a missing key. |
| `iter_objects(prefix="")` | Sorted `ObjectInfo` under a prefix, paginated on S3. |

Both backends validate keys the same way (no empty keys, `..`, backslashes or NUL) and accept an
optional `key_prefix`. `S3ObjectStore` also accepts an injected boto3 client, a botocore config
and a transfer config. Its `client` and `bucket` stay public for S3-only features (Gold ETag sync,
pyarrow file system, same-bucket remote render copies). The package does not log and has no
retry loop. S3 calls keep the botocore default retries, and transient render failures stay
handled by the worker's job-level requeue and backoff, which needs storage errors to propagate.

## Divergences

| Topic | Chosen behavior | Source |
|---|---|---|
| Not-found on S3 | Error code `NoSuchKey`, `404` or `NotFound`, or HTTP 404. `AccessDenied` stays a `ClientError`. | API |
| S3 response body | Closed after `get_bytes`. | API |
| S3 `delete` of a missing key | Ignored. | API |
| S3 constructor | Keyword-only, credentials and region default to `None`, empty strings normalized to `None` (region included). | worker defaults, API normalization |
| File upload | `upload_file` with a classic, single-threaded `TransferConfig`, 8 MiB threshold and parts. | worker |
| Ranged reads and copy | Kept as in the API copy, now available to the worker. | API |
| Local atomic writes | One hidden sibling temp file plus `os.replace` for `put_bytes`, `put_file`, `copy` and `get_file`. Listings skip temp files. | API mechanism |
| Local copy mtime | `shutil.copyfile`, so the copy gets a fresh mtime like S3 `CopyObject`. The orphan sweep deletes by mtime. | new |
| Content metadata | `content_type` and optional `cache_control` on S3; ignored locally. | union |
| Config and env vars | Unchanged names: `OBJECT_STORE_BACKEND`, `LOCAL_OBJECT_STORE_DIR`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_REGION`. The package reads no env vars. | both |
| Logging | None in the package; callers log with their own context. | both |

## Consequences

- The worker gains `open_stream`, `copy`, `stat`, `exists` and `iter_objects`; the API gains
  `put_file` and `get_file`, so whole-file reads into memory for MP4 transfers can be removed.
- Visible behavior changes: worker S3 `delete` stops raising on `NoSuchKey`; worker S3 maps
  HTTP-404-only errors to `FileNotFoundError`; local copies no longer keep the source mtime; local
  `put_bytes` becomes atomic; S3 keys are validated like local keys.
- `trip_api.storage` stays a thin re-export until Airflow imports the package directly.
- The telemetry package keeps its narrow read-only `ByteStore` protocol; the shared store satisfies it.
- A tile store S3 backend can be built on `put_file`, atomic `put_bytes` for the `current.json`
  pointer, `iter_objects` with `delete` for pruning, and `cache_control` for immutable versus
  no-cache files, without changes to the interface.
