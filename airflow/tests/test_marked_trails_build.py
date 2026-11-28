from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from trip_airflow.marked_trails_build import run_marked_trails_build

GIB = 1024**3


def enough_space(_directory: Path) -> int:
    return 100 * GIB


class FakeCli:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> int:
        self.calls.append(list(argv))
        return self.exit_code


def test_region_and_work_dir_are_passed_to_the_cli_with_resource_caps(tmp_path: Path) -> None:
    cli = FakeCli()

    run_marked_trails_build("europe", work_dir=tmp_path, cli_main=cli, free_bytes=enough_space)

    assert cli.calls == [
        ["build-publish", "--region", "europe", "--work-dir", str(tmp_path), "--cap-resources"]
    ]


def test_work_dir_defaults_to_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_WORK_DIR", str(tmp_path / "volume"))
    cli = FakeCli()

    run_marked_trails_build("europe/andorra", cli_main=cli, free_bytes=enough_space)

    assert cli.calls[0][cli.calls[0].index("--work-dir") + 1] == str(tmp_path / "volume")


def test_failed_build_fails_the_task(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="europe.*status 1"):
        run_marked_trails_build(
            "europe", work_dir=tmp_path, cli_main=FakeCli(exit_code=1), free_bytes=enough_space
        )


def test_build_does_not_start_with_less_than_12_gib_free(tmp_path: Path) -> None:
    cli = FakeCli()

    with pytest.raises(RuntimeError, match="12 GiB"):
        run_marked_trails_build(
            "europe", work_dir=tmp_path, cli_main=cli, free_bytes=lambda _directory: 12 * GIB - 1
        )

    assert cli.calls == []


def test_build_starts_with_exactly_12_gib_free(tmp_path: Path) -> None:
    cli = FakeCli()

    run_marked_trails_build(
        "europe", work_dir=tmp_path, cli_main=cli, free_bytes=lambda _directory: 12 * GIB
    )

    assert len(cli.calls) == 1


def test_free_space_is_measured_on_the_work_directory(tmp_path: Path) -> None:
    measured: list[Path] = []

    def record(directory: Path) -> int:
        measured.append(directory)
        return 100 * GIB

    work = tmp_path / "volume" / "work"
    run_marked_trails_build("europe", work_dir=work, cli_main=FakeCli(), free_bytes=record)

    assert measured == [work]
