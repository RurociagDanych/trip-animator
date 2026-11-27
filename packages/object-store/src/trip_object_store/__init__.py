"""Shared ObjectStore interface with Local filesystem and S3-compatible backends."""

from trip_object_store.base import ObjectInfo, ObjectStore
from trip_object_store.config import ObjectStoreConfig, build_object_store, materialize
from trip_object_store.keys import UnsafeKeyError
from trip_object_store.local import LocalObjectStore
from trip_object_store.ranges import InvalidRangeError, ObjectStream, parse_byte_range
from trip_object_store.s3 import DEFAULT_TRANSFER_CONFIG, S3ObjectStore, is_not_found_error

__all__ = [
    "DEFAULT_TRANSFER_CONFIG",
    "InvalidRangeError",
    "LocalObjectStore",
    "ObjectInfo",
    "ObjectStore",
    "ObjectStoreConfig",
    "ObjectStream",
    "S3ObjectStore",
    "UnsafeKeyError",
    "build_object_store",
    "is_not_found_error",
    "materialize",
    "parse_byte_range",
]
