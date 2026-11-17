// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import { describe, expect, it } from "vitest";
import {
  type ClimbHighlightOut,
  createDefaultRenderSpecV2,
  type RenderManifest,
  type StageOut,
  type TelemetryChartPoint,
} from "../lib/api/types";
import {
  buildTelemetryPlotData,
  buildTelemetryPlotOptions,
} from "../lib/charts/telemetry-options";
import { circularMeanBearingDeg, computeCameraFrame } from "../lib/geo/camera";
import { generateDirectorKeyframes } from "../lib/geo/director";
import { formatDistance, formatElapsedTime, formatElevation, formatSpeed } from "../lib/geo/format";
import {
  buildDeckOverlayProps,
  resolveMapStyleUrl,
  sanitizeMapStyle,
} from "../lib/geo/map-config";
import {
  buildTimelineFromManifest,
  buildTimelineFromProject,
  computeAdaptiveFollowCamera,
  computeColorLegendScale,
  computeFollowZoom,
  detectTransferArcs,
  evaluateTimeline,
  frameIndexToProgress,
  mapClimbsToTimelineProgress,
  sampleTelemetryReadoutAtProgress,
  smoothTelemetryChartPoints,
  type TimelineStage,
} from "../lib/geo/timeline";

function makeSampleManifest(): RenderManifest {
  const spec = createDefaultRenderSpecV2();
  spec.duration_seconds = 10;
  spec.intro_hold_seconds = 1;
  spec.outro_hold_seconds = 1;
  spec.trail_duration_seconds = 2;
  spec.show_overnight_stops = true;
  spec.stage_colors = { "s-1": "#38bdf8", "s-2": "#14b8a6" };
  spec.stage_transitions = { "s-1": "tent", "s-2": "bed" };

  return {
    job_id: "job-1",
    project_id: "proj-1",
    project_title: "Fictional Two-Day Journey",
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
        color: "#38bdf8",
        transition: "tent",
        distance_m: 1000,
        elevation_gain_m: 100,
        moving_seconds: 100,
        elapsed_seconds: 100,
        has_timestamps: true,
        points: [
          {
            lat: 0.1,
            lon: 0.2,
            lon_unwrapped: 0.2,
            elev_m: 1000,
            cum_dist_m: 0,
            speed_mps: 10,
            grade_pct: 0,
            elapsed_s: 0,
            ts_utc: "2040-04-12T08:00:00Z",
            segment_idx: 0,
          },
          {
            lat: 0.12,
            lon: 0.24,
            lon_unwrapped: 0.24,
            elev_m: 1100,
            cum_dist_m: 1000,
            speed_mps: 10,
            grade_pct: 10,
            elapsed_s: 100,
            ts_utc: "2040-04-12T08:01:40Z",
            segment_idx: 0,
          },
        ],
      },
      {
        id: "s-2",
        name: "Day 2",
        day_index: 2,
        stage_order: 1,
        color: "#14b8a6",
        transition: "bed",
        distance_m: 1000,
        elevation_gain_m: 200,
        moving_seconds: 200,
        elapsed_seconds: 200,
        has_timestamps: true,
        points: [
          {
            lat: -0.1,
            lon: 0.4,
            lon_unwrapped: 0.4,
            elev_m: 1100,
            cum_dist_m: 0,
            speed_mps: 5,
            grade_pct: 20,
            elapsed_s: 0,
            ts_utc: "2040-04-13T08:00:00Z",
            segment_idx: 0,
          },
          {
            lat: -0.08,
            lon: 0.45,
            lon_unwrapped: 0.45,
            elev_m: 1300,
            cum_dist_m: 1000,
            speed_mps: 5,
            grade_pct: 20,
            elapsed_s: 200,
            ts_utc: "2040-04-13T08:03:20Z",
            segment_idx: 0,
          },
        ],
      },
    ],
  };
}

describe("Timeline math and formatting", () => {
  it("maps frames respecting intro and outro holds and proportional 5s preview", () => {
    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 10;
    spec.intro_hold_seconds = 1;
    spec.outro_hold_seconds = 1;

    // 101 frames -> 0.1s per frame
    expect(frameIndexToProgress(0, 101, spec, 10, false)).toBe(0);
    expect(frameIndexToProgress(10, 101, spec, 10, false)).toBe(0);
    expect(frameIndexToProgress(50, 101, spec, 10, false)).toBeCloseTo(0.5, 4);
    expect(frameIndexToProgress(90, 101, spec, 10, false)).toBe(1);
    expect(frameIndexToProgress(100, 101, spec, 10, false)).toBe(1);

    // 5s preview scales 1s intro/outro holds proportionally to 0.5s each
    expect(frameIndexToProgress(5, 51, spec, 5, true)).toBeCloseTo(0, 4);
    expect(frameIndexToProgress(25, 51, spec, 5, true)).toBeCloseTo(0.5, 4);
    expect(frameIndexToProgress(45, 51, spec, 5, true)).toBeCloseTo(1, 4);
  });

  it("evaluates distance vs recorded-time timeline modes and overnight stops", () => {
    const manifest = makeSampleManifest();
    const stages = buildTimelineFromManifest(manifest);

    // At 50% distance (1000m / 2000m), we are at end of Day 1 / start of Day 2
    const distMid = evaluateTimeline(stages, { ...manifest.spec, timeline_mode: "distance" }, 0.5);
    expect(distMid.distanceTravelledM).toBeCloseTo(1000, 1);
    expect(distMid.distanceRemainingM).toBeCloseTo(1000, 1);

    // In recorded-time mode (total 300s: Day 1 = 100s, Day 2 = 200s), 50% time = 150s -> 25% into Day 2 (1250m)
    const timeMid = evaluateTimeline(
      stages,
      { ...manifest.spec, timeline_mode: "recorded-time" },
      0.5,
    );
    expect(timeMid.activeStage?.id).toBe("s-2");
    expect(timeMid.distanceTravelledM).toBeCloseTo(1250, 1);
    expect(timeMid.overnightStops.length).toBe(2);
    expect(timeMid.overnightStops[0]?.reached).toBe(true);
    expect(timeMid.overnightStops[1]?.reached).toBe(false);
  });

  it("handles full, traversed, and trail visibility modes", () => {
    const manifest = makeSampleManifest();
    const stages = buildTimelineFromManifest(manifest);

    const fullState = evaluateTimeline(
      stages,
      { ...manifest.spec, visibility_mode: "full" },
      0.2,
    );
    expect(fullState.visiblePaths.length).toBe(2);

    const traversedEarly = evaluateTimeline(
      stages,
      { ...manifest.spec, visibility_mode: "traversed" },
      0.25,
    );
    expect(traversedEarly.visiblePaths.length).toBe(1);
    expect(traversedEarly.visiblePaths[0]?.stageId).toBe("s-1");

    const trailMid = evaluateTimeline(
      stages,
      { ...manifest.spec, visibility_mode: "trail", trail_duration_seconds: 2 },
      0.8,
      { durationS: 10 },
    );
    // Trail window is [0.6, 0.8] -> strictly inside stage s-2
    expect(trailMid.visiblePaths.length).toBe(1);
    expect(trailMid.visiblePaths[0]?.stageId).toBe("s-2");
  });

  it("computes follow zoom and formats metric/imperial units cleanly", () => {
    expect(computeFollowZoom([0.2, 0.1, 0.45, -0.08])).toBeGreaterThanOrEqual(11.0);
    expect(formatDistance(10_000, "metric")).toBe("10.0 km");
    expect(formatDistance(1609.344, "imperial")).toBe("1.0 mi");
    expect(formatElevation(1000, "metric")).toBe("1000 m");
    expect(formatElevation(100, "imperial")).toBe("328 ft");
    expect(formatSpeed(10, "metric")).toBe("36.0 km/h");
    expect(formatElapsedTime(3661)).toBe("01:01:01");
  });

  it("falls back cleanly to distance-based progress when points have null elapsed_s", () => {
    const base = makeSampleManifest();
    const manifest = {
      ...base,
      spec: {
        ...base.spec,
        timeline_mode: "recorded-time" as const,
        intro_hold_seconds: 0,
        outro_hold_seconds: 0,
      },
      stages: base.stages.map((stage, idx) =>
        idx === 1
          ? {
              ...stage,
              has_timestamps: false,
              points: stage.points.map((pt) => ({ ...pt, elapsed_s: null })),
            }
          : stage,
      ),
    };

    const stages = buildTimelineFromManifest(manifest);
    expect(stages[1]?.hasTimestamps).toBe(false);
    expect(stages[1]?.elapsedSeconds).toBeGreaterThan(0);

    const mid = evaluateTimeline(stages, manifest.spec, 0.5, { durationS: 10 });
    expect(mid.activeStageIndex).toBe(1);
    expect(Number.isFinite(mid.elapsedSeconds)).toBe(true);
    expect(mid.distanceTravelledM).toBeCloseTo(1000, 1);
  });

  it("never synthesizes a 2-point straight-line bbox fallback when stage geometry is unloaded", () => {
    const spec = createDefaultRenderSpecV2();
    spec.visibility_mode = "full";
    const stages: StageOut[] = [
      {
        id: "stage-loaded",
        project_id: "proj-1",
        source_type: "gpx",
        activity_type: "cycling",
        external_source_id: null,
        strava_activity_id: null,
        name: "Loaded Stage",
        stage_order: 0,
        day_index: 1,
        trim_start_s: null,
        trim_end_s: null,
        started_at: null,
        point_count: 3,
        distance_m: 1500,
        elevation_gain_m: 100,
        elevation_loss_m: 20,
        moving_seconds: 300,
        elapsed_seconds: 300,
        has_timestamps: false,
        min_lat: 0.2173,
        max_lat: 0.2537,
        min_lon: 0.4512,
        max_lon: 0.5325,
        warnings: [],
        created_at: "2001-06-14T08:00:00Z",
      },
      {
        id: "stage-unloaded",
        project_id: "proj-1",
        source_type: "strava",
        activity_type: "gravel_mtb",
        external_source_id: "42",
        strava_activity_id: 42,
        name: "Unloaded Stage",
        stage_order: 1,
        day_index: 2,
        trim_start_s: null,
        trim_end_s: null,
        started_at: null,
        point_count: 500,
        distance_m: 25_000,
        elevation_gain_m: 600,
        elevation_loss_m: 500,
        moving_seconds: 3600,
        elapsed_seconds: 3800,
        has_timestamps: false,
        min_lat: 0.2537,
        max_lat: 0.3773,
        min_lon: 0.5325,
        max_lon: 0.7,
        warnings: [],
        created_at: "2001-06-15T08:00:00Z",
      },
    ];

    const timeline = buildTimelineFromProject(
      stages,
      [
        {
          type: "Feature",
          id: "stage-loaded",
          geometry: {
            type: "MultiLineString",
            coordinates: [[[0.4512, 0.2173], [0.5048, 0.2352], [0.5325, 0.2537]]],
          },
          properties: {
            lod: "medium",
            point_count: 3,
            original_point_count: 3,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5325, 0.2537],
            crosses_antimeridian: false,
            stage_id: "stage-loaded",
            name: "Loaded Stage",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 1500,
            elevation_gain_m: 100,
          },
        },
      ],
      undefined,
      spec,
    );

    expect(timeline[0]?.points.length).toBe(3);
    expect(timeline[1]?.points.length).toBe(0);
    expect(timeline[1]?.activityType).toBe("gravel_mtb");

    const evaluated = evaluateTimeline(timeline, spec, 1);
    expect(evaluated.visiblePaths.every((p) => p.stageId === "stage-loaded")).toBe(true);
  });

  it("prefers fresh stage.activity_type over stale cached feature.properties.activity_type", () => {
    const spec = createDefaultRenderSpecV2();
    const stage: StageOut = {
      id: "stage-1",
      project_id: "proj-1",
      source_type: "gpx",
      activity_type: "hiking",
      external_source_id: null,
      strava_activity_id: null,
      name: "Fictional Ridge",
      stage_order: 0,
      day_index: 1,
      trim_start_s: null,
      trim_end_s: null,
      started_at: null,
      point_count: 2,
      distance_m: 1000,
      elevation_gain_m: 150,
      elevation_loss_m: 50,
      moving_seconds: 600,
      elapsed_seconds: 600,
      has_timestamps: false,
      min_lat: 0.2173,
      max_lat: 0.2352,
      min_lon: 0.4512,
      max_lon: 0.5048,
      warnings: [],
      created_at: "2001-06-14T08:00:00Z",
    };

    const timeline = buildTimelineFromProject(
      [stage],
      [
        {
          type: "Feature",
          id: "stage-1",
          geometry: {
            type: "MultiLineString",
            coordinates: [[[0.4512, 0.2173], [0.5048, 0.2352]]],
          },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 2,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5048, 0.2352],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Fictional Ridge",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 1000,
            elevation_gain_m: 150,
          },
        },
      ],
      undefined,
      spec,
    );

    expect(timeline[0]?.activityType).toBe("hiking");
  });

  it("computes deterministic camera frames for flat, dynamic-3d, and drone-orbit presets", () => {
    const manifest = makeSampleManifest();
    const stages = buildTimelineFromManifest(manifest);
    const evaluated = evaluateTimeline(stages, manifest.spec, 0.5);

    const flat = computeCameraFrame({ ...manifest.spec, camera_preset: "flat" }, evaluated);
    expect(flat.pitch).toBe(0);
    expect(flat.bearing).toBe(0);

    const dynamic3d = computeCameraFrame(
      { ...manifest.spec, camera_preset: "dynamic-3d", camera_pitch_deg: 20, camera_bearing_deg: 0 },
      evaluated,
    );
    expect(dynamic3d.pitch).toBeGreaterThanOrEqual(45);
    expect(dynamic3d.bearing).toBeGreaterThan(0);

    const orbitStart = computeCameraFrame(
      { ...manifest.spec, camera_preset: "drone-orbit", camera_bearing_deg: 10 },
      evaluateTimeline(stages, manifest.spec, 0),
    );
    const orbitEnd = computeCameraFrame(
      { ...manifest.spec, camera_preset: "drone-orbit", camera_bearing_deg: 10 },
      evaluateTimeline(stages, manifest.spec, 1),
    );
    expect(orbitStart.pitch).toBeGreaterThanOrEqual(50);
    expect(orbitStart.bearing).toBeCloseTo(10, 4);
    expect(orbitEnd.bearing).toBeCloseTo(130, 4);
  });

  it("adapts follow-marker zoom below 11.0 for 21-day multi-week trips and keeps circular bearing wrap smooth", () => {
    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 60;
    spec.intro_hold_seconds = 1;
    spec.outro_hold_seconds = 1.5;
    spec.follow_zoom_mode = "adaptive";
    spec.target_screen_px_per_sec = 180;

    // Build a 21-day ~2,100 km tour (100 km per day moving East across ~27 deg lon)
    const twentyOneDays: TimelineStage[] = Array.from({ length: 21 }, (_, dayIdx) => {
      const startLon = 2.0 + dayIdx * 1.3;
      const endLon = startLon + 1.3;
      const globalStartDist = dayIdx * 100_000;
      const globalStartTime = dayIdx * 14_400;
      return {
        id: `day-${dayIdx + 1}`,
        name: `Day ${dayIdx + 1}`,
        dayIndex: dayIdx + 1,
        stageOrder: dayIdx,
        activityType: "cycling",
        color: "#38bdf8",
        transition: "bed",
        distanceM: 100_000,
        elevationGainM: 1200,
        movingSeconds: 14_400,
        elapsedSeconds: 14_400,
        hasTimestamps: true,
        points: [
          {
            lat: 0.2173,
            lon: startLon,
            lonUnwrapped: startLon,
            elevM: 800,
            cumDistM: 0,
            globalDistM: globalStartDist,
            speedMps: 7,
            gradePct: 1,
            elapsedS: 0,
            globalElapsedS: globalStartTime,
            tsUtcMs: 1_750_000_000_000 + globalStartTime * 1000,
            segmentIdx: 0,
          },
          {
            lat: 0.2537,
            lon: endLon,
            lonUnwrapped: endLon,
            elevM: 1200,
            cumDistM: 100_000,
            globalDistM: globalStartDist + 100_000,
            speedMps: 7,
            gradePct: 2,
            elapsedS: 14_400,
            globalElapsedS: globalStartTime + 14_400,
            tsUtcMs: 1_750_000_000_000 + (globalStartTime + 14_400) * 1000,
            segmentIdx: 0,
          },
        ],
      };
    });

    const adaptive60 = computeAdaptiveFollowCamera(twentyOneDays, spec, 0.5, {
      viewportWidth: 1920,
      viewportHeight: 1080,
      durationS: 60,
    });
    expect(adaptive60.zoom).toBeLessThan(11.0);
    expect(adaptive60.zoom).toBeGreaterThan(6.5);
    expect(adaptive60.zoom).toBeLessThan(10.0);

    // Increasing duration to 180s allows closer zoom while staying comfortable
    const adaptive180 = computeAdaptiveFollowCamera(
      twentyOneDays,
      { ...spec, duration_seconds: 180 },
      0.5,
      { viewportWidth: 1920, viewportHeight: 1080, durationS: 180 },
    );
    expect(adaptive180.zoom).toBeGreaterThan(adaptive60.zoom);
    expect(adaptive180.zoom).toBeLessThan(11.0);

    // Fixed mode falls back to legacy >= 11.0 floor
    const fixedMode = computeAdaptiveFollowCamera(
      twentyOneDays,
      { ...spec, follow_zoom_mode: "fixed" },
      0.5,
    );
    expect(fixedMode.zoom).toBeGreaterThanOrEqual(11.0);

    // Circular mean handles 355 -> 5 deg wrap smoothly (0 deg, not 180 deg)
    const meanWrap = circularMeanBearingDeg([
      { deg: 355, weight: 1 },
      { deg: 5, weight: 1 },
    ]);
    expect(meanWrap).not.toBeNull();
    expect(meanWrap! < 1 || meanWrap! > 359).toBe(true);
  });

  it("evaluates color_by quantized segments, >2km transfer arcs, chapter cards, climb mapping, and unified telemetry readout", () => {
    const manifest = makeSampleManifest();
    // Day 1 ends at (0.5325, 0.2352), Day 2 starts at (0.6800, 0.3337) -> > 9km transfer gap
    manifest.stages[1]!.points[0]!.lon = 0.7113;
    manifest.stages[1]!.points[0]!.lon_unwrapped = 0.7113;
    manifest.stages[1]!.points[0]!.lat = 0.3337;
    manifest.stages[0]!.points[0]!.hr_bpm = 132;
    manifest.stages[0]!.points[0]!.watts = 190;
    manifest.stages[0]!.points[1]!.hr_bpm = 168;
    manifest.stages[0]!.points[1]!.watts = 310;
    manifest.stages[1]!.points[0]!.hr_bpm = 150;
    manifest.stages[1]!.points[0]!.watts = 250;
    manifest.stages[1]!.points[1]!.hr_bpm = 180;
    manifest.stages[1]!.points[1]!.watts = 370;

    const stages = buildTimelineFromManifest(manifest);
    const arcs = detectTransferArcs(stages);
    expect(arcs).toHaveLength(1);
    expect(arcs[0]?.distanceM).toBeGreaterThan(2000);

    const spec = {
      ...manifest.spec,
      color_by: "grade" as const,
      show_transfer_arcs: true,
      show_chapter_cards: true,
      elevation_curtain: true,
      telemetry_smoothing: 3,
    };

    const evaluatedStart = evaluateTimeline(stages, spec, 0.01, { durationS: 20 });
    expect(evaluatedStart.chapterCard).not.toBeNull();
    expect(evaluatedStart.chapterCard?.dayIndex).toBe(1);
    expect(evaluatedStart.colorLegendStops?.length ?? 0).toBeGreaterThanOrEqual(5);

    const evaluatedLate = evaluateTimeline(stages, spec, 0.75, { durationS: 20 });
    expect(evaluatedLate.visiblePaths.length).toBeGreaterThanOrEqual(2);
    expect(evaluatedLate.transferArcs?.[0]?.reached).toBe(true);
    expect(evaluatedLate.hrBpm).not.toBeNull();
    expect(evaluatedLate.watts).not.toBeNull();
    expect(evaluatedLate.vamMPerH).not.toBeNull();

    expect(computeColorLegendScale("stage")).toEqual([]);
    expect(computeColorLegendScale("watts")).toHaveLength(5);

    const climbs: ClimbHighlightOut[] = [
      {
        id: "climb-1",
        stage_id: "s-2",
        name: "Fictional Ridge Climb",
        category: "Cat 1",
        start_dist_m: 100,
        end_dist_m: 900,
        start_progress: 0.1,
        end_progress: 0.9,
        summit_progress: 0.9,
        summit_lat: -0.08,
        summit_lon: 0.45,
        summit_elev_m: 1300,
        distance_m: 800,
        elevation_gain_m: 180,
        avg_grade_pct: 12,
        max_grade_pct: 18,
        fiets_score: 4.5,
      },
    ];
    const mappedClimbs = mapClimbsToTimelineProgress(climbs, stages, spec, 0.98);
    expect(mappedClimbs).toHaveLength(1);
    expect(mappedClimbs[0]?.summitProgress).toBeGreaterThan(0.5);
    expect(mappedClimbs[0]?.reached).toBe(true);

    const rawChart: TelemetryChartPoint[] = [
      { cum_dist_m: 0, elev_m: 1000, speed_mps: 10, grade_pct: 0, hr_bpm: 130, watts: 200 },
      { cum_dist_m: 500, elev_m: 1200, speed_mps: 6, grade_pct: 10, hr_bpm: 160, watts: 320 },
      { cum_dist_m: 1000, elev_m: 1100, speed_mps: 8, grade_pct: -2, hr_bpm: 140, watts: 230 },
    ];
    const smoothed = smoothTelemetryChartPoints(rawChart, 3);
    expect(smoothed[1]?.elev_m).toBeCloseTo(1100, 4);
    expect(smoothed[1]?.watts).toBeCloseTo(250, 4);

    const readout = sampleTelemetryReadoutAtProgress(stages, spec, 0.25, rawChart);
    expect(readout.hrBpm).not.toBeNull();
    expect(readout.watts).not.toBeNull();
  });

  it("generates deterministic Director presets, v2.1 map styles, Deck.gl layers, and uPlot HR/Watts options", () => {
    const manifest = makeSampleManifest();
    const stages = buildTimelineFromManifest(manifest);
    const climbs: ClimbHighlightOut[] = [
      {
        id: "c1",
        stage_id: "s-1",
        name: "Fictional Hill",
        category: "HC",
        start_dist_m: 200,
        end_dist_m: 800,
        start_progress: 0.2,
        end_progress: 0.8,
        summit_progress: 0.8,
        summit_lat: 0.12,
        summit_lon: 0.24,
        summit_elev_m: 1100,
        distance_m: 600,
        elevation_gain_m: 90,
        avg_grade_pct: 10,
        max_grade_pct: 14,
        fiets_score: 6.2,
      },
    ];

    expect(generateDirectorKeyframes("none", { stages })).toEqual([]);
    const doc = generateDirectorKeyframes("expedition-doc", { stages, spec: manifest.spec, climbs });
    const spotlight = generateDirectorKeyframes("climb-spotlight", { stages, spec: manifest.spec, climbs });
    const reel = generateDirectorKeyframes("social-reel", { stages, spec: manifest.spec, climbs });
    expect(doc.length).toBeGreaterThanOrEqual(4);
    expect(spotlight.some((kf) => kf.speed_multiplier >= 2)).toBe(true);
    expect(reel.length).toBeGreaterThanOrEqual(4);
    expect(doc).toEqual(generateDirectorKeyframes("expedition-doc", { stages, spec: manifest.spec, climbs }));

    // Map styles
    expect(resolveMapStyleUrl("topo-relief")).toContain("openfreemap");
    expect(resolveMapStyleUrl("satellite-hybrid")).toContain("openfreemap");
    expect(resolveMapStyleUrl("editorial-mono")).toContain("openfreemap");

    const baseStyle = {
      version: 8 as const,
      sources: {},
      layers: [{ id: "bg", type: "background" as const, paint: { "background-color": "#000000" } }],
    };
    const topo = sanitizeMapStyle(baseStyle, "topo-relief");
    expect(topo.layers.some((l) => l.id === "trip-topo-hillshade")).toBe(true);
    const sat = sanitizeMapStyle(baseStyle, "satellite-hybrid");
    expect(sat.layers.some((l) => l.id === "trip-satellite-imagery")).toBe(true);
    const mono = sanitizeMapStyle(baseStyle, "editorial-mono");
    expect((mono.layers[0]?.paint as Record<string, unknown>)["background-color"]).toBe("#f8fafc");

    // Deck.gl layers with elevation curtain + transfer arcs + climb pins
    const evaluated = evaluateTimeline(stages, manifest.spec, 0.8);
    const overlay = buildDeckOverlayProps(evaluated.visiblePaths, evaluated.markerPosition, {
      elevationCurtain: true,
      terrainExaggeration: 1.8,
      elevationStages: stages,
      transferArcs: [
        {
          id: "arc-1",
          fromStageId: "s-1",
          toStageId: "s-2",
          fromPosition: [0.24, 0.12],
          toPosition: [0.9, -0.2],
          distanceM: 9000,
          reached: true,
          active: false,
          waypoints: [
            [0.24, 0.12],
            [0.9, -0.2],
          ],
        },
      ],
      climbPins: mapClimbsToTimelineProgress(climbs, stages, manifest.spec, 0.9),
    });
    const layerIds = (overlay.layers ?? []).map((l) => (l as { id?: string }).id);
    expect(layerIds).toContain("trip-elevation-curtain");
    expect(layerIds).toContain("trip-elevation-ridge");
    expect(layerIds).toContain("trip-transfer-arcs");
    expect(layerIds).toContain("trip-climb-pins");

    // uPlot HR & Watts + brush + bands
    const plotData = buildTelemetryPlotData(
      [
        { cum_dist_m: 0, elev_m: 100, speed_mps: 5, grade_pct: 2, hr_bpm: 135, watts: 210 },
        { cum_dist_m: 1000, elev_m: 150, speed_mps: 6, grade_pct: 5, hr_bpm: 155, watts: 260 },
      ],
      "metric",
      { smoothingWindow: 1 },
    );
    expect(plotData).toHaveLength(6);
    expect(plotData[4]).toEqual([135, 155]);
    expect(plotData[5]).toEqual([210, 260]);

    const plotOpts = buildTelemetryPlotOptions(640, 120, {
      units: "metric",
      visibility: { elevation: true, speed: true, grade: false, hr: true, watts: true },
      enableDragBrush: true,
      dayDividers: [{ distM: 500, dayIndex: 2 }],
      climbBands: [{ id: "c1", name: "Alpe", startDistM: 200, endDistM: 800 }],
    });
    expect(plotOpts.series).toHaveLength(6);
    expect(plotOpts.series[4]?.show).toBe(true);
    expect(plotOpts.series[5]?.show).toBe(true);
    expect(plotOpts.cursor?.drag?.x).toBe(true);
  });
});
