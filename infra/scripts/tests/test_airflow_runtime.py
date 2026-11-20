"""Operational behavior of the single-host Airflow entrypoint."""

from __future__ import annotations

import importlib.util
import io
import json
import stat
from pathlib import Path
from types import ModuleType

import pytest


def load_runtime() -> ModuleType:
    path = Path(__file__).parents[1] / "airflow-runtime.py"
    spec = importlib.util.spec_from_file_location("airflow_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("unhealthy", [None, "metadatabase", "scheduler", "dag_processor"])
def test_healthcheck_rejects_unhealthy_components(
    monkeypatch: pytest.MonkeyPatch, unhealthy: str | None
) -> None:
    runtime = load_runtime()
    response = {
        component: {"status": "unhealthy" if component == unhealthy else "healthy"}
        for component in ("metadatabase", "scheduler", "dag_processor")
    }
    monkeypatch.setattr(
        runtime.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(response).encode()),
    )
    if unhealthy is None:
        runtime.healthcheck()
    else:
        with pytest.raises(SystemExit, match=unhealthy):
            runtime.healthcheck()


def test_generated_secrets_are_private_and_survive_runtime_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = load_runtime()
    monkeypatch.setenv("AIRFLOW__LOGGING__BASE_LOG_FOLDER", str(tmp_path))
    variables = ("AIRFLOW__API__SECRET_KEY", "AIRFLOW__API_AUTH__JWT_SECRET")
    for variable in variables:
        monkeypatch.delenv(variable, raising=False)
    runtime.configure_secrets()
    first_keys = {variable: runtime.os.environ[variable] for variable in variables}
    assert len(set(first_keys.values())) == 2
    for path in tmp_path.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for variable in variables:
        monkeypatch.delenv(variable)
    runtime.configure_secrets()
    assert {variable: runtime.os.environ[variable] for variable in variables} == first_keys


def test_explicit_secrets_are_not_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = load_runtime()
    monkeypatch.setenv("AIRFLOW__LOGGING__BASE_LOG_FOLDER", str(tmp_path))
    monkeypatch.setenv("AIRFLOW__API__SECRET_KEY", "configured-api-key")
    monkeypatch.setenv("AIRFLOW__API_AUTH__JWT_SECRET", "configured-jwt-key")
    runtime.configure_secrets()
    assert runtime.os.environ["AIRFLOW__API__SECRET_KEY"] == "configured-api-key"
    assert runtime.os.environ["AIRFLOW__API_AUTH__JWT_SECRET"] == "configured-jwt-key"
    assert not list(tmp_path.iterdir())
