import { useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { TripStageCanvas, type TripStageCanvasHandle } from "../components/stage/TripStageCanvas";
import {
  createDefaultRenderSpecV2,
  normalizeRenderSpecV2,
  type RenderManifest,
} from "../lib/api/types";
import { parseResolutionPreset } from "../lib/geo/format";
import { evaluateTimelineSchedule, matchPhotosToTimeline } from "../lib/geo/photos";
import { buildTimelineFromManifest } from "../lib/geo/timeline";

function createFallbackManifest(jobId: string): RenderManifest {
  const spec = createDefaultRenderSpecV2();
  const { width, height } = parseResolutionPreset(spec.resolution);
  return {
    job_id: jobId,
    project_id: "sample-project",
    project_title: "Sample Render",
    spec,
    width,
    height,
    fps: spec.fps,
    duration_s: spec.duration_seconds,
    total_frames: Math.round(spec.duration_seconds * spec.fps),
    is_preview: spec.is_preview,
    stages: [
      {
        id: "sample-stage-1",
        name: "Synthetic Sample Stage",
        day_index: 1,
        stage_order: 0,
        color: spec.track_color,
        transition: "bed",
        distance_m: 12_400,
        elevation_gain_m: 620,
        moving_seconds: 3200,
        elapsed_seconds: 3500,
        has_timestamps: true,
        points: [
          {
            lat: 0.1,
            lon: 0.2,
            lon_unwrapped: 0.2,
            elev_m: 1450,
            cum_dist_m: 0,
            speed_mps: 3.8,
            grade_pct: 2.1,
            elapsed_s: 0,
            ts_utc: "2000-01-01T08:00:00Z",
            segment_idx: 0,
          },
          {
            lat: 0.115,
            lon: 0.225,
            lon_unwrapped: 0.225,
            elev_m: 1790,
            cum_dist_m: 6200,
            speed_mps: 3.1,
            grade_pct: 5.5,
            elapsed_s: 1750,
            ts_utc: "2000-01-01T08:29:10Z",
            segment_idx: 0,
          },
          {
            lat: 0.13,
            lon: 0.25,
            lon_unwrapped: 0.25,
            elev_m: 2070,
            cum_dist_m: 12400,
            speed_mps: 4.4,
            grade_pct: 4.5,
            elapsed_s: 3500,
            ts_utc: "2000-01-01T08:58:20Z",
            segment_idx: 0,
          },
        ],
      },
    ],
  };
}

export function RenderHarnessPage() {
  const { jobId } = useParams({ from: "/render/$jobId" });
  const canvasControllerRef = useRef<TripStageCanvasHandle | null>(null);
  const [frameIndex, setFrameIndex] = useState(0);
  const currentFrameRef = useRef(0);

  const manifest = useMemo<RenderManifest>(() => {
    const injected = typeof window !== "undefined" ? window.__TRIP_RENDER_MANIFEST__ : undefined;
    if (injected) {
      return {
        ...injected,
        spec: normalizeRenderSpecV2(injected.spec),
      };
    }
    return createFallbackManifest(jobId);
  }, [jobId]);

  const totalFrames = Math.max(1, manifest.total_frames);
  const timelineStages = useMemo(() => buildTimelineFromManifest(manifest), [manifest]);
  const matchedPhotos = useMemo(
    () => matchPhotosToTimeline(manifest.photos ?? [], timelineStages, manifest.spec),
    [manifest.photos, timelineStages, manifest.spec],
  );

  const currentTimeS = useMemo(() => {
    if (totalFrames <= 1 || manifest.duration_s <= 0) {
      return 0;
    }
    const clampedFrame = Math.min(Math.max(0, frameIndex), totalFrames - 1);
    return (clampedFrame / (totalFrames - 1)) * manifest.duration_s;
  }, [frameIndex, totalFrames, manifest.duration_s]);

  const schedule = useMemo(
    () =>
      evaluateTimelineSchedule(
        timelineStages,
        matchedPhotos,
        manifest.spec,
        currentTimeS,
        manifest.duration_s,
        manifest.is_preview,
      ),
    [
      timelineStages,
      matchedPhotos,
      manifest.spec,
      currentTimeS,
      manifest.duration_s,
      manifest.is_preview,
    ],
  );

  useEffect(() => {
    const prevMargin = document.body.style.margin;
    const prevOverflow = document.body.style.overflow;
    document.body.style.margin = "0";
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.margin = prevMargin;
      document.body.style.overflow = prevOverflow;
    };
  }, []);

  const hasStageCameraOverrides =
    Object.keys(manifest.spec.stage_camera_overrides ?? {}).length > 0;

  // Register deterministic window.__TRIP_RENDERER__ contract
  useEffect(() => {
    const readyPromise = (async () => {
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      if (canvasControllerRef.current) {
        await canvasControllerRef.current.waitForReady();
      }
    })();

    window.__TRIP_RENDERER__ = {
      ready: readyPromise,
      getFrameCount: () => totalFrames,
      setFrame: async (nextIndex: number) => {
        const clamped = Math.min(Math.max(0, Math.round(nextIndex)), totalFrames - 1);
        const changed = clamped !== currentFrameRef.current;
        if (changed) {
          currentFrameRef.current = clamped;
          flushSync(() => {
            setFrameIndex(clamped);
          });
        }
        const cameraChanged =
          changed &&
          (manifest.spec.camera_mode === "follow-marker" ||
            manifest.spec.camera_preset !== "flat" ||
            (manifest.spec.camera_keyframes?.length ?? 0) > 0 ||
            hasStageCameraOverrides);
        if (canvasControllerRef.current) {
          await canvasControllerRef.current.waitForFrameSettle(cameraChanged);
        }
      },
    };

    return () => {
      delete window.__TRIP_RENDERER__;
    };
  }, [
    totalFrames,
    manifest.spec.camera_mode,
    manifest.spec.camera_preset,
    manifest.spec.camera_keyframes?.length,
    hasStageCameraOverrides,
  ]);

  const totalStats = useMemo(
    () => ({
      distanceM: manifest.stages.reduce((acc, s) => acc + s.distance_m, 0),
      elevationGainM: manifest.stages.reduce((acc, s) => acc + s.elevation_gain_m, 0),
      movingSeconds: manifest.stages.reduce((acc, s) => acc + s.moving_seconds, 0),
      stageCount: manifest.stages.length,
    }),
    [manifest.stages],
  );

  return (
    <main
      data-testid="render-harness-root"
      data-job-id={jobId}
      data-harness-ready="true"
      data-frame-index={frameIndex}
      data-total-frames={totalFrames}
      className="fixed inset-0 flex items-center justify-center overflow-hidden bg-black p-0 m-0"
    >
      <TripStageCanvas
        stages={timelineStages}
        spec={manifest.spec}
        progressRatio={schedule.progressRatio}
        durationS={manifest.duration_s}
        travelDurationS={schedule.travelDurationS}
        isPreview={manifest.is_preview}
        renderMode="deterministic"
        exactSize={{ width: manifest.width, height: manifest.height }}
        jobId={jobId}
        controllerRef={canvasControllerRef}
        matchedPhotos={matchedPhotos}
        activePhoto={schedule.activePhoto}
        climbs={manifest.climbs}
        projectTitle={manifest.project_title}
        totalStats={totalStats}
      />
    </main>
  );
}
