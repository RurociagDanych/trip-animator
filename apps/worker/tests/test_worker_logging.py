from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from trip_worker.logging_setup import JsonFormatter, configure_logging


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
    root.setLevel(saved_level)
    playwright_logger = logging.getLogger("playwright")
    playwright_logger.setLevel(logging.NOTSET)


def _record(
    msg: str,
    *,
    level: int = logging.INFO,
    name: str = "trip_worker.runner",
    exc_info: Any = None,
    **extra: Any,
) -> logging.LogRecord:
    logger = logging.getLogger(name)
    return logger.makeRecord(
        name,
        level,
        __file__,
        1,
        msg,
        (),
        exc_info,
        extra=extra or None,
    )


def _format(record: logging.LogRecord) -> dict[str, Any]:
    line = JsonFormatter().format(record)
    assert "\n" not in line
    parsed: dict[str, Any] = json.loads(line)
    return parsed


def test_json_formatter_emits_core_and_context_fields() -> None:
    record = _record(
        "Chunk finished",
        event="chunk_finish",
        job_id="3f2c0a4e-1111-4222-8333-444455556666",
        chunk_id="9a8b7c6d-1111-4222-8333-444455556666",
        stage="chunk",
        duration_ms=12.5,
        rss_mb=101.2,
        status="succeeded",
    )

    out = _format(record)

    assert set(out) >= {"ts", "level", "logger", "event", "message"}
    assert out["level"] == "INFO"
    assert out["logger"] == "trip_worker.runner"
    assert out["event"] == "chunk_finish"
    assert out["message"] == "Chunk finished"
    assert out["job_id"] == "3f2c0a4e-1111-4222-8333-444455556666"
    assert out["chunk_id"] == "9a8b7c6d-1111-4222-8333-444455556666"
    assert out["stage"] == "chunk"
    assert out["duration_ms"] == 12.5
    assert out["rss_mb"] == 101.2
    assert out["status"] == "succeeded"
    parsed_ts = datetime.fromisoformat(out["ts"])
    assert parsed_ts.utcoffset() == timedelta(0)


def test_json_formatter_omits_context_fields_that_were_not_provided() -> None:
    out = _format(_record("Worker initialized", event="worker_start"))

    assert out["event"] == "worker_start"
    for absent in ("job_id", "chunk_id", "stage", "duration_ms", "rss_mb", "exc_info"):
        assert absent not in out


def test_json_formatter_renders_exc_info_as_string() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    out = _format(_record("Render job failed", level=logging.ERROR, exc_info=exc_info))

    assert out["level"] == "ERROR"
    assert isinstance(out["exc_info"], str)
    assert "Traceback (most recent call last)" in out["exc_info"]
    assert "ValueError: boom" in out["exc_info"]


def test_json_formatter_does_not_leak_secrets() -> None:
    try:
        raise RuntimeError(
            "connect failed for postgresql+psycopg://trip:hunter2@db.internal:5432/app"
        )
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()

    record = _record(
        "Fetch from https://user:s3cr3t@tiles.example.com/a.pbf and "
        "https://bucket.s3.example.com/o.webp?X-Amz-Algorithm=AWS4&X-Amz-Signature=abc123&X-Amz-Credential=AKIAEXAMPLE",
        exc_info=exc_info,
        event="probe_finish",
        password="p4ssw0rd",
        db_dsn="postgresql://trip:dsnsecret@db/app",
    )

    line = JsonFormatter().format(record)

    for secret in ("hunter2", "s3cr3t", "abc123", "AKIAEXAMPLE", "p4ssw0rd", "dsnsecret"):
        assert secret not in line
    assert "tiles.example.com" in line
    assert "db.internal" in line
    assert "password" not in json.loads(line)
    assert "db_dsn" not in json.loads(line)


def test_level_env_var_controls_root_level(
    restore_root_logging: None,
) -> None:
    stream = io.StringIO()
    configure_logging(environ={"TRIP_WORKER_LOG_LEVEL": "WARNING"}, stream=stream)

    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("trip_worker.runner").info("hidden info", extra={"event": "x"})
    logging.getLogger("trip_worker.runner").warning("visible warning", extra={"event": "y"})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["visible warning"]


def test_level_defaults_to_info_and_ignores_unknown_values(
    restore_root_logging: None,
) -> None:
    configure_logging(environ={}, stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("httpx").level == logging.WARNING

    configure_logging(environ={"TRIP_WORKER_LOG_LEVEL": "bogus"}, stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_installs_single_json_handler(
    restore_root_logging: None,
) -> None:
    stream = io.StringIO()
    configure_logging(environ={}, stream=stream)
    configure_logging(environ={}, stream=stream)

    installed = [h for h in logging.getLogger().handlers if isinstance(h.formatter, JsonFormatter)]
    assert len(installed) == 1


def test_playwright_debug_override_only_touches_playwright_logger(
    restore_root_logging: None,
) -> None:
    configure_logging(
        environ={"TRIP_WORKER_PLAYWRIGHT_LOG_LEVEL": "DEBUG"},
        stream=io.StringIO(),
    )

    assert logging.getLogger("playwright").level == logging.DEBUG
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("trip_worker.runner").getEffectiveLevel() == logging.INFO
