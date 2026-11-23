import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExportPanel } from "../components/editor/ExportPanel";
import { InspectorPanel } from "../components/editor/InspectorPanel";
import { PhotoStopsSection } from "../components/editor/PhotoStopsSection";
import { StageSidebar } from "../components/editor/StageSidebar";
import { StatisticsHud } from "../components/stage/StatisticsHud";
import {
  ALL_STATISTIC_METRICS,
  createDefaultRenderSpecV2,
  type StageCameraOverride,
  type StageOut,
} from "../lib/api/types";
import { computeStageProgressSpans } from "../lib/geo/camera";
import { computePhotoStopTotalDurationS, type MatchedPhoto } from "../lib/geo/photos";
import type { EvaluatedTimelineState } from "../lib/geo/timeline";
import { useEditorStore } from "../lib/state/editor-store";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function makeStage(overrides: Partial<StageOut> = {}): StageOut {
  return {
    id: "stage-1",
    project_id: "proj-1",
    name: "Fictional Meridian Stage",
    day_index: 1,
    stage_order: 0,
    source_type: "gpx",
    activity_type: "cycling",
    external_source_id: null,
    strava_activity_id: null,
    distance_m: 25_000,
    elevation_gain_m: 900,
    elevation_loss_m: 850,
    moving_seconds: 3600,
    elapsed_seconds: 4000,
    has_timestamps: true,
    started_at: "2001-06-14T08:00:00Z",
    trim_start_s: null,
    trim_end_s: null,
    point_count: 150,
    min_lat: 0.2173,
    max_lat: 0.3552,
    min_lon: 0.4512,
    max_lon: 0.7113,
    warnings: [],
    created_at: "2001-06-14T08:00:00Z",
    ...overrides,
  };
}

function makeEvaluatedState(): EvaluatedTimelineState {
  return {
    progressRatio: 0.5,
    activeStageIndex: 0,
    activeStage: {
      id: "stage-1",
      name: "Fictional Meridian Stage",
      dayIndex: 2,
      stageOrder: 0,
      activityType: "cycling",
      color: "#38bdf8",
      transition: "none",
      distanceM: 25_000,
      elevationGainM: 900,
      movingSeconds: 3600,
      elapsedSeconds: 4000,
      hasTimestamps: true,
      points: [],
    },
    markerPosition: [0.5325, 0.2925],
    previousPosition: [0.5048, 0.2728],
    elevM: 2121,
    speedMps: 8.5,
    gradePct: 6.2,
    distanceTravelledM: 12_500,
    distanceRemainingM: 12_500,
    totalDistanceM: 25_000,
    elapsedSeconds: 3725,
    currentTsUtcMs: Date.parse("2001-06-14T10:15:30Z"),
    visiblePaths: [],
    overnightStops: [],
    bounds: [0.4512, 0.2173, 0.7113, 0.3552],
  };
}

describe("Component tests: InspectorPanel, StageSidebar, ExportPanel, StatisticsHud", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe("InspectorPanel", () => {
    it("warns in the Output section when a stored frame rate is invalid", () => {
      const spec = createDefaultRenderSpecV2();
      render(
        <QueryClientProvider client={makeQueryClient()}>
          <InspectorPanel
            projectId="proj-1"
            section="camera"
            spec={spec}
            stageCount={1}
            onSelectSection={vi.fn()}
            onUpdateSpec={vi.fn()}
            onUpdateHud={vi.fn()}
            onFlushSave={async () => true}
            invalidSpecValues={[{ field: "fps", value: 24, fallback: spec.fps }]}
          />
        </QueryClientProvider>,
      );
      expect(screen.getByRole("alert").textContent).toContain('Frame rate "24" is not supported');
    });

    it("updates track and marker color pickers in Route tab and text/panel color pickers in HUD tab", () => {
      const onUpdateSpec = vi.fn();
      const onUpdateHud = vi.fn();
      const onSelectSection = vi.fn();
      const spec = createDefaultRenderSpecV2();
      const qc = makeQueryClient();

      const { rerender } = render(
        <QueryClientProvider client={qc}>
          <InspectorPanel
            projectId="proj-1"
            section="route"
            spec={spec}
            stageCount={1}
            onSelectSection={onSelectSection}
            onUpdateSpec={onUpdateSpec}
            onUpdateHud={onUpdateHud}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      const colorInputs = Array.from(
        document.querySelectorAll<HTMLInputElement>('input[type="color"]'),
      );
      expect(colorInputs).toHaveLength(2);
      fireEvent.change(colorInputs[0]!, { target: { value: "#ef4444" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ track_color: "#ef4444" });

      fireEvent.change(colorInputs[1]!, { target: { value: "#22c55e" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ marker_color: "#22c55e" });

      fireEvent.click(screen.getByRole("tab", { name: "HUD" }));
      expect(onSelectSection).toHaveBeenCalledWith("hud");

      rerender(
        <QueryClientProvider client={qc}>
          <InspectorPanel
            projectId="proj-1"
            section="hud"
            spec={spec}
            stageCount={1}
            onSelectSection={onSelectSection}
            onUpdateSpec={onUpdateSpec}
            onUpdateHud={onUpdateHud}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      const hudColorInputs = Array.from(
        document.querySelectorAll<HTMLInputElement>('input[type="color"]'),
      );
      expect(hudColorInputs).toHaveLength(2);
      fireEvent.change(hudColorInputs[0]!, { target: { value: "#f8fafc" } });
      expect(onUpdateHud).toHaveBeenCalledWith({ text_color: "#f8fafc" });

      fireEvent.change(hudColorInputs[1]!, { target: { value: "#020617" } });
      expect(onUpdateHud).toHaveBeenCalledWith({ background_color: "#020617" });

      const collisionToggle = screen.getByLabelText(/Auto-avoid route collision/i);
      fireEvent.click(collisionToggle);
      expect(onUpdateHud).toHaveBeenCalledWith({ collision_avoidance: false });

      rerender(
        <QueryClientProvider client={qc}>
          <InspectorPanel
            projectId="proj-1"
            section="camera"
            spec={spec}
            stageCount={10}
            dayCount={10}
            onSelectSection={onSelectSection}
            onUpdateSpec={onUpdateSpec}
            onUpdateHud={onUpdateHud}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      const durationInput = screen.getByLabelText("Duration (s)") as HTMLInputElement;
      expect(durationInput.max).toBe("180");
      fireEvent.change(durationInput, { target: { value: "150" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ duration_seconds: 150 });

      fireEvent.click(screen.getByRole("button", { name: "Auto-fit" }));
      expect(onUpdateSpec).toHaveBeenCalledWith({ duration_seconds: 80 });
    });
  });

  describe("StageSidebar trim controls & stage color picker", () => {
    it("validates trim bounds, applies valid trim, clears trim, and changes stage color", async () => {
      const stage = makeStage({ trim_start_s: 30, trim_end_s: 500 });
      const spec = createDefaultRenderSpecV2();
      const onStageColorChange = vi.fn();
      const onStagesUpdated = vi.fn();

      vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
        return new Response(JSON.stringify(stage), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      });

      const rootRoute = createRootRoute({
        component: () => (
          <StageSidebar
            projectId="proj-1"
            stages={[stage]}
            selectedStageId={stage.id}
            spec={spec}
            stravaConnected={false}
            onSelectStage={vi.fn()}
            onStageColorChange={onStageColorChange}
            onStageTransitionChange={vi.fn()}
            onStagesMutated={onStagesUpdated}
            onOpenStravaModal={vi.fn()}
          />
        ),
      });
      const router = createRouter({
        routeTree: rootRoute,
        history: createMemoryHistory({ initialEntries: ["/"] }),
      });

      render(<RouterProvider router={router} />);

      fireEvent.click(await screen.findByRole("button", { name: "Color #14b8a6" }));
      expect(onStageColorChange).toHaveBeenCalledWith("stage-1", "#14b8a6");

      fireEvent.change(screen.getByLabelText("Custom stage color"), {
        target: { value: "#ec4899" },
      });
      expect(onStageColorChange).toHaveBeenCalledWith("stage-1", "#ec4899");

      fireEvent.change(screen.getByLabelText("Trim start seconds"), {
        target: { value: "600" },
      });
      fireEvent.change(screen.getByLabelText("Trim end seconds"), {
        target: { value: "300" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Apply stage changes/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Trim end must be greater than trim start/i,
      );

      fireEvent.change(screen.getByLabelText("Trim start seconds"), {
        target: { value: "60" },
      });
      fireEvent.change(screen.getByLabelText("Trim end seconds"), {
        target: { value: "1200" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Apply stage changes/i }));

      await waitFor(() => {
        expect(onStagesUpdated).toHaveBeenCalled();
      });

      fireEvent.click(screen.getByRole("button", { name: /Clear trim/i }));
      await waitFor(() => {
        expect(onStagesUpdated).toHaveBeenCalledTimes(2);
      });
    });

    it("supports quick pace chips (0.05x to 2x) and 0.05x slider minimum in StageSidebar", async () => {
      const stage = makeStage({ id: "stage-pace", name: "Downtown Section" });
      const spec = createDefaultRenderSpecV2();
      spec.stage_camera_overrides = {
        "stage-pace": {
          focus_mode: "custom",
          target_zoom: 15.8,
          speed_multiplier: 0.25,
          transition_margin: 0.02,
        },
      };
      const onOverrideChange = vi.fn<
        (stageId: string, override: StageCameraOverride | null) => void
      >();

      const rootRoute = createRootRoute({
        component: () => (
          <StageSidebar
            projectId="proj-1"
            stages={[stage]}
            selectedStageId={stage.id}
            spec={spec}
            stravaConnected={false}
            onSelectStage={vi.fn()}
            onStageColorChange={vi.fn()}
            onStageTransitionChange={vi.fn()}
            onStageCameraOverrideChange={onOverrideChange}
            onStagesMutated={vi.fn()}
            onOpenStravaModal={vi.fn()}
          />
        ),
      });
      const router = createRouter({
        routeTree: rootRoute,
        history: createMemoryHistory({ initialEntries: ["/"] }),
      });

      render(<RouterProvider router={router} />);

      const speedSlider = (await screen.findByLabelText(
        "Stage playback speed",
      )) as HTMLInputElement;
      expect(speedSlider.min).toBe("0.05");
      expect(speedSlider.max).toBe("3");
      expect(speedSlider.step).toBe("0.05");

      const expectedPaces = [0.05, 0.1, 0.25, 0.5, 1, 2] as const;
      for (const pace of expectedPaces) {
        const chip = screen.getByTestId(`stage-pace-chip-${pace}`);
        expect(chip).toHaveTextContent(`${pace}×`);
        expect(chip).toHaveAttribute("aria-pressed", pace === 0.25 ? "true" : "false");
      }

      fireEvent.click(screen.getByTestId("stage-pace-chip-0.05"));
      expect(onOverrideChange).toHaveBeenCalledWith("stage-pace", {
        focus_mode: "custom",
        target_zoom: 15.8,
        speed_multiplier: 0.05,
        transition_margin: 0.02,
      });

      fireEvent.click(screen.getByTestId("stage-pace-chip-2"));
      expect(onOverrideChange).toHaveBeenCalledWith("stage-pace", {
        focus_mode: "custom",
        target_zoom: 15.8,
        speed_multiplier: 2,
        transition_margin: 0.02,
      });
    });

    it("maps the playhead onto journey progress using the cinematic_stop film total", async () => {
      useEditorStore.getState().resetEditor();
      const stage1 = makeStage({ id: "stage-1", name: "Alpine Pass", stage_order: 0, distance_m: 20_000, point_count: 120 });
      const stage2 = makeStage({ id: "stage-2", name: "Valley Roll", stage_order: 1, distance_m: 20_000, point_count: 100 });
      const spec = createDefaultRenderSpecV2();
      spec.duration_seconds = 20;
      spec.intro_hold_seconds = 0;
      spec.outro_hold_seconds = 0;
      spec.photo_card_style = "cinematic_stop";
      spec.photo_display_seconds = 2;
      const photo: MatchedPhoto = {
        id: "photo-1",
        caption: "Fictional checkpoint",
        width: 640,
        height: 480,
        takenAtUtc: null,
        takenAtMs: null,
        lat: null,
        lon: null,
        matchedPosition: [0.1, 0.1],
        pinPosition: [0.1, 0.1],
        matchedProgress: 0.5,
        matchMethod: "manual",
        src: "/photo.jpg",
      };
      expect(computePhotoStopTotalDurationS(spec, [photo], [])).toBe(23);

      const rootRoute = createRootRoute({
        component: () => (
          <StageSidebar
            projectId="proj-1"
            stages={[stage1, stage2]}
            selectedStageId={stage1.id}
            spec={spec}
            matchedPhotos={[photo]}
            stravaConnected={false}
            onSelectStage={vi.fn()}
            onStageColorChange={vi.fn()}
            onStageTransitionChange={vi.fn()}
            onStagesMutated={vi.fn(async () => {})}
            onOpenStravaModal={vi.fn()}
          />
        ),
      });
      const router = createRouter({
        routeTree: rootRoute,
        history: createMemoryHistory({ initialEntries: ["/"] }),
      });
      render(<RouterProvider router={router} />);
      await screen.findByTestId("stage-split-section");

      // 4.6 s of a 23 s film is 4.6 s of journey, which is progress 0.23 and 9.2 km of stage 1.
      useEditorStore.getState().setCurrentTimeRatio(0.2);
      fireEvent.click(screen.getByTestId("stage-split-use-playhead"));
      expect((screen.getByTestId("stage-split-slider") as HTMLInputElement).value).toBe("9200");
    });

    it("splits a stage with slider readout, playhead snapping, other-stage notice, custom names, and Part 2 selection", async () => {
      useEditorStore.getState().resetEditor();
      const stage1 = makeStage({
        id: "stage-1",
        name: "Alpine Pass",
        stage_order: 0,
        distance_m: 20_000,
        point_count: 120,
      });
      const stage2 = makeStage({
        id: "stage-2",
        name: "Valley Roll",
        stage_order: 1,
        distance_m: 20_000,
        point_count: 100,
      });
      const part1 = makeStage({
        id: "stage-1-p1",
        name: "Climb Start",
        stage_order: 0,
        distance_m: 12_000,
      });
      const part2 = makeStage({
        id: "stage-1-p2",
        name: "Town Finish",
        stage_order: 1,
        distance_m: 8_000,
      });

      const spec = createDefaultRenderSpecV2();
      spec.duration_seconds = 20;
      spec.intro_hold_seconds = 0;
      spec.outro_hold_seconds = 0;

      const onSelectStage = vi.fn();
      const onStagesMutated = vi.fn(async () => {});

      const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.endsWith("/api/v1/projects/proj-1/stages/stage-1/split") && init?.method === "POST") {
          return new Response(
            JSON.stringify({
              part1,
              part2,
              stages: [part1, part2, { ...stage2, stage_order: 2 }],
              revision: 4,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });

      const rootRoute = createRootRoute({
        component: () => (
          <StageSidebar
            projectId="proj-1"
            stages={[stage1, stage2]}
            selectedStageId={stage1.id}
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

      const splitSection = await screen.findByTestId("stage-split-section");
      expect(splitSection).toBeInTheDocument();

      const readout = screen.getByTestId("stage-split-readout");
      expect(readout).toHaveTextContent("Part 1: 10.0 km · Part 2: 10.0 km");

      useEditorStore.getState().setCurrentTimeRatio(0.75);
      fireEvent.click(screen.getByTestId("stage-split-use-playhead"));
      expect(screen.getByTestId("stage-split-playhead-notice")).toHaveTextContent(
        "Playhead is currently on another stage.",
      );

      useEditorStore.getState().setCurrentTimeRatio(0.3);
      fireEvent.click(screen.getByTestId("stage-split-use-playhead"));
      expect(screen.queryByTestId("stage-split-playhead-notice")).not.toBeInTheDocument();
      expect((screen.getByTestId("stage-split-slider") as HTMLInputElement).value).toBe("12000");
      expect(readout).toHaveTextContent("Part 1: 12.0 km · Part 2: 8.0 km");

      fireEvent.change(screen.getByTestId("stage-split-slider"), { target: { value: "14500" } });
      expect(readout).toHaveTextContent("Part 1: 14.5 km · Part 2: 5.5 km");

      const p1Input = screen.getByLabelText("Part 1 stage name") as HTMLInputElement;
      const p2Input = screen.getByLabelText("Part 2 stage name") as HTMLInputElement;
      expect(p1Input.value).toBe("Alpine Pass (Part 1)");
      expect(p2Input.value).toBe("Alpine Pass (Part 2)");

      const fastStage = makeStage({
        id: "stage-fast",
        name: "Fast Stage",
        stage_order: 0,
        distance_m: 10_000,
        elapsed_seconds: 1000,
        has_timestamps: true,
      });
      const slowStage = makeStage({
        id: "stage-slow",
        name: "Slow Stage",
        stage_order: 1,
        distance_m: 10_000,
        elapsed_seconds: 3000,
        has_timestamps: true,
      });
      const recordedTimeSpec = { ...spec, timeline_mode: "recorded-time" as const };
      const spansRecorded = computeStageProgressSpans(
        [fastStage, slowStage].map((s) => ({
          id: s.id,
          name: s.name,
          dayIndex: s.day_index,
          stageOrder: s.stage_order,
          activityType: "cycling" as const,
          color: "#38bdf8",
          transition: "none" as const,
          distanceM: s.distance_m,
          elevationGainM: s.elevation_gain_m,
          movingSeconds: s.moving_seconds,
          elapsedSeconds: s.elapsed_seconds,
          hasTimestamps: s.has_timestamps,
          points: [],
        })),
        recordedTimeSpec,
      );
      expect(spansRecorded[0]!.endProgress).toBeCloseTo(0.25, 6);
      expect(spansRecorded[1]!.startProgress).toBeCloseTo(0.25, 6);

      fireEvent.change(p1Input, { target: { value: "Climb Start" } });
      fireEvent.change(p2Input, { target: { value: "Town Finish" } });

      fireEvent.click(screen.getByTestId("stage-split-submit"));

      await waitFor(() => {
        expect(onStagesMutated).toHaveBeenCalledTimes(1);
      });

      const splitCall = fetchSpy.mock.calls.find(
        ([u, req]) =>
          String(u).endsWith("/api/v1/projects/proj-1/stages/stage-1/split") &&
          req?.method === "POST",
      );
      expect(splitCall).toBeDefined();
      expect(JSON.parse(String(splitCall?.[1]?.body))).toEqual({
        split_distance_m: 14500,
        part1_name: "Climb Start",
        part2_name: "Town Finish",
      });
      expect(onSelectStage).toHaveBeenCalledWith("stage-1-p2");
      expect(onSelectStage).toHaveBeenCalledTimes(2);
      useEditorStore.getState().resetEditor();
    });

    it("disables Split stage button when point_count < 4 and displays API error on split failure", async () => {
      const tinyStage = makeStage({ id: "stage-tiny", point_count: 3, distance_m: 500 });
      const normalStage = makeStage({ id: "stage-err", point_count: 20, distance_m: 5000 });
      const spec = createDefaultRenderSpecV2();

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/split") && init?.method === "POST") {
          return new Response(JSON.stringify({ detail: "Stage cannot be split at boundary" }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });

      const renderWithSelected = (stage: StageOut) => {
        const rootRoute = createRootRoute({
          component: () => (
            <StageSidebar
              projectId="proj-1"
              stages={[stage]}
              selectedStageId={stage.id}
              spec={spec}
              stravaConnected={false}
              onSelectStage={vi.fn()}
              onStageColorChange={vi.fn()}
              onStageTransitionChange={vi.fn()}
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

      const tinyRender = renderWithSelected(tinyStage);
      expect(await screen.findByTestId("stage-split-submit")).toBeDisabled();
      tinyRender.unmount();

      renderWithSelected(normalStage);
      const submitBtn = await screen.findByTestId("stage-split-submit");
      expect(submitBtn).toBeEnabled();
      fireEvent.click(submitBtn);
      expect(await screen.findByTestId("stage-split-error")).toHaveTextContent(
        "Stage cannot be split at boundary",
      );
    });
  });

  describe("ExportPanel", () => {
    it("blocks export when stageCount is 0 and handles save failure + render job submission", async () => {
      const spec = createDefaultRenderSpecV2();
      const qc = makeQueryClient();

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/renders") && method === "GET") {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/renders") && method === "POST") {
          return new Response(
            JSON.stringify({
              id: "job-99",
              project_id: "proj-1",
              status: "succeeded",
              is_preview: false,
              progress: 1,
              spec,
              width: 1920,
              height: 1080,
              fps: 30,
              duration_seconds: 12,
              error_message: null,
              created_at: "2001-06-14T10:00:00Z",
              updated_at: "2001-06-14T10:00:10Z",
            }),
            { status: 201, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });

      const { rerender } = render(
        <QueryClientProvider client={qc}>
          <ExportPanel
            projectId="proj-1"
            spec={spec}
            stageCount={0}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: /Export MP4/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Add at least one GPX or Strava stage before rendering/i,
      );

      fireEvent.click(screen.getByText(/Export GPX/i));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Add at least one stage before exporting GPX/i,
      );

      rerender(
        <QueryClientProvider client={qc}>
          <ExportPanel
            projectId="proj-1"
            spec={spec}
            stageCount={1}
            onFlushSave={async () => false}
          />
        </QueryClientProvider>,
      );
      fireEvent.click(screen.getByRole("button", { name: /Export MP4/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Resolve unsaved changes or revision conflict before exporting/i,
      );

      rerender(
        <QueryClientProvider client={qc}>
          <ExportPanel
            projectId="proj-1"
            spec={spec}
            stageCount={1}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );
      fireEvent.click(screen.getByRole("button", { name: /Export MP4/i }));
      await waitFor(() => {
        expect(screen.getByText(/succeeded/i)).toBeInTheDocument();
      });
    });
  });

  describe("StatisticsHud", () => {
    it("renders nothing when disabled or empty, and formats all metrics in metric and imperial units", () => {
      const spec = createDefaultRenderSpecV2();
      const state = makeEvaluatedState();

      const { container, rerender } = render(
        <StatisticsHud
          config={{ ...spec.statistics_panel, enabled: false }}
          units="metric"
          state={state}
        />,
      );
      expect(container.firstChild).toBeNull();

      rerender(
        <StatisticsHud
          config={{
            ...spec.statistics_panel,
            enabled: true,
            position: "top",
            time_zone: "UTC",
            metrics: [...ALL_STATISTIC_METRICS],
          }}
          units="metric"
          state={state}
        />,
      );

      const hud = screen.getByTestId("statistics-hud");
      expect(hud).toHaveAttribute("data-hud-position", "top");
      expect(screen.getByTestId("hud-activity-chip")).toHaveTextContent("Cycling");
      expect(screen.getByText("Day 2")).toBeInTheDocument();
      expect(screen.getByText("01:02:05")).toBeInTheDocument();
      expect(screen.getByText("30.6 km/h")).toBeInTheDocument();
      expect(screen.getByText("2121 m")).toBeInTheDocument();
      expect(screen.getAllByText("12.5 km")).toHaveLength(2);

      rerender(
        <StatisticsHud
          config={{
            ...spec.statistics_panel,
            enabled: true,
            position: "right",
            time_zone: "UTC",
            metrics: ["speed", "elevation", "distance-travelled"],
          }}
          units="imperial"
          state={state}
          compact
        />,
      );
      expect(screen.getByTestId("statistics-hud")).toHaveAttribute("data-hud-position", "right");
      expect(screen.getByText("19.0 mph")).toBeInTheDocument();
      expect(screen.getByText("6959 ft")).toBeInTheDocument();
      expect(screen.getByText("7.8 mi")).toBeInTheDocument();
    });
  });

  describe("PhotoStopsSection", () => {
    it("renders uploaded photo stops, surfaces 422 upload errors, and deletes a photo", async () => {
      const onPhotosMutated = vi.fn();
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/photos") && init?.method === "POST") {
          return new Response(JSON.stringify({ detail: "Unsupported image format" }), {
            status: 422,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (url.includes("/photos/p-1") && init?.method === "DELETE") {
          return new Response(null, { status: 204 });
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });

      const { container } = render(
        <PhotoStopsSection
          projectId="proj-1"
          photos={[
            {
              id: "p-1",
              project_id: "proj-1",
              stage_id: null,
              source_type: "upload",
              width: 640,
              height: 480,
              byte_size: 12000,
              caption: "Fictional Ridge Stage",
              taken_at_utc: "2001-06-14T10:00:00Z",
              lat: 0.2000,
              lon: 0.65,
              matched_lat: 0.2000,
              matched_lon: 0.65,
              matched_progress: 0.4,
              match_method: "gps",
              enabled: true,
              created_at: "2001-06-14T10:00:00Z",
            },
          ]}
          onPhotosMutated={onPhotosMutated}
        />,
      );

      expect(screen.getByTestId("photo-item-p-1")).toHaveTextContent("Fictional Ridge Stage");
      expect(screen.getByTestId("photo-item-p-1")).toHaveTextContent("gps");

      const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
      const badFile = new File(["not-an-image"], "bad.svg", { type: "image/svg+xml" });
      fireEvent.change(fileInput, { target: { files: [badFile] } });

      await waitFor(() => {
        expect(screen.getByRole("status")).toHaveTextContent(/Unsupported image format/i);
      });
      expect(screen.getByRole("list", { name: "Photo upload status" })).toHaveTextContent("bad.svg");

      fireEvent.click(screen.getByRole("button", { name: /Delete photo Fictional Ridge Stage/i }));
      await waitFor(() => {
        expect(onPhotosMutated).toHaveBeenCalled();
      });
    });

    it("supports Pin on route toggle and batch camera clock-shift dry_run preview + apply", async () => {
      const onTogglePinMode = vi.fn();
      const onPhotosMutated = vi.fn();
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/photos/shift-clock") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { dry_run?: boolean; offset_seconds?: number | null };
          return new Response(
            JSON.stringify({
              applied_offset_seconds: body.offset_seconds ?? 3600,
              dry_run: Boolean(body.dry_run),
              matched_before: 0,
              matched_after: 1,
              newly_matched_count: 1,
              photos: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({}), { status: 200 });
      });

      render(
        <PhotoStopsSection
          projectId="proj-1"
          photos={[
            {
              id: "p-1",
              project_id: "proj-1",
              stage_id: null,
              source_type: "upload",
              width: 640,
              height: 480,
              byte_size: 12000,
              caption: "Fictional Ridge Stage",
              taken_at_utc: "2001-06-14T10:00:00Z",
              lat: null,
              lon: null,
              matched_lat: null,
              matched_lon: null,
              matched_progress: null,
              match_method: "unmatched",
              enabled: true,
              created_at: "2001-06-14T10:00:00Z",
            },
          ]}
          pinModePhotoId={null}
          onTogglePinMode={onTogglePinMode}
          onPhotosMutated={onPhotosMutated}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /Pin on route/i }));
      expect(onTogglePinMode).toHaveBeenCalledWith("p-1");

      fireEvent.click(screen.getByRole("button", { name: /Camera clock shift/i }));
      fireEvent.click(screen.getByRole("button", { name: /Auto-detect clock offset/i }));

      expect(await screen.findByTestId("clock-shift-preview")).toHaveTextContent("0 → 1 matched");
      fireEvent.click(screen.getByRole("button", { name: /Apply clock shift/i }));

      await waitFor(() => {
        expect(onPhotosMutated).toHaveBeenCalled();
      });
      const applyCall = fetchSpy.mock.calls.find(
        ([u, req]) =>
          String(u).includes("/photos/shift-clock") &&
          String(req?.body).includes('"dry_run":false'),
      );
      expect(applyCall).toBeDefined();
    });
  });

  describe("Wave 2.2 Studio UI controls (Inspector, StoryTrackStrip, TelemetryChart, StageSidebar)", () => {
    it("supports new Route/Camera/HUD controls and excludes A7 Export UI", () => {
      const onUpdateSpec = vi.fn();
      const onUpdateHud = vi.fn();
      const spec = createDefaultRenderSpecV2();
      const qc = makeQueryClient();

      const { rerender } = render(
        <QueryClientProvider client={qc}>
          <InspectorPanel
            projectId="proj-1"
            section="route"
            spec={spec}
            stageCount={2}
            onSelectSection={vi.fn()}
            onUpdateSpec={onUpdateSpec}
            onUpdateHud={onUpdateHud}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      fireEvent.change(screen.getByLabelText("Map style"), { target: { value: "topo-relief" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ map_style: "topo-relief" });

      fireEvent.change(screen.getByLabelText("Color route by"), { target: { value: "grade" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ color_by: "grade" });

      fireEvent.change(screen.getByLabelText("Photo card style"), { target: { value: "polaroid" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ photo_card_style: "polaroid" });

      fireEvent.click(screen.getByLabelText(/Show stage chapter cards/i));
      expect(onUpdateSpec).toHaveBeenCalledWith({ show_chapter_cards: true });

      rerender(
        <QueryClientProvider client={qc}>
          <InspectorPanel
            projectId="proj-1"
            section="camera"
            spec={spec}
            stageCount={2}
            currentProgress={0.4}
            onSelectSection={vi.fn()}
            onUpdateSpec={onUpdateSpec}
            onUpdateHud={onUpdateHud}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      fireEvent.click(screen.getByRole("button", { name: /Expedition Doc/i }));
      expect(onUpdateSpec).toHaveBeenCalledWith(
        expect.objectContaining({ director_preset: "expedition-doc" }),
      );

      fireEvent.change(screen.getByLabelText("Terrain exaggeration"), { target: { value: "2.2" } });
      expect(onUpdateSpec).toHaveBeenCalledWith({ terrain_exaggeration: 2.2 });

      fireEvent.click(screen.getByLabelText(/3D elevation curtain/i));
      expect(onUpdateSpec).toHaveBeenCalledWith({ elevation_curtain: true });

      rerender(
        <QueryClientProvider client={qc}>
          <InspectorPanel
            projectId="proj-1"
            section="export"
            spec={spec}
            stageCount={2}
            onSelectSection={vi.fn()}
            onUpdateSpec={onUpdateSpec}
            onUpdateHud={onUpdateHud}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      expect(screen.queryByText(/Chroma/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/Daily Reels/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/Audio/i)).not.toBeInTheDocument();
    });

    it("formats grade, hr, power, and vam in StatisticsHud", () => {
      const spec = createDefaultRenderSpecV2();
      const state: EvaluatedTimelineState = {
        ...makeEvaluatedState(),
        hrBpm: 158,
        watts: 285,
        vamMPerH: 1120,
      };
      render(
        <StatisticsHud
          config={{
            ...spec.statistics_panel,
            enabled: true,
            metrics: ["grade", "hr", "power", "vam"],
          }}
          units="metric"
          state={state}
        />,
      );
      expect(screen.getByText("+6.2%")).toBeInTheDocument();
      expect(screen.getByText("158 bpm")).toBeInTheDocument();
      expect(screen.getByText("285 W")).toBeInTheDocument();
      expect(screen.getByText("1120 m/h")).toBeInTheDocument();
    });
  });
});
