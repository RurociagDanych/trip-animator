import json
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from trip_marked_trails import regional
from trip_marked_trails.cli import main
from trip_marked_trails.pipeline import (
    BuildArtifact,
    BuildToolError,
    BuildTools,
    ResourceLimits,
    SubprocessToolRunner,
    limited_command,
)
from trip_marked_trails.regional import list_child_regions, run_regional_build

GIB = 1024**3
REAL_EXTRA_CHILDREN = regional.EXTRA_CHILDREN


def feature(
    region_id: str,
    parent: str | None,
    url_path: str | None,
    special_regions: list[str] | None = None,
) -> dict[str, object]:
    properties: dict[str, object] = {"id": region_id, "name": region_id}
    if parent is not None:
        properties["parent"] = parent
    if special_regions is not None:
        properties["special_regions"] = special_regions
    urls: dict[str, str] = {}
    if url_path is not None:
        urls["pbf"] = f"https://download.geofabrik.de/{url_path}-latest.osm.pbf"
    properties["urls"] = urls
    return {"type": "Feature", "properties": properties}


INDEX = {
    "type": "FeatureCollection",
    "features": [
        feature(
            "europe",
            None,
            "europe/europe",
            special_regions=["britain-and-ireland", "great-britain", "alps", "dach"],
        ),
        feature("poland", "europe", "europe/poland/poland"),
        feature("andorra", "europe", "europe/andorra/andorra"),
        feature("germany", "europe", "europe/germany/germany"),
        feature("bayern", "germany", "europe/germany/bayern/bayern"),
        feature("dach", "europe", "europe/dach/dach"),
        feature("alps", "europe", "europe/alps/alps"),
        feature("britain-and-ireland", "europe", "europe/britain-and-ireland/britain-and-ireland"),
        feature("great-britain", "europe", "europe/great-britain/great-britain"),
        feature("no-extract", "europe", None),
        feature("russia", None, "russia/russia", special_regions=["kaliningrad"]),
        feature(
            "central-fed-district", "russia", "russia/central-fed-district/central-fed-district"
        ),
        feature(
            "northwestern-fed-district",
            "russia",
            "russia/northwestern-fed-district/northwestern-fed-district",
        ),
        feature("south-fed-district", "russia", "russia/south-fed-district/south-fed-district"),
        feature(
            "north-caucasus-fed-district",
            "russia",
            "russia/north-caucasus-fed-district/north-caucasus-fed-district",
        ),
        feature("volga-fed-district", "russia", "russia/volga-fed-district/volga-fed-district"),
        feature("ural-fed-district", "russia", "russia/ural-fed-district/ural-fed-district"),
    ],
}


@pytest.fixture(autouse=True)
def no_extra_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regional, "EXTRA_CHILDREN", {})


def index_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index-v1.json":
            return httpx.Response(200, json=INDEX)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_children_of_a_parent_region_come_from_the_geofabrik_index() -> None:
    with index_client() as client:
        children = list_child_regions("europe", client=client)

    assert children == ["europe/andorra", "europe/germany", "europe/poland"]


def test_children_skip_special_regions_that_overlap_countries() -> None:
    with index_client() as client:
        children = list_child_regions("europe", client=client)

    assert not {"europe/dach", "europe/alps", "europe/great-britain"} & set(children)
    assert "europe/britain-and-ireland" not in children


def test_nested_parent_lists_its_own_children() -> None:
    with index_client() as client:
        assert list_child_regions("europe/germany", client=client) == ["europe/germany/bayern"]


def test_special_regions_come_from_the_index_properties_of_the_parent() -> None:
    index = {
        "features": [
            feature("europe", None, "europe/europe", special_regions=["alps"]),
            feature("alps", "europe", "europe/alps/alps"),
            feature("dach", "europe", "europe/dach/dach"),
            feature("poland", "europe", "europe/poland/poland"),
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=index))

    with httpx.Client(transport=transport) as client:
        children = list_child_regions("europe", client=client)

    assert children == ["europe/dach", "europe/poland"]


def test_parent_without_special_regions_keeps_all_children() -> None:
    index = {
        "features": [
            feature("europe", None, "europe/europe"),
            feature("dach", "europe", "europe/dach/dach"),
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=index))

    with httpx.Client(transport=transport) as client:
        assert list_child_regions("europe", client=client) == ["europe/dach"]


def test_europe_gets_the_european_russia_federal_districts_as_extra_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(regional, "EXTRA_CHILDREN", REAL_EXTRA_CHILDREN)

    with index_client() as client:
        children = list_child_regions("europe", client=client)

    assert children == [
        "europe/andorra",
        "europe/germany",
        "europe/poland",
        "russia/central-fed-district",
        "russia/north-caucasus-fed-district",
        "russia/northwestern-fed-district",
        "russia/south-fed-district",
        "russia/volga-fed-district",
    ]


def test_extra_children_are_a_fixed_list_for_europe_only() -> None:
    assert REAL_EXTRA_CHILDREN == {
        "europe": (
            "russia/central-fed-district",
            "russia/northwestern-fed-district",
            "russia/south-fed-district",
            "russia/north-caucasus-fed-district",
            "russia/volga-fed-district",
        )
    }


def test_extra_child_missing_from_the_index_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(regional, "EXTRA_CHILDREN", {"europe": ("russia/atlantis-fed-district",)})

    with index_client() as client, pytest.raises(BuildToolError, match="atlantis"):
        list_child_regions("europe", client=client)


def test_extra_child_already_listed_is_not_duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(regional, "EXTRA_CHILDREN", {"europe": ("europe/poland",)})

    with index_client() as client:
        children = list_child_regions("europe", client=client)

    assert children == ["europe/andorra", "europe/germany", "europe/poland"]


def test_leaf_region_has_no_children() -> None:
    with index_client() as client:
        assert list_child_regions("europe/andorra", client=client) == []


def test_unknown_region_is_rejected() -> None:
    with index_client() as client, pytest.raises(BuildToolError, match="not in the Geofabrik"):
        list_child_regions("europe/atlantis", client=client)


class Recorder:
    """Fake downloader and tool runner that record events and write plausible outputs."""

    def __init__(
        self,
        work_dir: Path,
        *,
        fail_filter_for: str | None = None,
        fail_download_for: str | None = None,
        fail_planetiler: bool = False,
    ) -> None:
        self.work_dir = work_dir
        self.events: list[tuple[str, ...]] = []
        self.fail_filter_for = fail_filter_for
        self.fail_download_for = fail_download_for
        self.fail_planetiler = fail_planetiler
        self.extracts: list[Path] = []
        self.dates = {
            "europe/andorra": "2026-11-26T21:21:01Z",
            "europe/germany": "2026-11-25T20:00:00Z",
            "europe/poland": "2026-11-27T01:00:00Z",
        }
        self.download_counts: dict[str, int] = {}
        self._by_extract: dict[str, str] = {}

    def download(self, region: str, dest: Path) -> None:
        for earlier in self.extracts:
            assert not earlier.exists(), f"{earlier.name} still on disk when {region} starts"
        if region == self.fail_download_for:
            raise BuildToolError(f"md5 mismatch for {region}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(region.encode())
        self.extracts.append(dest)
        self._by_extract[str(dest)] = region
        count = self.download_counts.get(region, 0)
        self.download_counts[region] = count + 1
        self.events.append(("download", region))

    def capture(self, args: Sequence[str]) -> str:
        assert args[1] == "fileinfo"
        extract = args[-1]
        assert args[3] == "header.option.osmosis_replication_timestamp"
        return self.dates[self._by_extract[extract]] + "\n"

    def run(self, args: Sequence[str]) -> None:
        command = args[1] if args[0] == "osmium" else "planetiler"
        if command == "tags-filter":
            output, source = Path(args[args.index("-o") + 1]), Path(args[-2])
            region = self._by_extract[str(source)]
            assert source.is_file()
            if region == self.fail_filter_for:
                raise BuildToolError(f"osmium exited with status 1 on {region}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"filtered-" + region.encode())
            self.events.append(("filter", region))
        elif command == "merge":
            output = Path(args[args.index("-o") + 1])
            inputs = [Path(a) for a in args[args.index("-o") + 2 :] if not a.startswith("--")]
            assert inputs and all(path.is_file() for path in inputs)
            assert not any(path.exists() for path in self.extracts)
            output.write_bytes(b"".join(path.read_bytes() for path in inputs))
            assert "--with-history" in args
            assert output.name.endswith(".osh.pbf")
            self.events.append(("merge", *(path.read_bytes().decode() for path in inputs)))
        elif command == "time-filter":
            output, history = Path(args[args.index("-o") + 1]), Path(args[-1])
            assert history.is_file() and history.name.endswith(".osh.pbf")
            output.write_bytes(history.read_bytes())
            self.events.append(("time-filter",))
        else:
            options = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
            merged = Path(options["osm_path"])
            assert merged.is_file()
            if self.fail_planetiler:
                raise BuildToolError("java exited with status 1")
            Path(options["output"]).write_bytes(b"PMTiles-fake")
            Path(options["marked_trail_count_path"]).write_text("9\n")
            self.events.append(("planetiler", merged.read_bytes().decode()))


def regional_build(tmp_path: Path, recorder: Recorder, region: str = "europe") -> BuildArtifact:
    with index_client() as client:
        return run_regional_build(
            region,
            tmp_path / "work",
            BuildTools(),
            runner=recorder,
            downloader=recorder.download,
            index_client=client,
        )


def test_each_country_is_downloaded_filtered_and_deleted_before_the_next(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work")

    regional_build(tmp_path, recorder)

    assert recorder.events[:6] == [
        ("download", "europe/andorra"),
        ("filter", "europe/andorra"),
        ("download", "europe/germany"),
        ("filter", "europe/germany"),
        ("download", "europe/poland"),
        ("filter", "europe/poland"),
    ]
    assert not any(path.exists() for path in recorder.extracts)


def test_filtered_files_are_merged_then_built_in_one_planetiler_run(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work")

    artifact = regional_build(tmp_path, recorder)

    merged = (
        "filtered-europe/andorra",
        "filtered-europe/germany",
        "filtered-europe/poland",
    )
    assert recorder.events[6:] == [
        ("merge", *merged),
        ("time-filter",),
        ("planetiler", "".join(merged)),
    ]
    assert artifact.pmtiles_path.read_bytes() == b"PMTiles-fake"
    assert artifact.marked_trail_count == 9


def test_osm_data_date_is_the_oldest_extract_date(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work")

    artifact = regional_build(tmp_path, recorder)

    assert artifact.osm_data_date == "2026-11-25T20:00:00Z"


def test_only_the_final_build_file_is_left_in_the_work_directory(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work")

    artifact = regional_build(tmp_path, recorder)

    leftovers = sorted(p.name for p in (tmp_path / "work").iterdir())
    assert leftovers == [artifact.pmtiles_path.name]


def test_extracts_with_different_replication_sequence_numbers_are_built_without_a_second_download(
    tmp_path: Path,
) -> None:
    recorder = Recorder(tmp_path / "work")

    regional_build(tmp_path, recorder)

    assert recorder.download_counts == {
        "europe/andorra": 1,
        "europe/germany": 1,
        "europe/poland": 1,
    }


def test_a_date_spread_over_two_days_only_logs_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = Recorder(tmp_path / "work")
    recorder.dates["europe/poland"] = "2026-12-01T00:00:00Z"

    with caplog.at_level("INFO", logger="trip_marked_trails.regional"):
        artifact = regional_build(tmp_path, recorder)

    assert artifact.osm_data_date == "2026-11-25T20:00:00Z"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "2026-11-25T20:00:00Z" in caplog.text
    assert "2026-12-01T00:00:00Z" in caplog.text


def test_a_date_spread_within_two_days_logs_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = Recorder(tmp_path / "work")

    with caplog.at_level("INFO", logger="trip_marked_trails.regional"):
        regional_build(tmp_path, recorder)

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert "spread" in caplog.text


def test_a_child_without_a_date_does_not_fail_the_run(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work")
    recorder.dates["europe/germany"] = ""

    artifact = regional_build(tmp_path, recorder)

    assert artifact.osm_data_date == "2026-11-26T21:21:01Z"


def test_stale_files_of_a_killed_leaf_run_are_removed_at_the_start(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    stale = [
        work / "extract.osm.pbf",
        work / "marked-trails-filtered.osm.pbf",
        work / "tmp" / "chunk",
    ]
    for path in stale:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"x")

    class Failing(Recorder):
        def download(self, region: str, dest: Path) -> None:
            assert not any(path.exists() for path in stale)
            raise BuildToolError("stop")

    with pytest.raises(BuildToolError, match="stop"):
        regional_build(tmp_path, Failing(work), region="europe/andorra")

    assert list(work.iterdir()) == []


def test_failing_leaf_filter_leaves_no_files_in_the_work_directory(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work", fail_filter_for="europe/andorra")

    with pytest.raises(BuildToolError, match="europe/andorra"):
        regional_build(tmp_path, recorder, region="europe/andorra")

    assert list((tmp_path / "work").iterdir()) == []


def test_failing_leaf_planetiler_leaves_no_files_in_the_work_directory(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work", fail_planetiler=True)

    with pytest.raises(BuildToolError, match="java"):
        regional_build(tmp_path, recorder, region="europe/andorra")

    assert list((tmp_path / "work").iterdir()) == []


def test_leaf_region_skips_the_per_country_loop(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work")

    regional_build(tmp_path, recorder, region="europe/andorra")

    assert recorder.events == [
        ("download", "europe/andorra"),
        ("filter", "europe/andorra"),
        ("planetiler", "filtered-europe/andorra"),
    ]


def test_failing_country_stops_the_build_and_cleans_the_work_directory(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work", fail_filter_for="europe/germany")

    with pytest.raises(BuildToolError, match="europe/germany"):
        regional_build(tmp_path, recorder)

    assert [event[0] for event in recorder.events] == ["download", "filter", "download"]
    assert list((tmp_path / "work").iterdir()) == []


def test_failing_download_stops_the_build(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work", fail_download_for="europe/poland")

    with pytest.raises(BuildToolError, match="europe/poland"):
        regional_build(tmp_path, recorder)

    assert ("merge",) not in recorder.events
    assert all(event[0] != "planetiler" for event in recorder.events)
    assert list((tmp_path / "work").iterdir()) == []


def test_failing_planetiler_leaves_no_scratch_files(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work", fail_planetiler=True)

    with pytest.raises(BuildToolError, match="java"):
        regional_build(tmp_path, recorder)

    assert list((tmp_path / "work").iterdir()) == []


def test_scratch_files_of_an_earlier_crashed_run_are_removed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    stale = work / "regional" / "extracts" / "old.osm.pbf"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x" * 10)

    regional_build(tmp_path, Recorder(work))

    assert not stale.exists()


def cli_run(tmp_path: Path, recorder: Recorder) -> int:
    def build_step(region: str, work_dir: Path) -> BuildArtifact:
        with index_client() as client:
            return run_regional_build(
                region,
                work_dir,
                BuildTools(),
                runner=recorder,
                downloader=recorder.download,
                index_client=client,
            )

    return main(
        [
            "build-publish",
            "--region",
            "europe",
            "--store-dir",
            str(tmp_path / "store"),
            "--work-dir",
            str(tmp_path / "work"),
            "--build-date",
            "2026-11-28",
        ],
        build_step=build_step,
    )


def test_successful_regional_build_publishes_the_pointer(tmp_path: Path) -> None:
    assert cli_run(tmp_path, Recorder(tmp_path / "work")) == 0

    pointer = json.loads((tmp_path / "store" / "current.json").read_text())
    assert pointer["current_build_id"] == "2026-11-28"
    assert pointer["marked_trail_count"] == 9
    assert pointer["osm_data_date"] == "2026-11-25T20:00:00Z"


def test_failing_country_fails_the_run_and_publishes_nothing(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "work", fail_filter_for="europe/poland")

    assert cli_run(tmp_path, recorder) == 1

    store = tmp_path / "store"
    assert not store.exists() or list(store.rglob("*")) == []


def test_limited_command_wraps_the_tool_in_nice_prlimit_taskset_and_setpriv() -> None:
    limits = ResourceLimits(memory_bytes=3 * GIB, nice=10, cpu=2)

    assert limited_command(["osmium", "fileinfo", "x.pbf"], limits) == [
        "nice",
        "-n",
        "10",
        "prlimit",
        f"--data={3 * GIB}",
        "taskset",
        "-c",
        "2",
        "setpriv",
        "--pdeathsig",
        "KILL",
        "osmium",
        "fileinfo",
        "x.pbf",
    ]


def test_command_without_limits_is_unchanged() -> None:
    assert limited_command(["osmium", "x"], None) == ["osmium", "x"]


needs_limit_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("nice", "prlimit", "taskset", "setpriv")),
    reason="needs nice, prlimit, taskset and setpriv from util-linux",
)


@needs_limit_tools
def test_tool_cannot_allocate_more_than_the_memory_cap(tmp_path: Path) -> None:
    runner = SubprocessToolRunner(
        BuildTools(limits=ResourceLimits(memory_bytes=512 * 1024**2, nice=10, cpu=None))
    )
    script = (
        "import sys\ntry:\n    bytearray(1024**3)\nexcept MemoryError:\n    sys.exit('MemoryError')"
    )

    with pytest.raises(BuildToolError, match="MemoryError"):
        runner.run([sys.executable, "-c", script])


@needs_limit_tools
def test_tool_under_the_cap_runs_and_its_output_is_captured() -> None:
    runner = SubprocessToolRunner(
        BuildTools(limits=ResourceLimits(memory_bytes=1 * GIB, nice=10, cpu=None))
    )

    assert runner.capture([sys.executable, "-c", "print('ok')"]).strip() == "ok"


PARENT_SCRIPT = """
import os, subprocess, sys, time
from pathlib import Path
from trip_marked_trails.pipeline import ResourceLimits, limited_command

pid_file = Path(sys.argv[1])
tool = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
command = limited_command(
    [sys.executable, "-c", tool, str(pid_file)], ResourceLimits(memory_bytes=2 * 1024**3)
)
subprocess.Popen(command)
while not pid_file.exists() or not pid_file.read_text():
    time.sleep(0.05)
os._exit(0)
"""


@needs_limit_tools
def test_tool_is_killed_when_the_process_that_started_it_dies(tmp_path: Path) -> None:
    pid_file = tmp_path / "tool.pid"
    subprocess.run([sys.executable, "-c", PARENT_SCRIPT, str(pid_file)], check=True, timeout=60)  # noqa: S603

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _is_running(pid):
        time.sleep(0.1)
    assert not _is_running(pid), "the tool outlived the process that started it"


def _is_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"
