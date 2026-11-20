import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivityLibraryMap } from "../components/dashboard/ActivityLibraryMap";
import { ActivityLibraryView } from "../components/dashboard/ActivityLibraryView";
import { vaultApi, type VaultActivityFilters } from "../lib/api/endpoints";
import { apiClient } from "../lib/api/client";
import type { VaultActivityDetailOut, VaultActivityListOut, VaultSyncStatusOut } from "../lib/api/types";

type MapHarnessEvent = { features?: Array<{ properties?: { id?: unknown } }> };
type MapHarnessInstance = {
  handlers: Record<string, (event?: MapHarnessEvent) => void>;
  layerHandlers: Record<string, (event?: MapHarnessEvent) => void>;
  source: { setData: ReturnType<typeof vi.fn> };
  fitBounds: ReturnType<typeof vi.fn>;
  setPaintProperty: ReturnType<typeof vi.fn>;
};
const mapHarness = vi.hoisted(() => ({ instances: [] as MapHarnessInstance[], holdLoad: false, bounds: [-2, 48, 3, 52] }));
vi.mock("maplibre-gl", () => ({
  default: { Map: class {
    handlers: Record<string, (event?: MapHarnessEvent) => void> = {};
    layerHandlers: Record<string, (event?: MapHarnessEvent) => void> = {};
    source = { setData: vi.fn() };
    constructor() { mapHarness.instances.push(this); }
    on(event: string, layerOrHandler: string | ((event?: MapHarnessEvent) => void), handler?: (event?: MapHarnessEvent) => void) {
      if (typeof layerOrHandler === "string") this.layerHandlers[layerOrHandler] = handler!;
      else this.handlers[event] = layerOrHandler;
      if (event === "load" && typeof layerOrHandler === "function" && !mapHarness.holdLoad) layerOrHandler();
    }
    addSource() {}
    addLayer() {}
    getSource() { return this.source; }
    getLayer() { return true; }
    setPaintProperty = vi.fn();
    getBounds() { return { getWest: () => mapHarness.bounds[0]!, getSouth: () => mapHarness.bounds[1]!, getEast: () => mapHarness.bounds[2]!, getNorth: () => mapHarness.bounds[3]! }; }
    fitBounds = vi.fn();
    remove() {}
  } },
}));

const readiness = { metadata_available: true, has_gps: true, track_ready: true, tile_precision: "full_stream" as const };
const item = (id: string, name: string) => ({
  id,
  name,
  activity_type: "Ride",
  raw_sport_type: "Ride",
  source_provider: "strava",
  started_at: "2026-06-18T08:00:00Z",
  local_date: "2026-06-18",
  timezone_name: "Europe/Paris",
  date_is_utc_fallback: false,
  distance_m: null,
  elevation_gain_m: null,
  moving_seconds: null,
  elapsed_seconds: null,
  has_gps: true,
  readiness,
});
const detail = (id: string, name: string): VaultActivityDetailOut => ({
  ...item(id, name),
  strava_activity_id: Number(id),
  started_at_local: null,
  utc_offset_s: 0,
  elevation_loss_m: null,
  elev_high_m: null,
  elev_low_m: null,
  avg_speed_mps: null,
  max_speed_mps: null,
  avg_hr_bpm: null,
  max_hr_bpm: null,
  avg_watts: null,
  weighted_watts: null,
  kilojoules: null,
  avg_cadence: null,
  suffer_score: null,
  gear_id: null,
  gear_name: null,
  is_commute: false,
  is_indoor_virtual: false,
  is_private: false,
  summary_polyline: null,
  tile_count_z14: 0,
  new_tiles_z14_count: 0,
  primary_country: null,
  source_url: null,
  project_usages: [],
});
const listResult = (items: VaultActivityListOut["items"], offset: number, total = items.length): VaultActivityListOut => ({
  items,
  total,
  offset,
  limit: 50,
  summary: {
    activity_count: total,
    distance_m: null,
    distance_known_count: 0,
    elevation_gain_m: null,
    elevation_gain_known_count: 0,
    moving_seconds: null,
    moving_seconds_known_count: 0,
    elapsed_seconds: null,
    elapsed_seconds_known_count: 0,
    gps_activity_count: total,
    no_gps_activity_count: 0,
  },
});
const syncStatus = (vault_version: number, status = "idle"): VaultSyncStatusOut => ({
  connected: true,
  status,
  phase: status,
  phase_label: status === "idle" ? "Ready to sync" : "Syncing",
  mode: "incremental",
  progress_pct: null,
  processed_activities: 0,
  estimated_total_activities: null,
  imported_count: 0,
  vault_version,
  last_synced_at: null,
  last_error: null,
});
const mapResult = {
  type: "FeatureCollection" as const,
  features: [{ type: "Feature" as const, geometry: { type: "LineString" as const, coordinates: [[0, 0], [1, 1]] }, properties: { id: "1", name: "Map Route", precision: "summary_polyline" as const } }],
  total: 1, drawable_count: 1, no_gps_count: 0, unavailable_count: 0, returned_count: 1, truncated: true, geographic_excluded_count: 0, unevaluated_count: 2,
};

function renderLibrary() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ActivityLibraryView /></QueryClientProvider>);
  return client;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  mapHarness.instances.length = 0;
  mapHarness.holdLoad = false;
  mapHarness.bounds = [-2, 48, 3, 52];
});

describe("ActivityLibraryView", () => {
  it("sends the geometry page offset to the HTTP endpoint without a list page-size limit", async () => {
    const get = vi.spyOn(apiClient, "get").mockResolvedValue(mapResult);
    const controller = new AbortController();
    await vaultApi.map({ name: "Italy ride", offset: 500, limit: 50 }, controller.signal);
    expect(get).toHaveBeenCalledWith("/api/v1/vault/activities/map?name=Italy+ride&offset=500", controller.signal);
  });

  it("sends filters to the server and resets pagination when a filter changes", async () => {
    vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    const list = vi.spyOn(vaultApi, "list").mockImplementation(async (filters: VaultActivityFilters) =>
      listResult([item("first", "Fictional Valley Ride")], filters.offset ?? 0, 60));
    vi.spyOn(vaultApi, "status").mockResolvedValue(syncStatus(1));
    renderLibrary();

    await screen.findByText("Fictional Valley Ride");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 50 }), expect.anything()));
    fireEvent.change(screen.getByRole("textbox", { name: "Search activity name" }), { target: { value: "summit" } });
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ name: "summit", offset: 0 }), expect.anything()));
    expect(screen.getByText(/page 1 of 2/)).toBeInTheDocument();
  });

  it("does not show stale details after selection changes and preserves detail errors", async () => {
    vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    vi.spyOn(vaultApi, "list").mockResolvedValue(listResult([item("1", "Old Activity"), item("2", "New Activity")], 0));
    vi.spyOn(vaultApi, "status").mockResolvedValue(syncStatus(1));
    const get = vi.spyOn(vaultApi, "get").mockImplementation(async (id) => {
      if (id === "2") throw new Error("details unavailable");
      return detail(id, "Old Activity");
    });
    renderLibrary();

    fireEvent.click(await screen.findByRole("button", { name: "Old Activity" }));
    const dialog = await screen.findByRole("dialog", { name: "Activity details" });
    await within(dialog).findByText(/Old Activity/);
    fireEvent.click(screen.getByRole("button", { name: "Close details" }));
    fireEvent.click(screen.getByRole("button", { name: "New Activity" }));
    const updatedDialog = await screen.findByRole("dialog", { name: "Activity details" });
    expect(await within(updatedDialog).findByRole("alert")).toHaveTextContent("Could not load activity details.");
    expect(within(updatedDialog).queryByText(/Old Activity/)).not.toBeInTheDocument();
    expect(get).toHaveBeenNthCalledWith(2, "2", expect.anything());
  });

  it("renders unknown measurements and refreshes catalog when sync completion changes vault version", async () => {
    vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    const list = vi.spyOn(vaultApi, "list").mockResolvedValue(listResult([item("1", "Unknown Metrics Ride")], 0));
    let statusCall = 0;
    vi.spyOn(vaultApi, "status").mockImplementation(async () => {
      statusCall += 1;
      return syncStatus(statusCall < 2 ? 1 : 2);
    });
    vi.spyOn(vaultApi, "sync").mockResolvedValue(syncStatus(1, "queued"));
    renderLibrary();

    await screen.findByText("Unknown Metrics Ride");
    expect(screen.getAllByText("Unknown").length).toBeGreaterThanOrEqual(3);
    const initialListCalls = list.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Sync Strava catalog" }));
    await waitFor(() => expect(statusCall).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(list.mock.calls.length).toBeGreaterThan(initialListCalls));
    expect(vaultApi.sync).toHaveBeenCalledOnce();
  });

  it("shares filters, filters an area only on explicit action, and links route selection to details", async () => {
    const list = vi.spyOn(vaultApi, "list").mockResolvedValue(listResult([item("1", "Map Route")], 0));
    vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    vi.spyOn(vaultApi, "status").mockResolvedValue(syncStatus(1));
    const get = vi.spyOn(vaultApi, "get").mockResolvedValue(detail("1", "Map Route"));
    renderLibrary();

    await screen.findByRole("button", { name: "Map Route" });
    await waitFor(() => expect(vaultApi.map).toHaveBeenCalledWith(expect.objectContaining({ sort: "started_at" }), expect.any(AbortSignal)));
    fireEvent.change(screen.getByRole("textbox", { name: "Search activity name" }), { target: { value: "ridge" } });
    await waitFor(() => expect(vaultApi.map).toHaveBeenLastCalledWith(expect.objectContaining({ name: "ridge" }), expect.any(AbortSignal)));
    const map = mapHarness.instances[0]!;
    act(() => map.handlers.moveend!());
    expect(vaultApi.map).not.toHaveBeenLastCalledWith(expect.objectContaining({ area: "-2,48,3,52" }), expect.any(AbortSignal));
    fireEvent.click(screen.getByRole("button", { name: "Filter this area" }));
    await waitFor(() => expect(vaultApi.map).toHaveBeenLastCalledWith(expect.objectContaining({ area: "-2,48,3,52", name: "ridge" }), expect.any(AbortSignal)));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ area: "-2,48,3,52", name: "ridge", offset: 0 }), expect.anything()));

    map.layerHandlers["activity-routes-line"]!({ features: [{ properties: { id: "1" } }] });
    await screen.findByRole("dialog", { name: "Activity details" });
    expect(get).toHaveBeenCalledWith("1", expect.anything());
    expect(screen.getByRole("button", { name: "Map Route" })).toHaveAttribute("aria-pressed", "true");
    expect(map.setPaintProperty).toHaveBeenCalledWith("activity-routes-line", "line-width", expect.any(Array));
    expect(screen.getByText(/2 tracks not evaluated \(evaluation limit\)/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear area" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ area: undefined }), expect.anything()));
  });

  it("applies route data and fits its initial geometry when map load follows data", async () => {
    mapHarness.holdLoad = true;
    vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    const list = vi.spyOn(vaultApi, "list").mockResolvedValue(listResult([item("1", "Map Route")], 0));
    vi.spyOn(vaultApi, "status").mockResolvedValue(syncStatus(1));
    renderLibrary();

    await screen.findByText(/1 routes shown/);
    const map = mapHarness.instances[0]!;
    expect(map.source.setData).not.toHaveBeenCalled();
    act(() => map.handlers.load!());

    expect(map.source.setData).toHaveBeenCalledWith(mapResult);
    expect(map.fitBounds).toHaveBeenCalledWith([[0, 0], [1, 1]], expect.objectContaining({ padding: expect.any(Number), maxZoom: expect.any(Number) }));
    expect(screen.getByRole("button", { name: "Filter this area" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Filter this area" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(expect.objectContaining({ area: "-2,48,3,52" }), expect.anything()));
  });

  it("fits the maximum response geometry without exceeding browser argument limits", async () => {
    const coordinates = Array.from({ length: 100_000 }, (_, index) => [index % 180, index % 90]);
    vi.spyOn(vaultApi, "map").mockResolvedValue({
      ...mapResult,
      features: [{ ...mapResult.features[0]!, geometry: { type: "LineString", coordinates } }],
    });
    const min = Math.min;
    const max = Math.max;
    const bounded = (fn: typeof Math.min) => (...values: number[]) => {
      if (values.length > 10_000) throw new RangeError("Browser argument limit exceeded");
      return fn(...values);
    };
    vi.spyOn(Math, "min").mockImplementation(bounded(min));
    vi.spyOn(Math, "max").mockImplementation(bounded(max));
    render(<ActivityLibraryMap filters={{}} area={undefined} onAreaChange={vi.fn()} selectedId={null} onSelect={vi.fn()} />);
    await waitFor(() => expect(mapHarness.instances[0]!.fitBounds).toHaveBeenCalledWith([[0, 0], [179, 89]], expect.anything()));
  });

  it("automatically loads older routes beyond the first page and initially fits Poland and Italy together", async () => {
    const poland = Array.from({ length: 500 }, (_, index) => ({
      ...mapResult.features[0]!,
      geometry: { type: "LineString" as const, coordinates: [[19, 50], [20, 51]] },
      properties: { ...mapResult.features[0]!.properties, id: `poland-${index}` },
    }));
    const italy = { ...mapResult.features[0]!, geometry: { type: "LineString" as const, coordinates: [[12, 42], [13, 43]] }, properties: { ...mapResult.features[0]!.properties, id: "italy" } };
    let completeSecondPage!: (value: typeof mapResult) => void;
    const secondPage = new Promise<typeof mapResult>((resolve) => { completeSecondPage = resolve; });
    const map = vi.spyOn(vaultApi, "map").mockImplementation(async (filters) => filters.offset === 500 ? secondPage : {
      ...mapResult, features: poland, total: 501, returned_count: 500, drawable_count: 500, next_offset: 500, unevaluated_count: 1,
    });
    const onAreaChange = vi.fn();
    render(<ActivityLibraryMap filters={{ offset: 50 }} area={undefined} onAreaChange={onAreaChange} selectedId={null} onSelect={vi.fn()} />);
    await waitFor(() => expect(map).toHaveBeenCalledTimes(2));
    expect(map).toHaveBeenNthCalledWith(1, { offset: 0, area: undefined }, expect.any(AbortSignal));
    expect(map).toHaveBeenNthCalledWith(2, { offset: 500, area: undefined }, expect.any(AbortSignal));
    expect(screen.getByText("Loading activity routes…")).toBeInTheDocument();
    expect(mapHarness.instances[0]!.fitBounds).not.toHaveBeenCalled();
    await act(async () => completeSecondPage({ ...mapResult, features: [italy], total: 501, truncated: false, unevaluated_count: 0 }));
    await screen.findByText(/501 routes shown of 501 matching activities/);
    const rendered = mapHarness.instances[0]!.source.setData.mock.lastCall![0];
    expect(rendered.features).toHaveLength(501);
    expect(rendered.features.at(-1).properties.id).toBe("italy");
    expect(rendered.truncated).toBe(false);
    expect(mapHarness.instances[0]!.fitBounds).toHaveBeenCalledExactlyOnceWith([[12, 42], [20, 51]], expect.anything());
    act(() => mapHarness.instances[0]!.handlers.moveend!());
    expect(onAreaChange).not.toHaveBeenCalled();
    expect(map).toHaveBeenCalledTimes(2);
  });

  it("does not show a successful partial map when a later route page fails", async () => {
    vi.spyOn(vaultApi, "map").mockResolvedValueOnce({ ...mapResult, next_offset: 500 }).mockRejectedValueOnce(new Error("Network failure"));
    render(<ActivityLibraryMap filters={{}} area={undefined} onAreaChange={vi.fn()} selectedId={null} onSelect={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load activity routes.");
    expect(screen.queryByText(/routes shown/)).not.toBeInTheDocument();
    expect(mapHarness.instances[0]!.fitBounds).not.toHaveBeenCalled();
  });

  it("cancels obsolete pagination when filters change and ignores its late response", async () => {
    let resolveObsolete!: (value: typeof mapResult) => void;
    const obsolete = new Promise<typeof mapResult>((resolve) => { resolveObsolete = resolve; });
    const map = vi.spyOn(vaultApi, "map").mockResolvedValueOnce({ ...mapResult, next_offset: 500 }).mockReturnValueOnce(obsolete).mockResolvedValue(mapResult);
    const props = { filters: {}, area: undefined, onAreaChange: vi.fn(), selectedId: null, onSelect: vi.fn() };
    const view = render(<ActivityLibraryMap {...props} />);
    await waitFor(() => expect(map).toHaveBeenCalledTimes(2));
    const oldSignal = map.mock.calls[1]![1]!;
    view.rerender(<ActivityLibraryMap {...props} filters={{ name: "new selection" }} />);
    await screen.findByText(/1 routes shown/);
    expect(oldSignal.aborted).toBe(true);
    await act(async () => resolveObsolete({ ...mapResult, features: [], returned_count: 0 }));
    expect(screen.getByText(/1 routes shown/)).toBeInTheDocument();
    expect(mapHarness.instances[0]!.source.setData.mock.lastCall![0].features).toHaveLength(1);
  });

  it("rejects a non-advancing pagination cursor instead of looping", async () => {
    const map = vi.spyOn(vaultApi, "map").mockResolvedValue({ ...mapResult, next_offset: 0 });
    render(<ActivityLibraryMap filters={{}} area={undefined} onAreaChange={vi.fn()} selectedId={null} onSelect={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load activity routes.");
    expect(map).toHaveBeenCalledOnce();
  });

  it("preserves a dateline-crossing viewport instead of selecting all longitudes", async () => {
    mapHarness.bounds = [178, -5, 182, 5];
    vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    const onAreaChange = vi.fn();
    render(<ActivityLibraryMap filters={{}} area={undefined} onAreaChange={onAreaChange} selectedId={null} onSelect={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Filter this area" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Filter this area" }));
    expect(onAreaChange).toHaveBeenCalledWith("178,-5,-178,5");
  });

  it("refetches map data when the Vault refresh version changes without recreating the map", async () => {
    const map = vi.spyOn(vaultApi, "map").mockResolvedValue(mapResult);
    const props = { filters: {}, area: undefined, onAreaChange: vi.fn(), selectedId: null, onSelect: vi.fn() };
    const view = render(<ActivityLibraryMap {...props} refreshKey={1} />);
    await waitFor(() => expect(map).toHaveBeenCalledOnce());

    view.rerender(<ActivityLibraryMap {...props} refreshKey={2} />);
    await waitFor(() => expect(map).toHaveBeenCalledTimes(2));
    expect(mapHarness.instances).toHaveLength(1);
  });
});
