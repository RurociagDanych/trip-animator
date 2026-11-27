"""Object key and prefix validation shared by every backend."""

from __future__ import annotations


class UnsafeKeyError(ValueError):
    """Raised when an object key or prefix is empty, traverses upward or is malformed."""


def _check_segments(clean: str, original: str, kind: str) -> None:
    if "\\" in clean or "\x00" in clean or ".." in clean.split("/"):
        raise UnsafeKeyError(f"Unsafe object {kind}: {original!r}")


def normalize_key(key: str) -> str:
    """Return the key without leading slashes; raise `UnsafeKeyError` if it is not allowed."""
    clean = key.lstrip("/")
    if not clean:
        raise UnsafeKeyError(f"Unsafe object key: {key!r}")
    _check_segments(clean, key, "key")
    return clean


def normalize_prefix(prefix: str) -> str:
    """Return a listing prefix without leading slashes; the empty prefix is allowed."""
    clean = prefix.lstrip("/")
    _check_segments(clean, prefix, "prefix")
    return clean
