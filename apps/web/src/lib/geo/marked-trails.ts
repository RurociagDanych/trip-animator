import { useQuery } from "@tanstack/react-query";

export const MARKED_TRAILS_PMTILES_PROTOCOL = "pmtiles";
export const MARKED_TRAILS_POINTER_FILE = "current.json";
// Matches the publisher's layout: build date, optional same-day suffix, fixed file name.
const MARKED_TRAILS_PMTILES_KEY_PATTERN = /^builds\/\d{4}-\d{2}-\d{2}(-\d+)?\/marked-trails\.pmtiles$/;

export interface MarkedTrailsPointer {
  current_build_id: string;
  pmtiles_key: string;
  osm_data_date: string | null;
}

export interface MarkedTrailsBuild {
  buildId: string;
  dataDate: string | null;
  pmtilesUrl: string;
}

/**
 * Validates `current.json`. The key must be a build PMTiles key of the store layout, so a
 * corrupted pointer cannot point the editor at another object. Unknown fields are ignored.
 */
export function parseMarkedTrailsPointer(raw: unknown): MarkedTrailsPointer | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const record = raw as Record<string, unknown>;
  const buildId = record.current_build_id;
  const key = record.pmtiles_key;
  if (typeof buildId !== "string" || buildId.length === 0) {
    return null;
  }
  if (typeof key !== "string" || !MARKED_TRAILS_PMTILES_KEY_PATTERN.test(key)) {
    return null;
  }
  return {
    current_build_id: buildId,
    pmtiles_key: key,
    osm_data_date: typeof record.osm_data_date === "string" ? record.osm_data_date : null,
  };
}

/** Resolves the PMTiles URL of the pointer's build against the store base URL. */
export function buildMarkedTrailsBuild(
  baseUrl: string,
  pointer: MarkedTrailsPointer,
): MarkedTrailsBuild {
  return {
    buildId: pointer.current_build_id,
    dataDate: pointer.osm_data_date,
    pmtilesUrl: `${stripTrailingSlashes(baseUrl)}/${pointer.pmtiles_key}`,
  };
}

/**
 * Returns the published build, or null when the store has no `current.json` yet.
 * Other failures throw, so React Query reports them instead of hiding a broken store.
 */
export async function fetchMarkedTrailsBuild(
  baseUrl: string,
  signal?: AbortSignal,
): Promise<MarkedTrailsBuild | null> {
  const root = stripTrailingSlashes(baseUrl);
  const response = await fetch(`${root}/${MARKED_TRAILS_POINTER_FILE}`, {
    cache: "no-cache",
    signal,
  });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Marked Trails pointer request failed with status ${response.status}`);
  }
  const pointer = parseMarkedTrailsPointer(await response.json());
  return pointer ? buildMarkedTrailsBuild(root, pointer) : null;
}

/**
 * Loads the published build of the store at `baseUrl`. Returns null while loading, when no
 * build is published, when the request fails, or when `baseUrl` is empty (overlay off).
 */
export function useMarkedTrailsBuild(baseUrl: string | undefined): MarkedTrailsBuild | null {
  const query = useQuery({
    queryKey: ["marked-trails", "build", baseUrl ?? ""],
    queryFn: ({ signal }) => fetchMarkedTrailsBuild(baseUrl ?? "", signal),
    enabled: Boolean(baseUrl),
    staleTime: 5 * 60_000,
    retry: false,
  });
  return query.data ?? null;
}

/** Adds or removes one Marked Trail type and keeps the others in their order. */
export function toggleMarkedTrailType<T extends string>(
  markedTrailTypes: readonly T[],
  markedTrailType: T,
  enabled: boolean,
): T[] {
  if (!enabled) {
    return markedTrailTypes.filter((value) => value !== markedTrailType);
  }
  return markedTrailTypes.includes(markedTrailType)
    ? [...markedTrailTypes]
    : [...markedTrailTypes, markedTrailType];
}

function stripTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, "");
}
