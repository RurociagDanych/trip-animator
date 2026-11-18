import { describe, expect, it } from "vitest";
import {
  createDefaultRenderSpecV2,
  type CameraKeyframe,
  type ManifestPhoto,
  type ProjectPhotoOut,
} from "../lib/api/types";
import { sampleKeyframeSpeedMultiplier } from "../lib/geo/camera";
import {
  evaluateTimelineSchedule,
  matchPhotosToTimeline,
  progressRatioToTimeRatio,
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

  it("samples keyframe speed multipliers with clamping and unsorted/invalid edge cases", () => {
    expect(sampleKeyframeSpeedMultiplier([], 0.5)).toBe(1.0);

    const kfs: CameraKeyframe[] = [
      {
        progress: 0.8,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 0,
        speed_multiplier: 10, // clamped to 4.0
        label: "Fast",
      },
      {
        progress: 0.2,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 0,
        speed_multiplier: -1, // clamped to 0.25
        label: "Slow",
      },
    ];
    expect(sampleKeyframeSpeedMultiplier(kfs, 0)).toBe(0.25);
    expect(sampleKeyframeSpeedMultiplier(kfs, 0.2)).toBe(0.25);
    expect(sampleKeyframeSpeedMultiplier(kfs, 0.5)).toBeCloseTo(2.125, 4);
    expect(sampleKeyframeSpeedMultiplier(kfs, 1.0)).toBe(4.0);

    const invalidKf: CameraKeyframe[] = [
      {
        progress: 0.5,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 0,
        speed_multiplier: Number.NaN,
        label: "NaN",
      },
      {
        progress: 0.5,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 0,
        speed_multiplier: 0,
        label: "Zero",
      },
    ];
    expect(sampleKeyframeSpeedMultiplier(invalidKf, 0.5)).toBe(1.0);
    expect(sampleKeyframeSpeedMultiplier(invalidKf, 0.9)).toBe(0.25);
  });

  it("warps travel time monotonically for valley (2.5x) vs climb (0.5x) keyframes, syncs photo triggers, and round-trips progressRatioToTimeRatio", () => {
    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 20;
    spec.intro_hold_seconds = 2;
    spec.outro_hold_seconds = 2;
    spec.pause_on_photo = false;
    spec.show_photos = true;
    spec.photo_display_seconds = 2;
    spec.camera_keyframes = [
      {
        progress: 0.0,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 0,
        speed_multiplier: 2.5,
        label: "Valley",
      },
      {
        progress: 0.5,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 0,
        speed_multiplier: 2.5,
        label: "Valley End",
      },
      {
        progress: 0.55,
        camera_mode: "follow-marker",
        camera_preset: "dynamic-3d",
        pitch_deg: 45,
        bearing_offset_deg: 0,
        zoom_offset: 1,
        speed_multiplier: 0.5,
        label: "Climb Start",
      },
      {
        progress: 1.0,
        camera_mode: "follow-marker",
        camera_preset: "dynamic-3d",
        pitch_deg: 45,
        bearing_offset_deg: 0,
        zoom_offset: 1,
        speed_multiplier: 0.5,
        label: "Summit",
      },
    ];

    const stage = makeStage();
    // Photo at midpoint p = 0.5 (end of 2.5x valley)
    const photos = [
      makePhoto({ id: "p-mid", taken_at_utc: "2040-04-12T08:08:20Z" }),
    ];
    const matched = matchPhotosToTimeline(photos, [stage], spec);

    // Monotonicity across entire timeline
    let prevProgress = -1;
    for (let t = 0; t <= 20; t += 0.25) {
      const res = evaluateTimelineSchedule([stage], matched, spec, t, 20, false);
      expect(res.progressRatio).toBeGreaterThanOrEqual(prevProgress - 1e-9);
      prevProgress = res.progressRatio;
    }

    // Halfway through activeDuration (t = 10s), marker is well past 0.5 because first half was 2.5x faster
    const midTimeRes = evaluateTimelineSchedule([stage], matched, spec, 10, 20, false);
    expect(midTimeRes.progressRatio).toBeGreaterThan(0.65);

    // Non-paused photo trigger time matches when marker crosses matchedProgress = 0.5
    const triggerTimeRatio = progressRatioToTimeRatio(matched, spec, 0.5, 20, false);
    const triggerTimeS = triggerTimeRatio * 20;
    const atTrigger = evaluateTimelineSchedule([stage], matched, spec, triggerTimeS, 20, false);
    expect(atTrigger.progressRatio).toBeCloseTo(0.5, 4);
    expect(atTrigger.activePhoto?.photo.id).toBe("p-mid");
    const justBeforeTrigger = evaluateTimelineSchedule(
      [stage],
      matched,
      spec,
      triggerTimeS - 0.1,
      20,
      false,
    );
    expect(justBeforeTrigger.activePhoto).toBeNull();

    // Round-trip progressRatioToTimeRatio -> evaluateTimelineSchedule (non-paused)
    for (const target of [0.1, 0.25, 0.5, 0.72, 0.9]) {
      const tr = progressRatioToTimeRatio(matched, spec, target, 20, false);
      const ev = evaluateTimelineSchedule([stage], matched, spec, tr * 20, 20, false);
      expect(ev.progressRatio).toBeCloseTo(target, 4);
    }

    // Uniform speed_multiplier (all 2.0x) normalizes to exact identity T(p) = p
    const uniformSpec = {
      ...spec,
      camera_keyframes: spec.camera_keyframes.map((kf) => ({ ...kf, speed_multiplier: 2.0 })),
    };
    const uniformMid = evaluateTimelineSchedule([stage], matched, uniformSpec, 10, 20, false);
    expect(uniformMid.progressRatio).toBeCloseTo(0.5, 6);

    // In-place array mutation invalidates WeakMap cache signature
    const mutableKfs = spec.camera_keyframes.map((kf) => ({ ...kf }));
    const mutSpec = { ...spec, camera_keyframes: mutableKfs };
    const beforeMut = evaluateTimelineSchedule([stage], matched, mutSpec, 10, 20, false);
    expect(beforeMut.progressRatio).toBeGreaterThan(0.65);
    mutableKfs.forEach((kf) => {
      kf.speed_multiplier = 1.0;
    });
    const afterMut = evaluateTimelineSchedule([stage], matched, mutSpec, 10, 20, false);
    expect(afterMut.progressRatio).toBeCloseTo(0.5, 6);

    // pause_on_photo + speed curve preserves travelDurationS, pauses at photo, and round-trips
    const pausedSpec = { ...spec, pause_on_photo: true, photo_display_seconds: 4 };
    const pausedTimeRatio = progressRatioToTimeRatio(matched, pausedSpec, 0.5, 20, false);
    const atPauseStart = evaluateTimelineSchedule(
      [stage],
      matched,
      pausedSpec,
      pausedTimeRatio * 20 + 0.5,
      20,
      false,
    );
    expect(atPauseStart.progressRatio).toBeCloseTo(0.5, 4);
    expect(atPauseStart.activePhoto?.photo.id).toBe("p-mid");
    expect(atPauseStart.travelDurationS).toBeCloseTo(12, 4);

    for (const target of [0.15, 0.4, 0.65, 0.88]) {
      const tr = progressRatioToTimeRatio(matched, pausedSpec, target, 20, false);
      const ev = evaluateTimelineSchedule([stage], matched, pausedSpec, tr * 20, 20, false);
      expect(ev.progressRatio).toBeCloseTo(target, 4);
    }
  });
});
