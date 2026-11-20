"""Supervise the single-host Airflow runtime and check scheduler health."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def healthcheck() -> None:
    with urllib.request.urlopen(
        "http://127.0.0.1:8080/api/v2/monitor/health", timeout=5
    ) as response:
        health = json.load(response)
    for component in ("metadatabase", "scheduler", "dag_processor"):
        if health.get(component, {}).get("status") != "healthy":
            raise SystemExit(f"Airflow {component} is not healthy")


def configure_secrets() -> None:
    directory = Path(os.environ.get("AIRFLOW__LOGGING__BASE_LOG_FOLDER", "/opt/airflow/logs"))
    directory.mkdir(parents=True, exist_ok=True)
    for variable, filename in (
        ("AIRFLOW__API__SECRET_KEY", ".api-secret"),
        ("AIRFLOW__API_AUTH__JWT_SECRET", ".jwt-secret"),
    ):
        if os.environ.get(variable):
            continue
        path = directory / filename
        try:
            with path.open("x", encoding="utf-8") as stream:
                path.chmod(0o600)
                stream.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
        os.environ[variable] = path.read_text(encoding="utf-8").strip()


def run() -> int:
    configure_secrets()
    # Parsing first also migrates an existing paused legacy sync DAG on upgrades.
    subprocess.run(["/app/.venv/bin/airflow", "dags", "reserialize"], check=True)
    subprocess.run(
        ["/app/.venv/bin/airflow", "dags", "unpause", "trip_strava_incremental_sync"], check=True
    )
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    result = 0
    try:
        for arguments in (
            # Compose publishes the container port on host loopback only.
            ["api-server", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"],  # noqa: S104
            ["scheduler"],
            ["dag-processor"],
            ["triggerer"],
        ):
            children.append(
                subprocess.Popen(  # noqa: S603 - fixed executable and arguments only
                    ["/app/.venv/bin/airflow", *arguments], start_new_session=True
                )
            )
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    result = child.returncode or 1
                    stopping = True
                    break
            if not stopping:
                time.sleep(1)
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 25
        for child in children:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return result


if __name__ == "__main__":
    if sys.argv[1:] == ["healthcheck"]:
        healthcheck()
    else:
        raise SystemExit(run())
