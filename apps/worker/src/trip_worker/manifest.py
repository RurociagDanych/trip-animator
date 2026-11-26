from __future__ import annotations

from trip_telemetry.render_manifest import (
    ByteStore,
    FrameSpan,
    ManifestClimb,
    ManifestColorSegment,
    ManifestPhoto,
    ManifestPoint,
    ManifestStage,
    PhotoRecord,
    RenderBundle,
    RenderManifest,
    StageRecord,
    _detect_stage_climbs_safe,
    _downsample_manifest_points,
    build_color_segments,
    build_render_bundle,
    build_render_manifest,
    chunk_manifest_key,
    compute_frame_render_spans,
    load_photo_asset,
    photo_asset_key,
    query_stage_kpis_and_manifest_points,
    query_stage_manifest_points,
)
from trip_telemetry.stage_timeline import (
    fill_stride_indices as _fill_stride_indices,
)
from trip_telemetry.stage_timeline import (
    format_ts_iso as _format_ts_iso,
)

__all__ = [
    "ByteStore",
    "FrameSpan",
    "ManifestClimb",
    "ManifestColorSegment",
    "ManifestPhoto",
    "ManifestPoint",
    "ManifestStage",
    "PhotoRecord",
    "RenderBundle",
    "RenderManifest",
    "StageRecord",
    "_detect_stage_climbs_safe",
    "_downsample_manifest_points",
    "_fill_stride_indices",
    "_format_ts_iso",
    "build_color_segments",
    "build_render_bundle",
    "build_render_manifest",
    "chunk_manifest_key",
    "compute_frame_render_spans",
    "load_photo_asset",
    "photo_asset_key",
    "query_stage_kpis_and_manifest_points",
    "query_stage_manifest_points",
]
