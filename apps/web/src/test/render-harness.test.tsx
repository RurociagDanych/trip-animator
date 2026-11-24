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
    delete window.__TRIP_RENDER_FRAME_COUNT_ONLY__;
    delete window.__TRIP_RENDERER__;
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
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

  it.each(["corner", "cinematic_stop"] as const)("renders a %s manifest photo deterministically with adaptive zoom", async (style) => {
    const spec = createDefaultRenderSpecV2();
    spec.resolution = "1920x1080";
    spec.duration_seconds = 60;
    spec.intro_hold_seconds = 0;
    spec.outro_hold_seconds = 0;
    spec.camera_mode = "follow-marker";
    spec.follow_zoom_mode = "adaptive";
    spec.show_photos = true;
    spec.photo_card_style = style;
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

    const cinematic = style === "cinematic_stop";
    expect(renderer!.getFrameCount()).toBe(cinematic ? 710 : manifest.total_frames);
    expect(screen.getByTestId("render-harness-root")).toHaveAttribute(
      "data-total-frames",
      String(renderer!.getFrameCount()),
    );

    await act(async () => {
      await renderer!.setFrame(cinematic ? 355 : 300);
    });

    const card = screen.getByTestId(cinematic ? "cinematic-photo-stop" : "photo-stop-card");
    if (cinematic) expect(screen.getByAltText("Fictional Midpoint")).toHaveClass("object-contain");
    else expect(card).toHaveTextContent("Fictional Midpoint");
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

    await act(async () => {
      await renderer.setFrame(5);
    });
    expect(screen.getByTestId("chapter-card")).toHaveTextContent("Fictional Meridian Summit");

    await act(async () => {
      await renderer.setFrame(45);
    });
    expect(screen.getByTestId("photo-stop-card")).toHaveAttribute("data-photo-style", "polaroid");

    await act(async () => {
      await renderer.setFrame(95);
    });
    expect(screen.getByTestId("finale-poster-card")).toHaveTextContent("Fictional Meridian Epic");
    expect(screen.getByTestId("svg-climb-pin-climb-1")).toBeInTheDocument();
  });
  describe("cinematic_stop film total and grouped crossfade", () => {
    const svgDataUri =
      "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI4IiBoZWlnaHQ9IjgiPjxyZWN0IHdpZHRoPSI4IiBoZWlnaHQ9IjgiIGZpbGw9IiMzOGJkZjgiLz48L3N2Zz4=";

    function buildManifest(jobId: string, progresses: number[]): RenderManifest {
      const spec = createDefaultRenderSpecV2();
      spec.resolution = "1280x720";
      spec.duration_seconds = 10;
      spec.intro_hold_seconds = 0;
      spec.outro_hold_seconds = 0;
      spec.photo_card_style = "cinematic_stop";
      spec.photo_display_seconds = 2;
      spec.photo_group_gap_seconds = 1;
      return {
        job_id: jobId,
        project_id: "proj-film",
        project_title: "Fictional Film Route",
        spec,
        width: 1280,
        height: 720,
        fps: 10,
        duration_s: 10,
        total_frames: 101,
        is_preview: false,
        stages: [
          {
            id: "s-film",
            name: "Fictional Film Stage",
            day_index: 1,
            stage_order: 0,
            activity_type: "cycling",
            color: "#38bdf8",
            transition: "none",
            distance_m: 10_000,
            elevation_gain_m: 100,
            moving_seconds: 1000,
            elapsed_seconds: 1000,
            has_timestamps: false,
            points: [0, 1].map((i) => ({
              lat: 0.1,
              lon: 0.2 + i * 0.1,
              lon_unwrapped: 0.2 + i * 0.1,
              elev_m: 100,
              cum_dist_m: i * 10_000,
              speed_mps: 5,
              grade_pct: 0,
              elapsed_s: i * 1000,
              ts_utc: null,
              segment_idx: 0,
            })),
          },
        ],
        photos: progresses.map((progress, i) => ({
          id: `photo-${i}`,
          caption: `Fictional Photo ${i}`,
          width: 640,
          height: 480,
          taken_at_utc: null,
          lat: null,
          lon: null,
          matched_lat: 0.1,
          matched_lon: 0.25,
          matched_progress: progress,
          match_method: "manual" as const,
          data_uri: svgDataUri,
        })),
      };
    }

    async function mountHarness(manifest: RenderManifest) {
      window.__TRIP_RENDER_MANIFEST__ = manifest;
      const router = createAppRouter(createMemoryHistory({ initialEntries: [`/render/${manifest.job_id}`] }));
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      render(<App router={router} queryClient={queryClient} />);
      await screen.findByTestId("render-harness-root");
      return window.__TRIP_RENDERER__!;
    }

    it.each([
      ["no photos", [] as number[], 101],
      ["one photo", [0.5], 130],
      ["grouped photos", [0.5, 0.52], 153],
    ])("reports the web film frame count for %s", async (_name, progresses, frames) => {
      const renderer = await mountHarness(buildManifest("job-film-count", progresses));
      await renderer.ready;
      expect(renderer.getFrameCount()).toBe(frames);
    });

    it.each([
      { frameCountOnly: false, jobId: "job-broken-photo" },
      { frameCountOnly: true, jobId: "job-probe-without-photo-bytes" },
    ])("handles unavailable photo bytes with frameCountOnly=$frameCountOnly", async ({ frameCountOnly, jobId }) => {
      const decode = vi.fn().mockRejectedValue(new Error("Image request failed"));
      vi.stubGlobal("Image", class {
        decode() { return decode(); }
      });
      window.__TRIP_RENDER_FRAME_COUNT_ONLY__ = frameCountOnly;
      const renderer = await mountHarness(buildManifest(jobId, [0.5]));
      if (frameCountOnly) {
        await renderer.ready;
        expect(renderer.getFrameCount()).toBe(130);
        expect(decode).not.toHaveBeenCalled();
      } else {
        await expect(renderer.ready).rejects.toThrow("Photo photo-0 could not be decoded");
        expect(decode).toHaveBeenCalledOnce();
      }
    });

    it("shows both photos of a group while crossfading between them", async () => {
      const renderer = await mountHarness(buildManifest("job-film-fade", [0.5, 0.52]));
      await renderer.ready;
      await act(async () => {
        await renderer.setFrame(76);
      });
      expect(screen.getByAltText("Fictional Photo 0")).toBeInTheDocument();
      expect(screen.getByAltText("Fictional Photo 1")).toBeInTheDocument();

      await act(async () => {
        await renderer.setFrame(90);
      });
      expect(screen.queryByAltText("Fictional Photo 0")).not.toBeInTheDocument();
      expect(screen.getByAltText("Fictional Photo 1")).toBeInTheDocument();
    });
  });
});
