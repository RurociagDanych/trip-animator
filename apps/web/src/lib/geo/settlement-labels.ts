import { VectorTile } from "@mapbox/vector-tile";
import type { Feature, FeatureCollection, Point } from "geojson";
import type {
  GeoJSONSource,
  Map as MapLibreMap,
  SymbolLayerSpecification,
} from "maplibre-gl";
import Pbf from "pbf";
import type { RenderSpecV2, SettlementLabelMode } from "../api/types";

const TILEJSON_URL = "https://tiles.openfreemap.org/planet";
const MIN_DETAIL_ZOOM = 9;
const MAX_DETAIL_ZOOM = 11;
const MAX_VIEWPORT_TILES = 36;
const MAX_PREFETCH_TILES = 49;
const MAX_ROUTE_TILES = 48;
const MAX_ACTIVE_TILES = 64;
const MAX_CACHE_TILES = 256;
const TILE_CONCURRENCY = 6;
const LABEL_SOURCE = "trip-settlement-labels";
const LABEL_LAYER_PREFIX = "trip-settlement-";
const EXCLUDED_PLACE_CLASSES = new Set(["city", "continent", "country", "state"]);
const BASE_LAYERS = new Set([
  "label_town",
  "label_village",
  "label_other",
  "place_town",
  "place_village",
  "place_other",
]);

export type SettlementRoutePoint = { lat: number; lon: number };
export type SettlementTileCoord = { z: number; x: number; y: number };

export interface SettlementFeatureProperties {
  class: string;
  name: string;
  label: string;
  name_en?: string;
  "name:latin"?: string;
  "name:nonlatin"?: string;
  rank?: number;
}

export type SettlementFeature = Feature<Point, SettlementFeatureProperties>;
export type SettlementLabels = FeatureCollection<Point, SettlementFeatureProperties>;

export interface ViewportBounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface SettlementLabelController {
  setEnabled: (enabled: boolean) => void;
  syncViewport: () => Promise<void>;
  waitForSync: (timeoutMs?: number) => Promise<void>;
  handleStyleLoad: () => void;
  dispose: () => void;
}

export function resolveSettlementLabelMode(
  spec: RenderSpecV2,
  stageId?: string,
): SettlementLabelMode {
  return (
    (stageId ? spec.stage_settlement_label_modes[stageId] : undefined) ??
    spec.settlement_label_mode
  );
}

export function pickSettlementDetailZoom(referenceZoom?: number): number {
  if (typeof referenceZoom !== "number" || !Number.isFinite(referenceZoom)) {
    return MIN_DETAIL_ZOOM;
  }
  return Math.min(
    MAX_DETAIL_ZOOM,
    Math.max(MIN_DETAIL_ZOOM, Math.floor(referenceZoom)),
  );
}

function tileKey(tile: SettlementTileCoord): string {
  return `${tile.z}/${tile.x}/${tile.y}`;
}

function lngLatToTile(lon: number, lat: number, zoom: number): { x: number; y: number } {
  const worldTiles = 2 ** zoom;
  const xNorm = (((lon + 180) / 360) * worldTiles % worldTiles + worldTiles) % worldTiles;
  const clampedLat = Math.max(-85.0511, Math.min(85.0511, lat));
  const latRad = (clampedLat * Math.PI) / 180;
  const yRaw = Math.floor(((1 - Math.asinh(Math.tan(latRad)) / Math.PI) * worldTiles) / 2);
  return {
    x: Math.min(worldTiles - 1, Math.max(0, Math.floor(xNorm))),
    y: Math.min(worldTiles - 1, Math.max(0, yRaw)),
  };
}

export function routeSettlementTiles(
  stages: readonly { points: readonly SettlementRoutePoint[] }[],
  zoom: number,
  maxTiles: number = MAX_ROUTE_TILES,
): SettlementTileCoord[] {
  const tiles = new Map<string, SettlementTileCoord>();
  for (const { points } of stages) {
    for (const point of points) {
      if (!Number.isFinite(point.lon) || !Number.isFinite(point.lat)) continue;
      const { x, y } = lngLatToTile(point.lon, point.lat, zoom);
      const coord: SettlementTileCoord = { z: zoom, x, y };
      const key = tileKey(coord);
      if (!tiles.has(key)) {
        tiles.set(key, coord);
      }
    }
  }
  const ordered = [...tiles.values()];
  if (ordered.length <= maxTiles) return ordered;
  return Array.from(
    { length: maxTiles },
    (_, i) => ordered[Math.round((i * (ordered.length - 1)) / (maxTiles - 1))]!,
  );
}

export function viewportSettlementTiles(
  bounds: ViewportBounds,
  zoom: number,
  options?: { maxVisibleTiles?: number; maxPrefetchTiles?: number },
): { visible: SettlementTileCoord[]; prefetch: SettlementTileCoord[] } | null {
  if (
    !Number.isFinite(bounds.west) ||
    !Number.isFinite(bounds.south) ||
    !Number.isFinite(bounds.east) ||
    !Number.isFinite(bounds.north)
  ) {
    return null;
  }
  const maxVisible = options?.maxVisibleTiles ?? MAX_VIEWPORT_TILES;
  const maxPrefetch = options?.maxPrefetchTiles ?? MAX_PREFETCH_TILES;
  const worldTiles = 2 ** zoom;
  const lonSpan = bounds.east - bounds.west;
  if (lonSpan >= 359 || lonSpan < 0 && lonSpan + 360 >= 359) {
    return null;
  }

  const nw = lngLatToTile(bounds.west, bounds.north, zoom);
  const se = lngLatToTile(bounds.east, bounds.south, zoom);
  const yMin = Math.min(nw.y, se.y);
  const yMax = Math.max(nw.y, se.y);
  const yCount = yMax - yMin + 1;

  const xValues: number[] = [];
  if (bounds.east >= bounds.west && nw.x <= se.x) {
    for (let x = nw.x; x <= se.x; x++) xValues.push(x);
  } else {
    const wrappedSpan = (se.x - nw.x + worldTiles) % worldTiles + 1;
    if (wrappedSpan * yCount > maxVisible) return null;
    for (let step = 0; step < wrappedSpan; step++) {
      xValues.push((nw.x + step) % worldTiles);
    }
  }

  const visibleCount = xValues.length * yCount;
  if (visibleCount === 0 || visibleCount > maxVisible) {
    return null;
  }

  const visible: SettlementTileCoord[] = [];
  for (const x of xValues) {
    for (let y = yMin; y <= yMax; y++) {
      visible.push({ z: zoom, x, y });
    }
  }

  const expandedXCount = Math.min(worldTiles, xValues.length + 2);
  const expandedYMin = Math.max(0, yMin - 1);
  const expandedYMax = Math.min(worldTiles - 1, yMax + 1);
  const expandedCount = expandedXCount * (expandedYMax - expandedYMin + 1);
  if (expandedCount > maxPrefetch) {
    return { visible, prefetch: visible };
  }

  const prefetchX = new Set<number>(xValues);
  prefetchX.add((xValues[0]! - 1 + worldTiles) % worldTiles);
  prefetchX.add((xValues[xValues.length - 1]! + 1) % worldTiles);
  const prefetch: SettlementTileCoord[] = [];
  for (const x of prefetchX) {
    for (let y = expandedYMin; y <= expandedYMax; y++) {
      prefetch.push({ z: zoom, x, y });
    }
  }
  return { visible, prefetch };
}

export function extractTileSettlements(
  bytes: ArrayBuffer,
  tile: SettlementTileCoord,
): SettlementFeature[] {
  const places = new VectorTile(new Pbf(bytes)).layers.place;
  if (!places) return [];
  const features: SettlementFeature[] = [];
  for (let i = 0; i < places.length; i++) {
    const feature = places.feature(i);
    if (feature.type !== 1) continue;
    const props = feature.properties;
    const cls = props.class;
    const name = props.name;
    if (
      typeof cls !== "string" ||
      EXCLUDED_PLACE_CLASSES.has(cls) ||
      typeof name !== "string" ||
      !name
    ) {
      continue;
    }
    const { geometry } = feature.toGeoJSON(tile.x, tile.y, tile.z);
    if (geometry.type !== "Point") continue;
    const [lon, lat] = geometry.coordinates;
    if (lon === undefined || lat === undefined) continue;
    const latin = typeof props["name:latin"] === "string" ? props["name:latin"] : undefined;
    const nonlatin =
      typeof props["name:nonlatin"] === "string" ? props["name:nonlatin"] : undefined;
    const nameEn = typeof props.name_en === "string" && props.name_en ? props.name_en : undefined;
    const label = latin && nonlatin ? `${latin}\n${nonlatin}` : (nameEn ?? name);
    const properties: SettlementFeatureProperties = {
      class: cls,
      name,
      label,
    };
    if (nameEn !== undefined) properties.name_en = nameEn;
    if (latin !== undefined) properties["name:latin"] = latin;
    if (nonlatin !== undefined) properties["name:nonlatin"] = nonlatin;
    if (typeof props.rank === "number" && Number.isFinite(props.rank)) {
      properties.rank = props.rank;
    }
    features.push({
      type: "Feature",
      geometry,
      properties,
    });
  }
  return features;
}

function mergeDeduplicatedFeatures(
  tileGroups: readonly (readonly SettlementFeature[])[],
): SettlementLabels {
  const seen = new Set<string>();
  const features: SettlementFeature[] = [];
  for (const group of tileGroups) {
    for (const feature of group) {
      const [lon, lat] = feature.geometry.coordinates;
      const key = `${feature.properties.class}:${feature.properties.name}:${lon!.toFixed(3)},${lat!.toFixed(3)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      features.push(feature);
    }
  }
  return { type: "FeatureCollection", features };
}

function getNativeSettlementLayers(map: MapLibreMap): SymbolLayerSpecification[] {
  const style = map.getStyle();
  if (!style?.glyphs || !Array.isArray(style.layers)) return [];
  return style.layers.filter(
    (layer): layer is SymbolLayerSpecification =>
      layer.type === "symbol" &&
      layer["source-layer"] === "place" &&
      BASE_LAYERS.has(layer.id),
  );
}

function buildUniformSymbolLayer(nativeLayer: SymbolLayerSpecification): SymbolLayerSpecification {
  const cloned = { ...nativeLayer } as Record<string, unknown>;
  delete cloned.minzoom;
  delete cloned.maxzoom;
  delete cloned["source-layer"];
  const layout: Record<string, unknown> = {
    ...(nativeLayer.layout ?? {}),
    visibility: "visible",
    "symbol-sort-key": ["coalesce", ["get", "rank"], 999],
  };
  return {
    ...cloned,
    id: `${LABEL_LAYER_PREFIX}${nativeLayer.id}`,
    type: "symbol",
    source: LABEL_SOURCE,
    layout,
  } as SymbolLayerSpecification;
}

function readMapBounds(map: MapLibreMap): ViewportBounds | null {
  if (typeof map.getBounds !== "function") return null;
  const bounds = map.getBounds();
  if (!bounds) return null;
  const west = bounds.getWest();
  const south = bounds.getSouth();
  const east = bounds.getEast();
  const north = bounds.getNorth();
  if (
    !Number.isFinite(west) ||
    !Number.isFinite(south) ||
    !Number.isFinite(east) ||
    !Number.isFinite(north)
  ) {
    return null;
  }
  return { west, south, east, north };
}

async function fetchTileTemplate(signal: AbortSignal): Promise<string | null> {
  const response = await fetch(TILEJSON_URL, { signal });
  if (!response.ok) return null;
  const { tiles: urls } = (await response.json()) as { tiles?: string[] };
  const template = urls?.[0];
  if (!template?.startsWith("https://tiles.openfreemap.org/planet/")) return null;
  return template;
}

export function createSettlementLabelController(options: {
  map: MapLibreMap;
  stages: readonly { points: readonly SettlementRoutePoint[] }[];
  detailZoom: number;
  onLayersChanged?: () => void;
}): SettlementLabelController {
  const { map, stages, onLayersChanged } = options;
  const detailZoom = Math.min(MAX_DETAIL_ZOOM, Math.max(MIN_DETAIL_ZOOM, Math.round(options.detailZoom)));
  const abortController = new AbortController();
  const tileCache = new Map<string, SettlementFeature[]>();
  const inFlight = new Map<string, Promise<SettlementFeature[] | null>>();
  const routeFallbackTiles = routeSettlementTiles(stages, detailZoom, MAX_ROUTE_TILES);

  let enabled = false;
  let disposed = false;
  let tileTemplatePromise: Promise<string | null> | null = null;
  let activeTileKeys: string[] = [];
  let activeDataSignature = "";
  let currentData: SettlementLabels = { type: "FeatureCollection", features: [] };
  let pendingSync: Promise<void> = Promise.resolve();
  let syncToken = 0;
  let installed: {
    layerIds: string[];
    nativeVisibility: { id: string; visibility: "visible" | "none" }[];
  } | null = null;

  const getTemplate = (): Promise<string | null> => {
    if (!tileTemplatePromise) {
      tileTemplatePromise = fetchTileTemplate(abortController.signal).catch(() => null);
    }
    return tileTemplatePromise;
  };

  const cacheTile = (key: string, features: SettlementFeature[]) => {
    tileCache.delete(key);
    tileCache.set(key, features);
    while (tileCache.size > MAX_CACHE_TILES) {
      const oldest = tileCache.keys().next().value;
      if (oldest === undefined) break;
      tileCache.delete(oldest);
    }
  };

  const fetchSingleTile = (
    template: string,
    tile: SettlementTileCoord,
  ): Promise<SettlementFeature[] | null> => {
    const key = tileKey(tile);
    const cached = tileCache.get(key);
    if (cached) {
      cacheTile(key, cached);
      return Promise.resolve(cached);
    }
    const existing = inFlight.get(key);
    if (existing) return existing;
    const url = template
      .replace("{z}", String(tile.z))
      .replace("{x}", String(tile.x))
      .replace("{y}", String(tile.y));
    const req = fetch(url, { signal: abortController.signal })
      .then(async (response) => {
        if (!response.ok) return null;
        const features = extractTileSettlements(await response.arrayBuffer(), tile);
        cacheTile(key, features);
        return features;
      })
      .catch(() => null)
      .finally(() => {
        inFlight.delete(key);
      });
    inFlight.set(key, req);
    return req;
  };

  const fetchTilesBounded = async (
    template: string,
    tiles: readonly SettlementTileCoord[],
  ): Promise<boolean> => {
    let anySucceeded = false;
    const missing = tiles.filter((t) => !tileCache.has(tileKey(t)));
    if (missing.length === 0) return tiles.length > 0;
    for (let offset = 0; offset < missing.length; offset += TILE_CONCURRENCY) {
      if (disposed || abortController.signal.aborted) return anySucceeded;
      const batch = missing.slice(offset, offset + TILE_CONCURRENCY);
      const results = await Promise.all(batch.map((tile) => fetchSingleTile(template, tile)));
      if (results.some((r) => r !== null)) {
        anySucceeded = true;
      }
    }
    return anySucceeded || tiles.some((t) => tileCache.has(tileKey(t)));
  };

  const uninstallOverlay = () => {
    if (!installed) return;
    const prev = installed;
    installed = null;
    activeDataSignature = "";
    try {
      for (const layerId of prev.layerIds) {
        if (map.getLayer(layerId)) {
          map.removeLayer(layerId);
        }
      }
      if (map.getSource(LABEL_SOURCE)) {
        map.removeSource(LABEL_SOURCE);
      }
      for (const { id, visibility } of prev.nativeVisibility) {
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, "visibility", visibility);
        }
      }
      onLayersChanged?.();
    } catch {}
  };

  const installOrUpdateOverlay = (data: SettlementLabels, signature: string) => {
    if (disposed || !enabled || data.features.length === 0) return;
    try {
      if (!installed) {
        const nativeLayers = getNativeSettlementLayers(map);
        if (nativeLayers.length === 0) return;
        if (map.getSource(LABEL_SOURCE)) {
          for (const nativeLayer of nativeLayers) {
            const id = `${LABEL_LAYER_PREFIX}${nativeLayer.id}`;
            if (map.getLayer(id)) map.removeLayer(id);
          }
          map.removeSource(LABEL_SOURCE);
        }
        map.addSource(LABEL_SOURCE, {
          type: "geojson",
          data,
          buffer: 64,
          maxzoom: 14,
        });
        const layerIds: string[] = [];
        const nativeVisibility: { id: string; visibility: "visible" | "none" }[] = [];
        for (const nativeLayer of nativeLayers) {
          const prevVis =
            nativeLayer.layout?.visibility === "none" ? "none" : "visible";
          nativeVisibility.push({ id: nativeLayer.id, visibility: prevVis });
          const uniformLayer = buildUniformSymbolLayer(nativeLayer);
          map.addLayer(uniformLayer, nativeLayer.id);
          layerIds.push(uniformLayer.id);
          map.setLayoutProperty(nativeLayer.id, "visibility", "none");
        }
        installed = { layerIds, nativeVisibility };
        activeDataSignature = signature;
        onLayersChanged?.();
        return;
      }
      if (signature === activeDataSignature) return;
      const source = map.getSource(LABEL_SOURCE) as GeoJSONSource | undefined;
      if (source && typeof source.setData === "function") {
        source.setData(data);
        activeDataSignature = signature;
        onLayersChanged?.();
      }
    } catch {}
  };

  const selectTargetTiles = (): { visible: SettlementTileCoord[]; prefetch: SettlementTileCoord[] } => {
    const bounds = readMapBounds(map);
    if (bounds) {
      const vp = viewportSettlementTiles(bounds, detailZoom);
      if (vp) return vp;
    }
    return { visible: routeFallbackTiles, prefetch: routeFallbackTiles };
  };

  const commitActiveTiles = (desiredTiles: readonly SettlementTileCoord[]) => {
    const desiredKeys = desiredTiles.map(tileKey).filter((k) => tileCache.has(k));
    if (desiredKeys.length === 0) return;
    const retained = activeTileKeys.filter(
      (k) => tileCache.has(k) && !desiredKeys.includes(k),
    );
    const nextActive = [...desiredKeys, ...retained].slice(
      0,
      Math.max(MAX_ACTIVE_TILES, desiredKeys.length),
    );
    const nextSignature = [...nextActive].sort().join("|");
    if (nextSignature === activeDataSignature && installed) return;
    activeTileKeys = nextActive;
    const groups = nextActive.map((k) => tileCache.get(k)!);
    currentData = mergeDeduplicatedFeatures(groups);
    installOrUpdateOverlay(currentData, nextSignature);
  };

  const syncViewport = (): Promise<void> => {
    if (disposed || !enabled) return Promise.resolve();
    const { visible, prefetch } = selectTargetTiles();
    if (visible.length === 0) return Promise.resolve();

    const activeSet = new Set(activeTileKeys);
    const allVisibleActive = visible.every((t) => activeSet.has(tileKey(t)));
    const allPrefetchCached = prefetch.every((t) => tileCache.has(tileKey(t)));

    if (allVisibleActive && installed) {
      if (!allPrefetchCached) {
        void getTemplate().then((template) => {
          if (!template || disposed || !enabled) return;
          void fetchTilesBounded(template, prefetch);
        });
      }
      return Promise.resolve();
    }

    if (prefetch.every((t) => tileCache.has(tileKey(t)))) {
      commitActiveTiles(prefetch);
      return Promise.resolve();
    }

    const token = ++syncToken;
    const run = (async () => {
      const template = await getTemplate();
      if (!template || disposed || !enabled || token !== syncToken) return;
      const loadedVisible = await fetchTilesBounded(template, visible);
      if (disposed || !enabled || token !== syncToken) return;
      if (loadedVisible) {
        commitActiveTiles(
          prefetch.filter((t) => tileCache.has(tileKey(t))),
        );
      }
      if (prefetch.length > visible.length) {
        const missingPrefetch = prefetch.filter((t) => !tileCache.has(tileKey(t)));
        if (missingPrefetch.length > 0) {
          void fetchTilesBounded(template, missingPrefetch).then((ok) => {
            if (!ok || disposed || !enabled || token !== syncToken) return;
            commitActiveTiles(prefetch);
          });
        }
      }
    })();
    pendingSync = run;
    return run;
  };

  const waitForSync = async (timeoutMs = 4000): Promise<void> => {
    if (disposed || !enabled) return;
    const activeSync = syncViewport();
    let timer: number | undefined;
    try {
      await Promise.race([
        Promise.all([pendingSync, activeSync]),
        new Promise<void>((resolve) => {
          timer = window.setTimeout(resolve, timeoutMs);
        }),
      ]);
    } finally {
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    }
  };

  const setEnabled = (next: boolean) => {
    if (disposed) return;
    enabled = next;
    if (!enabled) {
      syncToken++;
      uninstallOverlay();
      return;
    }
    if (currentData.features.length > 0 && activeTileKeys.length > 0) {
      const signature = [...activeTileKeys].sort().join("|");
      installOrUpdateOverlay(currentData, signature);
    }
    void syncViewport();
  };

  const handleStyleLoad = () => {
    if (disposed) return;
    installed = null;
    activeDataSignature = "";
    if (!enabled) return;
    if (currentData.features.length > 0 && activeTileKeys.length > 0) {
      const signature = [...activeTileKeys].sort().join("|");
      installOrUpdateOverlay(currentData, signature);
    }
    void syncViewport();
  };

  const dispose = () => {
    disposed = true;
    enabled = false;
    syncToken++;
    abortController.abort();
    uninstallOverlay();
    tileCache.clear();
    inFlight.clear();
  };

  return {
    setEnabled,
    syncViewport,
    waitForSync,
    handleStyleLoad,
    dispose,
  };
}
