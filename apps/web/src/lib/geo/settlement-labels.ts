/** Loads a bounded set of route-side settlement labels independently of camera zoom. */
import { VectorTile } from "@mapbox/vector-tile";
import Pbf from "pbf";
import type { FeatureCollection, Point } from "geojson";
import type { FilterSpecification, Map as MapLibreMap, SymbolLayerSpecification } from "maplibre-gl";

const TILEJSON_URL = "https://tiles.openfreemap.org/planet";
const DETAIL_ZOOM = 11;
const WORLD_TILES = 2 ** DETAIL_ZOOM;
// Bound transfer and label density even on continent-spanning trips.
const MAX_TILES = 48;
const TILE_CONCURRENCY = 6;
const SETTLEMENT_CLASSES = new Set(["town", "village", "hamlet"]);
const LABEL_SOURCE = "trip-settlement-labels";
const LABEL_LAYER = "trip-settlement-labels";
const BASE_LAYERS = new Set(["label_town", "label_village", "label_other", "place_town", "place_village", "place_other"]);

type RoutePoint = { lat: number; lon: number };
type Tile = { x: number; y: number; point: RoutePoint };
export type SettlementLabels = FeatureCollection<Point, { name: string; label: string }>;

function routeTiles(stages: readonly { points: readonly RoutePoint[] }[]): Tile[] {
  const tiles = new Map<string, Tile>();
  for (const { points } of stages) {
    for (const point of points) {
      if (!Number.isFinite(point.lon) || !Number.isFinite(point.lat)) continue;
      const x = ((point.lon + 180) / 360 * WORLD_TILES % WORLD_TILES + WORLD_TILES) % WORLD_TILES;
      const latRad = Math.max(-85.0511, Math.min(85.0511, point.lat)) * Math.PI / 180;
      const y = Math.floor((1 - Math.asinh(Math.tan(latRad)) / Math.PI) * WORLD_TILES / 2);
      const key = `${Math.floor(x)}/${y}`;
      if (!tiles.has(key)) tiles.set(key, { x: Math.floor(x), y, point });
    }
  }
  const ordered = [...tiles.values()];
  if (ordered.length <= MAX_TILES) return ordered;
  return Array.from({ length: MAX_TILES }, (_, i) => ordered[Math.round(i * (ordered.length - 1) / (MAX_TILES - 1))]!);
}

function nearestSettlement(bytes: ArrayBuffer, { x, y, point }: Tile): SettlementLabels["features"][number] | undefined {
  const places = new VectorTile(new Pbf(bytes)).layers.place;
  if (!places) return undefined;
  let best: SettlementLabels["features"][number] | undefined;
  let bestDistance = Infinity;
  const lonScale = Math.cos(point.lat * Math.PI / 180);
  for (let i = 0; i < places.length; i++) {
    const feature = places.feature(i);
    const props = feature.properties;
    if (feature.type !== 1 || typeof props.class !== "string" || !SETTLEMENT_CLASSES.has(props.class) || typeof props.name !== "string" || !props.name) continue;
    const { geometry } = feature.toGeoJSON(x, y, DETAIL_ZOOM);
    if (geometry.type !== "Point") continue;
    const [lon, lat] = geometry.coordinates;
    if (lon === undefined || lat === undefined) continue;
    const deltaLon = ((lon - point.lon + 180) % 360 + 360) % 360 - 180;
    const distance = Math.hypot(deltaLon * lonScale, lat - point.lat);
    if (distance >= bestDistance) continue;
    bestDistance = distance;
    const label = typeof props["name:nonlatin"] === "string" && typeof props["name:latin"] === "string"
      ? `${props["name:latin"]}\n${props["name:nonlatin"]}`
      : typeof props.name_en === "string" && props.name_en ? props.name_en : props.name;
    best = { type: "Feature", geometry, properties: { name: props.name, label } };
  }
  return best;
}

/** Returns at most one nearby label per sampled tile; unavailable tiles retain basemap behavior. */
export async function loadSettlementLabels(
  stages: readonly { points: readonly RoutePoint[] }[],
  signal: AbortSignal,
): Promise<SettlementLabels> {
  const result: SettlementLabels = { type: "FeatureCollection", features: [] };
  const tiles = routeTiles(stages);
  if (!tiles.length) return result;
  const response = await fetch(TILEJSON_URL, { signal });
  if (!response.ok) throw new Error(`Settlement TileJSON request failed (${response.status})`);
  const { tiles: urls } = await response.json() as { tiles?: string[] };
  const template = urls?.[0];
  if (!template?.startsWith("https://tiles.openfreemap.org/planet/")) return result;
  const seen = new Set<string>();
  for (let offset = 0; offset < tiles.length; offset += TILE_CONCURRENCY) {
    signal.throwIfAborted();
    const labels = await Promise.all(tiles.slice(offset, offset + TILE_CONCURRENCY).map(async (tile) => {
      try {
        const url = template.replace("{z}", String(DETAIL_ZOOM)).replace("{x}", String(tile.x)).replace("{y}", String(tile.y));
        const response = await fetch(url, { signal });
        return response.ok ? nearestSettlement(await response.arrayBuffer(), tile) : undefined;
      } catch {
        return undefined;
      }
    }));
    for (const label of labels) {
      if (!label) continue;
      // Vector tile buffers can repeat a point across tiles with slightly different rounding.
      const key = `${label.properties.name}:${label.geometry.coordinates.map((n) => n.toFixed(3)).join(",")}`;
      if (seen.has(key)) continue;
      seen.add(key);
      result.features.push(label);
    }
  }
  signal.throwIfAborted();
  return result;
}

/** Installs fixed route labels and hides their corresponding basemap labels until cleanup. */
export function installSettlementLabels(map: MapLibreMap, labels: SettlementLabels): (() => void) | undefined {
  if (!labels.features.length || map.getLayer(LABEL_LAYER)) return undefined;
  const nativeLayers = map.getStyle()?.layers.filter((layer): layer is SymbolLayerSpecification =>
    layer.type === "symbol" && layer["source-layer"] === "place" && BASE_LAYERS.has(layer.id),
  );
  if (!nativeLayers?.length) return undefined;
  const template = nativeLayers.find(({ id }) => id.endsWith("_village")) ?? nativeLayers[0]!;
  const filters = nativeLayers.map(({ id, filter }) => ({ id, filter }));
  const names = [...new Set(labels.features.map((feature) => feature.properties.name))];
  const exclusion: FilterSpecification = ["!", ["all",
    ["in", ["get", "class"], ["literal", [...SETTLEMENT_CLASSES]]],
    ["in", ["get", "name"], ["literal", names]],
  ]];
  for (const { id, filter } of filters) {
    // OpenFreeMap's place filters are expressions, not legacy property filters.
    map.setFilter(id, filter ? ["all", filter, exclusion] as FilterSpecification : exclusion);
  }
  map.addSource(LABEL_SOURCE, { type: "geojson", data: labels });
  map.addLayer({
    id: LABEL_LAYER,
    type: "symbol",
    source: LABEL_SOURCE,
    layout: {
      "text-field": ["get", "label"],
      "text-font": template.layout?.["text-font"],
      "text-size": 11,
      "text-allow-overlap": true,
    },
    paint: {
      "text-color": template.paint?.["text-color"],
      "text-halo-color": template.paint?.["text-halo-color"],
      "text-halo-width": template.paint?.["text-halo-width"],
    },
  });
  return () => {
    if (!map.getLayer(LABEL_LAYER)) return;
    map.removeLayer(LABEL_LAYER);
    map.removeSource(LABEL_SOURCE);
    for (const { id, filter } of filters) if (map.getLayer(id)) map.setFilter(id, filter ?? null);
  };
}
