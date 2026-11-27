import type { MapStylePreset } from "../api/types";

export const OSM_CONTRIBUTORS_CREDIT = "© OpenStreetMap contributors";

const BASEMAP_CREDITS = ["© OpenFreeMap", "© OpenMapTiles", OSM_CONTRIBUTORS_CREDIT] as const;

/**
 * Returns the credit lines the map must show for a style preset, in display order.
 *
 * Extension point: the Marked Trails overlay appends its data date here as an extra
 * argument, so the credit stays in one place for the editor, shared playback and films.
 */
export function buildMapAttributionCredits(styleId: MapStylePreset): readonly string[] {
  const credits: string[] = [...BASEMAP_CREDITS];
  if (styleId === "topo-relief") {
    credits.push("Elevation: Mapzen Terrain Tiles");
  }
  if (styleId === "satellite-hybrid") {
    credits.push("Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community");
  }
  return credits;
}
