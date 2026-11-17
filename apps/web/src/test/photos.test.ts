import { describe, expect, it } from "vitest";
import {
  createDefaultRenderSpecV2,
  type ManifestPhoto,
  type ProjectPhotoOut,
} from "../lib/api/types";
import {
  evaluateTimelineSchedule,
  matchPhotosToTimeline,
  resolvePhotoSrc,
} from "../lib/geo/photos";
import type { TimelinePoint, TimelineStage } from "../lib/geo/timeline";

function makePoint(
  lon: number,
  lat: number,
  cumDistM: number,
  elapsedS: number,
  tsIso: string,
): TimelinePoint {
  return {
    lat,
    lon,
    lonUnwrapped: lon,
    elevM: 1500,
    cumDistM,
    globalDistM: cumDistM,
    speedMps: 5,
    gradePct: 2,
    elapsedS,
    globalElapsedS: elapsedS,
    tsUtcMs: Date.parse(tsIso),
    segmentIdx: 0,
  };
}

function makeStage(): TimelineStage {
  return {
    id: "stage-1",
    name: "Fictional Equatorial Stage",
    dayIndex: 1,
    stageOrder: 0,
    activityType: "cycling",
    color: "#38bdf8",
    transition: "none",
    distanceM: 10_000,
    elevationGainM: 300,
    movingSeconds: 1000,
    elapsedSeconds: 1000,
    hasTimestamps: true,
    points: [
      makePoint(0.2, 0.1, 0, 0, "2040-04-12T08:00:00Z"),
      makePoint(0.3, 0.1, 10_000, 1000, "2040-04-12T08:16:40Z"),
    ],
  };
}

function makePhoto(overrides: Partial<ProjectPhotoOut>): ProjectPhotoOut {
  return {
    id: "photo-1",
    project_id: "proj-1",
    stage_id: null,
    source_type: "upload",
    width: 640,
    height: 480,
    byte_size: 24_000,
    caption: "Fictional checkpoint",
    taken_at_utc: null,
    lat: null,
    lon: null,
    matched_lat: null,
    matched_lon: null,
    matched_progress: null,
    match_method: "unmatched",
    enabled: true,
    created_at: "2040-04-12T09:00:00Z",
    ...overrides,
  };
}

describe("Photo matching and pause scheduler (photos.ts)", () => {
  it("matches photos by timestamp interpolation and falls back to GPS when timezone offset > 50km", () => {
    const spec = createDefaultRenderSpecV2();
    const stage = makeStage();

    // 1. Exact midpoint timestamp -> progress 0.5, lon 0.25
    const tsPhoto = makePhoto({
      id: "p-ts",
      taken_at_utc: "2040-04-12T08:08:20Z",
    });

    // Timestamp points to the start; GPS is 0.9 degrees away (>50km at the equator).
    const wideStage: TimelineStage = {
      ...stage,
      distanceM: 111_000,
      points: [
        makePoint(0.0, 0.0, 0, 0, "2040-04-12T08:00:00Z"),
        makePoint(1.0, 0.0, 111_000, 1000, "2040-04-12T08:16:40Z"),
      ],
    };

    const tzMismatchPhoto = makePhoto({
      id: "p-tz",
      taken_at_utc: "2040-04-12T08:00:00Z", // points to 0.0
      lat: 0.001,
      lon: 0.9, // >50km away from 0.0 -> triggers GPS fallback
    });

    const farPhoto = makePhoto({
      id: "p-far",
      lat: 1.0,
      lon: 0.5, // > 50km from route -> unmatched
    });

    const matchedTs = matchPhotosToTimeline([tsPhoto], [stage], spec, (id) => `/thumb/${id}`);
    expect(matchedTs[0]?.matchMethod).toBe("timestamp");
    expect(matchedTs[0]?.matchedProgress).toBeCloseTo(0.5, 2);
    expect(matchedTs[0]?.matchedPosition?.[0]).toBeCloseTo(0.25, 3);
    expect(matchedTs[0]?.src).toBe("/thumb/p-ts");

    const matchedTz = matchPhotosToTimeline([tzMismatchPhoto, farPhoto], [wideStage], spec);
    const tzRes = matchedTz.find((p) => p.id === "p-tz");
    const farRes = matchedTz.find((p) => p.id === "p-far");
    expect(tzRes?.matchMethod).toBe("gps");
    expect(tzRes?.matchedProgress).toBeCloseTo(0.9, 2);
    expect(farRes?.matchMethod).toBe("unmatched");
    expect(farRes?.matchedProgress).toBeNull();
  });

  it("evaluates pause_on_photo monotonically, holds progress at photo stops, and caps pause at 65% of activeDuration", () => {
    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 20;
    spec.intro_hold_seconds = 1;
    spec.outro_hold_seconds = 1;
    spec.photo_display_seconds = 4;
    spec.pause_on_photo = true;
    spec.show_photos = true;

    const stage = makeStage();
    const photos = [
      makePhoto({ id: "p1", taken_at_utc: "2040-04-12T08:04:10Z" }), // 0.25
      makePhoto({ id: "p2", taken_at_utc: "2040-04-12T08:12:30Z" }), // 0.75
    ];
    const matched = matchPhotosToTimeline(photos, [stage], spec, (id) => `/img/${id}`);

    // Active duration = 18s. Total pause = 2 * 4 = 8s (<= 0.65 * 18 = 11.7s). Travel = 10s.
    // Stop 1 at activeT = 0.25 * 10 = 2.5s (t = 3.5s) -> holds progress = 0.25 during t in [3.5, 7.5]
    let prevProgress = -1;
    for (let t = 0; t <= 20; t += 0.25) {
      const res = evaluateTimelineSchedule([stage], matched, spec, t, 20, false);
      expect(res.progressRatio).toBeGreaterThanOrEqual(prevProgress - 1e-9);
      prevProgress = res.progressRatio;
    }

    const duringPause1Start = evaluateTimelineSchedule([stage], matched, spec, 4.0, 20, false);
    const duringPause1End = evaluateTimelineSchedule([stage], matched, spec, 7.0, 20, false);
    expect(duringPause1Start.progressRatio).toBeCloseTo(0.25, 4);
    expect(duringPause1End.progressRatio).toBeCloseTo(0.25, 4);
    expect(duringPause1Start.activePhoto?.photo.id).toBe("p1");
    expect(duringPause1Start.travelDurationS).toBeCloseTo(10, 4);

    // Test 65% cap when many photos would exceed activeDuration
    const manyPhotos = Array.from({ length: 10 }, (_, idx) =>
      makePhoto({
        id: `many-${idx}`,
        taken_at_utc: new Date(Date.parse("2040-04-12T08:00:00Z") + (idx + 1) * 80_000).toISOString(),
      }),
    );
    const matchedMany = matchPhotosToTimeline(manyPhotos, [stage], spec);
    const cappedRes = evaluateTimelineSchedule([stage], matchedMany, spec, 5, 20, false);
    // activeDuration = 18s -> travelDurationS >= 0.35 * 18 = 6.3s
    expect(cappedRes.travelDurationS).toBeGreaterThanOrEqual(18 * 0.35 - 1e-4);
  });

  it("prefers asset_url before data_uri in resolvePhotoSrc and computes Ken Burns zoom in cinematic style", () => {
    const manifestPhotoWithBoth: ManifestPhoto = {
      id: "ph-asset",
      caption: "Fictional checkpoint",
      width: 800,
      height: 600,
      taken_at_utc: "2040-04-12T08:08:20Z",
      lat: 0.1,
      lon: 0.25,
      data_uri: "data:image/webp;base64,AAA",
      asset_url: "https://render-assets.local/ph-asset",
    };
    expect(resolvePhotoSrc(manifestPhotoWithBoth, (id) => `/fallback/${id}`)).toBe(
      "https://render-assets.local/ph-asset",
    );

    const manifestPhotoDataOnly: ManifestPhoto = {
      ...manifestPhotoWithBoth,
      asset_url: null,
    };
    expect(resolvePhotoSrc(manifestPhotoDataOnly, (id) => `/fallback/${id}`)).toBe(
      "data:image/webp;base64,AAA",
    );

    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 10;
    spec.intro_hold_seconds = 0;
    spec.outro_hold_seconds = 0;
    spec.photo_display_seconds = 4;
    spec.photo_card_style = "cinematic";

    const stage = makeStage();
    const matched = matchPhotosToTimeline([manifestPhotoWithBoth], [stage], spec);
    const early = evaluateTimelineSchedule([stage], matched, spec, 5.2, 10, false);
    const late = evaluateTimelineSchedule([stage], matched, spec, 8.5, 10, false);
    expect(early.activePhoto?.kenBurnsScale).toBeGreaterThanOrEqual(1.0);
    expect(late.activePhoto?.kenBurnsScale).toBeGreaterThan(early.activePhoto?.kenBurnsScale ?? 1.0);
    expect(late.activePhoto?.kenBurnsScale).toBeLessThanOrEqual(1.12);

    const cornerSpec = { ...spec, photo_card_style: "corner" as const };
    const cornerRes = evaluateTimelineSchedule([stage], matched, cornerSpec, 6.0, 10, false);
    expect(cornerRes.activePhoto?.kenBurnsScale).toBe(1.0);
  });
});
