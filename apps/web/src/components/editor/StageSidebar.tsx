import { Link } from "@tanstack/react-router";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BedDouble,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Coffee,
  GripVertical,
  Layers,
  Loader2,
  Scissors,
  Sparkles,
  Tent,
  TrainFront,
  Trash2,
  Upload,
  Utensils,
  X,
  Zap,
} from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { stagesApi } from "../../lib/api/endpoints";
import type {
  PalettePreset,
  ProjectPhotoOut,
  RenderSpecV2,
  StageOut,
  TransitionIcon,
} from "../../lib/api/types";
import { formatDistance, formatElevation } from "../../lib/geo/format";
import type { MatchedPhoto } from "../../lib/geo/photos";
import type { SidebarTab } from "../../lib/state/editor-store";
import { PhotoStopsSection } from "./PhotoStopsSection";
import {
  ACTIVITY_ICON_LIST,
  ActivityIcon,
  getActivityIconMeta,
  normalizeActivityType,
  type ActivityType,
} from "../icons";

const STAGE_COLOR_SWATCHES = [
  "#38bdf8",
  "#14b8a6",
  "#f59e0b",
  "#f97316",
  "#ec4899",
  "#a855f7",
  "#22c55e",
] as const;

const TRANSITION_OPTIONS: readonly { value: TransitionIcon; label: string }[] = [
  { value: "none", label: "None" },
  { value: "bed", label: "Hotel / Bed" },
  { value: "tent", label: "Camp / Tent" },
  { value: "meal", label: "Meal" },
  { value: "coffee", label: "Coffee" },
  { value: "transport", label: "Transport" },
];

interface UploadQueueItem {
  key: string;
  filename: string;
  status: "uploading" | "succeeded" | "failed";
  error?: string;
}

export interface StageSidebarProps {
  projectId: string;
  stages: readonly StageOut[];
  spec: RenderSpecV2;
  selectedStageId: string | null;
  stravaConnected: boolean;
  photos?: readonly ProjectPhotoOut[];
  matchedPhotos?: readonly MatchedPhoto[];
  googlePhotosConfigured?: boolean;
  activeTab?: SidebarTab;
  pinModePhotoId?: string | null;
  onTabChange?: (tab: SidebarTab) => void;
  onTogglePinMode?: (photoId: string | null) => void;
  onSelectStage: (stageId: string | null) => void;
  onStagesMutated: () => Promise<void> | void;
  onPhotosMutated?: () => Promise<void> | void;
  onStageColorChange: (stageId: string, colorHex: string) => void;
  onStageTransitionChange: (stageId: string, transition: TransitionIcon) => void;
  onOpenStravaModal: () => void;
}

function TransitionBadgeIcon({ icon }: { icon: TransitionIcon }) {
  switch (icon) {
    case "bed":
      return <BedDouble className="h-3 w-3" aria-hidden="true" />;
    case "meal":
      return <Utensils className="h-3 w-3" aria-hidden="true" />;
    case "coffee":
      return <Coffee className="h-3 w-3" aria-hidden="true" />;
    case "tent":
      return <Tent className="h-3 w-3" aria-hidden="true" />;
    case "transport":
      return <TrainFront className="h-3 w-3" aria-hidden="true" />;
    default:
      return null;
  }
}

export function StageSidebar({
  projectId,
  stages,
  spec,
  selectedStageId,
  stravaConnected,
  photos = [],
  matchedPhotos = [],
  googlePhotosConfigured = false,
  activeTab: controlledTab,
  pinModePhotoId = null,
  onTabChange,
  onTogglePinMode,
  onSelectStage,
  onStagesMutated,
  onPhotosMutated,
  onStageColorChange,
  onStageTransitionChange,
  onOpenStravaModal,
}: StageSidebarProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [internalTab, setInternalTab] = useState<SidebarTab>("stages");
  const activeTab = controlledTab ?? internalTab;
  const setActiveTab = (tab: SidebarTab) => {
    setInternalTab(tab);
    onTabChange?.(tab);
  };
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploads, setUploads] = useState<UploadQueueItem[]>([]);
  const [dragStageId, setDragStageId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [uploadsExpanded, setUploadsExpanded] = useState(false);
  const [warningsExpanded, setWarningsExpanded] = useState(false);
  const [collapsedDays, setCollapsedDays] = useState<Record<number, boolean>>({});
  const [palettePreset, setPalettePreset] = useState<PalettePreset>("alpine-dawn");

  const selectedStage = useMemo(
    () => stages.find((s) => s.id === selectedStageId) ?? null,
    [stages, selectedStageId],
  );

  const orderedStages = useMemo(
    () =>
      [...stages].sort(
        (a, b) => a.day_index - b.day_index || a.stage_order - b.stage_order,
      ),
    [stages],
  );

  const groupedByDay = useMemo(() => {
    const groups = new Map<number, StageOut[]>();
    for (const s of orderedStages) {
      const existing = groups.get(s.day_index);
      if (existing) {
        existing.push(s);
      } else {
        groups.set(s.day_index, [s]);
      }
    }
    return Array.from(groups.entries()).sort((a, b) => a[0] - b[0]);
  }, [orderedStages]);

  const visualStages = useMemo(
    () => groupedByDay.flatMap(([, dayStages]) => dayStages),
    [groupedByDay],
  );

  const visualIndexById = useMemo(() => {
    const map = new Map<string, number>();
    visualStages.forEach((s, idx) => map.set(s.id, idx));
    return map;
  }, [visualStages]);

  const aggregatedWarnings = useMemo(() => {
    const byMessage = new Map<
      string,
      { code: string; message: string; stageNames: string[] }
    >();
    for (const stage of visualStages) {
      for (const w of stage.warnings) {
        const key = `${w.code}::${w.message}`;
        const existing = byMessage.get(key);
        if (existing) {
          if (!existing.stageNames.includes(stage.name)) {
            existing.stageNames.push(stage.name);
          }
        } else {
          byMessage.set(key, {
            code: w.code,
            message: w.message,
            stageNames: [stage.name],
          });
        }
      }
    }
    return Array.from(byMessage.values());
  }, [visualStages]);

  async function handleTrackFiles(fileList: FileList | File[]) {
    const files = Array.from(fileList).filter((f) => {
      const lower = f.name.toLowerCase();
      return (
        lower.endsWith(".gpx") ||
        lower.endsWith(".fit") ||
        lower.endsWith(".tcx") ||
        f.type.includes("gpx") ||
        f.type.includes("xml")
      );
    });
    if (files.length === 0) {
      setActionError("Select one or more .gpx, .fit, or .tcx files to upload.");
      return;
    }
    setActionError(null);

    const maxDay = orderedStages.reduce((acc, s) => Math.max(acc, s.day_index), 0);
    let nextDay = maxDay === 0 ? 1 : maxDay;

    const initialQueue: UploadQueueItem[] = files.map((file, i) => ({
      key: `${Date.now()}-${i}-${file.name}`,
      filename: file.name,
      status: "uploading",
    }));
    setUploads(initialQueue);

    for (let i = 0; i < files.length; i++) {
      const file = files[i]!;
      const itemKey = initialQueue[i]!.key;
      const lower = file.name.toLowerCase();
      try {
        const created =
          lower.endsWith(".fit") || lower.endsWith(".tcx")
            ? await stagesApi.uploadTrack(projectId, file, { dayIndex: nextDay })
            : await stagesApi.uploadGpx(projectId, file, { dayIndex: nextDay });
        nextDay += 1;
        setUploads((prev) =>
          prev.map((u) => (u.key === itemKey ? { ...u, status: "succeeded" } : u)),
        );
        onSelectStage(created.id);
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : "Track upload failed";
        setUploads((prev) =>
          prev.map((u) => (u.key === itemKey ? { ...u, status: "failed", error: msg } : u)),
        );
      }
    }

    await onStagesMutated();
  }

  async function handleAutoOrganize() {
    setBusyAction("auto-organize");
    setActionError(null);
    try {
      await stagesApi.autoOrganize(projectId, {
        palette_preset: palettePreset,
        assign_overnight_icons: true,
      });
      await onStagesMutated();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Failed to auto-organize trip");
    } finally {
      setBusyAction(null);
    }
  }

  async function moveStage(stageId: string, direction: -1 | 1) {
    const idx = visualStages.findIndex((s) => s.id === stageId);
    const targetIdx = idx + direction;
    if (idx < 0 || targetIdx < 0 || targetIdx >= visualStages.length) {
      return;
    }
    const moved = visualStages[idx]!;
    const target = visualStages[targetIdx]!;
    const nextOrder = [...visualStages];
    nextOrder[idx] = target;
    nextOrder[targetIdx] = moved;

    const dayIndices =
      moved.day_index !== target.day_index
        ? { [moved.id]: target.day_index, [target.id]: moved.day_index }
        : undefined;

    setBusyAction(`reorder-${stageId}`);
    setActionError(null);
    try {
      await stagesApi.reorder(projectId, {
        stage_ids: nextOrder.map((s) => s.id),
        ...(dayIndices ? { day_indices: dayIndices } : {}),
      });
      await onStagesMutated();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Failed to reorder stages");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDropReorder(targetStageId: string) {
    if (!dragStageId || dragStageId === targetStageId) {
      setDragStageId(null);
      return;
    }
    const fromIdx = visualStages.findIndex((s) => s.id === dragStageId);
    const toIdx = visualStages.findIndex((s) => s.id === targetStageId);
    setDragStageId(null);
    if (fromIdx < 0 || toIdx < 0) {
      return;
    }
    const moved = visualStages[fromIdx]!;
    const target = visualStages[toIdx]!;
    const reordered = [...visualStages];
    const [removed] = reordered.splice(fromIdx, 1);
    reordered.splice(toIdx, 0, removed!);

    const targetDay = target.day_index;
    const dayIndices =
      moved.day_index !== targetDay ? { [moved.id]: targetDay } : undefined;
    const nextOrder = reordered
      .map((s, pos) => ({
        stage: s,
        pos,
        effectiveDay: s.id === moved.id ? targetDay : s.day_index,
      }))
      .sort((a, b) => a.effectiveDay - b.effectiveDay || a.pos - b.pos)
      .map((entry) => entry.stage);

    setBusyAction(`reorder-${targetStageId}`);
    setActionError(null);
    try {
      await stagesApi.reorder(projectId, {
        stage_ids: nextOrder.map((s) => s.id),
        ...(dayIndices ? { day_indices: dayIndices } : {}),
      });
      await onStagesMutated();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Failed to reorder stages");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSaveStageDetails(
    stage: StageOut,
    draft: {
      name: string;
      day: string;
      trimStart: string;
      trimEnd: string;
      activityType: ActivityType;
    },
  ) {
    const trimmedName = draft.name.trim();
    if (!trimmedName) {
      setActionError("Stage name cannot be empty.");
      return;
    }
    const parsedDay = Number.parseInt(draft.day, 10);
    if (!Number.isFinite(parsedDay) || parsedDay < 1 || parsedDay > 365) {
      setActionError("Day index must be between 1 and 365.");
      return;
    }

    const trimStartVal = draft.trimStart.trim() === "" ? null : Number(draft.trimStart);
    const trimEndVal = draft.trimEnd.trim() === "" ? null : Number(draft.trimEnd);

    if (trimStartVal !== null && (!Number.isFinite(trimStartVal) || trimStartVal < 0)) {
      setActionError("Trim start must be >= 0 seconds.");
      return;
    }
    if (trimEndVal !== null && (!Number.isFinite(trimEndVal) || trimEndVal <= 0)) {
      setActionError("Trim end must be > 0 seconds.");
      return;
    }
    if (trimStartVal !== null && trimEndVal !== null && trimEndVal <= trimStartVal) {
      setActionError("Trim end must be greater than trim start.");
      return;
    }

    setBusyAction(`update-${stage.id}`);
    setActionError(null);
    try {
      await stagesApi.update(projectId, stage.id, {
        name: trimmedName,
        day_index: parsedDay,
        trim_start_s: trimStartVal,
        trim_end_s: trimEndVal,
        activity_type: draft.activityType,
      });
      if (parsedDay !== stage.day_index && visualStages.length > 1) {
        const currentByOrder = [...stages].sort((a, b) => a.stage_order - b.stage_order);
        const aligned = [...stages]
          .map((s) => (s.id === stage.id ? { ...s, day_index: parsedDay } : s))
          .sort((a, b) => a.day_index - b.day_index || a.stage_order - b.stage_order);
        const orderChanged = aligned.some((s, idx) => s.id !== currentByOrder[idx]?.id);
        if (orderChanged) {
          await stagesApi.reorder(projectId, {
            stage_ids: aligned.map((s) => s.id),
          });
        }
      }
      await onStagesMutated();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Failed to update stage");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleClearTrim(stage: StageOut) {
    setBusyAction(`update-${stage.id}`);
    setActionError(null);
    try {
      await stagesApi.update(projectId, stage.id, {
        trim_start_s: null,
        trim_end_s: null,
      });
      await onStagesMutated();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Failed to reset trim");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDeleteStage(stageId: string) {
    setBusyAction(`delete-${stageId}`);
    setActionError(null);
    try {
      await stagesApi.delete(projectId, stageId);
      await onStagesMutated();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Failed to delete stage");
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <aside
      aria-label="Stages and telemetry sources"
      className="flex min-h-0 flex-col border-b border-border bg-surface lg:col-span-3 lg:border-r lg:border-b-0"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2 text-xs font-semibold text-ink">
          <Layers className="h-4 w-4 text-sky-accent" aria-hidden="true" />
          <span>Trip stages</span>
          <span className="tabular-telemetry rounded bg-elevated px-1.5 py-0.5 text-[10px] text-ink-muted">
            {stages.length}
          </span>
        </div>
        <Link
          to="/"
          className="inline-flex items-center gap-1 text-xs text-ink-muted hover:text-ink"
        >
          <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
          <span>All trips</span>
        </Link>
      </div>

      <div
        role="tablist"
        aria-label="Sidebar sections"
        className="grid grid-cols-3 border-b border-border bg-canvas/50 p-1"
      >
        {(
          [
            { id: "stages", label: `Stages (${stages.length})` },
            { id: "edit", label: "Edit" },
            { id: "photos", label: `Photos (${photos.length})` },
          ] as const
        ).map((tab) => {
          const active = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setActiveTab(tab.id)}
              className={`rounded py-1.5 text-xs font-medium transition-colors ${
                active ? "bg-elevated text-sky-accent" : "text-ink-muted hover:text-ink"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {activeTab === "stages" ? (
          <>
            {/* Track Dropzone (.gpx,.fit,.tcx) + Strava Import */}
            <div className="space-y-2">
              <div
                role="button"
                tabIndex={0}
                aria-label="Drop GPX files or click to browse"
                onClick={() => fileInputRef.current?.click()}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    fileInputRef.current?.click();
                  }
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  setIsDragOver(true);
                }}
                onDragLeave={() => setIsDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setIsDragOver(false);
                  if (e.dataTransfer.files.length > 0) {
                    void handleTrackFiles(e.dataTransfer.files);
                  }
                }}
                className={`cursor-pointer rounded-md border border-dashed p-3 text-center transition-colors ${
                  isDragOver
                    ? "border-sky-accent bg-sky-accent/10"
                    : "border-border bg-canvas/60 hover:border-sky-accent/50"
                }`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".gpx,.fit,.tcx,application/gpx+xml,application/octet-stream,application/vnd.garmin.tcx+xml"
                  multiple
                  className="hidden"
                  onChange={(e) => {
                    if (e.currentTarget.files && e.currentTarget.files.length > 0) {
                      void handleTrackFiles(e.currentTarget.files);
                      e.currentTarget.value = "";
                    }
                  }}
                />
                <Upload className="mx-auto h-4 w-4 text-sky-accent" aria-hidden="true" />
                <p className="mt-1 text-xs font-medium text-ink">
                  Drop GPX, FIT, or TCX tracks
                </p>
                <p className="mt-0.5 text-[11px] text-ink-muted">
                  Multi-file (.gpx, .fit, .tcx) • 25 MiB max
                </p>
              </div>

              <button
                type="button"
                onClick={onOpenStravaModal}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-border bg-elevated px-3 py-1.5 text-xs font-medium text-ink hover:border-amber-accent/60 hover:text-amber-accent transition-colors"
              >
                <Zap className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
                <span>{stravaConnected ? "Import from Strava" : "Import Strava (connect on Dashboard)"}</span>
              </button>

              {visualStages.length > 0 ? (
                <div className="flex items-center gap-1.5 pt-0.5">
                  <select
                    aria-label="Trip color palette preset"
                    value={palettePreset}
                    onChange={(e) => setPalettePreset(e.currentTarget.value as PalettePreset)}
                    className="min-w-0 flex-1 rounded-md border border-border bg-elevated px-2 py-1.5 text-[11px] text-ink focus:border-sky-accent focus:outline-none"
                  >
                    <option value="alpine-dawn">Alpine Dawn</option>
                    <option value="topo-emerald">Topo Emerald</option>
                    <option value="stage-by-stage">Stage-by-Stage</option>
                  </select>
                  <button
                    type="button"
                    disabled={busyAction !== null}
                    onClick={() => void handleAutoOrganize()}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-teal-accent/50 bg-teal-accent/10 px-2.5 py-1.5 text-[11px] font-medium text-teal-accent hover:bg-teal-accent/20 disabled:opacity-50"
                  >
                    {busyAction === "auto-organize" ? (
                      <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                    ) : (
                      <Sparkles className="h-3 w-3" aria-hidden="true" />
                    )}
                    <span>Auto-Organize Trip</span>
                  </button>
                </div>
              ) : null}
            </div>

            {/* Compact track upload status */}
            {uploads.length > 0 ? (() => {
              const uploadingCount = uploads.filter((u) => u.status === "uploading").length;
              const succeededCount = uploads.filter((u) => u.status === "succeeded").length;
              const failedCount = uploads.filter((u) => u.status === "failed").length;
              const allDone = uploadingCount === 0;

              if (uploads.length === 1) {
                const item = uploads[0]!;
                return (
                  <div
                    aria-label="GPX upload status"
                    className="flex items-center justify-between gap-2 rounded border border-border bg-canvas/70 px-2.5 py-1.5 text-[11px]"
                  >
                    <span className="truncate text-ink">{item.filename}</span>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {item.status === "uploading" ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-accent" aria-hidden="true" />
                      ) : item.status === "succeeded" ? (
                        <CheckCircle2 className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
                      ) : (
                        <span className="text-coral-accent">{item.error ?? "Error"}</span>
                      )}
                      {allDone ? (
                        <button
                          type="button"
                          aria-label="Dismiss upload status"
                          onClick={() => {
                            setUploads([]);
                            setUploadsExpanded(false);
                          }}
                          className="rounded p-0.5 text-ink-muted hover:text-ink"
                        >
                          <X className="h-3 w-3" aria-hidden="true" />
                        </button>
                      ) : null}
                    </div>
                  </div>
                );
              }

              return (
                <div
                  aria-label="GPX upload status"
                  data-testid="gpx-upload-summary"
                  className="rounded-md border border-border bg-canvas/70 text-[11px]"
                >
                  <div className="flex items-center justify-between gap-2 px-2.5 py-1.5">
                    <button
                      type="button"
                      onClick={() => setUploadsExpanded((v) => !v)}
                      className="inline-flex min-w-0 items-center gap-1.5 text-left text-ink hover:text-sky-accent"
                    >
                      {uploadingCount > 0 ? (
                        <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-accent" aria-hidden="true" />
                      ) : failedCount > 0 ? (
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-accent" aria-hidden="true" />
                      ) : (
                        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-teal-accent" aria-hidden="true" />
                      )}
                      <span className="truncate font-medium">
                        {uploadingCount > 0
                          ? `Uploading ${succeededCount + failedCount}/${uploads.length} tracks…`
                          : failedCount > 0
                            ? `Uploaded ${succeededCount}/${uploads.length} tracks (${failedCount} failed)`
                            : `Uploaded ${succeededCount} track files`}
                      </span>
                      {uploadsExpanded ? (
                        <ChevronDown className="h-3 w-3 shrink-0 text-ink-muted" aria-hidden="true" />
                      ) : (
                        <ChevronRight className="h-3 w-3 shrink-0 text-ink-muted" aria-hidden="true" />
                      )}
                    </button>
                    {allDone ? (
                      <button
                        type="button"
                        aria-label="Dismiss upload status"
                        onClick={() => {
                          setUploads([]);
                          setUploadsExpanded(false);
                        }}
                        className="shrink-0 rounded p-0.5 text-ink-muted hover:text-ink"
                      >
                        <X className="h-3 w-3" aria-hidden="true" />
                      </button>
                    ) : null}
                  </div>

                  {uploadsExpanded ? (
                    <ul className="space-y-1 border-t border-border/70 px-2.5 py-1.5">
                      {uploads.map((item) => (
                        <li
                          key={item.key}
                          className="flex items-center justify-between gap-2 text-[10px]"
                        >
                          <span className="truncate text-ink-muted">{item.filename}</span>
                          {item.status === "uploading" ? (
                            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-sky-accent" aria-hidden="true" />
                          ) : item.status === "succeeded" ? (
                            <CheckCircle2 className="h-3 w-3 shrink-0 text-teal-accent" aria-hidden="true" />
                          ) : (
                            <span className="shrink-0 text-coral-accent">{item.error ?? "Error"}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              );
            })() : null}

            {actionError ? (
              <div
                role="alert"
                className="rounded-md border border-coral-accent/40 bg-coral-accent/10 px-2.5 py-2 text-[11px] text-ink"
              >
                {actionError}
              </div>
            ) : null}

            {/* Collapsible Multi-day grouped stage list */}
            {visualStages.length === 0 ? (
              <div className="rounded-md border border-border bg-canvas/40 p-4 text-center text-xs text-ink-muted">
                No stages yet. Drop a GPX/FIT/TCX file or import a Strava activity to build your trip.
              </div>
            ) : (
              <div className="space-y-3" aria-label="Ordered trip stages">
                {groupedByDay.map(([dayNum, dayStages]) => {
                  const isCollapsed = Boolean(collapsedDays[dayNum]);
                  const dayDistM = dayStages.reduce((acc, s) => acc + s.distance_m, 0);
                  return (
                    <div key={dayNum} className="space-y-1.5">
                      <button
                        type="button"
                        aria-expanded={!isCollapsed}
                        onClick={() =>
                          setCollapsedDays((prev) => ({ ...prev, [dayNum]: !prev[dayNum] }))
                        }
                        className="flex w-full items-center justify-between rounded px-1 py-0.5 text-[11px] font-medium text-ink-muted hover:bg-elevated/60 hover:text-ink"
                      >
                        <span className="inline-flex items-center gap-1">
                          {isCollapsed ? (
                            <ChevronRight className="h-3 w-3" aria-hidden="true" />
                          ) : (
                            <ChevronDown className="h-3 w-3" aria-hidden="true" />
                          )}
                          <span>Day {dayNum}</span>
                        </span>
                        <span className="tabular-telemetry">
                          {dayStages.length} stage{dayStages.length > 1 ? "s" : ""} •{" "}
                          {formatDistance(dayDistM, spec.units)}
                        </span>
                      </button>

                      {!isCollapsed ? (
                        <ul className="space-y-1.5">
                          {dayStages.map((stage) => {
                            const isSelected = selectedStageId === stage.id;
                            const colorHex = spec.stage_colors[stage.id] ?? spec.track_color;
                            const transitionIcon = spec.stage_transitions[stage.id] ?? "none";
                            const visualIdx = visualIndexById.get(stage.id) ?? 0;

                            return (
                              <li
                                key={stage.id}
                                draggable
                                onDragStart={() => setDragStageId(stage.id)}
                                onDragOver={(e) => e.preventDefault()}
                                onDrop={() => void handleDropReorder(stage.id)}
                                className={`rounded-md border transition-colors ${
                                  isSelected
                                    ? "border-sky-accent bg-elevated"
                                    : "border-border bg-canvas/55 hover:border-border/90 hover:bg-elevated/60"
                                }`}
                              >
                                <div className="flex items-start gap-1.5 p-2.5">
                                  <GripVertical
                                    className="mt-0.5 h-3.5 w-3.5 shrink-0 cursor-grab text-ink-muted/60"
                                    aria-hidden="true"
                                  />
                                  <button
                                    type="button"
                                    onClick={() => onSelectStage(stage.id)}
                                    className="min-w-0 flex-1 text-left"
                                  >
                                    <div className="flex items-center justify-between gap-1.5">
                                      <span className="inline-flex min-w-0 items-center gap-1.5 text-xs font-medium text-ink">
                                        <span
                                          className="h-2.5 w-2.5 shrink-0 rounded-full"
                                          style={{ backgroundColor: colorHex }}
                                          aria-hidden="true"
                                        />
                                        <ActivityIcon
                                          type={stage.activity_type}
                                          size={14}
                                          accentColor={colorHex}
                                          duotoneOpacity={0.26}
                                          className="shrink-0 text-ink"
                                          title={getActivityIconMeta(stage.activity_type).label}
                                        />
                                        <span className="truncate">{stage.name}</span>
                                      </span>
                                      <div className="flex shrink-0 items-center gap-1">
                                        {stage.warnings.length > 0 ? (
                                          <span
                                            title={stage.warnings.map((w) => w.message).join("; ")}
                                            className="text-amber-accent"
                                          >
                                            <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                                          </span>
                                        ) : null}
                                        <span className="rounded border border-border bg-surface px-1.5 py-0.5 text-[10px] text-ink-muted">
                                          {stage.source_format ?? stage.source_type}
                                        </span>
                                      </div>
                                    </div>

                                    <div className="tabular-telemetry mt-1.5 flex items-center gap-2.5 text-[11px] text-ink-muted">
                                      <span>{formatDistance(stage.distance_m, spec.units)}</span>
                                      <span>+{formatElevation(stage.elevation_gain_m, spec.units)}</span>
                                      {transitionIcon !== "none" ? (
                                        <span className="inline-flex items-center gap-1 text-amber-accent">
                                          <TransitionBadgeIcon icon={transitionIcon} />
                                          <span>{transitionIcon}</span>
                                        </span>
                                      ) : null}
                                    </div>
                                  </button>

                                  <div className="flex shrink-0 flex-col gap-0.5">
                                    <button
                                      type="button"
                                      aria-label={`Move ${stage.name} up`}
                                      disabled={visualIdx === 0 || busyAction !== null}
                                      onClick={() => void moveStage(stage.id, -1)}
                                      className="rounded p-0.5 text-ink-muted hover:bg-surface hover:text-ink disabled:opacity-30"
                                    >
                                      <ArrowUp className="h-3 w-3" aria-hidden="true" />
                                    </button>
                                    <button
                                      type="button"
                                      aria-label={`Move ${stage.name} down`}
                                      disabled={visualIdx === visualStages.length - 1 || busyAction !== null}
                                      onClick={() => void moveStage(stage.id, 1)}
                                      className="rounded p-0.5 text-ink-muted hover:bg-surface hover:text-ink disabled:opacity-30"
                                    >
                                      <ArrowDown className="h-3 w-3" aria-hidden="true" />
                                    </button>
                                  </div>
                                </div>
                              </li>
                            );
                          })}
                        </ul>
                      ) : null}
                    </div>
                  );
                })}

                {aggregatedWarnings.length > 0 ? (
                  <div
                    data-testid="stage-warnings-summary"
                    className="rounded-md border border-amber-accent/35 bg-amber-accent/10 px-2.5 py-1.5 text-[10px] text-amber-accent"
                  >
                    <button
                      type="button"
                      onClick={() => setWarningsExpanded((v) => !v)}
                      className="flex w-full items-center justify-between gap-1.5 text-left"
                    >
                      <div className="flex min-w-0 items-center gap-1.5">
                        <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
                        <span className="truncate font-medium">
                          {aggregatedWarnings.length === 1
                            ? aggregatedWarnings[0]!.message
                            : `${aggregatedWarnings.length} ingest notices`}
                        </span>
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        {aggregatedWarnings.length === 1 &&
                        aggregatedWarnings[0]!.stageNames.length > 1 ? (
                          <span className="tabular-telemetry rounded bg-amber-accent/15 px-1 py-0.5 text-[9px]">
                            {aggregatedWarnings[0]!.stageNames.length} stages
                          </span>
                        ) : null}
                        {warningsExpanded ? (
                          <ChevronDown className="h-3 w-3" aria-hidden="true" />
                        ) : (
                          <ChevronRight className="h-3 w-3" aria-hidden="true" />
                        )}
                      </div>
                    </button>

                    {warningsExpanded ? (
                      <ul className="mt-1.5 space-y-1 border-t border-amber-accent/25 pt-1.5">
                        {aggregatedWarnings.map((w) => (
                          <li key={`${w.code}-${w.message}`} className="leading-snug">
                            <span>{w.message}</span>
                            <span className="ml-1 opacity-80">
                              ({w.stageNames.join(", ")})
                            </span>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )}
          </>
        ) : null}

        {activeTab === "edit" ? (
          <div className="space-y-3">
            {actionError ? (
              <div
                role="alert"
                className="rounded-md border border-coral-accent/40 bg-coral-accent/10 px-2.5 py-2 text-[11px] text-ink"
              >
                {actionError}
              </div>
            ) : null}
            {selectedStage ? (
              <StageEditorForm
                key={`${selectedStage.id}-${selectedStage.name}-${selectedStage.day_index}-${selectedStage.activity_type ?? "cycling"}-${selectedStage.trim_start_s ?? "n"}-${selectedStage.trim_end_s ?? "n"}`}
                stage={selectedStage}
                spec={spec}
                busyAction={busyAction}
                onSave={(draft) => void handleSaveStageDetails(selectedStage, draft)}
                onClearTrim={() => void handleClearTrim(selectedStage)}
                onDelete={() => void handleDeleteStage(selectedStage.id)}
                onStageColorChange={onStageColorChange}
                onStageTransitionChange={onStageTransitionChange}
              />
            ) : (
              <div className="rounded-md border border-border bg-canvas/40 p-4 text-center text-xs text-ink-muted">
                Select a stage from the Stages tab to edit its properties, color, or trim bounds.
              </div>
            )}
          </div>
        ) : null}

        {activeTab === "photos" ? (
          <PhotoStopsSection
            projectId={projectId}
            photos={photos}
            matchedPhotos={matchedPhotos}
            googlePhotosConfigured={googlePhotosConfigured}
            pinModePhotoId={pinModePhotoId}
            onTogglePinMode={onTogglePinMode}
            onPhotosMutated={onPhotosMutated}
          />
        ) : null}
      </div>
    </aside>
  );
}

interface StageEditorFormProps {
  stage: StageOut;
  spec: RenderSpecV2;
  busyAction: string | null;
  onSave: (draft: {
    name: string;
    day: string;
    trimStart: string;
    trimEnd: string;
    activityType: ActivityType;
  }) => void;
  onClearTrim: () => void;
  onDelete: () => void;
  onStageColorChange: (stageId: string, colorHex: string) => void;
  onStageTransitionChange: (stageId: string, transition: TransitionIcon) => void;
}

function StageEditorForm({
  stage,
  spec,
  busyAction,
  onSave,
  onClearTrim,
  onDelete,
  onStageColorChange,
  onStageTransitionChange,
}: StageEditorFormProps) {
  const [draftName, setDraftName] = useState(stage.name);
  const [draftDay, setDraftDay] = useState(String(stage.day_index));
  const [draftActivityType, setDraftActivityType] = useState<ActivityType>(() =>
    normalizeActivityType(stage.activity_type),
  );
  const [draftTrimStart, setDraftTrimStart] = useState(
    stage.trim_start_s !== null ? String(stage.trim_start_s) : "",
  );
  const [draftTrimEnd, setDraftTrimEnd] = useState(
    stage.trim_end_s !== null ? String(stage.trim_end_s) : "",
  );

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSave({
          name: draftName,
          day: draftDay,
          trimStart: draftTrimStart,
          trimEnd: draftTrimEnd,
          activityType: draftActivityType,
        });
      }}
      className="space-y-3 rounded-lg border border-border bg-canvas/80 p-3 text-xs"
    >
      <div className="flex items-center justify-between">
        <span className="font-semibold text-ink">Stage properties</span>
        <button
          type="button"
          disabled={busyAction !== null}
          onClick={onDelete}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-coral-accent hover:bg-coral-accent/15"
        >
          <Trash2 className="h-3 w-3" aria-hidden="true" />
          <span>Delete</span>
        </button>
      </div>

      <label className="block space-y-1">
        <span className="text-[11px] text-ink-muted">Stage name</span>
        <input
          type="text"
          value={draftName}
          onChange={(e) => setDraftName(e.currentTarget.value)}
          className="w-full rounded border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
        />
      </label>

      <div className="grid grid-cols-2 gap-2">
        <label className="block space-y-1">
          <span className="text-[11px] text-ink-muted">Day index</span>
          <input
            type="number"
            min={1}
            max={365}
            value={draftDay}
            onChange={(e) => setDraftDay(e.currentTarget.value)}
            className="w-full rounded border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
          />
        </label>

        <label className="block space-y-1">
          <span className="text-[11px] text-ink-muted">End stop icon</span>
          <select
            value={spec.stage_transitions[stage.id] ?? "none"}
            onChange={(e) =>
              onStageTransitionChange(stage.id, e.currentTarget.value as TransitionIcon)
            }
            className="w-full rounded border border-border bg-elevated px-2 py-1.5 text-xs text-ink focus:border-sky-accent focus:outline-none"
          >
            {TRANSITION_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-[11px]">
          <span className="text-ink-muted">Activity type</span>
          <span className="font-medium text-ink">
            {getActivityIconMeta(draftActivityType).label}
          </span>
        </div>
        <div
          role="group"
          aria-label="Stage activity type"
          className="grid grid-cols-7 gap-1"
        >
          {ACTIVITY_ICON_LIST.map((item) => {
            const active = draftActivityType === item.type;
            return (
              <button
                key={item.type}
                type="button"
                aria-label={item.label}
                aria-pressed={active}
                title={item.label}
                onClick={() => setDraftActivityType(item.type)}
                className={`flex flex-col items-center justify-center rounded border p-1.5 transition-colors ${
                  active
                    ? "border-sky-accent bg-sky-accent/15 text-ink"
                    : "border-border bg-elevated/60 text-ink-muted hover:border-border/90 hover:text-ink"
                }`}
              >
                <ActivityIcon
                  type={item.type}
                  size={15}
                  accentColor={active ? item.accentColor : "currentColor"}
                  duotoneOpacity={active ? 0.28 : 0.15}
                />
              </button>
            );
          })}
        </div>
      </div>

      <div className="space-y-1">
        <span className="text-[11px] text-ink-muted">Stage route color</span>
        <div className="flex items-center gap-1.5">
          {STAGE_COLOR_SWATCHES.map((swatch) => {
            const current = spec.stage_colors[stage.id] ?? spec.track_color;
            return (
              <button
                key={swatch}
                type="button"
                aria-label={`Color ${swatch}`}
                onClick={() => onStageColorChange(stage.id, swatch)}
                className={`h-5 w-5 rounded-full border ${
                  current.toLowerCase() === swatch.toLowerCase()
                    ? "border-white ring-1 ring-sky-accent"
                    : "border-border"
                }`}
                style={{ backgroundColor: swatch }}
              />
            );
          })}
          <input
            type="color"
            aria-label="Custom stage color"
            value={spec.stage_colors[stage.id] ?? spec.track_color}
            onChange={(e) => onStageColorChange(stage.id, e.currentTarget.value)}
            className="h-6 w-7 cursor-pointer rounded border border-border bg-transparent p-0"
          />
        </div>
      </div>

      <div className="space-y-1.5 border-t border-border/70 pt-2">
        <div className="flex items-center justify-between text-[11px] text-ink-muted">
          <span className="inline-flex items-center gap-1">
            <Scissors className="h-3 w-3" aria-hidden="true" />
            <span>Non-destructive trim (s)</span>
          </span>
          {stage.trim_start_s !== null || stage.trim_end_s !== null ? (
            <button
              type="button"
              onClick={onClearTrim}
              className="text-[10px] text-sky-accent hover:underline"
            >
              Clear trim
            </button>
          ) : null}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <input
            type="number"
            min={0}
            step="1"
            placeholder="Start (s)"
            aria-label="Trim start seconds"
            value={draftTrimStart}
            onChange={(e) => setDraftTrimStart(e.currentTarget.value)}
            className="w-full rounded border border-border bg-elevated px-2 py-1 text-xs text-ink placeholder:text-ink-muted/50 focus:border-sky-accent focus:outline-none"
          />
          <input
            type="number"
            min={1}
            step="1"
            placeholder="End (s)"
            aria-label="Trim end seconds"
            value={draftTrimEnd}
            onChange={(e) => setDraftTrimEnd(e.currentTarget.value)}
            className="w-full rounded border border-border bg-elevated px-2 py-1 text-xs text-ink placeholder:text-ink-muted/50 focus:border-sky-accent focus:outline-none"
          />
        </div>
      </div>

      <button
        type="submit"
        disabled={busyAction !== null}
        className="w-full rounded-md bg-elevated border border-border py-1.5 text-xs font-medium text-ink hover:border-sky-accent/60 hover:text-sky-accent transition-colors disabled:opacity-50"
      >
        {busyAction === `update-${stage.id}` ? "Applying..." : "Apply stage changes"}
      </button>
    </form>
  );
}
