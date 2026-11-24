/** Exercises route tile selection, decoding, and the fixed settlement overlay. */
import type { Map as MapLibreMap, SymbolLayerSpecification } from "maplibre-gl";
import Pbf from "pbf";
import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeRenderSpecV2 } from "../lib/api/types";
import { installSettlementLabels, loadSettlementLabels, resolveSettlementLabelMode } from "../lib/geo/settlement-labels";

const tileUrl = "https://tiles.openfreemap.org/planet/test/{z}/{x}/{y}.pbf";

function placeTile(): ArrayBuffer {
  const pbf = new Pbf();
  pbf.writeMessage(3, (_, layer) => {
    layer.writeStringField(1, "place");
    layer.writeMessage(2, (_, feature) => {
      feature.writePackedVarint(2, [0, 0, 1, 1]);
      feature.writeVarintField(3, 1);
      feature.writePackedVarint(4, [9, 400, 400]);
    }, null);
    layer.writeStringField(3, "class");
    layer.writeStringField(3, "name");
    for (const value of ["village", "Little Town"]) {
      layer.writeMessage(4, (text, writer) => writer.writeStringField(1, text), value);
    }
    layer.writeVarintField(5, 4096);
    layer.writeVarintField(15, 2);
  }, null);
  return Uint8Array.from(pbf.finish()).buffer;
}

afterEach(() => vi.unstubAllGlobals());

describe("stable route settlement labels", () => {
  it("preserves uniform labels for old trips and resolves only the active stage's override", () => {
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

  it("loads each detailed route tile once and uses a zoom-independent label layer", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ tiles: [tileUrl] }))
      .mockResolvedValueOnce(new Response(placeTile()));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    const labels = await loadSettlementLabels([{ points: [{ lon: 0, lat: 0 }, { lon: 0.01, lat: -0.01 }] }], signal);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith(tileUrl.replace("{z}/{x}/{y}", "11/1024/1024"), { signal });
    expect(labels.features).toEqual([expect.objectContaining({
      geometry: { type: "Point", coordinates: [expect.closeTo(0.00858, 4), expect.closeTo(-0.00858, 4)] },
      properties: { name: "Little Town", label: "Little Town" },
    })]);

    const nativeLayer: SymbolLayerSpecification = {
      id: "label_village", type: "symbol", source: "openmaptiles", "source-layer": "place", minzoom: 9,
      filter: ["==", ["get", "class"], "village"],
    };
    let overlay: SymbolLayerSpecification | undefined;
    const map = {
      getStyle: () => ({ layers: [nativeLayer], glyphs: "https://example.com/{fontstack}/{range}.pbf" }),
      getLayer: (id: string) => id === nativeLayer.id ? nativeLayer : overlay,
      setFilter: vi.fn(), addSource: vi.fn(), removeSource: vi.fn(), removeLayer: vi.fn(),
      addLayer: vi.fn((layer: SymbolLayerSpecification) => { overlay = layer; }),
    };
    const uninstall = installSettlementLabels(map as unknown as MapLibreMap, labels);
    expect(overlay).toMatchObject({ source: "trip-settlement-labels", layout: { "text-allow-overlap": true, "text-size": 11 } });
    expect(overlay?.minzoom).toBeUndefined();
    expect(overlay?.maxzoom).toBeUndefined();
    expect(map.addSource).toHaveBeenCalledWith("trip-settlement-labels", { type: "geojson", data: labels });
    expect(map.setFilter.mock.calls[0]?.[1]).not.toEqual(nativeLayer.filter);
    expect(installSettlementLabels(map as unknown as MapLibreMap, labels)).toBeUndefined();
    expect(uninstall).toBeTypeOf("function");
    uninstall?.();
    expect(map.removeLayer).toHaveBeenCalledWith("trip-settlement-labels");
    expect(map.removeSource).toHaveBeenCalledWith("trip-settlement-labels");
    expect(map.setFilter).toHaveBeenLastCalledWith(nativeLayer.id, nativeLayer.filter);
  });

  it("does not install an overlay when the route tiles are unavailable", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ tiles: [tileUrl] }))
      .mockResolvedValueOnce(new Response(null, { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    const labels = await loadSettlementLabels([{ points: [{ lon: 179.9, lat: 1 }] }], new AbortController().signal);
    expect(labels.features).toEqual([]);
    const map = { getStyle: vi.fn() };
    expect(installSettlementLabels(map as unknown as MapLibreMap, labels)).toBeUndefined();
    expect(map.getStyle).not.toHaveBeenCalled();
  });

  it("bounds downloads on long routes while including the first and last sampled tiles", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(Response.json({ tiles: [tileUrl] }))
      .mockImplementation(async () => new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const points = Array.from({ length: 100 }, (_, x) => ({ lon: (x + 0.5) * 360 / 2048 - 180, lat: 0 }));
    const signal = new AbortController().signal;
    await loadSettlementLabels([{ points }], signal);
    expect(fetchMock).toHaveBeenCalledTimes(49);
    expect(fetchMock).toHaveBeenNthCalledWith(2, tileUrl.replace("{z}/{x}/{y}", "11/0/1024"), { signal });
    expect(fetchMock).toHaveBeenLastCalledWith(tileUrl.replace("{z}/{x}/{y}", "11/99/1024"), { signal });
  });
});
