import { normalizeActivityType, type ActivityType } from "../../components/icons";
import type {
  ClimbCategory,
  ClimbHighlightOut,
  ColorByMode,
  ManifestClimb,
  ManifestColorSegment,
  PublicStageOut,
  RenderManifest,
  RenderSpecV2,
  StageOut,
  TelemetryChartOut,
  TelemetryChartPoint,
  TelemetryGeometryFeature,
  TelemetryGeometryOut,
  TransitionIcon,
} from "../api/types";
import { circularMeanBearingDeg, computeBearingDeg } from "./camera";
import { hexToRgbTuple } from "./format";

export interface TimelinePoint {
  lat: number;
  lon: number;
  lonUnwrapped: number;
  elevM: number | null;
  cumDistM: number; // local stage cumulative distance
  globalDistM: number; // cumulative across all stages
  speedMps: number;
  gradePct: number | null;
  hrBpm?: number | null;
  watts?: number | null;
  elapsedS: number; // local stage elapsed seconds
  globalElapsedS: number; // cumulative recorded seconds across stages
  tsUtcMs: number | null;
  segmentIdx: number;
}

export interface TimelineStage {
  id: string;
  name: string;
  dayIndex: number;
  stageOrder: number;
  activityType: ActivityType;
  color: string;
  transition: TransitionIcon;
  distanceM: number;
  elevationGainM: number;
  movingSeconds: number;
  elapsedSeconds: number;
  hasTimestamps: boolean;
  points: TimelinePoint[];
  colorSegments?: ManifestColorSegment[];
}

export interface VisiblePathSegment {
  id: string;
  stageId: string;
  color: [number, number, number];
  path: [number, number][];
}

export interface StopMarker {
  stageId: string;
  name: string;
  dayIndex: number;
  transition: TransitionIcon;
  position: [number, number];
  reached: boolean;
}

export interface ColorLegendStop {
  color: string;
  rgb: [number, number, number];
  label: string;
  minVal: number | null;
  maxVal: number | null;
}

export interface TransferArcSegment {
  id: string;
  fromStageId: string;
  toStageId: string;
  fromPosition: [number, number];
  toPosition: [number, number];
  distanceM: number;
  reached: boolean;
  active: boolean;
  waypoints: [number, number][];
}

export interface ChapterCardState {
  stageId: string;
  dayIndex: number;
  title: string;
  distanceM: number;
  elevationGainM: number;
  activityType: ActivityType;
  opacity: number;
  elapsedInCardS: number;
}

export interface ClimbProgressMarker {
  id: string;
  stageId: string;
  name: string;
  category: ClimbCategory;
  startDistM: number;
  endDistM: number;
  startProgress: number;
  endProgress: number;
  summitProgress: number;
  summitPosition: [number, number];
  summitElevM: number;
  distanceM: number;
  elevationGainM: number;
  avgGradePct: number;
  maxGradePct: number;
  fietsScore: number;
  reached: boolean;
  active: boolean;
}

export interface TelemetryReadout {
  elevM: number | null;
  speedMps: number;
  gradePct: number | null;
  hrBpm: number | null;
  watts: number | null;
  vamMPerH: number | null;
}

export interface EvaluatedTimelineState {
  progressRatio: number;
  activeStageIndex: number;
  activeStage: TimelineStage | null;
  markerPosition: [number, number];
  previousPosition: [number, number];
  elevM: number | null;
  speedMps: number;
  gradePct: number | null;
  hrBpm?: number | null;
  watts?: number | null;
  vamMPerH?: number | null;
  distanceTravelledM: number;
  distanceRemainingM: number;
  totalDistanceM: number;
  elapsedSeconds: number;
  currentTsUtcMs: number | null;
  visiblePaths: VisiblePathSegment[];
  overnightStops: StopMarker[];
  transferArcs?: TransferArcSegment[];
  chapterCard?: ChapterCardState | null;
  colorLegendStops?: ColorLegendStop[];
  bounds: [number, number, number, number];
}

const DEFAULT_BOUNDS: [number, number, number, number] = [0.18, 0.08, 0.27, 0.15];

export function clamp01(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.min(1, Math.max(0, value));
}

export function haversineMeters(lon1: number, lat1: number, lon2: number, lat2: number): number {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

export function buildTimelineFromManifest(manifest: RenderManifest): TimelineStage[] {
  const sortedStages = [...manifest.stages].sort((a, b) => a.stage_order - b.stage_order);
  let globalDistOffset = 0;
  let globalElapsedOffset = 0;

  return sortedStages.map((stage) => {
    const stageColor =
      manifest.spec.stage_colors[stage.id] ?? stage.color ?? manifest.spec.track_color;
    const stageTransition =
      manifest.spec.stage_transitions[stage.id] ?? stage.transition ?? "none";

    const lastPtDist =
      stage.points.length > 0 ? (stage.points[stage.points.length - 1]?.cum_dist_m ?? 0) : 0;
    const stageTotalDist = Math.max(lastPtDist, stage.distance_m, 1);
    const hasPointElapsed =
      stage.has_timestamps &&
      stage.points.every((p) => p.elapsed_s !== null && Number.isFinite(p.elapsed_s));
    const fallbackDurationS =
      stage.elapsed_seconds > 0
        ? stage.elapsed_seconds
        : stage.moving_seconds > 0
          ? stage.moving_seconds
          : Math.max(stage.distance_m / 4, 1);

    const pts: TimelinePoint[] = stage.points.map((p) => {
      const localElapsedS =
        hasPointElapsed && p.elapsed_s !== null
          ? p.elapsed_s
          : (p.cum_dist_m / stageTotalDist) * fallbackDurationS;

      return {
        lat: p.lat,
        lon: p.lon,
        lonUnwrapped: p.lon_unwrapped ?? p.lon,
        elevM: p.elev_m,
        cumDistM: p.cum_dist_m,
        globalDistM: globalDistOffset + p.cum_dist_m,
        speedMps: p.speed_mps,
        gradePct: p.grade_pct ?? null,
        hrBpm: p.hr_bpm ?? null,
        watts: p.watts ?? null,
        elapsedS: localElapsedS,
        globalElapsedS: globalElapsedOffset + localElapsedS,
        tsUtcMs: p.ts_utc ? Date.parse(p.ts_utc) : null,
        segmentIdx: p.segment_idx,
      };
    });

    const stageLastDist = pts.length > 0 ? (pts[pts.length - 1]?.cumDistM ?? stage.distance_m) : stage.distance_m;
    const stageLastElapsed =
      pts.length > 0
        ? Math.max(pts[pts.length - 1]?.elapsedS ?? 0, stage.elapsed_seconds)
        : stage.elapsed_seconds;

    globalDistOffset += Math.max(stageLastDist, stage.distance_m);
    globalElapsedOffset += Math.max(stageLastElapsed, 1);

    return {
      id: stage.id,
      name: stage.name,
      dayIndex: stage.day_index,
      stageOrder: stage.stage_order,
      activityType: normalizeActivityType(stage.activity_type),
      color: stageColor,
      transition: stageTransition,
      distanceM: stage.distance_m,
      elevationGainM: stage.elevation_gain_m,
      movingSeconds: stage.moving_seconds,
      elapsedSeconds: stage.elapsed_seconds,
      hasTimestamps: hasPointElapsed,
      points: pts,
      colorSegments: stage.color_segments,
    };
  });
}

function sampleChartAtDist(
  chartPoints: TelemetryChartOut["points"],
  targetGlobalDistM: number,
): {
  elevM: number | null;
  speedMps: number;
  gradePct: number | null;
  hrBpm: number | null;
  watts: number | null;
} {
  if (chartPoints.length === 0) {
    return { elevM: null, speedMps: 0, gradePct: null, hrBpm: null, watts: null };
  }
  const first = chartPoints[0]!;
  if (targetGlobalDistM <= first.cum_dist_m || chartPoints.length === 1) {
    return {
      elevM: first.elev_m,
      speedMps: first.speed_mps,
      gradePct: first.grade_pct,
      hrBpm: first.hr_bpm ?? null,
      watts: first.watts ?? null,
    };
  }
  const last = chartPoints[chartPoints.length - 1]!;
  if (targetGlobalDistM >= last.cum_dist_m) {
    return {
      elevM: last.elev_m,
      speedMps: last.speed_mps,
      gradePct: last.grade_pct,
      hrBpm: last.hr_bpm ?? null,
      watts: last.watts ?? null,
    };
  }

  let lo = 0;
  let hi = chartPoints.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if ((chartPoints[mid]?.cum_dist_m ?? 0) <= targetGlobalDistM) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  const a = chartPoints[lo]!;
  const b = chartPoints[hi]!;
  const span = b.cum_dist_m - a.cum_dist_m;
  const t = span > 0 ? clamp01((targetGlobalDistM - a.cum_dist_m) / span) : 0;

  const interpNullable = (va: number | null | undefined, vb: number | null | undefined): number | null => {
    if (va !== null && va !== undefined && vb !== null && vb !== undefined) {
      return va + (vb - va) * t;
    }
    return va ?? vb ?? null;
  };

  return {
    elevM: interpNullable(a.elev_m, b.elev_m),
    speedMps: a.speed_mps + (b.speed_mps - a.speed_mps) * t,
    gradePct: interpNullable(a.grade_pct, b.grade_pct),
    hrBpm: interpNullable(a.hr_bpm, b.hr_bpm),
    watts: interpNullable(a.watts, b.watts),
  };
}

function isValidCoordTuple(coord: unknown): coord is readonly [number, number] {
  return (
    Array.isArray(coord) &&
    coord.length >= 2 &&
    typeof coord[0] === "number" &&
    typeof coord[1] === "number" &&
    Number.isFinite(coord[0]) &&
    Number.isFinite(coord[1]) &&
    Math.abs(coord[1]) <= 90
  );
}

export function buildTimelineFromProject(
  stages: readonly (StageOut | PublicStageOut)[],
  geometry: TelemetryGeometryOut | readonly TelemetryGeometryFeature[] | undefined,
  chart: TelemetryChartOut | undefined,
  spec: RenderSpecV2,
): TimelineStage[] {
  const orderedStages = [...stages].sort((a, b) => a.stage_order - b.stage_order);
  const rawFeatures: readonly TelemetryGeometryFeature[] = Array.isArray(geometry)
    ? geometry
    : (geometry as TelemetryGeometryOut | undefined)?.features ?? [];
  const featuresByStageId = new Map<string, TelemetryGeometryFeature>(
    rawFeatures.map((f) => [f.properties?.stage_id ?? f.id, f]),
  );

  let globalDistOffset = 0;
  let globalElapsedOffset = 0;

  return orderedStages.map((stage) => {
    const feature = featuresByStageId.get(stage.id);
    const geometryObj = feature?.geometry;
    const lines =
      geometryObj?.type === "MultiLineString" && Array.isArray(geometryObj.coordinates)
        ? geometryObj.coordinates
        : [];
    const stageColor = spec.stage_colors[stage.id] ?? spec.track_color;
    const stageTransition = spec.stage_transitions[stage.id] ?? "none";
    const activityType = normalizeActivityType(
      stage.activity_type ?? feature?.properties?.activity_type,
    );

    // Compute raw local distances along coordinates, skipping malformed tuples
    const rawCoords: { lon: number; lat: number; segmentIdx: number; rawDistM: number }[] = [];
    let runningRawDist = 0;
    lines.forEach((line, segIdx) => {
      if (!Array.isArray(line)) return;
      let prevValid: { lon: number; lat: number } | null = null;
      line.forEach((coord) => {
        if (!isValidCoordTuple(coord)) return;
        const lon = coord[0];
        const lat = coord[1];
        if (prevValid !== null) {
          runningRawDist += haversineMeters(prevValid.lon, prevValid.lat, lon, lat);
        }
        prevValid = { lon, lat };
        rawCoords.push({ lon, lat, segmentIdx: segIdx, rawDistM: runningRawDist });
      });
    });

    const targetStageDist = Math.max(
      feature?.properties?.distance_m ?? stage.distance_m,
      runningRawDist,
      1,
    );
    const distScale = runningRawDist > 0 ? targetStageDist / runningRawDist : 1;
    const stageDurationS = Math.max(stage.elapsed_seconds || stage.moving_seconds || targetStageDist / 4, 1);
    const startMs = stage.started_at ? Date.parse(stage.started_at) : null;

    const chartPoints = smoothTelemetryChartPoints(chart?.points ?? [], spec.telemetry_smoothing);

    const points: TimelinePoint[] = rawCoords.map((rc) => {
      const cumDistM = rc.rawDistM * distScale;
      const frac = targetStageDist > 0 ? clamp01(cumDistM / targetStageDist) : 0;
      const elapsedS = frac * stageDurationS;
      const globalDistM = globalDistOffset + cumDistM;
      const sampled = sampleChartAtDist(chartPoints, globalDistM);
      return {
        lat: rc.lat,
        lon: rc.lon,
        lonUnwrapped: rc.lon,
        elevM: sampled.elevM,
        cumDistM,
        globalDistM,
        speedMps: sampled.speedMps,
        gradePct: sampled.gradePct,
        hrBpm: sampled.hrBpm,
        watts: sampled.watts,
        elapsedS,
        globalElapsedS: globalElapsedOffset + elapsedS,
        tsUtcMs: startMs !== null && Number.isFinite(startMs) ? startMs + elapsedS * 1000 : null,
        segmentIdx: rc.segmentIdx,
      };
    });

    const actualStageDist = feature?.properties?.distance_m ?? stage.distance_m;
    const realizedStageDist =
      points.length > 0
        ? Math.max(actualStageDist, points[points.length - 1]?.cumDistM ?? 0)
        : actualStageDist;
    globalDistOffset += realizedStageDist;
    globalElapsedOffset += stageDurationS;

    return {
      id: stage.id,
      name: stage.name,
      dayIndex: stage.day_index,
      stageOrder: stage.stage_order,
      activityType,
      color: stageColor,
      transition: stageTransition,
      distanceM: actualStageDist,
      elevationGainM: feature?.properties?.elevation_gain_m ?? stage.elevation_gain_m,
      movingSeconds: stage.moving_seconds,
      elapsedSeconds: stage.elapsed_seconds,
      hasTimestamps: stage.has_timestamps,
      points,
    };
  });
}

export function computeTimelineBounds(stages: readonly TimelineStage[]): [number, number, number, number] {
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  for (const stage of stages) {
    for (const pt of stage.points) {
      if (pt.lonUnwrapped < minLon) minLon = pt.lonUnwrapped;
      if (pt.lat < minLat) minLat = pt.lat;
      if (pt.lonUnwrapped > maxLon) maxLon = pt.lonUnwrapped;
      if (pt.lat > maxLat) maxLat = pt.lat;
    }
  }

  if (!Number.isFinite(minLon) || !Number.isFinite(minLat)) {
    return DEFAULT_BOUNDS;
  }

  // Ensure non-degenerate bbox
  if (Math.abs(maxLon - minLon) < 0.002) {
    minLon -= 0.005;
    maxLon += 0.005;
  }
  if (Math.abs(maxLat - minLat) < 0.002) {
    minLat -= 0.005;
    maxLat += 0.005;
  }
  return [minLon, minLat, maxLon, maxLat];
}

export function frameIndexToProgress(
  frameIndex: number,
  totalFrames: number,
  spec: RenderSpecV2,
  durationS: number,
  isPreview: boolean,
): number {
  if (totalFrames <= 1 || durationS <= 0) {
    return 0;
  }
  const clampedFrame = Math.min(Math.max(0, frameIndex), totalFrames - 1);
  const currentTimeS = (clampedFrame / (totalFrames - 1)) * durationS;

  const holdScale =
    isPreview && spec.duration_seconds > 0
      ? Math.min(1, durationS / spec.duration_seconds)
      : 1;
  const introHold = Math.min(spec.intro_hold_seconds * holdScale, durationS * 0.35);
  const outroHold = Math.min(spec.outro_hold_seconds * holdScale, durationS * 0.35);
  const activeDuration = Math.max(0.05, durationS - introHold - outroHold);

  if (currentTimeS <= introHold) {
    return 0;
  }
  if (currentTimeS >= durationS - outroHold) {
    return 1;
  }
  return clamp01((currentTimeS - introHold) / activeDuration);
}

interface FlatSample {
  stageIndex: number;
  point: TimelinePoint;
  metricValue: number; // either globalDistM or globalElapsedS
}

function interpolateNullable(
  va: number | null | undefined,
  vb: number | null | undefined,
  t: number,
): number | null {
  if (va !== null && va !== undefined && vb !== null && vb !== undefined) {
    return va + (vb - va) * t;
  }
  return va ?? vb ?? null;
}

function interpolatePoint(a: TimelinePoint, b: TimelinePoint, t: number): TimelinePoint {
  return {
    lat: a.lat + (b.lat - a.lat) * t,
    lon: a.lon + (b.lon - a.lon) * t,
    lonUnwrapped: a.lonUnwrapped + (b.lonUnwrapped - a.lonUnwrapped) * t,
    elevM: interpolateNullable(a.elevM, b.elevM, t),
    cumDistM: a.cumDistM + (b.cumDistM - a.cumDistM) * t,
    globalDistM: a.globalDistM + (b.globalDistM - a.globalDistM) * t,
    speedMps: a.speedMps + (b.speedMps - a.speedMps) * t,
    gradePct: interpolateNullable(a.gradePct, b.gradePct, t),
    hrBpm: interpolateNullable(a.hrBpm, b.hrBpm, t),
    watts: interpolateNullable(a.watts, b.watts, t),
    elapsedS: a.elapsedS + (b.elapsedS - a.elapsedS) * t,
    globalElapsedS: a.globalElapsedS + (b.globalElapsedS - a.globalElapsedS) * t,
    tsUtcMs:
      a.tsUtcMs !== null && b.tsUtcMs !== null
        ? Math.round(a.tsUtcMs + (b.tsUtcMs - a.tsUtcMs) * t)
        : a.tsUtcMs ?? b.tsUtcMs,
    segmentIdx: a.segmentIdx,
  };
}

const COLOR_SCALES: Record<Exclude<ColorByMode, "stage">, readonly [number, string, string][]> = {
  grade: [
    [0.0, "#38BDF8", "<0%"],
    [3.0, "#22C55E", "0-3%"],
    [6.0, "#EAB308", "3-6%"],
    [9.0, "#F97316", "6-9%"],
    [12.0, "#EF4444", "9-12%"],
    [Number.POSITIVE_INFINITY, "#A855F7", "12%+"],
  ],
  speed: [
    [3.0, "#3B82F6", "<11 km/h"],
    [5.5, "#14B8A6", "11-20 km/h"],
    [8.0, "#22C55E", "20-29 km/h"],
    [11.0, "#F59E0B", "29-40 km/h"],
    [Number.POSITIVE_INFINITY, "#EF4444", "40+ km/h"],
  ],
  hr: [
    [120.0, "#38BDF8", "Z1 <120"],
    [140.0, "#22C55E", "Z2 120-140"],
    [160.0, "#EAB308", "Z3 140-160"],
    [175.0, "#F97316", "Z4 160-175"],
    [Number.POSITIVE_INFINITY, "#EF4444", "Z5 175+"],
  ],
  watts: [
    [150.0, "#38BDF8", "<150W"],
    [220.0, "#22C55E", "150-220W"],
    [290.0, "#EAB308", "220-290W"],
    [360.0, "#F97316", "290-360W"],
    [Number.POSITIVE_INFINITY, "#EF4444", "360W+"],
  ],
};

export function computeColorLegendScale(colorBy: ColorByMode): ColorLegendStop[] {
  if (colorBy === "stage") {
    return [];
  }
  const scale = COLOR_SCALES[colorBy];
  const stops: ColorLegendStop[] = [];
  let prev = Number.NEGATIVE_INFINITY;
  for (const [upper, hexColor, label] of scale) {
    stops.push({
      color: hexColor,
      rgb: hexToRgbTuple(hexColor, [249, 115, 22]),
      label,
      minVal: Number.isFinite(prev) ? prev : null,
      maxVal: Number.isFinite(upper) ? upper : null,
    });
    prev = upper;
  }
  return stops;
}

function classifyPointColorRgb(
  pt: TimelinePoint,
  colorBy: ColorByMode,
  fallbackRgb: [number, number, number],
): [number, number, number] {
  if (colorBy === "stage") {
    return fallbackRgb;
  }
  let raw: number | null | undefined = null;
  if (colorBy === "grade") raw = pt.gradePct;
  else if (colorBy === "speed") raw = pt.speedMps;
  else if (colorBy === "hr") raw = pt.hrBpm;
  else if (colorBy === "watts") raw = pt.watts;

  if (raw === null || raw === undefined || !Number.isFinite(raw)) {
    return fallbackRgb;
  }
  for (const [upper, hexColor] of COLOR_SCALES[colorBy]) {
    if (raw < upper || !Number.isFinite(upper)) {
      return hexToRgbTuple(hexColor, fallbackRgb);
    }
  }
  return fallbackRgb;
}

function rgbEqual(a: [number, number, number], b: [number, number, number]): boolean {
  return a[0] === b[0] && a[1] === b[1] && a[2] === b[2];
}

function sliceStageColorByPaths(
  stage: TimelineStage,
  minMetric: number,
  maxMetric: number,
  useTimeMetric: boolean,
  colorBy: ColorByMode,
  fallbackRgb: [number, number, number],
): VisiblePathSegment[] {
  const pts = stage.points;
  if (pts.length === 0) {
    return [];
  }
  const metricOf = (p: TimelinePoint) => (useTimeMetric ? p.globalElapsedS : p.globalDistM);
  const clipped: { coord: [number, number]; rgb: [number, number, number]; segIdx: number }[] = [];

  for (let i = 0; i < pts.length; i++) {
    const p = pts[i]!;
    const m = metricOf(p);
    if (i > 0) {
      const prev = pts[i - 1]!;
      if (prev.segmentIdx === p.segmentIdx) {
        const prevM = metricOf(prev);
        const span = m - prevM;
        if (span > 0) {
          if (prevM < minMetric && m >= minMetric) {
            const entryPt = interpolatePoint(prev, p, clamp01((minMetric - prevM) / span));
            clipped.push({
              coord: [entryPt.lonUnwrapped, entryPt.lat],
              rgb: classifyPointColorRgb(entryPt, colorBy, fallbackRgb),
              segIdx: p.segmentIdx,
            });
          }
          if (prevM <= maxMetric && m > maxMetric) {
            const exitPt = interpolatePoint(prev, p, clamp01((maxMetric - prevM) / span));
            clipped.push({
              coord: [exitPt.lonUnwrapped, exitPt.lat],
              rgb: classifyPointColorRgb(exitPt, colorBy, fallbackRgb),
              segIdx: p.segmentIdx,
            });
            break;
          }
        }
      }
    }
    if (m >= minMetric && m <= maxMetric) {
      clipped.push({
        coord: [p.lonUnwrapped, p.lat],
        rgb: classifyPointColorRgb(p, colorBy, fallbackRgb),
        segIdx: p.segmentIdx,
      });
    }
  }

  if (clipped.length === 0) {
    return [];
  }
  if (clipped.length === 1) {
    const only = clipped[0]!;
    return [
      {
        id: `${stage.id}-cb-0`,
        stageId: stage.id,
        color: only.rgb,
        path: [only.coord, only.coord],
      },
    ];
  }

  const out: VisiblePathSegment[] = [];
  let curPath: [number, number][] = [clipped[0]!.coord];
  let curColor = clipped[0]!.rgb;
  let curSegIdx = clipped[0]!.segIdx;
  let outIdx = 0;

  for (let i = 1; i < clipped.length; i++) {
    const item = clipped[i]!;
    if (item.segIdx !== curSegIdx) {
      if (curPath.length >= 2) {
        out.push({
          id: `${stage.id}-cb-${outIdx++}`,
          stageId: stage.id,
          color: curColor,
          path: curPath,
        });
      }
      curPath = [item.coord];
      curColor = item.rgb;
      curSegIdx = item.segIdx;
      continue;
    }

    if (!rgbEqual(item.rgb, curColor)) {
      // Overlap boundary vertex so adjacent colored ribbons connect seamlessly
      curPath.push(item.coord);
      if (curPath.length >= 2) {
        out.push({
          id: `${stage.id}-cb-${outIdx++}`,
          stageId: stage.id,
          color: curColor,
          path: curPath,
        });
      }
      curPath = [clipped[i - 1]!.coord, item.coord];
      curColor = item.rgb;
    } else {
      curPath.push(item.coord);
    }
  }

  if (curPath.length >= 2) {
    out.push({
      id: `${stage.id}-cb-${outIdx}`,
      stageId: stage.id,
      color: curColor,
      path: curPath,
    });
  }
  return out;
}

export const TRANSFER_ARC_MIN_GAP_M = 2000;

function buildArcWaypoints(
  from: readonly [number, number],
  to: readonly [number, number],
  steps = 16,
): [number, number][] {
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const chord = Math.hypot(dx, dy);
  const nx = chord > 1e-9 ? -dy / chord : 0;
  const ny = chord > 1e-9 ? dx / chord : 0;
  const bulge = Math.min(2.5, chord * 0.16);
  const pts: [number, number][] = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const arch = Math.sin(Math.PI * t) * bulge;
    pts.push([from[0] + dx * t + nx * arch, from[1] + dy * t + ny * arch]);
  }
  return pts;
}

/**
 * Detects >2km geographic gaps between consecutive non-empty stages.
 */
export function detectTransferArcs(stages: readonly TimelineStage[]): TransferArcSegment[] {
  const nonEmpty = stages.filter((s) => s.points.length > 0);
  const arcs: TransferArcSegment[] = [];
  for (let i = 0; i < nonEmpty.length - 1; i++) {
    const prev = nonEmpty[i]!;
    const next = nonEmpty[i + 1]!;
    const lastPt = prev.points[prev.points.length - 1]!;
    const firstPt = next.points[0]!;
    const gapM = haversineMeters(lastPt.lonUnwrapped, lastPt.lat, firstPt.lonUnwrapped, firstPt.lat);
    if (gapM > TRANSFER_ARC_MIN_GAP_M) {
      const fromPosition: [number, number] = [lastPt.lonUnwrapped, lastPt.lat];
      const toPosition: [number, number] = [firstPt.lonUnwrapped, firstPt.lat];
      arcs.push({
        id: `transfer-${prev.id}-${next.id}`,
        fromStageId: prev.id,
        toStageId: next.id,
        fromPosition,
        toPosition,
        distanceM: gapM,
        reached: false,
        active: false,
        waypoints: buildArcWaypoints(fromPosition, toPosition),
      });
    }
  }
  return arcs;
}

/**
 * Centered rolling average smoother for TelemetryChartPoint series (`windowSize` 1..31).
 */
export function smoothTelemetryChartPoints(
  points: readonly TelemetryChartPoint[],
  windowSize: number,
): TelemetryChartPoint[] {
  const w = Math.min(31, Math.max(1, Math.round(Number.isFinite(windowSize) ? windowSize : 1)));
  if (w <= 1 || points.length <= 2) {
    return [...points];
  }
  const half = Math.floor(w / 2);

  const avgField = (idx: number, getter: (p: TelemetryChartPoint) => number | null | undefined): number | null => {
    const centerVal = getter(points[idx]!);
    if (centerVal === null || centerVal === undefined || !Number.isFinite(centerVal)) {
      return null;
    }
    const lo = Math.max(0, idx - half);
    const hi = Math.min(points.length - 1, idx + half);
    let sum = 0;
    let count = 0;
    for (let j = lo; j <= hi; j++) {
      const v = getter(points[j]!);
      if (v !== null && v !== undefined && Number.isFinite(v)) {
        sum += v;
        count += 1;
      }
    }
    return count > 0 ? sum / count : centerVal;
  };

  return points.map((pt, idx) => ({
    cum_dist_m: pt.cum_dist_m,
    elev_m: avgField(idx, (p) => p.elev_m),
    speed_mps: avgField(idx, (p) => p.speed_mps) ?? pt.speed_mps,
    grade_pct: avgField(idx, (p) => p.grade_pct),
    hr_bpm: avgField(idx, (p) => p.hr_bpm),
    watts: avgField(idx, (p) => p.watts),
  }));
}

/**
 * Maps stage-local climb highlights onto global timeline [0, 1] progress.
 */
export function mapClimbsToTimelineProgress(
  climbs: readonly (ClimbHighlightOut | ManifestClimb)[],
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
  progressRatio = 0,
): ClimbProgressMarker[] {
  const nonEmpty = stages.filter((s) => s.points.length > 0);
  if (nonEmpty.length === 0 || climbs.length === 0) {
    return [];
  }

  let totalDistM = 0;
  let totalElapsedS = 0;
  const stageBounds = new Map<
    string,
    {
      startDist: number;
      spanDist: number;
      startElapsed: number;
      spanElapsed: number;
    }
  >();

  for (const st of nonEmpty) {
    const first = st.points[0]!;
    const last = st.points[st.points.length - 1]!;
    const spanDist = Math.max(1, last.globalDistM - first.globalDistM, st.distanceM);
    const spanElapsed = Math.max(1, last.globalElapsedS - first.globalElapsedS, st.elapsedSeconds);
    stageBounds.set(st.id, {
      startDist: first.globalDistM,
      spanDist,
      startElapsed: first.globalElapsedS,
      spanElapsed,
    });
    totalDistM = Math.max(totalDistM, last.globalDistM);
    totalElapsedS = Math.max(totalElapsedS, last.globalElapsedS);
  }

  const allHaveTs = nonEmpty.every((s) => s.hasTimestamps);
  const useTime = spec.timeline_mode === "recorded-time" && allHaveTs && totalElapsedS > 0;
  const denom = useTime ? Math.max(1, totalElapsedS) : Math.max(1, totalDistM);
  const currentProgress = clamp01(progressRatio);

  return climbs
    .map((climb) => {
      const sb = stageBounds.get(climb.stage_id);
      if (!sb) {
        const sp = clamp01(climb.start_progress);
        const ep = clamp01(climb.end_progress);
        const smp = clamp01(climb.summit_progress);
        return {
          id: climb.id,
          stageId: climb.stage_id,
          name: climb.name,
          category: climb.category,
          startDistM: climb.start_dist_m,
          endDistM: climb.end_dist_m,
          startProgress: sp,
          endProgress: ep,
          summitProgress: smp,
          summitPosition: [climb.summit_lon, climb.summit_lat] as [number, number],
          summitElevM: climb.summit_elev_m,
          distanceM: climb.distance_m,
          elevationGainM: climb.elevation_gain_m,
          avgGradePct: climb.avg_grade_pct,
          maxGradePct: climb.max_grade_pct,
          fietsScore: climb.fiets_score,
          reached: currentProgress >= smp - 1e-6,
          active: currentProgress >= sp && currentProgress <= ep,
        };
      }

      const startProgress = useTime
        ? clamp01((sb.startElapsed + clamp01(climb.start_progress) * sb.spanElapsed) / denom)
        : clamp01((sb.startDist + climb.start_dist_m) / denom);
      const endProgress = useTime
        ? clamp01((sb.startElapsed + clamp01(climb.end_progress) * sb.spanElapsed) / denom)
        : clamp01((sb.startDist + Math.max(climb.end_dist_m, climb.start_dist_m)) / denom);
      const summitProgress = useTime
        ? clamp01((sb.startElapsed + clamp01(climb.summit_progress) * sb.spanElapsed) / denom)
        : clamp01((sb.startDist + clamp01(climb.summit_progress) * sb.spanDist) / denom);

      return {
        id: climb.id,
        stageId: climb.stage_id,
        name: climb.name,
        category: climb.category,
        startDistM: sb.startDist + climb.start_dist_m,
        endDistM: sb.startDist + climb.end_dist_m,
        startProgress,
        endProgress,
        summitProgress,
        summitPosition: [climb.summit_lon, climb.summit_lat] as [number, number],
        summitElevM: climb.summit_elev_m,
        distanceM: climb.distance_m,
        elevationGainM: climb.elevation_gain_m,
        avgGradePct: climb.avg_grade_pct,
        maxGradePct: climb.max_grade_pct,
        fietsScore: climb.fiets_score,
        reached: spec.visibility_mode === "full" || currentProgress >= summitProgress - 1e-6,
        active: currentProgress >= startProgress && currentProgress <= endProgress,
      };
    })
    .sort((a, b) => a.summitProgress - b.summitProgress);
}

function sliceStagePath(
  stage: TimelineStage,
  minMetric: number,
  maxMetric: number,
  useTimeMetric: boolean,
): [number, number][][] {
  const pts = stage.points;
  if (pts.length === 0) {
    return [];
  }
  const metricOf = (p: TimelinePoint) => (useTimeMetric ? p.globalElapsedS : p.globalDistM);

  const segments: [number, number][][] = [];
  let currentSeg: [number, number][] = [];
  let currentSegIdx = pts[0]!.segmentIdx;

  const flushSeg = () => {
    if (currentSeg.length >= 2) {
      segments.push(currentSeg);
    } else if (currentSeg.length === 1) {
      segments.push([currentSeg[0]!, currentSeg[0]!]);
    }
    currentSeg = [];
  };

  for (let i = 0; i < pts.length; i++) {
    const p = pts[i]!;
    const m = metricOf(p);
    if (p.segmentIdx !== currentSegIdx) {
      flushSeg();
      currentSegIdx = p.segmentIdx;
    }

    if (i > 0) {
      const prev = pts[i - 1]!;
      if (prev.segmentIdx === p.segmentIdx) {
        const prevM = metricOf(prev);
        const span = m - prevM;
        if (span > 0) {
          // Entry interpolation at minMetric
          if (prevM < minMetric && m >= minMetric) {
            const tEntry = clamp01((minMetric - prevM) / span);
            const entryPt = interpolatePoint(prev, p, tEntry);
            currentSeg.push([entryPt.lonUnwrapped, entryPt.lat]);
          }
          // Exit interpolation at maxMetric
          if (prevM <= maxMetric && m > maxMetric) {
            const tExit = clamp01((maxMetric - prevM) / span);
            const exitPt = interpolatePoint(prev, p, tExit);
            currentSeg.push([exitPt.lonUnwrapped, exitPt.lat]);
            flushSeg();
            continue;
          }
        }
      }
    }

    if (m >= minMetric && m <= maxMetric) {
      currentSeg.push([p.lonUnwrapped, p.lat]);
    }
  }
  flushSeg();
  return segments;
}

function computeVamMPerH(
  samples: readonly FlatSample[],
  lo: number,
  activePoint: TimelinePoint,
  activeStageIndex: number,
): number | null {
  if (activePoint.elevM === null && activePoint.gradePct === null) {
    return null;
  }
  const backIdx = Math.max(0, lo - 3);
  const fwdIdx = Math.min(samples.length - 1, lo + 3);
  const sA = samples[backIdx];
  const sB = samples[fwdIdx];
  if (
    sA &&
    sB &&
    sA.stageIndex === activeStageIndex &&
    sB.stageIndex === activeStageIndex &&
    sA.point.elevM !== null &&
    sB.point.elevM !== null
  ) {
    const dt = sB.point.elapsedS - sA.point.elapsedS;
    if (dt > 0.5) {
      const dElev = sB.point.elevM - sA.point.elevM;
      return dElev > 0 ? (dElev / dt) * 3600 : 0;
    }
  }
  if (activePoint.gradePct !== null && Number.isFinite(activePoint.gradePct) && activePoint.speedMps > 0) {
    return activePoint.gradePct > 0
      ? (activePoint.gradePct / 100) * activePoint.speedMps * 3600
      : 0;
  }
  return null;
}

function computeChapterCardState(
  nonEmptyStages: readonly TimelineStage[],
  activeStage: TimelineStage | null,
  activeStageIndex: number,
  targetMetric: number,
  minMetric: number,
  maxMetric: number,
  useTimeMetric: boolean,
  spec: RenderSpecV2,
  durationS: number,
  isPreview: boolean,
): ChapterCardState | null {
  if (!spec.show_chapter_cards || !activeStage) {
    return null;
  }
  const stageFirstPt = activeStage.points[0];
  if (!stageFirstPt) {
    return null;
  }
  const metricSpan = maxMetric - minMetric;
  if (metricSpan <= 0) {
    return null;
  }
  const holdScale =
    isPreview && spec.duration_seconds > 0
      ? Math.min(1, durationS / spec.duration_seconds)
      : 1;
  const introHold = Math.min(spec.intro_hold_seconds * holdScale, durationS * 0.35);
  const outroHold = Math.min(spec.outro_hold_seconds * holdScale, durationS * 0.35);
  const activeDurationS = Math.max(1, durationS - introHold - outroHold);

  const stageStartMetric = useTimeMetric ? stageFirstPt.globalElapsedS : stageFirstPt.globalDistM;
  const stageStartProgress = clamp01((stageStartMetric - minMetric) / metricSpan);
  const currentProgress = clamp01((targetMetric - minMetric) / metricSpan);

  const prevStage = activeStageIndex > 0 ? nonEmptyStages[activeStageIndex - 1] : null;
  const isEntryBoundary = activeStageIndex === 0 || (prevStage && activeStage.dayIndex >= prevStage.dayIndex);
  if (!isEntryBoundary) {
    return null;
  }

  const cardDurationS = 1.5;
  const elapsedInCardS = (currentProgress - stageStartProgress) * activeDurationS;
  if (elapsedInCardS < 0 || elapsedInCardS > cardDurationS) {
    return null;
  }
  const fade = Math.min(0.25, cardDurationS * 0.25);
  let opacity = 1;
  if (elapsedInCardS < fade) {
    opacity = clamp01(elapsedInCardS / fade);
  } else if (elapsedInCardS > cardDurationS - fade) {
    opacity = clamp01((cardDurationS - elapsedInCardS) / fade);
  }

  return {
    stageId: activeStage.id,
    dayIndex: activeStage.dayIndex,
    title: activeStage.name,
    distanceM: activeStage.distanceM,
    elevationGainM: activeStage.elevationGainM,
    activityType: activeStage.activityType,
    opacity,
    elapsedInCardS,
  };
}

export function evaluateTimeline(
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
  progressRatio: number,
  options?: { durationS?: number; isPreview?: boolean },
): EvaluatedTimelineState {
  const progress = clamp01(progressRatio);
  const bounds = computeTimelineBounds(stages);
  const nonEmptyStages = stages.filter((s) => s.points.length > 0);
  const colorBy = spec.color_by ?? "stage";
  const colorLegendStops = computeColorLegendScale(colorBy);

  if (nonEmptyStages.length === 0) {
    const centerLon = (bounds[0] + bounds[2]) / 2;
    const centerLat = (bounds[1] + bounds[3]) / 2;
    return {
      progressRatio: progress,
      activeStageIndex: 0,
      activeStage: null,
      markerPosition: [centerLon, centerLat],
      previousPosition: [centerLon, centerLat],
      elevM: null,
      speedMps: 0,
      gradePct: null,
      hrBpm: null,
      watts: null,
      vamMPerH: null,
      distanceTravelledM: 0,
      distanceRemainingM: 0,
      totalDistanceM: 0,
      elapsedSeconds: 0,
      currentTsUtcMs: null,
      visiblePaths: [],
      overnightStops: [],
      transferArcs: [],
      chapterCard: null,
      colorLegendStops,
      bounds,
    };
  }

  const allSamples: FlatSample[] = [];
  let maxGlobalDist = 0;
  let maxGlobalElapsed = 0;

  nonEmptyStages.forEach((stage, stageIndex) => {
    for (const pt of stage.points) {
      allSamples.push({ stageIndex, point: pt, metricValue: 0 });
      if (pt.globalDistM > maxGlobalDist) maxGlobalDist = pt.globalDistM;
      if (pt.globalElapsedS > maxGlobalElapsed) maxGlobalElapsed = pt.globalElapsedS;
    }
  });

  const allStagesHaveTimestamps = nonEmptyStages.every((s) => s.hasTimestamps);
  const useTimeMetric =
    spec.timeline_mode === "recorded-time" && allStagesHaveTimestamps && maxGlobalElapsed > 0;
  for (const s of allSamples) {
    s.metricValue = useTimeMetric ? s.point.globalElapsedS : s.point.globalDistM;
  }

  const minMetric = allSamples[0]!.metricValue;
  const maxMetric = allSamples[allSamples.length - 1]!.metricValue;
  const targetMetric = minMetric + (maxMetric - minMetric) * progress;

  let lo = 0;
  let hi = allSamples.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if ((allSamples[mid]?.metricValue ?? 0) <= targetMetric) {
      lo = mid;
    } else {
      hi = mid;
    }
  }

  const sampleA = allSamples[lo]!;
  const sampleB = allSamples[hi]!;
  const span = sampleB.metricValue - sampleA.metricValue;
  const sameStageAndSeg =
    sampleA.stageIndex === sampleB.stageIndex &&
    sampleA.point.segmentIdx === sampleB.point.segmentIdx;
  const t = sameStageAndSeg && span > 0 ? clamp01((targetMetric - sampleA.metricValue) / span) : 0;

  const activePoint = sameStageAndSeg ? interpolatePoint(sampleA.point, sampleB.point, t) : sampleA.point;
  const activeStageIndex = sampleA.stageIndex;
  const activeStage = nonEmptyStages[activeStageIndex] ?? null;

  // Smoothed lookback point for deterministic camera bearing hints
  let prevIdx = Math.max(0, lo - 1);
  for (let back = 1; back <= 6 && lo - back >= 0; back++) {
    const candidate = allSamples[lo - back]!;
    if (candidate.stageIndex !== sampleA.stageIndex) break;
    prevIdx = lo - back;
    if (activePoint.cumDistM - candidate.point.cumDistM >= 25) break;
  }
  const prevPt = allSamples[prevIdx]?.point ?? activePoint;

  // Visible path calculation based on visibility_mode and color_by
  const durationS = options?.durationS ?? spec.duration_seconds;
  const isPreview = options?.isPreview ?? spec.is_preview;
  const trailDenominator = isPreview
    ? Math.max(spec.duration_seconds, durationS, 1)
    : Math.max(durationS, 1);
  const effectiveTrailFraction = clamp01(spec.trail_duration_seconds / trailDenominator);
  const windowMinMetric =
    spec.visibility_mode === "full"
      ? minMetric
      : spec.visibility_mode === "traversed"
        ? minMetric
        : Math.max(minMetric, targetMetric - (maxMetric - minMetric) * effectiveTrailFraction);
  const windowMaxMetric = spec.visibility_mode === "full" ? maxMetric : targetMetric;

  const visiblePaths: VisiblePathSegment[] = [];
  nonEmptyStages.forEach((stage) => {
    const fallbackRgb = hexToRgbTuple(
      spec.stage_colors[stage.id] ?? stage.color ?? spec.track_color,
      [56, 189, 248],
    );
    if (colorBy !== "stage") {
      const colored = sliceStageColorByPaths(
        stage,
        windowMinMetric,
        windowMaxMetric,
        useTimeMetric,
        colorBy,
        fallbackRgb,
      );
      visiblePaths.push(...colored);
    } else {
      const sliced = sliceStagePath(stage, windowMinMetric, windowMaxMetric, useTimeMetric);
      sliced.forEach((path, segIndex) => {
        visiblePaths.push({
          id: `${stage.id}-seg-${segIndex}`,
          stageId: stage.id,
          color: fallbackRgb,
          path,
        });
      });
    }
  });

  // Overnight / stage transition stops
  const overnightStops: StopMarker[] = [];
  if (spec.show_overnight_stops) {
    nonEmptyStages.forEach((stage, idx) => {
      const transition = spec.stage_transitions[stage.id] ?? stage.transition ?? "none";
      const nextStage = nonEmptyStages[idx + 1];
      const isDayBoundary = nextStage !== undefined && nextStage.dayIndex > stage.dayIndex;
      if (transition !== "none" || isDayBoundary) {
        const lastPt = stage.points[stage.points.length - 1]!;
        const stopMetric = useTimeMetric ? lastPt.globalElapsedS : lastPt.globalDistM;
        overnightStops.push({
          stageId: stage.id,
          name: stage.name,
          dayIndex: stage.dayIndex,
          transition: transition !== "none" ? transition : "bed",
          position: [lastPt.lonUnwrapped, lastPt.lat],
          reached: spec.visibility_mode === "full" || targetMetric >= stopMetric - 1e-6,
        });
      }
    });
  }

  // >2km inter-stage transfer arcs
  const transferArcs: TransferArcSegment[] = [];
  if (spec.show_transfer_arcs !== false) {
    const rawArcs = detectTransferArcs(nonEmptyStages);
    const metricByStageId = new Map<string, { startMetric: number; endMetric: number }>();
    for (const st of nonEmptyStages) {
      const firstPt = st.points[0]!;
      const lastPt = st.points[st.points.length - 1]!;
      metricByStageId.set(st.id, {
        startMetric: useTimeMetric ? firstPt.globalElapsedS : firstPt.globalDistM,
        endMetric: useTimeMetric ? lastPt.globalElapsedS : lastPt.globalDistM,
      });
    }
    for (const arc of rawArcs) {
      const fromM = metricByStageId.get(arc.fromStageId)?.endMetric ?? minMetric;
      const toM = metricByStageId.get(arc.toStageId)?.startMetric ?? fromM;
      const reached = spec.visibility_mode === "full" || targetMetric >= fromM - 1e-6;
      const active = targetMetric >= fromM - 1e-6 && targetMetric <= toM + 1e-6;
      transferArcs.push({ ...arc, reached, active });
    }
  }

  const chapterCard = computeChapterCardState(
    nonEmptyStages,
    activeStage,
    activeStageIndex,
    targetMetric,
    minMetric,
    maxMetric,
    useTimeMetric,
    spec,
    durationS,
    isPreview,
  );
  const vamMPerH = computeVamMPerH(allSamples, lo, activePoint, activeStageIndex);

  return {
    progressRatio: progress,
    activeStageIndex,
    activeStage,
    markerPosition: [activePoint.lonUnwrapped, activePoint.lat],
    previousPosition: [prevPt.lonUnwrapped, prevPt.lat],
    elevM: activePoint.elevM,
    speedMps: activePoint.speedMps,
    gradePct: activePoint.gradePct,
    hrBpm: activePoint.hrBpm ?? null,
    watts: activePoint.watts ?? null,
    vamMPerH,
    distanceTravelledM: activePoint.globalDistM,
    distanceRemainingM: Math.max(0, maxGlobalDist - activePoint.globalDistM),
    totalDistanceM: maxGlobalDist,
    elapsedSeconds: activePoint.globalElapsedS,
    currentTsUtcMs: activePoint.tsUtcMs,
    visiblePaths,
    overnightStops,
    transferArcs,
    chapterCard,
    colorLegendStops,
    bounds,
  };
}

/**
 * Unified telemetry readout sampler shared by TelemetryChart hover/playhead and StatisticsHud.
 */
export function sampleTelemetryReadoutAtProgress(
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
  progressRatio: number,
  chartPoints?: readonly TelemetryChartPoint[],
): TelemetryReadout {
  const evaluated = evaluateTimeline(stages, spec, progressRatio);
  if (chartPoints && chartPoints.length > 0) {
    const smoothed = smoothTelemetryChartPoints(chartPoints, spec.telemetry_smoothing ?? 5);
    const totalChartDist = smoothed[smoothed.length - 1]?.cum_dist_m ?? 0;
    const targetDist =
      evaluated.totalDistanceM > 0
        ? evaluated.distanceTravelledM
        : clamp01(progressRatio) * totalChartDist;
    const sampled = sampleChartAtDist(smoothed, targetDist);
    return {
      elevM: sampled.elevM ?? evaluated.elevM,
      speedMps: sampled.speedMps || evaluated.speedMps,
      gradePct: sampled.gradePct ?? evaluated.gradePct,
      hrBpm: sampled.hrBpm ?? evaluated.hrBpm ?? null,
      watts: sampled.watts ?? evaluated.watts ?? null,
      vamMPerH: evaluated.vamMPerH ?? null,
    };
  }
  return {
    elevM: evaluated.elevM,
    speedMps: evaluated.speedMps,
    gradePct: evaluated.gradePct,
    hrBpm: evaluated.hrBpm ?? null,
    watts: evaluated.watts ?? null,
    vamMPerH: evaluated.vamMPerH ?? null,
  };
}

export function computeFollowZoom(bounds: [number, number, number, number]): number {
  const lonSpan = Math.abs(bounds[2] - bounds[0]);
  const latSpan = Math.abs(bounds[3] - bounds[1]);
  const maxSpan = Math.max(lonSpan, latSpan, 0.005);
  // Dynamic follow-marker zoom: closer than fit-track so the camera tracks the athlete
  const fitApproxZoom = Math.log2(360 / maxSpan) - 1;
  return Math.min(15.2, Math.max(11.5, fitApproxZoom + 2.3));
}

export interface AdaptiveFollowCameraOptions {
  viewportWidth?: number;
  viewportHeight?: number;
  durationS?: number;
  travelDurationS?: number;
  isPreview?: boolean;
  bounds?: [number, number, number, number];
}

export interface AdaptiveFollowCameraResult {
  center: [number, number];
  zoom: number;
  velocityMps: number;
  courseBearingDeg: number | null;
}

interface IndexedTimelineSamples {
  samples: FlatSample[];
  useTimeMetric: boolean;
  minMetric: number;
  maxMetric: number;
}

function buildIndexedSamples(
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
): IndexedTimelineSamples | null {
  const nonEmptyStages = stages.filter((s) => s.points.length > 0);
  if (nonEmptyStages.length === 0) {
    return null;
  }
  const samples: FlatSample[] = [];
  let maxGlobalElapsed = 0;

  nonEmptyStages.forEach((stage, stageIndex) => {
    for (const pt of stage.points) {
      samples.push({ stageIndex, point: pt, metricValue: 0 });
      if (pt.globalElapsedS > maxGlobalElapsed) {
        maxGlobalElapsed = pt.globalElapsedS;
      }
    }
  });

  if (samples.length === 0) {
    return null;
  }

  const allStagesHaveTimestamps = nonEmptyStages.every((s) => s.hasTimestamps);
  const useTimeMetric =
    spec.timeline_mode === "recorded-time" && allStagesHaveTimestamps && maxGlobalElapsed > 0;
  for (const s of samples) {
    s.metricValue = useTimeMetric ? s.point.globalElapsedS : s.point.globalDistM;
  }

  return {
    samples,
    useTimeMetric,
    minMetric: samples[0]!.metricValue,
    maxMetric: samples[samples.length - 1]!.metricValue,
  };
}

function sampleIndexedAtProgress(
  indexed: IndexedTimelineSamples,
  progressRatio: number,
): TimelinePoint {
  const p = clamp01(progressRatio);
  const { samples, minMetric, maxMetric } = indexed;
  const targetMetric = minMetric + (maxMetric - minMetric) * p;

  let lo = 0;
  let hi = samples.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if ((samples[mid]?.metricValue ?? 0) <= targetMetric) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  const sampleA = samples[lo]!;
  const sampleB = samples[hi]!;
  const span = sampleB.metricValue - sampleA.metricValue;
  const sameStageAndSeg =
    sampleA.stageIndex === sampleB.stageIndex &&
    sampleA.point.segmentIdx === sampleB.point.segmentIdx;
  const t = sameStageAndSeg && span > 0 ? clamp01((targetMetric - sampleA.metricValue) / span) : 0;
  return sameStageAndSeg ? interpolatePoint(sampleA.point, sampleB.point, t) : sampleA.point;
}

/**
 * Samples the interpolated timeline point at any progress in [0, 1].
 */
export function sampleTimelinePointAtProgress(
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
  progressRatio: number,
): TimelinePoint | null {
  const indexed = buildIndexedSamples(stages, spec);
  if (!indexed) {
    return null;
  }
  return sampleIndexedAtProgress(indexed, progressRatio);
}

function smoothstep01(t: number): number {
  const x = clamp01(t);
  return x * x * (3 - 2 * x);
}

/**
 * Computes velocity-aware dynamic zoom, raised-cosine zoom damping,
 * asymmetric lookahead center smoothing, and circular course bearing
 * so multi-week / 21-day trips pan smoothly without tile blur.
 */
export function computeAdaptiveFollowCamera(
  stages: readonly TimelineStage[],
  spec: RenderSpecV2,
  progressRatio: number,
  options?: AdaptiveFollowCameraOptions,
): AdaptiveFollowCameraResult {
  const bounds = options?.bounds ?? computeTimelineBounds(stages);
  const indexed = buildIndexedSamples(stages, spec);
  if (!indexed) {
    return {
      center: [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2],
      zoom: computeFollowZoom(bounds),
      velocityMps: 0,
      courseBearingDeg: null,
    };
  }

  const progress = clamp01(progressRatio);
  const exactPt = sampleIndexedAtProgress(indexed, progress);
  const isPreview = options?.isPreview ?? spec.is_preview;
  const fullSpecDuration = Math.max(1, spec.duration_seconds);
  const actualDurationS = Math.max(1, options?.durationS ?? fullSpecDuration);
  const holdScale =
    isPreview && fullSpecDuration > 0 ? Math.min(1, actualDurationS / fullSpecDuration) : 1;
  const introHold = Math.min(spec.intro_hold_seconds * holdScale, actualDurationS * 0.35);
  const outroHold = Math.min(spec.outro_hold_seconds * holdScale, actualDurationS * 0.35);
  const activeActualDurationS = Math.max(0.5, actualDurationS - introHold - outroHold);

  // Exclude photo pause time from velocity estimation so pausing never causes a zoom-in spike.
  const travelFraction =
    options?.travelDurationS !== undefined && activeActualDurationS > 0
      ? Math.min(1, Math.max(0.2, options.travelDurationS / activeActualDurationS))
      : 1;
  const baseFullActive = Math.max(
    1,
    fullSpecDuration -
      Math.min(spec.intro_hold_seconds, fullSpecDuration * 0.35) -
      Math.min(spec.outro_hold_seconds, fullSpecDuration * 0.35),
  );
  const effectiveZoomDurationS = Math.max(
    1,
    (isPreview ? baseFullActive : activeActualDurationS) * travelFraction,
  );

  const estimateVelocityAt = (q: number): { vEff: number; pt: TimelinePoint } => {
    const dp = Math.min(0.06, 1.5 / effectiveZoomDurationS);
    const q0 = clamp01(q - dp);
    const q1 = clamp01(q + dp);
    const dt = Math.max(0.05, (q1 - q0) * effectiveZoomDurationS);
    const p0 = sampleIndexedAtProgress(indexed, q0);
    const p1 = sampleIndexedAtProgress(indexed, q1);
    const pt = sampleIndexedAtProgress(indexed, q);

    const chordDist = haversineMeters(p0.lonUnwrapped, p0.lat, p1.lonUnwrapped, p1.lat);
    const pathDist = Math.abs(p1.globalDistM - p0.globalDistM);
    const vChord = chordDist / dt;
    const vPath = pathDist / dt;
    const vEff = Math.max(vChord, 0.5 * vPath, 1.0);
    return { vEff, pt };
  };

  const { vEff: currentVelocity } = estimateVelocityAt(progress);

  // Compute velocity-scaled circular course bearing across nearby chord windows.
  const chordMeters = Math.max(25, currentVelocity * 0.4);
  const bearingSamples: { deg: number | null; weight: number }[] = [];
  const totalDist = Math.max(
    1,
    indexed.samples[indexed.samples.length - 1]!.point.globalDistM -
      indexed.samples[0]!.point.globalDistM,
  );
  const bearingDp = Math.min(0.05, Math.max(0.002, chordMeters / totalDist));
  const offsets = [-0.5, 0, 0.5];
  const weights = [0.25, 0.5, 0.25];
  for (let i = 0; i < offsets.length; i++) {
    const c = clamp01(progress + offsets[i]! * bearingDp);
    const a = sampleIndexedAtProgress(indexed, clamp01(c - bearingDp * 0.6));
    const b = sampleIndexedAtProgress(indexed, clamp01(c + bearingDp * 0.6));
    const deg = computeBearingDeg([a.lonUnwrapped, a.lat], [b.lonUnwrapped, b.lat]);
    bearingSamples.push({ deg, weight: weights[i]! });
  }
  const courseBearingDeg = circularMeanBearingDeg(bearingSamples);

  const zoomOffset = Number.isFinite(spec.follow_zoom_offset)
    ? Math.min(3, Math.max(-3, spec.follow_zoom_offset))
    : 0;

  // Fixed legacy zoom mode fallback when explicitly requested
  if (spec.follow_zoom_mode === "fixed") {
    const fixedZoom = Math.min(16.5, Math.max(4.5, computeFollowZoom(bounds) + zoomOffset));
    return {
      center: [exactPt.lonUnwrapped, exactPt.lat],
      zoom: fixedZoom,
      velocityMps: currentVelocity,
      courseBearingDeg,
    };
  }

  const vpW = options?.viewportWidth ?? 1920;
  const vpH = options?.viewportHeight ?? 1080;
  const vpScale = Math.min(1.25, Math.max(0.75, Math.min(vpW, vpH) / 1080));
  const baseTargetPx = Number.isFinite(spec.target_screen_px_per_sec)
    ? Math.min(500, Math.max(80, spec.target_screen_px_per_sec))
    : 180;
  const targetScreenPxPerSec = baseTargetPx * vpScale;

  const lonSpan = Math.abs(bounds[2] - bounds[0]);
  const latSpan = Math.abs(bounds[3] - bounds[1]);
  const maxSpan = Math.max(lonSpan, latSpan, 0.005);
  const fitZoom = Math.log2(360 / maxSpan) - 1;
  const minZoom = Math.max(4.5, fitZoom + 0.4);
  const maxZoom = 15.8;

  const rawZoomAt = (q: number): number => {
    const { vEff, pt } = estimateVelocityAt(q);
    const reqMetersPerPx = vEff / targetScreenPxPerSec;
    const cosLat = Math.max(0.15, Math.cos((pt.lat * Math.PI) / 180));
    const metersPerWorldPx = (40_075_016.686 * cosLat) / (512 * reqMetersPerPx);
    const zRaw = Math.log2(Math.max(1e-6, metersPerWorldPx)) + zoomOffset;
    return Math.min(maxZoom, Math.max(minZoom, zRaw));
  };

  // 15-tap raised-cosine damping (+/- 1.8s) to prevent sudden zoom snaps between fast & slow days
  const zoomHalfWinP = Math.min(0.12, 1.8 / effectiveZoomDurationS);
  let zoomSum = 0;
  let zoomWeightSum = 0;
  for (let k = -7; k <= 7; k++) {
    const u = k / 7;
    const w = 0.54 + 0.46 * Math.cos(Math.PI * u);
    const q = clamp01(progress + u * zoomHalfWinP);
    zoomSum += rawZoomAt(q) * w;
    zoomWeightSum += w;
  }
  const dampedZoom = zoomWeightSum > 0 ? zoomSum / zoomWeightSum : rawZoomAt(progress);

  // Asymmetric 11-tap lookahead center smoothing ([-0.6s, +1.0s]), tapered to 0 at trip endpoints
  const dtBeforeP = Math.min(0.08, 0.6 / effectiveZoomDurationS);
  const dtAfterP = Math.min(0.12, 1.0 / effectiveZoomDurationS);
  const edgeDist = Math.min(progress, 1 - progress);
  const endpointBlend = smoothstep01(edgeDist / 0.08);

  let lonSum = 0;
  let latSum = 0;
  let posWeightSum = 0;
  for (let k = 0; k <= 10; k++) {
    const u = k / 10; // 0..1 spanning [-dtBeforeP, +dtAfterP]
    const offsetP = -dtBeforeP + (dtBeforeP + dtAfterP) * u;
    const w = Math.pow(Math.sin(Math.PI * ((u + 0.06) / 1.12)), 2);
    const samplePt = sampleIndexedAtProgress(indexed, clamp01(progress + offsetP));
    lonSum += samplePt.lonUnwrapped * w;
    latSum += samplePt.lat * w;
    posWeightSum += w;
  }
  const smoothedLon = posWeightSum > 0 ? lonSum / posWeightSum : exactPt.lonUnwrapped;
  const smoothedLat = posWeightSum > 0 ? latSum / posWeightSum : exactPt.lat;

  const center: [number, number] = [
    exactPt.lonUnwrapped + (smoothedLon - exactPt.lonUnwrapped) * endpointBlend,
    exactPt.lat + (smoothedLat - exactPt.lat) * endpointBlend,
  ];

  return {
    center,
    zoom: dampedZoom,
    velocityMps: currentVelocity,
    courseBearingDeg,
  };
}
