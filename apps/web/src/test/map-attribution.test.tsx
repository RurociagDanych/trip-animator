import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { clearCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2, type MapStylePreset, type RenderManifest } from "../lib/api/types";
import { createAppRouter } from "../router";

vi.mock("../components/dashboard/ActivityLibraryMap", () => ({
  ActivityLibraryMap: () => <div aria-label="Activity routes map" />,
}));

const OSM_CREDIT = "© OpenStreetMap contributors";
const PRESETS: readonly MapStylePreset[] = [
  "liberty",
  "positron",
  "bright",
  "dark",
  "topo-relief",
  "satellite-hybrid",
  "editorial-mono",
];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof Request ? input.url : input.toString();
}

function editorFetchMock(preset: MapStylePreset) {
  const settings = { ...createDefaultRenderSpecV2(), map_style: preset };
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = requestUrl(input);
    if (url.includes("/api/v1/public-config")) {
      return jsonResponse({ dev_auth_enabled: true, strava_configured: true });
    }
    if (url.includes("/api/v1/auth/me")) {
      return jsonResponse({
        user: { id: "u-1", email: "dev@trip-animator.local", display_name: "Developer", strava_connected: false },
        csrf_token: "csrf-attribution-token",
        dev_auth_enabled: true,
      });
    }
    if (url.includes("/api/v1/vault/activities")) {
      return jsonResponse({ items: [], total: 0, limit: 50, offset: 0, summary: null, by_sport: [] });
    }
    if (url.includes("/api/v1/projects/fictional-route") && !url.includes("/telemetry") && !url.includes("/kpis") && !url.includes("/renders")) {
      return jsonResponse({
        id: "fictional-route-2040",
        title: "Fictional Meridian Circuit",
        revision: 1,
        stage_count: 0,
        total_distance_m: 0,
        settings,
        stages: [],
        created_at: "2001-06-14T08:00:00Z",
        updated_at: "2001-06-14T08:00:00Z",
      });
    }
    if (url.includes("/telemetry/geometry")) {
      return jsonResponse({ type: "FeatureCollection", project_id: "fictional-route-2040", features: [] });
    }
    if (url.includes("/telemetry/chart")) {
      return jsonResponse({ project_id: "fictional-route-2040", total_distance_m: 0, points: [] });
    }
    if (url.includes("/kpis")) {
      return jsonResponse({
        project_id: "fictional-route-2040",
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
    return jsonResponse({ status: "ok", service: "trip-api" });
  });
}

function sharedFetchMock(preset: MapStylePreset) {
  const settings = { ...createDefaultRenderSpecV2(), map_style: preset };
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = requestUrl(input);
    if (url.includes("/api/v1/share/attribution-slug/chart")) {
      return jsonResponse({ project_id: "p-1", total_distance_m: 1000, points: [] });
    }
    if (url.includes("/api/v1/share/attribution-slug/stages/")) {
      return jsonResponse({
        type: "Feature",
        id: "s-1",
        geometry: { type: "MultiLineString", coordinates: [[[0.4512, 0.2173], [0.5325, 0.2537]]] },
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
    if (url.includes("/api/v1/share/attribution-slug")) {
      return jsonResponse({
        id: "p-1",
        title: "Shared Attribution Route",
        stage_count: 1,
        total_distance_m: 1000,
        settings,
        has_rendered_video: false,
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
}

function renderAt(path: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  render(<App router={router} queryClient={queryClient} />);
}

function expectVisibleAttribution(container: HTMLElement) {
  const attribution = within(container).getByTestId("map-attribution");
  expect(attribution).toHaveTextContent(OSM_CREDIT);
  expect(attribution).toBeVisible();
  return attribution;
}

describe("map attribution is always visible on every surface", () => {
  beforeEach(() => {
    clearCsrfToken();
    document.cookie = "trip_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  afterEach(() => {
    delete window.__TRIP_RENDER_MANIFEST__;
    delete window.__TRIP_RENDERER__;
    vi.restoreAllMocks();
  });

  for (const preset of PRESETS) {
    it(`shows the OpenStreetMap credit in the editor preview for ${preset}`, async () => {
      editorFetchMock(preset);
      renderAt("/projects/fictional-route-2040");

      const frame = await screen.findByTestId("editor-map-frame");
      expect(frame).toHaveAttribute("data-map-style", preset);
      expectVisibleAttribution(frame);
    });

    it(`shows the OpenStreetMap credit in shared playback for ${preset}`, async () => {
      sharedFetchMock(preset);
      renderAt("/share/attribution-slug");

      expect(await screen.findByText("Shared Attribution Route")).toBeInTheDocument();
      const frame = await screen.findByTestId("editor-map-frame");
      expect(frame).toHaveAttribute("data-map-style", preset);
      expectVisibleAttribution(frame);
    });

    it(`burns the OpenStreetMap credit into the render harness frame for ${preset}`, async () => {
      const spec = { ...createDefaultRenderSpecV2(), map_style: preset, is_preview: false };
      const manifest: RenderManifest = {
        job_id: "job-attribution",
        project_id: "proj-1",
        project_title: "Attribution Render",
        spec,
        width: 1280,
        height: 720,
        fps: 30,
        duration_s: 2,
        total_frames: 60,
        is_preview: false,
        stages: [],
        climbs: [],
        photos: [],
      } as unknown as RenderManifest;
      window.__TRIP_RENDER_MANIFEST__ = manifest;

      const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-attribution"] }));
      render(<App router={router} />);

      const harness = await screen.findByTestId("render-harness-root");
      expectVisibleAttribution(harness);
      await waitFor(() => expect(window.__TRIP_RENDERER__).toBeDefined());
    });
  }
});
