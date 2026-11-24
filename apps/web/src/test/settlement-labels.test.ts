import type {
  GeoJSONSource,
  Map as MapLibreMap,
  SymbolLayerSpecification,
} from "maplibre-gl";
import Pbf from "pbf";
import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeRenderSpecV2 } from "../lib/api/types";
import {
  createSettlementLabelController,
  extractTileSettlements,
  pickSettlementDetailZoom,
  resolveSettlementLabelMode,
  routeSettlementTiles,
  viewportSettlementTiles,
  type SettlementLabels,
} from "../lib/geo/settlement-labels";

const tileUrl = "https://tiles.openfreemap.org/planet/test/{z}/{x}/{y}.pbf";

interface FakePlaceFeature {
  cls: string;
  name: string;
  px: number;
  py: number;
  rank?: number;
  latin?: string;
  nonlatin?: string;
}

function buildPlaceTile(features: readonly FakePlaceFeature[]): ArrayBuffer {
  const keys = ["class", "name", "rank", "name:latin", "name:nonlatin"];
  const values: (string | number)[] = [];
  const valueIndex = (val: string | number): number => {
    const existing = values.indexOf(val);
    if (existing >= 0) return existing;
    values.push(val);
    return values.length - 1;
  };
  const encodedFeatures = features.map((f) => {
    const tags: number[] = [0, valueIndex(f.cls), 1, valueIndex(f.name)];
    if (f.rank !== undefined) tags.push(2, valueIndex(f.rank));
    if (f.latin !== undefined) tags.push(3, valueIndex(f.latin));
    if (f.nonlatin !== undefined) tags.push(4, valueIndex(f.nonlatin));
    const zig = (n: number) => (n << 1) ^ (n >> 31);
    return { tags, geometry: [9, zig(f.px), zig(f.py)] };
  });
  const pbf = new Pbf();
  pbf.writeMessage(
    3,
    (_, layer) => {
      layer.writeStringField(1, "place");
      for (const feat of encodedFeatures) {
        layer.writeMessage(
          2,
          (__, writer) => {
            writer.writePackedVarint(2, feat.tags);
            writer.writeVarintField(3, 1);
            writer.writePackedVarint(4, feat.geometry);
          },
          null,
        );
      }
      for (const key of keys) {
        layer.writeStringField(3, key);
      }
      for (const val of values) {
        layer.writeMessage(
          4,
          (v, writer) => {
            if (typeof v === "number") {
              writer.writeVarintField(5, v);
            } else {
              writer.writeStringField(1, v);
            }
          },
          val,
        );
      }
      layer.writeVarintField(5, 4096);
      layer.writeVarintField(15, 2);
    },
    null,
  );
  return Uint8Array.from(pbf.finish()).buffer;
}

function makeMockMap(options?: {
  bounds?: { west: number; south: number; east: number; north: number };
  nativeLayers?: SymbolLayerSpecification[];
}) {
  const nativeLayers: SymbolLayerSpecification[] = options?.nativeLayers ?? [
    {
      id: "label_village",
      type: "symbol",
      source: "openmaptiles",
      "source-layer": "place",
      minzoom: 9,
      filter: ["==", ["get", "class"], "village"],
      layout: {
        "text-field": ["coalesce", ["get", "name_en"], ["get", "name"]],
        "text-font": ["Noto Sans Regular"],
      },
      paint: { "text-color": "#333333" },
    },
    {
      id: "label_town",
      type: "symbol",
      source: "openmaptiles",
      "source-layer": "place",
      minzoom: 6,
      filter: ["==", ["get", "class"], "town"],
      layout: {
        "text-field": ["coalesce", ["get", "name_en"], ["get", "name"]],
        "text-font": ["Noto Sans Regular"],
      },
      paint: { "text-color": "#111111" },
    },
  ];
  const addedLayers = new Map<string, { layer: SymbolLayerSpecification; beforeId?: string }>();
  const sources = new Map<string, { data: SettlementLabels; setData: ReturnType<typeof vi.fn> }>();
  const layoutProps = new Map<string, Map<string, unknown>>();
  let bounds = options?.bounds ?? { west: 0.01, south: -0.2, east: 0.2, north: -0.01 };

  const map = {
    getBounds: () => ({
      getWest: () => bounds.west,
      getSouth: () => bounds.south,
      getEast: () => bounds.east,
      getNorth: () => bounds.north,
    }),
    getStyle: () => ({
      layers: nativeLayers,
      glyphs: "https://example.com/{fontstack}/{range}.pbf",
    }),
    getLayer: (id: string) =>
      nativeLayers.find((l) => l.id === id) ?? addedLayers.get(id)?.layer,
    getSource: (id: string) => sources.get(id) as unknown as GeoJSONSource | undefined,
    addSource: vi.fn((id: string, spec: { type: string; data: SettlementLabels }) => {
      const entry = {
        data: spec.data,
        setData: vi.fn((next: SettlementLabels) => {
          entry.data = next;
        }),
      };
      sources.set(id, entry);
    }),
    removeSource: vi.fn((id: string) => {
      sources.delete(id);
    }),
    addLayer: vi.fn((layer: SymbolLayerSpecification, beforeId?: string) => {
      addedLayers.set(layer.id, { layer, beforeId });
    }),
    removeLayer: vi.fn((id: string) => {
      addedLayers.delete(id);
    }),
    setLayoutProperty: vi.fn((id: string, prop: string, value: unknown) => {
      let layerProps = layoutProps.get(id);
      if (!layerProps) {
        layerProps = new Map();
        layoutProps.set(id, layerProps);
      }
      layerProps.set(prop, value);
    }),
  };

  return {
    map: map as unknown as MapLibreMap,
    raw: map,
    addedLayers,
    sources,
    layoutProps,
    setBounds: (next: { west: number; south: number; east: number; north: number }) => {
      bounds = next;
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("viewport-consistent settlement labels", () => {
  it("preserves uniform labels for old trips and resolves only the active stage override", () => {
    expect(resolveSettlementLabelMode(normalizeRenderSpecV2({}), "city")).toBe("uniform");
    const spec = normalizeRenderSpecV2({
      settlement_label_mode: "uniform",
      stage_settlement_label_modes: { city: "follow_zoom" },
    });
    expect(resolveSettlementLabelMode(spec, "departure")).toBe("uniform");
    expect(resolveSettlementLabelMode(spec, "city")).toBe("follow_zoom");
    expect(resolveSettlementLabelMode(spec, "arrival")).toBe("uniform");
    expect(resolveSettlementLabelMode(spec, undefined)).toBe("uniform");
    const zoomSpec = normalizeRenderSpecV2({
      settlement_label_mode: "follow_zoom",
      stage_settlement_label_modes: { city: "uniform" },
    });
    expect(resolveSettlementLabelMode(zoomSpec, "departure")).toBe("follow_zoom");
    expect(resolveSettlementLabelMode(zoomSpec, "city")).toBe("uniform");
  });

  it("clamps detail zoom to the 9..11 settlement band", () => {
    expect(pickSettlementDetailZoom(undefined)).toBe(9);
    expect(pickSettlementDetailZoom(6.4)).toBe(9);
    expect(pickSettlementDetailZoom(8.98)).toBe(9);
    expect(pickSettlementDetailZoom(9.45)).toBe(9);
    expect(pickSettlementDetailZoom(10.3)).toBe(10);
    expect(pickSettlementDetailZoom(11.9)).toBe(11);
    expect(pickSettlementDetailZoom(15.2)).toBe(11);
  });

  it("extracts all non-city settlements from a vector tile with rank and bilingual labels", () => {
    const buf = buildPlaceTile([
      { cls: "city", name: "Big City", px: 100, py: 100, rank: 2 },
      { cls: "town", name: "Oleszyce", px: 400, py: 400, rank: 8 },
      { cls: "village", name: "Maćkowice", px: 800, py: 800, rank: 11 },
      {
        cls: "village",
        name: "Краковець",
        px: 1200,
        py: 1200,
        latin: "Krakovets",
        nonlatin: "Краковець",
      },
      { cls: "country", name: "Poland", px: 1600, py: 1600, rank: 1 },
    ]);
    const extracted = extractTileSettlements(buf, { z: 9, x: 256, y: 256 });
    expect(extracted.map((f) => [f.properties.class, f.properties.name, f.properties.rank])).toEqual([
      ["town", "Oleszyce", 8],
      ["village", "Maćkowice", 11],
      ["village", "Краковець", undefined],
    ]);
    expect(extracted[2]?.properties["name:latin"]).toBe("Krakovets");
    expect(extracted[2]?.properties["name:nonlatin"]).toBe("Краковець");
    expect(extracted[2]?.properties.label).toBe("Krakovets\nКраковець");
  });

  it("installs zoom-independent cloned layers for the viewport and restores native layers on follow_zoom", async () => {
    const tileBuffer = buildPlaceTile([
      { cls: "town", name: "Oleszyce", px: 500, py: 500, rank: 8 },
      { cls: "village", name: "Cewków", px: 1500, py: 1500, rank: 11 },
    ]);
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url === "https://tiles.openfreemap.org/planet") {
        return Response.json({ tiles: [tileUrl] });
      }
      return new Response(tileBuffer.slice(0));
    });
    vi.stubGlobal("fetch", fetchMock);

    const mock = makeMockMap();
    const onLayersChanged = vi.fn();
    const controller = createSettlementLabelController({
      map: mock.map,
      stages: [{ points: [{ lon: 0.1, lat: -0.1 }] }],
      detailZoom: 9,
      onLayersChanged,
    });

    controller.setEnabled(true);
    await controller.waitForSync(2000);

    const villageLayer = mock.addedLayers.get("trip-settlement-label_village");
    const townLayer = mock.addedLayers.get("trip-settlement-label_town");
    expect(villageLayer?.beforeId).toBe("label_village");
    expect(townLayer?.beforeId).toBe("label_town");
    expect(villageLayer?.layer.minzoom).toBeUndefined();
    expect(villageLayer?.layer.maxzoom).toBeUndefined();
    expect(villageLayer?.layer["source-layer"]).toBeUndefined();
    expect(villageLayer?.layer.source).toBe("trip-settlement-labels");
    expect(villageLayer?.layer.layout?.["symbol-sort-key"]).toEqual([
      "coalesce",
      ["get", "rank"],
      999,
    ]);
    expect(mock.layoutProps.get("label_village")?.get("visibility")).toBe("none");
    expect(mock.layoutProps.get("label_town")?.get("visibility")).toBe("none");

    const source = mock.sources.get("trip-settlement-labels");
    expect(source?.data.features.map((f) => f.properties.name)).toEqual(
      expect.arrayContaining(["Oleszyce", "Cewków"]),
    );

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(10));
    const fetchedUrls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(new Set(fetchedUrls).size).toBe(fetchedUrls.length);

    await controller.syncViewport();
    expect(fetchMock).toHaveBeenCalledTimes(10);

    controller.setEnabled(false);
    expect(mock.raw.removeLayer).toHaveBeenCalledWith("trip-settlement-label_village");
    expect(mock.raw.removeLayer).toHaveBeenCalledWith("trip-settlement-label_town");
    expect(mock.raw.removeSource).toHaveBeenCalledWith("trip-settlement-labels");
    expect(mock.layoutProps.get("label_village")?.get("visibility")).toBe("visible");
    expect(mock.layoutProps.get("label_town")?.get("visibility")).toBe("visible");

    controller.setEnabled(true);
    expect(mock.addedLayers.has("trip-settlement-label_village")).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(10);

    controller.handleStyleLoad();
    expect(mock.addedLayers.has("trip-settlement-label_village")).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(10);

    controller.dispose();
  });

  it("leaves native basemap layers untouched when settlement tiles are unavailable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json({ tiles: [tileUrl] }))
      .mockImplementation(async () => new Response(null, { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);

    const mock = makeMockMap();
    const controller = createSettlementLabelController({
      map: mock.map,
      stages: [{ points: [{ lon: 0.1, lat: -0.1 }] }],
      detailZoom: 9,
    });
    controller.setEnabled(true);
    await controller.waitForSync(2000);

    expect(mock.raw.addSource).not.toHaveBeenCalled();
    expect(mock.raw.addLayer).not.toHaveBeenCalled();
    expect(mock.raw.setLayoutProperty).not.toHaveBeenCalled();
    controller.dispose();
  });

  it("bounds route fallback tiles and returns null when viewport exceeds tile budget", () => {
    const wide = viewportSettlementTiles({ west: -10, south: 35, east: 30, north: 60 }, 9);
    expect(wide).toBeNull();

    const narrow = viewportSettlementTiles({ west: 22.4, south: 52.0, east: 23.2, north: 52.6 }, 9);
    expect(narrow).not.toBeNull();
    expect(narrow!.visible.length).toBeGreaterThan(0);
    expect(narrow!.prefetch.length).toBeGreaterThanOrEqual(narrow!.visible.length);

    const points = Array.from({ length: 100 }, (_, x) => ({
      lon: ((x + 0.5) * 360) / 512 - 180,
      lat: 0,
    }));
    const sampled = routeSettlementTiles([{ points }], 9, 48);
    expect(sampled).toHaveLength(48);
    expect(sampled[0]).toEqual({ z: 9, x: 0, y: 256 });
    expect(sampled[47]).toEqual({ z: 9, x: 99, y: 256 });
  });
});
