import { Link } from "@tanstack/react-router";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BedDouble,
  Calendar,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Coffee,
  Focus,
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
  SettlementLabelMode,
  StageCameraOverride,
  StageFocusMode,
  StageOut,
  StageSplitRequest,
  TransitionIcon,
} from "../../lib/api/types";
import { computeStageProgressSpans } from "../../lib/geo/camera";
import {
  formatDistance,
  formatElevation,
  formatStageCalendarDate,
  formatStageTimeWindow,
} from "../../lib/geo/format";
import {
  computePhotoStopTotalDurationS,
  evaluateTimelineSchedule,
  type MatchedPhoto,
} from "../../lib/geo/photos";
import type { TimelineStage } from "../../lib/geo/timeline";
import { useEditorStore, type SidebarTab } from "../../lib/state/editor-store";
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

const STAGE_FOCUS_PRESETS: readonly {
  mode: StageFocusMode;
  label: string;
  hint: string;
}[] = [
  { mode: "auto", label: "Auto", hint: "1×" },
  { mode: "close", label: "Close", hint: "Zoom 13.8 · 0.7×" },
  { mode: "street", label: "Street Focus", hint: "Zoom 15.8 · 0.2×" },
  { mode: "custom", label: "Custom", hint: "Manual zoom & pace" },
];

const QUICK_PACE_OPTIONS = [0.05, 0.1, 0.25, 0.5, 1, 2] as const;

function buildDefaultSplitName(baseName: string, suffix: string): string {
  const maxBaseLen = Math.max(1, 200 - suffix.length);
  return `${baseName.slice(0, maxBaseLen)}${suffix}`;
}

function formatSpeedMultiplier(speed: number): string {
  if (!Number.isFinite(speed)) {
    return "1×";
  }
  return `${Number(speed.toFixed(2))}×`;
}

function formatStageFocusBadge(override: StageCameraOverride): string {
  const speedLabel = formatSpeedMultiplier(override.speed_multiplier);
  if (override.focus_mode === "street") {
    return `Street · ${speedLabel}`;
  }
  if (override.focus_mode === "close") {
    return `Close · ${speedLabel}`;
  }
  if (override.target_zoom !== null && Number.isFinite(override.target_zoom)) {
    return `Zoom ${override.target_zoom.toFixed(1)} · ${speedLabel}`;
  }
  return `Pace ${speedLabel}`;
}

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
  onStageCameraOverrideChange?: (stageId: string, override: StageCameraOverride | null) => void;
  onStageSettlementLabelModeChange?: (stageId: string, mode: SettlementLabelMode | null) => void;
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
  onStageCameraOverrideChange,
  onStageSettlementLabelModeChange,
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
  const [confirmDeleteStageId, setConfirmDeleteStageId] = useState<string | null>(null);

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
      const resolvedTimeZone =
        typeof Intl !== "undefined"
          ? Intl.DateTimeFormat().resolvedOptions().timeZone || undefined
          : undefined;
      await stagesApi.autoOrganize(projectId, {
        palette_preset: palettePreset,
        assign_overnight_icons: true,
        ...(resolvedTimeZone ? { timezone: resolvedTimeZone } : {}),
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
    setConfirmDeleteStageId(null);
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

  async function handleSplitStage(
    stage: StageOut,
    payload: StageSplitRequest,
  ): Promise<string | null> {
    setBusyAction(`split-${stage.id}`);
    setActionError(null);
    try {
      const res = await stagesApi.split(projectId, stage.id, payload);
      onSelectStage(res.part2.id);
      await onStagesMutated();
      onSelectStage(res.part2.id);
      return null;
    } catch (err) {
      return err instanceof ApiError ? err.message : "Failed to split stage";
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
        className="grid grid-cols-2 border-b border-border bg-canvas/50 p-1"
      >
        {(
          [
            { id: "stages", label: `Stages (${stages.length})` },
            { id: "photos", label: `Photos (${photos.length})` },
          ] as const
        ).map((tab) => {
          const effectiveTab = activeTab === "photos" ? "photos" : "stages";
          const active = effectiveTab === tab.id;
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

      {activeTab !== "photos" ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <div
            data-testid="stages-list-pane"
            className={`min-h-0 flex-1 space-y-3 overflow-y-auto p-3 ${
              selectedStage ? "basis-1/2" : ""
            }`}
          >
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
                onDragEnter={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setIsDragOver(true);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setIsDragOver(true);
                }}
                onDragLeave={() => setIsDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setIsDragOver(false);
                  if (e.dataTransfer.files.length > 0) {
                    void handleTrackFiles(Array.from(e.dataTransfer.files));
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
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => {
                    if (e.currentTarget.files && e.currentTarget.files.length > 0) {
                      const selectedFiles = Array.from(e.currentTarget.files);
                      e.currentTarget.value = "";
                      void handleTrackFiles(selectedFiles);
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

            {visualStages.length === 0 ? (
              <div className="rounded-md border border-border bg-canvas/40 p-4 text-center text-xs text-ink-muted">
                No stages yet. Drop a GPX/FIT/TCX file or import a Strava activity to build your trip.
              </div>
            ) : (
              <div className="space-y-3" aria-label="Ordered trip stages">
                {groupedByDay.map(([dayNum, dayStages]) => {
                  const isCollapsed = Boolean(collapsedDays[dayNum]);
                  const dayDistM = dayStages.reduce((acc, s) => acc + s.distance_m, 0);
                  const firstStartedAt =
                    dayStages.find((s) => Boolean(s.started_at))?.started_at ?? null;
                  const dayDateLabel = formatStageCalendarDate(firstStartedAt);
                  return (
                    <div key={dayNum} className="space-y-1.5">
                      <button
                        type="button"
                        aria-expanded={!isCollapsed}
                        onClick={() =>
                          setCollapsedDays((prev) => ({ ...prev, [dayNum]: !prev[dayNum] }))
                        }
                        className="flex w-full items-center justify-between gap-2 rounded px-1 py-0.5 text-[11px] font-medium text-ink-muted hover:bg-elevated/60 hover:text-ink"
                      >
                        <span className="inline-flex min-w-0 items-center gap-1">
                          {isCollapsed ? (
                            <ChevronRight className="h-3 w-3 shrink-0" aria-hidden="true" />
                          ) : (
                            <ChevronDown className="h-3 w-3 shrink-0" aria-hidden="true" />
                          )}
                          <span className="shrink-0">Day {dayNum}</span>
                          {dayDateLabel ? (
                            <span
                              data-testid={`day-${dayNum}-date`}
                              className="tabular-telemetry truncate font-normal text-ink-muted/80"
                            >
                              · {dayDateLabel}
                            </span>
                          ) : null}
                        </span>
                        <span className="tabular-telemetry shrink-0">
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
                            const cameraOverride = spec.stage_camera_overrides?.[stage.id];
                            const hasFocusBadge =
                              cameraOverride !== undefined && cameraOverride.focus_mode !== "auto";
                            const visualIdx = visualIndexById.get(stage.id) ?? 0;
                            const stageDurationS =
                              stage.elapsed_seconds > 0
                                ? stage.elapsed_seconds
                                : stage.moving_seconds;
                            const timeWindow = formatStageTimeWindow(
                              stage.started_at,
                              stage.ended_at,
                              stageDurationS,
                            );

                            return (
                              <li
                                key={stage.id}
                                draggable
                                onDragStart={() => setDragStageId(stage.id)}
                                onDragOver={(e) => e.preventDefault()}
                                onDrop={(e) => {
                                  e.preventDefault();
                                  void handleDropReorder(stage.id);
                                }}
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

                                    {timeWindow ? (
                                      <div
                                        data-testid="stage-date-range"
                                        className="tabular-telemetry mt-1 flex items-center gap-1 text-[11px] text-ink-muted"
                                      >
                                        <Calendar
                                          className="h-3 w-3 shrink-0 text-ink-muted/75"
                                          aria-hidden="true"
                                        />
                                        <span className="truncate">{timeWindow}</span>
                                      </div>
                                    ) : null}

                                    <div className="tabular-telemetry mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11px] text-ink-muted">
                                      <span>{formatDistance(stage.distance_m, spec.units)}</span>
                                      <span>+{formatElevation(stage.elevation_gain_m, spec.units)}</span>
                                      {transitionIcon !== "none" ? (
                                        <span className="inline-flex items-center gap-1 text-amber-accent">
                                          <TransitionBadgeIcon icon={transitionIcon} />
                                          <span>{transitionIcon}</span>
                                        </span>
                                      ) : null}
                                      {hasFocusBadge && cameraOverride ? (
                                        <span
                                          data-testid={`stage-focus-badge-${stage.id}`}
                                          className="inline-flex items-center gap-1 rounded border border-sky-accent/40 bg-sky-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-sky-accent"
                                        >
                                          <Focus className="h-2.5 w-2.5" aria-hidden="true" />
                                          <span>{formatStageFocusBadge(cameraOverride)}</span>
                                        </span>
                                      ) : null}
                                    </div>
                                  </button>

                                  <div className="flex shrink-0 flex-col gap-0.5">
                                    <button
                                      type="button"
                                      aria-label={`Move ${stage.name} up`}
                                      disabled={visualIdx === 0 || busyAction !== null}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        void moveStage(stage.id, -1);
                                      }}
                                      className="rounded p-0.5 text-ink-muted hover:bg-surface hover:text-ink disabled:opacity-30"
                                    >
                                      <ArrowUp className="h-3 w-3" aria-hidden="true" />
                                    </button>
                                    <button
                                      type="button"
                                      aria-label={`Move ${stage.name} down`}
                                      disabled={visualIdx === visualStages.length - 1 || busyAction !== null}
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        void moveStage(stage.id, 1);
                                      }}
                                      className="rounded p-0.5 text-ink-muted hover:bg-surface hover:text-ink disabled:opacity-30"
                                    >
                                      <ArrowDown className="h-3 w-3" aria-hidden="true" />
                                    </button>
                                    {confirmDeleteStageId === stage.id ? (
                                      <button
                                        type="button"
                                        aria-label={`Confirm delete ${stage.name}`}
                                        title={`Click again to confirm deleting ${stage.name}`}
                                        data-testid={`quick-delete-stage-${stage.id}`}
                                        disabled={busyAction !== null}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          void handleDeleteStage(stage.id);
                                        }}
                                        onBlur={() =>
                                          setConfirmDeleteStageId((cur) =>
                                            cur === stage.id ? null : cur,
                                          )
                                        }
                                        onKeyDown={(e) => {
                                          if (e.key === "Escape") {
                                            e.stopPropagation();
                                            setConfirmDeleteStageId(null);
                                          }
                                        }}
                                        className="rounded bg-coral-accent p-0.5 text-canvas hover:bg-coral-accent/90 focus:outline-none focus:ring-1 focus:ring-coral-accent disabled:opacity-30"
                                      >
                                        <Trash2 className="h-3 w-3" aria-hidden="true" />
                                      </button>
                                    ) : (
                                      <button
                                        type="button"
                                        aria-label={`Delete ${stage.name}`}
                                        title={`Delete ${stage.name}`}
                                        data-testid={`quick-delete-stage-${stage.id}`}
                                        disabled={busyAction !== null}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          setConfirmDeleteStageId(stage.id);
                                        }}
                                        className="rounded p-0.5 text-ink-muted hover:bg-coral-accent/15 hover:text-coral-accent disabled:opacity-30"
                                      >
                                        <Trash2 className="h-3 w-3" aria-hidden="true" />
                                      </button>
                                    )}
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
          </div>

          {selectedStage ? (
            <div
              data-testid="inline-stage-editor-pane"
              className="min-h-0 flex-1 basis-1/2 space-y-3 overflow-y-auto border-t border-border bg-canvas/35 p-3"
            >
              <div className="flex items-center justify-between gap-2 border-b border-border/70 pb-2">
                <div className="text-[10px] font-semibold uppercase tracking-wider text-sky-accent">
                  Edit stage
                </div>
                <button
                  type="button"
                  onClick={() => onSelectStage(null)}
                  aria-label="Deselect stage"
                  title="Deselect stage"
                  className="rounded p-1 text-ink-muted hover:bg-elevated hover:text-ink"
                >
                  <X className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </div>
              <StageEditorForm
                key={`${selectedStage.id}-${selectedStage.name}-${selectedStage.day_index}-${selectedStage.activity_type ?? "cycling"}-${selectedStage.trim_start_s ?? "n"}-${selectedStage.trim_end_s ?? "n"}-${selectedStage.distance_m}`}
                stage={selectedStage}
                stages={stages}
                matchedPhotos={matchedPhotos}
                spec={spec}
                busyAction={busyAction}
                onSave={(draft) => void handleSaveStageDetails(selectedStage, draft)}
                onClearTrim={() => void handleClearTrim(selectedStage)}
                onDelete={() => void handleDeleteStage(selectedStage.id)}
                onSplitStage={(payload) => handleSplitStage(selectedStage, payload)}
                onStageColorChange={onStageColorChange}
                onStageTransitionChange={onStageTransitionChange}
                onStageCameraOverrideChange={onStageCameraOverrideChange}
                onStageSettlementLabelModeChange={onStageSettlementLabelModeChange}
              />
            </div>
          ) : null}
        </div>
      ) : (
        <div className="flex-1 space-y-3 overflow-y-auto p-3">
          <PhotoStopsSection
            projectId={projectId}
            photos={photos}
            matchedPhotos={matchedPhotos}
            googlePhotosConfigured={googlePhotosConfigured}
            pinModePhotoId={pinModePhotoId}
            onTogglePinMode={onTogglePinMode}
            onPhotosMutated={onPhotosMutated}
          />
        </div>
      )}
    </aside>
  );
}

interface StageEditorFormProps {
  stage: StageOut;
  stages: readonly StageOut[];
  matchedPhotos: readonly MatchedPhoto[];
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
  onSplitStage: (payload: StageSplitRequest) => Promise<string | null>;
  onStageColorChange: (stageId: string, colorHex: string) => void;
  onStageTransitionChange: (stageId: string, transition: TransitionIcon) => void;
  onStageCameraOverrideChange?: (stageId: string, override: StageCameraOverride | null) => void;
  onStageSettlementLabelModeChange?: (stageId: string, mode: SettlementLabelMode | null) => void;
}

function StageEditorForm({
  stage,
  stages,
  matchedPhotos,
  spec,
  busyAction,
  onSave,
  onClearTrim,
  onDelete,
  onSplitStage,
  onStageColorChange,
  onStageTransitionChange,
  onStageCameraOverrideChange,
  onStageSettlementLabelModeChange,
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

  const minSplitM = Math.max(1, Math.round(stage.distance_m * 0.05));
  const maxSplitM = Math.max(minSplitM, Math.round(stage.distance_m * 0.95));
  const [splitDistanceM, setSplitDistanceM] = useState(() =>
    Math.min(maxSplitM, Math.max(minSplitM, Math.round(stage.distance_m * 0.5))),
  );
  const [part1Name, setPart1Name] = useState(() =>
    buildDefaultSplitName(stage.name, " (Part 1)"),
  );
  const [part2Name, setPart2Name] = useState(() =>
    buildDefaultSplitName(stage.name, " (Part 2)"),
  );
  const [playheadNotice, setPlayheadNotice] = useState<string | null>(null);
  const [splitError, setSplitError] = useState<string | null>(null);

  const currentOverride = spec.stage_camera_overrides?.[stage.id];
  const focusMode: StageFocusMode = currentOverride?.focus_mode ?? "auto";
  const targetZoom: number | null = currentOverride?.target_zoom ?? null;
  const speedMultiplier: number = currentOverride?.speed_multiplier ?? 1.0;
  const transitionMargin: number = currentOverride?.transition_margin ?? 0.02;

  function applyFocusPreset(mode: StageFocusMode) {
    if (!onStageCameraOverrideChange) {
      return;
    }
    if (mode === "auto") {
      onStageCameraOverrideChange(stage.id, null);
      return;
    }
    if (mode === "close") {
      onStageCameraOverrideChange(stage.id, {
        focus_mode: "close",
        target_zoom: 13.8,
        speed_multiplier: 0.7,
        transition_margin: transitionMargin,
      });
      return;
    }
    if (mode === "street") {
      onStageCameraOverrideChange(stage.id, {
        focus_mode: "street",
        target_zoom: 15.8,
        speed_multiplier: 0.2,
        transition_margin: transitionMargin,
      });
      return;
    }
    onStageCameraOverrideChange(stage.id, {
      focus_mode: "custom",
      target_zoom: targetZoom ?? 15.0,
      speed_multiplier: speedMultiplier,
      transition_margin: transitionMargin,
    });
  }

  function handleUseCurrentPlayhead() {
    const state = useEditorStore.getState();
    const lightweightStages: TimelineStage[] = [...stages]
      .sort((a, b) => a.stage_order - b.stage_order)
      .map((s) => ({
        id: s.id,
        name: s.name,
        dayIndex: s.day_index,
        stageOrder: s.stage_order,
        activityType: normalizeActivityType(s.activity_type),
        color: spec.stage_colors[s.id] ?? spec.track_color,
        transition: spec.stage_transitions[s.id] ?? "none",
        distanceM: s.distance_m,
        elevationGainM: s.elevation_gain_m,
        movingSeconds: s.moving_seconds,
        elapsedSeconds: s.elapsed_seconds,
        hasTimestamps: s.has_timestamps,
        points: [],
      }));
    const effectiveDurationS = computePhotoStopTotalDurationS(
      spec,
      matchedPhotos,
      lightweightStages,
      state.previewWatermark,
    );
    const { progressRatio } = evaluateTimelineSchedule(
      lightweightStages,
      matchedPhotos,
      spec,
      state.currentTimeRatio * effectiveDurationS,
      effectiveDurationS,
      state.previewWatermark,
    );
    const spans = computeStageProgressSpans(lightweightStages, spec);
    const span = spans.find((s) => s.stageId === stage.id);
    if (
      !span ||
      (spans.length > 1 &&
        (progressRatio < span.startProgress - 1e-6 ||
          progressRatio > span.endProgress + 1e-6))
    ) {
      setPlayheadNotice("Playhead is currently on another stage.");
      return;
    }
    const spanLen = span.endProgress - span.startProgress;
    const localFrac = spanLen > 1e-9 ? (progressRatio - span.startProgress) / spanLen : 0.5;
    const rawDist = stage.distance_m * localFrac;
    const clampedDist = Math.min(maxSplitM, Math.max(minSplitM, rawDist));
    setSplitDistanceM(Math.round(clampedDist));
    setPlayheadNotice(null);
  }

  async function handleSplitClick() {
    setSplitError(null);
    setPlayheadNotice(null);
    const errMessage = await onSplitStage({
      split_distance_m: splitDistanceM,
      part1_name: part1Name.trim() || buildDefaultSplitName(stage.name, " (Part 1)"),
      part2_name: part2Name.trim() || buildDefaultSplitName(stage.name, " (Part 2)"),
    });
    if (errMessage) {
      setSplitError(errMessage);
    }
  }

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

      {onStageSettlementLabelModeChange && (
        <label className="flex items-center justify-between gap-2 text-[11px] text-ink-muted">
          Stage place labels
          <select
            className="rounded border border-border bg-elevated px-2 py-1 text-ink"
            value={spec.stage_settlement_label_modes[stage.id] ?? "inherit"}
            onChange={(e) => {
              const mode = e.currentTarget.value;
              onStageSettlementLabelModeChange(stage.id, mode === "uniform" || mode === "follow_zoom" ? mode : null);
            }}
          >
            <option value="inherit">Use trip setting</option>
            <option value="uniform">Uniform across zoom levels</option>
            <option value="follow_zoom">Follow map zoom</option>
          </select>
        </label>
      )}

      <div
        data-testid="stage-camera-focus-section"
        className="space-y-2.5 border-t border-border/70 pt-2.5"
      >
        <div className="flex items-center justify-between text-[11px] text-ink-muted">
          <span className="inline-flex items-center gap-1 font-medium text-ink">
            <Focus className="h-3 w-3 text-sky-accent" aria-hidden="true" />
            <span>Camera focus &amp; pace</span>
          </span>
          {focusMode !== "auto" ? (
            <span className="tabular-telemetry text-[10px] text-sky-accent">
              {formatStageFocusBadge({
                focus_mode: focusMode,
                target_zoom: targetZoom,
                speed_multiplier: speedMultiplier,
                transition_margin: transitionMargin,
              })}
            </span>
          ) : null}
        </div>

        <div
          role="group"
          aria-label="Stage camera focus presets"
          className="grid grid-cols-2 gap-1.5"
        >
          {STAGE_FOCUS_PRESETS.map((preset) => {
            const active = focusMode === preset.mode;
            return (
              <button
                key={preset.mode}
                type="button"
                aria-pressed={active}
                onClick={() => applyFocusPreset(preset.mode)}
                className={`rounded-md border px-2 py-1.5 text-left transition-colors ${
                  active
                    ? "border-sky-accent bg-sky-accent/15 text-ink"
                    : "border-border bg-elevated/60 text-ink-muted hover:text-ink"
                }`}
              >
                <div className="text-[11px] font-semibold leading-tight">{preset.label}</div>
                <div className="tabular-telemetry mt-0.5 text-[9px] leading-tight opacity-75">
                  {preset.hint}
                </div>
              </button>
            );
          })}
        </div>

        <div className="space-y-2 rounded-md border border-border/70 bg-elevated/35 p-2">
          <div className="space-y-1">
            <div className="flex items-center justify-between gap-2 text-[11px] text-ink-muted">
              <span>Target zoom</span>
              <div className="flex items-center gap-1.5">
                <span className="tabular-telemetry text-ink">
                  {targetZoom !== null ? targetZoom.toFixed(1) : "Auto"}
                </span>
                <button
                  type="button"
                  data-testid="stage-target-zoom-auto-toggle"
                  aria-label="Toggle automatic target zoom"
                  aria-pressed={targetZoom === null}
                  onClick={() => {
                    if (!onStageCameraOverrideChange) return;
                    if (targetZoom !== null) {
                      if (Math.abs(speedMultiplier - 1.0) < 1e-6) {
                        onStageCameraOverrideChange(stage.id, null);
                      } else {
                        onStageCameraOverrideChange(stage.id, {
                          focus_mode: "custom",
                          target_zoom: null,
                          speed_multiplier: speedMultiplier,
                          transition_margin: transitionMargin,
                        });
                      }
                    } else {
                      onStageCameraOverrideChange(stage.id, {
                        focus_mode: "custom",
                        target_zoom: 15.8,
                        speed_multiplier: speedMultiplier,
                        transition_margin: transitionMargin,
                      });
                    }
                  }}
                  className={`rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors ${
                    targetZoom === null
                      ? "border-sky-accent bg-sky-accent/15 text-sky-accent"
                      : "border-border bg-elevated text-ink-muted hover:text-ink"
                  }`}
                >
                  Auto
                </button>
              </div>
            </div>
            <input
              type="range"
              aria-label="Stage target zoom"
              min={10}
              max={17.5}
              step={0.1}
              value={targetZoom ?? 14.0}
              onChange={(e) => {
                const nextZoom = Number(e.currentTarget.value);
                onStageCameraOverrideChange?.(stage.id, {
                  focus_mode: "custom",
                  target_zoom: nextZoom,
                  speed_multiplier: speedMultiplier,
                  transition_margin: transitionMargin,
                });
              }}
              className="w-full accent-sky-accent"
            />
            <div className="text-[10px] text-ink-muted/80">15.5+ = street level</div>
          </div>

          <div className="space-y-1">
            <div className="flex items-center justify-between text-[11px] text-ink-muted">
              <span>Playback pace</span>
              <span className="tabular-telemetry text-ink">
                {formatSpeedMultiplier(speedMultiplier)}
              </span>
            </div>
            <input
              type="range"
              aria-label="Stage playback speed"
              min={0.05}
              max={3.0}
              step={0.05}
              value={speedMultiplier}
              onChange={(e) => {
                const nextSpeed = Number(e.currentTarget.value);
                onStageCameraOverrideChange?.(stage.id, {
                  focus_mode: "custom",
                  target_zoom: targetZoom,
                  speed_multiplier: nextSpeed,
                  transition_margin: transitionMargin,
                });
              }}
              className="w-full accent-sky-accent"
            />
            <div
              role="group"
              aria-label="Quick playback pace presets"
              className="grid grid-cols-6 gap-1 pt-0.5"
            >
              {QUICK_PACE_OPTIONS.map((pace) => {
                const active = Math.abs(speedMultiplier - pace) < 1e-6;
                return (
                  <button
                    key={pace}
                    type="button"
                    data-testid={`stage-pace-chip-${pace}`}
                    aria-pressed={active}
                    onClick={() =>
                      onStageCameraOverrideChange?.(stage.id, {
                        focus_mode: "custom",
                        target_zoom: targetZoom,
                        speed_multiplier: pace,
                        transition_margin: transitionMargin,
                      })
                    }
                    className={`tabular-telemetry rounded border px-1 py-0.5 text-[10px] font-medium transition-colors ${
                      active
                        ? "border-sky-accent bg-sky-accent/15 text-sky-accent"
                        : "border-border bg-elevated text-ink-muted hover:text-ink"
                    }`}
                  >
                    {pace}×
                  </button>
                );
              })}
            </div>
          </div>
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

      <div
        data-testid="stage-split-section"
        className="space-y-2 border-t border-border/70 pt-2.5"
      >
        <div className="flex items-center justify-between gap-2 text-[11px]">
          <span className="inline-flex items-center gap-1 font-medium text-ink">
            <Scissors className="h-3 w-3 text-sky-accent" aria-hidden="true" />
            <span>Split stage</span>
          </span>
          <button
            type="button"
            data-testid="stage-split-use-playhead"
            disabled={busyAction !== null || stage.distance_m <= 0}
            onClick={handleUseCurrentPlayhead}
            className="rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-ink-muted hover:border-sky-accent/60 hover:text-sky-accent transition-colors disabled:opacity-50"
          >
            Use current playhead
          </button>
        </div>

        <div className="space-y-1">
          <input
            type="range"
            aria-label="Split distance"
            data-testid="stage-split-slider"
            min={minSplitM}
            max={maxSplitM}
            step={1}
            value={splitDistanceM}
            onChange={(e) => {
              setSplitDistanceM(Number(e.currentTarget.value));
              setPlayheadNotice(null);
            }}
            className="w-full accent-sky-accent"
          />
          <div
            data-testid="stage-split-readout"
            className="tabular-telemetry text-[10px] text-ink-muted"
          >
            Part 1: {formatDistance(splitDistanceM, spec.units)} · Part 2:{" "}
            {formatDistance(Math.max(0, stage.distance_m - splitDistanceM), spec.units)}
          </div>
        </div>

        {playheadNotice ? (
          <div
            data-testid="stage-split-playhead-notice"
            className="rounded border border-amber-accent/35 bg-amber-accent/10 px-2 py-1 text-[10px] text-amber-accent"
          >
            {playheadNotice}
          </div>
        ) : null}

        <div className="grid grid-cols-2 gap-1.5">
          <input
            type="text"
            aria-label="Part 1 stage name"
            maxLength={200}
            value={part1Name}
            onChange={(e) => setPart1Name(e.currentTarget.value)}
            placeholder={`${stage.name} (Part 1)`}
            className="w-full rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink placeholder:text-ink-muted/50 focus:border-sky-accent focus:outline-none"
          />
          <input
            type="text"
            aria-label="Part 2 stage name"
            maxLength={200}
            value={part2Name}
            onChange={(e) => setPart2Name(e.currentTarget.value)}
            placeholder={`${stage.name} (Part 2)`}
            className="w-full rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink placeholder:text-ink-muted/50 focus:border-sky-accent focus:outline-none"
          />
        </div>

        {splitError ? (
          <div
            role="alert"
            data-testid="stage-split-error"
            className="rounded border border-coral-accent/40 bg-coral-accent/10 px-2 py-1 text-[11px] text-ink"
          >
            {splitError}
          </div>
        ) : null}

        <button
          type="button"
          data-testid="stage-split-submit"
          disabled={busyAction !== null || stage.point_count < 4 || stage.distance_m <= 0}
          onClick={() => void handleSplitClick()}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-md border border-sky-accent/50 bg-sky-accent/10 py-1.5 text-xs font-medium text-sky-accent hover:bg-sky-accent/20 transition-colors disabled:opacity-50"
        >
          {busyAction === `split-${stage.id}` ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Scissors className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          <span>{busyAction === `split-${stage.id}` ? "Splitting..." : "Split stage"}</span>
        </button>
      </div>
    </form>
  );
}
