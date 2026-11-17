// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { TelemetryChart } from "../components/charts/TelemetryChart";
import { AuditLogDrawer } from "../components/editor/AuditLogDrawer";
import { ExportPanel } from "../components/editor/ExportPanel";
import { SharePopover } from "../components/editor/SharePopover";
import { StageSidebar } from "../components/editor/StageSidebar";
import { PhotoStopsSection } from "../components/editor/PhotoStopsSection";
import { StravaPickerModal } from "../components/editor/StravaPickerModal";
import { StoryTrackStrip } from "../components/charts/StoryTrackStrip";
import { clearCsrfToken, setCsrfToken } from "../lib/api/client";
import {
  createDefaultRenderSpecV2,
  type ProjectPhotoOut,
  type StageOut,
  type TelemetryGeometryFeature,
} from "../lib/api/types";
import {
  TELEMETRY_LEFT_AXIS_SIZE,
  TELEMETRY_RIGHT_AXIS_SIZE,
  buildTelemetryPlotOptions,
} from "../lib/charts/telemetry-options";
import { computeCameraFrame } from "../lib/geo/camera";
import { clampFitTrackPadding } from "../lib/geo/layout";
import { sanitizeMapStyle } from "../lib/geo/map-config";
import {
  evaluateTimelineSchedule,
  matchPhotosToTimeline,
  progressRatioToTimeRatio,
} from "../lib/geo/photos";
import { buildTimelineFromProject, evaluateTimeline } from "../lib/geo/timeline";
import { useEditorStore } from "../lib/state/editor-store";
import { createAppRouter } from "../router";

function mockJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

describe("Web UI & timeline fixes", () => {
  beforeEach(() => {
    setCsrfToken("csrf-test-token");
    useEditorStore.getState().resetPlayback();
    useEditorStore.getState().setPlaybackSpeed(1);
    useEditorStore.getState().setPreviewWatermark(false);
  });

  afterEach(() => {
    clearCsrfToken();
    vi.restoreAllMocks();
  });

  it("ExportPanel preserves spec.duration_seconds for 5s preview so holdScale stays proportional", async () => {
    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 30;
    spec.intro_hold_seconds = 3;
    spec.outro_hold_seconds = 3;
    spec.trail_duration_seconds = 6;

    let capturedBody: Record<string, unknown> | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.endsWith("/renders") && (!init?.method || init.method === "GET")) {
        return mockJsonResponse([]);
      }
      if (url.endsWith("/renders") && init?.method === "POST") {
        capturedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return mockJsonResponse({
          id: "job-preview-1",
          project_id: "proj-1",
          status: "queued",
          progress_pct: 0,
          spec: capturedBody,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        });
      }
      return mockJsonResponse({});
    });

    const qc = makeQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <ExportPanel
          projectId="proj-1"
          spec={spec}
          stageCount={1}
          onFlushSave={async () => true}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Preview clip \(5s\)/i }));

    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
    });
    expect(capturedBody).toMatchObject({
      is_preview: true,
      duration_seconds: 30,
      intro_hold_seconds: 3,
      outro_hold_seconds: 3,
      trail_duration_seconds: 6,
    });
  });

  it("EditorPage renders loading state and [role='alert'] error state on 404/network failure", async () => {
    let resolveProject!: (res: Response) => void;
    const pendingProject = new Promise<Response>((resolve) => {
      resolveProject = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/auth/me")) {
        return mockJsonResponse({
          user: {
            id: "u-1",
            email: "dev@trip-animator.local",
            display_name: "Developer",
            strava_connected: false,
          },
          csrf_token: "csrf-test",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/missing-proj")) {
        return pendingProject;
      }
      return mockJsonResponse({});
    });

    const qc = makeQueryClient();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/missing-proj"] }),
    );
    render(<App router={router} queryClient={qc} />);

    expect(await screen.findByText(/Loading project workspace/i)).toBeInTheDocument();

    resolveProject(mockJsonResponse({ detail: "Project not found" }, 404));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Project not found/i);
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Back to projects/i })).toHaveAttribute("href", "/");
  });

  it("EditorPage keeps responsive layout, accessible controls, and aria-pressed states", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/auth/me")) {
        return mockJsonResponse({
          user: {
            id: "u-1",
            email: "dev@trip-animator.local",
            display_name: "Developer",
            strava_connected: false,
          },
          csrf_token: "csrf-test",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-resp")) {
        return mockJsonResponse({
          id: "proj-resp",
          title: "Responsive Trip",
          revision: 1,
          stage_count: 0,
          total_distance_m: 0,
          settings: createDefaultRenderSpecV2(),
          stages: [],
          share_enabled: false,
          share_slug: null,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        });
      }
      if (url.includes("/telemetry/chart")) {
        return mockJsonResponse({ points: [], total_distance_m: 0 });
      }
      if (url.includes("/telemetry/kpis")) {
        return mockJsonResponse({
          total_distance_m: 0,
          total_elevation_gain_m: 0,
          total_moving_seconds: 0,
        });
      }
      if (url.endsWith("/renders")) {
        return mockJsonResponse([]);
      }
      return mockJsonResponse({});
    });

    const qc = makeQueryClient();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/proj-resp"] }),
    );
    const { container } = render(<App router={router} queryClient={qc} />);

    const titleInput = await screen.findByLabelText("Project title");
    expect(titleInput).not.toHaveClass("hidden");
    expect(screen.getByRole("button", { name: "Undo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Redo" })).toBeInTheDocument();

    const rootShell = container.firstElementChild;
    expect(rootShell).toHaveClass("min-h-screen", "lg:h-screen", "lg:overflow-hidden");

    const previewBtn = screen.getByRole("button", { name: /5s Clip Mode/i });
    expect(previewBtn).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(previewBtn);
    expect(previewBtn).toHaveAttribute("aria-pressed", "true");

    const speed1x = screen.getByRole("button", { name: "1×" });
    const speed2x = screen.getByRole("button", { name: "2×" });
    expect(speed1x).toHaveAttribute("aria-pressed", "true");
    expect(speed2x).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(speed2x);
    expect(speed2x).toHaveAttribute("aria-pressed", "true");
  });

  it("Router renders styled 404 NotFoundPage for unknown routes with link back to '/'", async () => {
    const qc = makeQueryClient();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/unknown/nonexistent-route"] }),
    );
    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("404")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Page not found/i })).toBeInTheDocument();
    const homeLink = screen.getByRole("link", { name: /Back to projects/i });
    expect(homeLink).toHaveAttribute("href", "/");
  });

  it("AuditLogDrawer closes on Escape key and backdrop click, not inner click", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockJsonResponse([]));
    const onClose = vi.fn();
    const qc = makeQueryClient();

    render(
      <QueryClientProvider client={qc}>
        <AuditLogDrawer projectId="proj-1" open={true} onClose={onClose} />
      </QueryClientProvider>,
    );

    const dialog = screen.getByRole("dialog", { name: /Project activity log/i });
    fireEvent.click(screen.getByText("Activity log"));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("SharePopover closes on Escape key and outside click, not inside click", () => {
    const qc = makeQueryClient();
    render(
      <QueryClientProvider client={qc}>
        <div>
          <span data-testid="outside-target">Outside</span>
          <SharePopover
            projectId="proj-1"
            shareEnabled={false}
            shareSlug={null}
            onShareChanged={() => {}}
          />
        </div>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Share/i }));
    expect(screen.getByRole("dialog", { name: /Public share link/i })).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByRole("dialog", { name: /Public share link/i }));
    expect(screen.getByRole("dialog", { name: /Public share link/i })).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByTestId("outside-target"));
    expect(screen.queryByRole("dialog", { name: /Public share link/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Share/i }));
    expect(screen.getByRole("dialog", { name: /Public share link/i })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: /Public share link/i })).not.toBeInTheDocument();
  });

  it("StravaPickerModal closes on Escape key and backdrop click", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockJsonResponse([]));
    const onClose = vi.fn();
    const qc = makeQueryClient();

    render(
      <QueryClientProvider client={qc}>
        <StravaPickerModal
          projectId="proj-1"
          units="metric"
          defaultDayIndex={1}
          onClose={onClose}
          onImported={() => {}}
        />
      </QueryClientProvider>,
    );

    const dialog = screen.getByRole("dialog", { name: /Import Strava activity/i });
    fireEvent.click(screen.getByText("Import Strava activities"));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("buildTimelineFromProject skips malformed geometry types and NaN/out-of-range coordinates", () => {
    const stages: StageOut[] = [
      {
        id: "s-valid",
        project_id: "p1",
        source_type: "gpx",
        external_source_id: null,
        strava_activity_id: null,
        name: "Valid with bad points",
        stage_order: 0,
        day_index: 1,
        distance_m: 1000,
        elevation_gain_m: 50,
        elevation_loss_m: 20,
        moving_seconds: 100,
        elapsed_seconds: 100,
        point_count: 4,
        has_timestamps: false,
        started_at: null,
        min_lat: 0.2173,
        max_lat: 0.2537,
        min_lon: 0.4512,
        max_lon: 0.5325,
        trim_start_s: null,
        trim_end_s: null,
        warnings: [],
        created_at: "2001-06-14T08:00:00Z",
      },
      {
        id: "s-bad-type",
        project_id: "p1",
        source_type: "gpx",
        external_source_id: null,
        strava_activity_id: null,
        name: "Wrong geometry type",
        stage_order: 1,
        day_index: 2,
        distance_m: 500,
        elevation_gain_m: 10,
        elevation_loss_m: 5,
        moving_seconds: 50,
        elapsed_seconds: 50,
        point_count: 1,
        has_timestamps: false,
        started_at: null,
        min_lat: 0.2173,
        max_lat: 0.2173,
        min_lon: 0.4512,
        max_lon: 0.4512,
        trim_start_s: null,
        trim_end_s: null,
        warnings: [],
        created_at: "2001-06-14T08:00:00Z",
      },
    ];

    const features: TelemetryGeometryFeature[] = [
      {
        type: "Feature",
        id: "s-valid",
        geometry: {
          type: "MultiLineString",
          coordinates: [
            [
              [0.4512, 0.2173],
              [Number.NaN, 0.2352],
              [0.5048, 95], // invalid latitude > 90
              [0.5325, 0.2537],
            ],
          ],
        },
        properties: {
          lod: "medium",
          point_count: 4,
          original_point_count: 4,
          segment_count: 1,
          bbox: [0.4512, 0.2173, 0.5325, 0.2537],
          crosses_antimeridian: false,
          stage_id: "s-valid",
          source_type: "gpx",
          name: "Valid with bad points",
          day_index: 1,
          stage_order: 0,
          distance_m: 1000,
          elevation_gain_m: 50,
        },
      },
      {
        type: "Feature",
        id: "s-bad-type",
        geometry: {
          type: "Point" as unknown as "MultiLineString",
          coordinates: [0.4512, 0.2173] as unknown as [number, number][][],
        },
        properties: {
          lod: "medium",
          point_count: 1,
          original_point_count: 1,
          segment_count: 1,
          bbox: [0.4512, 0.2173, 0.4512, 0.2173],
          crosses_antimeridian: false,
          stage_id: "s-bad-type",
          source_type: "gpx",
          name: "Wrong geometry type",
          day_index: 2,
          stage_order: 1,
          distance_m: 500,
          elevation_gain_m: 10,
        },
      },
    ];

    const timeline = buildTimelineFromProject(
      stages,
      features,
      undefined,
      createDefaultRenderSpecV2(),
    );

    expect(timeline[0]?.points).toHaveLength(2);
    expect(timeline[0]?.points.every((p) => Number.isFinite(p.lon) && Number.isFinite(p.lat))).toBe(true);
    expect(timeline[1]?.points).toHaveLength(0);
  });

  it("TelemetryChart captures pointer on pointerDown for smooth scrubbing", () => {
    const onSeek = vi.fn();
    const { container } = render(
      <TelemetryChart
        points={[
          { cum_dist_m: 0, elev_m: 100, speed_mps: 5, grade_pct: 0, hr_bpm: null, watts: null },
          { cum_dist_m: 1000, elev_m: 200, speed_mps: 6, grade_pct: 5, hr_bpm: null, watts: null },
        ]}
        totalDistanceM={1000}
        units="metric"
        progressRatio={0}
        onSeek={onSeek}
      />,
    );

    const scrubArea = container.querySelector(".select-none") as HTMLDivElement;
    const hostEl = scrubArea.querySelector(".cursor-ew-resize") as HTMLDivElement;
    expect(scrubArea).toBeTruthy();
    expect(hostEl).toBeTruthy();

    vi.spyOn(hostEl, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 200,
      bottom: 112,
      width: 200,
      height: 112,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    const setCapture = vi.fn();
    const hasCapture = vi.fn().mockReturnValue(true);
    const releaseCapture = vi.fn();
    scrubArea.setPointerCapture = setCapture;
    scrubArea.hasPointerCapture = hasCapture;
    scrubArea.releasePointerCapture = releaseCapture;

    fireEvent.pointerDown(scrubArea, { clientX: 50, pointerId: 7 });
    expect(setCapture).toHaveBeenCalledWith(7);
    expect(onSeek).toHaveBeenCalledWith(0.25);

    fireEvent.pointerMove(scrubArea, { clientX: 150, pointerId: 7 });
    expect(onSeek).toHaveBeenCalledWith(0.75);

    fireEvent.pointerUp(scrubArea, { pointerId: 7 });
    expect(releaseCapture).toHaveBeenCalledWith(7);
  });

  it("StageSidebar dropzone has an accessible aria-label", async () => {
    const qc = makeQueryClient();
    const rootRoute = createRootRoute({
      component: () => (
        <StageSidebar
          projectId="proj-1"
          stages={[]}
          spec={createDefaultRenderSpecV2()}
          selectedStageId={null}
          stravaConnected={false}
          onSelectStage={() => {}}
          onStagesMutated={async () => {}}
          onStageColorChange={() => {}}
          onStageTransitionChange={() => {}}
          onOpenStravaModal={() => {}}
        />
      ),
    });
    const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/" });
    const router = createRouter({
      routeTree: rootRoute.addChildren([indexRoute]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });

    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("button", { name: /Drop GPX files or click to browse/i }),
    ).toBeInTheDocument();
  });

  it("ShareViewPage exposes aria-pressed on speed, playback, and tab buttons", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.endsWith("/api/v1/share/test-slug")) {
        return mockJsonResponse({
          project_id: "proj-share",
          title: "Shared Fictional Tour",
          stage_count: 0,
          total_distance_m: 0,
          settings: createDefaultRenderSpecV2(),
          stages: [],
          has_rendered_video: true,
        });
      }
      if (url.includes("/telemetry/chart")) {
        return mockJsonResponse({ points: [], total_distance_m: 0 });
      }
      return mockJsonResponse({});
    });

    const qc = makeQueryClient();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/share/test-slug"] }),
    );
    render(<App router={router} queryClient={qc} />);

    const mapTab = await screen.findByRole("button", { name: /Interactive Map/i });
    const videoTab = screen.getByRole("button", { name: /MP4 Film/i });
    expect(mapTab).toHaveAttribute("aria-pressed", "true");
    expect(videoTab).toHaveAttribute("aria-pressed", "false");

    const playBtn = screen.getByRole("button", { name: /Play preview/i });
    expect(playBtn).toHaveAttribute("aria-pressed", "false");

    const speed4x = screen.getByRole("button", { name: "4×" });
    expect(speed4x).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(speed4x);
    expect(speed4x).toHaveAttribute("aria-pressed", "true");
  });

  it("StageSidebar cross-day reorder updates day_indices and collapses repetitive warnings", async () => {
    let capturedBody: Record<string, unknown> | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.endsWith("/stages/reorder") && init?.method === "POST") {
        capturedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return mockJsonResponse([]);
      }
      return mockJsonResponse({});
    });

    const stage1: StageOut = {
      id: "st-1",
      project_id: "proj-1",
      source_type: "gpx",
      external_source_id: null,
      strava_activity_id: null,
      name: "Stage One",
      stage_order: 0,
      day_index: 1,
      distance_m: 10000,
      elevation_gain_m: 200,
      elevation_loss_m: 100,
      moving_seconds: 1800,
      elapsed_seconds: 1900,
      point_count: 10,
      has_timestamps: true,
      started_at: null,
      min_lat: 0.15,
      max_lat: 0.3,
      min_lon: 0.2,
      max_lon: 0.3,
      trim_start_s: null,
      trim_end_s: null,
      warnings: [{ code: "DUP_COORD", message: "Collapsed consecutive duplicate coordinate" }],
      created_at: "2001-06-14T08:00:00Z",
    };
    const stage2: StageOut = {
      ...stage1,
      id: "st-2",
      name: "Stage Two",
      stage_order: 1,
      day_index: 2,
    };

    const qc = makeQueryClient();
    const rootRoute = createRootRoute({
      component: () => (
        <StageSidebar
          projectId="proj-1"
          stages={[stage1, stage2]}
          spec={createDefaultRenderSpecV2()}
          selectedStageId="st-1"
          stravaConnected={true}
          onSelectStage={() => {}}
          onStagesMutated={async () => {}}
          onStageColorChange={() => {}}
          onStageTransitionChange={() => {}}
          onOpenStravaModal={() => {}}
        />
      ),
    });
    const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/" });
    const router = createRouter({
      routeTree: rootRoute.addChildren([indexRoute]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });

    render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );

    // Repetitive warnings collapsed into one summary pill instead of 2 per-stage boxes
    expect(await screen.findByTestId("stage-warnings-summary")).toHaveTextContent(
      /Collapsed consecutive duplicate coordinate/i,
    );
    expect(screen.getAllByText(/Collapsed consecutive duplicate coordinate/i)).toHaveLength(1);
    expect(screen.getByText("Import from Strava")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Move Stage One down" }));
    await waitFor(() => {
      expect(capturedBody).not.toBeNull();
    });
    expect(capturedBody).toEqual({
      stage_ids: ["st-2", "st-1"],
      day_indices: { "st-1": 2, "st-2": 1 },
    });
  });

  it("computeCameraFrame keeps fit-track north-up without dynamic-3d 180° flip", () => {
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "fit-track";
    spec.camera_preset = "dynamic-3d";
    spec.camera_pitch_deg = 45;
    spec.camera_bearing_deg = 15;

    const stage: StageOut = {
      id: "s-south",
      project_id: "p1",
      source_type: "gpx",
      external_source_id: null,
      strava_activity_id: null,
      name: "Heading South",
      stage_order: 0,
      day_index: 1,
      distance_m: 10000,
      elevation_gain_m: 100,
      elevation_loss_m: 100,
      moving_seconds: 1000,
      elapsed_seconds: 1000,
      point_count: 2,
      has_timestamps: false,
      started_at: null,
      min_lat: -0.4,
      max_lat: 0.0,
      min_lon: 0.4,
      max_lon: 0.4,
      trim_start_s: null,
      trim_end_s: null,
      warnings: [],
      created_at: "2001-06-14T08:00:00Z",
    };
    const feat: TelemetryGeometryFeature = {
      type: "Feature",
      id: "s-south",
      geometry: {
        type: "MultiLineString",
        coordinates: [
          [
            [0.4, 0.0],
            [0.4, -0.4],
          ],
        ],
      },
      properties: {
        lod: "medium",
        point_count: 2,
        original_point_count: 2,
        segment_count: 1,
        bbox: [0.4, -0.4, 0.4, 0.0],
        crosses_antimeridian: false,
        stage_id: "s-south",
        name: "Heading South",
        day_index: 1,
        stage_order: 0,
        source_type: "gpx",
        distance_m: 10000,
        elevation_gain_m: 100,
      },
    };
    const timeline = buildTimelineFromProject([stage], [feat], undefined, spec);
    const evaluated = evaluateTimeline(timeline, spec, 0.5);
    const frame = computeCameraFrame(spec, evaluated, { courseBearingDeg: 180 });

    expect(frame.bearing).toBe(15);
    expect(frame.pitch).toBeLessThanOrEqual(25);
  });

  it("progressRatioToTimeRatio inverts evaluateTimelineSchedule under pause_on_photo", () => {
    const spec = createDefaultRenderSpecV2();
    spec.duration_seconds = 30;
    spec.intro_hold_seconds = 1;
    spec.outro_hold_seconds = 1;
    spec.show_photos = true;
    spec.pause_on_photo = true;
    spec.photo_display_seconds = 4;

    const stage: StageOut = {
      id: "s-p",
      project_id: "p1",
      source_type: "gpx",
      external_source_id: null,
      strava_activity_id: null,
      name: "Photo Stage",
      stage_order: 0,
      day_index: 1,
      distance_m: 10000,
      elevation_gain_m: 100,
      elevation_loss_m: 50,
      moving_seconds: 1000,
      elapsed_seconds: 1000,
      point_count: 2,
      has_timestamps: false,
      started_at: null,
      min_lat: 0.0,
      max_lat: 0.15,
      min_lon: 0.4,
      max_lon: 0.5,
      trim_start_s: null,
      trim_end_s: null,
      warnings: [],
      created_at: "2001-06-14T08:00:00Z",
    };
    const feat: TelemetryGeometryFeature = {
      type: "Feature",
      id: "s-p",
      geometry: {
        type: "MultiLineString",
        coordinates: [
          [
            [0.4, 0.0],
            [0.5, 0.15],
          ],
        ],
      },
      properties: {
        lod: "medium",
        point_count: 2,
        original_point_count: 2,
        segment_count: 1,
        bbox: [0.4, 0.0, 0.5, 0.15],
        crosses_antimeridian: false,
        stage_id: "s-p",
        name: "Photo Stage",
        day_index: 1,
        stage_order: 0,
        source_type: "gpx",
        distance_m: 10000,
        elevation_gain_m: 100,
      },
    };
    const timeline = buildTimelineFromProject([stage], [feat], undefined, spec);
    const photo: ProjectPhotoOut = {
      id: "ph-1",
      project_id: "p1",
      stage_id: "s-p",
      source_type: "upload",
      width: 800,
      height: 600,
      byte_size: 1000,
      caption: "Sunrise",
      taken_at_utc: null,
      lat: 48.05,
      lon: 19.05,
      matched_lat: 48.05,
      matched_lon: 19.05,
      matched_progress: 0.5,
      match_method: "gps",
      enabled: true,
      created_at: "2001-06-14T08:00:00Z",
    };
    const matched = matchPhotosToTimeline([photo], timeline, spec);
    const timeRatio = progressRatioToTimeRatio(matched, spec, 0.75, 30, false);
    const evaluated = evaluateTimelineSchedule(timeline, matched, spec, timeRatio * 30, 30, false);
    expect(evaluated.progressRatio).toBeCloseTo(0.75, 3);
  });

  it("telemetry plot options reserve generous Y-axis gutters for 1280x800 and ExportPanel merges liveJob into Render history", async () => {
    const opts = buildTelemetryPlotOptions(606, 108);
    expect(opts.axes?.[1]?.size).toBe(TELEMETRY_LEFT_AXIS_SIZE);
    expect(opts.axes?.[2]?.size).toBe(TELEMETRY_RIGHT_AXIS_SIZE);
    expect(TELEMETRY_LEFT_AXIS_SIZE).toBeGreaterThanOrEqual(56);
    expect(TELEMETRY_RIGHT_AXIS_SIZE).toBeGreaterThanOrEqual(60);

    const spec = createDefaultRenderSpecV2();
    let listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
    const origEventSource = globalThis.EventSource;
    class MockEventSource {
      public readonly url: string;
      constructor(url: string) {
        this.url = url;
      }
      addEventListener(type: string, cb: (e: MessageEvent) => void) {
        listeners[type] = listeners[type] ?? [];
        listeners[type].push(cb);
      }
      close() {}
      onerror: (() => void) | null = null;
    }
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;

    try {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.endsWith("/renders") && (!init?.method || init.method === "GET")) {
          return mockJsonResponse([
            {
              id: "job-live-1",
              project_id: "proj-1",
              status: "queued",
              progress_pct: 0,
              spec,
              created_at: "2001-06-14T08:00:00Z",
              updated_at: "2001-06-14T08:00:00Z",
            },
          ]);
        }
        return mockJsonResponse({});
      });

      const qc = makeQueryClient();
      render(
        <QueryClientProvider client={qc}>
          <ExportPanel
            projectId="proj-1"
            spec={spec}
            stageCount={1}
            onFlushSave={async () => true}
          />
        </QueryClientProvider>,
      );

      expect(await screen.findByText(/queued \(0%\)/i)).toBeInTheDocument();

      // Emit SSE status event at 42% and verify Render history updates in place
      const statusListeners = listeners["status"] ?? [];
      expect(statusListeners.length).toBeGreaterThan(0);
      for (const cb of statusListeners) {
        cb(
          new MessageEvent("status", {
            data: JSON.stringify({
              id: "job-live-1",
              project_id: "proj-1",
              status: "running",
              progress_pct: 42,
              spec,
              created_at: "2001-06-14T08:00:00Z",
              updated_at: "2001-06-14T08:00:02Z",
            }),
          }),
        );
      }

      expect(await screen.findByText(/running \(42%\)/i)).toBeInTheDocument();
      expect(screen.queryByText(/queued \(0%\)/i)).not.toBeInTheDocument();
    } finally {
      globalThis.EventSource = origEventSource;
      listeners = {};
    }
  });

  it("PhotoStopsSection shows HIDDEN badge for disabled photos and sanitizeMapStyle guards bare numeric getters", () => {
    const disabledPhoto: ProjectPhotoOut = {
      id: "ph-off",
      project_id: "proj-1",
      stage_id: null,
      source_type: "upload",
      width: 400,
      height: 300,
      byte_size: 500,
      caption: "Disabled Ridge",
      taken_at_utc: null,
      lat: null,
      lon: null,
      matched_lat: null,
      matched_lon: null,
      matched_progress: null,
      match_method: "unmatched",
      enabled: false,
      created_at: "2001-06-14T08:00:00Z",
    };

    render(
      <PhotoStopsSection
        projectId="proj-1"
        photos={[disabledPhoto]}
        matchedPhotos={[]}
      />,
    );

    expect(screen.getByTestId("photo-hidden-badge-ph-off")).toHaveTextContent("HIDDEN");
    expect(screen.queryByText(/unmatched/i)).not.toBeInTheDocument();

    const sanitized = sanitizeMapStyle({
      version: 8,
      sources: {},
      layers: [
        {
          id: "highway-shield-non-us",
          type: "symbol",
          source: "openmaptiles",
          filter: ["all", ["<=", ["get", "ref_length"], 6]],
        },
        {
          id: "building-3d",
          type: "fill-extrusion",
          source: "openmaptiles",
          paint: {
            "fill-extrusion-height": ["get", "render_height"],
            "fill-extrusion-base": ["get", "render_min_height"],
          },
        },
      ],
    } as never);

    expect(JSON.stringify(sanitized.layers[0])).toContain('"typeof"');
    expect(JSON.stringify(sanitized.layers[1])).toContain('"typeof"');
  });

  it("clampFitTrackPadding bounds fit padding <= 35% and EditorPage reads strava_connected accurately", async () => {
    const clamped = clampFitTrackPadding(
      { top: 120, bottom: 200, left: 100, right: 100 },
      640,
      360,
    );
    expect(clamped.left + clamped.right).toBeLessThanOrEqual(Math.floor(640 * 0.35));
    expect(clamped.top + clamped.bottom).toBeLessThanOrEqual(Math.floor(360 * 0.35));

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/auth/me")) {
        return mockJsonResponse({
          user: {
            id: "u-1",
            email: "dev@trip-animator.local",
            display_name: "Developer",
            strava_connected: true,
          },
          csrf_token: "csrf-test",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-strava")) {
        return mockJsonResponse({
          id: "proj-strava",
          title: "Strava Connected Trip",
          revision: 1,
          stage_count: 0,
          total_distance_m: 0,
          settings: createDefaultRenderSpecV2(),
          stages: [],
          share_enabled: false,
          share_slug: null,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        });
      }
      if (url.includes("/telemetry/chart")) {
        return mockJsonResponse({ points: [], total_distance_m: 0 });
      }
      if (url.includes("/telemetry/kpis")) {
        return mockJsonResponse({
          total_distance_m: 0,
          total_elevation_gain_m: 0,
          total_moving_seconds: 0,
        });
      }
      if (url.endsWith("/renders")) {
        return mockJsonResponse([]);
      }
      return mockJsonResponse({});
    });

    const qc = makeQueryClient();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/proj-strava"] }),
    );
    render(<App router={router} queryClient={qc} />);

    expect(await screen.findByText("Import from Strava")).toBeInTheDocument();
    expect(screen.queryByText(/connect on Dashboard/i)).not.toBeInTheDocument();
  });

  it("TelemetryChart supports HR/Watts toggles and Scissors trim brush; StoryTrackStrip adds camera cues", () => {
    const onSeek = vi.fn();
    const onTrimBrush = vi.fn();
    const onUpdateKeyframes = vi.fn();

    const { container } = render(
      <div>
        <StoryTrackStrip
          stages={[]}
          totalDistanceM={1000}
          progressRatio={0.25}
          cameraKeyframes={[]}
          onSeek={onSeek}
          onUpdateKeyframes={onUpdateKeyframes}
        />
        <TelemetryChart
          points={[
            { cum_dist_m: 0, elev_m: 100, speed_mps: 5, grade_pct: 2, hr_bpm: 135, watts: 210 },
            { cum_dist_m: 1000, elev_m: 200, speed_mps: 6, grade_pct: 6, hr_bpm: 165, watts: 310 },
          ]}
          totalDistanceM={1000}
          units="metric"
          progressRatio={0.5}
          onSeek={onSeek}
          onTrimBrush={onTrimBrush}
        />
      </div>,
    );

    fireEvent.click(screen.getByRole("button", { name: /Cue at 25%/i }));
    expect(onUpdateKeyframes).toHaveBeenCalledWith(
      expect.arrayContaining([expect.objectContaining({ progress: 0.25 })]),
    );

    const hrBtn = screen.getByRole("button", { name: "HR" });
    const wattsBtn = screen.getByRole("button", { name: "Watts" });
    fireEvent.click(hrBtn);
    fireEvent.click(wattsBtn);
    expect(hrBtn).toHaveAttribute("aria-pressed", "true");
    expect(wattsBtn).toHaveAttribute("aria-pressed", "true");

    const trimBtn = screen.getByRole("button", { name: /Trim brush/i });
    fireEvent.click(trimBtn);
    expect(trimBtn).toHaveAttribute("aria-pressed", "true");

    const scrubArea = container.querySelector('[data-testid="telemetry-chart-dock"] .select-none') as HTMLDivElement;
    const hostEl = scrubArea.querySelector(".cursor-crosshair") as HTMLDivElement;
    vi.spyOn(hostEl, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 200,
      bottom: 112,
      width: 200,
      height: 112,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    fireEvent.pointerDown(scrubArea, { clientX: 40, pointerId: 1 });
    fireEvent.pointerMove(scrubArea, { clientX: 160, pointerId: 1 });
    fireEvent.pointerUp(scrubArea, { clientX: 160, pointerId: 1 });
    expect(onTrimBrush).toHaveBeenCalledWith(200, 800);
  });
});
