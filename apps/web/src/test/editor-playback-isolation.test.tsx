// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import type { InspectorPanelProps } from "../components/editor/InspectorPanel";
import type { StageSidebarProps } from "../components/editor/StageSidebar";
import { clearCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { useEditorStore } from "../lib/state/editor-store";
import { createAppRouter } from "../router";

const renderCounts = vi.hoisted(() => ({
  stageSidebar: 0,
  inspectorPanel: 0,
}));

vi.mock("../components/editor/StageSidebar", async () => {
  const actual = await vi.importActual<{
    StageSidebar: ComponentType<StageSidebarProps>;
  }>("../components/editor/StageSidebar");
  return {
    ...actual,
    StageSidebar: (props: StageSidebarProps) => {
      renderCounts.stageSidebar += 1;
      return <actual.StageSidebar {...props} />;
    },
  };
});

vi.mock("../components/editor/InspectorPanel", async () => {
  const actual = await vi.importActual<{
    InspectorPanel: ComponentType<InspectorPanelProps>;
  }>("../components/editor/InspectorPanel");
  return {
    ...actual,
    InspectorPanel: (props: InspectorPanelProps) => {
      renderCounts.inspectorPanel += 1;
      return <actual.InspectorPanel {...props} />;
    },
  };
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("EditorPage 60Hz playback isolation", () => {
  beforeEach(() => {
    clearCsrfToken();
    renderCounts.stageSidebar = 0;
    renderCounts.inspectorPanel = 0;
    useEditorStore.setState({
      projectId: null,
      currentTimeRatio: 0,
      isPlaying: false,
      playbackSpeed: 1,
      previewWatermark: false,
      undoStack: [],
      redoStack: [],
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("incurs 0 re-renders in StageSidebar and InspectorPanel during playback while scrubber updates and Add cue captures progress", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/auth/me")) {
        return jsonResponse({
          user: {
            id: "u-1",
            email: "dev@trip-animator.local",
            display_name: "Developer",
            strava_connected: false,
          },
          csrf_token: "csrf-iso",
          dev_auth_enabled: true,
        });
      }
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({
          dev_auth_enabled: true,
          strava_configured: false,
          google_photos_configured: false,
        });
      }
      if (url.endsWith("/api/v1/projects/proj-iso")) {
        return jsonResponse({
          id: "proj-iso",
          title: "Playback Isolation Trip",
          revision: 1,
          stage_count: 1,
          total_distance_m: 10_000,
          settings: {
            ...createDefaultRenderSpecV2(),
            intro_hold_seconds: 0,
            outro_hold_seconds: 0,
          },
          share_enabled: false,
          share_slug: null,
          share_created_at: null,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
          stages: [
            {
              id: "s-iso-1",
              project_id: "proj-iso",
              source_type: "gpx",
              activity_type: "cycling",
              external_source_id: null,
              strava_activity_id: null,
              name: "Fictional Stage 1",
              stage_order: 0,
              day_index: 1,
              trim_start_s: null,
              trim_end_s: null,
              started_at: "2001-06-14T08:00:00Z",
              point_count: 2,
              distance_m: 10_000,
              elevation_gain_m: 300,
              elevation_loss_m: 100,
              moving_seconds: 1800,
              elapsed_seconds: 1800,
              has_timestamps: true,
              min_lat: 0.1,
              max_lat: 0.2,
              min_lon: 0.1,
              max_lon: 0.2,
              warnings: [],
              created_at: "2001-06-14T08:00:00Z",
            },
          ],
        });
      }
      if (url.includes("/stages/s-iso-1/geometry")) {
        return jsonResponse({
          type: "Feature",
          id: "s-iso-1",
          geometry: {
            type: "MultiLineString",
            coordinates: [
              [
                [0.1, 0.1],
                [0.2, 0.2],
              ],
            ],
          },
          properties: {
            lod: "medium",
            point_count: 2,
            original_point_count: 2,
            segment_count: 1,
            bbox: [0.1, 0.1, 0.2, 0.2],
            crosses_antimeridian: false,
            stage_id: "s-iso-1",
            name: "Fictional Stage 1",
            day_index: 1,
            stage_order: 0,
            source_type: "gpx",
            distance_m: 10_000,
            elevation_gain_m: 300,
          },
        });
      }
      if (url.includes("/telemetry/chart")) {
        return jsonResponse({
          project_id: "proj-iso",
          total_distance_m: 10_000,
          points: [
            { cum_dist_m: 0, elev_m: 100, speed_mps: 6, grade_pct: 2, hr_bpm: 130, watts: 200 },
            { cum_dist_m: 10_000, elev_m: 400, speed_mps: 5, grade_pct: 4, hr_bpm: 160, watts: 280 },
          ],
        });
      }
      if (url.includes("/telemetry/kpis")) {
        return jsonResponse({
          project_id: "proj-iso",
          stage_count: 1,
          total_distance_m: 10_000,
          total_elevation_gain_m: 300,
          total_moving_seconds: 1800,
          stages: [],
        });
      }
      if (url.includes("/telemetry/highlights")) {
        return jsonResponse({
          project_id: "proj-iso",
          climbs: [],
          splits: [],
          dwell_stops: [],
          peak_efforts: [],
          geo_context: [],
        });
      }
      if (url.includes("/photos")) {
        return jsonResponse([]);
      }
      if (url.includes("/renders")) {
        return jsonResponse([]);
      }
      return jsonResponse({});
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const history = createMemoryHistory({ initialEntries: ["/projects/proj-iso"] });
    const router = createAppRouter(history);
    render(<App router={router} queryClient={queryClient} />);

    // Wait for EditorPage and all initial telemetry queries to settle
    expect(await screen.findByText("Fictional Stage 1")).toBeInTheDocument();
    await screen.findByLabelText("Timeline scrubber");

    // Switch to Camera tab so "Add cue" is rendered before measuring playback ticks
    fireEvent.click(screen.getByRole("tab", { name: "Camera" }));
    const addCueBtn = await screen.findByRole("button", { name: /Add cue/i });

    // Allow any pending React Query microtasks to flush
    await act(async () => {
      await Promise.resolve();
    });

    renderCounts.stageSidebar = 0;
    renderCounts.inspectorPanel = 0;

    for (const ratio of [0.1, 0.2, 0.3, 0.4, 0.5]) {
      act(() => {
        useEditorStore.getState().setCurrentTimeRatio(ratio);
      });
    }

    expect(renderCounts.stageSidebar).toBe(0);
    expect(renderCounts.inspectorPanel).toBe(0);
    expect((screen.getByLabelText("Timeline scrubber") as HTMLInputElement).value).toBe("500");

    fireEvent.click(addCueBtn);
    const keyframes = useEditorStore.getState().spec.camera_keyframes;
    expect(keyframes).toHaveLength(1);
    expect(keyframes[0]?.progress).toBeCloseTo(0.5, 3);
  });
});
