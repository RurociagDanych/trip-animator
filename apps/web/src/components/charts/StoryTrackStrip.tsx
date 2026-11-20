import { Camera, Film, Plus, Trash2 } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import type { CameraKeyframe, CameraMode, CameraPreset } from "../../lib/api/types";
import type { MatchedPhoto } from "../../lib/geo/photos";
import type { TimelineStage } from "../../lib/geo/timeline";

export interface StoryTrackStripProps {
  stages: readonly TimelineStage[];
  totalDistanceM: number;
  progressRatio: number;
  cameraKeyframes: readonly CameraKeyframe[];
  matchedPhotos?: readonly MatchedPhoto[];
  selectedStageId?: string | null;
  cameraMode?: CameraMode;
  cameraPreset?: CameraPreset;
  cameraPitchDeg?: number;
  onSeek: (ratio: number) => void;
  onSelectStage?: (stageId: string) => void;
  onUpdateKeyframes?: (keyframes: CameraKeyframe[]) => void;
}

function clamp01(val: number): number {
  if (!Number.isFinite(val)) return 0;
  return Math.min(1, Math.max(0, val));
}

export function StoryTrackStrip({
  stages,
  totalDistanceM,
  progressRatio,
  cameraKeyframes,
  matchedPhotos = [],
  selectedStageId = null,
  cameraMode = "fit-track",
  cameraPreset = "flat",
  cameraPitchDeg = 0,
  onSeek,
  onSelectStage,
  onUpdateKeyframes,
}: StoryTrackStripProps) {
  const laneRef = useRef<HTMLDivElement | null>(null);
  const [draggingIdx, setDraggingIdx] = useState<number | null>(null);
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);

  const clamped = clamp01(progressRatio);

  const stageBlocks = useMemo(() => {
    if (stages.length === 0) return [];
    const sumStageDist = stages.reduce((acc, s) => acc + Math.max(0, s.distanceM), 0);
    const denom = Math.max(totalDistanceM, sumStageDist, 0);
    let cursor = 0;
    return stages.map((stage) => {
      const share = denom > 0 ? Math.max(0, stage.distanceM) / denom : 1 / stages.length;
      const start = cursor;
      const width = share;
      cursor += share;
      return { stage, start, width };
    });
  }, [stages, totalDistanceM]);

  const activeCue =
    selectedIdx !== null && selectedIdx >= 0 && selectedIdx < cameraKeyframes.length
      ? cameraKeyframes[selectedIdx]!
      : null;

  function ratioFromClientX(clientX: number): number {
    const el = laneRef.current;
    if (!el) return clamped;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return clamped;
    return clamp01((clientX - rect.left) / rect.width);
  }

  function handleAddCueAtPlayhead() {
    if (!onUpdateKeyframes || cameraKeyframes.length >= 40) return;
    const p = Math.round(clamped * 1000) / 1000;
    const nextCue: CameraKeyframe = {
      progress: p,
      camera_mode: cameraMode,
      camera_preset: cameraPreset,
      pitch_deg: cameraPitchDeg,
      bearing_offset_deg: 0,
      zoom_offset: 0,
      speed_multiplier: 1,
      label: `Cue ${cameraKeyframes.length + 1}`,
    };
    const nextList = [...cameraKeyframes, nextCue].sort((a, b) => a.progress - b.progress);
    onUpdateKeyframes(nextList);
    const idx = nextList.findIndex((k) => k === nextCue);
    setSelectedIdx(idx >= 0 ? idx : null);
  }

  function handleCueDrag(idx: number, clientX: number) {
    if (!onUpdateKeyframes) return;
    const nextProgress = Math.round(ratioFromClientX(clientX) * 1000) / 1000;
    const next = cameraKeyframes.map((kf, i) =>
      i === idx ? { ...kf, progress: nextProgress } : kf,
    );
    onUpdateKeyframes(next);
  }

  function handleRemoveCue(idx: number) {
    if (!onUpdateKeyframes) return;
    onUpdateKeyframes(cameraKeyframes.filter((_, i) => i !== idx));
    setSelectedIdx(null);
  }

  return (
    <div data-testid="story-track-strip" className="space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-ink-muted">
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center gap-1 font-semibold text-ink">
            <Film className="h-3 w-3 text-amber-accent" aria-hidden="true" />
            <span>Story track</span>
          </span>
          <span className="tabular-telemetry rounded bg-elevated px-1.5 py-0.5 text-[9px]">
            {cameraKeyframes.length} cue{cameraKeyframes.length === 1 ? "" : "s"}
          </span>
          {activeCue && selectedIdx !== null && onUpdateKeyframes ? (
            <div className="inline-flex items-center gap-1.5 rounded border border-amber-accent/40 bg-amber-accent/10 px-1.5 py-0.5 text-[10px] text-ink">
              <span className="font-medium text-amber-accent">
                {activeCue.label || `Cue ${selectedIdx + 1}`}
              </span>
              <select
                aria-label="Selected cue camera preset"
                value={activeCue.camera_preset}
                onChange={(e) => {
                  const preset = e.currentTarget.value as CameraPreset;
                  onUpdateKeyframes(
                    cameraKeyframes.map((kf, i) =>
                      i === selectedIdx ? { ...kf, camera_preset: preset } : kf,
                    ),
                  );
                }}
                className="rounded border border-border bg-elevated px-1 py-0 text-[10px] text-ink"
              >
                <option value="flat">Flat</option>
                <option value="dynamic-3d">3D</option>
                <option value="drone-orbit">Orbit</option>
              </select>
              <button
                type="button"
                aria-label="Delete selected cue"
                onClick={() => handleRemoveCue(selectedIdx)}
                className="rounded p-0.5 text-coral-accent hover:bg-coral-accent/15"
              >
                <Trash2 className="h-2.5 w-2.5" aria-hidden="true" />
              </button>
            </div>
          ) : null}
        </div>

        {onUpdateKeyframes ? (
          <button
            type="button"
            onClick={handleAddCueAtPlayhead}
            disabled={cameraKeyframes.length >= 40}
            className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] font-medium text-ink hover:border-amber-accent/60 hover:text-amber-accent disabled:opacity-40"
          >
            <Plus className="h-2.5 w-2.5" aria-hidden="true" />
            <span>Cue at {Math.round(clamped * 100)}%</span>
          </button>
        ) : null}
      </div>

      <div
        ref={laneRef}
        role="slider"
        aria-label="Story track timeline"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(clamped * 100)}
        tabIndex={0}
        onClick={(e) => {
          if (draggingIdx === null) {
            onSeek(ratioFromClientX(e.clientX));
          }
        }}
        className="relative h-7 w-full cursor-pointer overflow-hidden rounded border border-border bg-canvas/85 select-none"
      >
        {stageBlocks.map(({ stage, start, width }) => {
          const isSelected = selectedStageId === stage.id;
          return (
            <button
              key={stage.id}
              type="button"
              title={`Day ${stage.dayIndex}: ${stage.name}`}
              onClick={(e) => {
                e.stopPropagation();
                onSelectStage?.(stage.id);
                onSeek(start);
              }}
              style={{
                left: `${start * 100}%`,
                width: `${Math.max(width * 100, 1.5)}%`,
                borderColor: stage.color,
                backgroundColor: isSelected ? `${stage.color}38` : `${stage.color}1e`,
              }}
              className="absolute top-0.5 bottom-0.5 flex items-center overflow-hidden rounded-sm border-l-2 px-1 text-left text-[9px] font-medium text-ink/90 hover:brightness-125"
            >
              <span className="truncate">
                D{stage.dayIndex} {stage.name}
              </span>
            </button>
          );
        })}

        {matchedPhotos.map((photo) => {
          if (photo.matchedProgress === null) return null;
          return (
            <button
              key={photo.id}
              type="button"
              title={photo.caption ?? "Photo stop"}
              aria-label={`Seek to photo ${photo.caption ?? photo.id}`}
              onClick={(e) => {
                e.stopPropagation();
                onSeek(photo.matchedProgress!);
              }}
              style={{ left: `${clamp01(photo.matchedProgress) * 100}%` }}
              className="absolute bottom-0.5 z-10 flex h-3.5 w-3.5 -translate-x-1/2 items-center justify-center rounded-full border border-sky-accent bg-surface text-sky-accent hover:scale-110"
            >
              <Camera className="h-2 w-2" aria-hidden="true" />
            </button>
          );
        })}

        {cameraKeyframes.map((kf, idx) => {
          const isSelected = selectedIdx === idx;
          return (
            <button
              key={`${idx}-${kf.progress}`}
              type="button"
              data-testid={`camera-cue-${idx}`}
              aria-label={kf.label || `Camera cue ${idx + 1}`}
              title={`${kf.label || `Cue ${idx + 1}`} (${kf.camera_preset}, ${Math.round(kf.progress * 100)}%)`}
              onClick={(e) => {
                e.stopPropagation();
                setSelectedIdx(idx);
                onSeek(kf.progress);
              }}
              onPointerDown={(e) => {
                e.stopPropagation();
                setSelectedIdx(idx);
                if (onUpdateKeyframes) {
                  setDraggingIdx(idx);
                  e.currentTarget.setPointerCapture?.(e.pointerId);
                }
              }}
              onPointerMove={(e) => {
                if (draggingIdx === idx) {
                  handleCueDrag(idx, e.clientX);
                }
              }}
              onPointerUp={(e) => {
                if (draggingIdx === idx) {
                  setDraggingIdx(null);
                  if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
                    e.currentTarget.releasePointerCapture?.(e.pointerId);
                  }
                }
              }}
              style={{ left: `${clamp01(kf.progress) * 100}%` }}
              className={`absolute top-0.5 z-20 flex h-4 w-4 -translate-x-1/2 cursor-ew-resize items-center justify-center rounded-sm border text-[9px] font-bold transition-transform ${
                isSelected
                  ? "scale-110 border-white bg-amber-accent text-canvas shadow"
                  : "border-amber-accent bg-canvas/95 text-amber-accent hover:scale-110"
              }`}
            >
              ◆
            </button>
          );
        })}

        <div
          className="pointer-events-none absolute top-0 bottom-0 z-30 w-0.5 bg-amber-accent"
          style={{ left: `${clamped * 100}%` }}
        />
      </div>
    </div>
  );
}
