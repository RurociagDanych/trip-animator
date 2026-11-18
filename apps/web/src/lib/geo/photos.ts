import type {
  CameraKeyframe,
  ManifestPhoto,
  PhotoCardStyle,
  PhotoMatchMethod,
  ProjectPhotoOut,
  PublicPhotoOut,
  RenderSpecV2,
} from "../api/types";
import { sampleKeyframeSpeedMultiplier } from "./camera";
import { clamp01, haversineMeters, type TimelinePoint, type TimelineStage } from "./timeline";

export type PhotoInput = ProjectPhotoOut | PublicPhotoOut | ManifestPhoto;

export interface MatchedPhoto {
  id: string;
  caption: string | null;
  width: number;
  height: number;
  takenAtUtc: string | null;
  takenAtMs: number | null;
  lat: number | null;
  lon: number | null;
  matchedPosition: [number, number] | null;
  pinPosition: [number, number] | null;
  matchedProgress: number | null;
  matchMethod: PhotoMatchMethod;
  src: string;
}

export interface ActivePhotoDisplay {
  photo: MatchedPhoto;
  opacity: number;
  scale: number;
  kenBurnsScale: number;
  elapsedInDisplayS: number;
  displayDurationS: number;
}

export interface TimelineScheduleResult {
  progressRatio: number;
  travelDurationS: number;
  activePhoto: ActivePhotoDisplay | null;
  reachedPhotos: MatchedPhoto[];
}

const TIMESTAMP_TOLERANCE_MS = 120_000;
const MAX_GPS_MATCH_DISTANCE_M = 50_000;

export function resolvePhotoSrc(
  photo: PhotoInput,
  srcResolver?: (photoId: string, variant?: "full" | "thumb") => string,
): string {
  if ("asset_url" in photo && typeof photo.asset_url === "string" && photo.asset_url.length > 0) {
    return photo.asset_url;
  }
  if ("data_uri" in photo && typeof photo.data_uri === "string" && photo.data_uri.length > 0) {
    return photo.data_uri;
  }
  if (srcResolver) {
    return srcResolver(photo.id, "thumb");
  }
  return "";
}

function getMetricBounds(
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
): { useTimeMetric: boolean; minMetric: number; maxMetric: number } | null {
  const nonEmpty = stages.filter((s) => s.points.length > 0);
  if (nonEmpty.length === 0) {
    return null;
  }
  let maxGlobalElapsed = 0;
  for (const s of nonEmpty) {
    for (const p of s.points) {
      if (p.globalElapsedS > maxGlobalElapsed) {
        maxGlobalElapsed = p.globalElapsedS;
      }
    }
  }
  const allHaveTimestamps = nonEmpty.every((s) => s.hasTimestamps);
  const useTimeMetric =
    spec.timeline_mode === "recorded-time" && allHaveTimestamps && maxGlobalElapsed > 0;

  const firstPt = nonEmpty[0]!.points[0]!;
  const lastStage = nonEmpty[nonEmpty.length - 1]!;
  const lastPt = lastStage.points[lastStage.points.length - 1]!;
  const minMetric = useTimeMetric ? firstPt.globalElapsedS : firstPt.globalDistM;
  const maxMetric = useTimeMetric ? lastPt.globalElapsedS : lastPt.globalDistM;
  return { useTimeMetric, minMetric, maxMetric };
}

function metricValueOf(pt: TimelinePoint, useTimeMetric: boolean): number {
  return useTimeMetric ? pt.globalElapsedS : pt.globalDistM;
}

function matchByTimestamp(
  takenAtMs: number,
  stages: readonly TimelineStage[],
  useTimeMetric: boolean,
  minMetric: number,
  maxMetric: number,
): { position: [number, number]; progress: number } | null {
  let bestStagePts: TimelinePoint[] | null = null;
  let bestPenalty = Infinity;

  for (const stage of stages) {
    const tsPts = stage.points.filter((p) => p.tsUtcMs !== null && Number.isFinite(p.tsUtcMs));
    if (tsPts.length === 0) continue;
    const firstTs = tsPts[0]!.tsUtcMs!;
    const lastTs = tsPts[tsPts.length - 1]!.tsUtcMs!;
    if (takenAtMs < firstTs - TIMESTAMP_TOLERANCE_MS || takenAtMs > lastTs + TIMESTAMP_TOLERANCE_MS) {
      continue;
    }
    const penalty =
      takenAtMs >= firstTs && takenAtMs <= lastTs
        ? 0
        : Math.min(Math.abs(takenAtMs - firstTs), Math.abs(takenAtMs - lastTs));
    if (penalty < bestPenalty) {
      bestPenalty = penalty;
      bestStagePts = tsPts;
    }
  }

  if (!bestStagePts || bestStagePts.length === 0) {
    return null;
  }

  let lo = 0;
  let hi = bestStagePts.length - 1;
  if (takenAtMs <= bestStagePts[0]!.tsUtcMs!) {
    const pt = bestStagePts[0]!;
    const span = maxMetric - minMetric;
    const progress = span > 0 ? clamp01((metricValueOf(pt, useTimeMetric) - minMetric) / span) : 0;
    return { position: [pt.lonUnwrapped, pt.lat], progress };
  }
  if (takenAtMs >= bestStagePts[hi]!.tsUtcMs!) {
    const pt = bestStagePts[hi]!;
    const span = maxMetric - minMetric;
    const progress = span > 0 ? clamp01((metricValueOf(pt, useTimeMetric) - minMetric) / span) : 1;
    return { position: [pt.lonUnwrapped, pt.lat], progress };
  }

  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if ((bestStagePts[mid]?.tsUtcMs ?? 0) <= takenAtMs) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  const a = bestStagePts[lo]!;
  const b = bestStagePts[hi]!;
  const tsSpan = (b.tsUtcMs ?? 0) - (a.tsUtcMs ?? 0);
  const t = tsSpan > 0 ? clamp01((takenAtMs - (a.tsUtcMs ?? 0)) / tsSpan) : 0;

  const lon = a.lonUnwrapped + (b.lonUnwrapped - a.lonUnwrapped) * t;
  const lat = a.lat + (b.lat - a.lat) * t;
  const mA = metricValueOf(a, useTimeMetric);
  const mB = metricValueOf(b, useTimeMetric);
  const metric = mA + (mB - mA) * t;
  const metricSpan = maxMetric - minMetric;
  const progress = metricSpan > 0 ? clamp01((metric - minMetric) / metricSpan) : 0;
  return { position: [lon, lat], progress };
}

function matchByGps(
  photoLon: number,
  photoLat: number,
  stages: readonly TimelineStage[],
  useTimeMetric: boolean,
  minMetric: number,
  maxMetric: number,
): { position: [number, number]; progress: number; distanceM: number } | null {
  let bestDistM = Infinity;
  let bestLon = photoLon;
  let bestLat = photoLat;
  let bestMetric = minMetric;

  for (const stage of stages) {
    const pts = stage.points;
    if (pts.length === 0) continue;
    if (pts.length === 1) {
      const p = pts[0]!;
      const d = haversineMeters(photoLon, photoLat, p.lonUnwrapped, p.lat);
      if (d < bestDistM) {
        bestDistM = d;
        bestLon = p.lonUnwrapped;
        bestLat = p.lat;
        bestMetric = metricValueOf(p, useTimeMetric);
      }
      continue;
    }

    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1]!;
      const b = pts[i]!;
      if (a.segmentIdx !== b.segmentIdx) {
        const dA = haversineMeters(photoLon, photoLat, a.lonUnwrapped, a.lat);
        if (dA < bestDistM) {
          bestDistM = dA;
          bestLon = a.lonUnwrapped;
          bestLat = a.lat;
          bestMetric = metricValueOf(a, useTimeMetric);
        }
        continue;
      }

      const midLatRad = (((a.lat + b.lat) / 2) * Math.PI) / 180;
      const kx = Math.max(0.1, Math.cos(midLatRad)) * 111_320;
      const ky = 111_320;

      const ax = a.lonUnwrapped * kx;
      const ay = a.lat * ky;
      const bx = b.lonUnwrapped * kx;
      const by = b.lat * ky;
      const px = photoLon * kx;
      const py = photoLat * ky;

      const dx = bx - ax;
      const dy = by - ay;
      const lenSq = dx * dx + dy * dy;
      const t = lenSq > 1e-9 ? clamp01(((px - ax) * dx + (py - ay) * dy) / lenSq) : 0;

      const projLon = a.lonUnwrapped + (b.lonUnwrapped - a.lonUnwrapped) * t;
      const projLat = a.lat + (b.lat - a.lat) * t;
      const distM = haversineMeters(photoLon, photoLat, projLon, projLat);

      if (distM < bestDistM) {
        bestDistM = distM;
        bestLon = projLon;
        bestLat = projLat;
        const mA = metricValueOf(a, useTimeMetric);
        const mB = metricValueOf(b, useTimeMetric);
        bestMetric = mA + (mB - mA) * t;
      }
    }
  }

  if (!Number.isFinite(bestDistM) || bestDistM > MAX_GPS_MATCH_DISTANCE_M) {
    return null;
  }

  const metricSpan = maxMetric - minMetric;
  const progress = metricSpan > 0 ? clamp01((bestMetric - minMetric) / metricSpan) : 0;
  return { position: [bestLon, bestLat], progress, distanceM: bestDistM };
}

/**
 * Deterministically matches project photos onto the evaluated route timeline
 * via timestamp interpolation (with timezone-mismatch GPS fallback) or GPS nearest-segment projection.
 */
export function matchPhotosToTimeline(
  photos: readonly PhotoInput[],
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
  srcResolver?: (photoId: string, variant?: "full" | "thumb") => string,
): MatchedPhoto[] {
  const bounds = getMetricBounds(stages, spec);
  const results: MatchedPhoto[] = [];

  for (const photo of photos) {
    if ("enabled" in photo && photo.enabled === false) {
      continue;
    }

    const src = resolvePhotoSrc(photo, srcResolver);
    const takenAtMsRaw = photo.taken_at_utc ? Date.parse(photo.taken_at_utc) : Number.NaN;
    const takenAtMs = Number.isFinite(takenAtMsRaw) ? takenAtMsRaw : null;
    const hasValidGps =
      photo.lat !== null &&
      photo.lon !== null &&
      Number.isFinite(photo.lat) &&
      Number.isFinite(photo.lon) &&
      Math.abs(photo.lat) <= 90;

    let matchedPosition: [number, number] | null = null;
    let pinPosition: [number, number] | null = null;
    let matchedProgress: number | null = null;
    let matchMethod: PhotoMatchMethod = "unmatched";

    if (bounds) {
      const { useTimeMetric, minMetric, maxMetric } = bounds;

      // 1. Respect manual match if backend provided manual coordinates/progress
      if (
        photo.match_method === "manual" &&
        photo.matched_progress !== null &&
        photo.matched_progress !== undefined &&
        Number.isFinite(photo.matched_progress) &&
        photo.matched_lon !== null &&
        photo.matched_lon !== undefined &&
        photo.matched_lat !== null &&
        photo.matched_lat !== undefined
      ) {
        matchedProgress = clamp01(photo.matched_progress);
        matchedPosition = [photo.matched_lon, photo.matched_lat];
        pinPosition = hasValidGps ? [photo.lon!, photo.lat!] : matchedPosition;
        matchMethod = "manual";
      } else {
        // 2. Timestamp match with timezone-mismatch fallback (>50km from GPS)
        let tsCandidate: { position: [number, number]; progress: number } | null = null;
        if (takenAtMs !== null) {
          tsCandidate = matchByTimestamp(takenAtMs, stages, useTimeMetric, minMetric, maxMetric);
          if (tsCandidate && hasValidGps) {
            const distFromGps = haversineMeters(
              photo.lon!,
              photo.lat!,
              tsCandidate.position[0],
              tsCandidate.position[1],
            );
            if (distFromGps > MAX_GPS_MATCH_DISTANCE_M) {
              tsCandidate = null;
            }
          }
        }

        if (tsCandidate) {
          matchedPosition = tsCandidate.position;
          pinPosition = hasValidGps ? [photo.lon!, photo.lat!] : tsCandidate.position;
          matchedProgress = tsCandidate.progress;
          matchMethod = "timestamp";
        } else if (hasValidGps) {
          // 3. GPS nearest-segment projection
          const gpsCandidate = matchByGps(
            photo.lon!,
            photo.lat!,
            stages,
            useTimeMetric,
            minMetric,
            maxMetric,
          );
          if (gpsCandidate) {
            matchedPosition = gpsCandidate.position;
            pinPosition = [photo.lon!, photo.lat!];
            matchedProgress = gpsCandidate.progress;
            matchMethod = "gps";
          }
        }
      }
    } else if (
      photo.matched_progress !== null &&
      photo.matched_progress !== undefined &&
      Number.isFinite(photo.matched_progress) &&
      photo.matched_lon !== null &&
      photo.matched_lon !== undefined &&
      photo.matched_lat !== null &&
      photo.matched_lat !== undefined &&
      photo.match_method !== "unmatched"
    ) {
      matchedProgress = clamp01(photo.matched_progress);
      matchedPosition = [photo.matched_lon, photo.matched_lat];
      pinPosition = hasValidGps ? [photo.lon!, photo.lat!] : matchedPosition;
      matchMethod = photo.match_method ?? "gps";
    }

    results.push({
      id: photo.id,
      caption: photo.caption,
      width: photo.width,
      height: photo.height,
      takenAtUtc: photo.taken_at_utc,
      takenAtMs,
      lat: photo.lat,
      lon: photo.lon,
      matchedPosition,
      pinPosition,
      matchedProgress,
      matchMethod,
      src,
    });
  }

  return results.sort((a, b) => {
    const aMatched = a.matchedProgress !== null;
    const bMatched = b.matchedProgress !== null;
    if (aMatched !== bMatched) return aMatched ? -1 : 1;
    if (a.matchedProgress !== null && b.matchedProgress !== null) {
      if (Math.abs(a.matchedProgress - b.matchedProgress) > 1e-9) {
        return a.matchedProgress - b.matchedProgress;
      }
    }
    const aTs = a.takenAtMs ?? Infinity;
    const bTs = b.takenAtMs ?? Infinity;
    if (aTs !== bTs) return aTs - bTs;
    return a.id.localeCompare(b.id);
  });
}

function computeDisplayEnvelope(
  elapsedInDisplayS: number,
  displayDurationS: number,
  photoCardStyle: PhotoCardStyle = "corner",
): {
  opacity: number;
  scale: number;
  kenBurnsScale: number;
} {
  const safeDur = Math.max(0.2, displayDurationS);
  const fade = Math.min(0.22, safeDur * 0.25);
  let opacity = 1;
  if (elapsedInDisplayS < fade) {
    opacity = clamp01(elapsedInDisplayS / fade);
  } else if (elapsedInDisplayS > safeDur - fade) {
    opacity = clamp01((safeDur - elapsedInDisplayS) / fade);
  }
  const scale = 0.96 + 0.04 * opacity;
  const kenBurnsScale =
    photoCardStyle === "cinematic" ? 1.0 + 0.12 * clamp01(elapsedInDisplayS / safeDur) : 1.0;
  return { opacity, scale, kenBurnsScale };
}

const SPEED_LUT_STEPS = 512;

interface SpeedCurveLut {
  cumTime: Float64Array;
  dp: number;
}

interface CachedSpeedLutEntry {
  sig: string;
  lut: SpeedCurveLut | null;
}

const SPEED_LUT_CACHE = new WeakMap<readonly CameraKeyframe[], CachedSpeedLutEntry>();

function computeKeyframesSig(keyframes: readonly CameraKeyframe[]): string {
  return keyframes.map((kf) => `${kf.progress}:${kf.speed_multiplier}`).join("|");
}

function getSpeedCurveLut(keyframes: readonly CameraKeyframe[] | undefined): SpeedCurveLut | null {
  if (!keyframes || keyframes.length === 0) {
    return null;
  }
  const sig = computeKeyframesSig(keyframes);
  const cached = SPEED_LUT_CACHE.get(keyframes);
  if (cached && cached.sig === sig) {
    return cached.lut;
  }

  const sorted = [...keyframes].sort(
    (a, b) =>
      (Number.isFinite(a.progress) ? a.progress : 0) -
      (Number.isFinite(b.progress) ? b.progress : 0),
  );
  let minSpeed = Infinity;
  let maxSpeed = -Infinity;
  const speeds = new Float64Array(SPEED_LUT_STEPS + 1);
  for (let i = 0; i <= SPEED_LUT_STEPS; i++) {
    const s = sampleKeyframeSpeedMultiplier(sorted, i / SPEED_LUT_STEPS);
    speeds[i] = s;
    if (s < minSpeed) minSpeed = s;
    if (s > maxSpeed) maxSpeed = s;
  }

  if (maxSpeed - minSpeed < 1e-6) {
    SPEED_LUT_CACHE.set(keyframes, { sig, lut: null });
    return null;
  }

  const cumTime = new Float64Array(SPEED_LUT_STEPS + 1);
  const dp = 1 / SPEED_LUT_STEPS;
  cumTime[0] = 0;
  let prevPace = 1 / speeds[0]!;
  for (let i = 1; i <= SPEED_LUT_STEPS; i++) {
    const pace = 1 / speeds[i]!;
    cumTime[i] = cumTime[i - 1]! + 0.5 * (prevPace + pace) * dp;
    prevPace = pace;
  }

  const total = cumTime[SPEED_LUT_STEPS]!;
  if (!(total > 0) || !Number.isFinite(total)) {
    SPEED_LUT_CACHE.set(keyframes, { sig, lut: null });
    return null;
  }
  for (let i = 1; i < SPEED_LUT_STEPS; i++) {
    cumTime[i] = cumTime[i]! / total;
  }
  cumTime[SPEED_LUT_STEPS] = 1;

  const lut: SpeedCurveLut = { cumTime, dp };
  SPEED_LUT_CACHE.set(keyframes, { sig, lut });
  return lut;
}

function progressToTravelFraction(lut: SpeedCurveLut | null, progress: number): number {
  const p = clamp01(progress);
  if (!lut) {
    return p;
  }
  if (p <= 0) return 0;
  if (p >= 1) return 1;
  const pos = p / lut.dp;
  const idx = Math.min(SPEED_LUT_STEPS - 1, Math.floor(pos));
  const frac = pos - idx;
  const t0 = lut.cumTime[idx]!;
  const t1 = lut.cumTime[idx + 1]!;
  return clamp01(t0 + (t1 - t0) * frac);
}

function travelFractionToProgress(lut: SpeedCurveLut | null, tau: number): number {
  const t = clamp01(tau);
  if (!lut) {
    return t;
  }
  if (t <= 0) return 0;
  if (t >= 1) return 1;

  let lo = 0;
  let hi = SPEED_LUT_STEPS;
  while (lo + 1 < hi) {
    const mid = (lo + hi) >>> 1;
    if (lut.cumTime[mid]! <= t) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  const t0 = lut.cumTime[lo]!;
  const t1 = lut.cumTime[hi]!;
  const denom = t1 - t0;
  const frac = denom > 1e-12 ? (t - t0) / denom : 0;
  return clamp01((lo + frac) * lut.dp);
}

/**
 * Deterministic timeline scheduler shared by editor playback, share playback,
 * and /render/$jobId MP4 frames. Handles intro/outro holds, photo_display_seconds,
 * speed_multiplier timeline warping, and capped pause_on_photo timeline warping with preview holdScale parity.
 */
export function evaluateTimelineSchedule(
  _stages: readonly TimelineStage[],
  matchedPhotos: readonly MatchedPhoto[],
  spec: RenderSpecV2,
  currentTimeS: number,
  durationS: number,
  isPreview: boolean,
): TimelineScheduleResult {
  const safeDurationS = Number.isFinite(durationS) && durationS > 0 ? durationS : 1;
  const clampedTimeS = Math.min(Math.max(0, Number.isFinite(currentTimeS) ? currentTimeS : 0), safeDurationS);

  const holdScale =
    isPreview && spec.duration_seconds > 0
      ? Math.min(1, safeDurationS / spec.duration_seconds)
      : 1;
  const introHold = Math.min(spec.intro_hold_seconds * holdScale, safeDurationS * 0.35);
  const outroHold = Math.min(spec.outro_hold_seconds * holdScale, safeDurationS * 0.35);
  const activeDuration = Math.max(0.05, safeDurationS - introHold - outroHold);

  const stops = spec.show_photos
    ? matchedPhotos.filter((p): p is MatchedPhoto & { matchedProgress: number } => p.matchedProgress !== null)
    : [];

  const desiredDisplayS = Math.max(0.2, (spec.photo_display_seconds ?? 3.0) * holdScale);
  const speedLut = getSpeedCurveLut(spec.camera_keyframes);

  // Non-paused schedule when pause_on_photo is off or no matched photos exist
  if (!spec.pause_on_photo || stops.length === 0) {
    let progressRatio = 0;
    if (clampedTimeS <= introHold) {
      progressRatio = 0;
    } else if (clampedTimeS >= safeDurationS - outroHold) {
      progressRatio = 1;
    } else {
      const tau = clamp01((clampedTimeS - introHold) / activeDuration);
      progressRatio = travelFractionToProgress(speedLut, tau);
    }

    let activePhoto: ActivePhotoDisplay | null = null;
    for (let i = 0; i < stops.length; i++) {
      const stop = stops[i]!;
      const triggerTimeS =
        introHold + progressToTravelFraction(speedLut, stop.matchedProgress) * activeDuration;
      const elapsed = clampedTimeS - triggerTimeS;
      if (elapsed >= 0 && elapsed <= desiredDisplayS) {
        const env = computeDisplayEnvelope(elapsed, desiredDisplayS, spec.photo_card_style);
        activePhoto = {
          photo: stop,
          opacity: env.opacity,
          scale: env.scale,
          kenBurnsScale: env.kenBurnsScale,
          elapsedInDisplayS: elapsed,
          displayDurationS: desiredDisplayS,
        };
      }
    }

    const reachedPhotos = stops.filter(
      (p) => spec.visibility_mode === "full" || p.matchedProgress <= progressRatio + 1e-6,
    );

    return {
      progressRatio,
      travelDurationS: activeDuration,
      activePhoto,
      reachedPhotos,
    };
  }

  // Piecewise stop schedule when pause_on_photo is enabled (warped by keyframe speed curve)
  const maxTotalPauseS = activeDuration * 0.65;
  const rawTotalPauseS = stops.length * desiredDisplayS;
  const pauseScale = rawTotalPauseS > maxTotalPauseS ? maxTotalPauseS / rawTotalPauseS : 1;
  const perPauseS = desiredDisplayS * pauseScale;
  const totalPauseS = stops.length * perPauseS;
  const travelDurationS = Math.max(0.05, activeDuration - totalPauseS);

  let progressRatio = 0;
  let activePhoto: ActivePhotoDisplay | null = null;

  if (clampedTimeS <= introHold) {
    progressRatio = 0;
  } else if (clampedTimeS >= safeDurationS - outroHold) {
    progressRatio = 1;
  } else {
    const activeT = clampedTimeS - introHold;
    let cursorTime = 0;
    let prevProgress = 0;
    let resolved = false;

    for (let i = 0; i < stops.length; i++) {
      const stop = stops[i]!;
      const targetProgress = clamp01(stop.matchedProgress);
      const tauStart = progressToTravelFraction(speedLut, prevProgress);
      const tauEnd = progressToTravelFraction(speedLut, targetProgress);
      const segTravelS = Math.max(0, tauEnd - tauStart) * travelDurationS;

      if (activeT <= cursorTime + segTravelS) {
        const localFrac = segTravelS > 1e-9 ? clamp01((activeT - cursorTime) / segTravelS) : 1;
        const tau = tauStart + localFrac * Math.max(0, tauEnd - tauStart);
        progressRatio =
          segTravelS <= 1e-9 && localFrac >= 1
            ? targetProgress
            : travelFractionToProgress(speedLut, tau);
        resolved = true;
        break;
      }
      cursorTime += segTravelS;

      if (activeT <= cursorTime + perPauseS) {
        progressRatio = targetProgress;
        const elapsedInPause = Math.max(0, activeT - cursorTime);
        const env = computeDisplayEnvelope(elapsedInPause, perPauseS, spec.photo_card_style);
        activePhoto = {
          photo: stop,
          opacity: env.opacity,
          scale: env.scale,
          kenBurnsScale: env.kenBurnsScale,
          elapsedInDisplayS: elapsedInPause,
          displayDurationS: perPauseS,
        };
        resolved = true;
        break;
      }
      cursorTime += perPauseS;
      prevProgress = targetProgress;
    }

    if (!resolved) {
      const tauPrev = progressToTravelFraction(speedLut, prevProgress);
      const tailTauSpan = Math.max(0, 1 - tauPrev);
      const tailTravelS = Math.max(1e-9, tailTauSpan * travelDurationS);
      const localFrac = clamp01((activeT - cursorTime) / tailTravelS);
      const tau = tauPrev + tailTauSpan * localFrac;
      progressRatio = travelFractionToProgress(speedLut, tau);
    }
  }

  const reachedPhotos = stops.filter(
    (p) => spec.visibility_mode === "full" || p.matchedProgress <= progressRatio + 1e-6,
  );

  return {
    progressRatio: clamp01(progressRatio),
    travelDurationS,
    activePhoto,
    reachedPhotos,
  };
}

/**
 * Inverse of evaluateTimelineSchedule on travel segments: maps a route progress ratio [0, 1]
 * back to master timeline time ratio [0, 1] so seeking the scrubber/chart lands at the exact
 * requested route position even when pause_on_photo or keyframe speed warps the timeline.
 */
export function progressRatioToTimeRatio(
  matchedPhotos: readonly MatchedPhoto[],
  spec: RenderSpecV2,
  targetProgressRatio: number,
  durationS: number,
  isPreview: boolean,
): number {
  const target = clamp01(targetProgressRatio);
  if (target <= 0) return 0;
  if (target >= 1) return 1;

  const safeDurationS = Number.isFinite(durationS) && durationS > 0 ? durationS : 1;
  const holdScale =
    isPreview && spec.duration_seconds > 0
      ? Math.min(1, safeDurationS / spec.duration_seconds)
      : 1;
  const introHold = Math.min(spec.intro_hold_seconds * holdScale, safeDurationS * 0.35);
  const outroHold = Math.min(spec.outro_hold_seconds * holdScale, safeDurationS * 0.35);
  const activeDuration = Math.max(0.05, safeDurationS - introHold - outroHold);

  const stops = spec.show_photos
    ? matchedPhotos.filter((p): p is MatchedPhoto & { matchedProgress: number } => p.matchedProgress !== null)
    : [];

  const speedLut = getSpeedCurveLut(spec.camera_keyframes);

  if (!spec.pause_on_photo || stops.length === 0) {
    return clamp01(
      (introHold + progressToTravelFraction(speedLut, target) * activeDuration) / safeDurationS,
    );
  }

  const desiredDisplayS = Math.max(0.2, (spec.photo_display_seconds ?? 3.0) * holdScale);
  const maxTotalPauseS = activeDuration * 0.65;
  const rawTotalPauseS = stops.length * desiredDisplayS;
  const pauseScale = rawTotalPauseS > maxTotalPauseS ? maxTotalPauseS / rawTotalPauseS : 1;
  const perPauseS = desiredDisplayS * pauseScale;
  const totalPauseS = stops.length * perPauseS;
  const travelDurationS = Math.max(0.05, activeDuration - totalPauseS);

  let cursorTime = 0;
  let tauPrev = 0;
  for (let i = 0; i < stops.length; i++) {
    const stopProgress = clamp01(stops[i]!.matchedProgress);
    const tauStop = progressToTravelFraction(speedLut, stopProgress);
    const segTauSpan = Math.max(0, tauStop - tauPrev);
    if (target <= stopProgress) {
      const tauTarget = progressToTravelFraction(speedLut, target);
      const segTime = Math.max(0, tauTarget - tauPrev) * travelDurationS;
      return clamp01((introHold + cursorTime + segTime) / safeDurationS);
    }
    cursorTime += segTauSpan * travelDurationS + perPauseS;
    tauPrev = tauStop;
  }

  const tauTarget = progressToTravelFraction(speedLut, target);
  const tailTime = Math.max(0, tauTarget - tauPrev) * travelDurationS;
  return clamp01((introHold + cursorTime + tailTime) / safeDurationS);
}
