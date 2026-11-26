from __future__ import annotations

import uuid
from pathlib import Path

from trip_telemetry import (
    PhotoRecord,
    RenderBundle,
    RenderManifest,
    StageRecord,
    build_render_bundle,
    build_silver_dataframe,
    parse_gpx_bytes,
    write_silver_parquet,
)


class _MemoryByteStore:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = dict(blobs)

    def get_bytes(self, key: str) -> bytes:
        if key not in self._blobs:
            raise FileNotFoundError(key)
        return self._blobs[key]


_SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>Test Track</name><trkseg>
    <trkpt lat="46.5000" lon="10.0000"><ele>1000</ele><time>2026-08-01T08:00:00Z</time></trkpt>
    <trkpt lat="46.5050" lon="10.0050"><ele>1050</ele><time>2026-08-01T08:05:00Z</time></trkpt>
    <trkpt lat="46.5100" lon="10.0100"><ele>1100</ele><time>2026-08-01T08:10:00Z</time></trkpt>
  </trkseg></trk>
</gpx>
"""


def test_build_render_bundle_creates_self_contained_manifest_and_photo_assets(
    tmp_path: Path,
) -> None:
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    job_id = uuid.uuid4()
    photo_id = uuid.uuid4()

    parsed = parse_gpx_bytes(_SAMPLE_GPX)
    df, summary = build_silver_dataframe(
        parsed,
        trip_id=str(project_id),
        stage_id=str(stage_id),
    )
    parquet_path = write_silver_parquet(df, tmp_path / f"{stage_id}.parquet")
    silver_key = f"silver/{project_id}/{stage_id}.parquet"
    photo_key = f"photos/{project_id}/{photo_id}.webp"
    thumb_key = f"photos/{project_id}/{photo_id}_thumb.webp"
    thumb_bytes = b"RIFF\x00\x00\x00\x00WEBPfake-thumb"

    store = _MemoryByteStore({silver_key: parquet_path.read_bytes(), thumb_key: thumb_bytes})

    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Day 1",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=silver_key,
        distance_m=summary.distance_m,
        elevation_gain_m=summary.elevation_gain_m,
        moving_seconds=summary.moving_seconds,
        elapsed_seconds=summary.elapsed_seconds,
        has_timestamps=True,
    )
    photo = PhotoRecord(
        id=photo_id,
        project_id=project_id,
        thumb_object_key=thumb_key,
        object_key=photo_key,
        width=1200,
        height=800,
        caption="Summit",
        matched_progress=0.5,
        match_method="timestamp",
    )

    bundle = build_render_bundle(
        job_id=job_id,
        project_id=project_id,
        project_title="Alpine Tour",
        spec_json={
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 10.0,
            "show_photos": True,
        },
        stages=[stage],
        store=store,
        photos=[photo],
    )

    assert isinstance(bundle, RenderBundle)
    assert isinstance(bundle.manifest, RenderManifest)
    assert bundle.manifest.job_id == str(job_id)
    assert bundle.manifest.total_frames == 300
    assert len(bundle.manifest.stages) == 1
    assert len(bundle.manifest.photos) == 1
    assert bundle.manifest.photos[0].data_uri == ""
    assert bundle.manifest.photos[0].asset_url == f"https://render-assets.local/{photo_id}"
    assert bundle.photo_assets == {str(photo_id): thumb_bytes}

    roundtripped = RenderManifest.model_validate_json(bundle.manifest.model_dump_json())
    assert roundtripped.job_id == str(job_id)
    assert roundtripped.photos[0].asset_url == f"https://render-assets.local/{photo_id}"
