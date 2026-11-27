import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
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


def feature(region_id: str, parent: str | None, url_path: str | None) -> dict[str, object]:
    properties: dict[str, object] = {"id": region_id, "name": region_id}
    if parent is not None:
        properties["parent"] = parent
    urls: dict[str, str] = {}
    if url_path is not None:
        urls["pbf"] = f"https://download.geofabrik.de/{url_path}-latest.osm.pbf"
    properties["urls"] = urls
    return {"type": "Feature", "properties": properties}


INDEX = {
    "type": "FeatureCollection",
    "features": [
        feature("europe", None, "europe/europe"),
        feature("poland", "europe", "europe/poland/poland"),
        feature("andorra", "europe", "europe/andorra/andorra"),
        feature("germany", "europe", "europe/germany/germany"),
        feature("bayern", "germany", "europe/germany/bayern/bayern"),
        feature("dach", "europe", "europe/dach/dach"),
        feature("alps", "europe", "europe/alps/alps"),
        feature("britain-and-ireland", "europe", "europe/britain-and-ireland/britain-and-ireland"),
        feature("great-britain", "europe", "europe/great-britain/great-britain"),
        feature("no-extract", "europe", None),
    ],
}


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


def test_children_skip_aggregate_extracts_that_overlap_countries() -> None:
    with index_client() as client:
        children = list_child_regions("europe", client=client)

    assert not {"europe/dach", "europe/alps", "europe/great-britain"} & set(children)
    assert "europe/britain-and-ireland" not in children


def test_nested_parent_lists_its_own_children() -> None:
    with index_client() as client:
        assert list_child_regions("europe/germany", client=client) == ["europe/germany/bayern"]


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
    ) -> None:
        self.work_dir = work_dir
        self.events: list[tuple[str, ...]] = []
        self.fail_filter_for = fail_filter_for
        self.fail_download_for = fail_download_for
        self.extracts: list[Path] = []
        self.dates = {
            "europe/andorra": "2026-11-26T21:21:01Z",
            "europe/germany": "2026-11-25T20:00:00Z",
            "europe/poland": "2026-11-27T01:00:00Z",
        }
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
        self.events.append(("download", region))

    def capture(self, args: Sequence[str]) -> str:
        assert args[1] == "fileinfo"
        return self.dates[self._by_extract[args[-1]]] + "\n"

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
            self.events.append(("merge", *(path.read_bytes().decode() for path in inputs)))
        else:
            options = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
            merged = Path(options["osm_path"])
            assert merged.is_file()
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


def test_limited_command_wraps_the_tool_in_nice_prlimit_and_taskset() -> None:
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
        "osmium",
        "fileinfo",
        "x.pbf",
    ]


def test_command_without_limits_is_unchanged() -> None:
    assert limited_command(["osmium", "x"], None) == ["osmium", "x"]


needs_limit_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("nice", "prlimit", "taskset")),
    reason="needs nice, prlimit and taskset from util-linux",
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
