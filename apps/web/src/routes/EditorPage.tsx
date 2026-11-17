import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Download,
  Film,
  Loader2,
  Pause,
  Play,
  Redo2,
  RefreshCw,
  RotateCcw,
  Save,
  Undo2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { StoryTrackStrip } from "../components/charts/StoryTrackStrip";
import { TelemetryChart } from "../components/charts/TelemetryChart";
import { AuditLogDrawer } from "../components/editor/AuditLogDrawer";
import { InspectorPanel } from "../components/editor/InspectorPanel";
import { SharePopover } from "../components/editor/SharePopover";
import { StageSidebar } from "../components/editor/StageSidebar";
import { StravaPickerModal } from "../components/editor/StravaPickerModal";
import { AppHeader } from "../components/layout/AppHeader";
import { TripStageCanvas } from "../components/stage/TripStageCanvas";
import { ApiError } from "../lib/api/client";
import {
  authApi,
  photosApi,
  projectsApi,
  stagesApi,
  systemApi,
  telemetryApi,
} from "../lib/api/endpoints";
import type {
  DirectorPreset,
  ProjectDetailOut,
  ProjectHighlightsOut,
  ProjectKpisOut,
  ProjectPhotoOut,
  PublicConfigOut,
  SessionInfoOut,
  TelemetryChartOut,
  TelemetryGeometryFeature,
} from "../lib/api/types";
import { generateDirectorKeyframes } from "../lib/geo/director";
import { formatDistance, formatElapsedTime, formatElevation } from "../lib/geo/format";
import {
  evaluateTimelineSchedule,
  matchPhotosToTimeline,
  progressRatioToTimeRatio,
} from "../lib/geo/photos";
import { buildTimelineFromProject } from "../lib/geo/timeline";
import { type PlaybackSpeed, useEditorStore } from "../lib/state/editor-store";

const PLAYBACK_SPEEDS: readonly PlaybackSpeed[] = [0.5, 1, 2, 4];

function isEditableElement(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    return true;
  }
  if (target.isContentEditable) {
    return true;
  }
  return Boolean(target.closest("[role='dialog']"));
}

export function EditorPage() {
  const { projectId } = useParams({ from: "/projects/$projectId" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [stravaModalOpen, setStravaModalOpen] = useState(false);
  const [auditDrawerOpen, setAuditDrawerOpen] = useState(false);
  const autosaveTimerRef = useRef<number | null>(null);

  const {
    title,
    revision,
    stages,
    spec,
    shareEnabled,
    shareSlug,
    selectedStageId,
    inspectorSection,
    sidebarTab,
    pinModePhotoId,
    hasUnsavedChanges,
    isSaving,
    conflictError,
    currentTimeRatio,
    isPlaying,
    playbackSpeed,
    previewWatermark,
    undoStack,
    redoStack,
    hydrateProject,
    setShareState,
    setTitle,
    updateSpec,
    updateStatisticsPanel,
    setStageColor,
    setStageTransition,
    selectStage,
    setInspectorSection,
    setSidebarTab,
    setPinModePhotoId,
    setCurrentTimeRatio,
    setPlaying,
    togglePlaying,
    setPlaybackSpeed,
    setPreviewWatermark,
    resetPlayback,
    undo,
    redo,
    markSaving,
    markSaved,
    setConflictError,
  } = useEditorStore();

  const sessionQuery = useQuery<SessionInfoOut>({
    queryKey: ["auth", "me"],
    queryFn: ({ signal }) => authApi.me(signal),
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
    retry: false,
  });

  const projectQuery = useQuery<ProjectDetailOut>({
    queryKey: ["projects", projectId],
    queryFn: ({ signal }) => projectsApi.get(projectId, signal),
    retry: false,
  });

  const stageGeometryQueries = useQueries({
    queries: stages.map((stage) => ({
      queryKey: [
        "projects",
        projectId,
        "stage-geometry",
        stage.id,
        stage.point_count,
        stage.trim_start_s,
        stage.trim_end_s,
        "medium",
      ],
      queryFn: ({ signal }: { signal?: AbortSignal }) =>
        telemetryApi.stageGeometry(projectId, stage.id, { lod: "medium", signal }),
      staleTime: Infinity,
      retry: 2,
      enabled: Boolean(projectQuery.data),
    })),
  });

  const chartQuery = useQuery<TelemetryChartOut>({
    queryKey: ["projects", projectId, "chart"],
    queryFn: ({ signal }) => telemetryApi.chart(projectId, { maxPoints: 1200, signal }),
    enabled: Boolean(projectQuery.data),
    retry: false,
  });

  const kpisQuery = useQuery<ProjectKpisOut>({
    queryKey: ["projects", projectId, "kpis"],
    queryFn: ({ signal }) => telemetryApi.kpis(projectId, signal),
    enabled: Boolean(projectQuery.data),
    retry: false,
  });

  const highlightsQuery = useQuery<ProjectHighlightsOut>({
    queryKey: ["projects", projectId, "highlights"],
    queryFn: ({ signal }) => telemetryApi.highlights(projectId, signal),
    enabled: Boolean(projectQuery.data) && stages.length > 0,
    retry: false,
  });

  const photosQuery = useQuery<ProjectPhotoOut[]>({
    queryKey: ["projects", projectId, "photos"],
    queryFn: ({ signal }) => photosApi.list(projectId, signal),
    enabled: Boolean(projectQuery.data),
    retry: false,
  });

  const publicConfigQuery = useQuery<PublicConfigOut>({
    queryKey: ["public-config"],
    queryFn: ({ signal }) => systemApi.publicConfig(signal),
    retry: false,
    staleTime: 60_000,
  });

  useEffect(() => {
    if (
      sessionQuery.isError &&
      sessionQuery.error instanceof ApiError &&
      sessionQuery.error.status === 401
    ) {
      void navigate({ to: "/login" });
    }
  }, [sessionQuery.isError, sessionQuery.error, navigate]);

  useEffect(() => {
    if (projectQuery.data) {
      hydrateProject(projectQuery.data);
    }
  }, [projectQuery.data, hydrateProject]);

  const flushSave = useCallback(async (): Promise<boolean> => {
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }

    const state = useEditorStore.getState();
    if (!state.projectId || !state.hasUnsavedChanges) {
      return !state.conflictError;
    }
    if (state.conflictError) {
      return false;
    }

    markSaving(true);
    try {
      const updated = await projectsApi.update(state.projectId, {
        revision: state.revision,
        title: state.title.trim() || "Untitled trip",
        settings: state.spec,
      });
      markSaved(updated.revision);
      queryClient.setQueryData(["projects", projectId], updated);
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "audit"] });
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflictError(err.message);
      } else {
        setConflictError(err instanceof ApiError ? err.message : "Autosave failed.");
      }
      return false;
    }
  }, [markSaving, markSaved, setConflictError, queryClient, projectId]);

  // Debounced autosave
  useEffect(() => {
    if (!hasUnsavedChanges || conflictError || !projectQuery.data) {
      return;
    }
    autosaveTimerRef.current = window.setTimeout(() => {
      void flushSave();
    }, 1200);

    return () => {
      if (autosaveTimerRef.current !== null) {
        window.clearTimeout(autosaveTimerRef.current);
        autosaveTimerRef.current = null;
      }
    };
  }, [hasUnsavedChanges, conflictError, title, spec, projectQuery.data, flushSave]);

  // Keyboard shortcuts: Space (play/pause), ArrowLeft/Right (±2%, Shift ±10%), Home/End, Ctrl/Cmd+Z / Shift+Z
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (stravaModalOpen || auditDrawerOpen || isEditableElement(event.target)) {
        return;
      }

      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) {
          redo();
        } else {
          undo();
        }
        return;
      }

      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "y") {
        event.preventDefault();
        redo();
        return;
      }

      if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      if (event.code === "Space" || event.key === " ") {
        event.preventDefault();
        togglePlaying();
        return;
      }

      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const delta = (event.shiftKey ? 0.1 : 0.02) * (event.key === "ArrowLeft" ? -1 : 1);
        const cur = useEditorStore.getState().currentTimeRatio;
        setPlaying(false);
        setCurrentTimeRatio(cur + delta);
        return;
      }

      if (event.key === "Home") {
        event.preventDefault();
        setPlaying(false);
        setCurrentTimeRatio(0);
        return;
      }

      if (event.key === "End") {
        event.preventDefault();
        setPlaying(false);
        setCurrentTimeRatio(1);
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    stravaModalOpen,
    auditDrawerOpen,
    togglePlaying,
    setPlaying,
    setCurrentTimeRatio,
    undo,
    redo,
  ]);

  async function handleReloadServerVersion() {
    setConflictError(null);
    const fresh = await projectQuery.refetch();
    if (fresh.data) {
      hydrateProject(fresh.data);
    }
  }

  async function handleForceOverwrite() {
    setConflictError(null);
    markSaving(true);
    try {
      const latest = await projectsApi.get(projectId);
      const state = useEditorStore.getState();
      const updated = await projectsApi.update(projectId, {
        revision: latest.revision,
        title: state.title.trim() || latest.title,
        settings: state.spec,
      });
      markSaved(updated.revision);
      queryClient.setQueryData(["projects", projectId], updated);
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "audit"] });
    } catch (err) {
      setConflictError(err instanceof ApiError ? err.message : "Failed to overwrite project.");
    }
  }

  const refreshProjectAndTelemetry = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["projects", projectId], exact: true }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "chart"] }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "kpis"] }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "highlights"] }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "photos"] }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "audit"] }),
    ]);
  }, [queryClient, projectId]);

  const refreshPhotos = useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "photos"] }),
      queryClient.invalidateQueries({ queryKey: ["projects", projectId, "audit"] }),
    ]);
  }, [queryClient, projectId]);

  const effectiveDurationS = previewWatermark
    ? Math.min(spec.duration_seconds, 5)
    : spec.duration_seconds;

  // Playback animation loop
  useEffect(() => {
    if (!isPlaying) {
      return;
    }
    let rafId = 0;
    let lastTs = performance.now();
    const durationMs = Math.max(1000, effectiveDurationS * 1000);

    const tick = (now: number) => {
      const dt = now - lastTs;
      lastTs = now;
      const prev = useEditorStore.getState().currentTimeRatio;
      const next = prev + (dt * playbackSpeed) / durationMs;
      if (next >= 1) {
        setCurrentTimeRatio(1);
        setPlaying(false);
        return;
      }
      setCurrentTimeRatio(next);
      rafId = window.requestAnimationFrame(tick);
    };

    rafId = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(rafId);
  }, [isPlaying, playbackSpeed, effectiveDurationS, setCurrentTimeRatio, setPlaying]);

  const resolvedFeatures = useMemo(() => {
    const features: TelemetryGeometryFeature[] = [];
    for (const q of stageGeometryQueries) {
      if (q.data) {
        features.push(q.data);
      }
    }
    return features;
  }, [stageGeometryQueries]);

  const geometryStatus = useMemo(() => {
    const pendingCount = stageGeometryQueries.filter((q) => q.isPending).length;
    const errorCount = stageGeometryQueries.filter((q) => q.isError).length;
    return {
      pendingCount,
      errorCount,
      onRetryFailed: () => {
        for (const q of stageGeometryQueries) {
          if (q.isError) {
            void q.refetch();
          }
        }
      },
    };
  }, [stageGeometryQueries]);

  const timelineStages = useMemo(
    () =>
      buildTimelineFromProject(
        stages,
        resolvedFeatures,
        chartQuery.data,
        spec,
      ),
    [stages, resolvedFeatures, chartQuery.data, spec],
  );

  const projectPhotos = useMemo(
    () => (Array.isArray(photosQuery.data) ? photosQuery.data : []),
    [photosQuery.data],
  );

  const matchedPhotos = useMemo(
    () =>
      matchPhotosToTimeline(projectPhotos, timelineStages, spec, (photoId, variant) =>
        photosApi.imageUrl(projectId, photoId, variant),
      ),
    [projectPhotos, timelineStages, spec, projectId],
  );

  const schedule = useMemo(
    () =>
      evaluateTimelineSchedule(
        timelineStages,
        matchedPhotos,
        spec,
        currentTimeRatio * effectiveDurationS,
        effectiveDurationS,
        previewWatermark,
      ),
    [timelineStages, matchedPhotos, spec, currentTimeRatio, effectiveDurationS, previewWatermark],
  );

  const isPausedAtPhoto = Boolean(spec.pause_on_photo && schedule.activePhoto);

  const dayCount = useMemo(
    () => stages.reduce((acc, s) => Math.max(acc, s.day_index), 1),
    [stages],
  );

  const handleMapPinClick = useCallback(
    async ([lon, lat]: [number, number]) => {
      if (!pinModePhotoId) return;
      try {
        await photosApi.pin(projectId, pinModePhotoId, { lat, lon });
        setPinModePhotoId(null);
        await refreshPhotos();
      } catch {
        setPinModePhotoId(null);
      }
    },
    [pinModePhotoId, projectId, setPinModePhotoId, refreshPhotos],
  );

  const handleTrimBrush = useCallback(
    async (startDistM: number, endDistM: number) => {
      const midDist = (startDistM + endDistM) / 2;
      const targetTimelineStage =
        timelineStages.find((st) => st.id === selectedStageId && st.points.length >= 2) ??
        timelineStages.find((st) => {
          if (st.points.length < 2) return false;
          const firstD = st.points[0]!.globalDistM;
          const lastD = st.points[st.points.length - 1]!.globalDistM;
          return midDist >= firstD && midDist <= lastD;
        }) ??
        timelineStages.find((st) => st.points.length >= 2);
      if (!targetTimelineStage) return;

      const pts = targetTimelineStage.points;
      const sampleElapsed = (distM: number): number => {
        if (distM <= pts[0]!.globalDistM) return pts[0]!.elapsedS;
        const last = pts[pts.length - 1]!;
        if (distM >= last.globalDistM) return last.elapsedS;
        for (let i = 1; i < pts.length; i++) {
          const a = pts[i - 1]!;
          const b = pts[i]!;
          if (distM <= b.globalDistM) {
            const span = b.globalDistM - a.globalDistM;
            const t = span > 0 ? (distM - a.globalDistM) / span : 0;
            return a.elapsedS + (b.elapsedS - a.elapsedS) * t;
          }
        }
        return last.elapsedS;
      };

      const rawStartS = Math.max(0, Math.round(sampleElapsed(startDistM)));
      const rawEndS = Math.max(rawStartS + 1, Math.round(sampleElapsed(endDistM)));
      try {
        await stagesApi.update(projectId, targetTimelineStage.id, {
          trim_start_s: rawStartS,
          trim_end_s: rawEndS,
        });
        await refreshProjectAndTelemetry();
      } catch {
        // Ignore transient trim error; stage properties form remains available
      }
    },
    [timelineStages, selectedStageId, projectId, refreshProjectAndTelemetry],
  );

  const handleApplyDirectorPreset = useCallback(
    (preset: DirectorPreset) => {
      if (preset === "none") {
        updateSpec({ director_preset: "none", camera_keyframes: [] });
        return;
      }
      const generated = generateDirectorKeyframes(preset, {
        stages: timelineStages,
        spec,
        climbs: highlightsQuery.data?.climbs ?? [],
        photos: matchedPhotos,
      });
      updateSpec({ director_preset: preset, camera_keyframes: generated });
    },
    [timelineStages, spec, highlightsQuery.data?.climbs, matchedPhotos, updateSpec],
  );

  const user = sessionQuery.data?.user ?? null;
  const stravaConnected = Boolean(
    user?.strava_connected ??
      (sessionQuery.data as { strava_connected?: boolean } | undefined)?.strava_connected,
  );
  const kpis = kpisQuery.data;

  async function handleLogout() {
    try {
      await authApi.logout();
    } finally {
      queryClient.clear();
      await navigate({ to: "/login" });
    }
  }

  if (projectQuery.isLoading) {
    return (
      <div className="flex min-h-screen flex-col bg-canvas text-ink">
        <AppHeader
          user={user}
          onLogout={user ? () => void handleLogout() : undefined}
          sectionLabel={`Project: ${projectId}`}
        />
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="flex items-center gap-2 text-sm text-ink-muted">
            <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
            <span>Loading project workspace...</span>
          </div>
        </div>
      </div>
    );
  }

  if (projectQuery.isError) {
    const isNotFound =
      projectQuery.error instanceof ApiError && projectQuery.error.status === 404;
    const errorMessage =
      projectQuery.error instanceof Error
        ? projectQuery.error.message
        : "Unable to load project.";

    return (
      <div className="flex min-h-screen flex-col bg-canvas text-ink">
        <AppHeader
          user={user}
          onLogout={user ? () => void handleLogout() : undefined}
          sectionLabel={`Project: ${projectId}`}
        />
        <div className="flex flex-1 items-center justify-center p-6">
          <div
            role="alert"
            className="max-w-md space-y-3 rounded-xl border border-coral-accent/40 bg-surface p-6 text-center"
          >
            <AlertCircle className="mx-auto h-7 w-7 text-coral-accent" aria-hidden="true" />
            <h1 className="text-base font-semibold text-ink">
              {isNotFound ? "Project not found" : "Failed to load project"}
            </h1>
            <p className="text-xs text-ink-muted">{errorMessage}</p>
            <div className="flex items-center justify-center gap-2 pt-1">
              <button
                type="button"
                onClick={() => void projectQuery.refetch()}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-elevated px-3 py-1.5 text-xs font-medium text-ink hover:border-sky-accent"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Retry</span>
              </button>
              <Link
                to="/"
                className="inline-flex items-center gap-1.5 rounded-md bg-sky-accent px-3 py-1.5 text-xs font-semibold text-canvas hover:bg-sky-accent/90"
              >
                <span>Back to projects</span>
              </Link>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-canvas text-ink lg:h-screen lg:overflow-hidden">
      <AppHeader
        user={user}
        onLogout={user ? () => void handleLogout() : undefined}
        sectionLabel={
          projectQuery.data ? undefined : `Project: ${projectId}`
        }
        rightSlot={
          <div className="flex flex-wrap items-center gap-1.5">
            <input
              type="text"
              aria-label="Project title"
              value={title}
              onChange={(e) => setTitle(e.currentTarget.value)}
              placeholder="Trip title"
              className="w-28 sm:w-40 rounded border border-border bg-elevated px-2 py-1 text-xs font-medium text-ink focus:border-sky-accent focus:outline-none"
            />

            <div className="flex items-center rounded border border-border bg-elevated p-0.5">
              <button
                type="button"
                aria-label="Undo"
                title="Undo (Ctrl/Cmd+Z)"
                disabled={undoStack.length === 0}
                onClick={undo}
                className="rounded p-1 text-ink-muted hover:text-ink disabled:opacity-35"
              >
                <Undo2 className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
              <button
                type="button"
                aria-label="Redo"
                title="Redo (Ctrl/Cmd+Shift+Z)"
                disabled={redoStack.length === 0}
                onClick={redo}
                className="rounded p-1 text-ink-muted hover:text-ink disabled:opacity-35"
              >
                <Redo2 className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            </div>

            <button
              type="button"
              onClick={() => {
                if (conflictError) {
                  void handleForceOverwrite();
                } else {
                  void flushSave();
                }
              }}
              disabled={(!hasUnsavedChanges && !conflictError) || isSaving}
              className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors disabled:opacity-70 ${
                conflictError
                  ? "border-amber-accent/60 bg-amber-accent/15 text-amber-accent"
                  : "border-border bg-elevated text-ink-muted hover:text-ink"
              }`}
            >
              {isSaving ? (
                <Loader2 className="h-3 w-3 animate-spin text-sky-accent" aria-hidden="true" />
              ) : (
                <Save className="h-3 w-3" aria-hidden="true" />
              )}
              <span className="tabular-telemetry">
                {conflictError
                  ? "Retry"
                  : isSaving
                    ? "Saving…"
                    : hasUnsavedChanges
                      ? "Unsaved"
                      : `Saved rev ${revision}`}
              </span>
            </button>

            <SharePopover
              projectId={projectId}
              shareEnabled={shareEnabled}
              shareSlug={shareSlug}
              onShareChanged={setShareState}
            />

            <button
              type="button"
              aria-label="Activity log"
              title="Project activity log"
              onClick={() => setAuditDrawerOpen(true)}
              className="inline-flex items-center gap-1 rounded-md border border-border bg-elevated px-2 py-1 text-xs font-medium text-ink hover:border-sky-accent/50"
            >
              <Activity className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
              <span className="hidden xl:inline">Log</span>
            </button>

            <button
              type="button"
              aria-pressed={previewWatermark}
              title="Toggle 5s preview clip & watermark overlay in the stage canvas"
              onClick={() => {
                setPreviewWatermark(!previewWatermark);
                setInspectorSection("export");
              }}
              className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
                previewWatermark
                  ? "border-amber-accent/60 bg-amber-accent/15 text-amber-accent"
                  : "border-border bg-elevated text-ink hover:border-sky-accent/50"
              }`}
            >
              <Film className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
              <span className="hidden lg:inline">5s Clip Mode</span>
            </button>

            <button
              type="button"
              onClick={() => setInspectorSection("export")}
              className="inline-flex items-center gap-1.5 rounded-md bg-sky-accent px-2.5 py-1 text-xs font-medium text-canvas hover:bg-sky-accent/90 transition-colors"
            >
              <Download className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Export</span>
            </button>
          </div>
        }
      />

      {conflictError ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-2 border-b border-amber-accent/40 bg-amber-accent/15 px-4 py-2 text-xs text-ink"
        >
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-accent" aria-hidden="true" />
            <span>{conflictError}</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void handleReloadServerVersion()}
              className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2.5 py-1 text-[11px] font-medium hover:border-sky-accent"
            >
              <RefreshCw className="h-3 w-3" aria-hidden="true" />
              <span>Reload server version</span>
            </button>
            <button
              type="button"
              onClick={() => void handleForceOverwrite()}
              className="rounded bg-amber-accent px-2.5 py-1 text-[11px] font-semibold text-canvas"
            >
              Overwrite with my edits
            </button>
          </div>
        </div>
      ) : null}

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-12">
        {/* Left Pane: Trip & Stages */}
        <StageSidebar
          projectId={projectId}
          stages={stages}
          spec={spec}
          selectedStageId={selectedStageId}
          stravaConnected={stravaConnected}
          photos={projectPhotos}
          matchedPhotos={matchedPhotos}
          googlePhotosConfigured={Boolean(publicConfigQuery.data?.google_photos_configured)}
          activeTab={sidebarTab}
          pinModePhotoId={pinModePhotoId}
          onTabChange={setSidebarTab}
          onTogglePinMode={setPinModePhotoId}
          onSelectStage={selectStage}
          onStagesMutated={refreshProjectAndTelemetry}
          onPhotosMutated={refreshPhotos}
          onStageColorChange={setStageColor}
          onStageTransitionChange={setStageTransition}
          onOpenStravaModal={() => {
            void sessionQuery.refetch();
            setStravaModalOpen(true);
          }}
        />

        {/* Center Pane: 1:1 WYSIWYG Stage & Synchronized uPlot Telemetry Dock */}
        <section
          aria-label="Map preview and timeline dock"
          className="flex min-h-0 flex-col bg-canvas lg:col-span-6"
        >
          <div className="flex min-h-[340px] flex-1 items-center justify-center overflow-hidden p-3 lg:min-h-0">
            <TripStageCanvas
              stages={timelineStages}
              spec={spec}
              progressRatio={schedule.progressRatio}
              durationS={effectiveDurationS}
              travelDurationS={schedule.travelDurationS}
              isPreview={previewWatermark}
              renderMode="interactive"
              geometryStatus={geometryStatus}
              matchedPhotos={matchedPhotos}
              activePhoto={schedule.activePhoto}
              climbs={highlightsQuery.data?.climbs ?? []}
              pinModeActive={Boolean(pinModePhotoId)}
              onMapPinClick={(lngLat) => void handleMapPinClick(lngLat)}
              onCancelPinMode={() => setPinModePhotoId(null)}
              projectTitle={title}
              totalStats={
                kpis
                  ? {
                      distanceM: kpis.total_distance_m,
                      elevationGainM: kpis.total_elevation_gain_m,
                      movingSeconds: kpis.total_moving_seconds,
                      stageCount: kpis.stage_count,
                    }
                  : undefined
              }
            />
          </div>

          <div className="shrink-0 space-y-2.5 border-t border-border bg-surface px-4 py-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={togglePlaying}
                  aria-pressed={isPlaying}
                  aria-label={isPlaying ? "Pause preview" : "Play preview"}
                  className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-elevated px-3 text-xs font-medium text-ink hover:border-sky-accent/60 transition-colors"
                >
                  {isPlaying ? (
                    <Pause className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
                  ) : (
                    <Play className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
                  )}
                  <span>{isPlaying ? "Pause" : "Play"}</span>
                </button>

                <button
                  type="button"
                  onClick={resetPlayback}
                  aria-label="Reset playhead"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-ink-muted hover:text-ink transition-colors"
                >
                  <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                </button>

                <div className="flex items-center rounded-md border border-border bg-elevated p-0.5 text-[11px]">
                  {PLAYBACK_SPEEDS.map((spd) => (
                    <button
                      key={spd}
                      type="button"
                      aria-pressed={playbackSpeed === spd}
                      onClick={() => setPlaybackSpeed(spd)}
                      className={`rounded px-1.5 py-0.5 tabular-telemetry transition-colors ${
                        playbackSpeed === spd
                          ? "bg-sky-accent text-canvas font-semibold"
                          : "text-ink-muted hover:text-ink"
                      }`}
                    >
                      {spd}×
                    </button>
                  ))}
                </div>
              </div>

              <div className="flex flex-1 items-center gap-3 min-w-[160px]">
                <input
                  type="range"
                  min={0}
                  max={1000}
                  value={Math.round(schedule.progressRatio * 1000)}
                  aria-label="Timeline scrubber"
                  onChange={(e) => {
                    const targetProgress = Number(e.currentTarget.value) / 1000;
                    setPlaying(false);
                    setCurrentTimeRatio(
                      progressRatioToTimeRatio(
                        matchedPhotos,
                        spec,
                        targetProgress,
                        effectiveDurationS,
                        previewWatermark,
                      ),
                    );
                  }}
                  className="w-full accent-sky-accent"
                />
                {isPausedAtPhoto ? (
                  <span
                    data-testid="photo-pause-indicator"
                    className="shrink-0 rounded border border-amber-accent/50 bg-amber-accent/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-accent"
                  >
                    Paused at photo
                  </span>
                ) : null}
                <span className="tabular-telemetry shrink-0 text-xs text-ink-muted">
                  {(currentTimeRatio * effectiveDurationS).toFixed(1)}s /{" "}
                  {effectiveDurationS}s
                </span>
              </div>

              {kpis ? (
                <div className="tabular-telemetry hidden xl:flex items-center gap-3 text-[11px] text-ink-muted">
                  <span>{formatDistance(kpis.total_distance_m, spec.units)}</span>
                  <span className="text-teal-accent">
                    +{formatElevation(kpis.total_elevation_gain_m, spec.units)}
                  </span>
                  <span>{formatElapsedTime(kpis.total_moving_seconds)}</span>
                </div>
              ) : null}
            </div>

            <StoryTrackStrip
              stages={timelineStages}
              totalDistanceM={chartQuery.data?.total_distance_m ?? kpis?.total_distance_m ?? 0}
              progressRatio={schedule.progressRatio}
              cameraKeyframes={spec.camera_keyframes}
              matchedPhotos={matchedPhotos}
              selectedStageId={selectedStageId}
              cameraMode={spec.camera_mode}
              cameraPreset={spec.camera_preset}
              cameraPitchDeg={spec.camera_pitch_deg}
              onSelectStage={selectStage}
              onUpdateKeyframes={(keyframes) =>
                updateSpec({ director_preset: "none", camera_keyframes: keyframes })
              }
              onSeek={(ratio) => {
                setPlaying(false);
                setCurrentTimeRatio(
                  progressRatioToTimeRatio(
                    matchedPhotos,
                    spec,
                    ratio,
                    effectiveDurationS,
                    previewWatermark,
                  ),
                );
              }}
            />

            <TelemetryChart
              points={chartQuery.data?.points ?? []}
              totalDistanceM={chartQuery.data?.total_distance_m ?? 0}
              units={spec.units}
              progressRatio={schedule.progressRatio}
              smoothingWindow={spec.telemetry_smoothing}
              stages={timelineStages}
              climbs={highlightsQuery.data?.climbs ?? []}
              onTrimBrush={
                stages.length > 0
                  ? (startM, endM) => void handleTrimBrush(startM, endM)
                  : undefined
              }
              onSeek={(ratio) => {
                setPlaying(false);
                setCurrentTimeRatio(
                  progressRatioToTimeRatio(
                    matchedPhotos,
                    spec,
                    ratio,
                    effectiveDurationS,
                    previewWatermark,
                  ),
                );
              }}
            />
          </div>
        </section>

        {/* Right Pane: Settings & Export */}
        <InspectorPanel
          projectId={projectId}
          section={inspectorSection}
          spec={spec}
          stageCount={stages.length}
          dayCount={dayCount}
          currentProgress={schedule.progressRatio}
          onSelectSection={setInspectorSection}
          onUpdateSpec={updateSpec}
          onUpdateHud={updateStatisticsPanel}
          onApplyDirectorPreset={handleApplyDirectorPreset}
          onFlushSave={flushSave}
        />
      </div>

      {stravaModalOpen ? (
        <StravaPickerModal
          projectId={projectId}
          units={spec.units}
          defaultDayIndex={Math.max(1, stages.length + 1)}
          existingStages={stages}
          onClose={() => setStravaModalOpen(false)}
          onImported={() => {
            void refreshProjectAndTelemetry();
          }}
        />
      ) : null}

      <AuditLogDrawer
        projectId={projectId}
        open={auditDrawerOpen}
        onClose={() => setAuditDrawerOpen(false)}
      />
    </div>
  );
}
