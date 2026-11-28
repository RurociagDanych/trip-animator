import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { apiClient, clearCsrfToken, setCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { buildTelemetryPlotOptions } from "../lib/charts/telemetry-options";
import { buildDeckOverlayProps, resolveMapStyleUrl } from "../lib/geo/map-config";
import { createAppRouter } from "../router";

vi.mock("../components/dashboard/ActivityLibraryMap", () => ({
  ActivityLibraryMap: () => <div aria-label="Activity routes map" />,
}));

function mockJsonResponse(body: unknown, status = 200, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
  });
}

const PUBLISHED_POINTER = {
  current_build_id: "2026-11-27",
  pmtiles_key: "builds/2026-11-27/marked-trails.pmtiles",
  osm_data_date: "2026-11-26T21:21:00Z",
};

function mockStandardApiFetch(
  publicConfigExtra: Record<string, unknown> = {},
  markedTrailsPointer: { status: number; body: unknown } | "reject" | "pending" = {
    status: 404,
    body: { detail: "not published" },
  },
  savedMarkedTrailTypes: string[] = [],
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
    if (url.includes("/api/v1/public-config")) {
      return mockJsonResponse({ dev_auth_enabled: true, strava_configured: true, ...publicConfigExtra });
    }
    if (url.endsWith("/tiles/marked-trails/current.json")) {
      if (markedTrailsPointer === "reject") {
        throw new TypeError("Failed to fetch");
      }
      if (markedTrailsPointer === "pending") {
        return new Promise<Response>(() => {});
      }
      return mockJsonResponse(markedTrailsPointer.body, markedTrailsPointer.status);
    }
    if (url.includes("/api/v1/auth/me")) {
      return mockJsonResponse({
        user: {
          id: "u-1",
          email: "dev@trip-animator.local",
          display_name: "Developer",
          strava_connected: false,
        },
        csrf_token: "csrf-smoke-token",
        dev_auth_enabled: true,
      });
    }
    if (url.includes("/api/v1/vault/activities")) {
      return mockJsonResponse({ items: [], total: 0, limit: 50, offset: 0, summary: null, by_sport: [] });
    }
    if (url.endsWith("/api/v1/projects")) {
      return mockJsonResponse([
        {
          id: "proj-fictional-route",
          title: "Fictional Meridian Circuit",
          revision: 2,
          stage_count: 4,
          total_distance_m: 142_800,
          settings: createDefaultRenderSpecV2(),
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        },
      ]);
    }
    if (url.includes("/api/v1/projects/fictional-route") && !url.includes("/telemetry") && !url.includes("/kpis") && !url.includes("/renders")) {
      return mockJsonResponse({
        id: "fictional-route-2040",
        title: "Fictional Meridian Circuit",
        revision: 1,
        stage_count: 0,
        total_distance_m: 0,
        settings: { ...createDefaultRenderSpecV2(), marked_trail_types: savedMarkedTrailTypes },
        stages: [],
        created_at: "2001-06-14T08:00:00Z",
        updated_at: "2001-06-14T08:00:00Z",
      });
    }
    if (url.includes("/stages/") && url.includes("/geometry")) {
      return mockJsonResponse({
        type: "Feature",
        id: "stage-1",
        geometry: { type: "MultiLineString", coordinates: [] },
        properties: {
          lod: "medium",
          point_count: 0,
          original_point_count: 0,
          segment_count: 0,
          bbox: [0, 0, 0, 0],
          crosses_antimeridian: false,
          stage_id: "stage-1",
          name: "Stage",
          day_index: 1,
          stage_order: 0,
          source_type: "gpx",
          activity_type: "cycling",
          distance_m: 0,
          elevation_gain_m: 0,
        },
      });
    }
    if (url.includes("/telemetry/geometry")) {
      return mockJsonResponse({ type: "FeatureCollection", project_id: "fictional-route-2040", features: [] });
    }
    if (url.includes("/telemetry/chart")) {
      return mockJsonResponse({ project_id: "fictional-route-2040", total_distance_m: 0, points: [] });
    }
    if (url.includes("/kpis")) {
      return mockJsonResponse({
        project_id: "fictional-route-2040",
        stage_count: 0,
        total_distance_m: 0,
        total_elevation_gain_m: 0,
        total_moving_seconds: 0,
        stages: [],
      });
    }
    if (url.includes("/renders")) {
      return mockJsonResponse([]);
    }
    return mockJsonResponse({ status: "ok", service: "trip-api" });
  });
}

describe("Trip Animator web routes and config builders", () => {
  beforeEach(() => {
    clearCsrfToken();
    document.cookie = "trip_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the dashboard route with project list and Strava status", async () => {
    const fetchSpy = mockStandardApiFetch();

    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/"] }));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByRole("heading", { name: "Activity Library", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Activity Library" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("heading", { name: "Trip projects" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Trips" }));
    expect(await screen.findByRole("heading", { name: "Trip projects" })).toBeInTheDocument();
    expect(screen.queryByText("v2")).not.toBeInTheDocument();
    expect(screen.queryByText(/Trip Animator v2/i)).not.toBeInTheDocument();
    expect(await screen.findByText("Fictional Meridian Circuit")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Connect Strava/i })).toBeInTheDocument();
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/v1/projects",
        expect.objectContaining({ method: "GET", credentials: "same-origin" }),
      );
    });
  });

  it("renders the login route with conditional dev login", async () => {
    mockStandardApiFetch();
    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/login"] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByRole("heading", { name: "Sign in to workspace" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Development one-click sign in" }),
    ).toBeInTheDocument();
  });

  it("renders the 3-pane editor workspace shell", async () => {
    mockStandardApiFetch();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByDisplayValue("Fictional Meridian Circuit")).toBeInTheDocument();
    expect(screen.getByLabelText("Stages and telemetry sources")).toBeInTheDocument();
    expect(screen.getByLabelText("Map preview and timeline dock")).toBeInTheDocument();
    expect(screen.getByLabelText("Animation and render inspector")).toBeInTheDocument();
    expect(screen.getByTestId("editor-map-frame")).toBeInTheDocument();
  });

  it("hides the Marked Trails toggle when the server configures no Marked Trails store", async () => {
    mockStandardApiFetch();
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByRole("combobox", { name: "Map style" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Hiking Marked Trails" })).not.toBeInTheDocument();
  });

  it.each([
    ["missing", 404, { detail: "not published" }],
    ["forbidden", 403, "<Error>AccessDenied</Error>"],
    ["invalid", 200, { current_build_id: "2026-11-27", pmtiles_key: "elsewhere/x.pmtiles" }],
  ])(
    "disables the Marked Trails toggles with an explanation when the pointer is %s",
    async (_name, status, body) => {
      mockStandardApiFetch({ marked_trails_base_url: "/tiles/marked-trails" }, { status, body });
      const router = createAppRouter(
        createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
      );
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(<App router={router} queryClient={queryClient} />);

      expect(await screen.findByText(/Marked Trails data is not published yet/)).toBeInTheDocument();
      for (const name of ["Hiking Marked Trails", "Cycling Marked Trails", "MTB Marked Trails"]) {
        expect(screen.getByRole("checkbox", { name })).toBeDisabled();
      }
    },
  );

  it("enables the toggles and shows the trail data date in the attribution only while the overlay is on", async () => {
    mockStandardApiFetch(
      { marked_trails_base_url: "/tiles/marked-trails" },
      { status: 200, body: PUBLISHED_POINTER },
    );
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    const hiking = await screen.findByRole("checkbox", { name: "Hiking Marked Trails" });
    await waitFor(() => expect(screen.getByTestId("map-attribution")).toBeInTheDocument());
    expect(hiking).toBeEnabled();
    expect(screen.queryByText(/Marked Trails data is not published yet/)).not.toBeInTheDocument();
    expect(screen.getByTestId("map-attribution")).not.toHaveTextContent("Marked Trails data");

    fireEvent.click(hiking);
    await waitFor(() =>
      expect(screen.getByTestId("map-attribution")).toHaveTextContent(
        "© OpenStreetMap contributors · Marked Trails data: 2026-11-26",
      ),
    );
    fireEvent.click(hiking);
    await waitFor(() =>
      expect(screen.getByTestId("map-attribution")).not.toHaveTextContent("Marked Trails data"),
    );
  });

  it("disables the toggles without an explanation while the build is loading", async () => {
    mockStandardApiFetch({ marked_trails_base_url: "/tiles/marked-trails" }, "pending");
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    for (const name of ["Hiking Marked Trails", "Cycling Marked Trails", "MTB Marked Trails"]) {
      expect(await screen.findByRole("checkbox", { name })).toBeDisabled();
    }
    expect(screen.queryByText(/not published yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument();
  });

  it.each([
    ["the request fails", "reject" as const],
    ["the server answers 500", { status: 500, body: { detail: "boom" } }],
  ])(
    "disables the toggles with a could-not-be-loaded message when %s",
    async (_name, pointer) => {
      const fetchSpy = mockStandardApiFetch({ marked_trails_base_url: "/tiles/marked-trails" }, pointer);
      const router = createAppRouter(
        createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
      );
      const queryClient = new QueryClient();
      render(<App router={router} queryClient={queryClient} />);

      expect(
        await screen.findByText(/Marked Trails data could not be loaded/, undefined, {
          timeout: 9000,
        }),
      ).toBeInTheDocument();
      expect(screen.queryByText(/not published yet/)).not.toBeInTheDocument();
      for (const name of ["Hiking Marked Trails", "Cycling Marked Trails", "MTB Marked Trails"]) {
        expect(screen.getByRole("checkbox", { name })).toBeDisabled();
      }
      const pointerCalls = fetchSpy.mock.calls.filter(([input]) =>
        String(input).endsWith("/tiles/marked-trails/current.json"),
      );
      expect(pointerCalls).toHaveLength(3);
    },
    15000,
  );

  it("lets the user uncheck saved types while the data is not published and explains the saved choices", async () => {
    mockStandardApiFetch(
      { marked_trails_base_url: "/tiles/marked-trails" },
      { status: 404, body: { detail: "not published" } },
      ["hiking"],
    );
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByText(/Saved choices apply once data is published/)).toBeInTheDocument();
    const hiking = screen.getByRole("checkbox", { name: "Hiking Marked Trails" });
    expect(hiking).toBeChecked();
    expect(hiking).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Cycling Marked Trails" })).toBeDisabled();
    fireEvent.click(hiking);
    expect(hiking).not.toBeChecked();
  });

  it("toggles hiking, cycling and MTB Marked Trails independently and saves each to the project", async () => {
    const fetchSpy = mockStandardApiFetch(
      { marked_trails_base_url: "/tiles/marked-trails" },
      { status: 200, body: PUBLISHED_POINTER },
    );
    const router = createAppRouter(
      createMemoryHistory({ initialEntries: ["/projects/fictional-route-2040"] }),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    const hiking = await screen.findByRole("checkbox", { name: "Hiking Marked Trails" });
    const cycling = await screen.findByRole("checkbox", { name: "Cycling Marked Trails" });
    const mtb = await screen.findByRole("checkbox", { name: "MTB Marked Trails" });
    for (const toggle of [hiking, cycling, mtb]) {
      expect(toggle).not.toBeChecked();
    }
    fireEvent.click(cycling);
    expect(cycling).toBeChecked();
    expect(hiking).not.toBeChecked();
    expect(mtb).not.toBeChecked();
    fireEvent.click(hiking);
    fireEvent.click(mtb);
    fireEvent.click(cycling);
    expect(cycling).not.toBeChecked();
    expect(hiking).toBeChecked();
    expect(mtb).toBeChecked();

    await waitFor(
      () => {
        const saved = fetchSpy.mock.calls.some(
          ([, init]) =>
            init?.method === "PATCH" &&
            typeof init.body === "string" &&
            init.body.includes('"marked_trail_types":["hiking","mtb"]'),
        );
        expect(saved).toBe(true);
      },
      { timeout: 4000 },
    );
  });

  it("renders the deterministic render harness route", async () => {
    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-42"] }));
    render(<App router={router} />);

    const harness = await screen.findByTestId("render-harness-root");
    expect(harness).toHaveAttribute("data-job-id", "job-42");
    expect(harness).toHaveAttribute("data-harness-ready", "true");
    expect(screen.getByTestId("deterministic-map-frame")).toBeInTheDocument();
  });

  it("sends X-CSRF-Token header on mutations and omits it on GET requests", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        mockJsonResponse({ status: "ok" }, 200, { "X-CSRF-Token": "server-issued-csrf" }),
      )
      .mockResolvedValueOnce(mockJsonResponse({ id: "proj-1" }, 201));

    await apiClient.get<{ status: string }>("/health");
    const getHeaders = new Headers(fetchSpy.mock.calls[0]?.[1]?.headers);
    expect(getHeaders.get("X-CSRF-Token")).toBeNull();

    await apiClient.post<{ id: string }>("/api/v1/projects", { title: "Fictional Loop" });
    const postHeaders = new Headers(fetchSpy.mock.calls[1]?.[1]?.headers);
    expect(postHeaders.get("X-CSRF-Token")).toBe("server-issued-csrf");

    setCsrfToken("explicit-csrf-token");
    fetchSpy.mockResolvedValueOnce(mockJsonResponse({ ok: true }, 200));
    await apiClient.patch<{ ok: boolean }>("/api/v1/projects/proj-1", { revision: 2 });
    const patchHeaders = new Headers(fetchSpy.mock.calls[2]?.[1]?.headers);
    expect(patchHeaders.get("X-CSRF-Token")).toBe("explicit-csrf-token");
  });

  it("constructs typed MapLibre/Deck.gl and uPlot configurations", () => {
    expect(resolveMapStyleUrl("dark")).toBe("https://tiles.openfreemap.org/styles/dark");

    const deckProps = buildDeckOverlayProps(
      [{ id: "stage-1", color: [56, 189, 248], path: [[0.2, 0.1], [0.24, 0.14]] }],
      [0.22, 0.12],
    );
    expect(deckProps.interleaved).toBe(true);
    expect(Array.isArray(deckProps.layers)).toBe(true);
    expect((deckProps.layers as unknown[]).length).toBe(4);

    const plotOptions = buildTelemetryPlotOptions(640, 120);
    expect(plotOptions.width).toBe(640);
    expect(plotOptions.height).toBe(120);
  });
});
