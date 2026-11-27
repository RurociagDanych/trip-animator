import type { StyleSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";
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

describe("Marked Trails style key", () => {
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
