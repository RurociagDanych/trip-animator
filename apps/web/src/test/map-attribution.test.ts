import { describe, expect, it } from "vitest";
import { buildMapAttributionCredits, OSM_CONTRIBUTORS_CREDIT } from "../lib/geo/map-attribution";
import type { MapStylePreset } from "../lib/api/types";

const PRESETS: readonly MapStylePreset[] = [
  "liberty",
  "positron",
  "bright",
  "dark",
  "topo-relief",
  "satellite-hybrid",
  "editorial-mono",
];

describe("buildMapAttributionCredits", () => {
  it("credits OpenFreeMap, OpenMapTiles and OpenStreetMap contributors on OpenFreeMap styles", () => {
    expect(buildMapAttributionCredits("liberty")).toEqual([
      "© OpenFreeMap",
      "© OpenMapTiles",
      "© OpenStreetMap contributors",
    ]);
  });

  it("always includes the OpenStreetMap credit with the copyright sign for every preset", () => {
    expect(OSM_CONTRIBUTORS_CREDIT).toBe("© OpenStreetMap contributors");
    for (const preset of PRESETS) {
      expect(buildMapAttributionCredits(preset)).toContain("© OpenStreetMap contributors");
    }
  });

  it("adds the elevation source for topo-relief, whose hillshade reads Terrarium DEM tiles", () => {
    expect(buildMapAttributionCredits("topo-relief")).toEqual([
      "© OpenFreeMap",
      "© OpenMapTiles",
      "© OpenStreetMap contributors",
      "Elevation: Mapzen Terrain Tiles",
    ]);
  });

  it("adds the Esri imagery credit for satellite-hybrid", () => {
    expect(buildMapAttributionCredits("satellite-hybrid")).toEqual([
      "© OpenFreeMap",
      "© OpenMapTiles",
      "© OpenStreetMap contributors",
      "Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    ]);
  });

  it("adds no preset-specific credit to the plain vector presets", () => {
    for (const preset of ["positron", "bright", "dark", "editorial-mono"] as const) {
      expect(buildMapAttributionCredits(preset)).toHaveLength(3);
    }
  });
});
