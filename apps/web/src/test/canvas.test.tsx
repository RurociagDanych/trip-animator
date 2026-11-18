// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
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
    triggerRepaint() {}
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

    // Fit-track mode applies zoom_offset on top of fitted.zoom
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

    // Switch to fit-track mode
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

    // Disable climb callouts -> neither pills nor SVG leaders render
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

  it("fast-paths waitForFrameSettle in 1 rAF when tiles are cached and awaits idle when tiles are not loaded", async () => {
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
});
