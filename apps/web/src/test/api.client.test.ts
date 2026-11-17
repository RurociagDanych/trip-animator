// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type ApiError, apiClient, clearCsrfToken, setCsrfToken } from "../lib/api/client";
import {
  analyticsApi,
  authApi,
  photosApi,
  rendersApi,
  shareApi,
  stagesApi,
  telemetryApi,
} from "../lib/api/endpoints";
import { createDefaultRenderSpecV2, normalizeRenderSpecV2 } from "../lib/api/types";

function jsonResponse(body: unknown, status = 200, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
  });
}

describe("API client & CSRF/Idempotency contract", () => {
  beforeEach(() => {
    clearCsrfToken();
    document.cookie = "trip_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("extracts csrf_token from JSON body and X-CSRF-Token header, attaches on mutations, omits on GET", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({
          user: { id: "u1", email: "dev@trip.local", display_name: "Dev", strava_connected: false },
          csrf_token: "json-csrf-123",
          dev_auth_enabled: true,
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: "p1" }, 201))
      .mockResolvedValueOnce(jsonResponse({ ok: true }, 200, { "X-CSRF-Token": "header-csrf-456" }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await authApi.me();
    const getHeaders = new Headers(fetchSpy.mock.calls[0]?.[1]?.headers);
    expect(getHeaders.get("X-CSRF-Token")).toBeNull();

    await apiClient.post("/api/v1/projects", { title: "Fictional Meridian" });
    const postHeaders = new Headers(fetchSpy.mock.calls[1]?.[1]?.headers);
    expect(postHeaders.get("X-CSRF-Token")).toBe("json-csrf-123");

    await apiClient.patch("/api/v1/projects/p1", { revision: 1 });
    await apiClient.delete("/api/v1/projects/p1");
    const delHeaders = new Headers(fetchSpy.mock.calls[3]?.[1]?.headers);
    expect(delHeaders.get("X-CSRF-Token")).toBe("header-csrf-456");
  });

  it("sends Idempotency-Key and X-CSRF-Token on render creation", async () => {
    setCsrfToken("csrf-render-tok");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse(
        {
          id: "job-1",
          project_id: "proj-1",
          status: "queued",
          progress_pct: 0,
          attempt_count: 0,
          spec: createDefaultRenderSpecV2(),
          idempotency_key: "idem-key-12345",
          output_object_key: null,
          thumbnail_key: null,
          error_message: null,
          created_at: "2001-06-14T10:00:00Z",
          updated_at: "2001-06-14T10:00:00Z",
        },
        202,
      ),
    );

    const job = await rendersApi.create("proj-1", createDefaultRenderSpecV2(), "idem-key-12345");
    expect(job.id).toBe("job-1");
    const headers = new Headers(fetchSpy.mock.calls[0]?.[1]?.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-render-tok");
    expect(headers.get("Idempotency-Key")).toBe("idem-key-12345");
  });

  it("uploads GPX multipart FormData with X-CSRF-Token and without forcing JSON Content-Type", async () => {
    setCsrfToken("csrf-upload");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({ id: "stage-1" }, 201),
    );

    const file = new File(["<gpx></gpx>"], "stage1.gpx", { type: "application/gpx+xml" });
    await stagesApi.uploadGpx("proj-1", file, { dayIndex: 2, name: "Stage 1" });

    const reqInit = fetchSpy.mock.calls[0]?.[1];
    const headers = new Headers(reqInit?.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-upload");
    expect(headers.get("Content-Type")).toBeNull();
    expect(reqInit?.body).toBeInstanceOf(FormData);
  });

  it("throws ApiError with RFC 7807 detail/title message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      jsonResponse({ type: "about:blank", title: "Revision conflict", detail: "Revision conflict: expected 3, got 2", status: 409 }, 409),
    );

    await expect(apiClient.patch("/api/v1/projects/p1", { revision: 2 })).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      message: "Revision conflict: expected 3, got 2",
    } satisfies Partial<ApiError>);
  });

  it("normalizes RenderSpecV2 v2.1 extensions and clamps invalid values", () => {
    const normalized = normalizeRenderSpecV2({
      map_style: "topo-relief",
      color_by: "grade",
      terrain_exaggeration: 9.9,
      elevation_curtain: true,
      director_preset: "climb-spotlight",
      photo_card_style: "cinematic",
      telemetry_smoothing: 99,
      show_chapter_cards: true,
      show_transfer_arcs: false,
      show_finale_poster: true,
      show_climb_callouts: false,
      camera_keyframes: [
        {
          progress: 0.8,
          camera_mode: "follow-marker",
          camera_preset: "drone-orbit",
          pitch_deg: 75,
          bearing_offset_deg: 240,
          zoom_offset: 5,
          speed_multiplier: 0.1,
          label: "Summit",
        },
        {
          progress: Number.NaN,
          camera_mode: "fit-track",
          camera_preset: "flat",
          pitch_deg: 10,
          bearing_offset_deg: 0,
          zoom_offset: 0,
          speed_multiplier: 1,
          label: "Invalid",
        },
        {
          progress: 0.2,
          camera_mode: "fit-track",
          camera_preset: "flat",
          pitch_deg: 20,
          bearing_offset_deg: -10,
          zoom_offset: -1,
          speed_multiplier: 2,
          label: "Intro",
        },
      ],
    });

    expect(normalized.map_style).toBe("topo-relief");
    expect(normalized.color_by).toBe("grade");
    expect(normalized.terrain_exaggeration).toBe(3.0);
    expect(normalized.elevation_curtain).toBe(true);
    expect(normalized.director_preset).toBe("climb-spotlight");
    expect(normalized.photo_card_style).toBe("cinematic");
    expect(normalized.telemetry_smoothing).toBe(31);
    expect(normalized.show_chapter_cards).toBe(true);
    expect(normalized.show_transfer_arcs).toBe(false);
    expect(normalized.show_finale_poster).toBe(true);
    expect(normalized.show_climb_callouts).toBe(false);
    expect(normalized.camera_keyframes).toHaveLength(2);
    expect(normalized.camera_keyframes[0]?.label).toBe("Intro");
    expect(normalized.camera_keyframes[1]?.pitch_deg).toBe(60);
    expect(normalized.camera_keyframes[1]?.bearing_offset_deg).toBe(180);
    expect(normalized.camera_keyframes[1]?.zoom_offset).toBe(3);
    expect(normalized.camera_keyframes[1]?.speed_multiplier).toBe(0.25);
  });

  it("calls v2.1 endpoints for track upload, auto-organize, highlights, photo clock/pin, and analytics", async () => {
    setCsrfToken("csrf-v21");
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse({ id: "st-fit", source_format: "fit" }, 201))
      .mockResolvedValueOnce(jsonResponse({ id: "proj-1", stages: [] }, 200))
      .mockResolvedValueOnce(jsonResponse({ project_id: "proj-1", climbs: [] }, 200))
      .mockResolvedValueOnce(jsonResponse({ project_id: "proj-1", climbs: [] }, 200))
      .mockResolvedValueOnce(jsonResponse({ applied_offset_seconds: 3600, dry_run: true, photos: [] }, 200))
      .mockResolvedValueOnce(jsonResponse({ id: "ph-1", match_method: "manual" }, 200))
      .mockResolvedValueOnce(jsonResponse({ total_projects: 3, countries_visited: ["IT"] }, 200))
      .mockResolvedValueOnce(jsonResponse({ trips: [], hex_cells: [] }, 200));

    const fitFile = new File([new Uint8Array([14, 16])], "ride.fit", { type: "application/octet-stream" });
    await stagesApi.uploadTrack("proj-1", fitFile, { dayIndex: 1, activityType: "cycling" });
    expect(String(fetchSpy.mock.calls[0]?.[0])).toBe("/api/v1/projects/proj-1/tracks");

    await stagesApi.autoOrganize("proj-1", { palette_preset: "topo-emerald" });
    expect(String(fetchSpy.mock.calls[1]?.[0])).toBe("/api/v1/projects/proj-1/stages/auto-organize");

    await telemetryApi.highlights("proj-1");
    expect(String(fetchSpy.mock.calls[2]?.[0])).toBe("/api/v1/projects/proj-1/analytics/highlights");

    await shareApi.highlights("slug-1");
    expect(String(fetchSpy.mock.calls[3]?.[0])).toBe("/api/v1/share/slug-1/analytics/highlights");

    await photosApi.shiftClock("proj-1", { offset_seconds: 3600, dry_run: true });
    expect(String(fetchSpy.mock.calls[4]?.[0])).toBe("/api/v1/projects/proj-1/photos/shift-clock");

    await photosApi.pin("proj-1", "ph-1", { lat: 0.2173, lon: 0.4512 });
    expect(String(fetchSpy.mock.calls[5]?.[0])).toBe("/api/v1/projects/proj-1/photos/ph-1/pin");

    await analyticsApi.career({ year: 2001, activityType: "cycling" });
    expect(String(fetchSpy.mock.calls[6]?.[0])).toBe(
      "/api/v1/analytics/career?year=2001&activity_type=cycling",
    );

    await analyticsApi.heatmap({ year: 2001, lod: "low" });
    expect(String(fetchSpy.mock.calls[7]?.[0])).toBe(
      "/api/v1/analytics/heatmap?year=2001&lod=low",
    );
  });
});
