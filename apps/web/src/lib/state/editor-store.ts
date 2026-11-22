import { create } from "zustand";
import {
  createDefaultRenderSpecV2,
  normalizeRenderSpecV2,
  type ProjectDetailOut,
  type ProjectShareOut,
  type RenderSpecV2,
  type StageCameraOverride,
  type StageOut,
  type StatisticsPanelConfig,
  type TransitionIcon,
} from "../api/types";

export type InspectorSection = "route" | "camera" | "hud" | "export";
export type SidebarTab = "stages" | "edit" | "photos";
export type PlaybackSpeed = 0.5 | 1 | 2 | 4;

export const MAX_UNDO_HISTORY = 30;
const COALESCE_WINDOW_MS = 650;

export interface EditorHistorySnapshot {
  title: string;
  spec: RenderSpecV2;
}

function cloneSpec(spec: RenderSpecV2): RenderSpecV2 {
  return {
    ...spec,
    stage_colors: { ...spec.stage_colors },
    stage_transitions: { ...spec.stage_transitions },
    stage_camera_overrides: Object.fromEntries(
      Object.entries(spec.stage_camera_overrides ?? {}).map(([k, v]) => [k, { ...v }]),
    ),
    camera_keyframes: (spec.camera_keyframes ?? []).map((kf) => ({ ...kf })),
    statistics_panel: {
      ...spec.statistics_panel,
      metrics: [...spec.statistics_panel.metrics],
    },
  };
}

function pushUndoHistory(
  state: EditorState,
  coalesceKey: string | null,
): Pick<EditorState, "undoStack" | "redoStack" | "lastHistoryAt" | "lastHistoryKey"> {
  const now = Date.now();
  const canCoalesce =
    coalesceKey !== null &&
    state.lastHistoryKey === coalesceKey &&
    now - state.lastHistoryAt < COALESCE_WINDOW_MS &&
    state.undoStack.length > 0;

  if (canCoalesce) {
    return {
      undoStack: state.undoStack,
      redoStack: [],
      lastHistoryAt: now,
      lastHistoryKey: coalesceKey,
    };
  }

  const snapshot: EditorHistorySnapshot = {
    title: state.title,
    spec: cloneSpec(state.spec),
  };
  const nextUndo = [...state.undoStack, snapshot].slice(-MAX_UNDO_HISTORY);
  return {
    undoStack: nextUndo,
    redoStack: [],
    lastHistoryAt: now,
    lastHistoryKey: coalesceKey,
  };
}

export interface EditorState {
  projectId: string | null;
  title: string;
  revision: number;
  stages: StageOut[];
  spec: RenderSpecV2;
  shareEnabled: boolean;
  shareSlug: string | null;
  shareCreatedAt: string | null;
  selectedStageId: string | null;
  inspectorSection: InspectorSection;
  sidebarTab: SidebarTab;
  pinModePhotoId: string | null;
  hasUnsavedChanges: boolean;
  isSaving: boolean;
  lastSavedAt: string | null;
  conflictError: string | null;
  currentTimeRatio: number;
  isPlaying: boolean;
  playbackSpeed: PlaybackSpeed;
  previewWatermark: boolean;
  undoStack: EditorHistorySnapshot[];
  redoStack: EditorHistorySnapshot[];
  lastHistoryAt: number;
  lastHistoryKey: string | null;

  hydrateProject: (project: ProjectDetailOut) => void;
  setShareState: (share: ProjectShareOut) => void;
  setTitle: (title: string) => void;
  updateSpec: (patch: Partial<RenderSpecV2>, coalesceKey?: string) => void;
  updateStatisticsPanel: (patch: Partial<StatisticsPanelConfig>, coalesceKey?: string) => void;
  setStageColor: (stageId: string, colorHex: string) => void;
  setStageTransition: (stageId: string, transition: TransitionIcon) => void;
  setStageCameraOverride: (
    stageId: string,
    override: StageCameraOverride | null,
    coalesceKey?: string,
  ) => void;
  setStages: (stages: StageOut[]) => void;
  selectStage: (stageId: string | null) => void;
  setInspectorSection: (section: InspectorSection) => void;
  setSidebarTab: (tab: SidebarTab) => void;
  setPinModePhotoId: (photoId: string | null) => void;
  setCurrentTimeRatio: (ratio: number) => void;
  setPlaying: (playing: boolean) => void;
  togglePlaying: () => void;
  setPlaybackSpeed: (speed: PlaybackSpeed) => void;
  setPreviewWatermark: (enabled: boolean) => void;
  resetPlayback: () => void;
  undo: () => void;
  redo: () => void;
  markSaving: (saving: boolean) => void;
  markSaved: (revision: number) => void;
  setConflictError: (message: string | null) => void;
  resetEditor: () => void;
}

function clampUnit(value: number): number {
  if (Number.isNaN(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

function inferSpecCoalesceKey(patch: Partial<RenderSpecV2>): string | null {
  const keys = Object.keys(patch);
  if (keys.length === 1) {
    const key = keys[0]!;
    if (
      key === "camera_pitch_deg" ||
      key === "camera_bearing_deg" ||
      key === "track_width_px" ||
      key === "trail_duration_seconds" ||
      key === "duration_seconds" ||
      key === "intro_hold_seconds" ||
      key === "outro_hold_seconds" ||
      key === "follow_zoom_offset" ||
      key === "target_screen_px_per_sec" ||
      key === "photo_display_seconds" ||
      key === "photo_group_gap_seconds" ||
      key === "track_color" ||
      key === "marker_color" ||
      key === "terrain_exaggeration" ||
      key === "telemetry_smoothing" ||
      key === "climb_callout_duration_s"
    ) {
      return `spec:${key}`;
    }
  }
  return null;
}

function inferHudCoalesceKey(patch: Partial<StatisticsPanelConfig>): string | null {
  const keys = Object.keys(patch);
  if (keys.length === 1) {
    const key = keys[0]!;
    if (
      key === "text_size" ||
      key === "scale" ||
      key === "background_opacity" ||
      key === "text_color" ||
      key === "background_color" ||
      key === "time_zone"
    ) {
      return `hud:${key}`;
    }
  }
  return null;
}

export const useEditorStore = create<EditorState>((set) => ({
  projectId: null,
  title: "",
  revision: 1,
  stages: [],
  spec: createDefaultRenderSpecV2(),
  shareEnabled: false,
  shareSlug: null,
  shareCreatedAt: null,
  selectedStageId: null,
  inspectorSection: "route",
  sidebarTab: "stages",
  pinModePhotoId: null,
  hasUnsavedChanges: false,
  isSaving: false,
  lastSavedAt: null,
  conflictError: null,
  currentTimeRatio: 0,
  isPlaying: false,
  playbackSpeed: 1,
  previewWatermark: false,
  undoStack: [],
  redoStack: [],
  lastHistoryAt: 0,
  lastHistoryKey: null,

  hydrateProject: (project) =>
    set((state) => {
      const normalizedSpec = normalizeRenderSpecV2(project.settings);
      const isSameProject = state.projectId === project.id;
      const firstStageId = project.stages[0]?.id ?? null;
      const selectedExists = project.stages.some((s) => s.id === state.selectedStageId);

      return {
        projectId: project.id,
        title: project.title,
        revision: project.revision,
        stages: project.stages,
        spec: normalizedSpec,
        shareEnabled: Boolean(project.share_enabled),
        shareSlug: project.share_slug ?? null,
        shareCreatedAt: project.share_created_at ?? null,
        selectedStageId: isSameProject && selectedExists ? state.selectedStageId : firstStageId,
        sidebarTab: isSameProject ? state.sidebarTab : "stages",
        pinModePhotoId: isSameProject ? state.pinModePhotoId : null,
        hasUnsavedChanges: false,
        isSaving: false,
        conflictError: null,
        undoStack: isSameProject ? state.undoStack : [],
        redoStack: isSameProject ? state.redoStack : [],
        lastHistoryAt: isSameProject ? state.lastHistoryAt : 0,
        lastHistoryKey: isSameProject ? state.lastHistoryKey : null,
      };
    }),

  setShareState: (share) =>
    set({
      shareEnabled: share.share_enabled,
      shareSlug: share.share_slug,
      shareCreatedAt: share.share_created_at,
    }),

  setTitle: (title) =>
    set((state) => ({
      ...pushUndoHistory(state, "title"),
      title,
      hasUnsavedChanges: true,
    })),

  updateSpec: (patch, coalesceKey) =>
    set((state) => ({
      ...pushUndoHistory(state, coalesceKey ?? inferSpecCoalesceKey(patch)),
      spec: {
        ...state.spec,
        ...patch,
        ...(patch.camera_keyframes
          ? {
              camera_keyframes: [...patch.camera_keyframes]
                .slice(0, 40)
                .map((kf) => ({ ...kf }))
                .sort((a, b) => a.progress - b.progress),
            }
          : {}),
      },
      hasUnsavedChanges: true,
    })),

  updateStatisticsPanel: (patch, coalesceKey) =>
    set((state) => ({
      ...pushUndoHistory(state, coalesceKey ?? inferHudCoalesceKey(patch)),
      spec: {
        ...state.spec,
        statistics_panel: {
          ...state.spec.statistics_panel,
          ...patch,
        },
      },
      hasUnsavedChanges: true,
    })),

  setStageColor: (stageId, colorHex) =>
    set((state) => ({
      ...pushUndoHistory(state, `stage-color:${stageId}`),
      spec: {
        ...state.spec,
        stage_colors: {
          ...state.spec.stage_colors,
          [stageId]: colorHex,
        },
      },
      hasUnsavedChanges: true,
    })),

  setStageTransition: (stageId, transition) =>
    set((state) => ({
      ...pushUndoHistory(state, null),
      spec: {
        ...state.spec,
        stage_transitions: {
          ...state.spec.stage_transitions,
          [stageId]: transition,
        },
      },
      hasUnsavedChanges: true,
    })),

  setStageCameraOverride: (stageId, override, coalesceKey) =>
    set((state) => {
      const nextOverrides = { ...(state.spec.stage_camera_overrides ?? {}) };
      if (override === null || override.focus_mode === "auto") {
        delete nextOverrides[stageId];
      } else {
        nextOverrides[stageId] = { ...override };
      }
      return {
        ...pushUndoHistory(state, coalesceKey ?? `stage-camera:${stageId}`),
        spec: {
          ...state.spec,
          stage_camera_overrides: nextOverrides,
        },
        hasUnsavedChanges: true,
      };
    }),

  setStages: (stages) =>
    set((state) => {
      const validIds = new Set(stages.map((s) => s.id));
      const cleanColors = Object.fromEntries(
        Object.entries(state.spec.stage_colors).filter(([id]) => validIds.has(id)),
      );
      const cleanTransitions = Object.fromEntries(
        Object.entries(state.spec.stage_transitions).filter(([id]) => validIds.has(id)),
      );
      const cleanOverrides = Object.fromEntries(
        Object.entries(state.spec.stage_camera_overrides ?? {}).filter(([id]) => validIds.has(id)),
      );
      const selectedExists = stages.some((s) => s.id === state.selectedStageId);
      return {
        stages,
        selectedStageId: selectedExists ? state.selectedStageId : (stages[0]?.id ?? null),
        spec: {
          ...state.spec,
          stage_colors: cleanColors,
          stage_transitions: cleanTransitions,
          stage_camera_overrides: cleanOverrides,
        },
      };
    }),

  selectStage: (stageId) => set({ selectedStageId: stageId }),
  setInspectorSection: (section) => set({ inspectorSection: section }),
  setSidebarTab: (tab) => set({ sidebarTab: tab }),
  setPinModePhotoId: (photoId) => set({ pinModePhotoId: photoId }),
  setCurrentTimeRatio: (ratio) => set({ currentTimeRatio: clampUnit(ratio) }),
  setPlaying: (playing) => set({ isPlaying: playing }),
  togglePlaying: () => set((state) => ({ isPlaying: !state.isPlaying })),
  setPlaybackSpeed: (speed) => set({ playbackSpeed: speed }),
  setPreviewWatermark: (enabled) => set({ previewWatermark: enabled }),
  resetPlayback: () => set({ currentTimeRatio: 0, isPlaying: false }),

  undo: () =>
    set((state) => {
      if (state.undoStack.length === 0) {
        return state;
      }
      const previous = state.undoStack[state.undoStack.length - 1]!;
      const nextUndo = state.undoStack.slice(0, -1);
      const currentSnapshot: EditorHistorySnapshot = {
        title: state.title,
        spec: cloneSpec(state.spec),
      };
      const nextRedo = [...state.redoStack, currentSnapshot].slice(-MAX_UNDO_HISTORY);
      return {
        title: previous.title,
        spec: cloneSpec(previous.spec),
        undoStack: nextUndo,
        redoStack: nextRedo,
        lastHistoryKey: null,
        lastHistoryAt: 0,
        hasUnsavedChanges: true,
      };
    }),

  redo: () =>
    set((state) => {
      if (state.redoStack.length === 0) {
        return state;
      }
      const next = state.redoStack[state.redoStack.length - 1]!;
      const nextRedo = state.redoStack.slice(0, -1);
      const currentSnapshot: EditorHistorySnapshot = {
        title: state.title,
        spec: cloneSpec(state.spec),
      };
      const nextUndo = [...state.undoStack, currentSnapshot].slice(-MAX_UNDO_HISTORY);
      return {
        title: next.title,
        spec: cloneSpec(next.spec),
        undoStack: nextUndo,
        redoStack: nextRedo,
        lastHistoryKey: null,
        lastHistoryAt: 0,
        hasUnsavedChanges: true,
      };
    }),

  markSaving: (saving) => set({ isSaving: saving }),
  markSaved: (revision) =>
    set({
      revision,
      hasUnsavedChanges: false,
      isSaving: false,
      conflictError: null,
      lastSavedAt: new Date().toISOString(),
    }),
  setConflictError: (message) => set({ conflictError: message, isSaving: false }),
  resetEditor: () =>
    set({
      projectId: null,
      title: "",
      revision: 1,
      stages: [],
      spec: createDefaultRenderSpecV2(),
      shareEnabled: false,
      shareSlug: null,
      shareCreatedAt: null,
      selectedStageId: null,
      inspectorSection: "route",
      sidebarTab: "stages",
      pinModePhotoId: null,
      hasUnsavedChanges: false,
      isSaving: false,
      conflictError: null,
      currentTimeRatio: 0,
      isPlaying: false,
      previewWatermark: false,
      undoStack: [],
      redoStack: [],
      lastHistoryAt: 0,
      lastHistoryKey: null,
    }),
}));
