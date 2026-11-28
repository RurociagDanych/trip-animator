import os
from pathlib import Path

import pytest
from trip_marked_trails import cli
from trip_marked_trails.pipeline import BuildArtifact, BuildTools

GIB = 1024**3


class Calls:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.regional: list[tuple[str, BuildTools]] = []
        self.single: list[tuple[str, Path | None, BuildTools]] = []

    def artifact(self, work_dir: Path) -> BuildArtifact:
        work_dir.mkdir(parents=True, exist_ok=True)
        pmtiles = work_dir / "marked-trails.pmtiles"
        pmtiles.write_bytes(b"PMTiles-fake")
        return BuildArtifact(pmtiles_path=pmtiles, osm_data_date=None, marked_trail_count=2)

    def run_regional_build(self, region: str, work_dir: Path, tools: BuildTools) -> BuildArtifact:
        self.regional.append((region, tools))
        return self.artifact(work_dir)

    def run_build(
        self, region: str, work_dir: Path, tools: BuildTools, *, extract_path: Path | None
    ) -> BuildArtifact:
        self.single.append((region, extract_path, tools))
        return self.artifact(work_dir)


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Calls:
    recorded = Calls(tmp_path)
    monkeypatch.setattr(cli, "run_regional_build", recorded.run_regional_build)
    monkeypatch.setattr(cli, "run_build", recorded.run_build)
    return recorded


def run_cli(tmp_path: Path, *extra: str) -> int:
    return cli.main(
        [
            "build-publish",
            "--region",
            "europe",
            "--store-dir",
            str(tmp_path / "store"),
            "--work-dir",
            str(tmp_path / "work"),
            *extra,
        ]
    )


def test_region_without_extract_is_built_country_by_country(tmp_path: Path, calls: Calls) -> None:
    assert run_cli(tmp_path) == 0

    assert [region for region, _ in calls.regional] == ["europe"]
    assert calls.single == []
    assert calls.regional[0][1].limits is None


def test_cap_resources_limits_tools_to_three_gib_and_one_cpu(tmp_path: Path, calls: Calls) -> None:
    assert run_cli(tmp_path, "--cap-resources") == 0

    limits = calls.regional[0][1].limits
    assert limits is not None
    assert limits.memory_bytes == 3 * GIB
    assert limits.cpus == (min(os.sched_getaffinity(0)),)


def test_local_extract_skips_the_geofabrik_index(tmp_path: Path, calls: Calls) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(b"x")

    assert run_cli(tmp_path, "--extract", str(extract)) == 0

    assert calls.regional == []
    assert calls.single[0][1] == extract


def test_store_dir_defaults_to_the_environment(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_STORE", "local")
    monkeypatch.setenv("MARKED_TRAILS_STORE_DIR", str(tmp_path / "env-store"))

    code = cli.main(["build-publish", "--region", "europe", "--work-dir", str(tmp_path / "work")])

    assert code == 0
    assert (tmp_path / "env-store" / "current.json").is_file()


def test_tool_caps_default_to_the_vps_values(tmp_path: Path, calls: Calls) -> None:
    assert run_cli(tmp_path, "--cap-resources") == 0

    tools = calls.regional[0][1]
    assert tools.planetiler_heap_option == "-Xmx2g"
    assert tools.planetiler_direct_memory_option == "-XX:MaxDirectMemorySize=512m"
    assert tools.limits is not None
    assert tools.limits.cpus == (min(os.sched_getaffinity(0)),)


def test_tool_caps_come_from_the_environment(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKED_TRAILS_TOOL_MEMORY_BYTES", str(13 * GIB))
    monkeypatch.setenv("MARKED_TRAILS_TOOL_CPUS", "4")
    monkeypatch.setenv("MARKED_TRAILS_PLANETILER_HEAP", "-Xmx10g")
    monkeypatch.setenv("MARKED_TRAILS_PLANETILER_DIRECT_MEMORY", "-XX:MaxDirectMemorySize=1g")

    assert run_cli(tmp_path, "--cap-resources") == 0

    tools = calls.regional[0][1]
    assert tools.planetiler_heap_option == "-Xmx10g"
    assert tools.planetiler_direct_memory_option == "-XX:MaxDirectMemorySize=1g"
    assert tools.limits is not None
    assert tools.limits.memory_bytes == 13 * GIB
    assert tools.limits.cpus == tuple(sorted(os.sched_getaffinity(0))[:4])


def test_tool_cpus_are_ignored_without_cap_resources(tmp_path: Path, calls: Calls) -> None:
    assert run_cli(tmp_path, "--tool-cpus", "4") == 0

    assert calls.regional[0][1].limits is None


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--planetiler-heap", "10g"),
        ("--planetiler-heap", "-Xmx"),
        ("--planetiler-direct-memory", "512m"),
        ("--tool-memory-bytes", "0"),
        ("--tool-cpus", "-1"),
    ],
)
def test_malformed_tool_caps_are_rejected_before_the_build(
    tmp_path: Path, calls: Calls, flag: str, value: str
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_cli(tmp_path, flag, value)

    assert exit_info.value.code == 2
    assert calls.regional == []
