# ruff: noqa: S603, S607 - the tests drive the Docker CLI with fixed argument lists, no shell
import gzip
import json
import os
import shutil
import struct
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_NAME = "andorra-marked-trails.osm.pbf"
# Counted independently with osmium on the fixture: 22 relations match the route filter and
# two of them are `type=superroute`, which the profile does not read.
FIXTURE_ROUTE_RELATIONS = 20
FIXTURE_OSM_DATA_DATE = "2026-11-26T21:21:01Z"
BUILDER_IMAGE = os.environ.get(
    "MARKED_TRAILS_BUILDER_IMAGE", "trip-animator-marked-trails-builder:local"
)

pytestmark = pytest.mark.docker


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return False
    return True


@pytest.fixture(scope="module")
def builder_image() -> str:
    if not _docker_available():
        pytest.skip("Docker is not available")
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


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _protobuf_fields(data: bytes) -> list[tuple[int, int | bytes]]:
    fields: list[tuple[int, int | bytes]] = []
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, pos = _read_varint(data, pos)
            fields.append((number, value))
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            fields.append((number, data[pos : pos + length]))
            pos += length
        elif wire_type in (1, 5):
            size = 8 if wire_type == 1 else 4
            fields.append((number, data[pos : pos + size]))
            pos += size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
    return fields


def _mvt_feature_properties(tile: bytes) -> list[dict[str, str]]:
    features: list[dict[str, str]] = []
    for number, layer in _protobuf_fields(tile):
        if number != 3 or not isinstance(layer, bytes):
            continue
        keys: list[str] = []
        values: list[str] = []
        raw_features: list[bytes] = []
        for field, value in _protobuf_fields(layer):
            if field == 3 and isinstance(value, bytes):
                keys.append(value.decode())
            elif field == 4 and isinstance(value, bytes):
                strings = [v for f, v in _protobuf_fields(value) if f == 1 and isinstance(v, bytes)]
                values.append(strings[0].decode() if strings else "")
            elif field == 2 and isinstance(value, bytes):
                raw_features.append(value)
        for raw in raw_features:
            tags: list[int] = []
            for field, value in _protobuf_fields(raw):
                if field == 2 and isinstance(value, bytes):
                    pos = 0
                    while pos < len(value):
                        tag, pos = _read_varint(value, pos)
                        tags.append(tag)
            features.append(
                {keys[k]: values[v] for k, v in zip(tags[::2], tags[1::2], strict=True)}
            )
    return features


def _tile_zoom(tile_id: int) -> int:
    zoom = 0
    while (4 ** (zoom + 1) - 1) // 3 <= tile_id:
        zoom += 1
    return zoom


def _pmtiles_features_by_zoom(pmtiles: bytes) -> dict[int, list[dict[str, str]]]:
    root_offset, root_length, _, _, leaf_offset, _, data_offset = struct.unpack_from(
        "<7Q", pmtiles, 8
    )
    assert pmtiles[97] == 2, "directories must be gzip-compressed"
    assert pmtiles[98] == 2, "tiles must be gzip-compressed"
    by_zoom: dict[int, list[dict[str, str]]] = defaultdict(list)

    def walk(offset: int, length: int) -> None:
        directory = gzip.decompress(pmtiles[offset : offset + length])
        count, pos = _read_varint(directory, 0)
        columns: list[list[int]] = []
        for _ in range(4):
            column = []
            for _ in range(count):
                value, pos = _read_varint(directory, pos)
                column.append(value)
            columns.append(column)
        deltas, run_lengths, lengths, raw_offsets = columns
        tile_id = 0
        entry_offset = 0
        for i in range(count):
            tile_id += deltas[i]
            if raw_offsets[i] == 0 and i > 0:
                entry_offset += lengths[i - 1]
            else:
                entry_offset = raw_offsets[i] - 1
            if run_lengths[i] == 0:
                walk(leaf_offset + entry_offset, lengths[i])
                continue
            start = data_offset + entry_offset
            tile = gzip.decompress(pmtiles[start : start + lengths[i]])
            by_zoom[_tile_zoom(tile_id)].extend(_mvt_feature_properties(tile))

    walk(root_offset, root_length)
    return by_zoom


def test_builder_image_publishes_fixture_pmtiles_and_pointer(
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
            "--network",
            "none",
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
            "-v",
            f"{FIXTURES_DIR}:/fixtures:ro",
            builder_image,
            "build-publish",
            "--region",
            "europe/andorra",
            "--extract",
            f"/fixtures/{FIXTURE_NAME}",
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
    assert pointer["osm_data_date"] == FIXTURE_OSM_DATA_DATE
    assert pointer["marked_trail_count"] == FIXTURE_ROUTE_RELATIONS
    assert not any(work.rglob("*.pmtiles")), "the work copy must be deleted after publishing"
    assert (FIXTURES_DIR / FIXTURE_NAME).is_file()

    pmtiles = (store / pointer["pmtiles_key"]).read_bytes()
    assert len(pmtiles) == pointer["size_bytes"]
    assert pmtiles[:7] == b"PMTiles"
    assert pmtiles[7] == 3
    assert pmtiles[99] == 1, "tile type must be MVT"
    assert (pmtiles[100], pmtiles[101]) == (5, 13)

    by_zoom = _pmtiles_features_by_zoom(pmtiles)
    assert (min(by_zoom), max(by_zoom)) == (5, 13)
    for zoom in range(5, 9):
        networks = {feature.get("network") for feature in by_zoom[zoom]}
        assert networks, f"z{zoom} must carry the international and national trails"
        assert networks <= {"international", "national", "regional"}, (zoom, networks)
    networks_from_z9 = {feature.get("network") for feature in by_zoom[9]}
    assert "local" in networks_from_z9
    assert None in networks_from_z9, "a relation without a network tag must appear from z9"
    assert all("name" not in f and "ref" not in f for z in range(5, 13) for f in by_zoom[z])
    assert any("name" in feature for feature in by_zoom[13])
    assert {feature["type"] for feature in by_zoom[13]} == {"hiking", "cycling", "mtb"}
