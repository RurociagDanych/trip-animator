"""Key validation and HTTP byte-range parsing."""

from __future__ import annotations

import pytest
from trip_object_store import InvalidRangeError, UnsafeKeyError, parse_byte_range
from trip_object_store.keys import normalize_key, normalize_prefix


def test_normalize_key_strips_leading_slashes() -> None:
    assert normalize_key("/outputs/a.mp4") == "outputs/a.mp4"
    assert normalize_key("//a") == "a"
    assert normalize_key("a..b/c") == "a..b/c"


@pytest.mark.parametrize(
    "key", ["", "/", "../x", "a/../b", "a/..", "a\\b", "a\x00b", "outputs/../../escape.mp4"]
)
def test_normalize_key_rejects_unsafe_keys(key: str) -> None:
    with pytest.raises(UnsafeKeyError, match="Unsafe object key"):
        normalize_key(key)


def test_unsafe_key_error_is_a_value_error() -> None:
    assert issubclass(UnsafeKeyError, ValueError)


def test_normalize_prefix_allows_empty_and_rejects_traversal() -> None:
    assert normalize_prefix("") == ""
    assert normalize_prefix("/silver/") == "silver/"
    with pytest.raises(UnsafeKeyError, match="Unsafe object prefix"):
        normalize_prefix("../silver")


def test_parse_byte_range_unrecognized_forms_return_none() -> None:
    assert parse_byte_range("items=0-5", 100) is None
    assert parse_byte_range("bytes=0-1,2-3", 100) is None
    assert parse_byte_range("bytes=abc-def", 100) is None
    assert parse_byte_range("bytes=-abc", 100) is None
    assert parse_byte_range("bytes=0-xyz", 100) is None


def test_parse_byte_range_valid_forms() -> None:
    assert parse_byte_range("bytes=0-9", 26) == (0, 9)
    assert parse_byte_range("bytes=10-", 26) == (10, 25)
    assert parse_byte_range("bytes=-5", 26) == (21, 25)
    assert parse_byte_range("bytes=-999", 26) == (0, 25)
    assert parse_byte_range("bytes=20-999", 26) == (20, 25)


@pytest.mark.parametrize(
    ("header", "size"),
    [("bytes=-0", 26), ("bytes=999-", 26), ("bytes=10-5", 26), ("bytes=0-0", 0), ("bytes=-5", 0)],
)
def test_parse_byte_range_unsatisfiable(header: str, size: int) -> None:
    with pytest.raises(InvalidRangeError) as info:
        parse_byte_range(header, size)
    assert info.value.total_size == size
