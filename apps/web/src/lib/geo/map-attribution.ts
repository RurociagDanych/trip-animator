import type { MapStylePreset } from "../api/types";

export const OSM_CONTRIBUTORS_CREDIT = "© OpenStreetMap contributors";

const BASEMAP_CREDITS = ["© OpenFreeMap", "© OpenMapTiles", OSM_CONTRIBUTORS_CREDIT] as const;

/**
 * Returns the credit lines the map must show for a style preset, in display order.
 *
 * `markedTrailsDataDate` is the date of the Marked Trails data and is passed only while the
 * overlay is on with a published build; it follows the OpenStreetMap credit. Keeping it here
 * serves the editor, shared playback and films from one place, and a render can pass the
 * date of the build it pinned.
 */
export function buildMapAttributionCredits(
  styleId: MapStylePreset,
  markedTrailsDataDate?: string | null,
): readonly string[] {
  const credits: string[] = [...BASEMAP_CREDITS];
  if (markedTrailsDataDate) {
    credits.push(`Marked Trails data: ${markedTrailsDataDate}`);
  }
  if (styleId === "topo-relief") {
    credits.push("Elevation: Mapzen Terrain Tiles");
  }
  if (styleId === "satellite-hybrid") {
    credits.push("Imagery: Esri, Maxar, Earthstar Geographics, and the GIS User Community");
  }
  return credits;
}
