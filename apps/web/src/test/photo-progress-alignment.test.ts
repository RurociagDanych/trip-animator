import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2, type RenderManifest } from "../lib/api/types";
import { matchPhotosToTimeline, evaluateTimelineSchedule } from "../lib/geo/photos";
import { buildTimelineFromManifest, evaluateTimeline } from "../lib/geo/timeline";

function point(lon: number, cum: number, elapsed: number) {
  return {
    lat: 0,
    lon,
    lon_unwrapped: lon,
    elev_m: 0,
    cum_dist_m: cum,
    speed_mps: 5,
    grade_pct: 0,
    elapsed_s: elapsed,
    ts_utc: null,
    segment_idx: 0,
  };
}

function manifestWithStageDistance(firstStageDistanceM: number): RenderManifest {
  const spec = createDefaultRenderSpecV2();
  spec.duration_seconds = 10;
  spec.intro_hold_seconds = 0;
  spec.outro_hold_seconds = 0;
  spec.photo_display_seconds = 2;
  return {
    job_id: "job-1",
    project_id: "proj-1",
    project_title: "Alignment",
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
        distance_m: firstStageDistanceM,
        elevation_gain_m: 0,
        moving_seconds: 200,
        elapsed_seconds: 200,
        has_timestamps: false,
        points: [point(0, 0, 0), point(0.01, 1000, 200)],
      },
      {
        id: "s-2",
        name: "Day 2",
        day_index: 2,
        stage_order: 1,
        color: "#000000",
        transition: "none",
        distance_m: 1000,
        elevation_gain_m: 0,
        moving_seconds: 200,
        elapsed_seconds: 200,
        has_timestamps: false,
        points: [point(0.02, 0, 0), point(0.03, 1000, 200)],
      },
    ],
  } as unknown as RenderManifest;
}

describe("manual photo progress alignment with the marker", () => {
  it("triggers a manual pin when the marker reaches the pin's track position", () => {
    const manifest = manifestWithStageDistance(1500);
    const stages = buildTimelineFromManifest(manifest);
    const spec = { ...manifest.spec, show_photos: true };
    const pinLon = 0.025;
    const [photo] = matchPhotosToTimeline(
      [
        {
          id: "p-1",
          caption: null,
          width: 100,
          height: 100,
          taken_at_utc: null,
          lat: 0,
          lon: pinLon,
          enabled: true,
          match_method: "manual",
          matched_progress: 0.75,
          matched_lat: 0,
          matched_lon: pinLon,
        },
      ] as unknown as Parameters<typeof matchPhotosToTimeline>[0],
      stages,
      spec,
      () => "/photo.jpg",
    );

    const markerAtPin = evaluateTimeline(stages, spec, photo!.matchedProgress!);
    expect(markerAtPin.markerPosition[0]).toBeCloseTo(pinLon, 4);

    const beforePin = evaluateTimelineSchedule(stages, [photo!], spec, 0, 10, false);
    expect(beforePin.activePhoto).toBeNull();
  });
});
