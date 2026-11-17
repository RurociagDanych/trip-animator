import { useEffect, useRef } from "react";
import type { StatisticMetric, StatisticsPanelConfig, UnitsMode } from "../../lib/api/types";
import {
  formatDateInTimeZone,
  formatDistance,
  formatElapsedTime,
  formatElevation,
  formatGrade,
  formatHeartRate,
  formatPower,
  formatSpeed,
  formatTimeInTimeZone,
  formatVam,
  hexToRgbaCss,
} from "../../lib/geo/format";
import type { HudSlotId, MeasuredHudSize, Rect } from "../../lib/geo/layout";
import type { EvaluatedTimelineState } from "../../lib/geo/timeline";
import { ActivityIcon, getActivityIconMeta } from "../icons";

export interface StatisticsHudLayoutPlacement {
  rect: Rect;
  slot: HudSlotId;
  scale: number;
}

export interface StatisticsHudProps {
  config: StatisticsPanelConfig;
  units: UnitsMode;
  state: EvaluatedTimelineState;
  compact?: boolean;
  layoutPlacement?: StatisticsHudLayoutPlacement | null;
  onMeasuredUnscaledSize?: (size: MeasuredHudSize) => void;
}

const METRIC_LABELS: Record<StatisticMetric, string> = {
  date: "Date",
  time: "Local time",
  "trip-day": "Trip day",
  "elapsed-time": "Elapsed",
  speed: "Speed",
  elevation: "Elevation",
  "distance-travelled": "Distance",
  "distance-remaining": "Remaining",
  grade: "Grade",
  hr: "Heart rate",
  power: "Power",
  vam: "VAM",
};

function formatMetricValue(
  metric: StatisticMetric,
  state: EvaluatedTimelineState,
  units: UnitsMode,
  timeZone: string,
): string {
  switch (metric) {
    case "date":
      return formatDateInTimeZone(
        state.currentTsUtcMs !== null ? new Date(state.currentTsUtcMs) : null,
        timeZone,
      );
    case "time":
      return formatTimeInTimeZone(
        state.currentTsUtcMs !== null ? new Date(state.currentTsUtcMs) : null,
        timeZone,
      );
    case "trip-day":
      return `Day ${state.activeStage?.dayIndex ?? 1}`;
    case "elapsed-time":
      return formatElapsedTime(state.elapsedSeconds);
    case "speed":
      return formatSpeed(state.speedMps, units);
    case "elevation":
      return formatElevation(state.elevM, units);
    case "distance-travelled":
      return formatDistance(state.distanceTravelledM, units);
    case "distance-remaining":
      return formatDistance(state.distanceRemainingM, units);
    case "grade":
      return formatGrade(state.gradePct);
    case "hr":
      return formatHeartRate(state.hrBpm);
    case "power":
      return formatPower(state.watts);
    case "vam":
      return formatVam(state.vamMPerH, units);
  }
}

export function StatisticsHud({
  config,
  units,
  state,
  compact = false,
  layoutPlacement = null,
  onMeasuredUnscaledSize,
}: StatisticsHudProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);

  const effectivePosition = layoutPlacement
    ? layoutPlacement.slot.startsWith("left")
      ? "left"
      : layoutPlacement.slot.startsWith("right")
        ? "right"
        : layoutPlacement.slot.startsWith("bottom")
          ? "bottom"
          : "top"
    : config.position;
  const isHorizontal = effectivePosition === "top" || effectivePosition === "bottom";

  useEffect(() => {
    const el = hostRef.current;
    if (!el || !onMeasuredUnscaledSize || typeof ResizeObserver === "undefined") {
      return;
    }
    const orientation: "horizontal" | "vertical" = isHorizontal ? "horizontal" : "vertical";
    const observer = new ResizeObserver(() => {
      const w = Math.ceil(el.offsetWidth / 8) * 8;
      const h = Math.ceil(el.offsetHeight / 8) * 8;
      if (w > 0 && h > 0) {
        onMeasuredUnscaledSize({ width: w, height: h, orientation });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [onMeasuredUnscaledSize, config.enabled, isHorizontal, config.metrics.length, config.text_size, compact]);

  if (!config.enabled || config.metrics.length === 0) {
    return null;
  }

  const positionClasses: Record<StatisticsPanelConfig["position"], string> = {
    top: "top-3 left-1/2 -translate-x-1/2",
    bottom: "bottom-3 left-1/2 -translate-x-1/2",
    left: "left-3 top-1/2 -translate-y-1/2",
    right: "right-3 top-1/2 -translate-y-1/2",
  };

  const baseScale = layoutPlacement
    ? layoutPlacement.scale
    : compact
      ? Math.min(config.scale, 0.85)
      : config.scale;
  const valueFontSize = Math.round(config.text_size * (compact ? 0.62 : 0.78));
  const labelFontSize = Math.max(9, Math.round(valueFontSize * 0.56));
  const activeActivity = state.activeStage ? getActivityIconMeta(state.activeStage.activityType) : null;

  const placementStyle: React.CSSProperties = layoutPlacement
    ? {
        left: `${layoutPlacement.rect.x}px`,
        top: `${layoutPlacement.rect.y}px`,
        backgroundColor: hexToRgbaCss(config.background_color, config.background_opacity),
        color: config.text_color,
        transform: `scale(${baseScale})`,
        transformOrigin: "top left",
      }
    : {
        backgroundColor: hexToRgbaCss(config.background_color, config.background_opacity),
        color: config.text_color,
        transform:
          config.position === "top" || config.position === "bottom"
            ? `translateX(-50%) scale(${baseScale})`
            : `translateY(-50%) scale(${baseScale})`,
        transformOrigin:
          config.position === "top"
            ? "top center"
            : config.position === "bottom"
              ? "bottom center"
              : config.position === "left"
                ? "center left"
                : "center right",
      };

  return (
    <div
      ref={hostRef}
      data-testid="statistics-hud"
      data-hud-position={config.position}
      data-hud-slot={layoutPlacement?.slot ?? config.position}
      className={`pointer-events-none absolute z-20 rounded-lg border border-white/15 shadow-xl backdrop-blur-md ${
        layoutPlacement ? "" : positionClasses[config.position]
      }`}
      style={placementStyle}
    >
      <div
        className={
          isHorizontal
            ? "flex flex-wrap items-center gap-x-5 gap-y-1.5 px-4 py-2.5"
            : "flex flex-col gap-2.5 px-3.5 py-3 min-w-[132px]"
        }
      >
        {activeActivity && state.activeStage ? (
          <div
            data-testid="hud-activity-chip"
            className="inline-flex items-center gap-1.5 self-start rounded-full border border-white/20 bg-black/25 px-2.5 py-1 leading-none"
          >
            <ActivityIcon
              type={state.activeStage.activityType}
              size={Math.max(13, Math.round(labelFontSize * 1.25))}
              accentColor={state.activeStage.color || activeActivity.accentColor}
              duotoneOpacity={0.28}
            />
            <span
              className="font-semibold tracking-tight whitespace-nowrap"
              style={{ fontSize: `${Math.max(10, labelFontSize)}px` }}
            >
              {activeActivity.shortLabel}
            </span>
          </div>
        ) : null}

        {config.metrics.map((metric) => (
          <div key={metric} data-metric={metric} className="flex flex-col leading-tight">
            <span
              className="opacity-70"
              style={{
                fontSize: `${labelFontSize}px`,
              }}
            >
              {METRIC_LABELS[metric]}
            </span>
            <span
              className="tabular-telemetry font-semibold tracking-tight whitespace-nowrap"
              style={{
                fontSize: `${valueFontSize}px`,
              }}
            >
              {formatMetricValue(metric, state, units, config.time_zone)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
