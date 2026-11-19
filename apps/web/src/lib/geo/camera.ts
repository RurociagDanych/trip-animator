import type {
  CameraKeyframe,
  CameraMode,
  CameraPreset,
  RenderSpecV2,
  StageCameraOverride,
  StageFocusMode,
} from "../api/types";
import type { EvaluatedTimelineState, TimelineStage } from "./timeline";

export interface CameraFrame {
  pitch: number;
  bearing: number;
  zoomOffset?: number;
  cameraMode?: CameraMode;
  cameraPreset?: CameraPreset;
  speedMultiplier?: number;
  label?: string;
}

export interface InterpolatedCameraCue {
  cameraMode: CameraMode;
  cameraPreset: CameraPreset;
  pitchDeg: number;
  bearingOffsetDeg: number;
  zoomOffset: number;
  speedMultiplier: number;
  activeLabel: string;
}

export interface ComputeCameraFrameOptions {
  courseBearingDeg?: number | null;
  velocityMps?: number;
}

export function normalizeBearingDeg(deg: number): number {
  if (!Number.isFinite(deg)) {
    return 0;
  }
  return ((deg % 360) + 360) % 360;
}

/**
 * Computes a weighted circular mean of bearings in degrees [0, 360),
 * avoiding 0/360 wrap-around discontinuities (e.g. averaging 355° and 5° -> 0°).
 */
export function circularMeanBearingDeg(
  samples: readonly { deg: number | null; weight?: number }[],
): number | null {
  let sumSin = 0;
  let sumCos = 0;
  let totalWeight = 0;

  for (const sample of samples) {
    if (sample.deg === null || !Number.isFinite(sample.deg)) {
      continue;
    }
    const w = sample.weight ?? 1;
    if (!(w > 0) || !Number.isFinite(w)) {
      continue;
    }
    const rad = (normalizeBearingDeg(sample.deg) * Math.PI) / 180;
    sumSin += Math.sin(rad) * w;
    sumCos += Math.cos(rad) * w;
    totalWeight += w;
  }

  if (totalWeight <= 0 || (Math.abs(sumSin) < 1e-12 && Math.abs(sumCos) < 1e-12)) {
    return null;
  }
  return normalizeBearingDeg((Math.atan2(sumSin, sumCos) * 180) / Math.PI);
}

/**
 * Computes initial geodesic bearing in degrees [0, 360) from `from` ([lon, lat])
 * to `to` ([lon, lat]). Returns null when points are effectively coincident.
 */
export function computeBearingDeg(
  from: readonly [number, number],
  to: readonly [number, number],
): number | null {
  const [lon1Deg, lat1Deg] = from;
  const [lon2Deg, lat2Deg] = to;
  if (Math.abs(lon2Deg - lon1Deg) < 1e-7 && Math.abs(lat2Deg - lat1Deg) < 1e-7) {
    return null;
  }
  const rad = Math.PI / 180;
  const lat1 = lat1Deg * rad;
  const lat2 = lat2Deg * rad;
  const dLon = (lon2Deg - lon1Deg) * rad;
  const y = Math.sin(dLon) * Math.cos(lat2);
  const x =
    Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  const theta = Math.atan2(y, x) * (180 / Math.PI);
  return normalizeBearingDeg(theta);
}

const MIN_SPEED_MULTIPLIER = 0.25;
const MIN_STAGE_SPEED_MULTIPLIER = 0.2;
const MAX_SPEED_MULTIPLIER = 4.0;

export function smoothstep01(t: number): number {
  const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  return x * x * (3 - 2 * x);
}

function clampSpeedMultiplier(value: number): number {
  if (!Number.isFinite(value)) {
    return 1.0;
  }
  return Math.min(MAX_SPEED_MULTIPLIER, Math.max(MIN_SPEED_MULTIPLIER, value));
}

function clampStageSpeedMultiplier(value: number): number {
  if (!Number.isFinite(value)) {
    return 1.0;
  }
  return Math.min(MAX_SPEED_MULTIPLIER, Math.max(MIN_STAGE_SPEED_MULTIPLIER, value));
}

export interface StageProgressSpan {
  stageId: string;
  stageIndex: number;
  startProgress: number;
  endProgress: number;
}

export interface ResolvedStageCameraOverride {
  focusMode: StageFocusMode;
  targetZoom: number | null;
  speedMultiplier: number;
  transitionMargin: number;
}

export interface EvaluatedStageFocus {
  activeOverrideStageId: string | null;
  focusWeight: number;
  targetZoom: number | null;
  speedMultiplier: number;
}

export function resolveStageCameraOverride(
  override: StageCameraOverride | null | undefined,
): ResolvedStageCameraOverride {
  if (!override || override.focus_mode === "auto") {
    return {
      focusMode: "auto",
      targetZoom: null,
      speedMultiplier: 1.0,
      transitionMargin: 0.02,
    };
  }
  const focusMode: StageFocusMode =
    override.focus_mode === "street" ||
    override.focus_mode === "close" ||
    override.focus_mode === "custom"
      ? override.focus_mode
      : "auto";
  const transitionMargin = Number.isFinite(override.transition_margin)
    ? Math.min(0.25, Math.max(0.005, override.transition_margin))
    : 0.02;
  let targetZoom: number | null =
    typeof override.target_zoom === "number" && Number.isFinite(override.target_zoom)
      ? Math.min(18.0, Math.max(3.0, override.target_zoom))
      : null;
  let speedMultiplier = clampStageSpeedMultiplier(override.speed_multiplier);

  if (focusMode === "street") {
    if (targetZoom === null) {
      targetZoom = 15.8;
    }
    if (speedMultiplier === 1.0) {
      speedMultiplier = 0.45;
    }
  } else if (focusMode === "close") {
    if (targetZoom === null) {
      targetZoom = 13.8;
    }
    if (speedMultiplier === 1.0) {
      speedMultiplier = 0.7;
    }
  }

  return {
    focusMode,
    targetZoom,
    speedMultiplier,
    transitionMargin,
  };
}

/**
 * Partitions route progress [0, 1] across stages matching evaluateTimeline's metric
 * (globalElapsedS when timeline_mode is "recorded-time" and all non-empty stages have timestamps,
 * else globalDistM, falling back to distanceM when points are empty).
 */
export function computeStageProgressSpans(
  stages: readonly TimelineStage[],
  spec: Pick<RenderSpecV2, "timeline_mode">,
): StageProgressSpan[] {
  if (!stages || stages.length === 0) {
    return [];
  }

  const nonEmptyEntries: { stage: TimelineStage; stageIndex: number }[] = [];
  let maxGlobalElapsed = 0;
  for (let i = 0; i < stages.length; i++) {
    const stage = stages[i]!;
    if (stage.points.length > 0) {
      nonEmptyEntries.push({ stage, stageIndex: i });
      for (const pt of stage.points) {
        if (pt.globalElapsedS > maxGlobalElapsed) {
          maxGlobalElapsed = pt.globalElapsedS;
        }
      }
    }
  }

  if (nonEmptyEntries.length > 0) {
    const allHaveTimestamps = nonEmptyEntries.every((e) => e.stage.hasTimestamps);
    const useTimeMetric =
      spec.timeline_mode === "recorded-time" && allHaveTimestamps && maxGlobalElapsed > 0;
    const firstPt = nonEmptyEntries[0]!.stage.points[0]!;
    const lastStagePts = nonEmptyEntries[nonEmptyEntries.length - 1]!.stage.points;
    const lastPt = lastStagePts[lastStagePts.length - 1]!;
    const minMetric = useTimeMetric ? firstPt.globalElapsedS : firstPt.globalDistM;
    const maxMetric = useTimeMetric ? lastPt.globalElapsedS : lastPt.globalDistM;
    const totalSpan = maxMetric - minMetric;

    if (totalSpan > 1e-9 && Number.isFinite(totalSpan)) {
      const spans: StageProgressSpan[] = [];
      for (let i = 0; i < nonEmptyEntries.length; i++) {
        const entry = nonEmptyEntries[i]!;
        const pt0 = entry.stage.points[0]!;
        const startMetric = useTimeMetric ? pt0.globalElapsedS : pt0.globalDistM;
        const rawStart = i === 0 ? 0 : Math.min(1, Math.max(0, (startMetric - minMetric) / totalSpan));
        const startProgress =
          i > 0 ? Math.max(spans[i - 1]!.startProgress, rawStart) : 0;
        if (i > 0) {
          spans[i - 1]!.endProgress = startProgress;
        }
        spans.push({
          stageId: entry.stage.id,
          stageIndex: entry.stageIndex,
          startProgress,
          endProgress: 1,
        });
      }
      if (spans.length > 0) {
        spans[spans.length - 1]!.endProgress = 1;
      }
      return spans;
    }
  }

  // Fallback to stage.distanceM (or equal slices when total distance is 0)
  const weights = stages.map((s) =>
    Number.isFinite(s.distanceM) && s.distanceM > 0 ? s.distanceM : 0,
  );
  const totalWeight = weights.reduce((acc, w) => acc + w, 0);
  const spans: StageProgressSpan[] = [];
  let cum = 0;
  for (let i = 0; i < stages.length; i++) {
    const startProgress = totalWeight > 0 ? cum / totalWeight : i / stages.length;
    cum += weights[i]!;
    const endProgress =
      i === stages.length - 1
        ? 1
        : totalWeight > 0
          ? Math.min(1, Math.max(startProgress, cum / totalWeight))
          : (i + 1) / stages.length;
    spans.push({
      stageId: stages[i]!.id,
      stageIndex: i,
      startProgress,
      endProgress,
    });
  }
  return spans;
}

/**
 * Evaluates C1-smooth per-stage camera focus weight, target zoom, and speed multiplier
 * across stage boundaries using a smoothstep partition of unity.
 */
export function evaluateStageCameraFocus(
  stages: readonly TimelineStage[],
  spec: Pick<RenderSpecV2, "timeline_mode" | "stage_camera_overrides">,
  progress: number,
): EvaluatedStageFocus {
  const overrides = spec.stage_camera_overrides;
  if (!stages || stages.length === 0 || !overrides || Object.keys(overrides).length === 0) {
    return {
      activeOverrideStageId: null,
      focusWeight: 0,
      targetZoom: null,
      speedMultiplier: 1.0,
    };
  }

  const spans = computeStageProgressSpans(stages, spec);
  const n = spans.length;
  if (n === 0) {
    return {
      activeOverrideStageId: null,
      focusWeight: 0,
      targetZoom: null,
      speedMultiplier: 1.0,
    };
  }

  const resolved = spans.map((span) => resolveStageCameraOverride(overrides[span.stageId]));
  const hasAnyOverride = resolved.some(
    (r) => r.focusMode !== "auto" && (r.targetZoom !== null || r.speedMultiplier !== 1.0),
  );
  if (!hasAnyOverride) {
    return {
      activeOverrideStageId: null,
      focusWeight: 0,
      targetZoom: null,
      speedMultiplier: 1.0,
    };
  }

  const p = Math.min(1, Math.max(0, Number.isFinite(progress) ? progress : 0));

  // Build monotone smoothstep step functions H_0(p)=1 ... H_n(p)=0 across internal boundaries b_k
  const H = new Float64Array(n + 1);
  H[0] = 1;
  H[n] = 0;
  for (let k = 1; k < n; k++) {
    const prevSpan = spans[k - 1]!;
    const currSpan = spans[k]!;
    const bk = currSpan.startProgress;
    const lenPrev = Math.max(0, prevSpan.endProgress - prevSpan.startProgress);
    const lenCurr = Math.max(0, currSpan.endProgress - currSpan.startProgress);
    const maxMargin = Math.max(
      resolved[k - 1]!.transitionMargin,
      resolved[k]!.transitionMargin,
      0.02,
    );
    const deltaK = Math.min(maxMargin, 0.35 * lenPrev, 0.35 * lenCurr);
    if (deltaK > 1e-9) {
      H[k] = smoothstep01((p - (bk - deltaK)) / (2 * deltaK));
    } else {
      H[k] = p >= bk ? 1 : 0;
    }
  }

  let speedSum = 0;
  let focusWeight = 0;
  let weightedZoomSum = 0;
  let activeOverrideStageId: string | null = null;
  let bestOverrideWeight = 1e-6;

  for (let i = 0; i < n; i++) {
    const wi = Math.max(0, H[i]! - H[i + 1]!);
    const ov = resolved[i]!;
    speedSum += wi * ov.speedMultiplier;
    if (ov.targetZoom !== null) {
      focusWeight += wi;
      weightedZoomSum += wi * ov.targetZoom;
    }
    if (ov.focusMode !== "auto" && wi > bestOverrideWeight) {
      bestOverrideWeight = wi;
      activeOverrideStageId = spans[i]!.stageId;
    }
  }

  const clampedFocusWeight = Math.min(1, Math.max(0, focusWeight));
  const targetZoom =
    clampedFocusWeight > 1e-6
      ? Math.min(18.0, Math.max(3.0, weightedZoomSum / clampedFocusWeight))
      : null;

  return {
    activeOverrideStageId,
    focusWeight: clampedFocusWeight,
    targetZoom,
    speedMultiplier: clampStageSpeedMultiplier(speedSum),
  };
}

/**
 * Multiplies keyframe speed multiplier with smooth per-stage speed multiplier, clamped to [0.2, 4.0].
 */
export function sampleCombinedSpeedMultiplier(
  stages: readonly TimelineStage[],
  spec: Pick<RenderSpecV2, "camera_keyframes" | "timeline_mode" | "stage_camera_overrides">,
  progress: number,
): number {
  const kfSpeed = sampleKeyframeSpeedMultiplier(spec.camera_keyframes ?? [], progress);
  const stageSpeed = evaluateStageCameraFocus(stages, spec, progress).speedMultiplier;
  return clampStageSpeedMultiplier(kfSpeed * stageSpeed);
}

function sortKeyframes(keyframes: readonly CameraKeyframe[]): readonly CameraKeyframe[] {
  let isSorted = true;
  for (let i = 1; i < keyframes.length; i++) {
    const prev = Number.isFinite(keyframes[i - 1]!.progress) ? keyframes[i - 1]!.progress : 0;
    const curr = Number.isFinite(keyframes[i]!.progress) ? keyframes[i]!.progress : 0;
    if (prev > curr) {
      isSorted = false;
      break;
    }
  }
  if (isSorted) {
    return keyframes;
  }
  return [...keyframes].sort(
    (a, b) =>
      (Number.isFinite(a.progress) ? a.progress : 0) -
      (Number.isFinite(b.progress) ? b.progress : 0),
  );
}

function resolveKeyframeSegment(
  sorted: readonly CameraKeyframe[],
  progress: number,
): { a: CameraKeyframe; b: CameraKeyframe; u: number } {
  const p = Math.min(1, Math.max(0, Number.isFinite(progress) ? progress : 0));

  if (sorted.length === 1 || p <= sorted[0]!.progress) {
    const first = sorted[0]!;
    return { a: first, b: first, u: 0 };
  }

  const last = sorted[sorted.length - 1]!;
  if (p >= last.progress) {
    return { a: last, b: last, u: 0 };
  }

  let idx = 0;
  for (let i = 0; i < sorted.length - 1; i++) {
    if (p >= sorted[i]!.progress && p <= sorted[i + 1]!.progress) {
      idx = i;
      break;
    }
  }
  const a = sorted[idx]!;
  const b = sorted[idx + 1]!;
  const span = b.progress - a.progress;
  const u = span > 1e-9 ? smoothstep01((p - a.progress) / span) : 0;
  return { a, b, u };
}

/**
 * Smoothstep-samples the keyframe speed multiplier in [0.25, 4.0] at route `progress` in [0, 1].
 * Returns 1.0 when `keyframes` is empty.
 */
export function sampleKeyframeSpeedMultiplier(
  keyframes: readonly CameraKeyframe[],
  progress: number,
): number {
  if (!keyframes || keyframes.length === 0) {
    return 1.0;
  }
  const sorted = sortKeyframes(keyframes);
  const { a, b, u } = resolveKeyframeSegment(sorted, progress);
  const sa = clampSpeedMultiplier(a.speed_multiplier);
  const sb = clampSpeedMultiplier(b.speed_multiplier);
  return clampSpeedMultiplier(sa + (sb - sa) * u);
}

/**
 * Smoothstep-interpolates storyboard camera keyframes at `progress` in [0, 1].
 * Falls back to `baseSpec` camera settings when `keyframes` is empty.
 */
export function interpolateCameraKeyframes(
  keyframes: readonly CameraKeyframe[],
  progress: number,
  baseSpec: RenderSpecV2,
): InterpolatedCameraCue {
  if (!keyframes || keyframes.length === 0) {
    return {
      cameraMode: baseSpec.camera_mode,
      cameraPreset: baseSpec.camera_preset,
      pitchDeg: Number.isFinite(baseSpec.camera_pitch_deg) ? baseSpec.camera_pitch_deg : 0,
      bearingOffsetDeg: Number.isFinite(baseSpec.camera_bearing_deg) ? baseSpec.camera_bearing_deg : 0,
      zoomOffset: 0,
      speedMultiplier: 1.0,
      activeLabel: "",
    };
  }

  const sorted = sortKeyframes(keyframes);
  const { a, b, u } = resolveKeyframeSegment(sorted, progress);

  if (a === b) {
    return {
      cameraMode: a.camera_mode,
      cameraPreset: a.camera_preset,
      pitchDeg: Math.min(60, Math.max(0, Number.isFinite(a.pitch_deg) ? a.pitch_deg : 0)),
      bearingOffsetDeg: Number.isFinite(a.bearing_offset_deg) ? a.bearing_offset_deg : 0,
      zoomOffset: Math.min(3, Math.max(-3, Number.isFinite(a.zoom_offset) ? a.zoom_offset : 0)),
      speedMultiplier: clampSpeedMultiplier(a.speed_multiplier),
      activeLabel: a.label,
    };
  }

  const aPitch = Number.isFinite(a.pitch_deg) ? a.pitch_deg : 0;
  const bPitch = Number.isFinite(b.pitch_deg) ? b.pitch_deg : 0;
  const aBearing = Number.isFinite(a.bearing_offset_deg) ? a.bearing_offset_deg : 0;
  const bBearing = Number.isFinite(b.bearing_offset_deg) ? b.bearing_offset_deg : 0;
  const aZoom = Number.isFinite(a.zoom_offset) ? a.zoom_offset : 0;
  const bZoom = Number.isFinite(b.zoom_offset) ? b.zoom_offset : 0;

  // Shortest-angle interpolation for bearing_offset_deg
  let deltaBearing = bBearing - aBearing;
  if (deltaBearing > 180) deltaBearing -= 360;
  if (deltaBearing < -180) deltaBearing += 360;

  const anchor = u < 0.5 ? a : b;
  const sa = clampSpeedMultiplier(a.speed_multiplier);
  const sb = clampSpeedMultiplier(b.speed_multiplier);
  return {
    cameraMode: anchor.camera_mode,
    cameraPreset: anchor.camera_preset,
    pitchDeg: Math.min(60, Math.max(0, aPitch + (bPitch - aPitch) * u)),
    bearingOffsetDeg: aBearing + deltaBearing * u,
    zoomOffset: Math.min(3, Math.max(-3, aZoom + (bZoom - aZoom) * u)),
    speedMultiplier: clampSpeedMultiplier(sa + (sb - sa) * u),
    activeLabel: anchor.label,
  };
}

export function is3dCameraActive(spec: RenderSpecV2): boolean {
  return (
    spec.terrain_3d ||
    spec.camera_preset !== "flat" ||
    spec.camera_pitch_deg > 0 ||
    (spec.camera_keyframes?.some((kf) => kf.camera_preset !== "flat" || kf.pitch_deg > 0) ?? false)
  );
}

/**
 * Computes deterministic MapLibre camera pitch and bearing shared identically
 * between live studio preview, public share player, and /render/$jobId MP4 frames.
 */
export function computeCameraFrame(
  spec: RenderSpecV2,
  evaluated: EvaluatedTimelineState,
  options?: ComputeCameraFrameOptions,
): CameraFrame {
  const hasKeyframes = (spec.camera_keyframes?.length ?? 0) > 0;
  const cue = hasKeyframes
    ? interpolateCameraKeyframes(spec.camera_keyframes, evaluated.progressRatio, spec)
    : null;

  const effectiveMode = cue ? cue.cameraMode : spec.camera_mode;
  const effectivePreset = cue ? cue.cameraPreset : spec.camera_preset;
  const effectivePitch = cue ? cue.pitchDeg : spec.camera_pitch_deg;
  const manualBearing = cue
    ? Number.isFinite(cue.bearingOffsetDeg)
      ? cue.bearingOffsetDeg
      : 0
    : Number.isFinite(spec.camera_bearing_deg)
      ? spec.camera_bearing_deg
      : 0;
  const zoomOffset = cue ? cue.zoomOffset : 0;
  const speedMultiplier = cue ? cue.speedMultiplier : 1;
  const label = cue ? cue.activeLabel : "";

  if (effectiveMode === "fit-track") {
    // In fit-track mode, keep north-up (manualBearing only, never dynamic-3d 180° course-bearing flip)
    // and cap pitch so MapLibre fitBounds frames the full route cleanly without over-zooming out.
    const pitch = Math.min(25, Math.max(0, effectivePitch));
    const bearing =
      effectivePreset === "drone-orbit"
        ? normalizeBearingDeg(manualBearing + evaluated.progressRatio * 120)
        : normalizeBearingDeg(manualBearing);
    return {
      pitch,
      bearing,
      zoomOffset,
      cameraMode: effectiveMode,
      cameraPreset: effectivePreset,
      speedMultiplier,
      label,
    };
  }

  if (effectivePreset === "dynamic-3d") {
    const pitch = Math.min(60, Math.max(effectivePitch, 45));
    let courseBearing =
      options?.courseBearingDeg !== undefined &&
      options.courseBearingDeg !== null &&
      Number.isFinite(options.courseBearingDeg)
        ? normalizeBearingDeg(options.courseBearingDeg)
        : computeBearingDeg(evaluated.previousPosition, evaluated.markerPosition);
    if (courseBearing === null && evaluated.activeStage && evaluated.activeStage.points.length >= 2) {
      const first = evaluated.activeStage.points[0]!;
      const next = evaluated.activeStage.points[Math.min(3, evaluated.activeStage.points.length - 1)]!;
      courseBearing = computeBearingDeg(
        [first.lonUnwrapped, first.lat],
        [next.lonUnwrapped, next.lat],
      );
    }
    return {
      pitch,
      bearing: normalizeBearingDeg((courseBearing ?? 0) + manualBearing),
      zoomOffset,
      cameraMode: effectiveMode,
      cameraPreset: effectivePreset,
      speedMultiplier,
      label,
    };
  }

  if (effectivePreset === "drone-orbit") {
    const pitch = Math.min(60, Math.max(effectivePitch, 50));
    const orbitAzimuth = evaluated.progressRatio * 120;
    return {
      pitch,
      bearing: normalizeBearingDeg(manualBearing + orbitAzimuth),
      zoomOffset,
      cameraMode: effectiveMode,
      cameraPreset: effectivePreset,
      speedMultiplier,
      label,
    };
  }

  // "flat" preset: preserves flat 0/0 defaults, while honoring explicit pitch/terrain_3d overrides
  const pitch = spec.terrain_3d
    ? Math.min(60, Math.max(effectivePitch, 38))
    : Math.min(60, Math.max(0, effectivePitch));
  return {
    pitch,
    bearing: normalizeBearingDeg(manualBearing),
    zoomOffset,
    cameraMode: effectiveMode,
    cameraPreset: effectivePreset,
    speedMultiplier,
    label,
  };
}
