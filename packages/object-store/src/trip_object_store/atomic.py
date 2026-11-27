"""Atomic file replacement through a hidden sibling temp file."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_TEMP_NAME = re.compile(r"^\..+\.[0-9a-f]{32}\.tmp$")


def is_temp_name(name: str) -> bool:
    """Return whether `name` is a staging file created by `staged_path`."""
    return _TEMP_NAME.match(name) is not None


@contextmanager
def staged_path(target: Path) -> Iterator[Path]:
    """Yield a temp path beside `target` and `os.replace` it onto `target` on success.

    The temp file is on the destination filesystem so the rename is atomic. It is removed
    when the body raises, so a failed write never leaves a partial object behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        yield tmp
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
