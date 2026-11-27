import { useQuery } from "@tanstack/react-query";

export const MARKED_TRAILS_PMTILES_PROTOCOL = "pmtiles";
export const MARKED_TRAILS_POINTER_FILE = "current.json";

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
 * Validates `current.json`. The key must stay under `builds/` so a corrupted pointer cannot
 * point the editor at another object in the store.
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
  if (typeof key !== "string" || !key.startsWith("builds/") || key.split("/").includes("..")) {
    return null;
  }
  return {
    current_build_id: buildId,
    pmtiles_key: key,
    osm_data_date: typeof record.osm_data_date === "string" ? record.osm_data_date : null,
  };
}

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

function stripTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, "");
}
