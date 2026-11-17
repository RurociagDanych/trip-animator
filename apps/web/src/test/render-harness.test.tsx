// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { createDefaultRenderSpecV2, type RenderManifest } from "../lib/api/types";
import { createAppRouter } from "../router";

describe("Deterministic Render Harness (/render/$jobId)", () => {
  afterEach(() => {
    delete window.__TRIP_RENDER_MANIFEST__;
    delete window.__TRIP_RENDERER__;
    vi.restoreAllMocks();
  });

  it("reads window.__TRIP_RENDER_MANIFEST__, renders exact dimensions, and advances frames via window.__TRIP_RENDERER__", async () => {
    const spec = createDefaultRenderSpecV2();
    spec.resolution = "1080x1080";
    spec.duration_seconds = 5;
    spec.intro_hold_seconds = 0;
    spec.outro_hold_seconds = 0;
    spec.is_preview = true;
    spec.elevation_chart_overlay = true;
    spec.statistics_panel.enabled = true;
    spec.statistics_panel.metrics = ["trip-day", "distance-travelled", "elevation"];

    const manifest: RenderManifest = {
      job_id: "job-det-1",
      project_id: "proj-1",
      project_title: "Deterministic Test",
      spec,
      width: 1080,
      height: 1080,
      fps: 30,
      duration_s: 5,
      total_frames: 150,
      is_preview: true,
      stages: [
        {
          id: "stage-1",
          name: "Fictional Equatorial Trail",
          day_index: 1,
          stage_order: 0,
          activity_type: "hiking",
          color: "#38bdf8",
          transition: "bed",
          distance_m: 10_000,
          elevation_gain_m: 500,
          moving_seconds: 1800,
          elapsed_seconds: 1800,
          has_timestamps: true,
          points: [
            {
              lat: 0.2925,
              lon: 0.4512,
              lon_unwrapped: 0.4512,
              elev_m: 1500,
              cum_dist_m: 0,
              speed_mps: 5,
              grade_pct: 3,
              elapsed_s: 0,
              ts_utc: "2040-04-12T08:00:00Z",
              segment_idx: 0,
            },
            {
              lat: 0.3128,
              lon: 0.5608,
              lon_unwrapped: 0.5608,
              elev_m: 2000,
              cum_dist_m: 10_000,
              speed_mps: 5,
              grade_pct: 5,
              elapsed_s: 1800,
              ts_utc: "2040-04-12T08:30:00Z",
              segment_idx: 0,
            },
          ],
        },
      ],
    };

    window.__TRIP_RENDER_MANIFEST__ = manifest;

    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-det-1"] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    const root = await screen.findByTestId("render-harness-root");
    expect(root).toHaveAttribute("data-job-id", "job-det-1");
    expect(root).toHaveAttribute("data-harness-ready", "true");
    expect(root).toHaveAttribute("data-total-frames", "150");

    const canvasFrame = screen.getByTestId("deterministic-map-frame");
    expect(canvasFrame).toHaveStyle({ width: "1080px", height: "1080px" });
    expect(screen.getByTestId("preview-watermark")).toBeInTheDocument();

    const renderer = window.__TRIP_RENDERER__;
    expect(renderer).toBeDefined();
    await renderer!.ready;
    expect(renderer!.getFrameCount()).toBe(150);

    // Advance to final frame and verify StatisticsHud updates deterministically
    await act(async () => {
      await renderer!.setFrame(149);
    });

    expect(root).toHaveAttribute("data-frame-index", "149");
    const hud = screen.getByTestId("statistics-hud");
    expect(hud).toHaveTextContent("10.0 km");
    expect(hud).toHaveTextContent("2000 m");
    expect(screen.getByTestId("hud-activity-chip")).toHaveTextContent("Hiking");
    expect(screen.getByTestId("svg-activity-marker-hiking")).toBeInTheDocument();
    expect(screen.getByTestId("canvas-elevation-overlay")).toHaveTextContent("2000 m");
  });

  it("renders ManifestPhoto data_uri card and photo pin deterministically with pause_on_photo and adaptive multi-day zoom", async () => {
    const spec = createDefaultRenderSpecV2();
    spec.resolution = "1920x1080";
    spec.duration_seconds = 60;
    spec.intro_hold_seconds = 0;
    spec.outro_hold_seconds = 0;
    spec.camera_mode = "follow-marker";
    spec.follow_zoom_mode = "adaptive";
    spec.show_photos = true;
    spec.photo_display_seconds = 10;
    spec.pause_on_photo = true;
    spec.elevation_chart_overlay = true;

    const svgDataUri =
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4IiBoZWlnaHQ9IjgiPjxyZWN0IHdpZHRoPSI4IiBoZWlnaHQ9IjgiIGZpbGw9IiMzOGJkZjgiLz48L3N2Zz4=";

    const manifest: RenderManifest = {
      job_id: "job-photo-1",
      project_id: "proj-photo",
      project_title: "Fictional Long Route with Photo",
      spec,
      width: 1920,
      height: 1080,
      fps: 10,
      duration_s: 60,
      total_frames: 601,
      is_preview: false,
      stages: [
        {
          id: "s-long",
          name: "Fictional Meridian Route",
          day_index: 1,
          stage_order: 0,
          activity_type: "cycling",
          color: "#38bdf8",
          transition: "bed",
          distance_m: 1_500_000,
          elevation_gain_m: 15_000,
          moving_seconds: 180_000,
          elapsed_seconds: 180_000,
          has_timestamps: true,
          points: [
            {
              lat: 0.1,
              lon: -7.5,
              lon_unwrapped: -7.5,
              elev_m: 1000,
              cum_dist_m: 0,
              speed_mps: 8,
              grade_pct: 1,
              elapsed_s: 0,
              ts_utc: "2001-01-01T00:00:00Z",
              segment_idx: 0,
            },
            {
              lat: 0.1,
              lon: 7.5,
              lon_unwrapped: 7.5,
              elev_m: 2000,
              cum_dist_m: 1_500_000,
              speed_mps: 8,
              grade_pct: 2,
              elapsed_s: 180_000,
              ts_utc: "2001-01-03T02:00:00Z",
              segment_idx: 0,
            },
          ],
        },
      ],
      photos: [
        {
          id: "photo-mid",
          caption: "Fictional Midpoint",
          width: 640,
          height: 480,
          taken_at_utc: "2001-01-02T01:00:00Z",
          lat: 0.1,
          lon: 0,
          data_uri: svgDataUri,
        },
      ],
    };

    window.__TRIP_RENDER_MANIFEST__ = manifest;

    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-photo-1"] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    await screen.findByTestId("render-harness-root");
    const renderer = window.__TRIP_RENDERER__;
    expect(renderer).toBeDefined();
    await renderer!.ready;

    // 1 photo at progress 0.5, pause=10s, travel=50s -> pause spans t in [25s, 35s] -> frame 300 (t=30s)
    await act(async () => {
      await renderer!.setFrame(300);
    });

    const card = screen.getByTestId("photo-stop-card");
    expect(card).toHaveTextContent("Fictional Midpoint");
    expect(card.querySelector("img")).toHaveAttribute("src", svgDataUri);
    expect(screen.getByTestId("svg-photo-pin-photo-mid")).toBeInTheDocument();

    const frame = screen.getByTestId("deterministic-map-frame");
    const followZoom = Number(frame.getAttribute("data-follow-zoom"));
    expect(Number.isFinite(followZoom)).toBe(true);
    expect(followZoom).toBeLessThan(11.0);
  });

  it("renders polaroid photo card, chapter card, climb pin, and finale poster card deterministically", async () => {
    const spec = createDefaultRenderSpecV2();
    spec.resolution = "1920x1080";
    spec.duration_seconds = 10;
    spec.intro_hold_seconds = 0;
    spec.outro_hold_seconds = 2;
    spec.show_photos = true;
    spec.photo_card_style = "polaroid";
    spec.photo_display_seconds = 4;
    spec.show_chapter_cards = true;
    spec.show_finale_poster = true;
    spec.show_climb_callouts = true;

    const svgDataUri =
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4IiBoZWlnaHQ9IjgiPjxyZWN0IHdpZHRoPSI4IiBoZWlnaHQ9IjgiIGZpbGw9IiMzOGJkZjgiLz48L3N2Zz4=";

    const manifest: RenderManifest = {
      job_id: "job-poster-1",
      project_id: "proj-poster",
      project_title: "Fictional Meridian Epic",
      spec,
      width: 1920,
      height: 1080,
      fps: 10,
      duration_s: 10,
      total_frames: 101,
      is_preview: false,
      stages: [
        {
          id: "s-1",
          name: "Fictional Meridian Summit",
          day_index: 1,
          stage_order: 0,
          activity_type: "cycling",
          color: "#38bdf8",
          transition: "bed",
          distance_m: 20_000,
          elevation_gain_m: 1200,
          moving_seconds: 3600,
          elapsed_seconds: 3800,
          has_timestamps: true,
          points: [
            {
              lat: 0.1,
              lon: 0.1,
              lon_unwrapped: 0.1,
              elev_m: 1200,
              cum_dist_m: 0,
              speed_mps: 6,
              grade_pct: 5,
              elapsed_s: 0,
              ts_utc: "2040-04-12T08:00:00Z",
              segment_idx: 0,
            },
            {
              lat: 0.1,
              lon: 0.3,
              lon_unwrapped: 0.3,
              elev_m: 2236,
              cum_dist_m: 20_000,
              speed_mps: 5,
              grade_pct: 8,
              elapsed_s: 3600,
              ts_utc: "2040-04-12T09:00:00Z",
              segment_idx: 0,
            },
          ],
        },
      ],
      climbs: [
        {
          id: "climb-1",
          stage_id: "s-1",
          name: "Fictional Meridian Summit",
          category: "Cat 1",
          start_dist_m: 2000,
          end_dist_m: 18000,
          start_progress: 0.1,
          end_progress: 0.9,
          summit_progress: 0.9,
          summit_lat: 0.1,
          summit_lon: 0.3,
          summit_elev_m: 2236,
          distance_m: 16000,
          elevation_gain_m: 1036,
          avg_grade_pct: 8.5,
          max_grade_pct: 14,
          fiets_score: 8.2,
        },
      ],
      photos: [
        {
          id: "ph-1",
          caption: "Summit view",
          width: 640,
          height: 480,
          taken_at_utc: "2040-04-12T08:30:00Z",
          lat: 0.1,
          lon: 0.2,
          data_uri: svgDataUri,
        },
      ],
    };

    window.__TRIP_RENDER_MANIFEST__ = manifest;
    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-poster-1"] }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<App router={router} queryClient={queryClient} />);

    await screen.findByTestId("render-harness-root");
    const renderer = window.__TRIP_RENDERER__!;
    await renderer.ready;

    // Early frame -> Chapter Card visible
    await act(async () => {
      await renderer.setFrame(5);
    });
    expect(screen.getByTestId("chapter-card")).toHaveTextContent("Fictional Meridian Summit");

    // Mid frame -> Polaroid photo card visible (after photo fade-in at t=4.5s)
    await act(async () => {
      await renderer.setFrame(45);
    });
    expect(screen.getByTestId("photo-stop-card")).toHaveAttribute("data-photo-style", "polaroid");

    // Outro frame -> Finale Poster Card + Climb pin visible
    await act(async () => {
      await renderer.setFrame(95);
    });
    expect(screen.getByTestId("finale-poster-card")).toHaveTextContent("Fictional Meridian Epic");
    expect(screen.getByTestId("svg-climb-pin-climb-1")).toBeInTheDocument();
  });
});
