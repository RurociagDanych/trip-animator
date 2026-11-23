import { act, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDefaultRenderSpecV2, type ClimbHighlightOut } from "../lib/api/types";
import { rectsIntersect, solveStageLayout, type Rect } from "../lib/geo/layout";
import { computeAdaptiveFollowCamera, evaluateTimeline, type TimelinePoint, type TimelineStage } from "../lib/geo/timeline";
import { TripStageCanvas, type TripStageCanvasHandle } from "../components/stage/TripStageCanvas";

interface JumpToCall {
  center?: [number, number] | { lng: number; lat: number };
  zoom?: number;
  pitch?: number;
  bearing?: number;
  padding?: { top: number; bottom: number; left: number; right: number };
}

const mockState = vi.hoisted(() => ({
  jumpToCalls: [] as JumpToCall[],
  cameraForBoundsZoom: 10.5,
  cameraForBoundsCenter: [0.55, 0.25] as [number, number],
  zoom: 10.5,
  center: [0.55, 0.25] as [number, number],
  pitch: 0,
  bearing: 0,
  loaded: true,
  areTilesLoaded: true,
  moveOnCount: 0,
  idleOnceCount: 0,
  emit: null as ((event: string) => void) | null,
}));

vi.mock("maplibre-gl", () => {
  class MockMap {
    private listeners = new Map<string, Set<(...args: unknown[]) => void>>();

    constructor() {
      mockState.emit = (event: string) => {
        for (const cb of [...(this.listeners.get(event) ?? [])]) {
          cb();
        }
      };
    }

    setStyle() {}
    addControl() {}
    removeControl() {}
    isStyleLoaded() {
      return true;
    }
    loaded() {
      return mockState.loaded;
    }
    areTilesLoaded() {
      return mockState.areTilesLoaded;
    }
    getStyle() {
      return { sources: {}, layers: [] };
    }
    getLayer() {
      return undefined;
    }
    addLayer() {}
    setLayoutProperty() {}
    getSource() {
      return undefined;
    }
    addSource() {}
    setTerrain() {}
    setSky() {}
    getContainer() {
      return { clientWidth: 1280, clientHeight: 720 };
    }
    setPadding() {}
    cameraForBounds() {
      return {
        center: mockState.cameraForBoundsCenter,
        zoom: mockState.cameraForBoundsZoom,
      };
    }
    fitBounds() {}
    jumpTo(opts: JumpToCall) {
      mockState.jumpToCalls.push(opts);
      if (opts.zoom !== undefined) mockState.zoom = opts.zoom;
      if (opts.pitch !== undefined) mockState.pitch = opts.pitch;
      if (opts.bearing !== undefined) mockState.bearing = opts.bearing;
      if (opts.center) {
        mockState.center = Array.isArray(opts.center)
          ? opts.center
          : [opts.center.lng, opts.center.lat];
      }
      mockState.emit?.("move");
    }
    getCenter() {
      return { lng: mockState.center[0], lat: mockState.center[1] };
    }
    getZoom() {
      return mockState.zoom;
    }
    getPitch() {
      return mockState.pitch;
    }
    getBearing() {
      return mockState.bearing;
    }
    triggerRepaint() {
      requestAnimationFrame(() => mockState.emit?.("render"));
    }
    project(lngLat: [number, number]) {
      return {
        x: 640 + (lngLat[0] - mockState.center[0]) * 500,
        y: 360 - (lngLat[1] - mockState.center[1]) * 500,
      };
    }
    on(event: string, cb: (...args: unknown[]) => void) {
      if (event === "move") {
        mockState.moveOnCount += 1;
      }
      if (!this.listeners.has(event)) {
        this.listeners.set(event, new Set());
      }
      this.listeners.get(event)!.add(cb);
    }
    off(event: string, cb: (...args: unknown[]) => void) {
      this.listeners.get(event)?.delete(cb);
    }
    once(event: string, cb: (...args: unknown[]) => void) {
      if (event === "idle") {
        mockState.idleOnceCount += 1;
      }
      const wrapped = (...args: unknown[]) => {
        this.off(event, wrapped);
        cb(...args);
      };
      this.on(event, wrapped);
    }
    resize() {}
    remove() {
      this.listeners.clear();
    }
  }

  return {
    default: { Map: MockMap },
    Map: MockMap,
  };
});

vi.mock("@deck.gl/mapbox", () => {
  class MockMapboxOverlay {
    setProps() {}
    finalize() {}
  }
  return {
    MapboxOverlay: MockMapboxOverlay,
  };
});

function makePoint(
  lon: number,
  lat: number,
  cumDistM: number,
  elapsedS: number,
  gradePct = 4,
): TimelinePoint {
  return {
    lat,
    lon,
    lonUnwrapped: lon,
    elevM: 1200 + cumDistM * 0.04,
    cumDistM,
    globalDistM: cumDistM,
    speedMps: 5,
    gradePct,
    elapsedS,
    globalElapsedS: elapsedS,
    tsUtcMs: 1_750_000_000_000 + elapsedS * 1000,
    segmentIdx: 0,
  };
}

function makeStage(): TimelineStage {
  return {
    id: "stage-canvas-1",
    name: "Fictional Grid Stage",
    dayIndex: 1,
    stageOrder: 0,
    activityType: "cycling",
    color: "#38bdf8",
    transition: "none",
    distanceM: 20_000,
    elevationGainM: 800,
    movingSeconds: 3600,
    elapsedSeconds: 3600,
    hasTimestamps: true,
    points: [
      makePoint(0.45, 0.22, 0, 0, 2),
      makePoint(0.55, 0.25, 10_000, 1800, 7),
      makePoint(0.65, 0.28, 20_000, 3600, 11),
    ],
  };
}

function makeClimbs(): ClimbHighlightOut[] {
  return [
    {
      id: "climb-1",
      stage_id: "stage-canvas-1",
      name: "Fictional Col Alpha",
      category: "Cat 1",
      start_dist_m: 17000,
      end_dist_m: 18500,
      start_progress: 0.85,
      end_progress: 0.925,
      summit_progress: 0.9,
      summit_lat: 0.274,
      summit_lon: 0.632,
      summit_elev_m: 1560,
      distance_m: 1500,
      elevation_gain_m: 180,
      avg_grade_pct: 6.0,
      max_grade_pct: 10.5,
      fiets_score: 5.5,
    },
    {
      id: "climb-2",
      stage_id: "stage-canvas-1",
      name: "Fictional Col Beta",
      category: "HC",
      start_dist_m: 18600,
      end_dist_m: 19600,
      start_progress: 0.93,
      end_progress: 0.98,
      summit_progress: 0.94,
      summit_lat: 0.278,
      summit_lon: 0.644,
      summit_elev_m: 1920,
      distance_m: 1000,
      elevation_gain_m: 140,
      avg_grade_pct: 8.0,
      max_grade_pct: 13.0,
      fiets_score: 8.4,
    },
  ];
}

function parsePxRect(el: HTMLElement): Rect {
  return {
    x: parseFloat(el.style.left),
    y: parseFloat(el.style.top),
    width: parseFloat(el.style.width),
    height: parseFloat(el.style.height),
  };
}

describe("TripStageCanvas WebGL wiring (canvas.test.tsx)", () => {
  const originalWebGL2 = globalThis.WebGL2RenderingContext;

  beforeEach(() => {
    mockState.jumpToCalls = [];
    mockState.cameraForBoundsZoom = 10.5;
    mockState.cameraForBoundsCenter = [0.55, 0.25];
    mockState.zoom = 10.5;
    mockState.center = [0.55, 0.25];
    mockState.pitch = 0;
    mockState.bearing = 0;
    mockState.loaded = true;
    mockState.areTilesLoaded = true;
    mockState.moveOnCount = 0;
    mockState.idleOnceCount = 0;
    mockState.emit = null;

    globalThis.WebGL2RenderingContext = class {} as typeof WebGL2RenderingContext;
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(((contextId: string) => {
      if (contextId === "webgl2" || contextId === "webgl" || contextId === "experimental-webgl") {
        return {} as WebGL2RenderingContext;
      }
      return null;
    }) as typeof HTMLCanvasElement.prototype.getContext);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    if (originalWebGL2 === undefined) {
      delete (globalThis as { WebGL2RenderingContext?: unknown }).WebGL2RenderingContext;
    } else {
      globalThis.WebGL2RenderingContext = originalWebGL2;
    }
  });

  it("applies keyframe zoom_offset to data-follow-zoom and map.jumpTo in follow-marker and fit-track modes", () => {
    const stage = makeStage();
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "follow-marker";
    spec.follow_zoom_mode = "adaptive";
    spec.camera_keyframes = [
      {
        progress: 0.5,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 0,
        bearing_offset_deg: 0,
        zoom_offset: 2.0,
        speed_multiplier: 1.0,
        label: "Zoom In",
      },
    ];

    const baseAdaptive = computeAdaptiveFollowCamera([stage], spec, 0.5, {
      viewportWidth: 1280,
      viewportHeight: 720,
      durationS: 20,
      travelDurationS: 20,
      isPreview: false,
    });

    const { rerender } = render(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.5}
        durationS={20}
        travelDurationS={20}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    const frame = screen.getByTestId("deterministic-map-frame");
    const expectedFollowZoom = Math.min(18, Math.max(3, baseAdaptive.zoom + 2.0));
    expect(Number(frame.getAttribute("data-follow-zoom"))).toBeCloseTo(expectedFollowZoom, 3);
    const lastFollowJump = mockState.jumpToCalls[mockState.jumpToCalls.length - 1];
    expect(lastFollowJump?.zoom).toBeCloseTo(expectedFollowZoom, 3);

    mockState.jumpToCalls = [];
    const fitSpec = {
      ...spec,
      camera_mode: "fit-track" as const,
      camera_keyframes: [
        {
          ...spec.camera_keyframes[0]!,
          camera_mode: "fit-track" as const,
          zoom_offset: 1.5,
        },
      ],
    };

    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={fitSpec}
        progressRatio={0.5}
        durationS={20}
        travelDurationS={20}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    const fitJump = mockState.jumpToCalls.find((c) => c.zoom !== undefined);
    expect(fitJump?.zoom).toBeCloseTo(10.5 + 1.5, 3);
  });

  it("positions color-by-legend using layout.colorLegend.rect and avoids HUD overlap", () => {
    const stage = makeStage();
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "fit-track";
    spec.color_by = "grade";
    spec.statistics_panel.enabled = true;
    spec.statistics_panel.position = "top";

    const evaluated = evaluateTimeline([stage], spec, 0.5, { durationS: 20, isPreview: false });
    const expectedLayout = solveStageLayout({
      stages: [stage],
      spec,
      evaluated,
      viewportW: 1280,
      viewportH: 720,
      compact: false,
      isPreview: false,
      hasActivePhoto: false,
      travelDurationS: 20,
    });

    render(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.5}
        durationS={20}
        travelDurationS={20}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    const legend = screen.getByTestId("color-by-legend");
    expect(legend.style.left).toBe(`${expectedLayout.colorLegend!.rect.x}px`);
    expect(legend.style.top).toBe(`${expectedLayout.colorLegend!.rect.y}px`);
    expect(legend.style.maxWidth).toBe(`${expectedLayout.colorLegend!.rect.width}px`);

    expect(expectedLayout.hud).not.toBeNull();
    expect(rectsIntersect(expectedLayout.colorLegend!.rect, expectedLayout.hud!.rect)).toBe(false);
  });

  it("renders non-overlapping climb callouts and SVG leader lines in both follow-marker and fit-track modes, and hides them when disabled", () => {
    const stage = makeStage();
    const climbs = makeClimbs();
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "follow-marker";
    spec.show_climb_callouts = true;

    const { rerender } = render(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.95}
        durationS={20}
        travelDurationS={20}
        climbs={climbs}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    expect(screen.getByTestId("climb-callout-leaders")).toBeInTheDocument();
    const followLeader1 = screen.getByTestId("climb-leader-climb-1");
    const followLeader2 = screen.getByTestId("climb-leader-climb-2");
    expect(followLeader1.tagName.toLowerCase()).toBe("line");
    expect(followLeader2.tagName.toLowerCase()).toBe("line");

    const followPill1 = parsePxRect(screen.getByTestId("climb-callout-climb-1"));
    const followPill2 = parsePxRect(screen.getByTestId("climb-callout-climb-2"));
    expect(rectsIntersect(followPill1, followPill2)).toBe(false);

    const fitSpec = { ...spec, camera_mode: "fit-track" as const };
    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={fitSpec}
        progressRatio={0.95}
        durationS={20}
        travelDurationS={20}
        climbs={climbs}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    expect(screen.getByTestId("climb-callout-leaders")).toBeInTheDocument();
    expect(screen.getByTestId("climb-leader-climb-1")).toBeInTheDocument();
    expect(screen.getByTestId("climb-leader-climb-2")).toBeInTheDocument();
    const fitPill1 = parsePxRect(screen.getByTestId("climb-callout-climb-1"));
    const fitPill2 = parsePxRect(screen.getByTestId("climb-callout-climb-2"));
    expect(rectsIntersect(fitPill1, fitPill2)).toBe(false);

    const disabledSpec = { ...fitSpec, show_climb_callouts: false };
    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={disabledSpec}
        progressRatio={0.95}
        durationS={20}
        travelDurationS={20}
        climbs={climbs}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    expect(screen.queryByTestId("climb-callout-leaders")).not.toBeInTheDocument();
    expect(screen.queryByTestId("climb-callout-climb-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("climb-leader-climb-1")).not.toBeInTheDocument();
  });

  it("registers map.on('move') once across 10 frame updates and re-projects pins on manual move", () => {
    const stage: TimelineStage = {
      ...makeStage(),
      transition: "bed",
    };
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "fit-track";
    spec.show_overnight_stops = true;
    spec.visibility_mode = "full";

    const { rerender } = render(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.1}
        durationS={20}
        travelDurationS={20}
        renderMode="interactive"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    expect(mockState.moveOnCount).toBe(1);

    for (let i = 2; i <= 10; i++) {
      rerender(
        <TripStageCanvas
          stages={[stage]}
          spec={spec}
          progressRatio={i / 10}
          durationS={20}
          travelDurationS={20}
          renderMode="interactive"
          exactSize={{ width: 1280, height: 720 }}
        />,
      );
    }

    expect(mockState.moveOnCount).toBe(1);

    const stopPinBefore = screen.getByTestId("stop-marker-stage-canvas-1");
    const leftBefore = parseFloat(stopPinBefore.style.left);

    act(() => {
      mockState.center = [0.75, 0.25];
      mockState.emit?.("move");
    });

    const stopPinAfter = screen.getByTestId("stop-marker-stage-canvas-1");
    const leftAfter = parseFloat(stopPinAfter.style.left);
    expect(leftAfter).not.toBeCloseTo(leftBefore, 1);
  });

  it("waits for a rendered frame and only waits for idle when tiles are not loaded", async () => {
    const stage = makeStage();
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "follow-marker";
    const controllerRef = createRef<TripStageCanvasHandle | null>();

    render(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.5}
        durationS={20}
        travelDurationS={20}
        controllerRef={controllerRef}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    mockState.idleOnceCount = 0;
    mockState.loaded = true;
    mockState.areTilesLoaded = true;
    await controllerRef.current!.waitForFrameSettle(true);
    expect(mockState.idleOnceCount).toBe(0);

    mockState.areTilesLoaded = false;
    let settled = false;
    const settlePromise = controllerRef.current!.waitForFrameSettle(true).then(() => {
      settled = true;
    });
    await vi.waitFor(() => {
      expect(mockState.idleOnceCount).toBe(1);
    });
    expect(settled).toBe(false);
    mockState.emit?.("idle");
    await settlePromise;
    expect(settled).toBe(true);
  });

  it("smoothly blends street-level zoom and pan across stage boundaries in both follow-marker and fit-track modes without bearing jumps", () => {
    const stage1: TimelineStage = {
      id: "stage-focus-1",
      name: "Approach Stage",
      dayIndex: 1,
      stageOrder: 0,
      activityType: "cycling",
      color: "#38bdf8",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 150,
      movingSeconds: 1800,
      elapsedSeconds: 1800,
      hasTimestamps: true,
      points: [
        makePoint(0.45, 0.22, 0, 0, 2),
        makePoint(0.55, 0.25, 10_000, 1800, 3),
      ],
    };
    const stage2: TimelineStage = {
      id: "stage-focus-2",
      name: "City Walk Stage",
      dayIndex: 1,
      stageOrder: 1,
      activityType: "walking",
      color: "#14b8a6",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 40,
      movingSeconds: 1800,
      elapsedSeconds: 1800,
      hasTimestamps: true,
      points: [
        { ...makePoint(0.55, 0.25, 0, 0, 1), globalDistM: 10_000, globalElapsedS: 1800 },
        { ...makePoint(0.60, 0.26, 10_000, 1800, 1), globalDistM: 20_000, globalElapsedS: 3600 },
      ],
    };
    const stage3: TimelineStage = {
      id: "stage-focus-3",
      name: "Return Stage",
      dayIndex: 2,
      stageOrder: 2,
      activityType: "cycling",
      color: "#f59e0b",
      transition: "none",
      distanceM: 10_000,
      elevationGainM: 120,
      movingSeconds: 1800,
      elapsedSeconds: 1800,
      hasTimestamps: true,
      points: [
        { ...makePoint(0.60, 0.26, 0, 0, 2), globalDistM: 20_000, globalElapsedS: 3600 },
        { ...makePoint(0.70, 0.29, 10_000, 1800, 2), globalDistM: 30_000, globalElapsedS: 5400 },
      ],
    };
    const multiStages = [stage1, stage2, stage3];

    const followSpec = createDefaultRenderSpecV2();
    followSpec.camera_mode = "follow-marker";
    followSpec.camera_preset = "flat";
    followSpec.stage_camera_overrides = {
      "stage-focus-2": {
        focus_mode: "street",
        target_zoom: 15.8,
        speed_multiplier: 0.45,
        transition_margin: 0.02,
      },
    };

    const { rerender } = render(
      <TripStageCanvas
        stages={multiStages}
        spec={followSpec}
        progressRatio={0.15}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );

    const frame = screen.getByTestId("deterministic-map-frame");
    const zoomInStage1 = Number(frame.getAttribute("data-follow-zoom"));
    expect(zoomInStage1).toBeLessThan(14.5);

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={followSpec}
        progressRatio={1 / 3}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    const zoomAtBoundary = Number(frame.getAttribute("data-follow-zoom"));
    expect(zoomAtBoundary).toBeGreaterThan(zoomInStage1);
    expect(zoomAtBoundary).toBeLessThan(15.8);

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={followSpec}
        progressRatio={0.5}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    const zoomInStage2 = Number(frame.getAttribute("data-follow-zoom"));
    expect(zoomInStage2).toBeCloseTo(15.8, 3);

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={followSpec}
        progressRatio={0.85}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    const zoomInStage3 = Number(frame.getAttribute("data-follow-zoom"));
    expect(zoomInStage3).toBeLessThan(14.5);

    for (const call of mockState.jumpToCalls) {
      if (call.bearing !== undefined) {
        expect(call.bearing).toBe(0);
      }
    }

    mockState.jumpToCalls = [];
    const fitSpec = { ...followSpec, camera_mode: "fit-track" as const };

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={fitSpec}
        progressRatio={0.15}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    expect(frame.getAttribute("data-follow-zoom")).toBeNull();
    expect(mockState.zoom).toBeCloseTo(10.5, 3);

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={fitSpec}
        progressRatio={1 / 3}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    const fitMidZoom = Number(frame.getAttribute("data-follow-zoom"));
    expect(fitMidZoom).toBeCloseTo((10.5 + 15.8) / 2, 2);
    expect(mockState.zoom).toBeCloseTo(fitMidZoom, 2);

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={fitSpec}
        progressRatio={0.5}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    expect(Number(frame.getAttribute("data-follow-zoom"))).toBeCloseTo(15.8, 3);
    expect(mockState.zoom).toBeCloseTo(15.8, 3);

    rerender(
      <TripStageCanvas
        stages={multiStages}
        spec={fitSpec}
        progressRatio={0.85}
        durationS={30}
        travelDurationS={30}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    expect(frame.getAttribute("data-follow-zoom")).toBeNull();
    expect(mockState.zoom).toBeCloseTo(10.5, 3);
    for (const call of mockState.jumpToCalls) {
      if (call.bearing !== undefined) {
        expect(call.bearing).toBe(0);
      }
    }
  });

  it("fades climb callouts and leader lines in, holds, and removes them after climb_callout_duration_s, while keeping them permanently when duration is 0", () => {
    const stage = makeStage();
    const earlyClimb: ClimbHighlightOut = {
      id: "climb-early",
      stage_id: "stage-canvas-1",
      name: "Fictional Early Ridge",
      category: "Cat 2",
      start_dist_m: 4000,
      end_dist_m: 6000,
      start_progress: 0.2,
      end_progress: 0.3,
      summit_progress: 0.3,
      summit_lat: 0.238,
      summit_lon: 0.51,
      summit_elev_m: 1440,
      distance_m: 2000,
      elevation_gain_m: 160,
      avg_grade_pct: 6.5,
      max_grade_pct: 9.0,
      fiets_score: 4.5,
    };

    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "fit-track";
    spec.duration_seconds = 20;
    spec.intro_hold_seconds = 0;
    spec.outro_hold_seconds = 0;
    spec.show_climb_callouts = true;
    spec.climb_callout_duration_s = 4.0;

    const { rerender } = render(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.3025}
        durationS={20}
        travelDurationS={20}
        climbs={[earlyClimb]}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    const fadeInCallout = screen.getByTestId("climb-callout-climb-early");
    const fadeInLeader = screen.getByTestId("climb-leader-climb-early");
    const fadeInOpacity = parseFloat(fadeInCallout.style.opacity);
    expect(fadeInOpacity).toBeGreaterThan(0.25);
    expect(fadeInOpacity).toBeLessThan(1.0);
    expect(parseFloat(fadeInLeader.style.opacity)).toBeCloseTo(fadeInOpacity, 2);

    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.4}
        durationS={20}
        travelDurationS={20}
        climbs={[earlyClimb]}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    expect(parseFloat(screen.getByTestId("climb-callout-climb-early").style.opacity)).toBeCloseTo(1, 2);

    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.49}
        durationS={20}
        travelDurationS={20}
        climbs={[earlyClimb]}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    const fadeOutOpacity = parseFloat(screen.getByTestId("climb-callout-climb-early").style.opacity);
    expect(fadeOutOpacity).toBeGreaterThan(0.01);
    expect(fadeOutOpacity).toBeLessThan(0.8);

    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={spec}
        progressRatio={0.55}
        durationS={20}
        travelDurationS={20}
        climbs={[earlyClimb]}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    expect(screen.queryByTestId("climb-callout-climb-early")).not.toBeInTheDocument();
    expect(screen.queryByTestId("climb-leader-climb-early")).not.toBeInTheDocument();
    expect(screen.queryByTestId("climb-callout-leaders")).not.toBeInTheDocument();

    const alwaysSpec = { ...spec, climb_callout_duration_s: 0 };
    rerender(
      <TripStageCanvas
        stages={[stage]}
        spec={alwaysSpec}
        progressRatio={0.55}
        durationS={20}
        travelDurationS={20}
        climbs={[earlyClimb]}
        renderMode="deterministic"
        exactSize={{ width: 1280, height: 720 }}
      />,
    );
    expect(screen.getByTestId("climb-callout-climb-early")).toBeInTheDocument();
    expect(screen.getByTestId("climb-leader-climb-early")).toBeInTheDocument();
    expect(parseFloat(screen.getByTestId("climb-callout-climb-early").style.opacity)).toBeCloseTo(1, 2);
  });
});
