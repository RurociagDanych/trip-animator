import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2, type RenderManifest } from "../lib/api/types";
import { matchPhotosToTimeline } from "../lib/geo/photos";
import { buildTimelineFromManifest, evaluateTimeline } from "../lib/geo/timeline";

function point(lon: number, cum: number, elapsed: number, segment: number) {
  return {
    lat: 0,
    lon,
    lon_unwrapped: lon,
    elev_m: 0,
    cum_dist_m: cum,
    speed_mps: 5,
    grade_pct: 0,
    elapsed_s: elapsed,
    ts_utc: new Date(Date.UTC(2040, 3, 12, 8, 0, 0) + elapsed * 1000).toISOString(),
    segment_idx: segment,
  };
}

function gappedManifest(): RenderManifest {
  const spec = createDefaultRenderSpecV2();
  spec.duration_seconds = 10;
  spec.intro_hold_seconds = 0;
  spec.outro_hold_seconds = 0;
  return {
    job_id: "job-1",
    project_id: "proj-1",
    project_title: "Segment gap",
    spec,
    width: 1920,
    height: 1080,
    fps: 30,
    duration_s: 10,
    total_frames: 300,
    is_preview: false,
    stages: [
      {
        id: "s-1",
        name: "Day 1",
        day_index: 1,
        stage_order: 0,
        color: "#000000",
        transition: "none",
        distance_m: 6000,
        elevation_gain_m: 0,
        moving_seconds: 300,
        elapsed_seconds: 300,
        has_timestamps: true,
        points: [
          point(0.0, 0, 0, 0),
          point(0.01, 1000, 100, 0),
          point(0.05, 5000, 200, 1),
          point(0.06, 6000, 300, 1),
        ],
      },
    ],
  } as unknown as RenderManifest;
}

describe("timestamp-matched photos inside a track gap", () => {
  it("places a timestamp photo where the marker is at the same trigger point", () => {
    const manifest = gappedManifest();
    const stages = buildTimelineFromManifest(manifest);
    const spec = { ...manifest.spec, show_photos: true };
    const takenAt = new Date(Date.UTC(2040, 3, 12, 8, 0, 0) + 150_000).toISOString();
    const [photo] = matchPhotosToTimeline(
      [
        {
          id: "p-1",
          caption: null,
          width: 100,
          height: 100,
          taken_at_utc: takenAt,
          lat: null,
          lon: null,
          enabled: true,
          match_method: "timestamp",
          matched_progress: null,
          matched_lat: null,
          matched_lon: null,
        },
      ] as unknown as Parameters<typeof matchPhotosToTimeline>[0],
      stages,
      spec,
      () => "/photo.jpg",
    );

    expect(photo?.matchedPosition).not.toBeNull();
    const marker = evaluateTimeline(stages, spec, photo!.matchedProgress!);
    expect(marker.markerPosition[0]).toBeCloseTo(photo!.matchedPosition![0], 4);
  });
});
