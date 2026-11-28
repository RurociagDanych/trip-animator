import { render, screen, waitFor } from "@testing-library/react";
import { createMemoryHistory } from "@tanstack/react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { createDefaultRenderSpecV2, type RenderManifest } from "../lib/api/types";
import { createAppRouter } from "../router";

const BASE = "https://trails.example.test/pointer";
const PINNED_KEY = "builds/2026-11-20/marked-trails.pmtiles";
const PINNED_URL = `${BASE}/${PINNED_KEY}`;
const CURRENT_POINTER = {
  current_build_id: "2026-11-27",
  pmtiles_key: "builds/2026-11-27/marked-trails.pmtiles",
  osm_data_date: "2026-11-26T21:21:00Z",
};

function manifest(overrides: Partial<RenderManifest> = {}, types: string[] = ["hiking"]) {
  const spec = { ...createDefaultRenderSpecV2(), is_preview: false, marked_trail_types: types };
  return {
    job_id: "job-trails",
    project_id: "proj-1",
    project_title: "Trails Render",
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
    marked_trails: {
      build_id: "2026-11-20",
      pmtiles_key: PINNED_KEY,
      osm_data_date: "2026-11-19T10:00:00Z",
      base_url: BASE,
    },
    ...overrides,
  } as unknown as RenderManifest;
}

function mockStore(options: { pinnedStatus?: number; pointerStatus?: number }) {
  const calls: { method: string; url: string }[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url =
      typeof input === "string" ? input : input instanceof Request ? input.url : String(input);
    calls.push({ method: init?.method ?? "GET", url });
    if (url === PINNED_URL) {
      return new Response(null, { status: options.pinnedStatus ?? 200 });
    }
    if (url === `${BASE}/current.json`) {
      const status = options.pointerStatus ?? 200;
      return status === 200
        ? new Response(JSON.stringify(CURRENT_POINTER), { status })
        : new Response(null, { status });
    }
    return new Response("{}", { status: 200 });
  });
  return calls;
}

async function renderHarness(m: RenderManifest) {
  window.__TRIP_RENDER_MANIFEST__ = m;
  const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-trails"] }));
  render(<App router={router} />);
  await waitFor(() => expect(window.__TRIP_RENDERER__).toBeDefined());
  await window.__TRIP_RENDERER__?.ready;
  return screen.findByTestId("map-attribution");
}

describe("render route Marked Trails build", () => {
  afterEach(() => {
    delete window.__TRIP_RENDER_MANIFEST__;
    delete window.__TRIP_RENDER_FRAME_COUNT_ONLY__;
    delete window.__TRIP_RENDERER__;
    vi.restoreAllMocks();
  });

  it("uses the pinned build and shows its data date", async () => {
    const calls = mockStore({});
    const attribution = await renderHarness(manifest());

    expect(attribution).toHaveTextContent("Marked Trails data: 2026-11-19");
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([`HEAD ${PINNED_URL}`]);
  });

  it.each([404, 403])(
    "falls back to the current build when the pinned file answers %i",
    async (status) => {
      const calls = mockStore({ pinnedStatus: status });
      const attribution = await renderHarness(manifest());

      expect(attribution).toHaveTextContent("Marked Trails data: 2026-11-26");
      expect(calls.map((c) => c.url)).toEqual([PINNED_URL, `${BASE}/current.json`]);
    },
  );

  it("renders without trails and still becomes ready when the pinned file and current.json are gone", async () => {
    mockStore({ pinnedStatus: 404, pointerStatus: 404 });
    const attribution = await renderHarness(manifest());

    expect(attribution).toHaveTextContent("© OpenStreetMap contributors");
    expect(attribution).not.toHaveTextContent("Marked Trails data");
  });

  it("renders without trails when the pinned file is gone and the pointer request fails", async () => {
    const calls = mockStore({ pinnedStatus: 404, pointerStatus: 500 });
    const attribution = await renderHarness(manifest());

    expect(attribution).not.toHaveTextContent("Marked Trails data");
    expect(calls).toHaveLength(2);
  });

  it("keeps the pinned build when the availability check itself fails", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("network down"));
    const attribution = await renderHarness(manifest());

    expect(attribution).toHaveTextContent("Marked Trails data: 2026-11-19");
  });

  it("resolves a root-relative pinned base against the page origin", async () => {
    const calls = mockStore({});
    const relative = `${window.location.origin}/tiles/marked-trails`;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      calls.push({ method: init?.method ?? "GET", url: String(input) });
      return new Response(null, { status: 200 });
    });
    const base = manifest();
    const attribution = await renderHarness({
      ...base,
      marked_trails: { ...base.marked_trails!, base_url: "/tiles/marked-trails" },
    });

    expect(attribution).toHaveTextContent("Marked Trails data: 2026-11-19");
    expect(calls.map((c) => c.url)).toContain(`${relative}/${PINNED_KEY}`);
  });

  it("does not contact the trail store without a pin", async () => {
    const calls = mockStore({});
    const attribution = await renderHarness(manifest({ marked_trails: null }));

    expect(attribution).not.toHaveTextContent("Marked Trails data");
    expect(calls).toEqual([]);
  });

  it("does not contact the trail store when no trail type is enabled", async () => {
    const calls = mockStore({});
    const attribution = await renderHarness(manifest({}, []));

    expect(attribution).not.toHaveTextContent("Marked Trails data");
    expect(calls).toEqual([]);
  });

  it("does not contact the trail store when only the frame count is probed", async () => {
    const calls = mockStore({});
    window.__TRIP_RENDER_FRAME_COUNT_ONLY__ = true;
    await renderHarness(manifest());

    expect(calls).toEqual([]);
  });

  it("does not report ready before the pinned build is resolved", async () => {
    let releaseHead: (response: Response) => void = () => {};
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          releaseHead = resolve;
        }),
    );
    window.__TRIP_RENDER_MANIFEST__ = manifest();
    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/render/job-trails"] }));
    render(<App router={router} />);
    await waitFor(() => expect(window.__TRIP_RENDERER__).toBeDefined());

    let ready = false;
    void window.__TRIP_RENDERER__?.ready.then(() => {
      ready = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(ready).toBe(false);
    expect(screen.queryByTestId("map-attribution")).toBeNull();

    releaseHead(new Response(null, { status: 200 }));
    await waitFor(() => expect(ready).toBe(true));
    expect(screen.getByTestId("map-attribution")).toHaveTextContent(
      "Marked Trails data: 2026-11-19",
    );
  });
});
