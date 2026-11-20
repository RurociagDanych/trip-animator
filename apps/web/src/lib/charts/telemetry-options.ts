import type uPlot from "uplot";
import type { TelemetryChartPoint, UnitsMode } from "../api/types";
import { smoothTelemetryChartPoints } from "../geo/timeline";

export const TELEMETRY_LEFT_AXIS_SIZE = 56;
export const TELEMETRY_RIGHT_AXIS_SIZE = 60;
export const TELEMETRY_PADDING_TOP = 8;
export const TELEMETRY_PADDING_RIGHT = 10;
export const TELEMETRY_PADDING_BOTTOM = 0;
export const TELEMETRY_PADDING_LEFT = 10;

export interface ChartSeriesVisibility {
  elevation: boolean;
  speed: boolean;
  grade: boolean;
  hr?: boolean;
  watts?: boolean;
}

export interface ChartDayDivider {
  distM: number;
  dayIndex: number;
  label?: string;
}

export interface ChartClimbBand {
  id: string;
  name: string;
  category?: string;
  startDistM: number;
  endDistM: number;
}

export function buildTelemetryPlotData(
  points: readonly TelemetryChartPoint[],
  units: UnitsMode,
  options?: { smoothingWindow?: number },
): uPlot.AlignedData {
  if (points.length === 0) {
    return [[0], [0], [0], [0], [null], [null]];
  }
  const sourcePoints =
    options?.smoothingWindow && options.smoothingWindow > 1
      ? smoothTelemetryChartPoints(points, options.smoothingWindow)
      : points;
  const distFactor = units === "imperial" ? 1 / 1609.344 : 1 / 1000;
  const elevFactor = units === "imperial" ? 3.28084 : 1;
  const speedFactor = units === "imperial" ? 2.236936 : 3.6;

  const xs = new Float64Array(sourcePoints.length);
  const elevs: (number | null)[] = new Array(sourcePoints.length);
  const speeds = new Float64Array(sourcePoints.length);
  const grades: (number | null)[] = new Array(sourcePoints.length);
  const hrs: (number | null)[] = new Array(sourcePoints.length);
  const watts: (number | null)[] = new Array(sourcePoints.length);

  for (let i = 0; i < sourcePoints.length; i++) {
    const pt = sourcePoints[i]!;
    xs[i] = pt.cum_dist_m * distFactor;
    elevs[i] = pt.elev_m !== null ? pt.elev_m * elevFactor : null;
    speeds[i] = pt.speed_mps * speedFactor;
    grades[i] = pt.grade_pct !== null ? pt.grade_pct : null;
    hrs[i] = pt.hr_bpm !== null && pt.hr_bpm !== undefined ? pt.hr_bpm : null;
    watts[i] = pt.watts !== null && pt.watts !== undefined ? pt.watts : null;
  }

  return [xs, elevs, speeds, grades, hrs, watts];
}

export interface TelemetryPlotOptionsConfig {
  units?: UnitsMode;
  visibility?: ChartSeriesVisibility;
  onCursorIndex?: (index: number | null) => void;
  enableDragBrush?: boolean;
  onBrushRange?: (startDistM: number, endDistM: number) => void;
  dayDividers?: readonly ChartDayDivider[];
  climbBands?: readonly ChartClimbBand[];
}

export function buildTelemetryPlotOptions(
  width: number,
  height: number,
  options?: TelemetryPlotOptionsConfig,
): uPlot.Options {
  const units = options?.units ?? "metric";
  const visibility: Required<ChartSeriesVisibility> = {
    elevation: options?.visibility?.elevation ?? true,
    speed: options?.visibility?.speed ?? true,
    grade: options?.visibility?.grade ?? false,
    hr: options?.visibility?.hr ?? false,
    watts: options?.visibility?.watts ?? false,
  };
  const distUnit = units === "imperial" ? "mi" : "km";
  const elevUnit = units === "imperial" ? "ft" : "m";
  const speedUnit = units === "imperial" ? "mph" : "km/h";
  const distFactor = units === "imperial" ? 1 / 1609.344 : 1 / 1000;
  const dayDividers = options?.dayDividers ?? [];
  const climbBands = options?.climbBands ?? [];

  return {
    width: Math.max(120, Math.floor(width)),
    height: Math.max(64, Math.floor(height)),
    padding: [
      TELEMETRY_PADDING_TOP,
      TELEMETRY_PADDING_RIGHT,
      TELEMETRY_PADDING_BOTTOM,
      TELEMETRY_PADDING_LEFT,
    ],
    legend: { show: false },
    cursor: {
      show: true,
      y: false,
      drag: {
        x: Boolean(options?.enableDragBrush),
        y: false,
        setScale: false,
      },
    },
    scales: {
      x: { time: false },
      elev: { auto: true },
      speed: { auto: true },
      grade: { auto: true },
      hr: { auto: true },
      watts: { auto: true },
    },
    axes: [
      {
        stroke: "#8fa0b2",
        font: "10px JetBrains Mono, monospace",
        grid: { stroke: "rgba(143, 160, 178, 0.1)" },
        ticks: { stroke: "rgba(143, 160, 178, 0.15)" },
        values: (_u, vals) => vals.map((v) => `${v.toFixed(1)} ${distUnit}`),
      },
      {
        scale: "elev",
        stroke: "#14b8a6",
        font: "10px JetBrains Mono, monospace",
        size: TELEMETRY_LEFT_AXIS_SIZE,
        gap: 6,
        grid: { stroke: "rgba(143, 160, 178, 0.08)" },
        ticks: { show: false },
        values: (_u, vals) => vals.map((v) => `${Math.round(v)}${elevUnit}`),
      },
      {
        scale: "speed",
        side: 1,
        stroke: "#38bdf8",
        font: "10px JetBrains Mono, monospace",
        size: TELEMETRY_RIGHT_AXIS_SIZE,
        gap: 6,
        grid: { show: false },
        ticks: { show: false },
        show: visibility.speed,
        values: (_u, vals) => vals.map((v) => `${v.toFixed(0)} ${speedUnit}`),
      },
      {
        scale: "hr",
        side: 1,
        stroke: "#f43f5e",
        font: "10px JetBrains Mono, monospace",
        size: 46,
        gap: 4,
        grid: { show: false },
        ticks: { show: false },
        show: visibility.hr && !visibility.speed,
        values: (_u, vals) => vals.map((v) => `${Math.round(v)}`),
      },
      {
        scale: "watts",
        side: 1,
        stroke: "#a855f7",
        font: "10px JetBrains Mono, monospace",
        size: 46,
        gap: 4,
        grid: { show: false },
        ticks: { show: false },
        show: visibility.watts && !visibility.speed && !visibility.hr,
        values: (_u, vals) => vals.map((v) => `${Math.round(v)}W`),
      },
    ],
    series: [
      {},
      {
        label: `Elevation (${elevUnit})`,
        scale: "elev",
        show: visibility.elevation,
        stroke: "#14b8a6",
        fill: "rgba(20, 184, 166, 0.16)",
        width: 2,
      },
      {
        label: `Speed (${speedUnit})`,
        scale: "speed",
        show: visibility.speed,
        stroke: "#38bdf8",
        width: 1.5,
      },
      {
        label: "Grade (%)",
        scale: "grade",
        show: visibility.grade,
        stroke: "#f59e0b",
        width: 1.25,
        dash: [4, 3],
      },
      {
        label: "Heart Rate (bpm)",
        scale: "hr",
        show: visibility.hr,
        stroke: "#f43f5e",
        width: 1.4,
      },
      {
        label: "Power (W)",
        scale: "watts",
        show: visibility.watts,
        stroke: "#a855f7",
        width: 1.4,
      },
    ],
    hooks: {
      setCursor: [
        (u) => {
          options?.onCursorIndex?.(u.cursor.idx ?? null);
        },
      ],
      setSelect: [
        (u) => {
          if (!options?.onBrushRange) return;
          const { left, width: selW } = u.select;
          if (selW <= 2) return;
          const xStart = u.posToVal(left, "x");
          const xEnd = u.posToVal(left + selW, "x");
          if (Number.isFinite(xStart) && Number.isFinite(xEnd) && distFactor > 0) {
            const startM = Math.max(0, Math.min(xStart, xEnd) / distFactor);
            const endM = Math.max(0, Math.max(xStart, xEnd) / distFactor);
            options.onBrushRange(startM, endM);
          }
        },
      ],
      draw: [
        (u) => {
          const ctx = u.ctx;
          if (!ctx) return;
          const { left, top, width: bboxW, height: bboxH } = u.bbox;
          if (bboxW <= 0 || bboxH <= 0) return;

          ctx.save();
          ctx.beginPath();
          ctx.rect(left, top, bboxW, bboxH);
          ctx.clip();

          for (const band of climbBands) {
            const x0 = u.valToPos(band.startDistM * distFactor, "x", true);
            const x1 = u.valToPos(band.endDistM * distFactor, "x", true);
            if (!Number.isFinite(x0) || !Number.isFinite(x1)) continue;
            const bx = Math.min(x0, x1);
            const bw = Math.max(2, Math.abs(x1 - x0));
            ctx.fillStyle = "rgba(249, 115, 22, 0.14)";
            ctx.fillRect(bx, top, bw, bboxH);
          }

          ctx.strokeStyle = "rgba(148, 163, 184, 0.45)";
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 3]);
          for (const div of dayDividers) {
            const x = u.valToPos(div.distM * distFactor, "x", true);
            if (!Number.isFinite(x)) continue;
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, top + bboxH);
            ctx.stroke();
          }

          ctx.restore();
        },
      ],
    },
  };
}
