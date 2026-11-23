"""JSON stdout logging for the render worker, configured once at process start."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, TextIO

_LOG_LEVEL_ENV = "TRIP_WORKER_LOG_LEVEL"
_PLAYWRIGHT_LEVEL_ENV = "TRIP_WORKER_PLAYWRIGHT_LOG_LEVEL"
_HANDLER_MARKER = "_trip_worker_json_handler"

# Only these attributes reach the output, so ad-hoc extras cannot leak values such as
# passwords or DSNs into Loki.
_CONTEXT_FIELDS = ("job_id", "chunk_id", "stage", "status", "duration_ms", "rss_mb")

# Root stays at WARNING so third-party loggers (botocore, urllib3, httpx request URLs with
# presigned signatures) cannot emit DEBUG or INFO; only trip_worker.* follows the level env var.
_WORKER_LOGGER = "trip_worker"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"((?:\b[a-zA-Z][\w+.-]*:)?//)[^\s/?#]*@"), r"\1***@"),
    # The header value may carry a scheme; keep the scheme so the log still shows which
    # auth type was sent, and mask only the credential.
    (
        re.compile(
            r"((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|token|digest)\s+)?)"
            r"[^\s,;\"']+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # Cookie values are `name=value; name=value`, so the mask runs to the end of the line
    # or the closing quote of a repr'd header.
    (
        re.compile(r"(cookie[\"']?\s*[:=]\s*[\"']?)[^\r\n\"']+", re.IGNORECASE),
        r"\1***",
    ),
    (
        re.compile(
            r"((?:[?&;]|\b)(?:X-Amz-[\w-]+|sig|"
            r"[\w-]*(?:signature|token|secret|password|passwd|api[_-]?key|apikey|key|auth|"
            r"credential|session|cookie)[\w-]*)=)"
            r"[^&\s\"']+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)


def _redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def current_rss_mb() -> float | None:
    """Return this process's current resident set size in MiB, or None off Linux."""
    # VmRSS is the live footprint; ru_maxrss only reports the peak, which cannot show growth
    # between chunk start and end. Chromium child processes are not included.
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def elapsed_ms(started: float) -> float:
    """Return milliseconds elapsed since a `time.monotonic()` reading, rounded to 0.1 ms."""
    return round((time.monotonic() - started) * 1000.0, 1)


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line with context fields and redaction."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single-line JSON document."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "message": _redact(record.getMessage()),
        }
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


class _StdoutJsonHandler(logging.Handler):
    """Write JSON lines to the stdout that is current at emit time."""

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__()
        self._stream = stream
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted record and flush so container log drivers see it promptly."""
        try:
            stream = self._stream if self._stream is not None else sys.stdout
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


def _parse_level(raw: str | None, *, default: int) -> int:
    if not raw or not raw.strip():
        return default
    level = logging.getLevelNamesMapping().get(raw.strip().upper())
    return level if level is not None else default


def configure_logging(
    environ: Mapping[str, str] | None = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """Install the JSON stdout handler on the root logger once and apply level env vars."""
    env = os.environ if environ is None else environ
    root = logging.getLogger()
    if not any(getattr(handler, _HANDLER_MARKER, False) for handler in root.handlers):
        handler = _StdoutJsonHandler(stream)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger(_WORKER_LOGGER).setLevel(
        _parse_level(env.get(_LOG_LEVEL_ENV), default=logging.INFO)
    )

    playwright_raw = env.get(_PLAYWRIGHT_LEVEL_ENV)
    if playwright_raw and playwright_raw.strip():
        logging.getLogger("playwright").setLevel(
            _parse_level(playwright_raw, default=logging.NOTSET)
        )
