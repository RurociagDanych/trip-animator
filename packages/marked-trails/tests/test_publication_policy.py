import json
from pathlib import Path

import pytest
from trip_marked_trails.cli import BuildStep, main
from trip_marked_trails.pipeline import BuildArtifact


def build_of(size: int, trail_count: int = 100) -> BuildStep:
    def step(region: str, work_dir: Path) -> BuildArtifact:
        work_dir.mkdir(parents=True, exist_ok=True)
        pmtiles = work_dir / "fake.pmtiles"
        with pmtiles.open("wb") as fh:
            fh.truncate(size)
        return BuildArtifact(
            pmtiles_path=pmtiles,
            osm_data_date="2026-11-26T20:21:03Z",
            marked_trail_count=trail_count,
        )

    return step


def publish(tmp_path: Path, build_date: str, build_step: BuildStep, *extra_args: str) -> int:
    return main(
        [
            "build-publish",
            "--region",
            "europe/andorra",
            "--store-dir",
            str(tmp_path / "store"),
            "--work-dir",
            str(tmp_path / "work"),
            "--build-date",
            build_date,
            *extra_args,
        ],
        build_step=build_step,
    )


def pointer_of(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "store" / "current.json").read_text(encoding="utf-8"))


def build_ids(tmp_path: Path) -> list[str]:
    builds = tmp_path / "store" / "builds"
    return sorted({path.parent.name for path in builds.rglob("*") if path.is_file()})


def test_first_build_is_checked_only_against_the_size_budget(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(500, trail_count=7)) == 0

    pointer = pointer_of(tmp_path)
    assert pointer["current_build_id"] == "2026-11-01"
    assert pointer["previous_build_id"] is None
    assert build_ids(tmp_path) == ["2026-11-01"]


def test_second_build_switches_the_pointer_and_keeps_both_builds(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(1000)) == 0
    assert publish(tmp_path, "2026-11-02", build_of(1100, trail_count=110)) == 0

    pointer = pointer_of(tmp_path)
    assert pointer["current_build_id"] == "2026-11-02"
    assert pointer["previous_build_id"] == "2026-11-01"
    assert pointer["size_bytes"] == 1100
    assert pointer["marked_trail_count"] == 110
    assert build_ids(tmp_path) == ["2026-11-01", "2026-11-02"]


def test_third_build_deletes_the_oldest_before_building(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(1000)) == 0
    assert publish(tmp_path, "2026-11-02", build_of(1000)) == 0
    seen_during_build: list[list[str]] = []
    inner = build_of(1000)

    def observing_build(region: str, work_dir: Path) -> BuildArtifact:
        seen_during_build.append(build_ids(tmp_path))
        return inner(region, work_dir)

    assert publish(tmp_path, "2026-11-03", observing_build) == 0

    assert seen_during_build == [["2026-11-02"]]
    pointer = pointer_of(tmp_path)
    assert pointer["current_build_id"] == "2026-11-03"
    assert pointer["previous_build_id"] == "2026-11-02"
    assert build_ids(tmp_path) == ["2026-11-02", "2026-11-03"]


@pytest.mark.parametrize(
    ("size", "trail_count"),
    [(1300, 100), (700, 100), (1000, 130), (1000, 70)],
    ids=["size-too-large", "size-too-small", "count-too-large", "count-too-small"],
)
def test_validation_failure_keeps_the_pointer_and_exits_non_zero(
    tmp_path: Path, size: int, trail_count: int
) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(1000)) == 0
    pointer_before = (tmp_path / "store" / "current.json").read_bytes()

    assert publish(tmp_path, "2026-11-02", build_of(size, trail_count)) == 1

    assert (tmp_path / "store" / "current.json").read_bytes() == pointer_before
    assert build_ids(tmp_path) == ["2026-11-01"]


def test_builds_at_twenty_percent_from_the_current_build_are_accepted(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(1000, trail_count=100)) == 0
    assert publish(tmp_path, "2026-11-02", build_of(800, trail_count=120)) == 0

    assert pointer_of(tmp_path)["current_build_id"] == "2026-11-02"


def test_build_failure_keeps_the_pointer_and_exits_non_zero(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(1000)) == 0
    pointer_before = (tmp_path / "store" / "current.json").read_bytes()

    def failing_build(region: str, work_dir: Path) -> BuildArtifact:
        raise RuntimeError("planetiler exited with status 1")

    assert publish(tmp_path, "2026-11-02", failing_build) == 1

    assert (tmp_path / "store" / "current.json").read_bytes() == pointer_before
    assert build_ids(tmp_path) == ["2026-11-01"]


def test_build_over_the_size_budget_is_rejected_even_for_the_first_build(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(2001), "--size-budget-bytes", "2000") == 1

    assert not (tmp_path / "store" / "current.json").exists()
    assert build_ids(tmp_path) == []


def test_build_at_the_size_budget_is_accepted(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(2000), "--size-budget-bytes", "2000") == 0


def test_default_size_budget_is_one_gigabyte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MARKED_TRAILS_SIZE_BUDGET_BYTES", raising=False)

    assert publish(tmp_path, "2026-11-01", build_of(1024**3 + 1)) == 1
    assert publish(tmp_path, "2026-11-01", build_of(1024**3)) == 0


def test_size_budget_can_be_set_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_SIZE_BUDGET_BYTES", "999")

    assert publish(tmp_path, "2026-11-01", build_of(1000)) == 1


def test_budget_exceeded_on_a_later_build_keeps_the_pointer(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-01", build_of(1000)) == 0
    pointer_before = (tmp_path / "store" / "current.json").read_bytes()

    assert publish(tmp_path, "2026-11-02", build_of(1000), "--size-budget-bytes", "999") == 1

    assert (tmp_path / "store" / "current.json").read_bytes() == pointer_before


def test_current_build_without_a_trail_count_skips_the_count_check(tmp_path: Path) -> None:
    store_dir = tmp_path / "store"
    (store_dir / "builds/2026-11-01").mkdir(parents=True)
    (store_dir / "builds/2026-11-01/marked-trails.pmtiles").write_bytes(b"x" * 1000)
    (store_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "region": "europe/andorra",
                "current_build_id": "2026-11-01",
                "previous_build_id": None,
                "pmtiles_key": "builds/2026-11-01/marked-trails.pmtiles",
                "size_bytes": 1000,
                "sha256": "00",
                "osm_data_date": None,
                "built_at": "2026-11-01T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    assert publish(tmp_path, "2026-11-02", build_of(1000, trail_count=5)) == 0
