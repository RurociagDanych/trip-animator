import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { RenderManifestMarkedTrails } from "../api/types";
import { MARKED_TRAILS_SOURCE_ID } from "./map-config";

export const MARKED_TRAILS_PMTILES_PROTOCOL = "pmtiles";
export const MARKED_TRAILS_POINTER_FILE = "current.json";
const MARKED_TRAILS_POINTER_TIMEOUT_MS = 10_000;
// One budget for the whole pinned-build check on the render route, which holds back the ready
// signal: the HEAD request and the current.json fallback share it.
const MARKED_TRAILS_RESOLVE_DEADLINE_MS = 5_000;
const MARKED_TRAILS_POINTER_RETRIES = 2;
// Matches the publisher's layout: build date, optional same-day suffix from 2, fixed file name.
const MARKED_TRAILS_PMTILES_KEY_PATTERN =
  /^builds\/\d{4}-\d{2}-\d{2}(-[2-9]|-[1-9]\d+)?\/marked-trails\.pmtiles$/;

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
 * Returns the published build, or null when the store has none: `current.json` missing (404),
 * not readable by the public (403) or invalid. Other failures throw, so React Query reports
 * them instead of hiding a broken store. The request is cut off after ten seconds or when
 * `signal` aborts, whichever comes first.
 */
export async function fetchMarkedTrailsBuild(
  baseUrl: string,
  signal?: AbortSignal,
): Promise<MarkedTrailsBuild | null> {
  const timeout = AbortSignal.timeout(MARKED_TRAILS_POINTER_TIMEOUT_MS);
  return fetchMarkedTrailsPointerBuild(
    baseUrl,
    signal ? AbortSignal.any([signal, timeout]) : timeout,
  );
}

async function fetchMarkedTrailsPointerBuild(
  baseUrl: string,
  signal: AbortSignal,
): Promise<MarkedTrailsBuild | null> {
  const root = stripTrailingSlashes(baseUrl);
  const response = await fetch(`${root}/${MARKED_TRAILS_POINTER_FILE}`, {
    cache: "no-cache",
    signal,
  });
  if (response.status === 404 || response.status === 403) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Marked Trails pointer request failed with status ${response.status}`);
  }
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    return null;
  }
  const pointer = parseMarkedTrailsPointer(raw);
  return pointer ? buildMarkedTrailsBuild(root, pointer) : null;
}

/**
 * Resolves a store base as configured: a root-relative path (local development) is read from
 * the origin of the page, an absolute URL is used as it is. Trailing slashes are dropped.
 */
export function resolveMarkedTrailsBase(base: string): string {
  const stripped = stripTrailingSlashes(base);
  return stripped.startsWith("/") && !stripped.startsWith("//")
    ? `${window.location.origin}${stripped}`
    : stripped;
}

/** True for a map error event raised by the Marked Trails source, e.g. a failed PMTiles read. */
export function isMarkedTrailsSourceError(event: object): boolean {
  return (event as { sourceId?: unknown }).sourceId === MARKED_TRAILS_SOURCE_ID;
}

/**
 * Resolves the build a render draws. The pinned build is kept unless its PMTiles file answers
 * 404 or 403 to a HEAD request, which means a later publication deleted it. Then the current
 * build of the same store is used, and when there is none either (or its pointer cannot be
 * read) the result is null and the render goes on without trails. Any other failure of the
 * availability check keeps the pinned build, because it does not prove the file is gone. The
 * check and the fallback share one five second deadline.
 */
export async function resolvePinnedMarkedTrailsBuild(
  pin: RenderManifestMarkedTrails,
): Promise<MarkedTrailsBuild | null> {
  const root = resolveMarkedTrailsBase(pin.base_url);
  const deadline = AbortSignal.timeout(MARKED_TRAILS_RESOLVE_DEADLINE_MS);
  if (MARKED_TRAILS_PMTILES_KEY_PATTERN.test(pin.pmtiles_key)) {
    const pinned: MarkedTrailsBuild = {
      buildId: pin.build_id,
      dataDate: pin.osm_data_date,
      pmtilesUrl: `${root}/${pin.pmtiles_key}`,
    };
    if (!(await markedTrailsFileIsGone(pinned.pmtilesUrl, deadline))) {
      return pinned;
    }
  }
  try {
    return await fetchMarkedTrailsPointerBuild(root, deadline);
  } catch {
    return null;
  }
}

async function markedTrailsFileIsGone(url: string, signal: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch(url, { method: "HEAD", signal });
    return response.status === 404 || response.status === 403;
  } catch {
    return false;
  }
}

export interface PinnedMarkedTrailsBuildState {
  settled: boolean;
  build: MarkedTrailsBuild | null;
}

/**
 * Resolves the pinned build of a render manifest once. `settled` stays false until the
 * answer is known, so the caller can hold back the map and avoid loading its style twice.
 * Without a pin or with `enabled` false nothing is requested and the state is settled.
 */
export function usePinnedMarkedTrailsBuild(
  pin: RenderManifestMarkedTrails | null | undefined,
  enabled: boolean,
): PinnedMarkedTrailsBuildState {
  const pinKey = pin ? JSON.stringify(pin) : "";
  const active = enabled && pin != null;
  const [resolved, setResolved] = useState<{ key: string; build: MarkedTrailsBuild | null } | null>(
    null,
  );

  useEffect(() => {
    if (!active || !pin) {
      return;
    }
    let cancelled = false;
    void resolvePinnedMarkedTrailsBuild(pin).then((build) => {
      if (!cancelled) {
        setResolved({ key: pinKey, build });
      }
    });
    return () => {
      cancelled = true;
    };
    // `pin` is covered by `pinKey`; a new object with the same content must not refetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, pinKey]);

  if (!active) {
    return { settled: true, build: null };
  }
  return resolved?.key === pinKey
    ? { settled: true, build: resolved.build }
    : { settled: false, build: null };
}

export type MarkedTrailsAvailability =
  "not-configured" | "loading" | "available" | "unavailable" | "error";

export interface MarkedTrailsBuildState {
  availability: MarkedTrailsAvailability;
  build: MarkedTrailsBuild | null;
}

/**
 * Loads the published build of the store at `baseUrl` and says why there is none: no store
 * configured (`baseUrl` empty), still loading, available, unavailable (no `current.json`,
 * access denied or invalid), or an error (the request kept failing after two retries).
 */
export function useMarkedTrailsBuildState(baseUrl: string | undefined): MarkedTrailsBuildState {
  const query = useQuery({
    queryKey: ["marked-trails", "build", baseUrl ?? ""],
    queryFn: ({ signal }) => fetchMarkedTrailsBuild(baseUrl ?? "", signal),
    enabled: Boolean(baseUrl),
    staleTime: 5 * 60_000,
    retry: MARKED_TRAILS_POINTER_RETRIES,
  });
  if (!baseUrl) {
    return { availability: "not-configured", build: null };
  }
  if (query.data) {
    return { availability: "available", build: query.data };
  }
  if (query.isError) {
    return { availability: "error", build: null };
  }
  if (query.isPending) {
    return { availability: "loading", build: null };
  }
  return { availability: "unavailable", build: null };
}

/** Returns the published build, or null while loading or when none is usable. */
export function useMarkedTrailsBuild(baseUrl: string | undefined): MarkedTrailsBuild | null {
  return useMarkedTrailsBuildState(baseUrl).build;
}

/**
 * Returns the date of the trail data as `YYYY-MM-DD`: the day of `osm_data_date`, else the
 * build date from the build id (a same-day suffix is dropped), else null.
 */
export function markedTrailsDataDate(build: MarkedTrailsBuild | null): string | null {
  if (!build) {
    return null;
  }
  return leadingIsoDate(build.dataDate) ?? leadingIsoDate(build.buildId);
}

function leadingIsoDate(value: string | null): string | null {
  const match = value ? /^\d{4}-\d{2}-\d{2}/.exec(value) : null;
  return match ? match[0] : null;
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
