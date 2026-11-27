"""HTTP byte-range parsing and the chunked stream returned by `open_stream`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


class InvalidRangeError(ValueError):
    """Raised when an HTTP Range header cannot be satisfied for an object."""

    def __init__(self, total_size: int) -> None:
        super().__init__(f"Requested byte range is not satisfiable for size {total_size}")
        self.total_size = total_size


@dataclass(slots=True)
class ObjectStream:
    """Chunked object stream with byte-range metadata."""

    chunks: Iterator[bytes]
    total_size: int
    content_length: int
    start: int
    end: int
    is_partial: bool


def parse_byte_range(range_header: str, total_size: int) -> tuple[int, int] | None:
    """Parse an HTTP `Range: bytes=...` header into an inclusive `(start, end)` pair.

    Returns `None` when the header uses a non-bytes unit, or specifies a multi-range or
    syntactically unrecognized range so callers can serve a full 200 OK.
    Raises `InvalidRangeError` when a syntactically valid range cannot be satisfied.
    """
    value = range_header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].strip()
    if "," in spec or "-" not in spec:
        return None

    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    if start_str == "":
        if not (end_str.isascii() and end_str.isdigit()):
            return None
        suffix_len = int(end_str)
        if suffix_len == 0 or total_size == 0:
            raise InvalidRangeError(total_size)
        return max(total_size - suffix_len, 0), total_size - 1

    if not (start_str.isascii() and start_str.isdigit()):
        return None
    start = int(start_str)
    if total_size == 0 or start >= total_size:
        raise InvalidRangeError(total_size)

    if end_str == "":
        return start, total_size - 1
    if not (end_str.isascii() and end_str.isdigit()):
        return None
    end = int(end_str)
    if end < start:
        raise InvalidRangeError(total_size)
    return start, min(end, total_size - 1)
