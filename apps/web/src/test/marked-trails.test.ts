import type { StyleSpecification } from "maplibre-gl";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type MapStylePreset,
  type MarkedTrailType,
  createDefaultRenderSpecV2,
  normalizeRenderSpecV2,
} from "../lib/api/types";
import {
  MARKED_TRAILS_CYCLING_LAYER_ID,
  MARKED_TRAILS_HIKING_LAYER_ID,
  MARKED_TRAILS_MTB_LAYER_ID,
  MARKED_TRAILS_LABEL_LAYER_IDS,
  MARKED_TRAILS_SOURCE_ID,
  mapStyleKey,
  resolveMarkedTrailsOverlay,
  sanitizeMapStyle,
} from "../lib/geo/map-config";
import {
  buildMarkedTrailsBuild,
  fetchMarkedTrailsBuild,
  markedTrailsDataDate,
  parseMarkedTrailsPointer,
  toggleMarkedTrailType,
} from "../lib/geo/marked-trails";

const PRESETS: MapStylePreset[] = [
  "liberty",
  "positron",
  "bright",
  "dark",
  "topo-relief",
  "satellite-hybrid",
  "editorial-mono",
];

const PMTILES_URL = "/tiles/marked-trails/builds/2026-11-27/marked-trails.pmtiles";

function basemapStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {
      openmaptiles: { type: "vector", url: "https://tiles.openfreemap.org/planet" },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": "#f4f1ea" } },
      {
        id: "water",
        type: "fill",
        source: "openmaptiles",
        "source-layer": "water",
        paint: { "fill-color": "#9ec9e8" },
      },
      {
        id: "roads",
        type: "line",
        source: "openmaptiles",
        "source-layer": "transportation",
        paint: { "line-color": "#ffffff" },
      },
      {
        id: "place-label",
        type: "symbol",
        source: "openmaptiles",
        "source-layer": "place",
        layout: { "text-field": "{name}" },
      },
    ],
  } as StyleSpecification;
}

describe("Marked Trails map style", () => {
  it.each(PRESETS)("draws the hiking Marked Trail line under labels for %s", (preset) => {
    const style = sanitizeMapStyle(basemapStyle(), preset, {
      markedTrailTypes: ["hiking"],
      pmtilesUrl: PMTILES_URL,
    });

    expect(style.sources[MARKED_TRAILS_SOURCE_ID]).toEqual({
      type: "vector",
      url: `pmtiles://${PMTILES_URL}`,
      maxzoom: 13,
    });
    const index = style.layers.findIndex((layer) => layer.id === MARKED_TRAILS_HIKING_LAYER_ID);
    expect(index).toBeGreaterThanOrEqual(0);
    expect(style.layers[index]).toMatchObject({
      type: "line",
      source: MARKED_TRAILS_SOURCE_ID,
      "source-layer": "marked_trails",
      filter: ["==", ["get", "type"], "hiking"],
      paint: { "line-color": ["to-color", ["get", "colour"], "#b91c1c"] },
    });
    const firstSymbol = style.layers.findIndex((layer) => layer.type === "symbol");
    expect(index).toBeLessThan(firstSymbol);
  });

  it.each(PRESETS)("adds no Marked Trail source or layer when off for %s", (preset) => {
    const offStates = [
      undefined,
      { markedTrailTypes: [] as MarkedTrailType[], pmtilesUrl: PMTILES_URL },
      { markedTrailTypes: ["hiking"] as MarkedTrailType[], pmtilesUrl: null },
    ];
    for (const markedTrails of offStates) {
      const style = sanitizeMapStyle(basemapStyle(), preset, markedTrails);
      expect(style.sources[MARKED_TRAILS_SOURCE_ID]).toBeUndefined();
      expect(style.layers.some((layer) => layer.id === MARKED_TRAILS_HIKING_LAYER_ID)).toBe(false);
    }
  });
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- style expressions are untyped nested arrays
type LooseLayer = Record<string, any>;

const ALL_TYPES: MarkedTrailType[] = ["hiking", "cycling", "mtb"];
const LINE_LAYER_IDS = {
  hiking: MARKED_TRAILS_HIKING_LAYER_ID,
  cycling: MARKED_TRAILS_CYCLING_LAYER_ID,
  mtb: MARKED_TRAILS_MTB_LAYER_ID,
} as const;

function markedTrailLayerIds(style: StyleSpecification): string[] {
  return style.layers.filter((l) => l.id.startsWith("trip-marked-trails")).map((l) => l.id);
}

describe("Marked Trails cycling and MTB styling", () => {
  it.each(PRESETS)("draws only the enabled types, each below the route, for %s", (preset) => {
    for (const types of [["cycling"], ["mtb"], ["hiking", "mtb"], ALL_TYPES] as MarkedTrailType[][]) {
      const style = sanitizeMapStyle(basemapStyle(), preset, {
        markedTrailTypes: types,
        pmtilesUrl: PMTILES_URL,
      });
      for (const type of ALL_TYPES) {
        const lineLayers = style.layers.filter((l) => l.id === LINE_LAYER_IDS[type]);
        expect(lineLayers.length, `${type} in ${types.join(",")}`).toBe(types.includes(type) ? 1 : 0);
      }
      const ids = markedTrailLayerIds(style);
      expect(ids.every((id) => !id.includes("undefined"))).toBe(true);
      for (const type of ALL_TYPES.filter((t) => !types.includes(t))) {
        expect(ids.some((id) => id.includes(type))).toBe(false);
      }
    }
  });

  it("filters each type from the same source and gives cycling and MTB their own dash and colour", () => {
    const style = sanitizeMapStyle(basemapStyle(), "liberty", {
      markedTrailTypes: ALL_TYPES,
      pmtilesUrl: PMTILES_URL,
    });
    const byId = new Map(style.layers.map((l) => [l.id, l as unknown as LooseLayer]));
    const colours = new Set<string>();
    const dashes = new Set<string>();
    for (const type of ALL_TYPES) {
      const layer = byId.get(LINE_LAYER_IDS[type])!;
      expect(layer.source).toBe(MARKED_TRAILS_SOURCE_ID);
      expect(layer.filter).toEqual(["==", ["get", "type"], type]);
      expect(layer.paint["line-color"][0]).toBe("to-color");
      expect(layer.paint["line-color"][1]).toEqual(["get", "colour"]);
      colours.add(layer.paint["line-color"][2]);
      dashes.add(JSON.stringify(layer.paint["line-dasharray"] ?? null));
    }
    expect(colours.size).toBe(3);
    expect(dashes.size).toBe(3);
    expect(byId.get(MARKED_TRAILS_HIKING_LAYER_ID)!.paint["line-dasharray"]).toBeUndefined();
  });

  it("scales line width by network level and treats an unknown network as local", () => {
    const style = sanitizeMapStyle(basemapStyle(), "liberty", {
      markedTrailTypes: ["cycling"],
      pmtilesUrl: PMTILES_URL,
    });
    const width = (style.layers.find((l) => l.id === MARKED_TRAILS_CYCLING_LAYER_ID) as unknown as LooseLayer).paint[
      "line-width"
    ];
    expect(width[0]).toBe("interpolate");
    expect(width[2]).toEqual(["zoom"]);
    for (let i = 3; i < width.length; i += 2) {
      const match = width[i + 1];
      expect(match[0]).toBe("match");
      expect(match[1]).toEqual(["get", "network"]);
      const widths = new Map<string, number>();
      for (let j = 2; j + 1 < match.length - 1; j += 2) {
        widths.set(match[j], match[j + 1]);
      }
      const fallback = match[match.length - 1];
      expect(widths.get("international")!).toBeGreaterThan(widths.get("national")!);
      expect(widths.get("national")!).toBeGreaterThan(widths.get("regional")!);
      expect(widths.get("regional")!).toBeGreaterThan(fallback);
      expect(widths.has("local")).toBe(false);
    }
  });

  it("draws every type from z5 semi-transparent and relies on the tiles for the z9 local cut", () => {
    const style = sanitizeMapStyle(basemapStyle(), "liberty", {
      markedTrailTypes: ALL_TYPES,
      pmtilesUrl: PMTILES_URL,
    });
    for (const type of ALL_TYPES) {
      const layer = style.layers.find((l) => l.id === LINE_LAYER_IDS[type]) as unknown as LooseLayer;
      expect(layer.minzoom).toBe(5);
      expect(layer.paint["line-opacity"]).toBeLessThan(1);
    }
  });

  it("labels trails with ref or name from z13 and keeps the labels below the route", () => {
    const style = sanitizeMapStyle(basemapStyle(), "liberty", {
      markedTrailTypes: ALL_TYPES,
      pmtilesUrl: PMTILES_URL,
    });
    expect(MARKED_TRAILS_LABEL_LAYER_IDS.length).toBe(3);
    for (const id of MARKED_TRAILS_LABEL_LAYER_IDS) {
      const label = style.layers.find((l) => l.id === id) as unknown as LooseLayer;
      expect(label.type).toBe("symbol");
      expect(label.minzoom).toBe(13);
      expect(label.layout["symbol-placement"]).toBe("line");
      expect(label.layout["text-field"]).toEqual(["coalesce", ["get", "ref"], ["get", "name"]]);
    }
  });
});

function darkLikeBasemap(): StyleSpecification {
  const line = (id: string) => ({ id, type: "line", source: "openmaptiles", "source-layer": "transportation" });
  const symbol = (id: string) => ({
    id,
    type: "symbol",
    source: "openmaptiles",
    "source-layer": "transportation",
    layout: { "text-field": "{name}" },
  });
  return {
    version: 8,
    sources: { openmaptiles: { type: "vector", url: "https://tiles.openfreemap.org/planet" } },
    layers: [
      { id: "background", type: "background" },
      symbol("road_one_way_arrow"),
      { id: "water", type: "fill", source: "openmaptiles", "source-layer": "water" },
      line("highway_path"),
      line("highway_minor"),
      line("railway"),
      line("bridge_path_pedestrian"),
      { id: "building", type: "fill", source: "openmaptiles", "source-layer": "building" },
      symbol("highway_name_other"),
      { id: "boundary_state", type: "line", source: "openmaptiles", "source-layer": "boundary" },
      symbol("label_city"),
    ],
  } as unknown as StyleSpecification;
}

describe("Marked Trails layer order", () => {
  it("draws every trail line above roads, paths, railways, bridges and buildings and below basemap labels", () => {
    const style = sanitizeMapStyle(darkLikeBasemap(), "dark", {
      markedTrailTypes: ALL_TYPES,
      pmtilesUrl: PMTILES_URL,
    });
    const index = (id: string) => style.layers.findIndex((l) => l.id === id);
    const below = ["highway_path", "highway_minor", "railway", "bridge_path_pedestrian", "building"];
    const above = ["road_one_way_arrow", "highway_name_other", "label_city"];
    for (const type of ALL_TYPES) {
      for (const id of below) {
        expect(index(LINE_LAYER_IDS[type]), `${type} above ${id}`).toBeGreaterThan(index(id));
      }
      for (const id of above.slice(1)) {
        expect(index(LINE_LAYER_IDS[type]), `${type} below ${id}`).toBeLessThan(index(id));
      }
    }
  });

  it("keeps every trail line below every basemap symbol that follows the last road, rail or building layer", () => {
    const style = sanitizeMapStyle(darkLikeBasemap(), "dark", { markedTrailTypes: ALL_TYPES, pmtilesUrl: PMTILES_URL });
    const ids = style.layers.map((l) => l.id);
    const lastBuilding = ids.indexOf("building");
    const symbolsAfter = ids.filter((id, i) => i > lastBuilding && !id.startsWith("trip-marked-trails") && style.layers[i]?.type === "symbol");
    expect(symbolsAfter).toEqual(["highway_name_other", "label_city"]);
    for (const type of ALL_TYPES) {
      for (const id of symbolsAfter) {
        expect(ids.indexOf(LINE_LAYER_IDS[type]), `${type} below ${id}`).toBeLessThan(ids.indexOf(id));
      }
    }
  });

  it.each(PRESETS)("orders lines above basemap lines and fills, below basemap symbols, labels last for %s", (preset) => {
    for (const basemap of [basemapStyle(), darkLikeBasemap()]) {
      const style = sanitizeMapStyle(basemap, preset, { markedTrailTypes: ALL_TYPES, pmtilesUrl: PMTILES_URL });
      const isTrail = (l: { id: string }) => l.id.startsWith("trip-marked-trails");
      const lineIdx = style.layers.flatMap((l, i) => (isTrail(l) && l.type === "line" ? [i] : []));
      const labelIdx = style.layers.flatMap((l, i) => (isTrail(l) && l.type === "symbol" ? [i] : []));
      const isBoundary = (l: object) => (l as { "source-layer"?: string })["source-layer"] === "boundary";
      const baseNonSymbol = style.layers.flatMap((l, i) =>
        !isTrail(l) && l.type !== "symbol" && !isBoundary(l) ? [i] : [],
      );
      const baseSymbols = style.layers.flatMap((l, i) => (!isTrail(l) && l.type === "symbol" ? [i] : []));
      expect(lineIdx.length).toBe(3);
      expect(Math.min(...lineIdx)).toBeGreaterThan(Math.max(...baseNonSymbol));
      const lastBasemapSymbol = Math.max(...baseSymbols);
      expect(lineIdx.every((i) => i < lastBasemapSymbol)).toBe(true);
      expect(labelIdx).toEqual([style.layers.length - 3, style.layers.length - 2, style.layers.length - 1]);
    }
  });

  it("uses the Noto Sans Regular font on every trail label", () => {
    const style = sanitizeMapStyle(basemapStyle(), "liberty", { markedTrailTypes: ALL_TYPES, pmtilesUrl: PMTILES_URL });
    for (const id of MARKED_TRAILS_LABEL_LAYER_IDS) {
      const label = style.layers.find((l) => l.id === id) as unknown as LooseLayer;
      expect(label.layout["text-font"]).toEqual(["Noto Sans Regular"]);
    }
  });
});

describe("Marked Trails line legibility", () => {
  const layerFor = (type: MarkedTrailType, preset: MapStylePreset = "liberty") =>
    sanitizeMapStyle(basemapStyle(), preset, { markedTrailTypes: ALL_TYPES, pmtilesUrl: PMTILES_URL }).layers.find(
      (l) => l.id === LINE_LAYER_IDS[type],
    ) as unknown as LooseLayer;

  it.each([2, 1.5])("keeps every dash and gap at least 2 px long at a %s px line width", (width) => {
    for (const type of ["cycling", "mtb"] as const) {
      for (const unit of layerFor(type).paint["line-dasharray"] as number[]) {
        expect(unit * width, `${type} unit ${unit}`).toBeGreaterThanOrEqual(2);
      }
    }
  });

  it("draws hiking at the bottom, cycling in the middle and mtb on top without line offsets", () => {
    const ids = sanitizeMapStyle(basemapStyle(), "liberty", { markedTrailTypes: ALL_TYPES, pmtilesUrl: PMTILES_URL }).layers.map((l) => l.id);
    expect(ids.indexOf(LINE_LAYER_IDS.hiking)).toBeLessThan(ids.indexOf(LINE_LAYER_IDS.cycling));
    expect(ids.indexOf(LINE_LAYER_IDS.cycling)).toBeLessThan(ids.indexOf(LINE_LAYER_IDS.mtb));
    for (const type of ALL_TYPES) {
      expect(layerFor(type).paint["line-offset"], `${type} offset`).toBeUndefined();
    }
  });

  it("makes hiking wider than cycling and cycling wider than mtb for every network at every zoom stop", () => {
    const widths = (type: MarkedTrailType) => {
      const [, , , ...pairs] = layerFor(type).paint["line-width"] as unknown[];
      return pairs.map((item) => (Array.isArray(item) ? (item as unknown[]).filter((v) => typeof v === "number") : item));
    };
    const hiking = widths("hiking");
    const cycling = widths("cycling");
    const mtb = widths("mtb");
    expect(hiking.length).toBe(6);
    hiking.forEach((stop, i) => {
      if (!Array.isArray(stop)) {
        return;
      }
      (stop as number[]).forEach((hikingWidth, j) => {
        const cyclingWidth = (cycling[i] as number[])[j] ?? 0;
        const mtbWidth = (mtb[i] as number[])[j] ?? 0;
        expect(hikingWidth, `hiking > cycling at stop ${i} value ${j}`).toBeGreaterThan(cyclingWidth);
        expect(cyclingWidth, `cycling > mtb at stop ${i} value ${j}`).toBeGreaterThan(mtbWidth);
      });
    });
  });

  it("uses a light label colour with a dark halo on dark and satellite presets only", () => {
    const labelPaint = (preset: MapStylePreset) =>
      (
        sanitizeMapStyle(basemapStyle(), preset, { markedTrailTypes: ["hiking"], pmtilesUrl: PMTILES_URL }).layers.find(
          (l) => l.id === MARKED_TRAILS_LABEL_LAYER_IDS[0],
        ) as unknown as LooseLayer
      ).paint;
    for (const preset of ["dark", "satellite-hybrid"] as MapStylePreset[]) {
      expect(labelPaint(preset)["text-color"]).toBe("#f3f4f6");
      expect(labelPaint(preset)["text-halo-color"]).toBe("rgba(17,24,39,0.9)");
    }
    for (const preset of ["liberty", "positron", "bright", "topo-relief", "editorial-mono"] as MapStylePreset[]) {
      expect(labelPaint(preset)["text-color"]).toBe("#1f2937");
    }
  });
});

describe("Marked Trails style key", () => {
  it("does not depend on the order the types were switched on", () => {
    const a = mapStyleKey("liberty", resolveMarkedTrailsOverlay(["cycling", "hiking"], PMTILES_URL));
    const b = mapStyleKey("liberty", resolveMarkedTrailsOverlay(["hiking", "cycling"], PMTILES_URL));

    expect(a).toBe(b);
  });

  it("does not change when a build is published while the overlay is off", () => {
    const withoutBuild = mapStyleKey("liberty", resolveMarkedTrailsOverlay([], null));
    const withBuild = mapStyleKey("liberty", resolveMarkedTrailsOverlay([], PMTILES_URL));

    expect(withBuild).toBe(withoutBuild);
    expect(resolveMarkedTrailsOverlay([], PMTILES_URL).pmtilesUrl).toBeNull();
  });

  it("changes when hiking Marked Trails are switched on with a published build", () => {
    const off = mapStyleKey("liberty", resolveMarkedTrailsOverlay([], PMTILES_URL));
    const on = mapStyleKey("liberty", resolveMarkedTrailsOverlay(["hiking"], PMTILES_URL));

    expect(on).not.toBe(off);
  });
});

describe("Marked Trails toggles", () => {
  it("adds or removes only the toggled Marked Trail type", () => {
    expect(toggleMarkedTrailType(["cycling"], "hiking", true)).toEqual(["cycling", "hiking"]);
    expect(toggleMarkedTrailType(["cycling", "hiking"], "hiking", false)).toEqual(["cycling"]);
    expect(toggleMarkedTrailType(["hiking"], "hiking", true)).toEqual(["hiking"]);
    expect(toggleMarkedTrailType<MarkedTrailType>([], "hiking", false)).toEqual([]);
  });
});

describe("Marked Trails build pointer", () => {
  it("resolves the PMTiles URL of the published build from current.json", () => {
    const pointer = parseMarkedTrailsPointer({
      schema_version: 1,
      region: "europe/andorra",
      current_build_id: "2026-11-27",
      previous_build_id: null,
      pmtiles_key: "builds/2026-11-27/marked-trails.pmtiles",
      osm_data_date: "2026-11-26T20:21:03Z",
      size_bytes: 1234,
      sha256: "ab",
      built_at: "2026-11-27T10:00:00+00:00",
    });

    expect(pointer).not.toBeNull();
    expect(buildMarkedTrailsBuild("/tiles/marked-trails/", pointer!)).toEqual({
      buildId: "2026-11-27",
      dataDate: "2026-11-26T20:21:03Z",
      pmtilesUrl: PMTILES_URL,
    });
  });

  it("rejects a pointer whose key escapes the build prefix or is missing", () => {
    expect(parseMarkedTrailsPointer({ current_build_id: "x" })).toBeNull();
    expect(parseMarkedTrailsPointer(null)).toBeNull();
    for (const key of [
      "../secrets/marked-trails.pmtiles",
      "builds/%2e%2e/%2e%2e/api/v1/me",
      "builds/..\\..\\api",
      "builds/2026-11-27/../../api/marked-trails.pmtiles",
      "builds/2026-11-27/marked-trails.pmtiles?x=1",
      "builds/latest/marked-trails.pmtiles",
      "builds/2026-11-27/other.pmtiles",
    ]) {
      expect(parseMarkedTrailsPointer({ current_build_id: "x", pmtiles_key: key }), key).toBeNull();
    }
  });

  it("accepts the build key of a second build on the same day", () => {
    const pointer = parseMarkedTrailsPointer({
      current_build_id: "2026-11-27-2",
      pmtiles_key: "builds/2026-11-27-2/marked-trails.pmtiles",
    });

    expect(pointer?.pmtiles_key).toBe("builds/2026-11-27-2/marked-trails.pmtiles");
  });
});

describe("Marked Trails render settings", () => {
  it("defaults to off and drops unknown trail types", () => {
    expect(createDefaultRenderSpecV2().marked_trail_types).toEqual([]);
    const normalized = normalizeRenderSpecV2({
      marked_trail_types: ["hiking", "skiing"] as unknown as MarkedTrailType[],
    });
    expect(normalized.marked_trail_types).toEqual(["hiking"]);
  });
});

describe("Marked Trails style without a published build", () => {
  it.each(PRESETS)("builds the plain basemap with all three types enabled for %s", (preset) => {
    const withoutBuild = sanitizeMapStyle(
      basemapStyle(),
      preset,
      resolveMarkedTrailsOverlay(["hiking", "cycling", "mtb"], null),
    );

    expect(withoutBuild).toEqual(sanitizeMapStyle(basemapStyle(), preset));
    expect(withoutBuild.layers.some((layer) => layer.id.startsWith("trip-marked-trails"))).toBe(
      false,
    );
    expect(withoutBuild.sources[MARKED_TRAILS_SOURCE_ID]).toBeUndefined();
  });
});

describe("Marked Trails data date", () => {
  const build = (buildId: string, dataDate: string | null) =>
    buildMarkedTrailsBuild("/t", {
      current_build_id: buildId,
      pmtiles_key: `builds/${buildId}/marked-trails.pmtiles`,
      osm_data_date: dataDate,
    });

  it("uses the day of osm_data_date", () => {
    expect(markedTrailsDataDate(build("2026-11-27", "2026-11-26T21:21:00Z"))).toBe("2026-11-26");
  });

  it("falls back to the build date, ignoring a same-day suffix", () => {
    expect(markedTrailsDataDate(build("2026-11-27-2", null))).toBe("2026-11-27");
  });

  it("is null without a build or a usable date", () => {
    expect(markedTrailsDataDate(null)).toBeNull();
    expect(markedTrailsDataDate(build("manual", "not a date"))).toBeNull();
  });
});

describe("Marked Trails build lookup", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  function stubPointerResponse(response: Response) {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
  }

  it.each([403, 404])("reports no build for status %i", async (status) => {
    stubPointerResponse(new Response("{}", { status }));
    await expect(fetchMarkedTrailsBuild("/t")).resolves.toBeNull();
  });

  it("reports no build for an invalid or unreadable pointer", async () => {
    stubPointerResponse(new Response('{"current_build_id":"x"}', { status: 200 }));
    await expect(fetchMarkedTrailsBuild("/t")).resolves.toBeNull();
    stubPointerResponse(new Response("<html>", { status: 200 }));
    await expect(fetchMarkedTrailsBuild("/t")).resolves.toBeNull();
  });

  it("returns the build for a valid pointer", async () => {
    stubPointerResponse(
      new Response(
        JSON.stringify({
          current_build_id: "2026-11-27",
          pmtiles_key: "builds/2026-11-27/marked-trails.pmtiles",
          osm_data_date: "2026-11-26T21:21:00Z",
        }),
        { status: 200 },
      ),
    );
    await expect(fetchMarkedTrailsBuild("/t")).resolves.toMatchObject({ buildId: "2026-11-27" });
  });
});
