import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { InspectorPanel } from "../components/editor/InspectorPanel";
import { StageSidebar } from "../components/editor/StageSidebar";
import { createDefaultRenderSpecV2, type StageOut } from "../lib/api/types";
import { evaluateStageCameraFocus } from "../lib/geo/camera";
import {
  buildUniformStageCameraOverrides,
  computeUniformAdaptiveZoom,
  type TimelineStage,
} from "../lib/geo/timeline";
import { useEditorStore } from "../lib/state/editor-store";

function makeTimelineStage(
  id: string,
  stageOrder: number,
  startLon: number,
  endLon: number,
  distanceM: number,
  prevGlobalDistM: number,
): TimelineStage {
  const steps = 10;
  const points = Array.from({ length: steps + 1 }, (_, i) => {
    const t = i / steps;
    const lon = startLon + (endLon - startLon) * t;
    const dist = distanceM * t;
    return {
      lat: 51.0,
      lon,
      lonUnwrapped: lon,
      elevM: 200,
      cumDistM: dist,
      globalDistM: prevGlobalDistM + dist,
      speedMps: 6.5,
      gradePct: 0,
      elapsedS: dist / 6.5,
      globalElapsedS: (prevGlobalDistM + dist) / 6.5,
      tsUtc: null,
      tsUtcMs: null,
      segmentIdx: 0,
    };
  });
  return {
    id,
    name: `Stage ${stageOrder + 1}`,
    dayIndex: stageOrder + 1,
    stageOrder,
    activityType: "cycling",
    color: "#38bdf8",
    transition: "none",
    distanceM,
    elevationGainM: 120,
    movingSeconds: distanceM / 6.5,
    elapsedSeconds: distanceM / 6.5,
    hasTimestamps: false,
    points,
  };
}

function makeStageOut(id: string, order: number, distanceM: number): StageOut {
  return {
    id,
    project_id: "proj-1",
    name: `Stage ${order + 1}`,
    day_index: order + 1,
    stage_order: order,
    activity_type: "cycling",
    source_type: "gpx",
    external_source_id: null,
    strava_activity_id: null,
    trim_start_s: null,
    trim_end_s: null,
    distance_m: distanceM,
    elevation_gain_m: 120,
    elevation_loss_m: 100,
    moving_seconds: distanceM / 6.5,
    elapsed_seconds: distanceM / 6.5,
    point_count: 120,
    has_timestamps: false,
    started_at: null,
    ended_at: null,
    min_lat: 50.0,
    max_lat: 52.0,
    min_lon: 19.0,
    max_lon: 23.0,
    warnings: [],
    created_at: "2026-01-01T00:00:00Z",
  };
}

describe("uniform adaptive stage camera zoom", () => {
  it("computes a bounded non-integer zoom and applies the same target zoom to every stage regardless of distance", () => {
    const longStage = makeTimelineStage("stage-long", 0, 19.0, 21.0, 150_000, 0);
    const shortStage = makeTimelineStage("stage-short", 1, 21.0, 21.05, 4_000, 150_000);
    const nextLong = makeTimelineStage("stage-long-2", 2, 21.05, 23.05, 150_000, 154_000);
    const stages = [longStage, shortStage, nextLong];

    const spec = createDefaultRenderSpecV2();
    spec.follow_zoom_mode = "fixed";
    spec.duration_seconds = 60;
    spec.follow_zoom_offset = 2.0;
    spec.target_screen_px_per_sec = 80;

    const zoom = computeUniformAdaptiveZoom(stages, spec, {
      viewportWidth: 720,
      viewportHeight: 900,
      durationS: 60,
      isPreview: false,
    });
    expect(zoom).toBeGreaterThanOrEqual(5.0);
    expect(zoom).toBeLessThanOrEqual(17.5);
    expect(Math.abs(zoom - Math.round(zoom))).toBeGreaterThan(0.05);

    const overrides = buildUniformStageCameraOverrides(stages, zoom);
    expect(overrides["stage-long"]?.target_zoom).toBe(zoom);
    expect(overrides["stage-short"]?.target_zoom).toBe(zoom);
    expect(overrides["stage-long-2"]?.target_zoom).toBe(zoom);
    expect(overrides["stage-short"]?.speed_multiplier).toBe(1.0);

    const specWithUniform = { ...spec, stage_camera_overrides: overrides };
    for (const p of [0, 0.1, 0.48, 0.5, 0.52, 0.8, 1]) {
      const focus = evaluateStageCameraFocus(stages, specWithUniform, p);
      expect(focus.focusWeight).toBeCloseTo(1, 5);
      expect(focus.targetZoom).toBeCloseTo(zoom, 5);
      expect(focus.speedMultiplier).toBeCloseTo(1, 5);
    }

    const customized = {
      ...spec,
      stage_camera_overrides: {
        ...overrides,
        "stage-short": {
          focus_mode: "street" as const,
          target_zoom: 15.8,
          speed_multiplier: 0.2,
          transition_margin: 0.02,
        },
      },
    };
    expect(evaluateStageCameraFocus(stages, customized, 0.2).targetZoom).toBeCloseTo(zoom, 5);
    expect(evaluateStageCameraFocus(stages, customized, 0.5).targetZoom).toBeGreaterThan(zoom);
    expect(evaluateStageCameraFocus(stages, customized, 0.85).targetZoom).toBeCloseTo(zoom, 5);
  });

  it("exposes apply and reset controls in StageSidebar and allows slider zoom below 10", async () => {
    const stages = [
      makeStageOut("stage-1", 0, 150_000),
      makeStageOut("stage-2", 1, 5_000),
    ];
    const spec = createDefaultRenderSpecV2();
    const onApply = vi.fn();
    const onReset = vi.fn();
    const onStageOverrideChange = vi.fn();

    const renderSidebar = (currentSpec: typeof spec, selectedId: string | null = null) => {
      const rootRoute = createRootRoute({
        component: () => (
          <StageSidebar
            projectId="proj-1"
            stages={stages}
            spec={currentSpec}
            selectedStageId={selectedId}
            stravaConnected={false}
            onSelectStage={vi.fn()}
            onStagesMutated={vi.fn()}
            onStageColorChange={vi.fn()}
            onStageTransitionChange={vi.fn()}
            onStageCameraOverrideChange={onStageOverrideChange}
            suggestedUniformStageZoom={9.2}
            onApplyUniformStageZoom={onApply}
            onResetStageCameraOverrides={onReset}
            onOpenStravaModal={vi.fn()}
          />
        ),
      });
      const router = createRouter({
        routeTree: rootRoute,
        history: createMemoryHistory({ initialEntries: ["/"] }),
      });
      return render(<RouterProvider router={router} />);
    };

    const view = renderSidebar(spec);
    await waitFor(() => {
      expect(screen.getByTestId("stages-apply-uniform-zoom")).toBeInTheDocument();
    });
    expect(screen.getByTestId("stages-apply-uniform-zoom")).toHaveTextContent("Uniform zoom (9.2)");
    expect(screen.getByTestId("stages-reset-stage-zoom")).toBeDisabled();

    fireEvent.click(screen.getByTestId("stages-apply-uniform-zoom"));
    expect(onApply).toHaveBeenCalledTimes(1);

    view.unmount();

    const populatedSpec = {
      ...spec,
      stage_camera_overrides: buildUniformStageCameraOverrides(stages, 9.2),
    };
    renderSidebar(populatedSpec, "stage-1");
    await waitFor(() => {
      expect(screen.getByTestId("stages-reset-stage-zoom")).not.toBeDisabled();
    });
    fireEvent.click(screen.getByTestId("stages-reset-stage-zoom"));
    expect(onReset).toHaveBeenCalledTimes(1);

    const slider = screen.getByLabelText("Stage target zoom") as HTMLInputElement;
    expect(Number(slider.min)).toBe(5);
    expect(Number(slider.value)).toBeCloseTo(9.2, 1);
    fireEvent.change(slider, { target: { value: "8.4" } });
    expect(onStageOverrideChange).toHaveBeenCalledWith(
      "stage-1",
      expect.objectContaining({ focus_mode: "custom", target_zoom: 8.4 }),
    );
  });

  it("exposes apply and reset controls in InspectorPanel Camera tab and supports atomic undo in editor store", () => {
    const spec = createDefaultRenderSpecV2();
    const onApply = vi.fn();
    const onReset = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    const wrap = (currentSpec: typeof spec) => (
      <QueryClientProvider client={queryClient}>
        <InspectorPanel
          projectId="proj-1"
          section="camera"
          spec={currentSpec}
          stageCount={2}
          onSelectSection={vi.fn()}
          onUpdateSpec={vi.fn()}
          onUpdateHud={vi.fn()}
          suggestedUniformStageZoom={9.2}
          onApplyUniformStageZoom={onApply}
          onResetStageCameraOverrides={onReset}
          onFlushSave={vi.fn(async () => true)}
        />
      </QueryClientProvider>
    );

    const { rerender } = render(wrap(spec));

    expect(screen.getByTestId("camera-apply-uniform-zoom")).toBeInTheDocument();
    expect(screen.getByTestId("camera-reset-stage-zoom")).toBeDisabled();
    fireEvent.click(screen.getByTestId("camera-apply-uniform-zoom"));
    expect(onApply).toHaveBeenCalledTimes(1);

    const populatedSpec = {
      ...spec,
      stage_camera_overrides: buildUniformStageCameraOverrides(
        [{ id: "stage-1" }, { id: "stage-2" }],
        9.2,
      ),
    };
    rerender(wrap(populatedSpec));

    expect(screen.getByTestId("camera-reset-stage-zoom")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("camera-reset-stage-zoom"));
    expect(onReset).toHaveBeenCalledTimes(1);

    const store = useEditorStore.getState();
    store.resetEditor();
    store.updateSpec({
      stage_camera_overrides: {
        "stage-1": {
          focus_mode: "street",
          target_zoom: 15.8,
          speed_multiplier: 0.2,
          transition_margin: 0.02,
        },
      },
    });
    store.updateSpec({
      stage_camera_overrides: buildUniformStageCameraOverrides(
        [{ id: "stage-1" }, { id: "stage-2" }],
        9.2,
      ),
    });
    expect(useEditorStore.getState().spec.stage_camera_overrides["stage-1"]?.target_zoom).toBe(9.2);
    expect(useEditorStore.getState().spec.stage_camera_overrides["stage-2"]?.target_zoom).toBe(9.2);

    useEditorStore.getState().undo();
    expect(useEditorStore.getState().spec.stage_camera_overrides["stage-1"]?.focus_mode).toBe("street");
    expect(useEditorStore.getState().spec.stage_camera_overrides["stage-2"]).toBeUndefined();
    useEditorStore.getState().resetEditor();
  });
});
