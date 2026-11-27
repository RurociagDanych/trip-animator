import hashlib
import json
from pathlib import Path

import pytest
from trip_marked_trails import cli
from trip_marked_trails.cli import main
from trip_marked_trails.pipeline import BuildArtifact
from trip_object_store import LocalObjectStore

BUILD_PAYLOAD = b"PMTiles-fake-build-payload"


class RecordingStore(LocalObjectStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.cache_controls: dict[str, str | None] = {}

    def put_file(
        self,
        key: str,
        source: Path,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        self.cache_controls[key] = cache_control
        super().put_file(key, source, content_type=content_type, cache_control=cache_control)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        cache_control: str | None = None,
    ) -> None:
        self.cache_controls[key] = cache_control
        super().put_bytes(key, data, content_type=content_type, cache_control=cache_control)


def fake_build(region: str, work_dir: Path) -> BuildArtifact:
    work_dir.mkdir(parents=True, exist_ok=True)
    pmtiles = work_dir / "fake.pmtiles"
    pmtiles.write_bytes(BUILD_PAYLOAD)
    return BuildArtifact(pmtiles_path=pmtiles, osm_data_date="2026-11-26T20:21:03Z")


def publish(tmp_path: Path, build_date: str, build_step=fake_build) -> int:
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
        ],
        build_step=build_step,
    )


def read_pointer(store_dir: Path) -> dict[str, object]:
    return json.loads((store_dir / "current.json").read_text(encoding="utf-8"))


def test_publish_writes_build_prefix_and_pointer_with_layout(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-27") == 0

    store_dir = tmp_path / "store"
    assert (store_dir / "builds/2026-11-27/marked-trails.pmtiles").read_bytes() == BUILD_PAYLOAD
    pointer = read_pointer(store_dir)
    assert pointer["schema_version"] == 1
    assert pointer["region"] == "europe/andorra"
    assert pointer["current_build_id"] == "2026-11-27"
    assert pointer["previous_build_id"] is None
    assert pointer["pmtiles_key"] == "builds/2026-11-27/marked-trails.pmtiles"
    assert pointer["size_bytes"] == len(BUILD_PAYLOAD)
    assert pointer["sha256"] == hashlib.sha256(BUILD_PAYLOAD).hexdigest()
    assert pointer["osm_data_date"] == "2026-11-26T20:21:03Z"


def test_second_publish_on_same_day_keeps_previous_build_and_pointer(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-27") == 0
    assert publish(tmp_path, "2026-11-27") == 0

    store_dir = tmp_path / "store"
    pointer = read_pointer(store_dir)
    assert pointer["current_build_id"] == "2026-11-27-2"
    assert pointer["previous_build_id"] == "2026-11-27"
    assert (store_dir / "builds/2026-11-27/marked-trails.pmtiles").is_file()
    assert (store_dir / "builds/2026-11-27-2/marked-trails.pmtiles").is_file()


def test_failed_build_leaves_pointer_and_published_builds_unchanged(tmp_path: Path) -> None:
    assert publish(tmp_path, "2026-11-27") == 0
    pointer_before = (tmp_path / "store" / "current.json").read_bytes()

    def failing_build(region: str, work_dir: Path) -> BuildArtifact:
        raise RuntimeError("planetiler exited with status 1")

    assert publish(tmp_path, "2026-11-28", build_step=failing_build) == 1

    assert (tmp_path / "store" / "current.json").read_bytes() == pointer_before
    assert not (tmp_path / "store/builds/2026-11-28").exists()


def test_build_objects_are_immutable_and_pointer_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = RecordingStore(tmp_path / "store")
    monkeypatch.setattr(cli, "build_object_store", lambda config: recording)

    assert publish(tmp_path, "2026-11-27") == 0

    build_cache = recording.cache_controls["builds/2026-11-27/marked-trails.pmtiles"]
    assert build_cache is not None
    assert "immutable" in build_cache
    assert "max-age=31536000" in build_cache
    pointer_cache = recording.cache_controls["current.json"]
    assert pointer_cache is not None
    assert "no-cache" in pointer_cache
