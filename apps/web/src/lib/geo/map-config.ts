import type { Layer, LayersList } from "@deck.gl/core";
import { ArcLayer, PathLayer, PolygonLayer, ScatterplotLayer } from "@deck.gl/layers";
import type { MapboxOverlayProps } from "@deck.gl/mapbox";
import type { MapOptions, RasterDEMSourceSpecification, StyleSpecification } from "maplibre-gl";
import type { ColorByMode, MapStylePreset, MarkedTrailType } from "../api/types";
import { hexToRgbTuple } from "./format";
import type { ClimbProgressMarker, StopMarker, TimelineStage, TransferArcSegment, VisiblePathSegment } from "./timeline";

export const TERRARIUM_DEM_TILE_URL =
  "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
export const ESRI_SATELLITE_TILE_URL =
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
export const TERRAIN_SOURCE_ID = "trip-terrarium-dem";
export const HILLSHADE_SOURCE_ID = "trip-terrarium-hillshade";
export const MARKED_TRAILS_SOURCE_ID = "trip-marked-trails";
export const MARKED_TRAILS_HIKING_LAYER_ID = "trip-marked-trails-hiking";
export const MARKED_TRAILS_CYCLING_LAYER_ID = "trip-marked-trails-cycling";
export const MARKED_TRAILS_MTB_LAYER_ID = "trip-marked-trails-mtb";
export const MARKED_TRAILS_LABEL_LAYER_IDS = [
  "trip-marked-trails-hiking-label",
  "trip-marked-trails-cycling-label",
  "trip-marked-trails-mtb-label",
] as const;
// Matches the profile: tiles stop at z13 and MapLibre overzooms above it.
const MARKED_TRAILS_MAX_ZOOM = 13;
const MARKED_TRAILS_MIN_ZOOM = 5;
const MARKED_TRAILS_LABEL_MIN_ZOOM = 13;
const MARKED_TRAILS_LABEL_FONT = ["Noto Sans Regular"];

interface MarkedTrailTypeStyle {
  lineLayerId: string;
  labelLayerId: string;
  defaultColour: string;
  dashArray: number[] | null;
}

// Hiking stays solid; the dashes differ so cycling and MTB read apart from it and from each other.
const MARKED_TRAIL_TYPE_STYLES: Record<MarkedTrailType, MarkedTrailTypeStyle> = {
  hiking: {
    lineLayerId: MARKED_TRAILS_HIKING_LAYER_ID,
    labelLayerId: MARKED_TRAILS_LABEL_LAYER_IDS[0],
    defaultColour: "#b91c1c",
    dashArray: null,
  },
  cycling: {
    lineLayerId: MARKED_TRAILS_CYCLING_LAYER_ID,
    labelLayerId: MARKED_TRAILS_LABEL_LAYER_IDS[1],
    defaultColour: "#1d4ed8",
    dashArray: [3, 2],
  },
  mtb: {
    lineLayerId: MARKED_TRAILS_MTB_LAYER_ID,
    labelLayerId: MARKED_TRAILS_LABEL_LAYER_IDS[2],
    defaultColour: "#7e22ce",
    dashArray: [1, 1.5],
  },
};
const MARKED_TRAIL_TYPE_ORDER: readonly MarkedTrailType[] = ["hiking", "cycling", "mtb"];

// Widths in px at the zoom stops; an unknown or missing network falls through to the local width.
const NETWORK_WIDTHS: Array<{ zoom: number; international: number; national: number; regional: number; local: number }> = [
  { zoom: 5, international: 2, national: 1.5, regional: 1, local: 0.6 },
  { zoom: 9, international: 2.5, national: 2, regional: 1.5, local: 1 },
  { zoom: 13, international: 4, national: 3.25, regional: 2.5, local: 1.75 },
];

function networkWidthExpression(): unknown[] {
  const stops = NETWORK_WIDTHS.flatMap((stop) => [
    stop.zoom,
    [
      "match",
      ["get", "network"],
      "international",
      stop.international,
      "national",
      stop.national,
      "regional",
      stop.regional,
      stop.local,
    ],
  ]);
  return ["interpolate", ["linear"], ["zoom"], ...stops];
}

export interface MarkedTrailsStyleOverlay {
  markedTrailTypes: readonly MarkedTrailType[];
  pmtilesUrl: string | null;
}

/**
 * Returns the overlay for the map style. The PMTiles URL is kept only while a Marked Trail
 * type is on, so a build published while the overlay is off does not reload the style.
 */
export function resolveMarkedTrailsOverlay(
  markedTrailTypes: readonly MarkedTrailType[],
  pmtilesUrl: string | null,
): MarkedTrailsStyleOverlay {
  return {
    markedTrailTypes,
    pmtilesUrl: markedTrailTypes.length > 0 ? pmtilesUrl : null,
  };
}

/** Returns a key that changes exactly when the map style has to be rebuilt. */
export function mapStyleKey(mapStyle: MapStylePreset, overlay: MarkedTrailsStyleOverlay): string {
  return `${mapStyle}|${overlay.pmtilesUrl ?? ""}|${overlay.markedTrailTypes.join(",")}`;
}

export interface TrackFeatureDatum {
  id: string;
  color: [number, number, number];
  path: [number, number][];
}

const OPENFREEMAP_STYLE_URLS: Record<MapStylePreset, string> = {
  liberty: "https://tiles.openfreemap.org/styles/liberty",
  positron: "https://tiles.openfreemap.org/styles/positron",
  bright: "https://tiles.openfreemap.org/styles/bright",
  dark: "https://tiles.openfreemap.org/styles/dark",
  "topo-relief": "https://tiles.openfreemap.org/styles/liberty",
  "satellite-hybrid": "https://tiles.openfreemap.org/styles/dark",
  "editorial-mono": "https://tiles.openfreemap.org/styles/positron",
};

export function resolveMapStyleUrl(styleId: MapStylePreset): string {
  return OPENFREEMAP_STYLE_URLS[styleId] ?? OPENFREEMAP_STYLE_URLS.liberty;
}

export function buildTerrariumDemSourceSpec(): RasterDEMSourceSpecification {
  return {
    type: "raster-dem",
    tiles: [TERRARIUM_DEM_TILE_URL],
    tileSize: 256,
    maxzoom: 15,
    encoding: "terrarium",
  };
}

const NUMERIC_COMPARISON_OPS = new Set(["<", "<=", ">", ">="]);

function isBareGetExpr(node: unknown): node is ["get", string, ...unknown[]] {
  return Array.isArray(node) && node[0] === "get" && typeof node[1] === "string";
}

function sanitizeFilterNode(node: unknown): unknown {
  if (!Array.isArray(node) || node.length === 0) {
    return node;
  }
  const op = node[0];
  if (typeof op === "string" && NUMERIC_COMPARISON_OPS.has(op) && node.length === 3) {
    const left = node[1];
    const right = node[2];
    const bareGetters: unknown[] = [];
    if (isBareGetExpr(left)) bareGetters.push(left);
    if (isBareGetExpr(right)) bareGetters.push(right);
    if (bareGetters.length > 0) {
      return [
        "all",
        ...bareGetters.map((g) => ["==", ["typeof", g], "number"]),
        node,
      ];
    }
  }
  return node.map((child, idx) => (idx === 0 ? child : sanitizeFilterNode(child)));
}

function coerceNumericPaintExpr(expr: unknown, fallback: number): unknown {
  if (isBareGetExpr(expr)) {
    return ["case", ["==", ["typeof", expr], "number"], expr, fallback];
  }
  return expr;
}

/**
 * Applies the basemap fixes and, when `markedTrails` names a published build and a trail
 * type, the Marked Trail source and layers.
 *
 * The basemap fixes guard bare `["get", ...]` numeric expressions in OpenFreeMap styles so MapLibre v5
 * does not emit "Expected value to be of type number, but found null instead" warnings when
 * vector tile features omit optional attributes like `ref_length`, `rank`, or `render_height`,
 * and apply preset transforms for `topo-relief`, `satellite-hybrid`, and `editorial-mono`.
 * The trail lines go before the first symbol layer so basemap labels stay on top of them; the
 * trail labels are added last. deck.gl draws the user's route above the whole style, so every
 * trail layer is below the route.
 */
export function sanitizeMapStyle(
  style: StyleSpecification,
  styleId?: MapStylePreset,
  markedTrails?: MarkedTrailsStyleOverlay,
): StyleSpecification {
  const basemap = applyBasemapSanitizers(style, styleId);
  return markedTrails ? injectMarkedTrailLayers(basemap, markedTrails) : basemap;
}

function markedTrailLineLayer(type: MarkedTrailType) {
  const config = MARKED_TRAIL_TYPE_STYLES[type];
  return {
    id: config.lineLayerId,
    type: "line",
    source: MARKED_TRAILS_SOURCE_ID,
    "source-layer": "marked_trails",
    minzoom: MARKED_TRAILS_MIN_ZOOM,
    filter: ["==", ["get", "type"], type],
    layout: { "line-cap": config.dashArray ? "butt" : "round", "line-join": "round" },
    paint: {
      // OSM colour values such as "red;white" do not parse; to-color falls back to the default.
      "line-color": ["to-color", ["get", "colour"], config.defaultColour],
      "line-width": networkWidthExpression(),
      "line-opacity": 0.75,
      ...(config.dashArray ? { "line-dasharray": config.dashArray } : {}),
    },
  };
}

function markedTrailLabelLayer(type: MarkedTrailType) {
  const config = MARKED_TRAIL_TYPE_STYLES[type];
  return {
    id: config.labelLayerId,
    type: "symbol",
    source: MARKED_TRAILS_SOURCE_ID,
    "source-layer": "marked_trails",
    minzoom: MARKED_TRAILS_LABEL_MIN_ZOOM,
    filter: ["==", ["get", "type"], type],
    layout: {
      "symbol-placement": "line",
      "text-field": ["coalesce", ["get", "ref"], ["get", "name"]],
      "text-font": MARKED_TRAILS_LABEL_FONT,
      "text-size": 11,
    },
    paint: {
      "text-color": "#1f2937",
      "text-halo-color": "rgba(255,255,255,0.9)",
      "text-halo-width": 1.5,
    },
  };
}

function injectMarkedTrailLayers(
  style: StyleSpecification,
  overlay: MarkedTrailsStyleOverlay,
): StyleSpecification {
  const enabled = MARKED_TRAIL_TYPE_ORDER.filter((type) => overlay.markedTrailTypes.includes(type));
  if (!overlay.pmtilesUrl || enabled.length === 0) {
    return style;
  }
  const lineLayers = enabled.map(markedTrailLineLayer);
  const labelLayers = enabled.map(markedTrailLabelLayer);
  const layers = [...style.layers];
  const firstSymbolIndex = layers.findIndex((layer) => layer.type === "symbol");
  layers.splice(
    firstSymbolIndex >= 0 ? firstSymbolIndex : layers.length,
    0,
    ...(lineLayers as unknown as StyleSpecification["layers"]),
  );
  layers.push(...(labelLayers as unknown as StyleSpecification["layers"]));
  return {
    ...style,
    sources: {
      ...(style.sources ?? {}),
      [MARKED_TRAILS_SOURCE_ID]: {
        type: "vector",
        url: `pmtiles://${overlay.pmtilesUrl}`,
        maxzoom: MARKED_TRAILS_MAX_ZOOM,
      },
    } as StyleSpecification["sources"],
    layers,
  };
}

function applyBasemapSanitizers(
  style: StyleSpecification,
  styleId?: MapStylePreset,
): StyleSpecification {
  if (!style || !Array.isArray(style.layers)) {
    return style;
  }

  const sanitizedLayers = style.layers.map((layer) => {
    const candidate = layer as Record<string, unknown>;
    let changed = false;
    const nextLayer: Record<string, unknown> = { ...candidate };

    if ("filter" in candidate && Array.isArray(candidate.filter)) {
      const sanitizedFilter = sanitizeFilterNode(candidate.filter);
      if (sanitizedFilter !== candidate.filter) {
        nextLayer.filter = sanitizedFilter;
        changed = true;
      }
    }

    if (candidate.type === "fill-extrusion" && candidate.paint && typeof candidate.paint === "object") {
      const paint = candidate.paint as Record<string, unknown>;
      const nextPaint: Record<string, unknown> = { ...paint };
      let paintChanged = false;
      if ("fill-extrusion-height" in paint) {
        const coerced = coerceNumericPaintExpr(paint["fill-extrusion-height"], 12);
        if (coerced !== paint["fill-extrusion-height"]) {
          nextPaint["fill-extrusion-height"] = coerced;
          paintChanged = true;
        }
      }
      if ("fill-extrusion-base" in paint) {
        const coerced = coerceNumericPaintExpr(paint["fill-extrusion-base"], 0);
        if (coerced !== paint["fill-extrusion-base"]) {
          nextPaint["fill-extrusion-base"] = coerced;
          paintChanged = true;
        }
      }
      if (paintChanged) {
        nextLayer.paint = nextPaint;
        changed = true;
      }
    }

    return (changed ? nextLayer : layer) as StyleSpecification["layers"][number];
  });

  const baseStyle: StyleSpecification = {
    ...style,
    layers: sanitizedLayers,
  };

  if (styleId === "topo-relief") {
    const nextSources = {
      ...(baseStyle.sources ?? {}),
      [HILLSHADE_SOURCE_ID]: buildTerrariumDemSourceSpec(),
    } as StyleSpecification["sources"];
    const hasHillshade = baseStyle.layers.some((l) => l.id === "trip-topo-hillshade");
    const hillshadeLayer = {
      id: "trip-topo-hillshade",
      type: "hillshade",
      source: HILLSHADE_SOURCE_ID,
      paint: {
        "hillshade-exaggeration": 0.48,
        "hillshade-shadow-color": "#334155",
        "hillshade-highlight-color": "#fef3c7",
        "hillshade-accent-color": "#0f766e",
      },
    } as StyleSpecification["layers"][number];
    const insertIdx = Math.min(2, baseStyle.layers.length);
    const nextLayers = hasHillshade
      ? baseStyle.layers
      : [
          ...baseStyle.layers.slice(0, insertIdx),
          hillshadeLayer,
          ...baseStyle.layers.slice(insertIdx),
        ];
    return {
      ...baseStyle,
      sources: nextSources,
      layers: nextLayers,
    };
  }

  if (styleId === "satellite-hybrid") {
    const nextSources = {
      ...(baseStyle.sources ?? {}),
      "esri-world-imagery": {
        type: "raster",
        tiles: [ESRI_SATELLITE_TILE_URL],
        tileSize: 256,
        maxzoom: 18,
      },
    } as StyleSpecification["sources"];
    const satLayer = {
      id: "trip-satellite-imagery",
      type: "raster",
      source: "esri-world-imagery",
      paint: {
        "raster-opacity": 0.92,
      },
    } as StyleSpecification["layers"][number];
    const filteredLayers = baseStyle.layers
      .filter((l) => l.id !== "trip-satellite-imagery")
      .map((layer) => {
        const rec = layer as unknown as Record<string, unknown>;
        if (rec.type === "fill" || rec.type === "background") {
          const paint = (rec.paint as Record<string, unknown> | undefined) ?? {};
          return {
            ...rec,
            paint: {
              ...paint,
              ...(rec.type === "fill" ? { "fill-opacity": 0.12 } : { "background-opacity": 0 }),
            },
          } as unknown as StyleSpecification["layers"][number];
        }
        return layer;
      });
    const bgIdx = filteredLayers.findIndex((l) => l.type === "background");
    const insertAt = bgIdx >= 0 ? bgIdx + 1 : 0;
    return {
      ...baseStyle,
      sources: nextSources,
      layers: [
        ...filteredLayers.slice(0, insertAt),
        satLayer,
        ...filteredLayers.slice(insertAt),
      ],
    };
  }

  if (styleId === "editorial-mono") {
    const monoLayers = baseStyle.layers.map((layer) => {
      const rec = layer as unknown as Record<string, unknown>;
      const id = String(rec.id ?? "").toLowerCase();
      const paint = (rec.paint as Record<string, unknown> | undefined) ?? {};
      if (rec.type === "background") {
        return {
          ...rec,
          paint: { ...paint, "background-color": "#f8fafc" },
        } as unknown as StyleSpecification["layers"][number];
      }
      if (rec.type === "fill") {
        const isWater = id.includes("water");
        return {
          ...rec,
          paint: {
            ...paint,
            "fill-color": isWater ? "#dbe4ee" : "#eef2f6",
          },
        } as unknown as StyleSpecification["layers"][number];
      }
      if (rec.type === "line") {
        return {
          ...rec,
          paint: {
            ...paint,
            "line-color": id.includes("boundary") || id.includes("border") ? "#94a3b8" : "#cbd5e1",
          },
        } as unknown as StyleSpecification["layers"][number];
      }
      if (rec.type === "symbol") {
        return {
          ...rec,
          paint: {
            ...paint,
            "text-color": "#1e293b",
            "text-halo-color": "#f8fafc",
          },
        } as unknown as StyleSpecification["layers"][number];
      }
      return layer;
    });
    return {
      ...baseStyle,
      layers: monoLayers,
    };
  }

  return baseStyle;
}

export function buildMapLibreInitOptions(
  container: HTMLElement,
  styleId: MapStylePreset,
  options?: {
    pitch?: number;
    bearing?: number;
    interactive?: boolean;
    deferStyle?: boolean;
  },
): MapOptions {
  return {
    container,
    ...(options?.deferStyle ? {} : { style: resolveMapStyleUrl(styleId) }),
    center: [0.225, 0.115],
    zoom: 11,
    pitch: options?.pitch ?? 0,
    bearing: options?.bearing ?? 0,
    interactive: options?.interactive ?? true,
    attributionControl: false,
    fadeDuration: 0,
    canvasContextAttributes: {
      preserveDrawingBuffer: true,
      antialias: true,
    },
  };
}

export interface PhotoPinDatum {
  id: string;
  position: [number, number];
}

export interface ElevationCurtainPolygonDatum {
  id: string;
  color: [number, number, number, number];
  polygon: [number, number, number][];
}

export interface ElevationRidgeDatum {
  id: string;
  color: [number, number, number];
  path: [number, number, number][];
}

export interface ElevationCurtainData {
  polygons: ElevationCurtainPolygonDatum[];
  ridges: ElevationRidgeDatum[];
}

export interface DeckOverlayBuildOptions {
  trackWidthPx?: number;
  markerColorHex?: string;
  stops?: readonly StopMarker[];
  photoPins?: readonly PhotoPinDatum[];
  colorBy?: ColorByMode;
  elevationCurtain?: boolean;
  terrainExaggeration?: number;
  elevationStages?: readonly TimelineStage[];
  elevationCurtainData?: ElevationCurtainData | null;
  maxDistanceM?: number;
  transferArcs?: readonly TransferArcSegment[];
  showTransferArcs?: boolean;
  climbPins?: readonly ClimbProgressMarker[];
  showClimbCallouts?: boolean;
}

interface CachedElevationCurtainEntry {
  sig: string;
  byExag: Map<number, ElevationCurtainData>;
}

const ELEVATION_CURTAIN_CACHE = new WeakMap<
  readonly TimelineStage[],
  CachedElevationCurtainEntry
>();
const MAX_CURTAIN_EXAG_ENTRIES = 16;

function computeElevationCurtainData(
  stages: readonly TimelineStage[],
  exaggeration: number,
  maxDistanceM?: number,
): ElevationCurtainData {
  const polygons: ElevationCurtainPolygonDatum[] = [];
  const ridges: ElevationRidgeDatum[] = [];
  const scaleZ = Math.max(0.5, exaggeration) * 1.5;

  for (const stage of stages) {
    const stageRgb = hexToRgbTuple(stage.color, [56, 189, 248]);
    const pts = stage.points.filter(
      (p) =>
        p.elevM !== null &&
        Number.isFinite(p.elevM) &&
        (maxDistanceM === undefined || p.globalDistM <= maxDistanceM + 1),
    );
    if (pts.length < 2) continue;

    // Stride large tracks to keep polygon count crisp and deterministic
    const stride = Math.max(1, Math.floor(pts.length / 240));
    const sampled = pts.filter((_, i) => i % stride === 0 || i === pts.length - 1);
    const ridgePath: [number, number, number][] = [];

    for (let i = 0; i < sampled.length; i++) {
      const p = sampled[i]!;
      const z = Math.max(20, (p.elevM ?? 0) * scaleZ);
      ridgePath.push([p.lonUnwrapped, p.lat, z]);
      if (i > 0) {
        const prev = sampled[i - 1]!;
        if (prev.segmentIdx !== p.segmentIdx) continue;
        const zPrev = Math.max(20, (prev.elevM ?? 0) * scaleZ);
        polygons.push({
          id: `${stage.id}-curtain-${i}`,
          color: [stageRgb[0], stageRgb[1], stageRgb[2], 75],
          polygon: [
            [prev.lonUnwrapped, prev.lat, zPrev],
            [p.lonUnwrapped, p.lat, z],
            [p.lonUnwrapped, p.lat, 0],
            [prev.lonUnwrapped, prev.lat, 0],
          ],
        });
      }
    }

    if (ridgePath.length >= 2) {
      ridges.push({
        id: `${stage.id}-ridge`,
        color: stageRgb,
        path: ridgePath,
      });
    }
  }

  return { polygons, ridges };
}

export function buildElevationCurtainData(
  stages: readonly TimelineStage[],
  exaggeration: number,
  maxDistanceM?: number,
): ElevationCurtainData {
  const normalizedExag = Math.max(0.5, Number.isFinite(exaggeration) ? exaggeration : 1.4);
  if (maxDistanceM !== undefined) {
    return computeElevationCurtainData(stages, normalizedExag, maxDistanceM);
  }

  const sig = stages.map((s) => `${s.id}:${s.color}:${s.points.length}`).join("|");
  let entry = ELEVATION_CURTAIN_CACHE.get(stages);
  if (!entry || entry.sig !== sig) {
    entry = { sig, byExag: new Map() };
    ELEVATION_CURTAIN_CACHE.set(stages, entry);
  }

  const cached = entry.byExag.get(normalizedExag);
  if (cached) {
    return cached;
  }

  const computed = computeElevationCurtainData(stages, normalizedExag, undefined);
  if (entry.byExag.size >= MAX_CURTAIN_EXAG_ENTRIES) {
    const oldestKey = entry.byExag.keys().next().value;
    if (oldestKey !== undefined) {
      entry.byExag.delete(oldestKey);
    }
  }
  entry.byExag.set(normalizedExag, computed);
  return computed;
}

export function buildDeckOverlayProps(
  tracks: readonly VisiblePathSegment[] | readonly TrackFeatureDatum[],
  markerPosition: [number, number],
  options?: DeckOverlayBuildOptions,
): MapboxOverlayProps {
  const widthPx = options?.trackWidthPx ?? 5;
  const markerRgb = hexToRgbTuple(options?.markerColorHex ?? "#FFFFFF", [255, 255, 255]);
  const reachedStops = (options?.stops ?? []).filter((s) => s.reached);
  const photoPins = options?.photoPins ?? [];
  const layers: Layer[] = [];

  if (
    options?.elevationCurtain &&
    (options.elevationCurtainData || (options.elevationStages && options.elevationStages.length > 0))
  ) {
    const { polygons, ridges } =
      options.elevationCurtainData ??
      buildElevationCurtainData(
        options.elevationStages!,
        options.terrainExaggeration ?? 1.4,
        options.maxDistanceM,
      );
    if (polygons.length > 0) {
      layers.push(
        new PolygonLayer<ElevationCurtainPolygonDatum>({
          id: "trip-elevation-curtain",
          data: polygons,
          getPolygon: (d) => d.polygon,
          getFillColor: (d) => d.color,
          extruded: false,
          stroked: false,
          filled: true,
        }),
      );
    }
    if (ridges.length > 0) {
      layers.push(
        new PathLayer<ElevationRidgeDatum>({
          id: "trip-elevation-ridge",
          data: ridges,
          getPath: (d) => d.path,
          getColor: (d) => d.color,
          getWidth: Math.max(2, widthPx * 0.75),
          widthUnits: "pixels",
          capRounded: true,
          jointRounded: true,
        }),
      );
    }
  }

  layers.push(
    new PathLayer<TrackFeatureDatum>({
      id: "trip-route-casing",
      data: [...tracks],
      getPath: (d) => d.path,
      getColor: [13, 19, 25, 190],
      getWidth: widthPx + 3,
      widthUnits: "pixels",
      capRounded: true,
      jointRounded: true,
    }),
    new PathLayer<TrackFeatureDatum>({
      id: "trip-route-paths",
      data: [...tracks],
      getPath: (d) => d.path,
      getColor: (d) => d.color,
      getWidth: widthPx,
      widthUnits: "pixels",
      capRounded: true,
      jointRounded: true,
    }),
  );

  if (options?.showTransferArcs !== false && options?.transferArcs && options.transferArcs.length > 0) {
    const visibleArcs = options.transferArcs.filter((arc) => arc.reached);
    if (visibleArcs.length > 0) {
      layers.push(
        new ArcLayer<TransferArcSegment>({
          id: "trip-transfer-arcs",
          data: visibleArcs,
          getSourcePosition: (d) => d.fromPosition,
          getTargetPosition: (d) => d.toPosition,
          getSourceColor: [56, 189, 248, 220],
          getTargetColor: [245, 158, 11, 220],
          getWidth: 2.5,
          getHeight: 0.35,
          greatCircle: true,
        }),
      );
    }
  }

  if (reachedStops.length > 0) {
    layers.push(
      new ScatterplotLayer<StopMarker>({
        id: "trip-overnight-stops",
        data: reachedStops,
        getPosition: (d) => d.position,
        getFillColor: [20, 28, 36, 240],
        getLineColor: [245, 158, 11, 255],
        lineWidthMinPixels: 2,
        stroked: true,
        radiusUnits: "pixels",
        getRadius: 8,
      }),
    );
  }

  if (options?.showClimbCallouts !== false && options?.climbPins && options.climbPins.length > 0) {
    const reachedClimbs = options.climbPins.filter((c) => c.reached);
    if (reachedClimbs.length > 0) {
      layers.push(
        new ScatterplotLayer<ClimbProgressMarker>({
          id: "trip-climb-pins",
          data: reachedClimbs,
          getPosition: (d) => d.summitPosition,
          getFillColor: [249, 115, 22, 235],
          getLineColor: [255, 255, 255, 255],
          lineWidthMinPixels: 2,
          stroked: true,
          radiusUnits: "pixels",
          getRadius: 7,
        }),
      );
    }
  }

  if (photoPins.length > 0) {
    layers.push(
      new ScatterplotLayer<PhotoPinDatum>({
        id: "trip-photo-pins",
        data: [...photoPins],
        getPosition: (d) => d.position,
        getFillColor: [15, 23, 42, 235],
        getLineColor: [56, 189, 248, 255],
        lineWidthMinPixels: 2,
        stroked: true,
        radiusUnits: "pixels",
        getRadius: 8,
      }),
    );
  }

  layers.push(
    new ScatterplotLayer<{ position: [number, number] }>({
      id: "trip-active-marker-halo",
      data: [{ position: markerPosition }],
      getPosition: (d) => d.position,
      getFillColor: [245, 158, 11, 80],
      stroked: false,
      radiusUnits: "pixels",
      getRadius: Math.max(12, widthPx * 2.2),
    }),
    new ScatterplotLayer<{ position: [number, number] }>({
      id: "trip-active-marker",
      data: [{ position: markerPosition }],
      getPosition: (d) => d.position,
      getFillColor: markerRgb,
      getLineColor: [13, 19, 25, 255],
      lineWidthMinPixels: 2.5,
      stroked: true,
      radiusUnits: "pixels",
      getRadius: Math.max(6, widthPx * 1.15),
    }),
  );

  return {
    interleaved: true,
    layers: layers as LayersList,
  };
}
