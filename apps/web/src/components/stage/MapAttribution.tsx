import type { MapStylePreset } from "../../lib/api/types";
import { buildMapAttributionCredits } from "../../lib/geo/map-attribution";

/**
 * Map credit overlay in the stage's bottom-right corner.
 *
 * Lives in the stage React tree rather than MapLibre's attribution control because
 * the render route captures this DOM into MP4 frames; the MapLibre control is not in them.
 */
export function MapAttribution({
  styleId,
  markedTrailsDataDate = null,
}: {
  styleId: MapStylePreset;
  markedTrailsDataDate?: string | null;
}) {
  const credits = buildMapAttributionCredits(styleId, markedTrailsDataDate);
  return (
    <div
      data-testid="map-attribution"
      className="pointer-events-none absolute right-0 bottom-0 z-[70] max-w-full rounded-tl bg-canvas/80 px-2 py-0.5 text-[10px] leading-4 text-ink-muted backdrop-blur-sm"
    >
      {credits.join(" · ")}
    </div>
  );
}
