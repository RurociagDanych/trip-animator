import { Pause, Play, RotateCcw } from "lucide-react";
import { memo, useCallback, useEffect, useMemo } from "react";
import type {
  CameraKeyframe,
  ClimbHighlightOut,
  ProjectKpisOut,
  RenderSpecV2,
  TelemetryChartPoint,
} from "../../lib/api/types";
import { formatDistance, formatElapsedTime, formatElevation } from "../../lib/geo/format";
import {
  evaluateTimelineSchedule,
  progressRatioToTimeRatio,
  type MatchedPhoto,
} from "../../lib/geo/photos";
import type { TimelineStage } from "../../lib/geo/timeline";
import { type PlaybackSpeed, useEditorStore } from "../../lib/state/editor-store";
import { StoryTrackStrip } from "../charts/StoryTrackStrip";
import { TelemetryChart } from "../charts/TelemetryChart";
import { type CanvasGeometryStatus, TripStageCanvas } from "../stage/TripStageCanvas";

const PLAYBACK_SPEEDS: readonly PlaybackSpeed[] = [0.5, 1, 2, 4];

export interface EditorPlaybackDockProps {
  timelineStages: readonly TimelineStage[];
  spec: RenderSpecV2;
  matchedPhotos: readonly MatchedPhoto[];
  climbs: readonly ClimbHighlightOut[];
  chartPoints: readonly TelemetryChartPoint[];
  chartTotalDistanceM?: number;
  kpis?: ProjectKpisOut;
  selectedStageId: string | null;
  pinModeActive: boolean;
  previewWatermark: boolean;
  effectiveDurationS: number;
  geometryStatus: CanvasGeometryStatus;
  projectTitle: string;
  onMapPinClick: (lngLat: [number, number]) => void;
  onCancelPinMode: () => void;
  onSelectStage: (stageId: string | null) => void;
  onUpdateKeyframes: (keyframes: CameraKeyframe[]) => void;
  onTrimBrush?: (startM: number, endM: number) => void;
}

export const EditorPlaybackDock = memo(function EditorPlaybackDock({
  timelineStages,
  spec,
  matchedPhotos,
  climbs,
  chartPoints,
  chartTotalDistanceM,
  kpis,
  selectedStageId,
  pinModeActive,
  previewWatermark,
  effectiveDurationS,
  geometryStatus,
  projectTitle,
  onMapPinClick,
  onCancelPinMode,
  onSelectStage,
  onUpdateKeyframes,
  onTrimBrush,
}: EditorPlaybackDockProps) {
  const currentTimeRatio = useEditorStore((s) => s.currentTimeRatio);
  const isPlaying = useEditorStore((s) => s.isPlaying);
  const playbackSpeed = useEditorStore((s) => s.playbackSpeed);
  const setCurrentTimeRatio = useEditorStore((s) => s.setCurrentTimeRatio);
  const setPlaying = useEditorStore((s) => s.setPlaying);
  const togglePlaying = useEditorStore((s) => s.togglePlaying);
  const setPlaybackSpeed = useEditorStore((s) => s.setPlaybackSpeed);
  const resetPlayback = useEditorStore((s) => s.resetPlayback);

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

  const totalStats = useMemo(
    () =>
      kpis
        ? {
            distanceM: kpis.total_distance_m,
            elevationGainM: kpis.total_elevation_gain_m,
            movingSeconds: kpis.total_moving_seconds,
            stageCount: kpis.stage_count,
          }
        : undefined,
    [kpis],
  );

  const handleSeek = useCallback(
    (ratio: number) => {
      setPlaying(false);
      setCurrentTimeRatio(
        progressRatioToTimeRatio(
          matchedPhotos,
          spec,
          ratio,
          effectiveDurationS,
          previewWatermark,
          timelineStages,
        ),
      );
    },
    [
      setPlaying,
      setCurrentTimeRatio,
      matchedPhotos,
      spec,
      effectiveDurationS,
      previewWatermark,
      timelineStages,
    ],
  );

  return (
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
          climbs={climbs}
          pinModeActive={pinModeActive}
          onMapPinClick={onMapPinClick}
          onCancelPinMode={onCancelPinMode}
          projectTitle={projectTitle}
          totalStats={totalStats}
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
                handleSeek(targetProgress);
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
              {(currentTimeRatio * effectiveDurationS).toFixed(1)}s / {effectiveDurationS}s
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
          totalDistanceM={chartTotalDistanceM ?? kpis?.total_distance_m ?? 0}
          progressRatio={schedule.progressRatio}
          cameraKeyframes={spec.camera_keyframes}
          matchedPhotos={matchedPhotos}
          selectedStageId={selectedStageId}
          cameraMode={spec.camera_mode}
          cameraPreset={spec.camera_preset}
          cameraPitchDeg={spec.camera_pitch_deg}
          onSelectStage={onSelectStage}
          onUpdateKeyframes={onUpdateKeyframes}
          onSeek={handleSeek}
        />

        <TelemetryChart
          points={chartPoints}
          totalDistanceM={chartTotalDistanceM ?? 0}
          units={spec.units}
          progressRatio={schedule.progressRatio}
          smoothingWindow={spec.telemetry_smoothing}
          stages={timelineStages}
          climbs={climbs}
          onTrimBrush={onTrimBrush}
          onSeek={handleSeek}
        />
      </div>
    </section>
  );
});
