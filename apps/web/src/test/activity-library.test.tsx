import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivityLibraryView } from "../components/dashboard/ActivityLibraryView";
import { vaultApi, type VaultActivityFilters } from "../lib/api/endpoints";
import type { VaultActivityDetailOut, VaultActivityListOut, VaultSyncStatusOut } from "../lib/api/types";

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

function renderLibrary() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ActivityLibraryView /></QueryClientProvider>);
  return client;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ActivityLibraryView", () => {
  it("sends filters to the server and resets pagination when a filter changes", async () => {
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
});
