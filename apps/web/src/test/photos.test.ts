import { describe, expect, it } from "vitest";
import {
  createDefaultRenderSpecV2,
  normalizeRenderSpecV2,
  type CameraKeyframe,
  type ManifestPhoto,
  type ProjectPhotoOut,
} from "../lib/api/types";
import {
  computeStageProgressSpans,
  evaluateStageCameraFocus,
  sampleCombinedSpeedMultiplier,
  sampleKeyframeSpeedMultiplier,
} from "../lib/geo/camera";
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
      taken_at_utc: "2040-04-12T08:00:00Z",
      lat: 0.001,
      lon: 0.9, // >50km away from 0.0 -> triggers GPS fallback
    });

    const farPhoto = makePhoto({
      id: "p-far",
      lat: 1.0,
      lon: 0.5,
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
      makePhoto({ id: "p1", taken_at_utc: "2040-04-12T08:04:10Z" }),
      makePhoto({ id: "p2", taken_at_utc: "2040-04-12T08:12:30Z" }),
    ];
    const matched = matchPhotosToTimeline(photos, [stage], spec, (id) => `/img/${id}`);

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

    const manyPhotos = Array.from({ length: 10 }, (_, idx) =>
      makePhoto({
        id: `many-${idx}`,
        taken_at_utc: new Date(Date.parse("2040-04-12T08:00:00Z") + (idx + 1) * 80_000).toISOString(),
      }),
    );
    const matchedMany = matchPhotosToTimeline(manyPhotos, [stage], spec);
    const cappedRes = evaluateTimelineSchedule([stage], matchedMany, spec, 5, 20, false);
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
    const photos = [
      makePhoto({ id: "p-mid", taken_at_utc: "2040-04-12T08:08:20Z" }),
    ];
    const matched = matchPhotosToTimeline(photos, [stage], spec);

    let prevProgress = -1;
    for (let t = 0; t <= 20; t += 0.25) {
      const res = evaluateTimelineSchedule([stage], matched, spec, t, 20, false);
      expect(res.progressRatio).toBeGreaterThanOrEqual(prevProgress - 1e-9);
      prevProgress = res.progressRatio;
    }

    const midTimeRes = evaluateTimelineSchedule([stage], matched, spec, 10, 20, false);
    expect(midTimeRes.progressRatio).toBeGreaterThan(0.65);

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

    for (const target of [0.1, 0.25, 0.5, 0.72, 0.9]) {
      const tr = progressRatioToTimeRatio(matched, spec, target, 20, false);
      const ev = evaluateTimelineSchedule([stage], matched, spec, tr * 20, 20, false);
      expect(ev.progressRatio).toBeCloseTo(target, 4);
    }

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

  it("warps per-stage speed smoothly in evaluateTimelineSchedule and round-trips progressRatioToTimeRatio with stages", () => {
    const stageA: TimelineStage = {
      id: "stage-fast",
      name: "Stage Alpha",
      dayIndex: 1,
      stageOrder: 0,
      activityType: "cycling",
      color: "#38bdf8",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 100,
      movingSeconds: 1000,
      elapsedSeconds: 1000,
      hasTimestamps: true,
      points: [
        makePoint(0.1, 0.1, 0, 0, "2040-04-12T08:00:00Z"),
        makePoint(0.2, 0.1, 10_000, 1000, "2040-04-12T08:16:40Z"),
      ],
    };
    const stageB: TimelineStage = {
      id: "stage-street",
      name: "Stage Beta",
      dayIndex: 1,
      stageOrder: 1,
      activityType: "walking",
      color: "#14b8a6",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 50,
      movingSeconds: 1000,
      elapsedSeconds: 1000,
      hasTimestamps: true,
      points: [
        {
          ...makePoint(0.2, 0.1, 0, 0, "2040-04-12T09:00:00Z"),
          globalDistM: 10_000,
          globalElapsedS: 1000,
        },
        {
          ...makePoint(0.25, 0.12, 10_000, 1000, "2040-04-12T09:16:40Z"),
          globalDistM: 20_000,
          globalElapsedS: 2000,
        },
      ],
    };
    const stageC: TimelineStage = {
      id: "stage-tail",
      name: "Stage Gamma",
      dayIndex: 2,
      stageOrder: 2,
      activityType: "cycling",
      color: "#f59e0b",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 120,
      movingSeconds: 1000,
      elapsedSeconds: 1000,
      hasTimestamps: true,
      points: [
        {
          ...makePoint(0.25, 0.12, 0, 0, "2040-04-13T08:00:00Z"),
          globalDistM: 20_000,
          globalElapsedS: 2000,
        },
        {
          ...makePoint(0.35, 0.12, 10_000, 1000, "2040-04-13T08:16:40Z"),
          globalDistM: 30_000,
          globalElapsedS: 3000,
        },
      ],
    };

    const stages = [stageA, stageB, stageC];
    const spec = normalizeRenderSpecV2({
      duration_seconds: 30,
      intro_hold_seconds: 1,
      outro_hold_seconds: 1,
      stage_camera_overrides: {
        "stage-street": {
          focus_mode: "street",
          target_zoom: null,
          speed_multiplier: 1.0,
          transition_margin: 0.02,
        },
      },
      climb_callout_duration_s: 5.5,
    });

    expect(spec.climb_callout_duration_s).toBe(5.5);
    expect(spec.stage_camera_overrides["stage-street"]).toEqual({
      focus_mode: "street",
      target_zoom: 15.8,
      speed_multiplier: 0.2,
      transition_margin: 0.02,
    });

    const spans = computeStageProgressSpans(stages, spec);
    expect(spans).toHaveLength(3);
    expect(spans[0]!.startProgress).toBeCloseTo(0, 6);
    expect(spans[1]!.startProgress).toBeCloseTo(1 / 3, 6);
    expect(spans[2]!.startProgress).toBeCloseTo(2 / 3, 6);
    expect(spans[2]!.endProgress).toBeCloseTo(1, 6);

    const inA = evaluateStageCameraFocus(stages, spec, 0.15);
    const atEnterBoundary = evaluateStageCameraFocus(stages, spec, 1 / 3);
    const inB = evaluateStageCameraFocus(stages, spec, 0.5);
    const atExitBoundary = evaluateStageCameraFocus(stages, spec, 2 / 3);
    const inC = evaluateStageCameraFocus(stages, spec, 0.85);

    expect(inA.focusWeight).toBeCloseTo(0, 6);
    expect(inA.targetZoom).toBeNull();
    expect(inA.speedMultiplier).toBeCloseTo(1.0, 6);

    expect(atEnterBoundary.focusWeight).toBeCloseTo(0.5, 4);
    expect(atEnterBoundary.targetZoom).toBeCloseTo(15.8, 4);
    expect(atEnterBoundary.speedMultiplier).toBeCloseTo(0.6, 4);

    expect(inB.focusWeight).toBeCloseTo(1.0, 6);
    expect(inB.targetZoom).toBeCloseTo(15.8, 6);
    expect(inB.speedMultiplier).toBeCloseTo(0.2, 6);
    expect(sampleCombinedSpeedMultiplier(stages, spec, 0.5)).toBeCloseTo(0.2, 6);

    expect(atExitBoundary.focusWeight).toBeCloseTo(0.5, 4);
    expect(inC.focusWeight).toBeCloseTo(0, 6);
    expect(inC.speedMultiplier).toBeCloseTo(1.0, 6);

    const tEnterB = progressRatioToTimeRatio([], spec, 1 / 3, 30, false, stages) * 30;
    const tExitB = progressRatioToTimeRatio([], spec, 2 / 3, 30, false, stages) * 30;
    const tEnd = 29;
    const durA = tEnterB - 1;
    const durB = tExitB - tEnterB;
    const durC = tEnd - tExitB;
    expect(durB).toBeGreaterThan(durA * 3.5);
    expect(durC).toBeCloseTo(durA, 1);

    let prevProgress = -1;
    for (let t = 0; t <= 30; t += 0.5) {
      const res = evaluateTimelineSchedule(stages, [], spec, t, 30, false);
      expect(res.progressRatio).toBeGreaterThanOrEqual(prevProgress - 1e-9);
      prevProgress = res.progressRatio;
    }

    for (const target of [0.08, 0.25, 0.34, 0.5, 0.67, 0.85, 0.96]) {
      const timeRatio = progressRatioToTimeRatio([], spec, target, 30, false, stages);
      const evaluated = evaluateTimelineSchedule(stages, [], spec, timeRatio * 30, 30, false);
      expect(evaluated.progressRatio).toBeCloseTo(target, 4);
    }
  });

  it("supports extreme 0.05x slow pace override with monotonic scheduling and accurate round-trip inversion", () => {
    const stageA: TimelineStage = {
      id: "stage-approach",
      name: "Approach",
      dayIndex: 1,
      stageOrder: 0,
      activityType: "cycling",
      color: "#38bdf8",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 100,
      movingSeconds: 1000,
      elapsedSeconds: 1000,
      hasTimestamps: true,
      points: [
        makePoint(0.1, 0.1, 0, 0, "2040-04-12T08:00:00Z"),
        makePoint(0.2, 0.1, 10_000, 1000, "2040-04-12T08:16:40Z"),
      ],
    };
    const stageB: TimelineStage = {
      id: "stage-ultra-slow",
      name: "Old Town Walk",
      dayIndex: 1,
      stageOrder: 1,
      activityType: "walking",
      color: "#14b8a6",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 20,
      movingSeconds: 1000,
      elapsedSeconds: 1000,
      hasTimestamps: true,
      points: [
        {
          ...makePoint(0.2, 0.1, 0, 0, "2040-04-12T09:00:00Z"),
          globalDistM: 10_000,
          globalElapsedS: 1000,
        },
        {
          ...makePoint(0.25, 0.12, 10_000, 1000, "2040-04-12T09:16:40Z"),
          globalDistM: 20_000,
          globalElapsedS: 2000,
        },
      ],
    };
    const stageC: TimelineStage = {
      id: "stage-exit",
      name: "Exit Ride",
      dayIndex: 1,
      stageOrder: 2,
      activityType: "cycling",
      color: "#f59e0b",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 80,
      movingSeconds: 1000,
      elapsedSeconds: 1000,
      hasTimestamps: true,
      points: [
        {
          ...makePoint(0.25, 0.12, 0, 0, "2040-04-12T10:00:00Z"),
          globalDistM: 20_000,
          globalElapsedS: 2000,
        },
        {
          ...makePoint(0.35, 0.12, 10_000, 1000, "2040-04-12T10:16:40Z"),
          globalDistM: 30_000,
          globalElapsedS: 3000,
        },
      ],
    };
    const stages = [stageA, stageB, stageC];

    const spec = normalizeRenderSpecV2({
      duration_seconds: 60,
      intro_hold_seconds: 1,
      outro_hold_seconds: 1,
      stage_camera_overrides: {
        "stage-ultra-slow": {
          focus_mode: "custom",
          target_zoom: 16.0,
          speed_multiplier: 0.05,
          transition_margin: 0.02,
        },
        "stage-approach": {
          focus_mode: "custom",
          target_zoom: null,
          speed_multiplier: 0.01, // clamped to 0.05
          transition_margin: 0.02,
        },
      },
    });

    expect(spec.stage_camera_overrides["stage-ultra-slow"]?.speed_multiplier).toBe(0.05);
    expect(spec.stage_camera_overrides["stage-approach"]?.speed_multiplier).toBe(0.05);

    const singleSlowSpec = normalizeRenderSpecV2({
      duration_seconds: 60,
      intro_hold_seconds: 1,
      outro_hold_seconds: 1,
      stage_camera_overrides: {
        "stage-ultra-slow": {
          focus_mode: "custom",
          target_zoom: 16.0,
          speed_multiplier: 0.05,
          transition_margin: 0.02,
        },
      },
    });

    expect(sampleCombinedSpeedMultiplier(stages, singleSlowSpec, 0.5)).toBeCloseTo(0.05, 6);

    // Keyframes up to 4.0x remain uncapped at 3.0x when no stage override limits them,
    // while per-stage overrides remain capped at 3.0x.
    const keyframe4xSpec = normalizeRenderSpecV2({
      camera_keyframes: [
        {
          progress: 0.5,
          camera_mode: "follow-marker",
          camera_preset: "dynamic-3d",
          pitch_deg: 45,
          bearing_offset_deg: 0,
          zoom_offset: 0,
          speed_multiplier: 4.0,
          label: "Fast",
        },
      ],
    });
    expect(sampleCombinedSpeedMultiplier(stages, keyframe4xSpec, 0.5)).toBeCloseTo(4.0, 6);

    const stageCappedSpec = normalizeRenderSpecV2({
      stage_camera_overrides: {
        "stage-ultra-slow": {
          focus_mode: "custom",
          target_zoom: 15.0,
          speed_multiplier: 3.5,
          transition_margin: 0.02,
        },
      },
    });
    expect(sampleCombinedSpeedMultiplier(stages, stageCappedSpec, 0.5)).toBeCloseTo(3.0, 6);

    const tEnterB = progressRatioToTimeRatio([], singleSlowSpec, 1 / 3, 60, false, stages) * 60;
    const tExitB = progressRatioToTimeRatio([], singleSlowSpec, 2 / 3, 60, false, stages) * 60;
    const durA = tEnterB - 1;
    const durB = tExitB - tEnterB;
    const durC = 59 - tExitB;
    expect(durB).toBeGreaterThan(durA * 12);
    expect(durC).toBeCloseTo(durA, 1);

    let prevProgress = -1;
    for (let t = 0; t <= 60; t += 0.5) {
      const res = evaluateTimelineSchedule(stages, [], singleSlowSpec, t, 60, false);
      expect(res.progressRatio).toBeGreaterThanOrEqual(prevProgress - 1e-9);
      prevProgress = res.progressRatio;
    }

    for (const target of [0.05, 0.2, 0.32, 0.3333, 0.35, 0.5, 0.66, 0.6667, 0.8, 0.95]) {
      const timeRatio = progressRatioToTimeRatio([], singleSlowSpec, target, 60, false, stages);
      const evaluated = evaluateTimelineSchedule(
        stages,
        [],
        singleSlowSpec,
        timeRatio * 60,
        60,
        false,
      );
      expect(evaluated.progressRatio).toBeCloseTo(target, 4);
    }
  });
});
