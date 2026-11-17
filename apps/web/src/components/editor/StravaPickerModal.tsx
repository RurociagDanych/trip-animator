import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertCircle, CalendarRange, Check, CheckSquare, Loader2, Search, Square, X, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { stravaApi } from "../../lib/api/endpoints";
import type {
  StageOut,
  StravaActivityOut,
  StravaBatchImportItem,
  UnitsMode,
} from "../../lib/api/types";
import { formatDistance, formatElapsedTime, formatElevation } from "../../lib/geo/format";
import { coordsToSvgPath, decodeEncodedPolyline } from "../../lib/geo/polyline";
import {
  ActivityIcon,
  getActivityIconMeta,
  inferActivityTypeFromStrava,
  type ActivityType,
} from "../icons";

export interface StravaPickerModalProps {
  projectId: string;
  units: UnitsMode;
  defaultDayIndex: number;
  existingStages?: readonly StageOut[];
  onClose: () => void;
  onImported: (stage?: StageOut) => void;
}

type DatePresetId = "7d" | "30d" | "90d" | "year" | "all";
type DayAssignMode = "same" | "increment";

const BATCH_CHUNK_SIZE = 4;

const INDOOR_VIRTUAL_SPORT_RE =
  /virtual|indoor|zwift|trainer|spinning|treadmill|elliptical|stair|yoga|weight|workout/i;

function isIndoorOrVirtualActivity(act: StravaActivityOut): boolean {
  if (!act.has_gps) return true;
  return INDOOR_VIRTUAL_SPORT_RE.test(act.sport_type);
}

function detectMultiDayClusters(activities: readonly StravaActivityOut[]): StravaActivityOut[][] {
  const dated = activities
    .map((act) => ({ act, ts: Date.parse(act.start_date) }))
    .filter((entry) => Number.isFinite(entry.ts))
    .sort((a, b) => a.ts - b.ts);

  if (dated.length < 2) {
    return [];
  }

  const maxGapMs = 48 * 3600 * 1000;
  const clusters: StravaActivityOut[][] = [];
  let current: typeof dated = [dated[0]!];

  for (let i = 1; i < dated.length; i++) {
    const prev = dated[i - 1]!;
    const item = dated[i]!;
    if (item.ts - prev.ts <= maxGapMs) {
      current.push(item);
    } else {
      if (current.length >= 2) {
        clusters.push(current.map((e) => e.act));
      }
      current = [item];
    }
  }
  if (current.length >= 2) {
    clusters.push(current.map((e) => e.act));
  }
  return clusters;
}

function toIsoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function computePresetDates(preset: DatePresetId): { after: string; before: string } {
  const today = new Date();
  const todayStr = toIsoDate(today);
  if (preset === "all") {
    return { after: "", before: "" };
  }
  if (preset === "year") {
    return { after: `${today.getUTCFullYear()}-01-01`, before: todayStr };
  }
  const days = preset === "7d" ? 7 : preset === "30d" ? 30 : 90;
  const start = new Date(today.getTime() - days * 86_400_000);
  return { after: toIsoDate(start), before: todayStr };
}

export function StravaPickerModal({
  projectId,
  units,
  defaultDayIndex,
  existingStages = [],
  onClose,
  onImported,
}: StravaPickerModalProps) {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [sportFilter, setSportFilter] = useState<ActivityType | "all">("all");
  const [afterDate, setAfterDate] = useState("");
  const [beforeDate, setBeforeDate] = useState("");
  const [activePreset, setActivePreset] = useState<DatePresetId | null>("all");
  const [gpsOnly, setGpsOnly] = useState(true);
  const [hideIndoorVirtual, setHideIndoorVirtual] = useState(true);
  const [clusterNotice, setClusterNotice] = useState<string | null>(null);
  const [dayIndex, setDayIndex] = useState(defaultDayIndex);
  const [dayAssignMode, setDayAssignMode] = useState<DayAssignMode>("increment");
  const [selectedIds, setSelectedIds] = useState<Record<number, boolean>>({});
  const [importedIds, setImportedIds] = useState<Record<number, boolean>>({});
  const [importingId, setImportingId] = useState<number | null>(null);
  const [batchProgress, setBatchProgress] = useState<{
    done: number;
    total: number;
    failures: string[];
  } | null>(null);
  const [importError, setImportError] = useState<string | null>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const existingStravaIds = useMemo(() => {
    const ids = new Set<number>();
    for (const s of existingStages) {
      if (s.strava_activity_id !== null && s.strava_activity_id !== undefined) {
        ids.add(s.strava_activity_id);
      }
    }
    return ids;
  }, [existingStages]);

  const activitiesQuery = useQuery<StravaActivityOut[]>({
    queryKey: ["strava", "activities", page, afterDate, beforeDate],
    queryFn: ({ signal }) =>
      stravaApi.activities(page, 30, {
        after: afterDate || undefined,
        before: beforeDate || undefined,
        signal,
      }),
    retry: false,
  });

  const importMutation = useMutation({
    mutationFn: (activity: StravaActivityOut) =>
      stravaApi.importActivity(projectId, {
        activity_id: activity.id,
        day_index: dayIndex,
        name: activity.name,
        activity_type: inferActivityTypeFromStrava(activity.sport_type, activity.name),
      }),
    onMutate: (activity) => {
      setImportingId(activity.id);
      setImportError(null);
    },
    onSuccess: (stage, activity) => {
      setImportedIds((prev) => ({ ...prev, [activity.id]: true }));
      setSelectedIds((prev) => {
        const next = { ...prev };
        delete next[activity.id];
        return next;
      });
      onImported(stage);
    },
    onError: (err) => {
      setImportError(err instanceof ApiError ? err.message : "Failed to import Strava activity.");
    },
    onSettled: () => {
      setImportingId(null);
    },
  });

  const sportChipCounts = useMemo(() => {
    const counts = new Map<ActivityType, number>();
    for (const act of activitiesQuery.data ?? []) {
      if (gpsOnly && !act.has_gps) continue;
      if (hideIndoorVirtual && isIndoorOrVirtualActivity(act)) continue;
      const inferred = inferActivityTypeFromStrava(act.sport_type, act.name);
      counts.set(inferred, (counts.get(inferred) ?? 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [activitiesQuery.data, gpsOnly, hideIndoorVirtual]);

  const filtered = useMemo(() => {
    const items = activitiesQuery.data ?? [];
    const needle = search.trim().toLowerCase();
    return items.filter((act) => {
      if (gpsOnly && !act.has_gps) return false;
      if (hideIndoorVirtual && isIndoorOrVirtualActivity(act)) return false;
      const detectedType = inferActivityTypeFromStrava(act.sport_type, act.name);
      if (sportFilter !== "all" && detectedType !== sportFilter) return false;
      if (needle && !act.name.toLowerCase().includes(needle)) return false;
      const dateStr = act.start_date.slice(0, 10);
      if (afterDate && dateStr && dateStr < afterDate) return false;
      if (beforeDate && dateStr && dateStr > beforeDate) return false;
      return true;
    });
  }, [activitiesQuery.data, gpsOnly, hideIndoorVirtual, sportFilter, search, afterDate, beforeDate]);

  const selectableVisible = useMemo(
    () =>
      filtered.filter(
        (act) => act.has_gps && !existingStravaIds.has(act.id) && !importedIds[act.id],
      ),
    [filtered, existingStravaIds, importedIds],
  );

  const selectedActivities = useMemo(() => {
    const allItems = activitiesQuery.data ?? [];
    return allItems.filter(
      (act) =>
        Boolean(selectedIds[act.id]) &&
        act.has_gps &&
        !existingStravaIds.has(act.id) &&
        !importedIds[act.id],
    );
  }, [activitiesQuery.data, selectedIds, existingStravaIds, importedIds]);

  const allVisibleSelected =
    selectableVisible.length > 0 && selectableVisible.every((a) => Boolean(selectedIds[a.id]));

  function applyDatePreset(preset: DatePresetId) {
    const { after, before } = computePresetDates(preset);
    setActivePreset(preset);
    setAfterDate(after);
    setBeforeDate(before);
    setPage(1);
  }

  function handleAfterChange(nextAfter: string) {
    setActivePreset(null);
    setAfterDate(nextAfter);
    setPage(1);
  }

  function handleBeforeChange(nextBefore: string) {
    setActivePreset(null);
    setBeforeDate(nextBefore);
    setPage(1);
  }

  function toggleSelectAllVisible() {
    if (allVisibleSelected) {
      setSelectedIds((prev) => {
        const next = { ...prev };
        for (const a of selectableVisible) {
          delete next[a.id];
        }
        return next;
      });
    } else {
      setSelectedIds((prev) => {
        const next = { ...prev };
        for (const a of selectableVisible) {
          next[a.id] = true;
        }
        return next;
      });
    }
  }

  function handleDetectMultiDayTrips() {
    const clusters = detectMultiDayClusters(selectableVisible);
    if (clusters.length === 0) {
      setClusterNotice("No consecutive multi-day clusters found in the current view.");
      return;
    }
    const chosen = [...clusters].sort((a, b) => b.length - a.length)[0]!;
    const nextSelected: Record<number, boolean> = {};
    for (const act of chosen) {
      nextSelected[act.id] = true;
    }
    setSelectedIds(nextSelected);
    setDayAssignMode("increment");
    const firstDate = chosen[0]?.start_date.slice(0, 10) ?? "";
    const lastDate = chosen[chosen.length - 1]?.start_date.slice(0, 10) ?? "";
    setClusterNotice(
      `Detected ${clusters.length} multi-day cluster${clusters.length > 1 ? "s" : ""} — selected ${chosen.length} stages (${firstDate} → ${lastDate}).`,
    );
  }

  async function handleBatchImport() {
    if (selectedActivities.length === 0 || batchProgress !== null) {
      return;
    }
    setImportError(null);
    const ordered = [...selectedActivities].sort((a, b) =>
      a.start_date.localeCompare(b.start_date),
    );
    const total = ordered.length;
    const failures: string[] = [];
    let done = 0;
    setBatchProgress({ done: 0, total, failures: [] });

    for (let offset = 0; offset < ordered.length; offset += BATCH_CHUNK_SIZE) {
      const chunk = ordered.slice(offset, offset + BATCH_CHUNK_SIZE);
      const items: StravaBatchImportItem[] = chunk.map((act, idx) => ({
        activity_id: act.id,
        day_index: dayAssignMode === "increment" ? dayIndex + offset + idx : dayIndex,
        name: act.name,
        activity_type: inferActivityTypeFromStrava(act.sport_type, act.name),
      }));

      try {
        const res = await stravaApi.importBatch(projectId, { items });
        const newlyImported: Record<number, boolean> = {};
        for (const st of res.imported) {
          if (st.strava_activity_id) {
            newlyImported[st.strava_activity_id] = true;
          }
        }
        setImportedIds((prev) => ({ ...prev, ...newlyImported }));
        setSelectedIds((prev) => {
          const next = { ...prev };
          for (const st of res.imported) {
            if (st.strava_activity_id) {
              delete next[st.strava_activity_id];
            }
          }
          return next;
        });
        for (const fail of res.failed) {
          failures.push(`Activity #${fail.activity_id}: ${fail.detail}`);
        }
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : "Batch import request failed";
        failures.push(msg);
      }

      done += chunk.length;
      setBatchProgress({ done, total, failures: [...failures] });
    }

    onImported();
    if (failures.length > 0) {
      setImportError(
        `Completed with ${failures.length} failure(s): ${failures.slice(0, 2).join("; ")}`,
      );
    }
    setBatchProgress(null);
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Import Strava activity"
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
        }
      }}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm"
    >
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <Zap className="h-4 w-4 text-amber-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-ink">Import Strava activities</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close Strava picker"
            className="rounded-md p-1 text-ink-muted hover:bg-elevated hover:text-ink"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Search + spacious non-overlapping Date Toolbar */}
        <div className="space-y-2.5 border-b border-border bg-canvas/50 px-5 py-3">
          <div className="flex flex-wrap items-center gap-2.5">
            <label className="relative min-w-[200px] flex-1">
              <Search
                className="pointer-events-none absolute top-2.5 left-2.5 h-3.5 w-3.5 text-ink-muted"
                aria-hidden="true"
              />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.currentTarget.value)}
                placeholder="Search activity name..."
                className="w-full rounded-md border border-border bg-elevated py-1.5 pr-2.5 pl-8 text-xs text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
              />
            </label>

            <label className="flex items-center gap-1.5 text-xs text-ink-muted">
              <span className="text-[11px]">From</span>
              <input
                type="date"
                aria-label="From date"
                value={afterDate}
                onChange={(e) => handleAfterChange(e.currentTarget.value)}
                className="min-w-[155px] rounded-md border border-border bg-elevated px-2.5 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
              />
            </label>

            <label className="flex items-center gap-1.5 text-xs text-ink-muted">
              <span className="text-[11px]">To</span>
              <input
                type="date"
                aria-label="To date"
                value={beforeDate}
                onChange={(e) => handleBeforeChange(e.currentTarget.value)}
                className="min-w-[155px] rounded-md border border-border bg-elevated px-2.5 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
              />
            </label>

            <label className="flex min-w-[84px] items-center gap-1.5 text-xs text-ink-muted">
              <span className="text-[11px]">Day</span>
              <input
                type="number"
                aria-label="Target day"
                min={1}
                max={365}
                value={dayIndex}
                onChange={(e) => setDayIndex(Math.max(1, Number(e.currentTarget.value) || 1))}
                className="w-14 rounded-md border border-border bg-elevated px-2 py-1.5 text-center text-xs text-ink focus:border-sky-accent focus:outline-none"
              />
            </label>
          </div>

          {/* Quick Date Presets + Sport Filter Chips */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Date range presets">
              {(
                [
                  { id: "7d", label: "7D" },
                  { id: "30d", label: "30D" },
                  { id: "90d", label: "90D" },
                  { id: "year", label: "This Year" },
                  { id: "all", label: "All" },
                ] as const
              ).map((preset) => {
                const active = activePreset === preset.id;
                return (
                  <button
                    key={preset.id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => applyDatePreset(preset.id)}
                    className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
                      active
                        ? "border-sky-accent bg-sky-accent/15 text-sky-accent"
                        : "border-border bg-elevated text-ink-muted hover:text-ink"
                    }`}
                  >
                    {preset.label}
                  </button>
                );
              })}
            </div>

            <select
              aria-label="Bulk day assignment"
              value={dayAssignMode}
              onChange={(e) => setDayAssignMode(e.currentTarget.value as DayAssignMode)}
              className="rounded-md border border-border bg-elevated px-2 py-1 text-[11px] text-ink focus:border-sky-accent focus:outline-none"
            >
              <option value="increment">Auto-increment Day (N, N+1…)</option>
              <option value="same">All selected on Day {dayIndex}</option>
            </select>
          </div>

          {sportChipCounts.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5 pt-0.5" role="group" aria-label="Sport filter chips">
              <button
                type="button"
                aria-pressed={sportFilter === "all"}
                onClick={() => setSportFilter("all")}
                className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
                  sportFilter === "all"
                    ? "border-teal-accent bg-teal-accent/15 text-teal-accent"
                    : "border-border bg-elevated text-ink-muted hover:text-ink"
                }`}
              >
                <span>All sports</span>
              </button>
              {sportChipCounts.map(([type, count]) => {
                const meta = getActivityIconMeta(type);
                const active = sportFilter === type;
                return (
                  <button
                    key={type}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setSportFilter(active ? "all" : type)}
                    className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
                      active
                        ? "border-sky-accent bg-sky-accent/15 text-ink"
                        : "border-border bg-elevated text-ink-muted hover:text-ink"
                    }`}
                  >
                    <ActivityIcon
                      type={type}
                      size={12}
                      accentColor={meta.accentColor}
                      duotoneOpacity={0.28}
                    />
                    <span>{meta.shortLabel}</span>
                    <span className="tabular-telemetry opacity-70">{count}</span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>

        {/* Sub-toolbar: GPS filter, Select All Visible, Batch Import, Pagination */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-2 text-xs text-ink-muted">
          <div className="flex flex-wrap items-center gap-3">
            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={gpsOnly}
                onChange={(e) => setGpsOnly(e.currentTarget.checked)}
                className="rounded border-border accent-sky-accent"
              />
              <span>Only GPS</span>
            </label>

            <label className="inline-flex cursor-pointer items-center gap-1.5">
              <input
                type="checkbox"
                checked={hideIndoorVirtual}
                onChange={(e) => setHideIndoorVirtual(e.currentTarget.checked)}
                className="rounded border-border accent-sky-accent"
              />
              <span>Hide indoor/virtual</span>
            </label>

            <button
              type="button"
              onClick={handleDetectMultiDayTrips}
              disabled={selectableVisible.length < 2}
              className="inline-flex items-center gap-1 rounded border border-teal-accent/50 bg-teal-accent/10 px-2 py-0.5 text-[11px] font-medium text-teal-accent hover:bg-teal-accent/20 disabled:opacity-40"
            >
              <CalendarRange className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Detect Multi-Day Trips</span>
            </button>

            {selectableVisible.length > 0 ? (
              <button
                type="button"
                onClick={toggleSelectAllVisible}
                className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-2 py-0.5 text-[11px] text-ink hover:border-sky-accent/60"
              >
                {allVisibleSelected ? (
                  <CheckSquare className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
                ) : (
                  <Square className="h-3.5 w-3.5" aria-hidden="true" />
                )}
                <span>{allVisibleSelected ? "Clear visible" : "Select all visible"}</span>
              </button>
            ) : null}

            {selectedActivities.length > 0 ? (
              <button
                type="button"
                disabled={batchProgress !== null}
                onClick={() => void handleBatchImport()}
                className="inline-flex items-center gap-1.5 rounded-md bg-sky-accent px-2.5 py-1 text-[11px] font-semibold text-canvas hover:bg-sky-accent/90 disabled:opacity-50"
              >
                {batchProgress ? (
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                ) : (
                  <Zap className="h-3 w-3" aria-hidden="true" />
                )}
                <span>
                  {batchProgress
                    ? `Importing ${batchProgress.done} / ${batchProgress.total}…`
                    : `Import ${selectedActivities.length} selected`}
                </span>
              </button>
            ) : null}
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              className="rounded border border-border bg-elevated px-2 py-0.5 text-[11px] hover:border-sky-accent/50 disabled:opacity-40"
            >
              Prev
            </button>
            <span className="tabular-telemetry text-[11px]">Page {page}</span>
            <button
              type="button"
              disabled={(activitiesQuery.data?.length ?? 0) < 30}
              onClick={() => setPage((p) => p + 1)}
              className="rounded border border-border bg-elevated px-2 py-0.5 text-[11px] hover:border-sky-accent/50 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>

        {/* Batch progress bar */}
        {batchProgress ? (
          <div className="border-b border-border bg-canvas/70 px-5 py-2">
            <div className="mb-1 flex items-center justify-between text-[11px] text-ink">
              <span>Importing Strava batch…</span>
              <span className="tabular-telemetry">
                Imported {batchProgress.done} / {batchProgress.total}
              </span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-elevated">
              <div
                className="h-full bg-sky-accent transition-all duration-200"
                style={{
                  width: `${Math.round((batchProgress.done / Math.max(1, batchProgress.total)) * 100)}%`,
                }}
              />
            </div>
          </div>
        ) : null}

        {clusterNotice ? (
          <div
            data-testid="strava-cluster-notice"
            className="mx-5 mt-3 flex items-center justify-between gap-2 rounded-md border border-teal-accent/40 bg-teal-accent/10 px-3 py-1.5 text-[11px] text-teal-accent"
          >
            <span>{clusterNotice}</span>
            <button
              type="button"
              onClick={() => setClusterNotice(null)}
              aria-label="Dismiss cluster notice"
              className="rounded p-0.5 hover:bg-teal-accent/20"
            >
              <X className="h-3 w-3" aria-hidden="true" />
            </button>
          </div>
        ) : null}

        {importError ? (
          <div
            role="alert"
            className="mx-5 mt-3 flex items-center gap-2 rounded-md border border-coral-accent/40 bg-coral-accent/10 px-3 py-2 text-xs text-ink"
          >
            <AlertCircle className="h-4 w-4 shrink-0 text-coral-accent" aria-hidden="true" />
            <span>{importError}</span>
          </div>
        ) : null}

        <div className="flex-1 overflow-y-auto p-5">
          {activitiesQuery.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-xs text-ink-muted">
              <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
              <span>Loading Strava activities...</span>
            </div>
          ) : activitiesQuery.isError ? (
            <div className="rounded-lg border border-amber-accent/40 bg-amber-accent/10 p-4 text-xs text-ink">
              {activitiesQuery.error instanceof ApiError
                ? activitiesQuery.error.message
                : "Unable to load Strava activities. Ensure Strava is connected on the Dashboard."}
            </div>
          ) : filtered.length === 0 ? (
            <p className="py-10 text-center text-xs text-ink-muted">
              No activities match your current filters.
            </p>
          ) : (
            <ul className="space-y-2">
              {filtered.map((act) => {
                const alreadyInTrip = existingStravaIds.has(act.id);
                const isImported = Boolean(importedIds[act.id]);
                const isBusy = importingId === act.id;
                const isSelected = Boolean(selectedIds[act.id]);
                const detectedType = inferActivityTypeFromStrava(act.sport_type, act.name);
                const meta = getActivityIconMeta(detectedType);
                const canSelect = act.has_gps && !alreadyInTrip && !isImported;
                const sparkCoords = act.summary_polyline
                  ? decodeEncodedPolyline(act.summary_polyline)
                  : [];
                const sparkPath =
                  sparkCoords.length >= 2 ? coordsToSvgPath(sparkCoords, 72, 28, 3) : "";

                return (
                  <li
                    key={act.id}
                    className={`flex items-center justify-between gap-3 rounded-lg border px-3.5 py-2.5 transition-colors ${
                      isSelected
                        ? "border-sky-accent bg-sky-accent/10"
                        : "border-border bg-canvas/60"
                    }`}
                  >
                    <div className="flex min-w-0 items-center gap-3">
                      <input
                        type="checkbox"
                        aria-label={`Select ${act.name}`}
                        disabled={!canSelect || batchProgress !== null}
                        checked={isSelected}
                        onChange={(e) => {
                          const checked = e.currentTarget.checked;
                          setSelectedIds((prev) => {
                            const next = { ...prev };
                            if (checked) {
                              next[act.id] = true;
                            } else {
                              delete next[act.id];
                            }
                            return next;
                          });
                        }}
                        className="h-4 w-4 shrink-0 rounded border-border accent-sky-accent disabled:opacity-35"
                      />

                      {sparkPath ? (
                        <svg
                          viewBox="0 0 72 28"
                          data-testid={`strava-sparkline-${act.id}`}
                          aria-hidden="true"
                          className="h-8 w-16 shrink-0 rounded border border-border/80 bg-surface p-0.5"
                        >
                          <path
                            d={sparkPath}
                            fill="none"
                            stroke={meta.accentColor}
                            strokeWidth={1.75}
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          />
                        </svg>
                      ) : (
                        <span
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-elevated"
                          title={meta.label}
                        >
                          <ActivityIcon
                            type={detectedType}
                            size={18}
                            accentColor={meta.accentColor}
                            duotoneOpacity={0.28}
                          />
                        </span>
                      )}

                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="truncate text-xs font-medium text-ink">{act.name}</span>
                          <span className="shrink-0 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-ink-muted">
                            {act.sport_type}
                          </span>
                          {alreadyInTrip ? (
                            <span className="shrink-0 rounded border border-teal-accent/40 bg-teal-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-teal-accent">
                              Already in trip
                            </span>
                          ) : null}
                        </div>
                        <div className="tabular-telemetry mt-1 flex flex-wrap items-center gap-3 text-[11px] text-ink-muted">
                          <span>{act.start_date.slice(0, 10) || "Unknown date"}</span>
                          <span>{formatDistance(act.distance_m, units)}</span>
                          <span>+{formatElevation(act.elevation_gain_m, units)}</span>
                          <span>{formatElapsedTime(act.moving_time_s)}</span>
                        </div>
                      </div>
                    </div>

                    <button
                      type="button"
                      disabled={!act.has_gps || isBusy || isImported || alreadyInTrip || batchProgress !== null}
                      onClick={() => importMutation.mutate(act)}
                      className={`inline-flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                        isImported || alreadyInTrip
                          ? "border border-teal-accent/50 bg-teal-accent/15 text-teal-accent"
                          : "bg-sky-accent text-canvas hover:bg-sky-accent/90 disabled:opacity-45"
                      }`}
                    >
                      {isBusy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                      ) : isImported || alreadyInTrip ? (
                        <Check className="h-3.5 w-3.5" aria-hidden="true" />
                      ) : null}
                      <span>
                        {alreadyInTrip
                          ? "In trip"
                          : isImported
                            ? "Imported"
                            : isBusy
                              ? "Importing"
                              : "Import"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
