import type { ResolutionPreset, UnitsMode } from "../api/types";

export function parseResolutionPreset(preset: ResolutionPreset): { width: number; height: number } {
  const [wStr, hStr] = preset.split("x");
  const width = Number.parseInt(wStr ?? "1920", 10);
  const height = Number.parseInt(hStr ?? "1080", 10);
  return {
    width: Number.isFinite(width) && width > 0 ? width : 1920,
    height: Number.isFinite(height) && height > 0 ? height : 1080,
  };
}

export function hexToRgbTuple(hex: string, fallback: [number, number, number] = [56, 189, 248]): [number, number, number] {
  const clean = hex.trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(clean)) {
    return fallback;
  }
  const r = Number.parseInt(clean.slice(0, 2), 16);
  const g = Number.parseInt(clean.slice(2, 4), 16);
  const b = Number.parseInt(clean.slice(4, 6), 16);
  return [r, g, b];
}

export function hexToRgbaCss(hex: string, opacity: number): string {
  const [r, g, b] = hexToRgbTuple(hex, [15, 23, 42]);
  const clamped = Math.min(1, Math.max(0, opacity));
  return `rgba(${r}, ${g}, ${b}, ${clamped})`;
}

export function formatDistance(meters: number, units: UnitsMode, decimals = 1): string {
  const safe = Number.isFinite(meters) ? Math.max(0, meters) : 0;
  if (units === "imperial") {
    const miles = safe / 1609.344;
    return `${miles.toFixed(decimals)} mi`;
  }
  const km = safe / 1000;
  return `${km.toFixed(decimals)} km`;
}

export function formatElevation(meters: number | null | undefined, units: UnitsMode): string {
  if (meters === null || meters === undefined || !Number.isFinite(meters)) {
    return units === "imperial" ? "-- ft" : "-- m";
  }
  if (units === "imperial") {
    return `${Math.round(meters * 3.28084)} ft`;
  }
  return `${Math.round(meters)} m`;
}

export function formatSpeed(mps: number | null | undefined, units: UnitsMode): string {
  if (mps === null || mps === undefined || !Number.isFinite(mps)) {
    return units === "imperial" ? "0.0 mph" : "0.0 km/h";
  }
  const safe = Math.max(0, mps);
  if (units === "imperial") {
    return `${(safe * 2.236936).toFixed(1)} mph`;
  }
  return `${(safe * 3.6).toFixed(1)} km/h`;
}

export function formatGrade(gradePct: number | null | undefined): string {
  if (gradePct === null || gradePct === undefined || !Number.isFinite(gradePct)) {
    return "--%";
  }
  const rounded = Math.round(gradePct * 10) / 10;
  const sign = rounded > 0 ? "+" : "";
  return `${sign}${rounded.toFixed(1)}%`;
}

export function formatHeartRate(hrBpm: number | null | undefined): string {
  if (hrBpm === null || hrBpm === undefined || !Number.isFinite(hrBpm) || hrBpm <= 0) {
    return "-- bpm";
  }
  return `${Math.round(hrBpm)} bpm`;
}

export function formatPower(watts: number | null | undefined): string {
  if (watts === null || watts === undefined || !Number.isFinite(watts) || watts < 0) {
    return "-- W";
  }
  return `${Math.round(watts)} W`;
}

export function formatVam(vamMPerH: number | null | undefined, units: UnitsMode = "metric"): string {
  if (vamMPerH === null || vamMPerH === undefined || !Number.isFinite(vamMPerH)) {
    return units === "imperial" ? "-- ft/h" : "-- m/h";
  }
  const safe = Math.max(0, vamMPerH);
  if (units === "imperial") {
    return `${Math.round(safe * 3.28084)} ft/h`;
  }
  return `${Math.round(safe)} m/h`;
}

export function formatElapsedTime(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function formatDateInTimeZone(isoOrDate: Date | string | null | undefined, timeZone: string): string {
  if (!isoOrDate) {
    return "--";
  }
  const date = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "short",
      day: "2-digit",
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: "UTC",
      year: "numeric",
      month: "short",
      day: "2-digit",
    }).format(date);
  }
}

export function formatTimeInTimeZone(isoOrDate: Date | string | null | undefined, timeZone: string): string {
  if (!isoOrDate) {
    return "--:--";
  }
  const date = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
  if (Number.isNaN(date.getTime())) {
    return "--:--";
  }
  try {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-GB", {
      timeZone: "UTC",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }
}
