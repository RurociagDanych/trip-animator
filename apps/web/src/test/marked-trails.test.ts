import type { StyleSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";
import {
  type MapStylePreset,
  type MarkedTrailType,
  createDefaultRenderSpecV2,
  normalizeRenderSpecV2,
} from "../lib/api/types";
import {
  MARKED_TRAILS_HIKING_LAYER_ID,
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
