import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createDefaultRenderSpecV2,
  type ProjectDetailOut,
  type StageOut,
} from "../lib/api/types";
import { MAX_UNDO_HISTORY, useEditorStore } from "../lib/state/editor-store";

function makeStage(id: string, order: number, overrides: Partial<StageOut> = {}): StageOut {
  return {
    id,
    project_id: "proj-1",
    name: `Stage ${id}`,
    day_index: order + 1,
    stage_order: order,
    source_type: "gpx",
    activity_type: "cycling",
    external_source_id: null,
    strava_activity_id: null,
    distance_m: 10_000,
    elevation_gain_m: 350,
    elevation_loss_m: 300,
    moving_seconds: 1800,
    elapsed_seconds: 2000,
    has_timestamps: true,
    started_at: "2040-04-12T08:00:00Z",
    trim_start_s: null,
    trim_end_s: null,
    point_count: 100,
    min_lat: 0.1,
    max_lat: 0.2,
    min_lon: 0.2,
    max_lon: 0.3,
    warnings: [],
    created_at: "2001-06-14T08:00:00Z",
    ...overrides,
  };
}

function makeProject(overrides: Partial<ProjectDetailOut> = {}): ProjectDetailOut {
  return {
    id: "proj-1",
    title: "Fictional Equatorial Loop",
    revision: 1,
    stage_count: 2,
    total_distance_m: 20_000,
    settings: createDefaultRenderSpecV2(),
    stages: [makeStage("s1", 0), makeStage("s2", 1)],
    share_enabled: false,
    share_slug: null,
    share_created_at: null,
    created_at: "2040-04-12T08:00:00Z",
    updated_at: "2040-04-12T08:00:00Z",
    ...overrides,
  };
}

describe("editor-store.ts", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2040-04-12T10:00:00Z"));
    useEditorStore.getState().resetEditor();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("enforces MAX_UNDO_HISTORY (30) on undo and redo stacks", () => {
    useEditorStore.getState().hydrateProject(makeProject());

    for (let i = 1; i <= MAX_UNDO_HISTORY + 5; i++) {
      vi.advanceTimersByTime(1000);
      useEditorStore.getState().setTitle(`Title ${i}`);
    }

    expect(useEditorStore.getState().undoStack).toHaveLength(MAX_UNDO_HISTORY);
    expect(useEditorStore.getState().title).toBe(`Title ${MAX_UNDO_HISTORY + 5}`);

    for (let i = 0; i < MAX_UNDO_HISTORY; i++) {
      useEditorStore.getState().undo();
    }

    expect(useEditorStore.getState().undoStack).toHaveLength(0);
    expect(useEditorStore.getState().redoStack).toHaveLength(MAX_UNDO_HISTORY);
    expect(useEditorStore.getState().title).toBe("Title 5");

    useEditorStore.getState().undo();
    expect(useEditorStore.getState().title).toBe("Title 5");

    for (let i = 0; i < MAX_UNDO_HISTORY; i++) {
      useEditorStore.getState().redo();
    }
    expect(useEditorStore.getState().redoStack).toHaveLength(0);
    expect(useEditorStore.getState().title).toBe(`Title ${MAX_UNDO_HISTORY + 5}`);

    useEditorStore.getState().undo();
    expect(useEditorStore.getState().redoStack).toHaveLength(1);
    useEditorStore.getState().updateSpec({ terrain_3d: true });
    expect(useEditorStore.getState().redoStack).toHaveLength(0);
  });

  it("coalesces rapid edits with the same key within 650ms and splits after window", () => {
    useEditorStore.getState().hydrateProject(makeProject());

    useEditorStore.getState().updateSpec({ camera_pitch_deg: 20 });
    vi.advanceTimersByTime(200);
    useEditorStore.getState().updateSpec({ camera_pitch_deg: 35 });
    expect(useEditorStore.getState().undoStack).toHaveLength(1);

    useEditorStore.getState().updateStatisticsPanel({ text_size: 16 });
    expect(useEditorStore.getState().undoStack).toHaveLength(2);

    // Advancing past COALESCE_WINDOW_MS starts a new snapshot
    vi.advanceTimersByTime(700);
    useEditorStore.getState().updateStatisticsPanel({ text_size: 18 });
    expect(useEditorStore.getState().undoStack).toHaveLength(3);
  });

  it("coalesces rapid photo_group_gap_seconds edits into one undo step", () => {
    useEditorStore.getState().hydrateProject(makeProject());

    useEditorStore.getState().updateSpec({ photo_group_gap_seconds: 2 });
    vi.advanceTimersByTime(200);
    useEditorStore.getState().updateSpec({ photo_group_gap_seconds: 3 });
    expect(useEditorStore.getState().undoStack).toHaveLength(1);
  });

  it("tracks dirty state, saving lifecycle, conflict errors, and project hydration", () => {
    const store = useEditorStore.getState();
    store.hydrateProject(makeProject());
    expect(useEditorStore.getState().hasUnsavedChanges).toBe(false);
    expect(useEditorStore.getState().selectedStageId).toBe("s1");

    store.selectStage("s2");
    store.setTitle("Edited Loop");
    expect(useEditorStore.getState().hasUnsavedChanges).toBe(true);

    store.markSaving(true);
    expect(useEditorStore.getState().isSaving).toBe(true);

    store.setConflictError("409 revision conflict");
    expect(useEditorStore.getState().isSaving).toBe(false);
    expect(useEditorStore.getState().conflictError).toBe("409 revision conflict");

    store.markSaved(2);
    expect(useEditorStore.getState().revision).toBe(2);
    expect(useEditorStore.getState().hasUnsavedChanges).toBe(false);
    expect(useEditorStore.getState().conflictError).toBeNull();
    expect(useEditorStore.getState().lastSavedAt).not.toBeNull();

    store.hydrateProject(makeProject({ revision: 3 }));
    expect(useEditorStore.getState().undoStack).toHaveLength(1);
    expect(useEditorStore.getState().selectedStageId).toBe("s2");

    store.hydrateProject(makeProject({ id: "proj-2", stages: [makeStage("s9", 0)] }));
    expect(useEditorStore.getState().undoStack).toHaveLength(0);
    expect(useEditorStore.getState().selectedStageId).toBe("s9");
  });

  it("handles stage reorder, delete, and trim transitions and cleans stage maps", () => {
    useEditorStore.getState().hydrateProject(makeProject());
    useEditorStore.getState().setStageColor("s1", "#ff0000");
    useEditorStore.getState().setStageColor("s2", "#00ff00");
    useEditorStore.getState().setStageTransition("s2", "tent");
    useEditorStore.getState().selectStage("s2");

    useEditorStore.getState().setStages([makeStage("s2", 0), makeStage("s1", 1)]);
    expect(useEditorStore.getState().stages.map((s) => s.id)).toEqual(["s2", "s1"]);
    expect(useEditorStore.getState().selectedStageId).toBe("s2");
    expect(useEditorStore.getState().spec.stage_colors).toEqual({
      s1: "#ff0000",
      s2: "#00ff00",
    });

    useEditorStore
      .getState()
      .setStages([
        makeStage("s2", 0),
        makeStage("s1", 1, { trim_start_s: 120, trim_end_s: 1500 }),
      ]);
    expect(useEditorStore.getState().stages[1]?.trim_start_s).toBe(120);
    expect(useEditorStore.getState().stages[1]?.trim_end_s).toBe(1500);

    useEditorStore.getState().setStages([makeStage("s1", 0)]);
    expect(useEditorStore.getState().selectedStageId).toBe("s1");
    expect(useEditorStore.getState().spec.stage_colors).toEqual({ s1: "#ff0000" });
    expect(useEditorStore.getState().spec.stage_transitions).toEqual({});

    useEditorStore.getState().setStages([]);
    expect(useEditorStore.getState().selectedStageId).toBeNull();
  });

  it("clamps playback time ratio and manages playback controls", () => {
    const store = useEditorStore.getState();
    store.setCurrentTimeRatio(-0.5);
    expect(useEditorStore.getState().currentTimeRatio).toBe(0);
    store.setCurrentTimeRatio(1.8);
    expect(useEditorStore.getState().currentTimeRatio).toBe(1);
    store.setCurrentTimeRatio(Number.NaN);
    expect(useEditorStore.getState().currentTimeRatio).toBe(0);

    store.togglePlaying();
    expect(useEditorStore.getState().isPlaying).toBe(true);
    store.setPlaybackSpeed(2);
    expect(useEditorStore.getState().playbackSpeed).toBe(2);
    store.setPreviewWatermark(true);
    expect(useEditorStore.getState().previewWatermark).toBe(true);
    store.resetPlayback();
    expect(useEditorStore.getState().isPlaying).toBe(false);
    expect(useEditorStore.getState().currentTimeRatio).toBe(0);
  });

  it("deep-clones camera_keyframes, director_preset, and color_by across undo/redo and tracks sidebarTab/pinModePhotoId", () => {
    const store = useEditorStore.getState();
    store.hydrateProject(makeProject());

    store.setSidebarTab("photos");
    store.setPinModePhotoId("photo-42");
    store.setInspectorSection("camera");
    expect(useEditorStore.getState().sidebarTab).toBe("photos");
    expect(useEditorStore.getState().pinModePhotoId).toBe("photo-42");
    expect(useEditorStore.getState().inspectorSection).toBe("camera");

    const kf = [
      {
        progress: 0.5,
        camera_mode: "follow-marker" as const,
        camera_preset: "drone-orbit" as const,
        pitch_deg: 52,
        bearing_offset_deg: 30,
        zoom_offset: 1,
        speed_multiplier: 0.8,
        label: "Summit",
      },
    ];
    vi.advanceTimersByTime(1000);
    store.updateSpec({
      director_preset: "expedition-doc",
      color_by: "grade",
      camera_keyframes: kf,
    });
    // Mutating external array/item must not corrupt stored spec
    kf[0]!.pitch_deg = 0;
    expect(useEditorStore.getState().spec.camera_keyframes[0]?.pitch_deg).toBe(52);
    expect(useEditorStore.getState().spec.director_preset).toBe("expedition-doc");
    expect(useEditorStore.getState().spec.color_by).toBe("grade");

    store.undo();
    expect(useEditorStore.getState().spec.camera_keyframes).toEqual([]);
    expect(useEditorStore.getState().spec.director_preset).toBe("none");
    expect(useEditorStore.getState().spec.color_by).toBe("stage");

    store.redo();
    expect(useEditorStore.getState().spec.camera_keyframes).toHaveLength(1);
    expect(useEditorStore.getState().spec.camera_keyframes[0]?.label).toBe("Summit");

    vi.advanceTimersByTime(1000);
    const undoBefore = useEditorStore.getState().undoStack.length;
    store.updateSpec({ terrain_exaggeration: 1.8 });
    vi.advanceTimersByTime(150);
    store.updateSpec({ terrain_exaggeration: 2.2 });
    expect(useEditorStore.getState().undoStack).toHaveLength(undoBefore + 1);

    store.hydrateProject(makeProject({ id: "proj-new" }));
    expect(useEditorStore.getState().pinModePhotoId).toBeNull();
    expect(useEditorStore.getState().sidebarTab).toBe("stages");
  });
});
