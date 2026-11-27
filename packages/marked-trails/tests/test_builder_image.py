# ruff: noqa: S603, S607 - the tests drive the Docker CLI with fixed argument lists, no shell
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILDER_IMAGE = os.environ.get(
    "MARKED_TRAILS_BUILDER_IMAGE", "trip-animator-marked-trails-builder:local"
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return False
    return True


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="Docker is not available"),
]


@pytest.fixture(scope="module")
def builder_image() -> str:
    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(REPO_ROOT / "infra/docker/Dockerfile.marked-trails-builder"),
            "-t",
            BUILDER_IMAGE,
            str(REPO_ROOT),
        ],
        check=True,
        timeout=1800,
    )
    return BUILDER_IMAGE


def test_builder_image_publishes_andorra_pmtiles_and_pointer(
    tmp_path: Path, builder_image: str
) -> None:
    store = tmp_path / "store"
    work = tmp_path / "work"
    store.mkdir()
    work.mkdir()

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cpus",
            "1",
            "--memory",
            "3g",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{store}:/store",
            "-v",
            f"{work}:/work",
            builder_image,
            "build-publish",
            "--region",
            "europe/andorra",
            "--store-dir",
            "/store",
            "--work-dir",
            "/work",
            "--build-date",
            "2026-11-27",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]

    pointer = json.loads((store / "current.json").read_text(encoding="utf-8"))
    assert pointer["region"] == "europe/andorra"
    assert pointer["current_build_id"] == "2026-11-27"
    assert pointer["pmtiles_key"] == "builds/2026-11-27/marked-trails.pmtiles"

    pmtiles = (store / pointer["pmtiles_key"]).read_bytes()
    assert len(pmtiles) == pointer["size_bytes"]
    assert pmtiles[:7] == b"PMTiles"
    assert pmtiles[7] == 3
    tile_type = pmtiles[99]
    min_zoom, max_zoom = pmtiles[100], pmtiles[101]
    assert tile_type == 1, "tile type must be MVT"
    assert min_zoom == 5
    assert max_zoom == 13
    tile_entries = int.from_bytes(pmtiles[80:88], "little")
    assert tile_entries > 0
