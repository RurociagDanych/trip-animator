import type {
  ColorByMode,
  PanelPosition,
  PhotoCardStyle,
  RenderSpecV2,
  StatisticsPanelConfig,
} from "../api/types";
import type {
  ClimbProgressMarker,
  ColorLegendStop,
  EvaluatedTimelineState,
  TimelineStage,
} from "./timeline";

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ViewportPadding {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export type HudSlotId =
  | "top-center"
  | "top-left"
  | "top-right"
  | "bottom-center"
  | "bottom-left"
  | "bottom-right"
  | "left-center"
  | "right-center";

export interface ScreenProjection {
  x: number;
  y: number;
  visible: boolean;
}

export interface ClimbCalloutSlot {
  id: string;
  rect: Rect;
  anchorX: number;
  anchorY: number;
}

export interface StageLayoutSolution {
  hud: {
    rect: Rect;
    slot: HudSlotId;
    scale: number;
  } | null;
  elevation: {
    rect: Rect;
    anchorLeft: boolean;
  } | null;
  colorLegend: {
    rect: Rect;
  } | null;
  photoCard: {
    rect: Rect;
    corner: "top-right" | "bottom-right" | "top-left" | "bottom-left";
    style: PhotoCardStyle;
  } | null;
  chapterCard: {
    rect: Rect;
  } | null;
  finalePoster: {
    rect: Rect;
  } | null;
  climbCallouts: ClimbCalloutSlot[];
  watermarkRect: Rect | null;
  fitPadding: ViewportPadding;
  followPadding: ViewportPadding;
}

const EDGE_MARGIN = 12;
const DEG2RAD = Math.PI / 180;
// MapLibre standard vertical FOV ~36.87 deg -> cameraToCenterDistance = 1.5 * H
const CAMERA_DISTANCE_FACTOR = 1.5;

export function rectsIntersect(a: Rect, b: Rect, gap = 0): boolean {
  return !(
    a.x + a.width + gap <= b.x ||
    b.x + b.width + gap <= a.x ||
    a.y + a.height + gap <= b.y ||
    b.y + b.height + gap <= a.y
  );
}

export function rectIntersectionArea(a: Rect, b: Rect): number {
  const xOverlap = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x));
  const yOverlap = Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
  return xOverlap * yOverlap;
}

export function clampRectToViewport(
  rect: Rect,
  viewportW: number,
  viewportH: number,
  margin = EDGE_MARGIN,
): Rect {
  const maxX = Math.max(margin, viewportW - margin - rect.width);
  const maxY = Math.max(margin, viewportH - margin - rect.height);
  return {
    x: Math.round(Math.min(maxX, Math.max(margin, rect.x))),
    y: Math.round(Math.min(maxY, Math.max(margin, rect.y))),
    width: rect.width,
    height: rect.height,
  };
}

function roundToStep(value: number, step = 4): number {
  return Math.ceil(value / step) * step;
}

/**
 * Pure Web Mercator + clockwise bearing + perspective pitch projection
 * matching MapLibre 512px tile scale and asymmetric viewport padding.
 */
export function projectLngLatToScreen(
  lngLat: readonly [number, number],
  cameraCenter: readonly [number, number],
  zoom: number,
  pitchDeg: number,
  bearingDeg: number,
  viewportW: number,
  viewportH: number,
  padding: ViewportPadding = { top: 0, bottom: 0, left: 0, right: 0 },
): ScreenProjection {
  const [lon, latRaw] = lngLat;
  const [cLon, cLatRaw] = cameraCenter;
  if (
    !Number.isFinite(lon) ||
    !Number.isFinite(latRaw) ||
    !Number.isFinite(cLon) ||
    !Number.isFinite(cLatRaw) ||
    !Number.isFinite(zoom)
  ) {
    return { x: viewportW / 2, y: viewportH / 2, visible: false };
  }

  const clampLat = (v: number) => Math.min(85.05112878, Math.max(-85.05112878, v));
  const toWorld = (lo: number, la: number): [number, number] => {
    const sinLat = Math.sin(clampLat(la) * DEG2RAD);
    const wx = (lo + 180) / 360;
    const wy = 0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI);
    return [wx, wy];
  };

  const worldSize = 512 * Math.pow(2, zoom);
  const [wx, wy] = toWorld(lon, latRaw);
  const [cx, cy] = toWorld(cLon, cLatRaw);

  const dx = (wx - cx) * worldSize;
  const dy = (wy - cy) * worldSize;

  const bRad = -bearingDeg * DEG2RAD;
  const cosB = Math.cos(bRad);
  const sinB = Math.sin(bRad);
  const rx = dx * cosB - dy * sinB;
  const ry = dx * sinB + dy * cosB;

  const pitchRad = Math.min(60, Math.max(0, pitchDeg)) * DEG2RAD;
  const camDist = CAMERA_DISTANCE_FACTOR * Math.max(1, viewportH);
  const depth = camDist - ry * Math.sin(pitchRad);
  if (depth <= 10) {
    return { x: viewportW / 2, y: -viewportH, visible: false };
  }

  const scale = camDist / depth;
  const centerX = padding.left + (viewportW - padding.left - padding.right) / 2;
  const centerY = padding.top + (viewportH - padding.top - padding.bottom) / 2;
  const sx = centerX + rx * scale;
  const sy = centerY + ry * Math.cos(pitchRad) * scale;

  return {
    x: sx,
    y: sy,
    visible: sx >= -120 && sx <= viewportW + 120 && sy >= -120 && sy <= viewportH + 120,
  };
}

export interface MeasuredHudSize {
  width: number;
  height: number;
  orientation?: "horizontal" | "vertical";
}

/**
 * Deterministic analytical footprint estimator for StatisticsHud,
 * quantized to 4px increments so fitBounds boundsKey does not jitter on digit changes.
 * Clamps effectiveScale so tall vertical rails fit inside shorter canvases without clipping.
 */
export function estimateStatisticsHudSize(
  config: StatisticsPanelConfig,
  compact: boolean,
  hasActivityChip: boolean,
  viewportW = 1920,
  measuredUnscaled?: MeasuredHudSize | null,
  viewportH = 1080,
): { width: number; height: number; scale: number } {
  const baseScale = compact ? Math.min(config.scale, 0.85) : config.scale;
  const valueFontSize = Math.round(config.text_size * (compact ? 0.62 : 0.78));
  const labelFontSize = Math.max(9, Math.round(valueFontSize * 0.56));
  const isHorizontal = config.position === "top" || config.position === "bottom";
  const orientation: "horizontal" | "vertical" = isHorizontal ? "horizontal" : "vertical";

  let unscaledW: number;
  let unscaledH: number;

  if (isHorizontal) {
    const metricWidth = Math.max(66, Math.round(valueFontSize * 4.4));
    const chipWidth = hasActivityChip ? 88 : 0;
    const rawContentW =
      32 + chipWidth + (hasActivityChip ? 20 : 0) + config.metrics.length * metricWidth + Math.max(0, config.metrics.length - 1) * 20;
    const maxUnscaledW = Math.max(220, (viewportW - EDGE_MARGIN * 2) / Math.max(0.4, baseScale));
    const rows = rawContentW > maxUnscaledW ? Math.ceil(rawContentW / maxUnscaledW) : 1;
    unscaledW = Math.min(rawContentW, maxUnscaledW);
    const rowH = Math.ceil(labelFontSize * 1.25) + Math.ceil(valueFontSize * 1.25) + 4;
    unscaledH = 22 + rows * rowH + Math.max(0, rows - 1) * 6;
  } else {
    const metricWidth = Math.max(104, Math.round(valueFontSize * 4.6));
    unscaledW = Math.max(132, metricWidth + 28);
    const chipH = hasActivityChip ? 26 : 0;
    const rowH = Math.ceil(labelFontSize * 1.25) + Math.ceil(valueFontSize * 1.25);
    unscaledH = 26 + chipH + (hasActivityChip ? 10 : 0) + config.metrics.length * rowH + Math.max(0, config.metrics.length - 1) * 10;
  }

  if (
    measuredUnscaled &&
    measuredUnscaled.width > 0 &&
    measuredUnscaled.height > 0 &&
    (!measuredUnscaled.orientation || measuredUnscaled.orientation === orientation)
  ) {
    unscaledW = Math.max(unscaledW, measuredUnscaled.width);
    unscaledH = Math.max(unscaledH, measuredUnscaled.height);
  }

  const maxAllowedW = Math.max(160, viewportW - EDGE_MARGIN * 2);
  const maxAllowedH = Math.max(120, viewportH - EDGE_MARGIN * 2);
  const fitScaleW = maxAllowedW / Math.max(1, unscaledW);
  const fitScaleH = maxAllowedH / Math.max(1, unscaledH);
  const effectiveScale = Math.max(
    0.35,
    Math.floor(Math.min(baseScale, fitScaleW, fitScaleH) * 100) / 100,
  );

  return {
    width: Math.min(maxAllowedW, roundToStep(unscaledW * effectiveScale, 4)),
    height: Math.min(maxAllowedH, roundToStep(unscaledH * effectiveScale, 4)),
    scale: effectiveScale,
  };
}

export function estimateColorLegendSize(
  colorBy: ColorByMode,
  stops: readonly ColorLegendStop[] | undefined,
  viewportW = 1920,
): { width: number; height: number } | null {
  if (colorBy === "stage" || !stops || stops.length === 0) {
    return null;
  }
  const maxW = Math.max(180, viewportW - EDGE_MARGIN * 2);
  const titleW = colorBy.length * 7 + 10;
  const stopsW = stops.reduce((acc, s) => acc + 14 + s.label.length * 5.6 + 6, 0);
  const rawW = Math.ceil(22 + titleW + stopsW);
  const rows = rawW > maxW ? Math.ceil(rawW / maxW) : 1;
  return {
    width: Math.min(rawW, maxW),
    height: rows === 1 ? 26 : 12 + rows * 16,
  };
}

export function estimateElevationOverlaySize(compact: boolean): { width: number; height: number } {
  return compact ? { width: 234, height: 82 } : { width: 236, height: 84 };
}

export function estimateWatermarkSize(isPreview: boolean, compact: boolean): { width: number; height: number } | null {
  if (isPreview) {
    return { width: 192, height: 28 };
  }
  if (compact) {
    return { width: 146, height: 24 };
  }
  return null;
}

export function estimatePhotoCardSize(
  viewportW: number,
  viewportH: number,
  style: PhotoCardStyle = "corner",
): { width: number; height: number } {
  const minDim = Math.min(viewportW, viewportH);
  if (style === "cinematic") {
    const width = Math.round(Math.min(340, Math.max(216, minDim * 0.32)));
    const height = Math.round(width * 0.64);
    return { width, height };
  }
  if (style === "polaroid") {
    const width = Math.round(Math.min(264, Math.max(184, minDim * 0.25)));
    const height = Math.round(width * 0.96);
    return { width, height };
  }
  const width = Math.round(Math.min(280, Math.max(192, minDim * 0.26)));
  const height = Math.round(width * 0.82);
  return { width, height };
}

export function estimateChapterCardSize(
  viewportW: number,
  compact: boolean,
): { width: number; height: number } {
  const width = Math.round(Math.min(340, Math.max(200, viewportW * 0.28)));
  const height = compact ? 64 : 76;
  return { width, height };
}

export function estimateFinalePosterSize(
  viewportW: number,
  viewportH: number,
  compact: boolean,
): { width: number; height: number } {
  const maxW = Math.max(200, viewportW - EDGE_MARGIN * 2);
  const maxH = Math.max(140, viewportH - EDGE_MARGIN * 2);
  const width = Math.round(Math.min(maxW, compact ? 360 : 460));
  const height = Math.round(Math.min(maxH, compact ? 200 : 250));
  return { width, height };
}

function positionToNominalSlot(pos: PanelPosition): HudSlotId {
  switch (pos) {
    case "top":
      return "top-center";
    case "bottom":
      return "bottom-center";
    case "left":
      return "left-center";
    case "right":
      return "right-center";
  }
}

function slotToRect(
  slot: HudSlotId,
  width: number,
  height: number,
  viewportW: number,
  viewportH: number,
): Rect {
  let x = EDGE_MARGIN;
  let y = EDGE_MARGIN;

  switch (slot) {
    case "top-center":
      x = (viewportW - width) / 2;
      y = EDGE_MARGIN;
      break;
    case "top-left":
      x = EDGE_MARGIN;
      y = EDGE_MARGIN;
      break;
    case "top-right":
      x = viewportW - EDGE_MARGIN - width;
      y = EDGE_MARGIN;
      break;
    case "bottom-center":
      x = (viewportW - width) / 2;
      y = viewportH - EDGE_MARGIN - height;
      break;
    case "bottom-left":
      x = EDGE_MARGIN;
      y = viewportH - EDGE_MARGIN - height;
      break;
    case "bottom-right":
      x = viewportW - EDGE_MARGIN - width;
      y = viewportH - EDGE_MARGIN - height;
      break;
    case "left-center":
      x = EDGE_MARGIN;
      y = (viewportH - height) / 2;
      break;
    case "right-center":
      x = viewportW - EDGE_MARGIN - width;
      y = (viewportH - height) / 2;
      break;
  }

  return clampRectToViewport({ x, y, width, height }, viewportW, viewportH, EDGE_MARGIN);
}

function clampViewportPadding(
  pad: ViewportPadding,
  viewportW: number,
  viewportH: number,
  maxRatio = 0.55,
): ViewportPadding {
  let { top, bottom, left, right } = pad;
  const maxHoriz = Math.max(16, Math.floor(viewportW * maxRatio));
  const maxVert = Math.max(16, Math.floor(viewportH * maxRatio));

  if (left + right > maxHoriz) {
    const scale = maxHoriz / (left + right);
    left = Math.floor(left * scale);
    right = Math.floor(right * scale);
  }
  if (top + bottom > maxVert) {
    const scale = maxVert / (top + bottom);
    top = Math.floor(top * scale);
    bottom = Math.floor(bottom * scale);
  }
  return {
    top: Math.max(0, Math.round(top)),
    bottom: Math.max(0, Math.round(bottom)),
    left: Math.max(0, Math.round(left)),
    right: Math.max(0, Math.round(right)),
  };
}

/**
 * Clamps fitBounds padding strictly to <= 35% of actual canvas dimensions
 * so MapLibre never emits "Map cannot fit within canvas with the given bounds, padding, and/or offset."
 */
export function clampFitTrackPadding(
  pad: ViewportPadding,
  canvasW: number,
  canvasH: number,
): ViewportPadding {
  return clampViewportPadding(pad, Math.max(120, canvasW), Math.max(120, canvasH), 0.35);
}

function getSlotOrderForPreferred(preferred: HudSlotId): HudSlotId[] {
  switch (preferred) {
    case "top-center":
      return [
        "top-center",
        "top-right",
        "top-left",
        "right-center",
        "left-center",
        "bottom-center",
        "bottom-right",
        "bottom-left",
      ];
    case "bottom-center":
      return [
        "bottom-center",
        "bottom-right",
        "bottom-left",
        "right-center",
        "left-center",
        "top-center",
        "top-right",
        "top-left",
      ];
    case "left-center":
      return [
        "left-center",
        "top-left",
        "bottom-left",
        "right-center",
        "top-center",
        "bottom-center",
      ];
    case "right-center":
      return [
        "right-center",
        "top-right",
        "bottom-right",
        "left-center",
        "top-center",
        "bottom-center",
      ];
    default:
      return [preferred, "top-center", "bottom-center", "top-right", "bottom-right"];
  }
}

function slotToPanelPosition(slot: HudSlotId): PanelPosition {
  if (slot.startsWith("left")) return "left";
  if (slot.startsWith("right")) return "right";
  if (slot.startsWith("bottom")) return "bottom";
  return "top";
}

function pointToRectMinDistSq(px: number, py: number, r: Rect): number {
  const dx = px < r.x ? r.x - px : px > r.x + r.width ? px - (r.x + r.width) : 0;
  const dy = py < r.y ? r.y - py : py > r.y + r.height ? py - (r.y + r.height) : 0;
  return dx * dx + dy * dy;
}

export interface SolveStageLayoutInput {
  stages: readonly TimelineStage[];
  spec: RenderSpecV2;
  evaluated: EvaluatedTimelineState;
  viewportW: number;
  viewportH: number;
  compact: boolean;
  isPreview: boolean;
  hasActivePhoto: boolean;
  hasActiveChapterCard?: boolean;
  hasActiveFinalePoster?: boolean;
  climbCalloutTargets?: readonly { id: string; screenX: number; screenY: number }[];
  climbMarkers?: readonly ClimbProgressMarker[];
  followCamera?: {
    center: [number, number];
    zoom: number;
    pitch: number;
    bearing: number;
  };
  travelDurationS?: number;
  measuredHudUnscaled?: MeasuredHudSize | null;
}

/**
 * Deterministic HUD, elevation overlay, photo card, and camera padding solver.
 * Produces identical rects and paddings for a given frame across editor, share, and /render/$jobId.
 */
export function solveStageLayout(input: SolveStageLayoutInput): StageLayoutSolution {
  const {
    stages,
    spec,
    evaluated,
    viewportW,
    viewportH,
    compact,
    isPreview,
    hasActivePhoto,
    followCamera,
    measuredHudUnscaled,
  } = input;

  const safeW = Math.max(240, Math.round(viewportW));
  const safeH = Math.max(240, Math.round(viewportH));
  const gutter = compact ? 16 : 20;

  const wmSize = estimateWatermarkSize(isPreview || spec.is_preview, compact);
  const watermarkRect: Rect | null = wmSize
    ? clampRectToViewport(
        {
          x: safeW - EDGE_MARGIN - wmSize.width,
          y: safeH - EDGE_MARGIN - wmSize.height,
          width: wmSize.width,
          height: wmSize.height,
        },
        safeW,
        safeH,
      )
    : null;

  const hudEnabled = spec.statistics_panel.enabled && spec.statistics_panel.metrics.length > 0;
  const hasActivityChip = evaluated.activeStage !== null;
  const hudSize = hudEnabled
    ? estimateStatisticsHudSize(
        spec.statistics_panel,
        compact,
        hasActivityChip,
        safeW,
        measuredHudUnscaled,
        safeH,
      )
    : null;

  const nominalSlot = positionToNominalSlot(spec.statistics_panel.position);
  const nominalHudRect = hudSize
    ? slotToRect(nominalSlot, hudSize.width, hudSize.height, safeW, safeH)
    : null;

  const basePad = compact ? 32 : 48;
  const elevSize = spec.elevation_chart_overlay ? estimateElevationOverlaySize(compact) : null;

  const rawSafePad: ViewportPadding = {
    top: basePad,
    bottom: basePad,
    left: basePad,
    right: basePad,
  };

  if (nominalHudRect) {
    if (nominalSlot.startsWith("top")) {
      rawSafePad.top = Math.max(rawSafePad.top, nominalHudRect.y + nominalHudRect.height + gutter);
    } else if (nominalSlot.startsWith("bottom")) {
      rawSafePad.bottom = Math.max(
        rawSafePad.bottom,
        safeH - nominalHudRect.y + gutter,
      );
    } else if (nominalSlot.startsWith("left")) {
      rawSafePad.left = Math.max(rawSafePad.left, nominalHudRect.x + nominalHudRect.width + gutter);
    } else if (nominalSlot.startsWith("right")) {
      rawSafePad.right = Math.max(rawSafePad.right, safeW - nominalHudRect.x + gutter);
    }
  }
  if (elevSize) {
    const elevBottomClearance =
      nominalHudRect && nominalSlot.startsWith("bottom")
        ? safeH - nominalHudRect.y + 12 + elevSize.height + gutter
        : EDGE_MARGIN + elevSize.height + gutter;
    rawSafePad.bottom = Math.max(rawSafePad.bottom, elevBottomClearance);
  }
  const fitPadding = clampViewportPadding(rawSafePad, safeW, safeH);

  // Follow-marker padding adds subtle directional lookahead offset (<= 14% viewport)
  let followPadding: ViewportPadding = { ...fitPadding };
  if (spec.camera_mode === "follow-marker" && followCamera) {
    const lookaheadOffsetMax = Math.round(Math.min(safeW, safeH) * 0.14);
    const pCur = projectLngLatToScreen(
      evaluated.markerPosition,
      followCamera.center,
      followCamera.zoom,
      followCamera.pitch,
      followCamera.bearing,
      safeW,
      safeH,
      fitPadding,
    );
    const pPrev = projectLngLatToScreen(
      evaluated.previousPosition,
      followCamera.center,
      followCamera.zoom,
      followCamera.pitch,
      followCamera.bearing,
      safeW,
      safeH,
      fitPadding,
    );
    const dx = pCur.x - pPrev.x;
    const dy = pCur.y - pPrev.y;
    const len = Math.hypot(dx, dy);
    if (len > 1) {
      const ux = dx / len;
      const uy = dy / len;
      const shift = Math.min(lookaheadOffsetMax, Math.round(len * 0.4));
      followPadding = clampViewportPadding(
        {
          top: fitPadding.top + (uy < -0.25 ? Math.round(-uy * shift) : 0),
          bottom: fitPadding.bottom + (uy > 0.25 ? Math.round(uy * shift) : 0),
          left: fitPadding.left + (ux < -0.25 ? Math.round(-ux * shift) : 0),
          right: fitPadding.right + (ux > 0.25 ? Math.round(ux * shift) : 0),
        },
        safeW,
        safeH,
      );
    }
  }

  // Resolve HUD placement:
  // In follow-marker mode the camera is centered on the active marker with followPadding
  // reserving clearance for the nominal HUD edge, so the HUD stays anchored at nominalSlot
  // unless the active marker itself actually collides with the nominal HUD rect (e.g. under
  // a custom camera offset or extreme viewport aspect ratio).
  let resolvedHud: StageLayoutSolution["hud"] = nominalHudRect && hudSize
    ? { rect: nominalHudRect, slot: nominalSlot, scale: hudSize.scale }
    : null;

  if (
    resolvedHud &&
    nominalHudRect &&
    hudSize &&
    spec.statistics_panel.collision_avoidance &&
    spec.camera_mode === "follow-marker" &&
    followCamera &&
    stages.some((s) => s.points.length > 0)
  ) {
    const markerScreen = projectLngLatToScreen(
      evaluated.markerPosition,
      followCamera.center,
      followCamera.zoom,
      followCamera.pitch,
      followCamera.bearing,
      safeW,
      safeH,
      followPadding,
    );
    const markerMargin = 16;
    const markerHitsRect = (rect: Rect): boolean =>
      markerScreen.visible &&
      markerScreen.x >= rect.x - markerMargin &&
      markerScreen.x <= rect.x + rect.width + markerMargin &&
      markerScreen.y >= rect.y - markerMargin &&
      markerScreen.y <= rect.y + rect.height + markerMargin;

    if (markerHitsRect(nominalHudRect)) {
      const candidateSlots = getSlotOrderForPreferred(nominalSlot);
      const slotDimensions = (slot: HudSlotId) => {
        const panelPos = slotToPanelPosition(slot);
        if (panelPos === spec.statistics_panel.position) {
          return hudSize;
        }
        return estimateStatisticsHudSize(
          { ...spec.statistics_panel, position: panelPos },
          compact,
          hasActivityChip,
          safeW,
          null,
          safeH,
        );
      };

      let bestSlot: HudSlotId = nominalSlot;
      let bestDim = hudSize;
      let bestRect = nominalHudRect;
      let bestScore = Infinity;

      for (let sIdx = 0; sIdx < candidateSlots.length; sIdx++) {
        const slot = candidateSlots[sIdx]!;
        const dim = slotDimensions(slot);
        const rect = slotToRect(slot, dim.width, dim.height, safeW, safeH);

        let score = markerHitsRect(rect) ? 10_000 : 0;
        if (watermarkRect && rectsIntersect(rect, watermarkRect, 8)) {
          score += 800;
        }
        if (slot !== nominalSlot) {
          const sameEdge = slot.split("-")[0] === nominalSlot.split("-")[0];
          score += sameEdge ? 120 + sIdx * 10 : 240 + sIdx * 10;
        }

        if (score < bestScore) {
          bestScore = score;
          bestSlot = slot;
          bestDim = dim;
          bestRect = rect;
        }
      }

      resolvedHud = {
        rect: clampRectToViewport(bestRect, safeW, safeH, EDGE_MARGIN),
        slot: bestSlot,
        scale: bestDim.scale,
      };
    }
  }

  let resolvedElevation: StageLayoutSolution["elevation"] = null;
  if (elevSize) {
    const hudRect = resolvedHud?.rect ?? null;
    let elevRect: Rect = {
      x: EDGE_MARGIN,
      y: safeH - EDGE_MARGIN - elevSize.height,
      width: elevSize.width,
      height: elevSize.height,
    };
    let anchorLeft = true;

    if (hudRect && rectsIntersect(elevRect, hudRect, 12)) {
      const liftedY = hudRect.y - 12 - elevSize.height;
      const liftedCandidate: Rect = {
        ...elevRect,
        y: liftedY,
      };
      if (liftedY >= EDGE_MARGIN && !rectsIntersect(liftedCandidate, hudRect, 12)) {
        elevRect = liftedCandidate;
      } else {
        // Flip to right side above watermark if left/bottom-left is blocked by HUD
        anchorLeft = false;
        const wmTop = watermarkRect ? watermarkRect.y - 12 : safeH - EDGE_MARGIN;
        elevRect = {
          x: safeW - EDGE_MARGIN - elevSize.width,
          y: Math.max(EDGE_MARGIN, wmTop - elevSize.height),
          width: elevSize.width,
          height: elevSize.height,
        };
        if (hudRect && rectsIntersect(elevRect, hudRect, 12)) {
          elevRect = {
            ...elevRect,
            y: Math.max(EDGE_MARGIN, hudRect.y - 12 - elevSize.height),
          };
        }
      }
    }

    elevRect = clampRectToViewport(elevRect, safeW, safeH, EDGE_MARGIN);
    resolvedElevation = { rect: elevRect, anchorLeft };
  }

  const legendSize = estimateColorLegendSize(spec.color_by, evaluated.colorLegendStops, safeW);
  let resolvedColorLegend: StageLayoutSolution["colorLegend"] = null;
  if (legendSize) {
    const obstacles: Rect[] = [];
    if (resolvedHud) obstacles.push(resolvedHud.rect);
    if (resolvedElevation) obstacles.push(resolvedElevation.rect);
    if (watermarkRect) obstacles.push(watermarkRect);

    const lgW = legendSize.width;
    const lgH = legendSize.height;
    const legendCandidates: Rect[] = [
      { x: safeW - EDGE_MARGIN - lgW, y: EDGE_MARGIN, width: lgW, height: lgH },
      { x: EDGE_MARGIN, y: EDGE_MARGIN, width: lgW, height: lgH },
      { x: Math.round((safeW - lgW) / 2), y: EDGE_MARGIN, width: lgW, height: lgH },
      ...(resolvedHud
        ? [
            {
              x: safeW - EDGE_MARGIN - lgW,
              y: resolvedHud.rect.y + resolvedHud.rect.height + 10,
              width: lgW,
              height: lgH,
            },
            {
              x: Math.round((safeW - lgW) / 2),
              y: resolvedHud.rect.y + resolvedHud.rect.height + 10,
              width: lgW,
              height: lgH,
            },
            {
              x: EDGE_MARGIN,
              y: resolvedHud.rect.y + resolvedHud.rect.height + 10,
              width: lgW,
              height: lgH,
            },
          ]
        : []),
      {
        x: safeW - EDGE_MARGIN - lgW,
        y: (watermarkRect ? watermarkRect.y - 10 : safeH - EDGE_MARGIN) - lgH,
        width: lgW,
        height: lgH,
      },
      {
        x: EDGE_MARGIN,
        y:
          (resolvedElevation && resolvedElevation.anchorLeft
            ? resolvedElevation.rect.y - 10
            : safeH - EDGE_MARGIN) - lgH,
        width: lgW,
        height: lgH,
      },
      {
        x: Math.round((safeW - lgW) / 2),
        y: safeH - EDGE_MARGIN - lgH,
        width: lgW,
        height: lgH,
      },
      ...(resolvedHud
        ? [
            {
              x: safeW - EDGE_MARGIN - lgW,
              y: resolvedHud.rect.y - 10 - lgH,
              width: lgW,
              height: lgH,
            },
            {
              x: Math.round((safeW - lgW) / 2),
              y: resolvedHud.rect.y - 10 - lgH,
              width: lgW,
              height: lgH,
            },
          ]
        : []),
    ];

    let bestLegendRect = clampRectToViewport(legendCandidates[0]!, safeW, safeH, EDGE_MARGIN);
    let bestLegendScore = Infinity;
    for (let i = 0; i < legendCandidates.length; i++) {
      const clamped = clampRectToViewport(legendCandidates[i]!, safeW, safeH, EDGE_MARGIN);
      let score = i * 4;
      for (const obs of obstacles) {
        if (rectsIntersect(clamped, obs, 8)) {
          score += 2000 + rectIntersectionArea(clamped, obs);
        }
      }
      if (score < bestLegendScore) {
        bestLegendScore = score;
        bestLegendRect = clamped;
      }
    }
    resolvedColorLegend = { rect: bestLegendRect };
  }

  const photoStyle: PhotoCardStyle = spec.photo_card_style ?? "corner";
  let resolvedPhotoCard: StageLayoutSolution["photoCard"] = null;
  if (hasActivePhoto) {
    const cardSize = estimatePhotoCardSize(safeW, safeH, photoStyle);
    const obstacles: Rect[] = [];
    if (resolvedHud) obstacles.push(resolvedHud.rect);
    if (resolvedElevation) obstacles.push(resolvedElevation.rect);
    if (resolvedColorLegend) obstacles.push(resolvedColorLegend.rect);
    if (watermarkRect) obstacles.push(watermarkRect);

    let markerScreen: { x: number; y: number } | null = null;
    if (followCamera) {
      const scr = projectLngLatToScreen(
        evaluated.markerPosition,
        followCamera.center,
        followCamera.zoom,
        followCamera.pitch,
        followCamera.bearing,
        safeW,
        safeH,
        followPadding,
      );
      if (scr.visible) {
        markerScreen = { x: scr.x, y: scr.y };
      }
    }

    const candidates: {
      corner: "top-right" | "bottom-right" | "top-left" | "bottom-left";
      rect: Rect;
    }[] = [
      {
        corner: "top-right",
        rect: {
          x: safeW - EDGE_MARGIN - cardSize.width,
          y: EDGE_MARGIN,
          width: cardSize.width,
          height: cardSize.height,
        },
      },
      {
        corner: "bottom-right",
        rect: {
          x: safeW - EDGE_MARGIN - cardSize.width,
          y: (watermarkRect ? watermarkRect.y - 12 : safeH - EDGE_MARGIN) - cardSize.height,
          width: cardSize.width,
          height: cardSize.height,
        },
      },
      {
        corner: "top-left",
        rect: {
          x: EDGE_MARGIN,
          y: EDGE_MARGIN,
          width: cardSize.width,
          height: cardSize.height,
        },
      },
      {
        corner: "bottom-left",
        rect: {
          x: EDGE_MARGIN,
          y:
            (resolvedElevation && resolvedElevation.anchorLeft
              ? resolvedElevation.rect.y - 12
              : safeH - EDGE_MARGIN) - cardSize.height,
          width: cardSize.width,
          height: cardSize.height,
        },
      },
    ];

    let bestCandidate = candidates[0]!;
    let bestCardScore = Infinity;

    for (let i = 0; i < candidates.length; i++) {
      const cand = candidates[i]!;
      const clamped = clampRectToViewport(cand.rect, safeW, safeH, EDGE_MARGIN);
      let score = i * 5;
      for (const obs of obstacles) {
        if (rectsIntersect(clamped, obs, 10)) {
          score += 2000 + rectIntersectionArea(clamped, obs);
        }
      }
      if (markerScreen) {
        const dSq = pointToRectMinDistSq(markerScreen.x, markerScreen.y, clamped);
        if (dSq < 2500) {
          score += 500 - Math.sqrt(dSq) * 5;
        }
      }
      if (score < bestCardScore) {
        bestCardScore = score;
        bestCandidate = { corner: cand.corner, rect: clamped };
      }
    }

    resolvedPhotoCard = { ...bestCandidate, style: photoStyle };
  }

  const activeChapter = input.hasActiveChapterCard ?? Boolean(evaluated.chapterCard);
  let resolvedChapterCard: StageLayoutSolution["chapterCard"] = null;
  if (activeChapter) {
    const chSize = estimateChapterCardSize(safeW, compact);
    const obstacles: Rect[] = [];
    if (resolvedHud) obstacles.push(resolvedHud.rect);
    if (resolvedElevation) obstacles.push(resolvedElevation.rect);
    if (resolvedColorLegend) obstacles.push(resolvedColorLegend.rect);
    if (resolvedPhotoCard) obstacles.push(resolvedPhotoCard.rect);
    if (watermarkRect) obstacles.push(watermarkRect);

    const chapterCandidates: Rect[] = [
      {
        x: EDGE_MARGIN,
        y: safeH - EDGE_MARGIN - chSize.height,
        width: chSize.width,
        height: chSize.height,
      },
      {
        x: Math.round((safeW - chSize.width) / 2),
        y: safeH - EDGE_MARGIN - chSize.height,
        width: chSize.width,
        height: chSize.height,
      },
      {
        x: EDGE_MARGIN,
        y:
          (resolvedElevation && resolvedElevation.anchorLeft
            ? resolvedElevation.rect.y - 12
            : safeH - EDGE_MARGIN) - chSize.height,
        width: chSize.width,
        height: chSize.height,
      },
      {
        x: safeW - EDGE_MARGIN - chSize.width,
        y: (watermarkRect ? watermarkRect.y - 12 : safeH - EDGE_MARGIN) - chSize.height,
        width: chSize.width,
        height: chSize.height,
      },
      {
        x: EDGE_MARGIN,
        y: EDGE_MARGIN,
        width: chSize.width,
        height: chSize.height,
      },
      {
        x: safeW - EDGE_MARGIN - chSize.width,
        y: EDGE_MARGIN,
        width: chSize.width,
        height: chSize.height,
      },
    ];

    let bestRect = clampRectToViewport(chapterCandidates[0]!, safeW, safeH, EDGE_MARGIN);
    let bestScore = Infinity;
    for (let i = 0; i < chapterCandidates.length; i++) {
      const clamped = clampRectToViewport(chapterCandidates[i]!, safeW, safeH, EDGE_MARGIN);
      let score = i * 4;
      for (const obs of obstacles) {
        if (rectsIntersect(clamped, obs, 8)) {
          score += 2000 + rectIntersectionArea(clamped, obs);
        }
      }
      if (score < bestScore) {
        bestScore = score;
        bestRect = clamped;
      }
    }
    resolvedChapterCard = { rect: bestRect };
  }

  const activeFinale =
    input.hasActiveFinalePoster ?? (spec.show_finale_poster && evaluated.progressRatio >= 0.999);
  let resolvedFinalePoster: StageLayoutSolution["finalePoster"] = null;
  if (activeFinale) {
    const fpSize = estimateFinalePosterSize(safeW, safeH, compact);
    const obstacles: Rect[] = [];
    if (resolvedHud) obstacles.push(resolvedHud.rect);
    if (resolvedElevation) obstacles.push(resolvedElevation.rect);
    if (resolvedColorLegend) obstacles.push(resolvedColorLegend.rect);
    if (resolvedPhotoCard) obstacles.push(resolvedPhotoCard.rect);
    if (resolvedChapterCard) obstacles.push(resolvedChapterCard.rect);
    if (watermarkRect) obstacles.push(watermarkRect);

    const cx = Math.round((safeW - fpSize.width) / 2);
    const cy = Math.round((safeH - fpSize.height) / 2);
    const posterCandidates: Rect[] = [
      { x: cx, y: cy, width: fpSize.width, height: fpSize.height },
      {
        x: cx,
        y: Math.max(EDGE_MARGIN, cy - 60),
        width: fpSize.width,
        height: fpSize.height,
      },
      {
        x: cx,
        y: Math.min(safeH - EDGE_MARGIN - fpSize.height, cy + 60),
        width: fpSize.width,
        height: fpSize.height,
      },
      {
        x: Math.max(EDGE_MARGIN, resolvedHud ? resolvedHud.rect.x + resolvedHud.rect.width + 12 : cx),
        y: cy,
        width: fpSize.width,
        height: fpSize.height,
      },
    ];

    let bestPosterRect = clampRectToViewport(posterCandidates[0]!, safeW, safeH, EDGE_MARGIN);
    let bestPosterScore = Infinity;
    for (let i = 0; i < posterCandidates.length; i++) {
      const clamped = clampRectToViewport(posterCandidates[i]!, safeW, safeH, EDGE_MARGIN);
      let score = i * 5;
      for (const obs of obstacles) {
        if (rectsIntersect(clamped, obs, 8)) {
          score += 2000 + rectIntersectionArea(clamped, obs);
        }
      }
      if (score < bestPosterScore) {
        bestPosterScore = score;
        bestPosterRect = clamped;
      }
    }
    resolvedFinalePoster = { rect: bestPosterRect };
  }

  const resolvedClimbCallouts: ClimbCalloutSlot[] = [];
  const calloutTargets: { id: string; screenX: number; screenY: number }[] = [];
  const isCalloutAnchorOnScreen = (screenX: number, screenY: number) =>
    Number.isFinite(screenX) &&
    Number.isFinite(screenY) &&
    screenX >= -24 &&
    screenX <= safeW + 24 &&
    screenY >= -24 &&
    screenY <= safeH + 24;

  if (spec.show_climb_callouts !== false) {
    if (input.climbCalloutTargets && input.climbCalloutTargets.length > 0) {
      for (const target of input.climbCalloutTargets) {
        if (isCalloutAnchorOnScreen(target.screenX, target.screenY)) {
          calloutTargets.push(target);
        }
      }
    } else if (input.climbMarkers && followCamera) {
      for (const c of input.climbMarkers) {
        if (!c.reached) continue;
        const scr = projectLngLatToScreen(
          c.summitPosition,
          followCamera.center,
          followCamera.zoom,
          followCamera.pitch,
          followCamera.bearing,
          safeW,
          safeH,
          followPadding,
        );
        if (scr.visible && isCalloutAnchorOnScreen(scr.x, scr.y)) {
          calloutTargets.push({ id: c.id, screenX: scr.x, screenY: scr.y });
        }
      }
    }
  }

  if (calloutTargets.length > 0) {
    const pillW = compact ? 116 : 136;
    const pillH = compact ? 28 : 32;
    const staticObstacles: Rect[] = [];
    if (resolvedHud) staticObstacles.push(resolvedHud.rect);
    if (resolvedElevation) staticObstacles.push(resolvedElevation.rect);
    if (resolvedColorLegend) staticObstacles.push(resolvedColorLegend.rect);
    if (resolvedPhotoCard) staticObstacles.push(resolvedPhotoCard.rect);
    if (resolvedChapterCard) staticObstacles.push(resolvedChapterCard.rect);
    if (resolvedFinalePoster) staticObstacles.push(resolvedFinalePoster.rect);
    if (watermarkRect) staticObstacles.push(watermarkRect);

    for (const target of calloutTargets) {
      const offsets: [number, number][] = [
        [-pillW / 2, -pillH - 12],
        [14, -pillH / 2],
        [-pillW - 14, -pillH / 2],
        [-pillW / 2, 14],
      ];
      let chosenRect: Rect | null = null;
      let bestScore = Infinity;

      for (let i = 0; i < offsets.length; i++) {
        const [ox, oy] = offsets[i]!;
        const cand = clampRectToViewport(
          {
            x: target.screenX + ox,
            y: target.screenY + oy,
            width: pillW,
            height: pillH,
          },
          safeW,
          safeH,
          EDGE_MARGIN,
        );
        let score = i * 4;
        for (const obs of staticObstacles) {
          if (rectsIntersect(cand, obs, 6)) {
            score += 2000 + rectIntersectionArea(cand, obs);
          }
        }
        for (const placed of resolvedClimbCallouts) {
          if (rectsIntersect(cand, placed.rect, 6)) {
            score += 1500 + rectIntersectionArea(cand, placed.rect);
          }
        }
        if (score < bestScore) {
          bestScore = score;
          chosenRect = cand;
        }
      }

      if (chosenRect && bestScore < 1500) {
        resolvedClimbCallouts.push({
          id: target.id,
          rect: chosenRect,
          anchorX: target.screenX,
          anchorY: target.screenY,
        });
      }
    }
  }

  return {
    hud: resolvedHud,
    elevation: resolvedElevation,
    colorLegend: resolvedColorLegend,
    photoCard: resolvedPhotoCard,
    chapterCard: resolvedChapterCard,
    finalePoster: resolvedFinalePoster,
    climbCallouts: resolvedClimbCallouts,
    watermarkRect,
    fitPadding,
    followPadding,
  };
}
