import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { clearCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2, type ProjectDetailOut } from "../lib/api/types";
import { useEditorStore } from "../lib/state/editor-store";
import { createAppRouter } from "../router";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeProjectDetail(overrides?: Partial<ProjectDetailOut>): ProjectDetailOut {
  return {
    id: "proj-1",
    title: "Fictional Meridian Loop",
    revision: 3,
    stage_count: 2,
    total_distance_m: 42_000,
    settings: createDefaultRenderSpecV2(),
    created_at: "2001-06-14T08:00:00Z",
    updated_at: "2001-06-14T08:00:00Z",
    stages: [
      {
        id: "stage-1",
        project_id: "proj-1",
        source_type: "gpx",
        activity_type: "cycling",
        external_source_id: null,
        strava_activity_id: null,
        name: "Day 1 Climb",
        stage_order: 0,
        day_index: 1,
        trim_start_s: null,
        trim_end_s: null,
        started_at: "2001-06-14T08:00:00Z",
        point_count: 10,
        distance_m: 20_000,
        elevation_gain_m: 900,
        elevation_loss_m: 200,
        moving_seconds: 3600,
        elapsed_seconds: 3800,
        has_timestamps: true,
        min_lat: 0.2173,
        max_lat: 0.3128,
        min_lon: 0.4512,
        max_lon: 0.5897,
        warnings: [{ code: "MISSING_HR", message: "No heart rate stream found" }],
        created_at: "2001-06-14T08:00:00Z",
      },
      {
        id: "stage-2",
        project_id: "proj-1",
        source_type: "strava",
        activity_type: "gravel_mtb",
        external_source_id: "999",
        strava_activity_id: 999,
        name: "Day 2 Descent",
        stage_order: 1,
        day_index: 2,
        trim_start_s: null,
        trim_end_s: null,
        started_at: "2001-06-15T08:00:00Z",
        point_count: 10,
        distance_m: 22_000,
        elevation_gain_m: 400,
        elevation_loss_m: 1100,
        moving_seconds: 3000,
        elapsed_seconds: 3200,
        has_timestamps: true,
        min_lat: 0.3128,
        max_lat: 0.3552,
        min_lon: 0.5897,
        max_lon: 0.7113,
        warnings: [],
        created_at: "2001-06-15T08:00:00Z",
      },
    ],
    ...overrides,
  };
}

describe("Login, Editor state, Stage reorder & Revision Conflict handling", () => {
  beforeEach(() => {
    clearCsrfToken();
    useEditorStore.getState().resetEditor();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("hides dev login button when public-config returns dev_auth_enabled=false", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({ dev_auth_enabled: false, strava_configured: false });
      }
      return jsonResponse({}, 404);
    });

    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/login"] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByRole("heading", { name: "Sign in to workspace" })).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Development one-click sign in" }),
      ).not.toBeInTheDocument();
    });
  });

  it("shows dev login button only when public-config returns dev_auth_enabled=true", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({ dev_auth_enabled: true, strava_configured: true });
      }
      return jsonResponse({}, 404);
    });

    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/login"] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(
      await screen.findByRole("button", { name: "Development one-click sign in" }),
    ).toBeInTheDocument();
  });

  it("loads project, reorders stages, and surfaces 409 revision conflict banner", async () => {
    const project = makeProjectDetail();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/auth/me")) {
        return jsonResponse({
          user: { id: "u1", email: "dev@trip.local", display_name: "Dev", strava_connected: true },
          csrf_token: "csrf-editor",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-1") && method === "GET") {
        return jsonResponse(project);
      }
      if (url.includes("/stages/stage-1/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-1",
          geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 10,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5897, 0.3128],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Day 1 Climb",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 20_000,
            elevation_gain_m: 900,
          },
        });
      }
      if (url.includes("/stages/stage-2/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-2",
          geometry: { type: "MultiLineString", coordinates: [[[0.5897, 0.3128], [0.7113, 0.3552]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 10,
            segment_count: 1,
            bbox: [0.5897, 0.3128, 0.7113, 0.3552],
            crosses_antimeridian: false,
            stage_id: "stage-2",
            name: "Day 2 Descent",
            day_index: 2,
            stage_order: 1,
            source_type: "strava",
            activity_type: "gravel_mtb",
            distance_m: 22_000,
            elevation_gain_m: 400,
          },
        });
      }
      if (url.includes("/telemetry/geometry")) {
        return jsonResponse({
          type: "FeatureCollection",
          project_id: "proj-1",
          features: [
            {
              type: "Feature",
              id: "stage-1",
              geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
              properties: {
                lod: "medium",
                point_count: 2,
                original_point_count: 10,
                segment_count: 1,
                bbox: [0.4512, 0.2173, 0.5897, 0.3128],
                crosses_antimeridian: false,
                stage_id: "stage-1",
                name: "Day 1 Climb",
                day_index: 1,
                stage_order: 0,
                source_type: "gpx",
                distance_m: 20_000,
                elevation_gain_m: 900,
              },
            },
          ],
        });
      }
      if (url.includes("/telemetry/chart")) {
        return jsonResponse({
          project_id: "proj-1",
          total_distance_m: 42_000,
          points: [
            { cum_dist_m: 0, elev_m: 1200, speed_mps: 5, grade_pct: 2, hr_bpm: null, watts: null },
            { cum_dist_m: 42000, elev_m: 1800, speed_mps: 6, grade_pct: 4, hr_bpm: null, watts: null },
          ],
        });
      }
      if (url.endsWith("/kpis")) {
        return jsonResponse({
          project_id: "proj-1",
          stage_count: 2,
          total_distance_m: 42_000,
          total_elevation_gain_m: 1300,
          total_moving_seconds: 6600,
          stages: [],
        });
      }
      if (url.endsWith("/stages/reorder") && method === "POST") {
        return jsonResponse([project.stages[1], project.stages[0]]);
      }
      if (url.endsWith("/api/v1/projects/proj-1") && method === "PATCH") {
        return jsonResponse(
          {
            type: "about:blank",
            title: "Revision conflict",
            detail: "Revision conflict: expected 4, got 3",
            status: 409,
          },
          409,
        );
      }
      return jsonResponse([], 200);
    });

    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/proj-1"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByText("Day 1 Climb")).toBeInTheDocument();
    expect(screen.getByText("No heart rate stream found")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Move Day 1 Climb down" }));
    await waitFor(() => {
      const reorderCall = fetchSpy.mock.calls.find(([u]) =>
        String(u).endsWith("/stages/reorder"),
      );
      expect(reorderCall).toBeDefined();
      const body = JSON.parse(String(reorderCall?.[1]?.body));
      expect(body.stage_ids).toEqual(["stage-2", "stage-1"]);
    });

    fireEvent.change(screen.getByLabelText("Project title"), {
      target: { value: "Fictional Meridian Edited" },
    });
    const saveBtn = screen.getByRole("button", { name: /Unsaved/i });
    fireEvent.click(saveBtn);

    expect(
      await screen.findByText("Revision conflict: expected 4, got 3"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload server version" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Overwrite with my edits" })).toBeInTheDocument();
  });

  it("retries failed per-stage geometry, supports activity type selection, undo/redo, keyboard shortcuts, and audit drawer", async () => {
    const project = makeProjectDetail();
    let stage2Attempts = 0;

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/auth/me")) {
        return jsonResponse({
          user: { id: "u1", email: "dev@trip.local", display_name: "Dev", strava_connected: true },
          csrf_token: "csrf-editor-2",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-1") && method === "GET") {
        return jsonResponse(project);
      }
      if (url.includes("/stages/stage-1/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-1",
          geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 10,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5897, 0.3128],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Day 1 Climb",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 20_000,
            elevation_gain_m: 900,
          },
        });
      }
      if (url.includes("/stages/stage-2/geometry")) {
        stage2Attempts += 1;
        if (stage2Attempts <= 3) {
          return jsonResponse({ detail: "Temporary S3 error" }, 500);
        }
        return jsonResponse({
          type: "Feature",
          id: "stage-2",
          geometry: { type: "MultiLineString", coordinates: [[[0.5897, 0.3128], [0.7113, 0.3552]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 10,
            segment_count: 1,
            bbox: [0.5897, 0.3128, 0.7113, 0.3552],
            crosses_antimeridian: false,
            stage_id: "stage-2",
            name: "Day 2 Descent",
            day_index: 2,
            stage_order: 1,
            source_type: "strava",
            activity_type: "gravel_mtb",
            distance_m: 22_000,
            elevation_gain_m: 400,
          },
        });
      }
      if (url.includes("/stages/stage-1") && method === "PATCH") {
        const body = JSON.parse(String(init?.body)) as { activity_type?: string };
        return jsonResponse({ ...project.stages[0], activity_type: body.activity_type ?? "hiking" });
      }
      if (url.includes("/projects/proj-1/audit")) {
        return jsonResponse([
          {
            id: 1,
            project_id: "proj-1",
            event_type: "stage.gpx_uploaded",
            metadata: { stage_name: "Day 1 Climb" },
            created_at: "2001-06-14T08:05:00Z",
          },
        ]);
      }
      if (url.includes("/telemetry/chart")) {
        return jsonResponse({ project_id: "proj-1", total_distance_m: 42_000, points: [] });
      }
      if (url.endsWith("/kpis")) {
        return jsonResponse({
          project_id: "proj-1",
          stage_count: 2,
          total_distance_m: 42_000,
          total_elevation_gain_m: 1300,
          total_moving_seconds: 6600,
          stages: [],
        });
      }
      return jsonResponse([], 200);
    });

    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/proj-1"] }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retryDelay: 1 } },
    });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByText("Day 1 Climb")).toBeInTheDocument();

    const errorBanner = await screen.findByTestId("geometry-error-banner", {}, { timeout: 3000 });
    expect(errorBanner).toHaveTextContent("1 stage geometry failed to load.");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(screen.queryByTestId("geometry-error-banner")).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Hiking / Trekking" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply stage changes" }));
    await waitFor(() => {
      const patchCall = fetchSpy.mock.calls.find(
        ([u, req]) => String(u).endsWith("/stages/stage-1") && req?.method === "PATCH",
      );
      expect(patchCall).toBeDefined();
      const body = JSON.parse(String(patchCall?.[1]?.body)) as { activity_type: string };
      expect(body.activity_type).toBe("hiking");
    });

    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(useEditorStore.getState().currentTimeRatio).toBeCloseTo(0.02, 4);
    const titleInput = screen.getByLabelText("Project title");
    fireEvent.keyDown(titleInput, { key: "ArrowRight" });
    expect(useEditorStore.getState().currentTimeRatio).toBeCloseTo(0.02, 4);
    fireEvent.keyDown(window, { key: "End" });
    expect(useEditorStore.getState().currentTimeRatio).toBe(1);
    fireEvent.keyDown(window, { key: "Home" });
    expect(useEditorStore.getState().currentTimeRatio).toBe(0);

    fireEvent.change(titleInput, { target: { value: "Fictional Meridian Revised" } });
    expect(screen.getByLabelText("Project title")).toHaveValue("Fictional Meridian Revised");
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(screen.getByLabelText("Project title")).toHaveValue("Fictional Meridian Loop");
    fireEvent.click(screen.getByRole("button", { name: "Redo" }));
    expect(screen.getByLabelText("Project title")).toHaveValue("Fictional Meridian Revised");

    fireEvent.click(screen.getByRole("button", { name: "Activity log" }));
    expect(await screen.findByText("Stage Gpx Uploaded")).toBeInTheDocument();
  });

  it("forwards Strava date filters, shows Already in trip badge, and batch imports selected activities", async () => {
    const project = makeProjectDetail();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/auth/me")) {
        return jsonResponse({
          user: { id: "u1", email: "dev@trip.local", display_name: "Dev", strava_connected: true },
          csrf_token: "csrf-strava",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-1") && method === "GET") {
        return jsonResponse(project);
      }
      if (url.includes("/stages/") && url.includes("/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-1",
          geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 10,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5897, 0.3128],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Stage",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 20_000,
            elevation_gain_m: 900,
          },
        });
      }
      if (url.includes("/api/v1/strava/activities")) {
        return jsonResponse([
          {
            id: 999,
            name: "Day 2 Descent",
            sport_type: "GravelRide",
            start_date: "2001-06-15T08:00:00Z",
            distance_m: 22_000,
            elevation_gain_m: 400,
            moving_time_s: 3000,
            has_gps: true,
          },
          {
            id: 1001,
            name: "Equator Pass Pordoi Morning Ride",
            sport_type: "Ride",
            start_date: "2001-06-16T08:00:00Z",
            distance_m: 45_000,
            elevation_gain_m: 1200,
            moving_time_s: 5400,
            has_gps: true,
            summary_polyline: "_pR_pR_|B_yF",
          },
          {
            id: 1002,
            name: "Zwift Recovery Spin",
            sport_type: "VirtualRide",
            start_date: "2001-06-17T08:00:00Z",
            distance_m: 20_000,
            elevation_gain_m: 150,
            moving_time_s: 2400,
            has_gps: true,
          },
        ]);
      }
      if (url.endsWith("/import-strava/batch") && method === "POST") {
        return jsonResponse({
          imported: [{ ...project.stages[1], id: "stage-3", strava_activity_id: 1001, name: "Equator Pass Pordoi Morning Ride" }],
          failed: [],
        });
      }
      if (url.includes("/telemetry/chart")) {
        return jsonResponse({ project_id: "proj-1", total_distance_m: 42_000, points: [] });
      }
      if (url.endsWith("/kpis")) {
        return jsonResponse({
          project_id: "proj-1",
          stage_count: 2,
          total_distance_m: 42_000,
          total_elevation_gain_m: 1300,
          total_moving_seconds: 6600,
          stages: [],
        });
      }
      return jsonResponse([], 200);
    });

    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/proj-1"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    fireEvent.click(await screen.findByRole("button", { name: /Import from Strava/i }));
    expect(await screen.findByText("Already in trip")).toBeInTheDocument();
    expect(screen.getByText("Equator Pass Pordoi Morning Ride")).toBeInTheDocument();
    expect(screen.getByTestId("strava-sparkline-1001")).toBeInTheDocument();
    expect(screen.queryByText("Zwift Recovery Spin")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2001-06-01" } });
    fireEvent.change(screen.getByLabelText("To date"), { target: { value: "2001-06-30" } });

    await waitFor(() => {
      const stravaCall = fetchSpy.mock.calls.find(([u]) =>
        String(u).includes("after=2001-06-01") && String(u).includes("before=2001-06-30"),
      );
      expect(stravaCall).toBeDefined();
    });

    fireEvent.click(await screen.findByLabelText("Select Equator Pass Pordoi Morning Ride"));
    fireEvent.click(screen.getByRole("button", { name: /Import 1 selected/i }));

    await waitFor(() => {
      const batchCall = fetchSpy.mock.calls.find(([u]) =>
        String(u).endsWith("/import-strava/batch"),
      );
      expect(batchCall).toBeDefined();
      const body = JSON.parse(String(batchCall?.[1]?.body)) as {
        items: { activity_id: number; activity_type: string }[];
      };
      expect(body.items[0]?.activity_id).toBe(1001);
      expect(body.items[0]?.activity_type).toBe("cycling");
    });
  });

  it("queues bulk import-all from StravaPickerModal with active date and GPS filters", async () => {
    const project = makeProjectDetail();
    let importAllBody: Record<string, unknown> | null = null;
    let projectFetchCount = 0;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";

      if (url.endsWith("/api/v1/auth/me")) {
        return jsonResponse({
          user: { id: "u1", email: "dev@trip.local", display_name: "Dev", strava_connected: true },
          csrf_token: "csrf-bulk",
          dev_auth_enabled: true,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-1") && method === "GET") {
        projectFetchCount += 1;
        return jsonResponse(project);
      }
      if (url.includes("/stages/") && url.includes("/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-1",
          geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 10,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5897, 0.3128],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Stage",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 20_000,
            elevation_gain_m: 900,
          },
        });
      }
      if (url.includes("/api/v1/strava/activities")) {
        return jsonResponse([
          {
            id: 1001,
            name: "Fictional Crest Ride",
            sport_type: "Ride",
            start_date: "2001-06-16T08:00:00Z",
            distance_m: 45_000,
            elevation_gain_m: 1200,
            moving_time_s: 5400,
            has_gps: true,
          },
        ]);
      }
      if (url.includes("/api/v1/strava/import-all") && method === "POST") {
        importAllBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return jsonResponse({
          queued_count: 3,
          skipped_existing_count: 1,
          skipped_no_gps_count: 0,
          total_scanned: 4,
          project_id: "proj-1",
          project_title: "Fictional Meridian Loop",
          queued_activity_ids: [1001, 1002, 1003],
        });
      }
      if (url.includes("/telemetry/chart")) {
        return jsonResponse({ project_id: "proj-1", total_distance_m: 42_000, points: [] });
      }
      if (url.endsWith("/kpis")) {
        return jsonResponse({
          project_id: "proj-1",
          stage_count: 2,
          total_distance_m: 42_000,
          total_elevation_gain_m: 1300,
          total_moving_seconds: 6600,
          stages: [],
        });
      }
      return jsonResponse([], 200);
    });

    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/proj-1"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    fireEvent.click(await screen.findByRole("button", { name: /Import from Strava/i }));
    expect(await screen.findByText("Fictional Crest Ride")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2001-06-01" } });
    fireEvent.change(screen.getByLabelText("To date"), { target: { value: "2001-06-30" } });

    const fetchCountBefore = projectFetchCount;
    fireEvent.click(screen.getByTestId("strava-picker-import-all"));

    const notice = await screen.findByTestId("strava-bulk-import-notice");
    expect(notice).toHaveTextContent("Queued 3 activities");
    expect(notice).toHaveTextContent("1 already imported");
    expect(notice).toHaveTextContent("4 scanned");
    expect(importAllBody).toEqual({
      project_id: "proj-1",
      max_activities: 200,
      after: "2001-06-01",
      before: "2001-06-30",
      gps_only: true,
    });
    await waitFor(() => {
      expect(projectFetchCount).toBeGreaterThan(fetchCountBefore);
    });
  });

  it("shows the trail data date in the shared playback attribution while the overlay is on", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({ marked_trails_base_url: "/tiles/marked-trails" });
      }
      if (url.endsWith("/tiles/marked-trails/current.json")) {
        return jsonResponse({
          current_build_id: "2026-11-27",
          pmtiles_key: "builds/2026-11-27/marked-trails.pmtiles",
          osm_data_date: "2026-11-26T21:21:00Z",
        });
      }
      if (url.endsWith("/api/v1/share/active-slug")) {
        return jsonResponse({
          id: "proj-1",
          title: "Shared Fictional Meridian Tour",
          stage_count: 1,
          total_distance_m: 20_000,
          settings: { ...createDefaultRenderSpecV2(), marked_trail_types: ["hiking"] },
          stages: [
            {
              id: "stage-1",
              project_id: "proj-1",
              source_type: "gpx",
              activity_type: "cycling",
              name: "Shared Stage 1",
              stage_order: 0,
              day_index: 1,
              trim_start_s: null,
              trim_end_s: null,
              started_at: "2001-06-14T08:00:00Z",
              point_count: 2,
              distance_m: 20_000,
              elevation_gain_m: 900,
              elevation_loss_m: 200,
              moving_seconds: 3600,
              elapsed_seconds: 3800,
              has_timestamps: true,
              min_lat: 0.2173,
              max_lat: 0.3128,
              min_lon: 0.4512,
              max_lon: 0.5897,
              created_at: "2001-06-14T08:00:00Z",
            },
          ],
          has_rendered_video: false,
          latest_render_id: null,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        });
      }
      if (url.includes("/api/v1/share/active-slug/stages/stage-1/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-1",
          geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 2,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5897, 0.3128],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Shared Stage 1",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 20_000,
            elevation_gain_m: 900,
          },
        });
      }
      if (url.includes("/api/v1/share/active-slug/chart")) {
        return jsonResponse({ project_id: "proj-1", total_distance_m: 20_000, points: [] });
      }
      if (url.endsWith("/api/v1/share/disabled-slug")) {
        return jsonResponse({ detail: "Not found" }, 404);
      }
      return jsonResponse({}, 404);
    });

    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/share/active-slug"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    await screen.findByText("Shared Fictional Meridian Tour");
    await waitFor(() =>
      expect(screen.getByTestId("map-attribution")).toHaveTextContent(
        "© OpenStreetMap contributors · Marked Trails data: 2026-11-26",
      ),
    );
  });

  it("renders public share view (/share/:slug) and shows unavailable message on 404", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.endsWith("/api/v1/share/active-slug")) {
        return jsonResponse({
          id: "proj-1",
          title: "Shared Fictional Meridian Tour",
          stage_count: 1,
          total_distance_m: 20_000,
          settings: createDefaultRenderSpecV2(),
          stages: [
            {
              id: "stage-1",
              project_id: "proj-1",
              source_type: "gpx",
              activity_type: "cycling",
              name: "Shared Stage 1",
              stage_order: 0,
              day_index: 1,
              trim_start_s: null,
              trim_end_s: null,
              started_at: "2001-06-14T08:00:00Z",
              point_count: 2,
              distance_m: 20_000,
              elevation_gain_m: 900,
              elevation_loss_m: 200,
              moving_seconds: 3600,
              elapsed_seconds: 3800,
              has_timestamps: true,
              min_lat: 0.2173,
              max_lat: 0.3128,
              min_lon: 0.4512,
              max_lon: 0.5897,
              created_at: "2001-06-14T08:00:00Z",
            },
          ],
          has_rendered_video: false,
          latest_render_id: null,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        });
      }
      if (url.includes("/api/v1/share/active-slug/stages/stage-1/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "stage-1",
          geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5897, 0.3128]]] },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 2,
            segment_count: 1,
            bbox: [0.4512, 0.2173, 0.5897, 0.3128],
            crosses_antimeridian: false,
            stage_id: "stage-1",
            name: "Shared Stage 1",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            activity_type: "cycling",
            distance_m: 20_000,
            elevation_gain_m: 900,
          },
        });
      }
      if (url.includes("/api/v1/share/active-slug/chart")) {
        return jsonResponse({ project_id: "proj-1", total_distance_m: 20_000, points: [] });
      }
      if (url.endsWith("/api/v1/share/disabled-slug")) {
        return jsonResponse({ detail: "Not found" }, 404);
      }
      return jsonResponse({}, 404);
    });

    const activeRouter = createAppRouter(
      createMemoryHistory({ initialEntries: ["/share/active-slug"] }),
    );
    const qc1 = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = render(<App router={activeRouter} queryClient={qc1} />);

    expect(await screen.findByText("Shared Fictional Meridian Tour")).toBeInTheDocument();
    expect(screen.getByText("Shared Stage 1")).toBeInTheDocument();
    expect(screen.getByText("Shared Trip")).toBeInTheDocument();
    unmount();

    const disabledRouter = createAppRouter(
      createMemoryHistory({ initialEntries: ["/share/disabled-slug"] }),
    );
    const qc2 = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={disabledRouter} queryClient={qc2} />);
    expect(await screen.findByText("Shared trip unavailable")).toBeInTheDocument();
  });
});
