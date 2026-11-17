import type { CameraKeyframe, CameraMode, CameraPreset, RenderSpecV2 } from "../api/types";
import type { EvaluatedTimelineState } from "./timeline";

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

function smoothstep01(t: number): number {
  const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  return x * x * (3 - 2 * x);
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

  const sorted = [...keyframes].sort((a, b) => a.progress - b.progress);
  const p = Math.min(1, Math.max(0, Number.isFinite(progress) ? progress : 0));

  if (sorted.length === 1 || p <= sorted[0]!.progress) {
    const k = sorted[0]!;
    return {
      cameraMode: k.camera_mode,
      cameraPreset: k.camera_preset,
      pitchDeg: k.pitch_deg,
      bearingOffsetDeg: k.bearing_offset_deg,
      zoomOffset: k.zoom_offset,
      speedMultiplier: k.speed_multiplier,
      activeLabel: k.label,
    };
  }

  const last = sorted[sorted.length - 1]!;
  if (p >= last.progress) {
    return {
      cameraMode: last.camera_mode,
      cameraPreset: last.camera_preset,
      pitchDeg: last.pitch_deg,
      bearingOffsetDeg: last.bearing_offset_deg,
      zoomOffset: last.zoom_offset,
      speedMultiplier: last.speed_multiplier,
      activeLabel: last.label,
    };
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

  // Shortest-angle interpolation for bearing_offset_deg
  let deltaBearing = b.bearing_offset_deg - a.bearing_offset_deg;
  if (deltaBearing > 180) deltaBearing -= 360;
  if (deltaBearing < -180) deltaBearing += 360;

  const anchor = u < 0.5 ? a : b;
  return {
    cameraMode: anchor.camera_mode,
    cameraPreset: anchor.camera_preset,
    pitchDeg: Math.min(60, Math.max(0, a.pitch_deg + (b.pitch_deg - a.pitch_deg) * u)),
    bearingOffsetDeg: a.bearing_offset_deg + deltaBearing * u,
    zoomOffset: Math.min(3, Math.max(-3, a.zoom_offset + (b.zoom_offset - a.zoom_offset) * u)),
    speedMultiplier: Math.min(
      4,
      Math.max(0.25, a.speed_multiplier + (b.speed_multiplier - a.speed_multiplier) * u),
    ),
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
