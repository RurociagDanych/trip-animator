/** Display the searchable activity catalog and its shared selection. */
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { vaultApi, type VaultActivityFilters } from "../../lib/api/endpoints";
import type { ProjectReuseEligibility } from "../../lib/api/types";
import { ActivityLibraryMap } from "./ActivityLibraryMap";
import { LibraryActivityActions } from "./LibraryActivityActions";
import { LibrarySavedViews } from "./LibrarySavedViews";
import { loadActivityLibrarySelection, saveActivityLibrarySelection } from "./activity-library-selection";


const PAGE_SIZE = 50;
const baseInput = "rounded border border-border bg-surface px-2 py-1.5 text-sm text-ink";
const metric = (value: number | null, unit: string) => value == null ? "Unknown" : unit === "h" ? duration(value) : `${(value / unitFactor(unit)).toFixed(1)} ${unit}`;
function unitFactor(unit: string) { return unit === "km" ? 1000 : unit === "m" ? 1 : unit === "h" ? 3600 : 1; }

function duration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "Unknown";
  if (seconds > 0 && seconds < 60) return "<1 min";
  const minutes = Math.max(0, Math.floor(seconds / 60));
  return minutes < 60 ? `${minutes} min` : `${minutes} min (${Math.floor(minutes / 60)}h ${minutes % 60}min)`;
}

/** Render an owner-scoped catalog with filters, map and project actions. */
export function ActivityLibraryView({ ownerId }: { ownerId: string | null }) {
  return <ActivityLibraryOwnerView key={ownerId ?? "anonymous"} ownerId={ownerId} />;
}

function ActivityLibraryOwnerView({ ownerId }: { ownerId: string | null }) {
  const client = useQueryClient();
  const [filters, setFilters] = useState<VaultActivityFilters>(() => ({ sort: "started_at", order: "desc", ...loadActivityLibrarySelection(ownerId), include_hidden: false, offset: 0, limit: PAGE_SIZE }));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const previousVaultVersion = useRef<number | undefined>(undefined);
  const previousSyncStatus = useRef<string | undefined>(undefined);
  const list = useQuery({ queryKey: ["vault", ownerId, "activities", filters], queryFn: ({ signal }) => vaultApi.list(filters, signal), refetchInterval: (query) => query.state.data?.items.some((item) => item.project_reuse_eligibility === "waiting") ? 3000 : false, retry: false });
  const selectedElsewhere = selectedIds.filter((id) => !list.data?.items.some((item) => item.id === id));
  const selectedActivities = useQueries({ queries: selectedElsewhere.map((id) => ({
    queryKey: ["vault", ownerId, "activity", id],
    queryFn: ({ signal }: { signal: AbortSignal }) => vaultApi.get(id, signal),
    refetchInterval: (query: { state: { data?: { project_reuse_eligibility?: ProjectReuseEligibility } } }) => !query.state.data?.project_reuse_eligibility || query.state.data.project_reuse_eligibility === "waiting" ? 3000 : false,
    retry: false,
  })) });
  const awaitingTrack = list.data?.items.some((item) => item.project_reuse_eligibility === "waiting") || selectedActivities.some((query) => !query.data?.project_reuse_eligibility || query.data.project_reuse_eligibility === "waiting");
  const status = useQuery({ queryKey: ["vault", ownerId, "sync-status"], queryFn: ({ signal }) => vaultApi.status(signal), refetchInterval: (query) => awaitingTrack || ["queued", "running", "rate_limited"].includes(query.state.data?.status ?? "") ? 3000 : false, retry: false });
  const activity = useQuery({ queryKey: ["vault", ownerId, "activity", selectedId], queryFn: ({ signal }) => vaultApi.get(selectedId!, signal), enabled: Boolean(selectedId), retry: false });
  const sync = useMutation({ mutationFn: vaultApi.sync, onSuccess: () => { void client.invalidateQueries({ queryKey: ["vault", ownerId] }); } });
  useEffect(() => {
    const version = status.data?.vault_version;
    if (version !== undefined && previousVaultVersion.current !== undefined && version !== previousVaultVersion.current) {
      void client.invalidateQueries({ queryKey: ["vault", ownerId] });
    }
    if (version !== undefined) previousVaultVersion.current = version;
  }, [client, ownerId, status.data?.vault_version]);
  useEffect(() => {
    const syncStatus = status.data?.status;
    if (syncStatus && previousSyncStatus.current && ["queued", "running", "rate_limited"].includes(previousSyncStatus.current) && !["queued", "running", "rate_limited"].includes(syncStatus)) {
      void client.invalidateQueries({ queryKey: ["vault", ownerId] });
    }
    if (syncStatus) previousSyncStatus.current = syncStatus;
  }, [client, ownerId, status.data?.status]);
  const eligibilityById: Record<string, ProjectReuseEligibility> = {};
  for (const selected of selectedActivities) if (!selected.isError && selected.data?.project_reuse_eligibility) eligibilityById[selected.data.id] = selected.data.project_reuse_eligibility;
  for (const item of list.data?.items ?? []) if (item.project_reuse_eligibility) eligibilityById[item.id] = item.project_reuse_eligibility;
  useEffect(() => {
    saveActivityLibrarySelection(ownerId, filters);
  }, [ownerId, filters]);
  function change(key: keyof VaultActivityFilters, value: string | number | boolean | undefined) {
    setFilters((current) => ({ ...current, [key]: value, offset: 0 }));
  }
  const data = list.data;
  const pageCount = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  return <section className="space-y-5" aria-label="Activity library">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-xl font-semibold text-ink">Activity Library</h2><p className="text-sm text-ink-muted">Search and inspect your vaulted activity catalog. Unknown measurements remain unknown.</p></div>
      <button className="rounded bg-sky-accent px-3 py-2 text-sm font-medium text-canvas disabled:opacity-50" disabled={sync.isPending || status.data?.status === "running" || status.data?.status === "queued" || !status.data?.connected} onClick={() => sync.mutate()}>{sync.isPending ? "Starting sync…" : "Sync Strava catalog"}</button>
    </div>
    {status.data && <div className="rounded border border-border bg-surface p-3 text-sm" role="status">{status.data.connected ? `${status.data.phase_label} · ${status.data.processed_activities} processed${status.data.progress_pct == null ? "" : ` · ${Math.round(status.data.progress_pct)}%`}` : "Connect Strava before syncing the activity catalog."}{status.data.last_error && <p className="text-red-600">{status.data.last_error}</p>}{status.data.last_synced_at && <p className="text-ink-muted">Last synced {new Date(status.data.last_synced_at).toLocaleString()}</p>}</div>}
    {sync.isError && <p role="alert" className="text-sm text-red-600">{sync.error instanceof ApiError ? sync.error.message : "Could not start catalog sync."}</p>}
    <ActivityLibraryMap ownerId={ownerId} filters={filters} area={filters.area} onAreaChange={(area) => setFilters((current) => ({ ...current, area, offset: 0 }))} selectedId={selectedId} onSelect={setSelectedId} refreshKey={status.data?.vault_version} />
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
      <label className="text-xs text-ink-muted">Search name<input aria-label="Search activity name" className={`${baseInput} mt-1 w-full`} value={filters.name ?? ""} onChange={(e) => change("name", e.currentTarget.value || undefined)} /></label>
      <label className="text-xs text-ink-muted">Sport<input aria-label="Sport type" className={`${baseInput} mt-1 w-full`} value={filters.sport ?? ""} placeholder="e.g. Ride" onChange={(e) => change("sport", e.currentTarget.value || undefined)} /></label>
      <label className="text-xs text-ink-muted">From<input type="date" className={`${baseInput} mt-1 w-full`} value={filters.start_date ?? ""} onChange={(e) => change("start_date", e.currentTarget.value || undefined)} /></label>
      <label className="text-xs text-ink-muted">To<input type="date" className={`${baseInput} mt-1 w-full`} value={filters.end_date ?? ""} onChange={(e) => change("end_date", e.currentTarget.value || undefined)} /></label>
      <label className="text-xs text-ink-muted">GPS<select aria-label="GPS filter" className={`${baseInput} mt-1 w-full`} value={filters.has_gps === undefined ? "" : String(filters.has_gps)} onChange={(e) => change("has_gps", e.currentTarget.value === "" ? undefined : e.currentTarget.value === "true")}><option value="">Any</option><option value="true">GPS available</option><option value="false">No GPS</option></select></label>
      <label className="flex items-center gap-2 text-xs text-ink-muted"><input aria-label="Include hidden activities" type="checkbox" checked={filters.include_hidden ?? false} onChange={(e) => change("include_hidden", e.currentTarget.checked)} />Include hidden activities</label>
      {([["min_distance_m", "Min distance (km)", 1000], ["max_distance_m", "Max distance (km)", 1000], ["min_elevation_gain_m", "Min climb (m)", 1], ["max_elevation_gain_m", "Max climb (m)", 1], ["min_moving_seconds", "Min moving time (h)", 3600], ["max_moving_seconds", "Max moving time (h)", 3600]] as const).map(([key, label, divisor]) => <label key={key} className="text-xs text-ink-muted">{label}<input type="number" min="0" step="any" className={`${baseInput} mt-1 w-full`} value={filters[key] == null ? "" : Number(filters[key]) / divisor} onChange={(e) => change(key, e.currentTarget.value === "" ? undefined : Number(e.currentTarget.value) * divisor)} /></label>)}
      <label className="text-xs text-ink-muted">Sort by<select className={`${baseInput} mt-1 w-full`} value={filters.sort} onChange={(e) => change("sort", e.currentTarget.value)}><option value="started_at">Date</option><option value="local_date">Local date</option><option value="name">Name</option><option value="distance_m">Distance</option><option value="elevation_gain_m">Elevation</option><option value="moving_seconds">Moving time</option></select></label>
      <label className="text-xs text-ink-muted">Direction<select className={`${baseInput} mt-1 w-full`} value={filters.order} onChange={(e) => change("order", e.currentTarget.value)}><option value="desc">Descending</option><option value="asc">Ascending</option></select></label>
    </div>
    {ownerId && <LibrarySavedViews key={ownerId} ownerId={ownerId} filters={filters} onApply={(next) => setFilters({ ...next, offset: 0, limit: PAGE_SIZE })} />}
    {data && <div className="rounded border border-border bg-surface p-3 text-sm">Filtered summary ({data.summary.activity_count} activities): {metric(data.summary.distance_m, "km")} total distance · {metric(data.summary.elevation_gain_m, "m")} elevation · {metric(data.summary.moving_seconds, "h")} moving · {metric(data.summary.elapsed_seconds, "h")} elapsed · {data.summary.gps_activity_count} with GPS · {data.summary.no_gps_activity_count} without GPS{data.by_sport?.length > 0 && <ul aria-label="Summary by sport" className="mt-2 list-inside list-disc">{data.by_sport.map((sport) => <li key={sport.activity_type}>{sport.activity_type}: {sport.activity_count} activities · {metric(sport.distance_m, "km")} · {sport.gps_activity_count} with GPS</li>)}</ul>}</div>}
    {list.isLoading && <p role="status">Loading activities…</p>}
    {list.isError && <p role="alert" className="text-red-600">{list.error instanceof ApiError ? list.error.message : "Could not load activities."}</p>}
    {selectedActivities.some((query) => query.isError) && <p role="alert">Could not refresh selected activity readiness.</p>}
    {(data || selectedIds.length > 0 || activity.data) && <LibraryActivityActions ownerId={ownerId} selectedIds={selectedIds} eligibilityById={eligibilityById} activity={activity.data} onClearSelection={() => setSelectedIds([])} onDisconnected={() => setSelectedId(null)} />}
    {data?.items.length === 0 && <p className="rounded border border-border p-6 text-center text-sm text-ink-muted">No activities match these filters.</p>}
    {data && data.items.length > 0 && <>

      <div className="overflow-x-auto rounded border border-border"><table className="w-full text-left text-sm"><thead className="bg-surface text-ink-muted"><tr><th className="p-3">Select</th>{["Activity", "Date", "Sport", "Distance", "Climb", "Moving", "GPS / readiness", "Project reuse"].map((x) => <th key={x} className="p-3">{x}</th>)}</tr></thead><tbody>{data.items.map((item) => <tr key={item.id} className="border-t border-border"><td className="p-3"><input type="checkbox" aria-label={`Select ${item.name}`} checked={selectedIds.includes(item.id)} onChange={(e) => { const checked = e.currentTarget.checked; setSelectedIds((ids) => checked ? [...ids, item.id] : ids.filter((id) => id !== item.id)); }} /></td><td className="p-3"><button aria-pressed={selectedId === item.id} className="font-medium text-sky-accent hover:underline" onClick={() => setSelectedId(item.id)}>{item.name}</button>{item.hidden && <span className="ml-2 text-xs text-amber-700">Hidden</span>}</td><td className="p-3">{item.local_date}{item.date_is_utc_fallback ? " (UTC fallback)" : ""}</td><td className="p-3">{item.activity_type}</td><td className="p-3">{metric(item.distance_m, "km")}</td><td className="p-3">{metric(item.elevation_gain_m, "m")}</td><td className="p-3">{metric(item.moving_seconds, "h")}</td><td className="p-3">{item.has_gps ? "GPS" : "No GPS"} · {item.readiness.track_ready ? `track ready (${item.readiness.tile_precision})` : "track not ready"}</td><td className="p-3">{item.project_reuse_eligibility === "ready" ? "Ready" : item.project_reuse_eligibility === "no_gps" ? "No GPS track" : item.project_reuse_eligibility === "unavailable" ? "Unavailable" : "Waiting for reusable track"}</td></tr>)}</tbody></table></div>
      <div className="flex items-center justify-between text-sm"><span>{data.total} activities · page {Math.floor((filters.offset ?? 0) / PAGE_SIZE) + 1} of {pageCount}</span><div className="flex gap-2"><button className="rounded border border-border px-3 py-1.5 disabled:opacity-40" disabled={(filters.offset ?? 0) === 0} onClick={() => setFilters((f) => ({ ...f, offset: Math.max(0, (f.offset ?? 0) - PAGE_SIZE) }))}>Previous</button><button className="rounded border border-border px-3 py-1.5 disabled:opacity-40" disabled={(filters.offset ?? 0) + PAGE_SIZE >= data.total} onClick={() => setFilters((f) => ({ ...f, offset: (f.offset ?? 0) + PAGE_SIZE }))}>Next</button></div></div>
    </>}

    {selectedId && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={() => setSelectedId(null)}><div role="dialog" aria-modal="true" aria-label="Activity details" className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded-lg bg-canvas p-5 text-ink shadow-xl" onClick={(e) => e.stopPropagation()}><div className="flex justify-between gap-3"><h3 className="text-lg font-semibold text-ink">{activity.data?.name ?? "Activity details"}</h3><button aria-label="Close details" onClick={() => setSelectedId(null)}>Close</button></div>{activity.isLoading && <p>Loading details…</p>}{activity.isError && <p role="alert" className="text-red-600">{activity.error instanceof ApiError ? activity.error.message : "Could not load activity details."}</p>}{activity.data && <div className="mt-4 space-y-3 text-sm"><p>{activity.data.local_date} · {activity.data.activity_type} · {activity.data.source_provider}</p><p>Readiness: metadata {activity.data.readiness.metadata_available ? "available" : "unavailable"}; GPS {activity.data.readiness.has_gps ? "available" : "unavailable"}; track {activity.data.readiness.track_ready ? `ready (${activity.data.readiness.tile_precision})` : "not ready"}.</p><p>Distance {metric(activity.data.distance_m, "km")} · elevation {metric(activity.data.elevation_gain_m, "m")} · moving {metric(activity.data.moving_seconds, "h")} · elapsed {metric(activity.data.elapsed_seconds, "h")}</p><p>Heart rate {activity.data.avg_hr_bpm == null ? "Unknown" : `${activity.data.avg_hr_bpm} bpm`} · power {activity.data.avg_watts == null ? "Unknown" : `${activity.data.avg_watts} W`} · cadence {activity.data.avg_cadence ?? "Unknown"}</p>{activity.data.primary_country && <p>Country: {activity.data.primary_country}</p>}{activity.data.source_url && <p><a className="text-sky-accent underline" href={activity.data.source_url} target="_blank" rel="noreferrer">Open source activity</a></p>}<div><h4 className="font-medium">Project usages ({activity.data.project_usages.length})</h4>{activity.data.project_usages.length ? <ul className="list-inside list-disc">{activity.data.project_usages.map((usage) => <li key={usage.stage_id}>{usage.project_title} — {usage.stage_name}</li>)}</ul> : <p>Not used in a project.</p>}</div><LibraryActivityActions ownerId={ownerId} selectedIds={selectedIds} eligibilityById={eligibilityById} activity={activity.data} onClearSelection={() => setSelectedIds([])} onDisconnected={() => setSelectedId(null)} /></div>}</div></div>}
  </section>;
}
