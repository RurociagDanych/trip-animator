from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from trip_telemetry import (
    ActivityType,
    ParsedTrack,
    TrackPoint,
    build_silver_dataframe,
    write_silver_parquet,
)
from trip_worker import (
    LocalObjectStore,
    ManifestPoint,
    PhotoRecord,
    StageRecord,
    build_color_segments,
    build_render_manifest,
    compute_frame_render_spans,
    query_stage_kpis_and_manifest_points,
)
from trip_worker.manifest import (
    _downsample_manifest_points,
    _fill_stride_indices,
    _format_ts_iso,
    query_stage_manifest_points,
)


def _write_stage_parquet(
    store: LocalObjectStore,
    *,
    project_id: uuid.UUID,
    stage_id: uuid.UUID,
    points: list[TrackPoint],
    trim_start_s: float | None = None,
    trim_end_s: float | None = None,
) -> tuple[str, float, float]:
    parsed = ParsedTrack(
        name="Test Stage",
        points=points,
        point_count=len(points),
        segment_count=len({p.segment_idx for p in points}),
        has_timestamps=all(p.ts_utc is not None for p in points),
        has_elevation=any(p.elev_m is not None for p in points),
    )
    df, summary = build_silver_dataframe(
        parsed,
        trip_id=str(project_id),
        stage_id=str(stage_id),
        trim_start_s=trim_start_s,
        trim_end_s=trim_end_s,
    )
    key = f"silver/{project_id}/{stage_id}.parquet"
    tmp_file = store.root_dir / "_tmp" / f"{stage_id}.parquet"
    write_silver_parquet(df, tmp_file)
    store.put_bytes(key, tmp_file.read_bytes(), content_type="application/vnd.apache.parquet")
    tmp_file.unlink()
    return key, summary.distance_m, summary.elevation_gain_m


def test_build_render_manifest_multi_stage_with_trim_and_antimeridian(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    job_id = uuid.uuid4()
    stage1_id = uuid.uuid4()
    stage2_id = uuid.uuid4()

    t0 = datetime(2001, 6, 1, 8, 0, 0, tzinfo=UTC)
    s1_points = [
        TrackPoint(lat=0.100, lon=179.990, elev_m=1000.0, ts_utc=t0, segment_idx=0),
        TrackPoint(
            lat=0.105,
            lon=-179.990,
            elev_m=1050.0,
            ts_utc=t0 + timedelta(seconds=60),
            segment_idx=0,
        ),
        TrackPoint(
            lat=0.110,
            lon=-179.980,
            elev_m=1100.0,
            ts_utc=t0 + timedelta(seconds=120),
            segment_idx=0,
        ),
        TrackPoint(
            lat=0.115,
            lon=-179.970,
            elev_m=1120.0,
            ts_utc=t0 + timedelta(seconds=180),
            segment_idx=0,
        ),
    ]
    s2_points = [
        TrackPoint(lat=0.300, lon=0.200, elev_m=1400.0, ts_utc=t0, segment_idx=0),
        TrackPoint(
            lat=0.310,
            lon=0.210,
            elev_m=1500.0,
            ts_utc=t0 + timedelta(seconds=100),
            segment_idx=1,
        ),
    ]

    key1, dist1, gain1 = _write_stage_parquet(
        store,
        project_id=project_id,
        stage_id=stage1_id,
        points=s1_points,
        trim_start_s=60.0,
        trim_end_s=180.0,
    )
    key2, dist2, gain2 = _write_stage_parquet(
        store,
        project_id=project_id,
        stage_id=stage2_id,
        points=s2_points,
    )

    stages = [
        # Deliberately pass stage_order=1 before 0 to verify ordering
        StageRecord(
            id=stage2_id,
            project_id=project_id,
            name="Fictional Day 2",
            stage_order=1,
            day_index=2,
            trim_start_s=None,
            trim_end_s=None,
            silver_object_key=key2,
            distance_m=dist2,
            elevation_gain_m=gain2,
            moving_seconds=100.0,
            elapsed_seconds=100.0,
            has_timestamps=True,
        ),
        StageRecord(
            id=stage1_id,
            project_id=project_id,
            name="Pacific Crossing Day 1",
            stage_order=0,
            day_index=1,
            trim_start_s=60.0,
            trim_end_s=180.0,
            silver_object_key=key1,
            distance_m=dist1,
            elevation_gain_m=gain1,
            moving_seconds=120.0,
            elapsed_seconds=120.0,
            has_timestamps=True,
        ),
    ]

    manifest = build_render_manifest(
        job_id=job_id,
        project_id=project_id,
        project_title="Fictional Crossing",
        spec_json={
            "version": 2,
            "resolution": "1080x1920",
            "fps": 60,
            "duration_seconds": 12.0,
            "map_style": "dark",
            "camera_mode": "follow-marker",
            "stage_colors": {str(stage1_id): "#22C55E"},
            "stage_transitions": {str(stage1_id): "bed"},
            "is_preview": False,
        },
        stages=stages,
        store=store,
    )

    assert manifest.width == 1080
    assert manifest.height == 1920
    assert manifest.fps == 60
    assert manifest.duration_s == 12.0
    assert manifest.total_frames == 720
    assert manifest.is_preview is False

    assert [s.id for s in manifest.stages] == [str(stage1_id), str(stage2_id)]
    first, second = manifest.stages
    assert first.color == "#22C55E"
    assert first.transition == "bed"
    assert len(first.points) == 3
    assert first.points[0].cum_dist_m == 0.0
    assert first.points[0].elapsed_s == 0.0
    assert first.points[-1].lon_unwrapped > 180.0

    assert second.color == "#F97316"
    assert second.transition == "none"
    assert [p.segment_idx for p in second.points] == [0, 1]


def test_build_render_manifest_preview_caps_duration_and_downsamples(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 2, 9, 0, 0, tzinfo=UTC)

    points = [
        TrackPoint(
            lat=0.1 + i * 0.001 + (0.002 if i % 2 == 0 else -0.002),
            lon=0.2 + i * 0.001,
            elev_m=1200.0 + i,
            ts_utc=t0 + timedelta(seconds=i * 5),
            segment_idx=0 if i < 150 else 1,
        )
        for i in range(300)
    ]
    key, dist, gain = _write_stage_parquet(
        store,
        project_id=project_id,
        stage_id=stage_id,
        points=points,
    )

    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Long Stage",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=key,
        distance_m=dist,
        elevation_gain_m=gain,
        moving_seconds=1495.0,
        elapsed_seconds=1495.0,
        has_timestamps=True,
    )

    manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Preview Trip",
        spec_json={
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 30.0,
            "is_preview": True,
        },
        stages=[stage],
        store=store,
        max_points_per_stage=60,
    )

    assert manifest.is_preview is True
    assert manifest.fps == 15
    assert manifest.duration_s == 5.0
    assert manifest.total_frames == 75
    assert len(manifest.stages[0].points) <= 60
    assert manifest.stages[0].points[0].segment_idx == 0
    assert manifest.stages[0].points[-1].segment_idx == 1


def test_build_render_manifest_total_points_cap_and_activity_type(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    t0 = datetime(2001, 6, 3, 8, 0, 0, tzinfo=UTC)

    stages: list[StageRecord] = []
    activity_types: tuple[ActivityType, ...] = ("gravel_mtb", "hiking", "train")
    for idx, act_type in enumerate(activity_types):
        sid = uuid.uuid4()
        pts = [
            TrackPoint(
                lat=0.1 + idx * 0.1 + i * 0.0005,
                lon=0.2 + i * 0.0005,
                elev_m=1000.0 + i,
                ts_utc=t0 + timedelta(seconds=i * 10),
                segment_idx=0,
            )
            for i in range(120)
        ]
        key, dist, gain = _write_stage_parquet(
            store,
            project_id=project_id,
            stage_id=sid,
            points=pts,
        )
        stages.append(
            StageRecord(
                id=sid,
                project_id=project_id,
                name=f"Stage {idx + 1}",
                stage_order=idx,
                day_index=idx + 1,
                trim_start_s=None,
                trim_end_s=None,
                silver_object_key=key,
                distance_m=dist,
                elevation_gain_m=gain,
                moving_seconds=1190.0,
                elapsed_seconds=1190.0,
                has_timestamps=True,
                activity_type=act_type,
            )
        )

    manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Multi-modal Cap Trip",
        spec_json={"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        stages=stages,
        store=store,
        max_points_per_stage=12_000,
        max_total_manifest_points=90,
    )

    assert [s.activity_type for s in manifest.stages] == ["gravel_mtb", "hiking", "train"]
    total_points = sum(len(s.points) for s in manifest.stages)
    assert total_points <= 90
    assert all(len(s.points) == 30 for s in manifest.stages)


def _make_collinear_points(count: int, *, segments: int = 1) -> list[ManifestPoint]:
    pts: list[ManifestPoint] = []
    seg_size = max(1, count // segments)
    for i in range(count):
        seg_idx = min(segments - 1, i // seg_size)
        pts.append(
            ManifestPoint(
                lat=round(0.1 + i * 0.0001, 6),
                lon=round(0.2 + i * 0.0001, 6),
                lon_unwrapped=round(0.2 + i * 0.0001, 6),
                elev_m=1000.0 + i * 2.0,
                cum_dist_m=i * 15.0,
                speed_mps=5.0 + (i % 10),
                grade_pct=2.0,
                elapsed_s=float(i * 3),
                ts_utc=None,
                segment_idx=seg_idx,
            )
        )
    return pts


def test_downsample_preserves_collinear_telemetry_and_guards_small_max_points() -> None:
    collinear = _make_collinear_points(500)
    downsampled = _downsample_manifest_points(collinear, max_points=50)
    assert len(downsampled) == 50
    assert downsampled[0] == collinear[0]
    assert downsampled[-1] == collinear[-1]
    assert downsampled[25].elev_m is not None
    assert 1400.0 < downsampled[25].elev_m < 1600.0
    assert downsampled[25].elapsed_s is not None
    assert 600.0 < downsampled[25].elapsed_s < 900.0

    multi_seg = _make_collinear_points(300, segments=3)
    downsampled_multi = _downsample_manifest_points(multi_seg, max_points=30)
    assert len(downsampled_multi) == 30
    assert {p.segment_idx for p in downsampled_multi} == {0, 1, 2}

    # Guard against ZeroDivisionError when max_points <= 1
    assert _downsample_manifest_points(collinear, max_points=1) == [collinear[0]]
    assert _downsample_manifest_points(collinear, max_points=0) == []
    assert _downsample_manifest_points(collinear, max_points=-3) == []


def test_build_render_manifest_handles_min_floor_one_point_per_stage(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    t0 = datetime(2001, 6, 4, 8, 0, 0, tzinfo=UTC)
    stages: list[StageRecord] = []

    for idx in range(3):
        sid = uuid.uuid4()
        pts = [
            TrackPoint(
                lat=0.1 + idx * 0.1 + i * 0.0005,
                lon=0.2 + i * 0.0005,
                elev_m=1000.0 + i,
                ts_utc=t0 + timedelta(seconds=i * 10),
                segment_idx=0,
            )
            for i in range(10)
        ]
        key, dist, gain = _write_stage_parquet(
            store, project_id=project_id, stage_id=sid, points=pts
        )
        stages.append(
            StageRecord(
                id=sid,
                project_id=project_id,
                name=f"Stage {idx + 1}",
                stage_order=idx,
                day_index=idx + 1,
                trim_start_s=None,
                trim_end_s=None,
                silver_object_key=key,
                distance_m=dist,
                elevation_gain_m=gain,
                moving_seconds=90.0,
                elapsed_seconds=90.0,
                has_timestamps=True,
            )
        )

    manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Tiny Budget",
        spec_json={"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
        stages=stages,
        store=store,
        max_points_per_stage=100,
        max_total_manifest_points=3,
    )
    assert sum(len(s.points) for s in manifest.stages) <= 3
    assert all(len(s.points) == 1 for s in manifest.stages)


def _make_90_deg_zigzag_points(count: int) -> list[ManifestPoint]:
    pts: list[ManifestPoint] = []
    lat = 0.100
    lon = 0.200
    for i in range(count):
        if i > 0:
            if i % 2 == 1:
                lat = round(lat + 0.01, 6)
            else:
                lon = round(lon + 0.01, 6)
        pts.append(
            ManifestPoint(
                lat=lat,
                lon=lon,
                lon_unwrapped=lon,
                elev_m=1000.0 + i * 5.0,
                cum_dist_m=i * 100.0,
                speed_mps=6.0,
                grade_pct=3.0,
                elapsed_s=float(i * 10),
                ts_utc=None,
                segment_idx=0,
            )
        )
    return pts


def _make_duplicate_coord_points(count: int) -> list[ManifestPoint]:
    pts: list[ManifestPoint] = []
    for i in range(count):
        lat = round(0.1 + (i // 3) * 0.002, 6)
        lon = round(0.2 + (i // 3) * 0.002, 6)
        pts.append(
            ManifestPoint(
                lat=lat,
                lon=lon,
                lon_unwrapped=lon,
                elev_m=1100.0 + i,
                cum_dist_m=float(i * 5),
                speed_mps=0.0 if i % 3 != 0 else 4.5,
                grade_pct=0.0,
                elapsed_s=float(i * 4),
                ts_utc=None,
                segment_idx=0,
            )
        )
    return pts


def test_downsample_budget_matrix_collinear_zigzag_and_duplicate_coords() -> None:
    collinear = _make_collinear_points(12)
    zigzag = _make_90_deg_zigzag_points(12)
    duplicates = _make_duplicate_coord_points(12)
    all_identical = [
        ManifestPoint(
            lat=0.1,
            lon=0.2,
            lon_unwrapped=0.2,
            elev_m=1000.0 + i,
            cum_dist_m=float(i),
            speed_mps=0.0,
            grade_pct=0.0,
            elapsed_s=float(i),
            ts_utc=None,
            segment_idx=0,
        )
        for i in range(10)
    ]

    for dataset_name, pts in (
        ("collinear", collinear),
        ("zigzag", zigzag),
        ("duplicates", duplicates),
        ("all_identical", all_identical),
    ):
        n = len(pts)
        for max_points in (0, 1, 2, 3, n - 1, n, n + 10):
            out = _downsample_manifest_points(pts, max_points=max_points)
            expected_len = min(n, max(0, max_points))
            assert len(out) == expected_len, (
                f"{dataset_name} max_points={max_points}: expected {expected_len}, got {len(out)}"
            )
            if expected_len >= 1:
                assert out[0] == pts[0]
            if expected_len >= 2:
                assert out[-1] == pts[-1]
            cum_dists = [p.cum_dist_m for p in out]
            assert cum_dists == sorted(cum_dists)

    for max_points in (0, 1, 2, 3, 10):
        assert _downsample_manifest_points([], max_points=max_points) == []

    zigzag_n_minus_1 = _downsample_manifest_points(zigzag, max_points=len(zigzag) - 1)
    assert len(zigzag_n_minus_1) == len(zigzag) - 1
    assert zigzag_n_minus_1[0] == zigzag[0]
    assert zigzag_n_minus_1[-1] == zigzag[-1]

    assert _fill_stride_indices(0, set(), 5) == []
    assert _fill_stride_indices(5, {0, 1, 3, 4}, 5) == [0, 1, 2, 3, 4]
    assert _fill_stride_indices(6, {0, 1, 2, 3, 4, 5}, 3) == [0, 2, 5]


def test_manifest_validation_trim_empty_and_timestamp_formatting(tmp_path: Path) -> None:
    assert _format_ts_iso(None) is None
    assert _format_ts_iso(1_000_000) == "1970-01-01T00:00:01Z"
    assert _format_ts_iso(datetime(2001, 6, 1, 12, 0, 0)) == "2001-06-01T12:00:00Z"
    assert _format_ts_iso(datetime(2001, 6, 1, 12, 0, 0, tzinfo=UTC)) == "2001-06-01T12:00:00Z"
    assert _format_ts_iso("not-a-timestamp") is None

    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 5, 8, 0, 0, tzinfo=UTC)
    key, _, _ = _write_stage_parquet(
        store,
        project_id=project_id,
        stage_id=stage_id,
        points=[
            TrackPoint(lat=0.10, lon=0.20, elev_m=1000.0, ts_utc=t0, segment_idx=0),
            TrackPoint(
                lat=0.11,
                lon=0.21,
                elev_m=1020.0,
                ts_utc=t0 + timedelta(seconds=30),
                segment_idx=0,
            ),
        ],
    )
    pq_path = store.resolve_existing_path(key)

    with pytest.raises(ValueError, match="trim_start_s"):
        query_stage_manifest_points(pq_path, trim_start_s=-1.0)
    with pytest.raises(ValueError, match="trim_end_s"):
        query_stage_manifest_points(pq_path, trim_end_s=float("nan"))
    with pytest.raises(ValueError, match="strictly less"):
        query_stage_manifest_points(pq_path, trim_start_s=20.0, trim_end_s=10.0)
    with pytest.raises(ValueError, match="zero active telemetry points"):
        query_stage_manifest_points(pq_path, trim_start_s=100.0, trim_end_s=200.0)

    with pytest.raises(ValueError, match="zero stages"):
        build_render_manifest(
            job_id=uuid.uuid4(),
            project_id=project_id,
            project_title="Empty",
            spec_json={"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 5.0},
            stages=[],
            store=store,
        )


def test_build_render_manifest_embeds_photo_data_uris_and_respects_show_photos(
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 5, 8, 0, 0, tzinfo=UTC)
    key, dist, gain = _write_stage_parquet(
        store,
        project_id=project_id,
        stage_id=stage_id,
        points=[
            TrackPoint(lat=0.10, lon=0.20, elev_m=1000.0, ts_utc=t0, segment_idx=0),
            TrackPoint(
                lat=0.11,
                lon=0.21,
                elev_m=1020.0,
                ts_utc=t0 + timedelta(seconds=30),
                segment_idx=0,
            ),
        ],
    )
    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Stage 1",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=key,
        distance_m=dist,
        elevation_gain_m=gain,
        moving_seconds=30.0,
        elapsed_seconds=30.0,
        has_timestamps=True,
    )

    photo_id = uuid.uuid4()
    missing_photo_id = uuid.uuid4()
    thumb_key = f"photos/{project_id}/{photo_id}_thumb.webp"
    thumb_bytes = b"RIFF\x10\x00\x00\x00WEBPVP8 \x04\x00\x00\x00test"
    store.put_bytes(thumb_key, thumb_bytes, content_type="image/webp")

    photos = [
        PhotoRecord(
            id=photo_id,
            project_id=project_id,
            thumb_object_key=thumb_key,
            width=640,
            height=480,
            caption="Passo Gardena",
            taken_at_utc=t0 + timedelta(seconds=15),
            lat=0.105,
            lon=0.205,
            match_method="timestamp",
        ),
        PhotoRecord(
            id=missing_photo_id,
            project_id=project_id,
            thumb_object_key=f"photos/{project_id}/{missing_photo_id}_thumb.webp",
            width=640,
            height=480,
            caption="Missing blob should be skipped",
        ),
    ]

    manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Photo Manifest",
        spec_json={
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 10.0,
            "show_photos": True,
        },
        stages=[stage],
        store=store,
        photos=photos,
    )
    assert len(manifest.photos) == 1
    mp = manifest.photos[0]
    assert mp.id == str(photo_id)
    assert mp.caption == "Passo Gardena"
    assert mp.taken_at_utc == "2001-06-05T08:00:15Z"
    assert mp.data_uri.startswith("data:image/webp;base64,")
    assert "UklGRhAAAABXRUJQ" in mp.data_uri

    hidden_manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Photo Manifest Hidden",
        spec_json={
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 10.0,
            "show_photos": False,
        },
        stages=[stage],
        store=store,
        photos=photos,
    )
    assert hidden_manifest.photos == []


def test_single_pass_fetchnumpy_kpis_virtual_photo_urls_and_color_segments(
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 6, 7, 0, 0, tzinfo=UTC)
    points = [
        TrackPoint(
            lat=0.1 + i * 0.0004,
            lon=0.2 + i * 0.0004,
            elev_m=1000.0 + i * 8.0 if i % 5 != 0 else None,
            ts_utc=t0 + timedelta(seconds=i * 10),
            hr_bpm=130 + (i % 40),
            cadence=80 + (i % 15),
            watts=180 + (i % 120),
            segment_idx=0 if i < 25 else 1,
        )
        for i in range(50)
    ]
    key, _, _ = _write_stage_parquet(store, project_id=project_id, stage_id=stage_id, points=points)
    pq_path = store.resolve_existing_path(key)
    kpis, sampled = query_stage_kpis_and_manifest_points(pq_path, max_points=20)
    assert kpis["point_count"] == 50
    assert kpis["total_distance_m"] > 0.0
    assert kpis["elevation_gain_m"] > 0.0
    assert len(sampled) <= 20
    assert sampled[0].segment_idx == 0
    assert sampled[-1].segment_idx == 1
    assert any(p.hr_bpm is not None and p.watts is not None for p in sampled)

    photo_id = uuid.uuid4()
    thumb_key = f"photos/{project_id}/{photo_id}_thumb.webp"
    thumb_bytes = b"RIFF\x10\x00\x00\x00WEBPVP8 \x04\x00\x00\x00test"
    store.put_bytes(thumb_key, thumb_bytes, content_type="image/webp")

    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Color Stage",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=key,
        distance_m=float(kpis["total_distance_m"]),
        elevation_gain_m=float(kpis["elevation_gain_m"]),
        moving_seconds=float(kpis["moving_time_s"]),
        elapsed_seconds=float(kpis["elapsed_time_s"]),
        has_timestamps=True,
    )
    photo = PhotoRecord(
        id=photo_id,
        project_id=project_id,
        thumb_object_key=thumb_key,
        width=640,
        height=480,
        caption="Summit",
        matched_progress=0.5,
        match_method="gps",
    )
    photo_assets: dict[str, bytes] = {}
    manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Virtual Photo Manifest",
        spec_json={"version": 2, "resolution": "1280x720", "fps": 30, "duration_seconds": 10.0},
        stages=[stage],
        store=store,
        photos=[photo],
        embed_photo_data_uris=False,
        out_photo_assets=photo_assets,
    )
    assert len(manifest.photos) == 1
    assert manifest.photos[0].data_uri == ""
    assert manifest.photos[0].asset_url == f"https://render-assets.local/{photo_id}"
    assert photo_assets == {str(photo_id): thumb_bytes}

    for mode in ("grade", "speed", "hr", "watts"):
        segs = build_color_segments(sampled, mode, "#F97316")
        assert segs
        assert segs[0].start_index == 0
        assert segs[-1].end_index == len(sampled) - 1
    assert build_color_segments(sampled, "stage", "#F97316") == []


def test_compute_frame_render_spans_deduplicates_holds_and_photo_plateaus(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 7, 8, 0, 0, tzinfo=UTC)
    key, dist, gain = _write_stage_parquet(
        store,
        project_id=project_id,
        stage_id=stage_id,
        points=[
            TrackPoint(lat=0.10, lon=0.20, elev_m=1000.0, ts_utc=t0, segment_idx=0),
            TrackPoint(
                lat=0.11,
                lon=0.21,
                elev_m=1100.0,
                ts_utc=t0 + timedelta(seconds=60),
                segment_idx=0,
            ),
        ],
    )
    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Hold Test",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=key,
        distance_m=dist,
        elevation_gain_m=gain,
        moving_seconds=60.0,
        elapsed_seconds=60.0,
        has_timestamps=True,
    )
    photo_id = uuid.uuid4()
    thumb_key = f"photos/{project_id}/{photo_id}_thumb.webp"
    store.put_bytes(thumb_key, b"RIFFwebp", content_type="image/webp")
    photo = PhotoRecord(
        id=photo_id,
        project_id=project_id,
        thumb_object_key=thumb_key,
        width=640,
        height=480,
        matched_progress=0.5,
        match_method="gps",
    )

    manifest = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Pause Dedup",
        spec_json={
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 10.0,
            "intro_hold_seconds": 1.0,
            "outro_hold_seconds": 1.5,
            "pause_on_photo": True,
            "photo_display_seconds": 3.0,
            "show_photos": True,
        },
        stages=[stage],
        store=store,
        photos=[photo],
    )
    spans = compute_frame_render_spans(manifest, 0, manifest.total_frames)
    assert sum(s.repeat_count for s in spans) == manifest.total_frames
    assert len(spans) < manifest.total_frames - 100
    assert any(s.repeat_count > 10 for s in spans)

    chunk_spans = compute_frame_render_spans(manifest, 50, 200)
    assert sum(s.repeat_count for s in chunk_spans) == 150
    assert chunk_spans[0].frame_idx == 50

    orbit_manifest = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(update={"camera_preset": "drone-orbit"}),
        }
    )
    orbit_spans = compute_frame_render_spans(orbit_manifest, 0, orbit_manifest.total_frames)
    assert len(orbit_spans) > len(spans)


def test_build_render_manifest_includes_climbs_and_respects_show_climb_callouts(
    tmp_path: Path,
) -> None:
    store = LocalObjectStore(tmp_path)
    project_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    t0 = datetime(2001, 6, 8, 8, 0, 0, tzinfo=UTC)

    points = [
        TrackPoint(
            lat=0.100 + i * 0.00045,
            lon=0.200 + i * 0.00045,
            elev_m=1000.0 + i * 5.0,
            ts_utc=t0 + timedelta(seconds=i * 15),
            segment_idx=0,
        )
        for i in range(65)
    ]
    key, dist, gain = _write_stage_parquet(
        store, project_id=project_id, stage_id=stage_id, points=points
    )
    stage = StageRecord(
        id=stage_id,
        project_id=project_id,
        name="Passo Climb",
        stage_order=0,
        day_index=1,
        trim_start_s=None,
        trim_end_s=None,
        silver_object_key=key,
        distance_m=dist,
        elevation_gain_m=gain,
        moving_seconds=960.0,
        elapsed_seconds=960.0,
        has_timestamps=True,
    )

    manifest_with_climbs = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="Climb Trip",
        spec_json={
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 10.0,
            "show_climb_callouts": True,
            "color_by": "grade",
        },
        stages=[stage],
        store=store,
    )
    assert len(manifest_with_climbs.climbs) >= 1
    climb = manifest_with_climbs.climbs[0]
    assert climb.stage_id == str(stage_id)
    assert climb.elevation_gain_m >= 100.0
    assert 0.0 <= climb.start_progress <= climb.summit_progress <= 1.0
    assert manifest_with_climbs.stages[0].color_segments

    manifest_no_climbs = build_render_manifest(
        job_id=uuid.uuid4(),
        project_id=project_id,
        project_title="No Climbs",
        spec_json={
            "version": 2,
            "resolution": "1280x720",
            "fps": 30,
            "duration_seconds": 10.0,
            "show_climb_callouts": False,
        },
        stages=[stage],
        store=store,
    )
    assert manifest_no_climbs.climbs == []
