import type { VaultActivityFilters } from "../../lib/api/endpoints";

export type ActivityLibrarySelection = Omit<VaultActivityFilters, "offset" | "limit">;

const sorts = ["started_at", "local_date", "name", "distance_m", "elevation_gain_m", "moving_seconds"];
const selectionKeys: (keyof ActivityLibrarySelection)[] = [
  "name",
  "start_date",
  "end_date",
  "sport",
  "min_distance_m",
  "max_distance_m",
  "min_elevation_gain_m",
  "max_elevation_gain_m",
  "min_moving_seconds",
  "max_moving_seconds",
  "has_gps",
  "include_hidden",
  "sort",
  "order",
  "area",
];

function isDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
}

function isArea(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const coordinates = value.split(",").map(Number);
  return coordinates.length === 4
    && coordinates.every(Number.isFinite)
    && coordinates[0]! >= -180 && coordinates[0]! <= 180
    && coordinates[1]! >= -90 && coordinates[1]! <= 90
    && coordinates[2]! >= -180 && coordinates[2]! <= 180
    && coordinates[3]! >= -90 && coordinates[3]! <= 90
    && coordinates[1]! <= coordinates[3]!;
}

function sanitize(value: unknown): ActivityLibrarySelection {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  const source = value as Record<string, unknown>;
  const selection: Record<string, unknown> = {};
  for (const key of selectionKeys) {
    const candidate = source[key];
    if (candidate === undefined) continue;
    if ((key === "name" || key === "sport") && typeof candidate === "string" && candidate.length <= 200) {
      if (candidate) selection[key] = candidate;
    } else if ((key === "start_date" || key === "end_date") && isDate(candidate)) {
      selection[key] = candidate;
    } else if (key.endsWith("_m") || key.endsWith("_seconds")) {
      if (typeof candidate === "number" && Number.isFinite(candidate) && candidate >= 0) selection[key] = candidate;
    } else if ((key === "has_gps" || key === "include_hidden") && typeof candidate === "boolean") {
      selection[key] = candidate;
    } else if (key === "sort" && typeof candidate === "string" && sorts.includes(candidate)) {
      selection.sort = candidate;
    } else if (key === "order" && (candidate === "asc" || candidate === "desc")) {
      selection.order = candidate;
    } else if (key === "area" && isArea(candidate)) {
      selection.area = candidate;
    }
  }
  return selection;
}

type SelectionStorage = Pick<Storage, "getItem" | "setItem">;

function storageOrNull(): SelectionStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function loadActivityLibrarySelection(ownerId: string | null, storage: SelectionStorage | null = storageOrNull()): ActivityLibrarySelection {
  if (!ownerId || !storage) return {};
  try {
    const value = storage.getItem(`activity-library-selection:${ownerId}`);
    return value === null ? {} : sanitize(JSON.parse(value));
  } catch {
    return {};
  }
}

export function saveActivityLibrarySelection(ownerId: string | null, filters: VaultActivityFilters, storage: SelectionStorage | null = storageOrNull()): void {
  if (!ownerId || !storage) return;
  try {
    const selection = sanitize(filters);
    storage.setItem(`activity-library-selection:${ownerId}`, JSON.stringify(selection));
  } catch {
    return;
  }
}
