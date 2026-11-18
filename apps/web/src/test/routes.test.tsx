// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { clearCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { createAppRouter } from "../router";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAtRoute(initialPath: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const history = createMemoryHistory({ initialEntries: [initialPath] });
  const router = createAppRouter(history);
  render(<App router={router} queryClient={queryClient} />);
  return { router, queryClient, history };
}

describe("Route tests: DashboardPage, LoginPage, ShareViewPage, StravaCallbackPage", () => {
  beforeEach(() => {
    clearCsrfToken();
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  describe("DashboardPage", () => {
    it("renders empty state, validates empty title, and creates a new project", async () => {
      let createdCalled = false;
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: true, strava_configured: false });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: {
              id: "u-1",
              email: "dev@trip-animator.local",
              display_name: "Developer",
              strava_connected: false,
            },
            csrf_token: "csrf-1",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects") && method === "GET") {
          return jsonResponse([]);
        }
        if (url.endsWith("/api/v1/projects") && method === "POST") {
          createdCalled = true;
          return jsonResponse(
            {
              id: "proj-new",
              title: "Fictional Meridian Route",
              revision: 1,
              stage_count: 0,
              total_distance_m: 0,
              settings: createDefaultRenderSpecV2(),
              stages: [],
              created_at: "2001-06-14T08:00:00Z",
              updated_at: "2001-06-14T08:00:00Z",
            },
            201,
          );
        }
        if (url.includes("/api/v1/projects/proj-new")) {
          return jsonResponse({
            id: "proj-new",
            title: "Fictional Meridian Route",
            revision: 1,
            stage_count: 0,
            total_distance_m: 0,
            settings: createDefaultRenderSpecV2(),
            stages: [],
            created_at: "2001-06-14T08:00:00Z",
            updated_at: "2001-06-14T08:00:00Z",
          });
        }
        if (url.includes("/telemetry/geometry")) {
          return jsonResponse({ type: "FeatureCollection", project_id: "proj-new", features: [] });
        }
        if (url.includes("/telemetry/chart")) {
          return jsonResponse({ project_id: "proj-new", total_distance_m: 0, points: [] });
        }
        if (url.includes("/kpis")) {
          return jsonResponse({
            project_id: "proj-new",
            stage_count: 0,
            total_distance_m: 0,
            total_elevation_gain_m: 0,
            total_moving_seconds: 0,
            stages: [],
          });
        }
        if (url.includes("/renders")) {
          return jsonResponse([]);
        }
        return jsonResponse({});
      });

      const { router } = renderAtRoute("/");
      expect(await screen.findByText(/No trip projects yet/i)).toBeInTheDocument();

      // Submitting empty title shows validation alert
      fireEvent.click(screen.getByRole("button", { name: /Create trip/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Enter a trip name to create a project/i,
      );

      // Creating a valid project navigates to editor
      fireEvent.change(screen.getByLabelText("New trip title"), {
        target: { value: "Fictional Meridian Route" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Create trip/i }));

      await waitFor(() => {
        expect(createdCalled).toBe(true);
        expect(router.state.location.pathname).toBe("/projects/proj-new");
      });
    });

    it("deletes a project after confirmation and surfaces API error banners", async () => {
      let projects = [
        {
          id: "proj-1",
          title: "Fictional Meridian Raid",
          revision: 1,
          stage_count: 2,
          total_distance_m: 50_000,
          settings: createDefaultRenderSpecV2(),
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        },
      ];
      let failDeleteOnce = true;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: false, strava_configured: false });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: {
              id: "u-1",
              email: "dev@trip-animator.local",
              display_name: "Developer",
              strava_connected: false,
            },
            csrf_token: "csrf-1",
            dev_auth_enabled: false,
          });
        }
        if (url.endsWith("/api/v1/projects") && method === "GET") {
          return jsonResponse(projects);
        }
        if (url.endsWith("/api/v1/projects/proj-1") && method === "DELETE") {
          if (failDeleteOnce) {
            failDeleteOnce = false;
            return jsonResponse({ detail: "Cannot delete locked project" }, 409);
          }
          projects = [];
          return new Response(null, { status: 204 });
        }
        return jsonResponse({});
      });

      renderAtRoute("/");
      expect(await screen.findByText("Fictional Meridian Raid")).toBeInTheDocument();

      // First click arms confirm, second click triggers DELETE
      fireEvent.click(screen.getByRole("button", { name: /Delete Fictional Meridian Raid/i }));
      fireEvent.click(screen.getByRole("button", { name: /^Confirm$/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/Cannot delete locked project/i);

      // Retry delete succeeds
      fireEvent.click(screen.getByRole("button", { name: /^Confirm$/i }));
      expect(await screen.findByText(/No trip projects yet/i)).toBeInTheDocument();
    });

    it("renders SVG route silhouettes on project cards and switches to Atlas tab with career & heatmap data", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: true, strava_configured: false });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: {
              id: "u-1",
              email: "dev@trip-animator.local",
              display_name: "Developer",
              strava_connected: false,
            },
            csrf_token: "csrf-1",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects")) {
          return jsonResponse([
            {
              id: "proj-atlas-1",
              title: "Fictional Meridian Circuit",
              revision: 2,
              stage_count: 3,
              total_distance_m: 112_000,
              settings: createDefaultRenderSpecV2(),
              created_at: "2001-06-14T08:00:00Z",
              updated_at: "2001-06-14T08:00:00Z",
            },
          ]);
        }
        if (url.includes("/api/v1/analytics/heatmap")) {
          return jsonResponse({
            trips: [
              {
                project_id: "proj-atlas-1",
                title: "Fictional Meridian Circuit",
                year: 2001,
                primary_activity: "cycling",
                distance_m: 112_000,
                elevation_gain_m: 3100,
                stage_count: 3,
                bbox: [0.1, 0.1, 0.3, 0.3],
                polylines: [[[0.1, 0.1], [0.22, 0.26], [0.3, 0.3]]],
              },
            ],
            hex_cells: [
              { h3_index: "fictional-cell", lat: 0.2, lon: 0.22, point_count: 150, stage_count: 2 },
            ],
          });
        }
        if (url.includes("/api/v1/analytics/career")) {
          return jsonResponse({
            total_projects: 1,
            total_stages: 3,
            total_distance_m: 112_000,
            total_elevation_gain_m: 3100,
            total_moving_seconds: 18_000,
            countries_visited: ["Fictionland", "Imaginary Isles"],
            by_year: [
              {
                year: 2001,
                project_count: 1,
                stage_count: 3,
                distance_m: 112_000,
                elevation_gain_m: 3100,
                moving_seconds: 18_000,
              },
            ],
            by_activity: [
              {
                activity_type: "cycling",
                stage_count: 3,
                distance_m: 112_000,
                elevation_gain_m: 3100,
                moving_seconds: 18_000,
              },
            ],
          });
        }
        return jsonResponse({});
      });

      renderAtRoute("/");
      expect(await screen.findByText("Fictional Meridian Circuit")).toBeInTheDocument();
      expect(screen.getByTestId("project-silhouette-proj-atlas-1")).toBeInTheDocument();
      expect(screen.getByText("+3100 m")).toBeInTheDocument();
      expect(screen.queryByTestId("atlas-view")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("tab", { name: "Atlas" }));
      expect(await screen.findByTestId("atlas-view")).toBeInTheDocument();
      expect(await screen.findByText("Fictionland")).toBeInTheDocument();
      expect(screen.getByText("Imaginary Isles")).toBeInTheDocument();
      expect(screen.getByText("05:00:00")).toBeInTheDocument();
    });
  });

  describe("LoginPage", () => {
    it("displays API validation error on failed login and redirects on success", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: true, strava_configured: false });
        }
        if (url.includes("/api/v1/auth/login") && method === "POST") {
          const body = JSON.parse(String(init?.body ?? "{}")) as { password?: string };
          if (body.password === "wrong") {
            return jsonResponse({ detail: "Invalid email or password" }, 401);
          }
          return jsonResponse({
            user: {
              id: "u-1",
              email: "rider@example.com",
              display_name: "Rider",
              strava_connected: false,
            },
            csrf_token: "csrf-ok",
            dev_auth_enabled: true,
          });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: {
              id: "u-1",
              email: "rider@example.com",
              display_name: "Rider",
              strava_connected: false,
            },
            csrf_token: "csrf-ok",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects")) {
          return jsonResponse([]);
        }
        return jsonResponse({});
      });

      const { router } = renderAtRoute("/login");
      expect(await screen.findByRole("heading", { name: /Sign in to workspace/i })).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText(/Email address/i), {
        target: { value: "rider@example.com" },
      });
      fireEvent.change(screen.getByLabelText(/Password/i), {
        target: { value: "wrong" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^Sign in$/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(/Invalid email or password/i);

      fireEvent.change(screen.getByLabelText(/Password/i), {
        target: { value: "correct-password" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^Sign in$/i }));

      await waitFor(() => {
        expect(router.state.location.pathname).toBe("/");
      });
    });
  });

  describe("ShareViewPage", () => {
    it("renders unavailable message for invalid share token", async () => {
      vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
        jsonResponse({ detail: "Share not found" }, 404),
      );

      renderAtRoute("/share/invalid-token");
      expect(await screen.findByRole("heading", { name: /Shared trip unavailable/i })).toBeInTheDocument();
    });

    it("toggles between Interactive Map and MP4 Film when rendered video is available", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/share/valid-slug/chart")) {
          return jsonResponse({ project_id: "p-1", total_distance_m: 1000, points: [] });
        }
        if (url.includes("/api/v1/share/valid-slug/stages/")) {
          return jsonResponse({
            type: "Feature",
            id: "s-1",
            geometry: {
              type: "MultiLineString",
              coordinates: [[[0.4512, 0.2173], [0.5325, 0.2537]]],
            },
            properties: {
              lod: "medium",
              point_count: 2,
              original_point_count: 2,
              segment_count: 1,
              bbox: [0.4512, 0.2173, 0.5325, 0.2537],
              crosses_antimeridian: false,
              stage_id: "s-1",
              name: "Day 1",
              day_index: 1,
              stage_order: 0,
              source_type: "gpx",
              activity_type: "cycling",
              distance_m: 1000,
              elevation_gain_m: 50,
            },
          });
        }
        if (url.includes("/api/v1/share/valid-slug")) {
          return jsonResponse({
            id: "p-1",
            title: "Shared Fictional Meridian Route",
            stage_count: 1,
            total_distance_m: 1000,
            settings: createDefaultRenderSpecV2(),
            has_rendered_video: true,
            stages: [
              {
                id: "s-1",
                name: "Day 1",
                day_index: 1,
                stage_order: 0,
                source_type: "gpx",
                activity_type: "cycling",
                distance_m: 1000,
                elevation_gain_m: 50,
                moving_seconds: 120,
                elapsed_seconds: 120,
                has_timestamps: true,
                started_at: "2001-06-14T08:00:00Z",
                trim_start_s: null,
                trim_end_s: null,
                point_count: 2,
              },
            ],
          });
        }
        return jsonResponse({});
      });

      renderAtRoute("/share/valid-slug");
      expect(await screen.findByText("Shared Fictional Meridian Route")).toBeInTheDocument();

      const mapTab = screen.getByRole("button", { name: /Interactive Map/i });
      const mp4Tab = screen.getByRole("button", { name: /MP4 Film/i });
      expect(mapTab).toHaveAttribute("aria-pressed", "true");
      expect(mp4Tab).toHaveAttribute("aria-pressed", "false");

      fireEvent.click(mp4Tab);
      expect(mp4Tab).toHaveAttribute("aria-pressed", "true");
      expect(mapTab).toHaveAttribute("aria-pressed", "false");
      expect(document.querySelector("video")).not.toBeNull();

      fireEvent.click(mapTab);
      expect(mapTab).toHaveAttribute("aria-pressed", "true");
    });

    it("renders hero KPIs, climb highlights, Copy embed button, and ?embed=1 chromeless shell", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/share/embed-slug/analytics/highlights")) {
          return jsonResponse({
            project_id: "p-1",
            climbs: [
              {
                id: "c-1",
                stage_id: "s-1",
                name: "Fictional Meridian Climb",
                category: "HC",
                start_dist_m: 1000,
                end_dist_m: 20_000,
                start_progress: 0.1,
                end_progress: 0.9,
                summit_progress: 0.9,
                summit_lat: 0.12,
                summit_lon: 0.18,
                summit_elev_m: 2758,
                distance_m: 19_000,
                elevation_gain_m: 1650,
                avg_grade_pct: 7.4,
                max_grade_pct: 12.1,
                fiets_score: 12.5,
              },
            ],
            splits: [],
            dwell_stops: [],
            peak_efforts: [],
            geo_context: [
              { stage_id: "s-1", countries: ["Fictionland"], regions: ["Meridian District"], passes: ["Equator Pass"], primary_country: "Fictionland" },
            ],
          });
        }
        if (url.includes("/api/v1/share/embed-slug/chart")) {
          return jsonResponse({ project_id: "p-1", total_distance_m: 25_000, points: [] });
        }
        if (url.includes("/api/v1/share/embed-slug/stages/")) {
          return jsonResponse({
            type: "Feature",
            id: "s-1",
            geometry: { type: "MultiLineString", coordinates: [[[0.1, 0.1], [0.18, 0.12]]] },
            properties: {
              lod: "medium",
              point_count: 2,
              original_point_count: 2,
              segment_count: 1,
              bbox: [0.1, 0.1, 0.18, 0.12],
              crosses_antimeridian: false,
              stage_id: "s-1",
              name: "Fictional Meridian Ascent",
              day_index: 1,
              stage_order: 0,
              source_type: "gpx",
              activity_type: "cycling",
              distance_m: 25_000,
              elevation_gain_m: 1650,
            },
          });
        }
        if (url.includes("/api/v1/share/embed-slug")) {
          return jsonResponse({
            id: "p-1",
            title: "Fictional Meridian Showcase",
            stage_count: 1,
            total_distance_m: 25_000,
            settings: createDefaultRenderSpecV2(),
            has_rendered_video: false,
            stages: [
              {
                id: "s-1",
                name: "Fictional Meridian Ascent",
                day_index: 1,
                stage_order: 0,
                source_type: "gpx",
                activity_type: "cycling",
                distance_m: 25_000,
                elevation_gain_m: 1650,
                moving_seconds: 5400,
                elapsed_seconds: 5600,
                has_timestamps: true,
                started_at: "2001-06-14T08:00:00Z",
                trim_start_s: null,
                trim_end_s: null,
                point_count: 2,
              },
            ],
          });
        }
        return jsonResponse({});
      });

      renderAtRoute("/share/embed-slug");
      expect(await screen.findByTestId("share-hero-kpis")).toHaveTextContent("25.0 km");
      expect(await screen.findByText("Fictional Meridian Climb")).toBeInTheDocument();
      expect(screen.getByText("HC")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /Copy embed/i }));
      expect(await screen.findByText(/Copied embed/i)).toBeInTheDocument();

      cleanup();
      window.history.replaceState({}, "", "/share/embed-slug?embed=1");
      renderAtRoute("/share/embed-slug?embed=1");
      expect(await screen.findByTestId("share-embed-shell")).toBeInTheDocument();
      expect(screen.queryByTestId("share-hero-kpis")).not.toBeInTheDocument();
    });
  });

  describe("StravaCallbackPage", () => {
    it("shows error message when Strava returns an error query param", async () => {
      window.history.replaceState({}, "", "/strava/callback?error=access_denied");
      renderAtRoute("/strava/callback");

      expect(await screen.findByText(/Strava authorization was denied/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Back to dashboard/i })).toBeInTheDocument();
    });

    it("completes Strava OAuth callback and redirects to dashboard", async () => {
      vi.useFakeTimers();
      window.history.replaceState({}, "", "/strava/callback?code=auth-123&state=st-456");

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/strava/callback")) {
          return jsonResponse({ connected: true });
        }
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: true, strava_configured: true });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: {
              id: "u-1",
              email: "dev@trip-animator.local",
              display_name: "Developer",
              strava_connected: true,
            },
            csrf_token: "csrf-1",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects")) {
          return jsonResponse([]);
        }
        return jsonResponse({});
      });

      const { router } = renderAtRoute("/strava/callback");
      await vi.waitFor(() => {
        expect(screen.getByText(/Strava connected! Redirecting to dashboard/i)).toBeInTheDocument();
      });

      await vi.advanceTimersByTimeAsync(750);
      expect(router.state.location.pathname).toBe("/");
    });
  });
});
