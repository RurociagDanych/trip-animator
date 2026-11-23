from __future__ import annotations

import io
import json
import logging
import sys
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
    logging.getLogger("trip_worker").setLevel(logging.NOTSET)
    logging.getLogger("playwright").setLevel(logging.NOTSET)


def _record(
    msg: str,
    *,
    args: tuple[Any, ...] = (),
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
        args,
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


def test_json_formatter_does_not_leak_secrets_from_args_traceback_or_extras() -> None:
    try:
        raise RuntimeError(
            "connect failed for postgresql+psycopg://trip:dsnsecret@db.internal:5432/app"
        )
    except RuntimeError:
        exc_info = sys.exc_info()

    record = _record(
        "Fetch from %s and %s",
        args=(
            "https://user:s3cr3t@tiles.example.com/a.pbf",
            "https://bucket.s3.example.com/o.webp?X-Amz-Algorithm=AWS4"
            "&X-Amz-Signature=abc123&X-Amz-Credential=AKIAEXAMPLE",
        ),
        exc_info=exc_info,
        event="probe_finish",
        password="p4ssw0rd",
        db_dsn="postgresql://trip:hunter2@db/app",
    )

    line = JsonFormatter().format(record)

    for secret in ("dsnsecret", "s3cr3t", "abc123", "AKIAEXAMPLE", "p4ssw0rd", "hunter2"):
        assert secret not in line
    assert "tiles.example.com" in line
    assert "db.internal" in line
    assert "bucket.s3.example.com/o.webp" in line


@pytest.mark.parametrize(
    "raw",
    [
        "https://bucket.s3.us-east-1.amazonaws.com/o.webp?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAEXAMPLE%2F20261123%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20261123T000000Z&X-Amz-Expires=300&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=deadbeef1234",
        "https://acct.blob.core.windows.net/c/b.mp4?sv=2024-11-04&sig=sasSig%2Bval%3D&se=2026-12-01",
        "https://api.example.com/v1?api_key=ak-live-123",
        "https://api.example.com/v1?apikey=ak-live-456",
        "https://api.example.com/v1?x-api-key=ak-live-789",
        "https://maps.example.com/t?key=gmapkey777",
        "https://svc.example.com/x?auth=authtok999",
        "https://svc.example.com/x?credential=credval555",
        "https://svc.example.com/x?session=sessid444",
        "https://svc.example.com/x?cookie=cookval333",
        "https://svc.example.com/x?access_token=tokval222",
        "Authorization: Bearer XYZtoken111",
        "Authorization: Basic dXNlcjpwYXNz222",
        "authorization: token ghp_tok333",
        "Authorization: opaqueraw444",
        "'Authorization': 'Bearer quoted555'",
        "Cookie: session=sessck555; theme=dark",
    ],
)
def test_redacts_secret_values_in_message(raw: str) -> None:
    line = JsonFormatter().format(_record(raw))

    for secret in (
        "deadbeef1234",
        "sasSig",
        "ak-live-123",
        "ak-live-456",
        "ak-live-789",
        "gmapkey777",
        "authtok999",
        "credval555",
        "sessid444",
        "cookval333",
        "tokval222",
        "XYZtoken111",
        "dXNlcjpwYXNz222",
        "ghp_tok333",
        "opaqueraw444",
        "quoted555",
        "sessck555",
        "theme=dark",
    ):
        assert secret not in line


def test_bearer_header_keeps_scheme_and_masks_token() -> None:
    out = _format(_record("Authorization: Bearer XYZ"))

    assert out["message"] == "Authorization: Bearer ***"


def test_presigned_s3_url_keeps_host_and_path_and_masks_signature() -> None:
    out = _format(
        _record(
            "GET https://bucket.s3.us-east-1.amazonaws.com/a/o.webp"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef1234&X-Amz-Expires=300"
        )
    )

    assert out["message"] == (
        "GET https://bucket.s3.us-east-1.amazonaws.com/a/o.webp"
        "?X-Amz-Algorithm=***&X-Amz-Signature=***&X-Amz-Expires=***"
    )


def test_redacts_userinfo_up_to_last_at_sign_before_path() -> None:
    out = _format(_record("dsn postgresql://u:pw@ss@h/db"))

    assert out["message"] == "dsn postgresql://***@h/db"
    assert "ss@" not in out["message"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("connect //user:pw@host/db", "connect //***@host/db"),
        ("open //u:p@ss@host?x=1", "open //***@host?x=1"),
        (
            "https://u:p@a.example.com:8443/x?y=1#frag@z",
            "https://***@a.example.com:8443/x?y=1#frag@z",
        ),
        ("https://example.com/path/user@mail", "https://example.com/path/user@mail"),
    ],
)
def test_userinfo_redaction_variants(raw: str, expected: str) -> None:
    assert _format(_record(raw))["message"] == expected


def test_level_env_var_controls_worker_loggers_only(
    restore_root_logging: None,
) -> None:
    stream = io.StringIO()
    configure_logging(environ={"TRIP_WORKER_LOG_LEVEL": "WARNING"}, stream=stream)

    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("trip_worker.runner").info("hidden info", extra={"event": "x"})
    logging.getLogger("trip_worker.runner").warning("visible warning", extra={"event": "y"})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["visible warning"]


def test_level_defaults_to_info_for_worker_and_warning_for_root(
    restore_root_logging: None,
) -> None:
    configure_logging(environ={}, stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("trip_worker.runner").getEffectiveLevel() == logging.INFO


@pytest.mark.parametrize(
    "third_party",
    [
        "botocore.hooks",
        "urllib3.connectionpool",
        "boto3",
        "s3transfer",
        "psycopg",
        "httpx",
        "httpcore.connection",
    ],
)
def test_third_party_debug_and_info_stay_hidden_when_worker_level_is_debug(
    third_party: str,
    restore_root_logging: None,
) -> None:
    stream = io.StringIO()
    configure_logging(environ={"TRIP_WORKER_LOG_LEVEL": "DEBUG"}, stream=stream)

    logging.getLogger(third_party).debug("third party debug", extra={"event": "x"})
    logging.getLogger(third_party).info("third party info", extra={"event": "x"})
    logging.getLogger("trip_worker.runner").debug("worker debug", extra={"event": "y"})

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["worker debug"]


def test_playwright_level_resets_to_inherit_when_env_var_is_removed(
    restore_root_logging: None,
) -> None:
    configure_logging(
        environ={"TRIP_WORKER_PLAYWRIGHT_LOG_LEVEL": "DEBUG"},
        stream=io.StringIO(),
    )
    assert logging.getLogger("playwright").level == logging.DEBUG

    configure_logging(environ={}, stream=io.StringIO())

    assert logging.getLogger("playwright").level == logging.NOTSET


def test_invalid_level_values_warn_once_each_and_fall_back(
    restore_root_logging: None,
) -> None:
    stream = io.StringIO()
    configure_logging(
        environ={
            "TRIP_WORKER_LOG_LEVEL": "bogus",
            "TRIP_WORKER_PLAYWRIGHT_LOG_LEVEL": "loud",
        },
        stream=stream,
    )

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["level"] for line in lines] == ["WARNING", "WARNING"]
    assert "TRIP_WORKER_LOG_LEVEL" in lines[0]["message"]
    assert "TRIP_WORKER_PLAYWRIGHT_LOG_LEVEL" in lines[1]["message"]
    assert "bogus" not in stream.getvalue()
    assert logging.getLogger("trip_worker").level == logging.INFO
    assert logging.getLogger("playwright").level == logging.NOTSET


def test_valid_level_values_emit_no_setup_warning(
    restore_root_logging: None,
) -> None:
    stream = io.StringIO()
    configure_logging(
        environ={"TRIP_WORKER_LOG_LEVEL": " debug ", "TRIP_WORKER_PLAYWRIGHT_LOG_LEVEL": "WARNING"},
        stream=stream,
    )

    assert stream.getvalue() == ""
    assert logging.getLogger("trip_worker").level == logging.DEBUG


def test_repeat_configure_writes_to_the_latest_stream(
    restore_root_logging: None,
) -> None:
    first = io.StringIO()
    second = io.StringIO()
    configure_logging(environ={}, stream=first)
    configure_logging(environ={}, stream=second)

    logging.getLogger("trip_worker.runner").warning("after reconfigure", extra={"event": "x"})

    assert first.getvalue() == ""
    assert [json.loads(line)["message"] for line in second.getvalue().splitlines()] == [
        "after reconfigure"
    ]


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
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("trip_worker.runner").getEffectiveLevel() == logging.INFO
