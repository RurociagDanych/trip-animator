import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { clearCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { createAppRouter } from "../router";

vi.mock("../components/dashboard/ActivityLibraryView", () => ({
  ActivityLibraryView: () => <div>Activity catalog</div>,
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAtRoute(initialPath: string, options?: { strict?: boolean }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const history = createMemoryHistory({ initialEntries: [initialPath] });
  const router = createAppRouter(history);
  const app = <App router={router} queryClient={queryClient} />;
  render(options?.strict ? <StrictMode>{app}</StrictMode> : app);
  return { router, queryClient, history };
}

describe("Route tests: DashboardPage, LoginPage, ForgotPasswordPage, ResetPasswordPage, AccountPage, ShareViewPage, StravaCallbackPage, VerifyEmailPage", () => {
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
      fireEvent.click(await screen.findByRole("tab", { name: "Trips" }));
      expect(await screen.findByText(/No trip projects yet/i)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Account: Developer/i })).toHaveAttribute(
        "href",
        "/account",
      );

      fireEvent.click(screen.getByRole("button", { name: /Create trip/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Enter a trip name to create a project/i,
      );

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
      fireEvent.click(await screen.findByRole("tab", { name: "Trips" }));
      expect(await screen.findByText("Fictional Meridian Raid")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /Delete Fictional Meridian Raid/i }));
      fireEvent.click(screen.getByRole("button", { name: /^Confirm$/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/Cannot delete locked project/i);

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
      fireEvent.click(await screen.findByRole("tab", { name: "Trips" }));
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

    it("keeps the connected Atlas free of activity import controls", async () => {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: { id: "u-1", email: "rider@example.com", display_name: "Rider", strava_connected: true },
            csrf_token: "csrf-atlas-connected",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects")) {
          return jsonResponse([{
            id: "proj-atlas-1",
            title: "Fictional Meridian Circuit",
            revision: 1,
            stage_count: 2,
            total_distance_m: 60_000,
            settings: createDefaultRenderSpecV2(),
            created_at: "2001-06-14T08:00:00Z",
            updated_at: "2001-06-14T08:00:00Z",
          }]);
        }
        if (url.includes("/api/v1/analytics/heatmap")) return jsonResponse({
          trips: [{
            project_id: "proj-atlas-1", title: "Fictional Meridian Circuit", year: 2001,
            primary_activity: "cycling", distance_m: 60_000, elevation_gain_m: 0,
            stage_count: 2, bbox: [0.1, 0.1, 0.3, 0.3],
            polylines: [[[0.1, 0.1], [0.3, 0.3]]],
          }],
          hex_cells: [],
        });
        if (url.includes("/api/v1/analytics/career")) return jsonResponse({
          total_projects: 1, total_stages: 2, total_distance_m: 60_000,
          total_elevation_gain_m: 0, total_moving_seconds: 0,
          countries_visited: [], by_year: [], by_activity: [],
        });
        return jsonResponse({});
      });

      renderAtRoute("/");
      fireEvent.click(await screen.findByRole("tab", { name: "Atlas" }));

      expect(await screen.findByTestId("atlas-view")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Import from Strava/i })).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Target trip for Strava import")).not.toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: /Import Strava activity/i })).not.toBeInTheDocument();
      expect(fetchSpy.mock.calls.some(([input]) => String(input).includes("/api/v1/strava/"))).toBe(false);
    });

    it("does not offer Strava OAuth from Atlas when disconnected", async () => {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: true, strava_configured: true });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: { id: "u-1", email: "rider@example.com", display_name: "Rider", strava_connected: false },
            csrf_token: "csrf-atlas-disconnected",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects")) return jsonResponse([]);
        if (url.includes("/api/v1/analytics/heatmap")) return jsonResponse({ trips: [], hex_cells: [] });
        if (url.includes("/api/v1/analytics/career")) return jsonResponse({
          total_projects: 0, total_stages: 0, total_distance_m: 0,
          total_elevation_gain_m: 0, total_moving_seconds: 0,
          countries_visited: [], by_year: [], by_activity: [],
        });
        return jsonResponse({});
      });

      renderAtRoute("/");
      fireEvent.click(await screen.findByRole("tab", { name: "Atlas" }));

      expect(await screen.findByTestId("atlas-view")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Connect Strava/i })).not.toBeInTheDocument();
      expect(fetchSpy.mock.calls.some(([input]) => String(input).includes("/api/v1/strava/"))).toBe(false);
    });

    it("opens Activity Library from Atlas without creating a project or starting Strava OAuth", async () => {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: { id: "u-1", email: "rider@example.com", display_name: "Rider", strava_connected: false },
            csrf_token: "csrf-atlas-library",
            dev_auth_enabled: true,
          });
        }
        if (url.endsWith("/api/v1/projects")) return jsonResponse([]);
        if (url.includes("/api/v1/analytics/heatmap")) return jsonResponse({ trips: [], hex_cells: [] });
        if (url.includes("/api/v1/analytics/career")) return jsonResponse({
          total_projects: 0, total_stages: 0, total_distance_m: 0,
          total_elevation_gain_m: 0, total_moving_seconds: 0,
          countries_visited: [], by_year: [], by_activity: [],
        });
        return jsonResponse({});
      });

      renderAtRoute("/");
      fireEvent.click(await screen.findByRole("tab", { name: "Atlas" }));
      fireEvent.click(await screen.findByRole("button", { name: /Activity Library/i }));

      expect(await screen.findByText("Activity catalog")).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "Activity Library" })).toHaveAttribute("aria-selected", "true");
      expect(fetchSpy.mock.calls.some(([input, init]) =>
        String(input).includes("/api/v1/strava/") ||
        (String(input).endsWith("/api/v1/projects") && init?.method === "POST"),
      )).toBe(false);
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
      expect(
        screen.queryByText(/Same-origin session cookies protected by CSRF verification\./i),
      ).not.toBeInTheDocument();
      expect(screen.getByText(/Access your multi-day GPX and Strava route projects\./i)).toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /Create account/i })).not.toBeInTheDocument();

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

    it("hides Create account tab when registration_enabled is false and supports password visibility toggle", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({
            dev_auth_enabled: false,
            registration_enabled: false,
            strava_configured: false,
          });
        }
        return jsonResponse({}, 401);
      });

      renderAtRoute("/login");
      expect(await screen.findByRole("heading", { name: /Sign in to workspace/i })).toBeInTheDocument();
      expect(
        screen.queryByText(/Same-origin session cookies protected by CSRF verification\./i),
      ).not.toBeInTheDocument();
      expect(screen.queryByRole("tablist", { name: /Authentication mode/i })).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /Create account/i })).not.toBeInTheDocument();

      const passwordInput = screen.getByLabelText(/^Password$/i);
      expect(passwordInput).toHaveAttribute("type", "password");
      expect(screen.getByRole("link", { name: /Forgot password\?/i })).toHaveAttribute(
        "href",
        "/forgot-password",
      );
      fireEvent.click(screen.getByRole("button", { name: /^Show$/i }));
      expect(passwordInput).toHaveAttribute("type", "text");
      fireEvent.click(screen.getByRole("button", { name: /^Hide$/i }));
      expect(passwordInput).toHaveAttribute("type", "password");
    });

    it("validates registration fields, surfaces API errors, and shows Check your email confirmation after successful registration", async () => {
      let registered = false;
      let capturedPayload: Record<string, unknown> | null = null;
      let resendPayload: Record<string, unknown> | null = null;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({
            dev_auth_enabled: false,
            registration_enabled: true,
            strava_configured: false,
          });
        }
        if (url.includes("/api/v1/auth/register") && method === "POST") {
          const body = JSON.parse(String(init?.body ?? "{}")) as {
            email?: string;
            display_name?: string;
            password?: string;
            invite_code?: string;
          };
          capturedPayload = body;
          if (body.invite_code === "wrong-code") {
            return jsonResponse({ detail: "Invalid registration invite code" }, 403);
          }
          registered = true;
          return jsonResponse(
            {
              status: "verification_required",
              email: body.email ?? "alpine@example.com",
              message: "Please check your email to verify your account before signing in.",
            },
            201,
          );
        }
        if (url.includes("/api/v1/auth/resend-verification") && method === "POST") {
          resendPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          return jsonResponse(
            {
              status: "accepted",
              message:
                "If an unverified account exists for that email address, a new verification link has been sent.",
            },
            202,
          );
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({ detail: "Not authenticated" }, 401);
        }
        return jsonResponse({});
      });

      const { router } = renderAtRoute("/login");
      const createTab = await screen.findByRole("tab", { name: /Create account/i });
      expect(screen.getByRole("tablist", { name: /Authentication mode/i })).toBeInTheDocument();
      fireEvent.click(createTab);

      expect(await screen.findByRole("heading", { name: /Create your account/i })).toBeInTheDocument();
      expect(
        screen.getByText(/Enter your invite code to set up your personal workspace\./i),
      ).toBeInTheDocument();
      expect(
        screen.queryByText(/Same-origin session cookies protected by CSRF verification\./i),
      ).not.toBeInTheDocument();

      fireEvent.change(screen.getByLabelText(/Email address/i), {
        target: { value: "alpine@example.com" },
      });
      fireEvent.change(screen.getByLabelText(/Display name/i), {
        target: { value: "Alpine Explorer" },
      });
      fireEvent.change(screen.getByLabelText(/^Password$/i), {
        target: { value: "too-short" },
      });
      fireEvent.change(screen.getByLabelText(/Confirm password/i), {
        target: { value: "too-short" },
      });
      fireEvent.change(screen.getByLabelText(/Invite code/i), {
        target: { value: "alpine-2026" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Password must be at least 12 characters long\./i,
      );
      expect(capturedPayload).toBeNull();

      fireEvent.change(screen.getByLabelText(/^Password$/i), {
        target: { value: "secure-alpine-pass" },
      });
      fireEvent.change(screen.getByLabelText(/Confirm password/i), {
        target: { value: "different-alpine-pass" },
      });
      expect(screen.getByText(/Passwords do not match/i)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(/Passwords do not match\./i);
      expect(capturedPayload).toBeNull();

      fireEvent.change(screen.getByLabelText(/Confirm password/i), {
        target: { value: "secure-alpine-pass" },
      });
      fireEvent.change(screen.getByLabelText(/Invite code/i), {
        target: { value: "wrong-code" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Invalid registration invite code/i,
      );

      fireEvent.change(screen.getByLabelText(/Invite code/i), {
        target: { value: "alpine-2026" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

      expect(await screen.findByRole("heading", { name: /Check your email/i })).toBeInTheDocument();
      expect(registered).toBe(true);
      expect(capturedPayload).toEqual({
        email: "alpine@example.com",
        display_name: "Alpine Explorer",
        password: "secure-alpine-pass",
        invite_code: "alpine-2026",
      });
      expect(screen.getByText("alpine@example.com")).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/login");

      fireEvent.click(screen.getByRole("button", { name: /Resend verification email/i }));
      expect(await screen.findByRole("status")).toHaveTextContent(
        /a new verification link has been sent/i,
      );
      expect(resendPayload).toEqual({ email: "alpine@example.com" });

      fireEvent.click(screen.getByRole("button", { name: /Back to sign in/i }));
      expect(
        await screen.findByRole("heading", { name: /Sign in to workspace/i }),
      ).toBeInTheDocument();
    });

    it("surfaces resend verification action on 403 unverified email login error", async () => {
      let resendPayload: Record<string, unknown> | null = null;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: false, strava_configured: false });
        }
        if (url.includes("/api/v1/auth/login") && method === "POST") {
          return jsonResponse(
            {
              detail:
                "Email address is not verified. Please check your inbox or request a new verification link.",
            },
            403,
          );
        }
        if (url.includes("/api/v1/auth/resend-verification") && method === "POST") {
          resendPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          return jsonResponse(
            {
              status: "accepted",
              message:
                "If an unverified account exists for that email address, a new verification link has been sent.",
            },
            202,
          );
        }
        return jsonResponse({});
      });

      renderAtRoute("/login");
      expect(await screen.findByRole("heading", { name: /Sign in to workspace/i })).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText(/Email address/i), {
        target: { value: "rider@example.com" },
      });
      fireEvent.change(screen.getByLabelText(/Password/i), {
        target: { value: "correct-password" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^Sign in$/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Email address is not verified/i,
      );

      fireEvent.click(screen.getByRole("button", { name: /Resend verification email/i }));
      expect(await screen.findByRole("status")).toHaveTextContent(
        /a new verification link has been sent/i,
      );
      expect(resendPayload).toEqual({ email: "rider@example.com" });
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

  describe("VerifyEmailPage", () => {
    it("exchanges token once under StrictMode, invalidates auth query, and redirects to workspace", async () => {
      vi.useFakeTimers();
      window.history.replaceState({}, "", "/verify-email?token=valid-tok");

      let verifyCalls = 0;
      let capturedToken: string | undefined;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/auth/verify-email") && method === "POST") {
          verifyCalls += 1;
          const body = JSON.parse(String(init?.body ?? "{}")) as { token?: string };
          capturedToken = body.token;
          return jsonResponse({
            user: {
              id: "u-verified",
              email: "alpine@example.com",
              display_name: "Alpine Explorer",
              strava_connected: false,
            },
            csrf_token: "csrf-verified",
            dev_auth_enabled: false,
          });
        }
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: false, strava_configured: false });
        }
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({
            user: {
              id: "u-verified",
              email: "alpine@example.com",
              display_name: "Alpine Explorer",
              strava_connected: false,
            },
            csrf_token: "csrf-verified",
            dev_auth_enabled: false,
          });
        }
        if (url.endsWith("/api/v1/projects")) {
          return jsonResponse([]);
        }
        return jsonResponse({});
      });

      const { router, queryClient } = renderAtRoute("/verify-email", { strict: true });
      const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

      await vi.waitFor(() => {
        expect(
          screen.getByText(/Email verified! Redirecting to workspace\.\.\./i),
        ).toBeInTheDocument();
      });

      expect(verifyCalls).toBe(1);
      expect(capturedToken).toBe("valid-tok");
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["auth", "me"] });
      expect(screen.getByRole("button", { name: /Continue to workspace/i })).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(750);
      expect(router.state.location.pathname).toBe("/");
    });

    it("renders error view and allows resending verification email on invalid or expired token", async () => {
      window.history.replaceState({}, "", "/verify-email?token=tok-expired");
      let resendPayload: Record<string, unknown> | null = null;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/auth/verify-email") && method === "POST") {
          return jsonResponse({ detail: "Invalid or expired email verification link" }, 400);
        }
        if (url.includes("/api/v1/auth/resend-verification") && method === "POST") {
          resendPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          return jsonResponse(
            {
              status: "accepted",
              message:
                "If an unverified account exists for that email address, a new verification link has been sent.",
            },
            202,
          );
        }
        return jsonResponse({});
      });

      renderAtRoute("/verify-email");

      expect(
        await screen.findByText(/Invalid or expired email verification link/i),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Back to sign in/i })).toHaveAttribute(
        "href",
        "/login",
      );

      fireEvent.change(screen.getByLabelText(/Email address/i), {
        target: { value: "alpine@example.com" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Resend verification email/i }));

      expect(await screen.findByRole("status")).toHaveTextContent(
        /a new verification link has been sent/i,
      );
      expect(resendPayload).toEqual({ email: "alpine@example.com" });
    });

    it("renders error view without calling verify-email when token is missing and allows resending", async () => {
      window.history.replaceState({}, "", "/verify-email");
      let verifyCalls = 0;
      let resendPayload: Record<string, unknown> | null = null;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/auth/verify-email")) {
          verifyCalls += 1;
          return jsonResponse({}, 400);
        }
        if (url.includes("/api/v1/auth/resend-verification") && method === "POST") {
          resendPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          return jsonResponse(
            {
              status: "accepted",
              message:
                "If an unverified account exists for that email address, a new verification link has been sent.",
            },
            202,
          );
        }
        return jsonResponse({});
      });

      renderAtRoute("/verify-email");

      expect(await screen.findByText(/Missing email verification token\./i)).toBeInTheDocument();
      expect(verifyCalls).toBe(0);
      expect(screen.getByRole("link", { name: /Back to sign in/i })).toHaveAttribute(
        "href",
        "/login",
      );

      fireEvent.change(screen.getByLabelText(/Email address/i), {
        target: { value: "missing-token@example.com" },
      });
      fireEvent.click(screen.getByRole("button", { name: /Resend verification email/i }));

      expect(await screen.findByRole("status")).toHaveTextContent(
        /a new verification link has been sent/i,
      );
      expect(resendPayload).toEqual({ email: "missing-token@example.com" });
    });
  });

  describe("ForgotPasswordPage", () => {
    it("submits trimmed email, shows Check your email confirmation, and allows sending another link", async () => {
      let capturedPayload: Record<string, unknown> | null = null;
      let failFirst = true;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/auth/forgot-password") && method === "POST") {
          capturedPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          if (failFirst) {
            failFirst = false;
            return jsonResponse({ detail: "Too many reset requests" }, 429);
          }
          return jsonResponse(
            {
              status: "accepted",
              message:
                "If an active account exists for that email address, a password reset link has been sent.",
            },
            202,
          );
        }
        return jsonResponse({});
      });

      renderAtRoute("/forgot-password");
      expect(
        await screen.findByRole("heading", { name: /Reset your password/i }),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Back to sign in/i })).toHaveAttribute(
        "href",
        "/login",
      );

      fireEvent.change(screen.getByLabelText(/Email address/i), {
        target: { value: "  rider@example.com  " },
      });
      fireEvent.click(screen.getByRole("button", { name: /Send reset link/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(/Too many reset requests/i);
      expect(capturedPayload).toEqual({ email: "rider@example.com" });

      fireEvent.click(screen.getByRole("button", { name: /Send reset link/i }));
      expect(await screen.findByRole("heading", { name: /Check your email/i })).toBeInTheDocument();
      expect(screen.getByText("rider@example.com")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Back to sign in/i })).toHaveAttribute(
        "href",
        "/login",
      );

      fireEvent.click(screen.getByRole("button", { name: /Send another link/i }));
      expect(
        await screen.findByRole("heading", { name: /Reset your password/i }),
      ).toBeInTheDocument();
    });
  });

  describe("ResetPasswordPage", () => {
    it("renders missing token error without calling reset-password when token is absent", async () => {
      window.history.replaceState({}, "", "/reset-password");
      let resetCalls = 0;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/auth/reset-password")) {
          resetCalls += 1;
        }
        return jsonResponse({});
      });

      renderAtRoute("/reset-password");
      expect(await screen.findByText(/Missing password reset token\./i)).toBeInTheDocument();
      expect(resetCalls).toBe(0);
      expect(screen.getByRole("link", { name: /Request a new reset link/i })).toHaveAttribute(
        "href",
        "/forgot-password",
      );
    });

    it("validates password criteria, toggles visibility, handles errors, and completes reset", async () => {
      window.history.replaceState({}, "", "/reset-password?token=reset-tok");
      let capturedPayload: Record<string, unknown> | null = null;
      let failOnce = true;

      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        const method = init?.method ?? "GET";
        if (url.includes("/api/v1/auth/reset-password") && method === "POST") {
          capturedPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          if (failOnce) {
            failOnce = false;
            return jsonResponse({ detail: "Invalid or expired password reset link" }, 400);
          }
          return jsonResponse({
            ok: true,
            message: "Password has been reset. Please sign in again.",
          });
        }
        return jsonResponse({});
      });

      renderAtRoute("/reset-password");
      expect(await screen.findByRole("heading", { name: /Set a new password/i })).toBeInTheDocument();

      const newPasswordInput = screen.getByLabelText(/^New password$/i);
      const confirmInput = screen.getByLabelText(/^Confirm new password$/i);

      expect(newPasswordInput).toHaveAttribute("type", "password");
      fireEvent.click(screen.getByRole("button", { name: /^Show$/i }));
      expect(newPasswordInput).toHaveAttribute("type", "text");
      expect(confirmInput).toHaveAttribute("type", "text");
      fireEvent.click(screen.getByRole("button", { name: /^Hide$/i }));
      expect(newPasswordInput).toHaveAttribute("type", "password");

      expect(screen.getByText(/12\+ characters required/i)).toBeInTheDocument();

      fireEvent.change(newPasswordInput, { target: { value: "short" } });
      fireEvent.change(confirmInput, { target: { value: "short" } });
      fireEvent.click(screen.getByRole("button", { name: /Reset password/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Password must be at least 12 characters long\./i,
      );
      expect(capturedPayload).toBeNull();

      fireEvent.change(newPasswordInput, { target: { value: "new-alpine-password-12" } });
      fireEvent.change(confirmInput, { target: { value: "mismatch-password-12" } });
      expect(screen.getByText(/Passwords do not match/i)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /Reset password/i }));
      expect(await screen.findByRole("alert")).toHaveTextContent(/Passwords do not match\./i);
      expect(capturedPayload).toBeNull();

      fireEvent.change(confirmInput, { target: { value: "new-alpine-password-12" } });
      expect(screen.getByText(/Passwords match/i)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /Reset password/i }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        /Invalid or expired password reset link/i,
      );
      expect(capturedPayload).toEqual({
        token: "reset-tok",
        new_password: "new-alpine-password-12",
      });

      fireEvent.click(screen.getByRole("button", { name: /Reset password/i }));
      expect(
        await screen.findByRole("heading", { name: /Password reset complete/i }),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Sign in with new password/i })).toHaveAttribute(
        "href",
        "/login",
      );
    });
  });

  describe("AccountPage", () => {
    it("redirects to /login when unauthenticated", async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
        if (url.includes("/api/v1/auth/me")) {
          return jsonResponse({ detail: "Not authenticated" }, 401);
        }
        if (url.includes("/api/v1/public-config")) {
          return jsonResponse({ dev_auth_enabled: false, strava_configured: false });
        }
        return jsonResponse({});
      });

      const { router } = renderAtRoute("/account");
      await waitFor(() => {
        expect(router.state.location.pathname).toBe("/login");
      });
    });

    it("changes password, refreshes sessions, revokes another session, and signs out all devices", async () => {
      let sessions = [
        {
          id: "sess-current-1111",
          created_at: "2026-11-18T10:00:00Z",
          expires_at: "2026-11-25T10:00:00Z",
          is_current: true,
        },
        {
          id: "sess-other-2222",
          created_at: "2026-11-17T09:00:00Z",
          expires_at: "2026-11-24T09:00:00Z",
          is_current: false,
        },
        {
          id: "sess-other-3333",
          created_at: "2026-11-16T08:00:00Z",
          expires_at: "2026-11-23T08:00:00Z",
          is_current: false,
        },
      ];
      let changePasswordPayload: Record<string, unknown> | null = null;
      let revokedSessionId: string | null = null;
      let logoutAllCalls = 0;

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
              email: "rider@example.com",
              display_name: "Rider",
              strava_connected: false,
            },
            csrf_token: "csrf-acct",
            dev_auth_enabled: false,
          });
        }
        if (url.endsWith("/api/v1/auth/sessions") && method === "GET") {
          return jsonResponse(sessions);
        }
        if (url.includes("/api/v1/auth/change-password") && method === "POST") {
          changePasswordPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          sessions = sessions.filter((item) => item.id !== "sess-other-3333");
          return jsonResponse({ ok: true, revoked_sessions_count: 1 });
        }
        if (url.includes("/api/v1/auth/sessions/sess-other-2222") && method === "DELETE") {
          revokedSessionId = "sess-other-2222";
          sessions = sessions.filter((item) => item.id !== "sess-other-2222");
          return jsonResponse({ ok: true });
        }
        if (url.includes("/api/v1/auth/logout-all") && method === "POST") {
          logoutAllCalls += 1;
          return jsonResponse({ ok: true, revoked_sessions_count: sessions.length });
        }
        return jsonResponse({});
      });

      const { router, queryClient } = renderAtRoute("/account");
      const clearSpy = vi.spyOn(queryClient, "clear");

      expect(await screen.findByRole("heading", { name: /Account security/i })).toBeInTheDocument();
      expect(await screen.findByText("Current session")).toBeInTheDocument();
      expect(screen.getAllByRole("button", { name: /^Revoke$/i })).toHaveLength(2);

      fireEvent.click(screen.getByRole("button", { name: /^Show$/i }));
      expect(screen.getByLabelText(/^New password$/i)).toHaveAttribute("type", "text");
      fireEvent.click(screen.getByRole("button", { name: /^Hide$/i }));
      expect(screen.getByLabelText(/^New password$/i)).toHaveAttribute("type", "password");

      fireEvent.change(screen.getByLabelText(/^Current password$/i), {
        target: { value: "old-alpine-password" },
      });
      fireEvent.change(screen.getByLabelText(/^New password$/i), {
        target: { value: "new-alpine-password-12" },
      });
      fireEvent.change(screen.getByLabelText(/^Confirm new password$/i), {
        target: { value: "different-password-12" },
      });
      expect(screen.getByText(/Passwords do not match/i)).toBeInTheDocument();

      fireEvent.change(screen.getByLabelText(/^Confirm new password$/i), {
        target: { value: "new-alpine-password-12" },
      });
      expect(screen.getByText(/Passwords match/i)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /Update password/i }));

      expect(await screen.findByRole("status")).toHaveTextContent(/Password updated\./i);
      expect(changePasswordPayload).toEqual({
        current_password: "old-alpine-password",
        new_password: "new-alpine-password-12",
      });

      await waitFor(() => {
        expect(screen.getAllByRole("button", { name: /^Revoke$/i })).toHaveLength(1);
      });

      fireEvent.click(screen.getByRole("button", { name: /^Revoke$/i }));
      await waitFor(() => {
        expect(revokedSessionId).toBe("sess-other-2222");
        expect(screen.queryByRole("button", { name: /^Revoke$/i })).not.toBeInTheDocument();
      });
      expect(
        screen
          .getAllByRole("status")
          .some((node) => /Session revoked\./i.test(node.textContent ?? "")),
      ).toBe(true);

      fireEvent.click(screen.getByRole("button", { name: /Sign out all devices/i }));
      await waitFor(() => {
        expect(logoutAllCalls).toBe(1);
        expect(clearSpy).toHaveBeenCalled();
        expect(router.state.location.pathname).toBe("/login");
      });
    });
  });
});
