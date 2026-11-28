from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from trip_telemetry import (
    MARKED_TRAILS_PIN_KEY,
    MarkedTrailsPin,
    RenderManifest,
    StageRecord,
    build_render_bundle,
    build_silver_dataframe,
    parse_gpx_bytes,
    write_silver_parquet,
)

_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>T</name><trkseg>
    <trkpt lat="0.1000" lon="0.2000"><ele>10</ele><time>2000-01-01T08:00:00Z</time></trkpt>
    <trkpt lat="0.1050" lon="0.2050"><ele>20</ele><time>2000-01-01T08:05:00Z</time></trkpt>
    <trkpt lat="0.1100" lon="0.2100"><ele>30</ele><time>2000-01-01T08:10:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""

_PIN = {
    "build_id": "2026-11-27",
    "pmtiles_key": "builds/2026-11-27/marked-trails.pmtiles",
    "osm_data_date": "2026-11-20T00:00:00Z",
    "base_url": "https://trails.example.test",
}


class _Store:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def get_bytes(self, key: str) -> bytes:
        return self._blobs[key]


@pytest.fixture
def stage_and_store(tmp_path: Path) -> tuple[StageRecord, _Store]:
    project_id, stage_id = uuid.uuid4(), uuid.uuid4()
    df, summary = build_silver_dataframe(
        parse_gpx_bytes(_GPX), trip_id=str(project_id), stage_id=str(stage_id)
    )
    parquet = write_silver_parquet(df, tmp_path / "s.parquet")
    key = f"silver/{project_id}/{stage_id}.parquet"
    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Day 1",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=key,
        distance_m=summary.distance_m,
        elevation_gain_m=summary.elevation_gain_m,
        moving_seconds=summary.moving_seconds,
        elapsed_seconds=summary.elapsed_seconds,
        has_timestamps=True,
    )
    return stage, _Store({key: parquet.read_bytes()})


def _bundle(stage: StageRecord, store: _Store, spec_extra: dict[str, object]) -> RenderManifest:
    return build_render_bundle(
        job_id=uuid.uuid4(),
        project_id=stage.project_id,
        project_title="Trails",
        spec_json={"resolution": "1280x720", "duration_seconds": 5.0, **spec_extra},
        stages=[stage],
        store=store,
    ).manifest


def test_manifest_carries_the_pin_and_survives_a_json_round_trip(
    stage_and_store: tuple[StageRecord, _Store],
) -> None:
    stage, store = stage_and_store
    manifest = _bundle(
        stage, store, {"marked_trail_types": ["hiking"], MARKED_TRAILS_PIN_KEY: _PIN}
    )

    assert manifest.marked_trails == MarkedTrailsPin(**_PIN)
    again = RenderManifest.model_validate_json(manifest.model_dump_json())
    assert again.marked_trails == MarkedTrailsPin(**_PIN)


def test_manifest_without_a_pin_has_no_trail_data(
    stage_and_store: tuple[StageRecord, _Store],
) -> None:
    stage, store = stage_and_store
    assert _bundle(stage, store, {"marked_trail_types": ["hiking"]}).marked_trails is None


def test_manifest_ignores_a_pin_when_no_trail_types_are_enabled(
    stage_and_store: tuple[StageRecord, _Store],
) -> None:
    stage, store = stage_and_store
    assert _bundle(stage, store, {MARKED_TRAILS_PIN_KEY: _PIN}).marked_trails is None


def test_manifest_from_an_older_worker_without_the_field_still_loads(
    stage_and_store: tuple[StageRecord, _Store],
) -> None:
    stage, store = stage_and_store
    raw = _bundle(stage, store, {}).model_dump_json()
    assert RenderManifest.model_validate_json(raw).marked_trails is None


def test_malformed_pin_is_dropped_instead_of_failing_the_render(
    stage_and_store: tuple[StageRecord, _Store],
) -> None:
    stage, store = stage_and_store
    manifest = _bundle(
        stage,
        store,
        {"marked_trail_types": ["hiking"], MARKED_TRAILS_PIN_KEY: {"build_id": 5}},
    )
    assert manifest.marked_trails is None


@pytest.mark.parametrize(
    "override",
    [
        {
            "build_id": "2026-11-27/../x",
            "pmtiles_key": "builds/2026-11-27/../x/marked-trails.pmtiles",
        },
        {"build_id": "2026-11-27-1", "pmtiles_key": "builds/2026-11-27-1/marked-trails.pmtiles"},
        {"pmtiles_key": "builds/2026-11-26/marked-trails.pmtiles"},
        {"osm_data_date": "not a date"},
        {"osm_data_date": "2026-11-20T00:00:00Z" + "0" * 40},
        {"base_url": "ftp://trails.example.test"},
        {"base_url": "//evil.example.test/x"},
        {"base_url": "https://"},
    ],
)
def test_pin_model_rejects_unsafe_values(override: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        MarkedTrailsPin(**{**_PIN, **override})


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:5173/tiles", "https://trails.example.test/pointer", "/tiles/marked-trails"],
)
def test_pin_model_accepts_http_https_and_root_relative_bases(base_url: str) -> None:
    assert MarkedTrailsPin(**{**_PIN, "base_url": base_url}).base_url == base_url


def test_pin_model_accepts_a_same_day_build_suffix_and_a_missing_date() -> None:
    pin = MarkedTrailsPin(
        build_id="2026-11-27-2",
        pmtiles_key="builds/2026-11-27-2/marked-trails.pmtiles",
        osm_data_date=None,
        base_url="https://trails.example.test",
    )
    assert pin.osm_data_date is None
