import hashlib
import logging
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from trip_marked_trails.pipeline import (
    BuildToolError,
    BuildTools,
    check_free_space,
    download_extract,
    run_build,
)

OSM_DATA_DATE = "2026-11-26T21:21:01Z"
EXTRACT_BYTES = b"fake-osm-pbf-extract"


def write_tool(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\nimport sys, time\nargs = sys.argv[1:]\n{body}")
    path.chmod(0o755)
    return str(path)


def fake_tools(tmp_path: Path, java_body: str, *, timeout: float = 60.0) -> BuildTools:
    calls = tmp_path / "calls.log"
    osmium = write_tool(
        tmp_path / "osmium",
        f"""
with open({str(calls)!r}, "a") as log:
    log.write(" ".join(args) + "\\n")
if args[0] == "fileinfo":
    print({OSM_DATA_DATE!r})
elif args[0] == "tags-filter":
    out = args[args.index("-o") + 1]
    with open(args[-2], "rb") as src, open(out, "wb") as dst:
        dst.write(src.read())
""",
    )
    java = write_tool(tmp_path / "java", java_body)
    return BuildTools(osmium=osmium, java=java, tool_timeout_seconds=timeout)


PLANETILER_OK = """
opts = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
print("planetiler: reading osm", flush=True)
print("planetiler: writing tiles", flush=True)
with open(opts["output"], "wb") as fh:
    fh.write(b"PMTiles-fake")
with open(opts["marked_trail_count_path"], "w") as fh:
    fh.write("7\\n")
"""


def test_local_extract_is_built_without_download_and_left_in_place(tmp_path: Path) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    tools = fake_tools(tmp_path, PLANETILER_OK)

    artifact = run_build("europe/andorra", tmp_path / "work", tools, extract_path=extract)

    assert artifact.pmtiles_path.read_bytes() == b"PMTiles-fake"
    assert artifact.osm_data_date == OSM_DATA_DATE
    assert artifact.marked_trail_count == 7
    assert extract.read_bytes() == EXTRACT_BYTES
    assert str(extract) in (tmp_path / "calls.log").read_text()


def test_tool_output_is_streamed_to_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    caplog.set_level(logging.INFO, logger="trip_marked_trails")

    run_build(
        "europe/andorra",
        tmp_path / "work",
        fake_tools(tmp_path, PLANETILER_OK),
        extract_path=extract,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert any("planetiler: reading osm" in message for message in messages)
    assert any("planetiler: writing tiles" in message for message in messages)


def test_failing_tool_reports_the_tail_of_its_output(tmp_path: Path) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    tools = fake_tools(tmp_path, 'print("planetiler: out of memory", flush=True)\nsys.exit(3)\n')

    with pytest.raises(BuildToolError, match="status 3.*out of memory"):
        run_build("europe/andorra", tmp_path / "work", tools, extract_path=extract)


def test_tool_that_exceeds_the_timeout_fails_the_build(tmp_path: Path) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    tools = fake_tools(tmp_path, "time.sleep(30)\n", timeout=1.0)

    with pytest.raises(BuildToolError, match="timed out"):
        run_build("europe/andorra", tmp_path / "work", tools, extract_path=extract)


def geofabrik_client(md5_line: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/europe/andorra-latest.osm.pbf":
            return httpx.Response(
                307,
                headers={"Location": "https://download.geofabrik.de/europe/andorra-261126.osm.pbf"},
            )
        if request.url.path == "/europe/andorra-261126.osm.pbf":
            return httpx.Response(200, content=EXTRACT_BYTES)
        if request.url.path == "/europe/andorra-261126.osm.pbf.md5":
            return httpx.Response(200, text=md5_line)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_download_keeps_an_extract_whose_md5_matches(tmp_path: Path) -> None:
    dest = tmp_path / "extract.osm.pbf"
    md5 = hashlib.md5(EXTRACT_BYTES, usedforsecurity=False).hexdigest()

    with geofabrik_client(f"{md5}  andorra-261126.osm.pbf\n") as client:
        download_extract("europe/andorra", dest, client=client)

    assert dest.read_bytes() == EXTRACT_BYTES


def test_download_rejects_an_extract_whose_md5_differs(tmp_path: Path) -> None:
    dest = tmp_path / "extract.osm.pbf"

    with (
        geofabrik_client(f"{'0' * 32}  andorra-261126.osm.pbf\n") as client,
        pytest.raises(BuildToolError, match="md5"),
    ):
        download_extract("europe/andorra", dest, client=client)

    assert list(tmp_path.iterdir()) == []


def test_planetiler_gets_no_nodemap_arguments_by_default(tmp_path: Path) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    seen = tmp_path / "java-args.txt"
    body = f"open({str(seen)!r}, 'w').write(' '.join(args))\n" + PLANETILER_OK

    run_build("europe/andorra", tmp_path / "work", fake_tools(tmp_path, body), extract_path=extract)

    assert "nodemap" not in seen.read_text()


def test_planetiler_gets_the_configured_nodemap_arguments(tmp_path: Path) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    seen = tmp_path / "java-args.txt"
    body = f"open({str(seen)!r}, 'w').write(' '.join(args))\n" + PLANETILER_OK
    tools = replace(
        fake_tools(tmp_path, body),
        planetiler_nodemap_storage="ram",
        planetiler_nodemap_type="sparsearray",
    )

    run_build("europe/andorra", tmp_path / "work", tools, extract_path=extract)

    assert "--nodemap-storage=ram --nodemap-type=sparsearray" in seen.read_text()


def test_each_stage_logs_free_space_and_work_dir_size(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extract = tmp_path / "andorra.osm.pbf"
    extract.write_bytes(EXTRACT_BYTES)
    caplog.set_level(logging.INFO, logger="trip_marked_trails")

    run_build(
        "europe/andorra",
        tmp_path / "work",
        fake_tools(tmp_path, PLANETILER_OK),
        extract_path=extract,
    )

    stages = [
        record.getMessage().split(":")[0]
        for record in caplog.records
        if record.getMessage().startswith("Disk after")
    ]
    assert stages == ["Disk after filter", "Disk after planetiler"]
    assert any("GiB free, work dir" in record.getMessage() for record in caplog.records)


def test_check_free_space_names_the_volume_and_both_sizes(tmp_path: Path) -> None:
    with pytest.raises(BuildToolError, match="GiB free; a Marked Trails build needs"):
        check_free_space(tmp_path, 10**18)
