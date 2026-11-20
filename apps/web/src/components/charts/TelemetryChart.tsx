import { Scissors } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";
import type {
  ClimbHighlightOut,
  ManifestClimb,
  TelemetryChartPoint,
  UnitsMode,
} from "../../lib/api/types";
import {
  TELEMETRY_LEFT_AXIS_SIZE,
  TELEMETRY_PADDING_LEFT,
  TELEMETRY_PADDING_RIGHT,
  TELEMETRY_RIGHT_AXIS_SIZE,
  buildTelemetryPlotData,
  buildTelemetryPlotOptions,
  type ChartClimbBand,
  type ChartDayDivider,
  type ChartSeriesVisibility,
} from "../../lib/charts/telemetry-options";
import {
  formatDistance,
  formatElevation,
  formatGrade,
  formatHeartRate,
  formatPower,
  formatSpeed,
} from "../../lib/geo/format";
import { smoothTelemetryChartPoints, type TimelineStage } from "../../lib/geo/timeline";

export interface TelemetryChartProps {
  points: readonly TelemetryChartPoint[];
  totalDistanceM: number;
  units: UnitsMode;
  progressRatio: number;
  smoothingWindow?: number;
  stages?: readonly TimelineStage[];
  climbs?: readonly (ClimbHighlightOut | ManifestClimb)[];
  onSeek: (ratio: number) => void;
  onTrimBrush?: (startDistM: number, endDistM: number) => void;
}

function canUseCanvas2d(): boolean {
  if (
    typeof document === "undefined" ||
    (typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent))
  ) {
    return false;
  }
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("2d"));
  } catch {
    return false;
  }
}

export function TelemetryChart({
  points,
  totalDistanceM,
  units,
  progressRatio,
  smoothingWindow = 1,
  stages = [],
  climbs = [],
  onSeek,
  onTrimBrush,
}: TelemetryChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const plotRef = useRef<uPlot | null>(null);
  const [size, setSize] = useState<{ width: number; height: number }>({ width: 640, height: 108 });
  const [visibility, setVisibility] = useState<ChartSeriesVisibility>({
    elevation: true,
    speed: true,
    grade: false,
    hr: false,
    watts: false,
  });
  const [trimMode, setTrimMode] = useState(false);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [brushOverlay, setBrushOverlay] = useState<{ startRatio: number; endRatio: number } | null>(
    null,
  );
  const [hasCanvas2d] = useState<boolean>(() => canUseCanvas2d());
  const isDraggingRef = useRef(false);
  const brushAnchorRatioRef = useRef<number | null>(null);

  const clamped = Math.min(1, Math.max(0, Number.isFinite(progressRatio) ? progressRatio : 0));

  const smoothedPoints = useMemo(
    () =>
      smoothingWindow > 1
        ? smoothTelemetryChartPoints(points, smoothingWindow)
        : points,
    [points, smoothingWindow],
  );

  const hasHrData = useMemo(
    () => points.some((p) => p.hr_bpm !== null && p.hr_bpm !== undefined && Number.isFinite(p.hr_bpm)),
    [points],
  );
  const hasWattsData = useMemo(
    () => points.some((p) => p.watts !== null && p.watts !== undefined && Number.isFinite(p.watts)),
    [points],
  );

  const dayDividers = useMemo<ChartDayDivider[]>(() => {
    const dividers: ChartDayDivider[] = [];
    let prevDay: number | null = null;
    for (const st of stages) {
      if (st.points.length === 0) continue;
      if (prevDay !== null && st.dayIndex > prevDay) {
        dividers.push({
          distM: st.points[0]!.globalDistM,
          dayIndex: st.dayIndex,
          label: `Day ${st.dayIndex}`,
        });
      }
      prevDay = st.dayIndex;
    }
    return dividers;
  }, [stages]);

  const climbBands = useMemo<ChartClimbBand[]>(() => {
    if (climbs.length === 0) return [];
    const stageStartDist = new Map<string, number>();
    for (const st of stages) {
      if (st.points.length > 0) {
        stageStartDist.set(st.id, st.points[0]!.globalDistM);
      }
    }
    return climbs.map((c) => {
      const offset = stageStartDist.get(c.stage_id) ?? 0;
      return {
        id: c.id,
        name: c.name,
        category: c.category,
        startDistM: offset + c.start_dist_m,
        endDistM: offset + c.end_dist_m,
      };
    });
  }, [climbs, stages]);

  const activePoint = useMemo(() => {
    if (smoothedPoints.length === 0) {
      return null;
    }
    if (hoverIndex !== null && hoverIndex >= 0 && hoverIndex < smoothedPoints.length) {
      return smoothedPoints[hoverIndex] ?? null;
    }
    const idx = Math.min(
      smoothedPoints.length - 1,
      Math.round(clamped * (smoothedPoints.length - 1)),
    );
    return smoothedPoints[idx] ?? null;
  }, [smoothedPoints, hoverIndex, clamped]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) {
      return;
    }
    const measure = (w: number) => {
      if (w > 0) {
        const nextW = Math.max(200, Math.floor(w));
        setSize((prev) => (prev.width === nextW ? prev : { width: nextW, height: 108 }));
      }
    };
    measure(el.clientWidth);
    if (typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 0) {
        measure(rect.width);
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const el = hostRef.current;
    if (!el || !hasCanvas2d || points.length === 0) {
      plotRef.current?.destroy();
      plotRef.current = null;
      return;
    }

    const data = buildTelemetryPlotData(points, units, { smoothingWindow });
    const opts = buildTelemetryPlotOptions(size.width, size.height, {
      units,
      visibility,
      dayDividers,
      climbBands,
      enableDragBrush: trimMode,
      onCursorIndex: (idx) => setHoverIndex(idx),
      onBrushRange: (startDistM, endDistM) => {
        if (trimMode && onTrimBrush && endDistM - startDistM > 5) {
          onTrimBrush(startDistM, endDistM);
          setTrimMode(false);
        }
      },
    });

    const plot = new uPlot(opts, data, el);
    plotRef.current = plot;

    return () => {
      plot.destroy();
      if (plotRef.current === plot) {
        plotRef.current = null;
      }
    };
  }, [
    hasCanvas2d,
    points,
    units,
    visibility,
    smoothingWindow,
    dayDividers,
    climbBands,
    trimMode,
    onTrimBrush,
    size.width,
    size.height,
  ]);

  const leftPad = hasCanvas2d ? TELEMETRY_LEFT_AXIS_SIZE + TELEMETRY_PADDING_LEFT : 0;
  const rightPad = hasCanvas2d
    ? (visibility.speed
        ? TELEMETRY_RIGHT_AXIS_SIZE
        : visibility.hr || visibility.watts
          ? 46
          : 0) + TELEMETRY_PADDING_RIGHT
    : 0;

  function ratioFromClientX(clientX: number): number | null {
    const el = hostRef.current ?? containerRef.current;
    if (!el) {
      return null;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) {
      return null;
    }
    const innerWidth = Math.max(1, rect.width - leftPad - rightPad);
    const ratio = (clientX - rect.left - leftPad) / innerWidth;
    return Math.min(1, Math.max(0, ratio));
  }

  function seekFromPointer(clientX: number) {
    const ratio = ratioFromClientX(clientX);
    if (ratio !== null) {
      onSeek(ratio);
    }
  }

  const effectiveTotalDist = Math.max(
    totalDistanceM,
    smoothedPoints[smoothedPoints.length - 1]?.cum_dist_m ?? 0,
    1,
  );

  return (
    <div data-testid="telemetry-chart-dock" className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            aria-pressed={visibility.elevation}
            onClick={() => setVisibility((v) => ({ ...v, elevation: !v.elevation }))}
            className={`rounded border px-2 py-0.5 transition-colors ${
              visibility.elevation
                ? "border-teal-accent/60 bg-teal-accent/15 text-teal-accent"
                : "border-border bg-elevated text-ink-muted"
            }`}
          >
            Elevation
          </button>
          <button
            type="button"
            aria-pressed={visibility.speed}
            onClick={() => setVisibility((v) => ({ ...v, speed: !v.speed }))}
            className={`rounded border px-2 py-0.5 transition-colors ${
              visibility.speed
                ? "border-sky-accent/60 bg-sky-accent/15 text-sky-accent"
                : "border-border bg-elevated text-ink-muted"
            }`}
          >
            Speed
          </button>
          <button
            type="button"
            aria-pressed={visibility.grade}
            onClick={() => setVisibility((v) => ({ ...v, grade: !v.grade }))}
            className={`rounded border px-2 py-0.5 transition-colors ${
              visibility.grade
                ? "border-amber-accent/60 bg-amber-accent/15 text-amber-accent"
                : "border-border bg-elevated text-ink-muted"
            }`}
          >
            Grade
          </button>
          <button
            type="button"
            aria-pressed={Boolean(visibility.hr)}
            disabled={!hasHrData && !visibility.hr}
            title={hasHrData ? "Toggle heart rate series" : "No heart rate data in tracks"}
            onClick={() => setVisibility((v) => ({ ...v, hr: !v.hr }))}
            className={`rounded border px-2 py-0.5 transition-colors disabled:opacity-45 ${
              visibility.hr
                ? "border-coral-accent/60 bg-coral-accent/15 text-coral-accent"
                : "border-border bg-elevated text-ink-muted"
            }`}
          >
            HR
          </button>
          <button
            type="button"
            aria-pressed={Boolean(visibility.watts)}
            disabled={!hasWattsData && !visibility.watts}
            title={hasWattsData ? "Toggle power (Watts) series" : "No power data in tracks"}
            onClick={() => setVisibility((v) => ({ ...v, watts: !v.watts }))}
            className={`rounded border px-2 py-0.5 transition-colors disabled:opacity-45 ${
              visibility.watts
                ? "border-purple-400/60 bg-purple-400/15 text-purple-300"
                : "border-border bg-elevated text-ink-muted"
            }`}
          >
            Watts
          </button>

          {onTrimBrush ? (
            <button
              type="button"
              aria-pressed={trimMode}
              onClick={() => {
                setTrimMode((v) => !v);
                setBrushOverlay(null);
              }}
              className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 transition-colors ${
                trimMode
                  ? "border-amber-accent bg-amber-accent/20 text-amber-accent"
                  : "border-border bg-elevated text-ink-muted hover:text-ink"
              }`}
            >
              <Scissors className="h-3 w-3" aria-hidden="true" />
              <span>{trimMode ? "Drag chart to trim…" : "Trim brush"}</span>
            </button>
          ) : null}
        </div>

        <div className="tabular-telemetry flex flex-wrap items-center gap-3 text-ink-muted">
          <span>
            {activePoint ? formatDistance(activePoint.cum_dist_m, units) : formatDistance(0, units)}
            {" / "}
            {formatDistance(totalDistanceM, units)}
          </span>
          <span className="text-teal-accent">
            {formatElevation(activePoint?.elev_m, units)}
          </span>
          <span className="text-sky-accent">
            {formatSpeed(activePoint?.speed_mps, units)}
          </span>
          {activePoint?.grade_pct !== null && activePoint?.grade_pct !== undefined ? (
            <span className="text-amber-accent">{formatGrade(activePoint.grade_pct)}</span>
          ) : null}
          {visibility.hr || (activePoint?.hr_bpm !== null && activePoint?.hr_bpm !== undefined) ? (
            <span className="text-coral-accent">{formatHeartRate(activePoint?.hr_bpm)}</span>
          ) : null}
          {visibility.watts || (activePoint?.watts !== null && activePoint?.watts !== undefined) ? (
            <span className="text-purple-300">{formatPower(activePoint?.watts)}</span>
          ) : null}
        </div>
      </div>

      <div
        ref={containerRef}
        className="relative h-[112px] w-full overflow-hidden rounded-md border border-border bg-canvas/75 select-none"
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture?.(e.pointerId);
          isDraggingRef.current = true;
          if (trimMode) {
            const ratio = ratioFromClientX(e.clientX);
            if (ratio !== null) {
              brushAnchorRatioRef.current = ratio;
              setBrushOverlay({ startRatio: ratio, endRatio: ratio });
            }
          } else {
            seekFromPointer(e.clientX);
          }
        }}
        onPointerMove={(e) => {
          const ratio = ratioFromClientX(e.clientX);
          if (ratio !== null && !hasCanvas2d && smoothedPoints.length > 0) {
            const idx = Math.min(
              smoothedPoints.length - 1,
              Math.round(ratio * (smoothedPoints.length - 1)),
            );
            setHoverIndex(idx);
          }
          if (!isDraggingRef.current) return;
          if (trimMode && brushAnchorRatioRef.current !== null && ratio !== null) {
            setBrushOverlay({
              startRatio: Math.min(brushAnchorRatioRef.current, ratio),
              endRatio: Math.max(brushAnchorRatioRef.current, ratio),
            });
          } else if (!trimMode) {
            seekFromPointer(e.clientX);
          }
        }}
        onPointerLeave={() => {
          if (!isDraggingRef.current) {
            setHoverIndex(null);
          }
        }}
        onPointerUp={(e) => {
          isDraggingRef.current = false;
          if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
            e.currentTarget.releasePointerCapture?.(e.pointerId);
          }
          if (trimMode && brushOverlay && onTrimBrush) {
            const spanRatio = Math.abs(brushOverlay.endRatio - brushOverlay.startRatio);
            if (spanRatio > 0.01) {
              onTrimBrush(
                brushOverlay.startRatio * effectiveTotalDist,
                brushOverlay.endRatio * effectiveTotalDist,
              );
              setTrimMode(false);
            }
            brushAnchorRatioRef.current = null;
            setBrushOverlay(null);
          }
        }}
        onPointerCancel={(e) => {
          isDraggingRef.current = false;
          brushAnchorRatioRef.current = null;
          setBrushOverlay(null);
          if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
            e.currentTarget.releasePointerCapture?.(e.pointerId);
          }
        }}
      >
        {points.length === 0 ? (
          <div className="flex h-full items-center justify-center text-xs text-ink-muted">
            Upload GPX, FIT, or TCX tracks or import Strava activities to view DuckDB telemetry profiles.
          </div>
        ) : (
          <>
            <div
              ref={hostRef}
              className={`h-full w-full ${trimMode ? "cursor-crosshair" : "cursor-ew-resize"}`}
            />
            {!hasCanvas2d ? (
              <svg
                viewBox="0 0 600 90"
                preserveAspectRatio="none"
                className="pointer-events-none absolute inset-0 h-full w-full"
                aria-hidden="true"
              >
                {climbBands.map((band) => {
                  const x0 = (band.startDistM / effectiveTotalDist) * 600;
                  const x1 = (band.endDistM / effectiveTotalDist) * 600;
                  return (
                    <rect
                      key={band.id}
                      x={Math.min(x0, x1)}
                      y={0}
                      width={Math.max(4, Math.abs(x1 - x0))}
                      height={90}
                      fill="rgba(249, 115, 22, 0.16)"
                    />
                  );
                })}
                {dayDividers.map((div) => {
                  const dx = (div.distM / effectiveTotalDist) * 600;
                  return (
                    <line
                      key={`day-${div.dayIndex}-${div.distM}`}
                      x1={dx}
                      x2={dx}
                      y1={0}
                      y2={90}
                      stroke="rgba(148, 163, 184, 0.45)"
                      strokeDasharray="4 3"
                      strokeWidth="1"
                    />
                  );
                })}
                <path
                  d="M 0 78 L 80 65 L 180 42 L 290 25 L 410 38 L 520 18 L 600 32 L 600 90 L 0 90 Z"
                  fill="rgba(20, 184, 166, 0.14)"
                />
                <path
                  d="M 0 78 L 80 65 L 180 42 L 290 25 L 410 38 L 520 18 L 600 32"
                  fill="none"
                  stroke="#14b8a6"
                  strokeWidth="2"
                />
              </svg>
            ) : null}

            {trimMode && brushOverlay ? (
              <div
                data-testid="telemetry-trim-brush-selection"
                className="pointer-events-none absolute top-0 bottom-0 border-x border-amber-accent bg-amber-accent/20"
                style={{
                  left: hasCanvas2d
                    ? `calc(${leftPad}px + (100% - ${leftPad + rightPad}px) * ${brushOverlay.startRatio})`
                    : `${brushOverlay.startRatio * 100}%`,
                  width: hasCanvas2d
                    ? `calc((100% - ${leftPad + rightPad}px) * ${Math.max(0, brushOverlay.endRatio - brushOverlay.startRatio)})`
                    : `${Math.max(0, brushOverlay.endRatio - brushOverlay.startRatio) * 100}%`,
                }}
              />
            ) : null}

            <div
              data-testid="telemetry-playhead"
              className="pointer-events-none absolute top-0 bottom-0 w-0.5 bg-amber-accent"
              style={{
                left: hasCanvas2d
                  ? `calc(${leftPad}px + (100% - ${leftPad + rightPad}px) * ${clamped})`
                  : `${clamped * 100}%`,
              }}
            />
          </>
        )}
      </div>
    </div>
  );
}
