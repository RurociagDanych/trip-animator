import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRouter,
} from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InspectorPanel } from "../components/editor/InspectorPanel";
import { StageSidebar } from "../components/editor/StageSidebar";
import { StravaPickerModal } from "../components/editor/StravaPickerModal";
import { setCsrfToken } from "../lib/api/client";
import {
  createDefaultRenderSpecV2,
  type ClimbHighlightOut,
  type StageCameraOverride,
  type StageOut,
} from "../lib/api/types";
import { generateClimbSpotlightKeyframes } from "../lib/geo/director";
import { formatStageCalendarDate, formatStageTimeWindow } from "../lib/geo/format";
import type { TimelineStage } from "../lib/geo/timeline";
import { useEditorStore } from "../lib/state/editor-store";

function makeStage(overrides: Partial<StageOut> = {}): StageOut {
  return {
    id: "stage-1",
    project_id: "proj-1",
    source_type: "gpx",
    source_format: "gpx",
    activity_type: "cycling",
    external_source_id: null,
    strava_activity_id: null,
    name: "Morning Alpine Climb",
    stage_order: 0,
    day_index: 1,
    trim_start_s: null,
    trim_end_s: null,
    started_at: "2026-07-14T08:15:00Z",
    ended_at: "2026-07-14T12:40:00Z",
    point_count: 100,
    distance_m: 42500,
    elevation_gain_m: 1120,
    elevation_loss_m: 640,
    moving_seconds: 14400,
    elapsed_seconds: 15900,
    has_timestamps: true,
    min_lat: 45.0,
    max_lat: 45.5,
    min_lon: 10.0,
    max_lon: 10.5,
    warnings: [],
    created_at: "2026-07-14T08:00:00Z",
    ...overrides,
  };
}

describe("formatStageTimeWindow & formatStageCalendarDate", () => {
  it("formats same-day start and end timestamps in 24h time", () => {
    expect(
      formatStageTimeWindow("2026-07-14T08:15:00Z", "2026-07-14T12:40:00Z", 15900, "UTC"),
    ).toBe("Jul 14, 2026 · 08:15 – 12:40");
  });

  it("computes end time from elapsedSeconds when endedAt is omitted", () => {
    expect(formatStageTimeWindow("2026-07-14T08:15:00Z", null, 15900, "UTC")).toBe(
      "Jul 14, 2026 · 08:15 – 12:40",
    );
  });

  it("formats overnight multi-day activities with both dates and 00:xx midnight hours", () => {
    expect(
      formatStageTimeWindow("2026-07-14T22:30:00Z", "2026-07-15T00:15:00Z", 6300, "UTC"),
    ).toBe("Jul 14 22:30 – Jul 15 00:15");
    expect(
      formatStageTimeWindow("2026-07-14T22:30:00Z", "2026-07-15T03:15:00Z", 17100, "UTC"),
    ).toBe("Jul 14 22:30 – Jul 15 03:15");
  });

  it("omits end time when neither endedAt nor positive elapsedSeconds is available", () => {
    expect(formatStageTimeWindow("2026-07-14T08:15:00Z", null, 0, "UTC")).toBe(
      "Jul 14, 2026 · 08:15",
    );
    expect(formatStageTimeWindow(null, null, 0, "UTC")).toBeNull();
    expect(formatStageCalendarDate("2026-07-14T08:15:00Z", "UTC")).toBe("Jul 14, 2026");
    expect(formatStageCalendarDate(null, "UTC")).toBeNull();
  });
});

describe("StageSidebar stage date ranges & auto-organize timezone", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders start/end date-time on stage cards, day calendar date, and sends timezone on auto-organize", async () => {
    setCsrfToken("csrf-stage-dates");
    const stage1 = makeStage();
    const stage2 = makeStage({
      id: "stage-2",
      name: "Afternoon Valley Spin",
      stage_order: 1,
      day_index: 1,
      started_at: "2026-07-14T15:30:00Z",
      ended_at: "2026-07-14T17:45:00Z",
      elapsed_seconds: 8100,
    });
    const spec = createDefaultRenderSpecV2();
    const onStagesMutated = vi.fn();

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "proj-1", stages: [stage1, stage2] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const rootRoute = createRootRoute({
      component: () => (
        <StageSidebar
          projectId="proj-1"
          stages={[stage1, stage2]}
          selectedStageId={stage1.id}
          spec={spec}
          stravaConnected={false}
          onSelectStage={vi.fn()}
          onStageColorChange={vi.fn()}
          onStageTransitionChange={vi.fn()}
          onStagesMutated={onStagesMutated}
          onOpenStravaModal={vi.fn()}
        />
      ),
    });
    const router = createRouter({
      routeTree: rootRoute,
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });

    render(<RouterProvider router={router} />);

    const dateRanges = await screen.findAllByTestId("stage-date-range");
    expect(dateRanges).toHaveLength(2);
    expect(dateRanges[0]?.textContent).toBe(
      formatStageTimeWindow(stage1.started_at, stage1.ended_at, stage1.elapsed_seconds),
    );
    expect(dateRanges[1]?.textContent).toBe(
      formatStageTimeWindow(stage2.started_at, stage2.ended_at, stage2.elapsed_seconds),
    );

    const dayDate = screen.getByTestId("day-1-date");
    expect(dayDate.textContent).toContain(formatStageCalendarDate(stage1.started_at) ?? "");

    fireEvent.click(screen.getByRole("button", { name: /Auto-Organize Trip/i }));

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/projects/proj-1/stages/auto-organize");
    const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
    const expectedTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    expect(body.timezone).toBe(expectedTz);
    expect(onStagesMutated).toHaveBeenCalledTimes(1);
  });

  it("quick-deletes a stage directly from the StageSidebar card with two-click inline confirm and no bubbling", async () => {
    setCsrfToken("csrf-quick-delete");
    const stage1 = makeStage();
    const spec = createDefaultRenderSpecV2();
    const onSelectStage = vi.fn();
    const onStagesMutated = vi.fn();

    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 204 }));

    const rootRoute = createRootRoute({
      component: () => (
        <StageSidebar
          projectId="proj-1"
          stages={[stage1]}
          selectedStageId={null}
          spec={spec}
          stravaConnected={false}
          onSelectStage={onSelectStage}
          onStageColorChange={vi.fn()}
          onStageTransitionChange={vi.fn()}
          onStagesMutated={onStagesMutated}
          onOpenStravaModal={vi.fn()}
        />
      ),
    });
    const router = createRouter({
      routeTree: rootRoute,
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });

    render(<RouterProvider router={router} />);

    const deleteBtn = await screen.findByTestId(`quick-delete-stage-${stage1.id}`);
    expect(deleteBtn).toHaveAttribute("aria-label", `Delete ${stage1.name}`);

    fireEvent.click(deleteBtn);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onSelectStage).not.toHaveBeenCalled();
    const confirmBtn = screen.getByTestId(`quick-delete-stage-${stage1.id}`);
    expect(confirmBtn).toHaveAttribute("aria-label", `Confirm delete ${stage1.name}`);

    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(String(url)).toBe(`/api/v1/projects/proj-1/stages/${stage1.id}`);
    expect(init?.method).toBe("DELETE");
    expect(onSelectStage).not.toHaveBeenCalled();
    expect(onStagesMutated).toHaveBeenCalledTimes(1);
  });
});

describe("StravaPickerModal duplicate import guard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("disables selection and import button with badge for activities already imported in the project", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const existingStage = makeStage({
      source_type: "strava",
      source_format: "strava",
      strava_activity_id: 9001,
      name: "Dolomites Loop",
    });

    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            id: 9001,
            name: "Dolomites Loop",
            sport_type: "Ride",
            start_date: "2026-07-14T08:00:00Z",
            distance_m: 64000,
            elevation_gain_m: 1500,
            moving_time_s: 10800,
            has_gps: true,
          },
          {
            id: 9002,
            name: "Gavia Pass",
            sport_type: "Ride",
            start_date: "2026-07-15T08:00:00Z",
            distance_m: 48000,
            elevation_gain_m: 1300,
            moving_time_s: 8600,
            has_gps: true,
          },
        ]),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(
      <QueryClientProvider client={qc}>
        <StravaPickerModal
          projectId="proj-1"
          units="metric"
          defaultDayIndex={2}
          existingStages={[existingStage]}
          onClose={vi.fn()}
          onImported={vi.fn()}
        />
      </QueryClientProvider>,
    );

    const dupCheckbox = await screen.findByLabelText("Select Dolomites Loop");
    expect(dupCheckbox).toBeDisabled();
    expect(screen.getByTestId("strava-imported-badge-9001")).toBeInTheDocument();
    const inTripBtn = screen.getByRole("button", { name: /In trip/i });
    expect(inTripBtn).toBeDisabled();

    const freshCheckbox = screen.getByLabelText("Select Gavia Pass");
    expect(freshCheckbox).not.toBeDisabled();
  });
});

describe("Climb Spotlight removal from InspectorPanel & smooth legacy director keyframes", () => {
  it("hides Climb Spotlight from InspectorPanel Director Presets while keeping Manual, Expedition Doc, and Social Reel", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spec = createDefaultRenderSpecV2();

    render(
      <QueryClientProvider client={qc}>
        <InspectorPanel
          projectId="proj-1"
          section="camera"
          spec={spec}
          stageCount={2}
          onSelectSection={vi.fn()}
          onUpdateSpec={vi.fn()}
          onUpdateHud={vi.fn()}
          onFlushSave={async () => true}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("button", { name: /Manual/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Expedition Doc/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Social Reel/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Climb Spotlight/i })).not.toBeInTheDocument();
  });

  it("keeps legacy generateClimbSpotlightKeyframes smooth without preset or bearing jumps", () => {
    const spec = createDefaultRenderSpecV2();
    const stages: TimelineStage[] = [
      {
        id: "stage-1",
        name: "Mountain Stage",
        dayIndex: 1,
        stageOrder: 0,
        activityType: "cycling",
        color: "#38bdf8",
        transition: "none",
        distanceM: 10000,
        elevationGainM: 800,
        movingSeconds: 2400,
        elapsedSeconds: 2400,
        hasTimestamps: true,
        points: [
          {
            lat: 45.0,
            lon: 10.0,
            lonUnwrapped: 10.0,
            elevM: 500,
            cumDistM: 0,
            globalDistM: 0,
            speedMps: 5,
            gradePct: 4,
            hrBpm: null,
            watts: null,
            elapsedS: 0,
            globalElapsedS: 0,
            tsUtcMs: 0,
            segmentIdx: 0,
          },
          {
            lat: 45.05,
            lon: 10.05,
            lonUnwrapped: 10.05,
            elevM: 1300,
            cumDistM: 10000,
            globalDistM: 10000,
            speedMps: 4,
            gradePct: 8,
            hrBpm: null,
            watts: null,
            elapsedS: 2400,
            globalElapsedS: 2400,
            tsUtcMs: 2400000,
            segmentIdx: 0,
          },
        ],
      },
    ];
    const climbs: ClimbHighlightOut[] = [
      {
        id: "climb-1",
        stage_id: "stage-1",
        name: "Passo Stelvio",
        category: "HC",
        start_dist_m: 2000,
        end_dist_m: 8000,
        start_progress: 0.2,
        end_progress: 0.8,
        summit_progress: 0.75,
        summit_lat: 45.04,
        summit_lon: 10.04,
        summit_elev_m: 1300,
        distance_m: 6000,
        elevation_gain_m: 600,
        avg_grade_pct: 10,
        max_grade_pct: 14,
        fiets_score: 12.5,
      },
    ];

    const withClimbs = generateClimbSpotlightKeyframes({ stages, spec, climbs });
    const withoutClimbs = generateClimbSpotlightKeyframes({ stages, spec, climbs: [] });

    for (const list of [withClimbs, withoutClimbs]) {
      expect(list.length).toBeGreaterThanOrEqual(4);
      for (const kf of list) {
        expect(kf.camera_preset).toBe("flat");
        expect(kf.bearing_offset_deg).toBe(0);
      }
    }
  });
});

describe("Per-stage camera focus UI, InspectorPanel climb tag duration, and editor store", () => {
  afterEach(() => {
    useEditorStore.getState().resetEditor();
  });

  it("renders StageSidebar focus badge, preset pills (Auto, Close, Street Focus, Custom), and target zoom / pace sliders", async () => {
    const stage1 = makeStage({ id: "stage-focus-ui", name: "Downtown Walk" });
    let currentSpec = createDefaultRenderSpecV2();
    const calls: { stageId: string; override: StageCameraOverride | null }[] = [];
    const onOverride = vi.fn((stageId: string, override: StageCameraOverride | null) => {
      calls.push({ stageId, override });
      const nextOverrides = { ...currentSpec.stage_camera_overrides };
      if (override === null || override.focus_mode === "auto") {
        delete nextOverrides[stageId];
      } else {
        nextOverrides[stageId] = override;
      }
      currentSpec = { ...currentSpec, stage_camera_overrides: nextOverrides };
    });

    const renderSidebar = (activeTab: "stages" | "edit", spec = currentSpec) => {
      const rootRoute = createRootRoute({
        component: () => (
          <StageSidebar
            projectId="proj-1"
            stages={[stage1]}
            selectedStageId={stage1.id}
            spec={spec}
            activeTab={activeTab}
            stravaConnected={false}
            onSelectStage={vi.fn()}
            onStageColorChange={vi.fn()}
            onStageTransitionChange={vi.fn()}
            onStageCameraOverrideChange={onOverride}
            onStagesMutated={vi.fn()}
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

    const firstRender = renderSidebar("edit");
    const section = await screen.findByTestId("stage-camera-focus-section");
    expect(section).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Street Focus/i }));
    expect(calls[calls.length - 1]).toEqual({
      stageId: stage1.id,
      override: {
        focus_mode: "street",
        target_zoom: 15.8,
        speed_multiplier: 0.2,
        transition_margin: 0.02,
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Close/i }));
    expect(calls[calls.length - 1]).toEqual({
      stageId: stage1.id,
      override: {
        focus_mode: "close",
        target_zoom: 13.8,
        speed_multiplier: 0.7,
        transition_margin: 0.02,
      },
    });

    fireEvent.click(screen.getByRole("button", { name: /Custom/i }));
    expect(calls[calls.length - 1]?.override?.focus_mode).toBe("custom");

    const zoomSlider = screen.getByLabelText("Stage target zoom");
    fireEvent.change(zoomSlider, { target: { value: "16.2" } });
    expect(calls[calls.length - 1]?.override).toMatchObject({
      focus_mode: "custom",
      target_zoom: 16.2,
    });

    const speedSlider = screen.getByLabelText("Stage playback speed");
    fireEvent.change(speedSlider, { target: { value: "0.5" } });
    expect(calls[calls.length - 1]?.override).toMatchObject({
      focus_mode: "custom",
      speed_multiplier: 0.5,
    });

    fireEvent.click(screen.getByRole("button", { name: /^Auto\s+1×$/i }));
    expect(calls[calls.length - 1]).toEqual({ stageId: stage1.id, override: null });

    firstRender.unmount();

    const streetSpec = createDefaultRenderSpecV2();
    streetSpec.stage_camera_overrides = {
      [stage1.id]: {
        focus_mode: "street",
        target_zoom: 15.8,
        speed_multiplier: 0.2,
        transition_margin: 0.02,
      },
    };
    renderSidebar("stages", streetSpec);
    const badge = await screen.findByTestId(`stage-focus-badge-${stage1.id}`);
    expect(badge).toHaveTextContent("Street · 0.2×");
  });

  it("renders InspectorPanel climb tag display duration slider and Always toggle when show_climb_callouts is enabled", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onUpdateSpec = vi.fn();
    const spec = createDefaultRenderSpecV2();
    spec.show_climb_callouts = true;
    spec.climb_callout_duration_s = 4.0;

    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <InspectorPanel
          projectId="proj-1"
          section="route"
          spec={spec}
          stageCount={1}
          onSelectSection={vi.fn()}
          onUpdateSpec={onUpdateSpec}
          onUpdateHud={vi.fn()}
          onFlushSave={async () => true}
        />
      </QueryClientProvider>,
    );

    const controls = screen.getByTestId("climb-callout-duration-controls");
    expect(controls).toHaveTextContent("Tag display duration");
    expect(controls).toHaveTextContent("4s");

    const slider = screen.getByLabelText("Climb tag display duration");
    fireEvent.change(slider, { target: { value: "7.5" } });
    expect(onUpdateSpec).toHaveBeenCalledWith({ climb_callout_duration_s: 7.5 });

    const alwaysBtn = screen.getByRole("button", { name: "Always" });
    expect(alwaysBtn).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(alwaysBtn);
    expect(onUpdateSpec).toHaveBeenCalledWith({ climb_callout_duration_s: 0 });

    rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel
          projectId="proj-1"
          section="route"
          spec={{ ...spec, climb_callout_duration_s: 0 }}
          stageCount={1}
          onSelectSection={vi.fn()}
          onUpdateSpec={onUpdateSpec}
          onUpdateHud={vi.fn()}
          onFlushSave={async () => true}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("button", { name: "Always" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Always" }));
    expect(onUpdateSpec).toHaveBeenCalledWith({ climb_callout_duration_s: 4.0 });

    rerender(
      <QueryClientProvider client={qc}>
        <InspectorPanel
          projectId="proj-1"
          section="route"
          spec={{ ...spec, show_climb_callouts: false }}
          stageCount={1}
          onSelectSection={vi.fn()}
          onUpdateSpec={onUpdateSpec}
          onUpdateHud={vi.fn()}
          onFlushSave={async () => true}
        />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("climb-callout-duration-controls")).not.toBeInTheDocument();
  });

  it("manages stage_camera_overrides with undo/redo and prunes deleted stages in useEditorStore", () => {
    const store = useEditorStore.getState();
    const s1 = makeStage({ id: "s-keep" });
    const s2 = makeStage({ id: "s-remove", stage_order: 1 });
    store.setStages([s1, s2]);

    store.setStageCameraOverride("s-keep", {
      focus_mode: "street",
      target_zoom: 15.8,
      speed_multiplier: 0.45,
      transition_margin: 0.02,
    });
    store.setStageCameraOverride("s-remove", {
      focus_mode: "close",
      target_zoom: 13.8,
      speed_multiplier: 0.7,
      transition_margin: 0.02,
    });

    expect(useEditorStore.getState().spec.stage_camera_overrides["s-keep"]?.focus_mode).toBe("street");
    expect(useEditorStore.getState().spec.stage_camera_overrides["s-remove"]?.focus_mode).toBe("close");
    expect(useEditorStore.getState().hasUnsavedChanges).toBe(true);

    useEditorStore.getState().setStages([s1]);
    expect(useEditorStore.getState().spec.stage_camera_overrides["s-remove"]).toBeUndefined();
    expect(useEditorStore.getState().spec.stage_camera_overrides["s-keep"]?.focus_mode).toBe("street");

    useEditorStore.getState().undo();
    expect(useEditorStore.getState().spec.stage_camera_overrides["s-remove"]).toBeUndefined();
    expect(useEditorStore.getState().spec.stage_camera_overrides["s-keep"]?.focus_mode).toBe("street");

    useEditorStore.getState().setStageCameraOverride("s-keep", {
      focus_mode: "auto",
      target_zoom: null,
      speed_multiplier: 1.0,
      transition_margin: 0.02,
    });
    expect(useEditorStore.getState().spec.stage_camera_overrides["s-keep"]).toBeUndefined();
  });
});
