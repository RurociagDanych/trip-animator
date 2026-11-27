"""Compatibility re-export of `trip_object_store` for consumers that still import it from here."""

from trip_object_store import (
    InvalidRangeError,
    LocalObjectStore,
    ObjectStore,
    ObjectStream,
    S3ObjectStore,
    parse_byte_range,
)

__all__ = [
    "InvalidRangeError",
    "LocalObjectStore",
    "ObjectStore",
    "ObjectStream",
    "S3ObjectStore",
    "parse_byte_range",
]
