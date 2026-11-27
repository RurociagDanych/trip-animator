from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from trip_airflow.marked_trails_build import run_marked_trails_build


class FakeCli:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self.exit_code


def test_region_and_work_dir_are_passed_to_the_cli_with_resource_caps(tmp_path: Path) -> None:
    cli = FakeCli()

    run_marked_trails_build("europe", work_dir=tmp_path, cli_main=cli)

    assert cli.calls == [
        ["build-publish", "--region", "europe", "--work-dir", str(tmp_path), "--cap-resources"]
    ]


def test_work_dir_defaults_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_WORK_DIR", str(tmp_path / "volume"))
    cli = FakeCli()

    run_marked_trails_build("europe/andorra", cli_main=cli)

    assert cli.calls[0][cli.calls[0].index("--work-dir") + 1] == str(tmp_path / "volume")


def test_failed_build_fails_the_task(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="europe.*status 1"):
        run_marked_trails_build("europe", work_dir=tmp_path, cli_main=FakeCli(exit_code=1))
