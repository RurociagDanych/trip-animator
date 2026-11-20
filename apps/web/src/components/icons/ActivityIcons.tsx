/* eslint-disable react-refresh/only-export-components */
import type { FC, ReactNode, SVGProps } from "react";

export const ACTIVITY_TYPES = [
  "cycling",
  "gravel_mtb",
  "hiking",
  "trail_run",
  "running",
  "walking",
  "skiing",
  "kayak",
  "swimming",
  "climbing",
  "motorcycle",
  "car",
  "train",
  "camp",
] as const;

export type ActivityType = (typeof ACTIVITY_TYPES)[number];

export type ActivityCategory =
  | "cycling"
  | "foot"
  | "mountain"
  | "water"
  | "winter"
  | "transfer"
  | "camp";

export const DEFAULT_ACTIVITY_TYPE: ActivityType = "cycling";

export interface ActivityGlyphProps extends Omit<SVGProps<SVGSVGElement>, "type"> {
  /**
   * Pixel width/height shorthand (defaults to 24 when neither size nor className overrides).
   */
  size?: number | string;
  /**
   * Optional accent color for duotone fills (defaults to `currentColor`).
   */
  accentColor?: string;
  /**
   * Opacity for duotone fill layers (defaults to `0.2`).
   */
  duotoneOpacity?: number;
  /**
   * Accessible title rendered inside the SVG when provided.
   */
  title?: string;
}

export interface ActivityIconProps extends ActivityGlyphProps {
  /**
   * Activity type identifier (falls back to `cycling` when omitted or unrecognized).
   */
  type?: ActivityType | string | null;
  /**
   * Alias for `type` so callers can pass either `type` or `activity`.
   */
  activity?: ActivityType | string | null;
}

export interface ActivityIconMeta {
  readonly id: ActivityType;
  readonly type: ActivityType;
  readonly label: string;
  readonly shortLabel: string;
  readonly description: string;
  readonly category: ActivityCategory;
  readonly accentColor: string;
  readonly svgPath: string;
  readonly stravaTypes: readonly string[];
  readonly Icon: FC<ActivityGlyphProps>;
  readonly Component: FC<ActivityGlyphProps>;
}

export type ActivityIconEntry = FC<ActivityGlyphProps> & ActivityIconMeta;

interface GeometryRenderOptions {
  accentColor: string;
  duotoneOpacity: number;
}

function renderActivityGeometry(
  type: ActivityType,
  { accentColor, duotoneOpacity }: GeometryRenderOptions,
): ReactNode {
  switch (type) {
    case "cycling":
      return (
        <>
          <circle
            cx="5.5"
            cy="16.5"
            r="3.5"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle
            cx="18.5"
            cy="16.5"
            r="3.5"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <polygon
            points="9.5,9.5 15.5,9.5 11.5,16.5"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 0.85}
            stroke="none"
          />
          <circle cx="5.5" cy="16.5" r="3.5" />
          <circle cx="18.5" cy="16.5" r="3.5" />
          <path d="M5.5 16.5L9.5 9.5H15.5L11.5 16.5H5.5Z" />
          <path d="M11.5 16.5L9 8M7.5 8H10.8" />
          <path d="M18.5 16.5L15 8H17.8C19 8 19.5 8.8 19.5 9.6C19.5 10.4 18.8 11 17.8 11" />
        </>
      );

    case "gravel_mtb":
      return (
        <>
          <circle
            cx="5.5"
            cy="15.2"
            r="3.5"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle
            cx="18.5"
            cy="15.2"
            r="3.5"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <path
            d="M2 21L6.5 19.2L11.5 21L17 18.8L22 21Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle cx="5.5" cy="15.2" r="3.5" />
          <circle cx="18.5" cy="15.2" r="3.5" />
          <path d="M5.5 15.2L9.2 9L15.8 8.4L11.2 15.2H5.5Z" />
          <path d="M11.2 15.2L8.6 7.5M7.2 7.5H10.2" />
          <path d="M18.5 15.2L15.2 7L17.8 6.5" />
          <path d="M2 21L6.5 19.4L11.5 21L17 19.2L22 21" />
        </>
      );

    case "hiking":
      return (
        <>
          <circle
            cx="13.5"
            cy="4.5"
            r="2"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.15}
            stroke="none"
          />
          <path
            d="M9.5 8.2L6.8 8.8C5.9 9 5.4 9.9 5.6 10.8L6.4 13.8C6.6 14.7 7.5 15.2 8.4 15L10.3 14.5Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.25}
            stroke="none"
          />
          <circle cx="13.5" cy="4.5" r="2" />
          <path d="M9.5 8.2L6.8 8.8C5.9 9 5.4 9.9 5.6 10.8L6.4 13.8C6.6 14.7 7.5 15.2 8.4 15L10.3 14.5" />
          <path d="M12.2 8L10.5 14.2L7.5 20.5M10.5 14.2L14.5 16.5L15.8 20.5" />
          <path d="M12 9.2L15.2 11.5L18.5 10.2" />
          <path d="M18.8 7.5L17.2 21" />
        </>
      );

    case "trail_run":
      return (
        <>
          <path
            d="M2.5 20.8L8.5 16.2L13.5 18.8L18.5 14.2L21.5 20.8Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle
            cx="15.5"
            cy="4.5"
            r="1.9"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.2}
            stroke="none"
          />
          <circle cx="15.5" cy="4.5" r="1.9" />
          <path d="M9.5 9.5L12.5 7.5L16.2 9.2L18.8 7.8" />
          <path d="M13.5 8.2L11.2 13.2L15.5 15.2L17.2 19.2M11.2 13.2L7.8 14.8L5.2 18" />
          <path d="M2.5 20.8L8.5 18.8L14.5 20.4L21.5 17.4" />
        </>
      );

    case "running":
      return (
        <>
          <circle
            cx="15.5"
            cy="4.5"
            r="2"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.2}
            stroke="none"
          />
          <path
            d="M2.5 10.5H8L6.5 14.5H2Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 0.85}
            stroke="none"
          />
          <circle cx="15.5" cy="4.5" r="2" />
          <path d="M9 9.5L12.2 7.5L16.5 9.5L19.5 7.5" />
          <path d="M13.8 8.3L11.5 13.8L16 15.8L17.8 20.5M11.5 13.8L7.5 15.2L5 19.5" />
          <path d="M2.5 10.5H6.5M2 13.8H5" />
        </>
      );

    case "walking":
      return (
        <>
          <circle
            cx="12.5"
            cy="4.5"
            r="2"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.2}
            stroke="none"
          />
          <ellipse
            cx="12"
            cy="20.8"
            rx="5.5"
            ry="1.2"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle cx="12.5" cy="4.5" r="2" />
          <path d="M12 8.2L11.2 14L8.5 20.2M11.2 14L14.5 17L15.8 20.2" />
          <path d="M8.8 12.2L11.8 8.5L15.2 11.8" />
        </>
      );

    case "skiing":
      return (
        <>
          <circle
            cx="15.5"
            cy="4.5"
            r="1.9"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.2}
            stroke="none"
          />
          <path
            d="M3.5 16L19.5 21H3.5Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle cx="15.5" cy="4.5" r="1.9" />
          <path d="M10.2 8.5L13.5 7.5L16.5 10.5L19.2 9.5" />
          <path d="M13.5 7.8L11 12.5L13.5 15.5L11.5 18.2" />
          <path d="M10.2 8.5L6.5 15.8" />
          <path d="M3.5 16L18.5 21C19.8 21.4 21.2 20.8 21.8 19.6" />
        </>
      );

    case "kayak":
      return (
        <>
          <path
            d="M2.5 14.5C6.5 12.2 17.5 12.2 21.5 14.5C17.5 16.8 6.5 16.8 2.5 14.5Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.15}
            stroke="none"
          />
          <circle
            cx="12"
            cy="7.5"
            r="1.8"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle cx="12" cy="7.5" r="1.8" />
          <path d="M10.2 13.2L11.2 10.2H12.8L13.8 13.2" />
          <path d="M2.5 14.5C6.5 12.2 17.5 12.2 21.5 14.5C17.5 16.8 6.5 16.8 2.5 14.5Z" />
          <path d="M5.5 6.5L18.5 19.5M4.5 8.2L7.2 5.5M16.8 20.5L19.5 17.8" />
          <path d="M4 19.8C6 18.8 8 20.8 10 19.8C12 18.8 14 20.8 16 19.8" />
        </>
      );

    case "swimming":
      return (
        <>
          <path
            d="M2 16.2C4.2 14.8 6.2 17.6 8.5 16.2C10.8 14.8 12.8 17.6 15.2 16.2C17.5 14.8 19.5 17.6 22 16.2V20.5H2Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle
            cx="16.5"
            cy="8.5"
            r="2"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.2}
            stroke="none"
          />
          <circle cx="16.5" cy="8.5" r="2" />
          <path d="M5.5 13.5L11.2 10.5L14.2 12.5M11.2 10.5L9.2 6.8L13.2 5.5" />
          <path d="M2 16.2C4.2 14.8 6.2 17.6 8.5 16.2C10.8 14.8 12.8 17.6 15.2 16.2C17.5 14.8 19.5 17.6 22 16.2" />
          <path d="M3.5 19.8C5.8 18.5 7.8 21 10.2 19.8C12.5 18.5 14.5 21 16.8 19.8C19 18.5 20.5 20.5 21.5 19.8" />
        </>
      );

    case "climbing":
      return (
        <>
          <path
            d="M2.5 20L10.5 6.5L18.5 20Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <path
            d="M10.5 2.8L14.8 4.2L10.5 5.6Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.35}
            stroke="none"
          />
          <path d="M2.5 20L10.5 6.5L16.5 17.2M13.8 12.4L16.8 8.5L21.5 20" />
          <path d="M2 20H22" />
          <path d="M7.6 11.8L10.5 13.5L13.4 11.8" />
          <path d="M10.5 6.5V2.8L14.8 4.2L10.5 5.6" />
        </>
      );

    case "motorcycle":
      return (
        <>
          <circle
            cx="5.5"
            cy="16.5"
            r="3.2"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <circle
            cx="18.5"
            cy="16.5"
            r="3.2"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <path
            d="M8.2 12.2H14.5L16.2 15.2H9.8Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.25}
            stroke="none"
          />
          <circle cx="5.5" cy="16.5" r="3.2" />
          <circle cx="18.5" cy="16.5" r="3.2" />
          <path d="M5.5 16.5L8.2 12.2H11.5L13.5 10.5H16.2L18.5 16.5" />
          <path d="M8.2 12.2L10.5 16.5H15.2L13.5 10.5" />
          <path d="M14.5 7.8H16.8L17.8 9.8" />
          <path d="M5.2 12.2H8.2" />
        </>
      );

    case "car":
      return (
        <>
          <path
            d="M5 11L7.2 6.8H16.8L19 11H20.5C21.1 11 21.5 11.4 21.5 12V16.5H2.5V12C2.5 11.4 2.9 11 3.5 11H5Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <path d="M5 11L7.2 6.8C7.5 6.3 8 6 8.6 6H15.4C16 6 16.5 6.3 16.8 6.8L19 11" />
          <path d="M3.8 11H20.2C21 11 21.5 11.6 21.5 12.4V16C21.5 16.6 21 17 20.5 17H19.5M4.5 17H3.5C3 17 2.5 16.6 2.5 16V12.4C2.5 11.6 3 11 3.8 11Z" />
          <path d="M9.5 17H14.5" />
          <circle cx="7" cy="17" r="2.5" />
          <circle cx="17" cy="17" r="2.5" />
          <path d="M5 13.8H6.5M17.5 13.8H19" />
        </>
      );

    case "train":
      return (
        <>
          <rect
            x="5"
            y="3.5"
            width="14"
            height="13.5"
            rx="3"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <rect x="5" y="3.5" width="14" height="13.5" rx="3" />
          <path d="M5 10.5H19" />
          <path d="M9.5 3.5L12 1.8L14.5 3.5" />
          <circle cx="8.5" cy="14" r="1" fill="currentColor" stroke="none" />
          <circle cx="15.5" cy="14" r="1" fill="currentColor" stroke="none" />
          <path d="M8 17L5.5 21M16 17L18.5 21M7 19.2H17" />
        </>
      );

    case "camp":
      return (
        <>
          <path
            d="M3 19.5L12 5.5L21 19.5Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity}
            stroke="none"
          />
          <path
            d="M9.2 19.5L12 13L14.8 19.5Z"
            fill={accentColor}
            fillOpacity={duotoneOpacity * 1.25}
            stroke="none"
          />
          <path d="M10.2 3.5L21 19.5M13.8 3.5L3 19.5" />
          <path d="M9.2 19.5L12 13L14.8 19.5" />
          <path d="M2 19.5H22" />
          <path d="M19 5.5V7.5M18 6.5H20" />
        </>
      );
  }
}

export function isActivityType(value: unknown): value is ActivityType {
  return typeof value === "string" && (ACTIVITY_TYPES as readonly string[]).includes(value);
}

export function normalizeActivityType(
  value: unknown,
  fallback: ActivityType = DEFAULT_ACTIVITY_TYPE,
): ActivityType {
  if (isActivityType(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim().length > 0) {
    return inferActivityTypeFromStrava(value, null, fallback);
  }
  return fallback;
}

/**
 * Infers our canonical `ActivityType` from a Strava `sport_type` / `type` string
 * (and optional activity/stage name for finer disambiguation such as gravel rides).
 */
export function inferActivityTypeFromStrava(
  sportType: string | null | undefined,
  activityName?: string | null,
  fallback: ActivityType = DEFAULT_ACTIVITY_TYPE,
): ActivityType {
  const raw = (sportType ?? "").trim();
  const normalized = raw.toLowerCase().replace(/[\s-]+/g, "_");
  const compact = normalized.replace(/_/g, "");
  const nameLower = (activityName ?? "").toLowerCase();

  if (isActivityType(normalized)) {
    return normalized;
  }

  if (
    compact === "gravelride" ||
    compact === "mountainbikeride" ||
    compact === "emountainbikeride" ||
    compact === "cyclocross" ||
    compact === "velomobile" ||
    compact === "handcycle" ||
    normalized.includes("gravel") ||
    normalized.includes("mtb") ||
    normalized.includes("mountain_bike") ||
    normalized.includes("cyclocross") ||
    normalized.includes("singletrack") ||
    normalized.includes("bikepack")
  ) {
    return "gravel_mtb";
  }

  if (
    compact === "ride" ||
    compact === "virtualride" ||
    compact === "ebikeride" ||
    normalized.includes("cycle") ||
    normalized.includes("cycling") ||
    normalized.includes("bike") ||
    normalized.includes("biking")
  ) {
    if (
      nameLower.includes("gravel") ||
      nameLower.includes("mtb") ||
      nameLower.includes("singletrack") ||
      nameLower.includes("enduro")
    ) {
      return "gravel_mtb";
    }
    return "cycling";
  }

  if (
    compact === "trailrun" ||
    normalized.includes("trail_run") ||
    normalized.includes("skyrun") ||
    normalized.includes("fell_run") ||
    normalized.includes("ultra_run")
  ) {
    return "trail_run";
  }

  if (
    compact === "run" ||
    compact === "virtualrun" ||
    normalized.includes("run") ||
    normalized.includes("jog") ||
    normalized.includes("marathon")
  ) {
    if (nameLower.includes("trail")) {
      return "trail_run";
    }
    return "running";
  }

  if (
    compact === "hike" ||
    compact === "snowshoe" ||
    normalized.includes("hike") ||
    normalized.includes("hiking") ||
    normalized.includes("trek") ||
    normalized.includes("backpack") ||
    normalized.includes("snowshoe") ||
    normalized.includes("ruck")
  ) {
    return "hiking";
  }

  if (
    compact === "walk" ||
    compact === "wheelchair" ||
    compact === "iceskate" ||
    compact === "inlineskate" ||
    compact === "skateboard" ||
    normalized.includes("walk") ||
    normalized.includes("stroll")
  ) {
    return "walking";
  }

  if (
    compact === "alpineski" ||
    compact === "backcountryski" ||
    compact === "nordicski" ||
    compact === "snowboard" ||
    compact === "rollerski" ||
    normalized.includes("ski") ||
    normalized.includes("snowboard") ||
    normalized.includes("skimo") ||
    normalized.includes("telemark")
  ) {
    return "skiing";
  }

  if (
    compact === "kayaking" ||
    compact === "canoeing" ||
    compact === "standuppaddling" ||
    compact === "rowing" ||
    compact === "virtualrow" ||
    compact === "sail" ||
    compact === "surfing" ||
    compact === "windsurf" ||
    compact === "kitesurf" ||
    normalized.includes("kayak") ||
    normalized.includes("canoe") ||
    normalized.includes("paddle") ||
    normalized.includes("packraft") ||
    normalized.includes("raft") ||
    normalized.includes("row") ||
    normalized.includes("sup") ||
    normalized.includes("sail") ||
    normalized.includes("surf") ||
    normalized.includes("ferry") ||
    normalized.includes("boat")
  ) {
    return "kayak";
  }

  if (
    compact === "swim" ||
    compact === "openwaterswim" ||
    normalized.includes("swim")
  ) {
    return "swimming";
  }

  if (
    compact === "rockclimbing" ||
    compact === "mountaineering" ||
    normalized.includes("climb") ||
    normalized.includes("boulder") ||
    normalized.includes("mountaineer") ||
    normalized.includes("alpin") ||
    normalized.includes("summit") ||
    normalized.includes("ferrata")
  ) {
    return "climbing";
  }

  if (
    compact === "motorcycle" ||
    compact === "moto" ||
    normalized.includes("motorcycle") ||
    normalized.includes("motorbike") ||
    normalized.includes("moto") ||
    normalized.includes("scooter") ||
    normalized.includes("dirtbike")
  ) {
    return "motorcycle";
  }

  if (
    compact === "car" ||
    compact === "drive" ||
    compact === "driving" ||
    compact === "auto" ||
    compact === "overland" ||
    normalized.includes("drive") ||
    normalized.includes("car") ||
    normalized.includes("overland") ||
    normalized.includes("4x4") ||
    normalized.includes("bus") ||
    normalized.includes("shuttle") ||
    normalized.includes("van") ||
    normalized.includes("roadtrip") ||
    normalized.includes("road_trip")
  ) {
    return "car";
  }

  if (
    compact === "train" ||
    compact === "rail" ||
    compact === "transit" ||
    compact === "transport" ||
    normalized.includes("train") ||
    normalized.includes("rail") ||
    normalized.includes("metro") ||
    normalized.includes("subway") ||
    normalized.includes("tram") ||
    normalized.includes("tgv") ||
    normalized.includes("eurostar")
  ) {
    return "train";
  }

  if (
    compact === "camp" ||
    compact === "camping" ||
    compact === "tent" ||
    compact === "bivouac" ||
    normalized.includes("camp") ||
    normalized.includes("bivouac") ||
    normalized.includes("bivy") ||
    normalized.includes("tent") ||
    normalized.includes("hut") ||
    normalized.includes("refuge") ||
    normalized.includes("rifugio")
  ) {
    return "camp";
  }

  // Fallback inspection of activityName if sportType was empty or generic (e.g. "Workout" or GPX filename)
  if (nameLower.length > 0 && nameLower !== normalized) {
    return inferActivityTypeFromStrava(nameLower, null, fallback);
  }

  return fallback;
}

/**
 * Renders the inner `<g>` SVG vector nodes for an activity icon.
 * Useful when embedding directly inside an existing `<svg>` canvas (such as `SvgFallbackLayer`).
 */
export function ActivitySvgGroup({
  type,
  accentColor = "currentColor",
  duotoneOpacity = 0.2,
}: {
  type?: ActivityType | string | null;
  accentColor?: string;
  duotoneOpacity?: number;
}) {
  const resolved = normalizeActivityType(type);
  return <>{renderActivityGeometry(resolved, { accentColor, duotoneOpacity })}</>;
}

/**
 * Primary custom SVG Activity Icon component.
 * Uses a 24x24 viewBox, crisp 1.75px rounded strokes, and subtle duotone fills.
 */
export function ActivityIcon({
  type,
  activity,
  size = 24,
  accentColor = "currentColor",
  duotoneOpacity = 0.2,
  strokeWidth = 1.75,
  title,
  className,
  ...rest
}: ActivityIconProps) {
  const resolved = normalizeActivityType(type ?? activity);
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      data-activity-icon={resolved}
      className={className}
      aria-hidden={title ? undefined : rest["aria-label"] ? undefined : true}
      role={title || rest["aria-label"] ? "img" : undefined}
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      {renderActivityGeometry(resolved, { accentColor, duotoneOpacity })}
    </svg>
  );
}

/**
 * High-contrast telemetry map marker badge for rendering active athlete position
 * or stage activity badges on top of MapLibre / HUD / deterministic MP4 frames.
 */
export function ActivityMarkerBadge({
  type,
  colorHex,
  sizePx = 28,
  pulse = false,
  className = "",
  title,
}: {
  type?: ActivityType | string | null;
  colorHex?: string;
  sizePx?: number;
  pulse?: boolean;
  className?: string;
  title?: string;
}) {
  const resolved = normalizeActivityType(type);
  const meta = ACTIVITY_ICONS[resolved];
  const accent = colorHex ?? meta.accentColor;
  const iconSize = Math.max(12, Math.round(sizePx * 0.58));

  return (
    <div
      data-testid={`activity-marker-${resolved}`}
      data-activity-type={resolved}
      title={title ?? meta.label}
      className={`relative inline-flex select-none items-center justify-center rounded-full border-2 bg-[#0d1319]/95 text-[#eef2f6] shadow-lg ${className}`}
      style={{
        width: `${sizePx}px`,
        height: `${sizePx}px`,
        borderColor: accent,
        boxShadow: `0 0 0 3px rgba(13, 19, 25, 0.75), 0 4px 12px rgba(0, 0, 0, 0.55)`,
      }}
    >
      {pulse ? (
        <span
          className="pointer-events-none absolute inset-0 rounded-full opacity-35"
          style={{ backgroundColor: accent }}
          aria-hidden="true"
        />
      ) : null}
      <ActivityIcon
        type={resolved}
        size={iconSize}
        accentColor={accent}
        duotoneOpacity={0.28}
        className="relative z-10"
      />
    </div>
  );
}

interface RawActivityDef {
  readonly type: ActivityType;
  readonly label: string;
  readonly shortLabel: string;
  readonly description: string;
  readonly category: ActivityCategory;
  readonly accentColor: string;
  readonly stravaTypes: readonly string[];
}

const RAW_ACTIVITY_DEFS: readonly RawActivityDef[] = [
  {
    type: "cycling",
    label: "Road Cycling",
    shortLabel: "Cycling",
    description: "Road bike, peloton, or tarmac endurance stage",
    category: "cycling",
    accentColor: "#38bdf8",
    stravaTypes: ["Ride", "VirtualRide", "EBikeRide"],
  },
  {
    type: "gravel_mtb",
    label: "Gravel / MTB",
    shortLabel: "Gravel/MTB",
    description: "Gravel grinding, mountain biking, bikepacking, or cyclocross",
    category: "cycling",
    accentColor: "#f59e0b",
    stravaTypes: [
      "GravelRide",
      "MountainBikeRide",
      "EMountainBikeRide",
      "Cyclocross",
      "Handcycle",
      "Velomobile",
    ],
  },
  {
    type: "hiking",
    label: "Hiking / Trekking",
    shortLabel: "Hiking",
    description: "Mountain hike, multi-day trek, backpacking, or snowshoeing",
    category: "mountain",
    accentColor: "#14b8a6",
    stravaTypes: ["Hike", "Snowshoe"],
  },
  {
    type: "trail_run",
    label: "Trail Running",
    shortLabel: "Trail Run",
    description: "Off-road trail run, ultra marathon, or skyrunning",
    category: "mountain",
    accentColor: "#f97316",
    stravaTypes: ["TrailRun"],
  },
  {
    type: "running",
    label: "Running",
    shortLabel: "Running",
    description: "Road run, track session, marathon, or tempo run",
    category: "foot",
    accentColor: "#fb7185",
    stravaTypes: ["Run", "VirtualRun"],
  },
  {
    type: "walking",
    label: "Walking",
    shortLabel: "Walking",
    description: "Urban walk, recovery stroll, or sightseeing stage",
    category: "foot",
    accentColor: "#38bdf8",
    stravaTypes: ["Walk", "Wheelchair", "InlineSkate", "IceSkate", "Skateboard"],
  },
  {
    type: "skiing",
    label: "Skiing / Touring",
    shortLabel: "Skiing",
    description: "Alpine skiing, backcountry ski touring, nordic ski, or snowboard",
    category: "winter",
    accentColor: "#38bdf8",
    stravaTypes: ["AlpineSki", "BackcountrySki", "NordicSki", "Snowboard", "RollerSki"],
  },
  {
    type: "kayak",
    label: "Kayaking / Paddling",
    shortLabel: "Kayak",
    description: "Sea kayak, whitewater packraft, canoe, SUP, or rowing",
    category: "water",
    accentColor: "#14b8a6",
    stravaTypes: [
      "Kayaking",
      "Canoeing",
      "StandUpPaddling",
      "Rowing",
      "VirtualRow",
      "Sail",
      "Surfing",
      "Windsurf",
      "Kitesurf",
    ],
  },
  {
    type: "swimming",
    label: "Open Water Swim",
    shortLabel: "Swimming",
    description: "Open water crossing, wild swim, or triathlon swim leg",
    category: "water",
    accentColor: "#38bdf8",
    stravaTypes: ["Swim"],
  },
  {
    type: "climbing",
    label: "Climbing / Summit",
    shortLabel: "Climbing",
    description: "Mountaineering, alpine summit push, rock climbing, or via ferrata",
    category: "mountain",
    accentColor: "#a855f7",
    stravaTypes: ["RockClimbing"],
  },
  {
    type: "motorcycle",
    label: "Motorcycle",
    shortLabel: "Moto",
    description: "Adventure motorcycle, dual-sport, or moto touring stage",
    category: "transfer",
    accentColor: "#f97316",
    stravaTypes: ["Motorcycle"],
  },
  {
    type: "car",
    label: "Car / Overland",
    shortLabel: "Car",
    description: "4x4 overland drive, campervan, shuttle, or road-trip transfer",
    category: "transfer",
    accentColor: "#8fa0b2",
    stravaTypes: ["Drive", "Car", "Overland"],
  },
  {
    type: "train",
    label: "Train / Rail",
    shortLabel: "Train",
    description: "High-speed rail, scenic mountain train, or transit connector",
    category: "transfer",
    accentColor: "#14b8a6",
    stravaTypes: ["Train", "Rail", "Transit"],
  },
  {
    type: "camp",
    label: "Camp / Overnight",
    shortLabel: "Camp",
    description: "Alpine bivouac, tent camp, mountain hut, or basecamp stop",
    category: "camp",
    accentColor: "#f59e0b",
    stravaTypes: ["Camp"],
  },
];

function createActivityIconEntry(def: RawActivityDef): ActivityIconEntry {
  const SpecificIcon: FC<ActivityGlyphProps> = (props) => (
    <ActivityIcon type={def.type} {...props} />
  );
  SpecificIcon.displayName = `ActivityIcon(${def.type})`;

  const meta: ActivityIconMeta = {
    id: def.type,
    type: def.type,
    label: def.label,
    shortLabel: def.shortLabel,
    description: def.description,
    category: def.category,
    accentColor: def.accentColor,
    svgPath: `/icons/${def.type}.svg`,
    stravaTypes: def.stravaTypes,
    Icon: SpecificIcon,
    Component: SpecificIcon,
  };

  return Object.assign(SpecificIcon, meta);
}

export const ACTIVITY_ICONS: Readonly<Record<ActivityType, ActivityIconEntry>> = {
  cycling: createActivityIconEntry(RAW_ACTIVITY_DEFS[0]!),
  gravel_mtb: createActivityIconEntry(RAW_ACTIVITY_DEFS[1]!),
  hiking: createActivityIconEntry(RAW_ACTIVITY_DEFS[2]!),
  trail_run: createActivityIconEntry(RAW_ACTIVITY_DEFS[3]!),
  running: createActivityIconEntry(RAW_ACTIVITY_DEFS[4]!),
  walking: createActivityIconEntry(RAW_ACTIVITY_DEFS[5]!),
  skiing: createActivityIconEntry(RAW_ACTIVITY_DEFS[6]!),
  kayak: createActivityIconEntry(RAW_ACTIVITY_DEFS[7]!),
  swimming: createActivityIconEntry(RAW_ACTIVITY_DEFS[8]!),
  climbing: createActivityIconEntry(RAW_ACTIVITY_DEFS[9]!),
  motorcycle: createActivityIconEntry(RAW_ACTIVITY_DEFS[10]!),
  car: createActivityIconEntry(RAW_ACTIVITY_DEFS[11]!),
  train: createActivityIconEntry(RAW_ACTIVITY_DEFS[12]!),
  camp: createActivityIconEntry(RAW_ACTIVITY_DEFS[13]!),
};

export const ACTIVITY_ICON_LIST: readonly ActivityIconEntry[] = ACTIVITY_TYPES.map(
  (t) => ACTIVITY_ICONS[t],
);

export function getActivityIconMeta(
  type: ActivityType | string | null | undefined,
): ActivityIconEntry {
  return ACTIVITY_ICONS[normalizeActivityType(type)];
}
