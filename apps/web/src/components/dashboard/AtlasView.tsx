import { MapboxOverlay } from "@deck.gl/mapbox";
import { PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { Link } from "@tanstack/react-router";
import { Compass, Globe2, Mountain, Route } from "lucide-react";
import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AnalyticsHeatmapOut,
  AtlasTripOut,
  CareerAnalyticsOut,
  HeatmapCellOut,
  ProjectOut,
  UnitsMode,
} from "../../lib/api/types";
import { formatDistance, formatElapsedTime, formatElevation } from "../../lib/geo/format";
import { buildMapLibreInitOptions, resolveMapStyleUrl, sanitizeMapStyle } from "../../lib/geo/map-config";
import { ActivityIcon, getActivityIconMeta } from "../icons";

export interface AtlasViewProps {
  projects: readonly ProjectOut[];
  career?: CareerAnalyticsOut | null;
  heatmap?: AnalyticsHeatmapOut | null;
  units?: UnitsMode;
}

const TRIP_PALETTE: readonly [number, number, number][] = [
  [56, 189, 248],
  [20, 184, 166],
  [245, 158, 11],
  [249, 115, 22],
  [168, 85, 247],
  [236, 72, 153],
];

function isWebGlAvailable(): boolean {
  if (
    typeof window === "undefined" ||
    typeof document === "undefined" ||
    (typeof WebGL2RenderingContext === "undefined" && typeof WebGLRenderingContext === "undefined")
  ) {
    return false;
  }
  try {
    const c = document.createElement("canvas");
    return Boolean(c.getContext("webgl2") || c.getContext("webgl"));
  } catch {
    return false;
  }
}

export function AtlasView({
  projects,
  career,
  heatmap,
  units = "metric",
}: AtlasViewProps) {
  const mapContainerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const [yearFilter, setYearFilter] = useState<string>("all");
  const [activityFilter, setActivityFilter] = useState<string>("all");
  const [webglAvailable] = useState<boolean>(() => isWebGlAvailable());

  const safeTrips: AtlasTripOut[] = useMemo(
    () => (Array.isArray(heatmap?.trips) ? heatmap.trips : []),
    [heatmap],
  );
  const safeHexCells: HeatmapCellOut[] = useMemo(
    () => (Array.isArray(heatmap?.hex_cells) ? heatmap.hex_cells : []),
    [heatmap],
  );
  const safeCountries: string[] = useMemo(
    () => (Array.isArray(career?.countries_visited) ? career.countries_visited : []),
    [career],
  );
  const safeByYear = useMemo(
    () => (Array.isArray(career?.by_year) ? career.by_year : []),
    [career],
  );
  const safeByActivity = useMemo(
    () => (Array.isArray(career?.by_activity) ? career.by_activity : []),
    [career],
  );

  const availableYears = useMemo(() => {
    const years = new Set<number>();
    for (const b of safeByYear) years.add(b.year);
    for (const t of safeTrips) {
      if (t.year !== null) years.add(t.year);
    }
    return Array.from(years).sort((a, b) => b - a);
  }, [safeByYear, safeTrips]);

  const availableActivities = useMemo(() => {
    const acts = new Set<string>();
    for (const a of safeByActivity) acts.add(a.activity_type);
    for (const t of safeTrips) {
      if (t.primary_activity) acts.add(t.primary_activity);
    }
    return Array.from(acts).sort();
  }, [safeByActivity, safeTrips]);

  const filteredTrips = useMemo(
    () =>
      safeTrips.filter((t) => {
        if (yearFilter !== "all" && String(t.year ?? "") !== yearFilter) return false;
        if (activityFilter !== "all" && t.primary_activity !== activityFilter) return false;
        return true;
      }),
    [safeTrips, yearFilter, activityFilter],
  );

  const totals = useMemo(() => {
    const hasCareer = career && typeof career.total_distance_m === "number";
    if (hasCareer && yearFilter === "all" && activityFilter === "all") {
      return {
        projects: career.total_projects ?? projects.length,
        stages: career.total_stages ?? projects.reduce((a, p) => a + p.stage_count, 0),
        distanceM: career.total_distance_m,
        elevationM: career.total_elevation_gain_m ?? 0,
        movingS: career.total_moving_seconds ?? 0,
      };
    }
    if (filteredTrips.length > 0) {
      return {
        projects: filteredTrips.length,
        stages: filteredTrips.reduce((a, t) => a + t.stage_count, 0),
        distanceM: filteredTrips.reduce((a, t) => a + t.distance_m, 0),
        elevationM: filteredTrips.reduce((a, t) => a + t.elevation_gain_m, 0),
        movingS: career?.total_moving_seconds ?? 0,
      };
    }
    return {
      projects: projects.length,
      stages: projects.reduce((a, p) => a + p.stage_count, 0),
      distanceM: projects.reduce((a, p) => a + p.total_distance_m, 0),
      elevationM: 0,
      movingS: 0,
    };
  }, [career, projects, filteredTrips, yearFilter, activityFilter]);

  const bounds = useMemo<[number, number, number, number]>(() => {
    let minLon = Infinity;
    let minLat = Infinity;
    let maxLon = -Infinity;
    let maxLat = -Infinity;
    for (const trip of filteredTrips) {
      for (const line of trip.polylines) {
        for (const [lon, lat] of line) {
          if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
          if (lon < minLon) minLon = lon;
          if (lon > maxLon) maxLon = lon;
          if (lat < minLat) minLat = lat;
          if (lat > maxLat) maxLat = lat;
        }
      }
    }
    for (const cell of safeHexCells) {
      if (!Number.isFinite(cell.lon) || !Number.isFinite(cell.lat)) continue;
      if (cell.lon < minLon) minLon = cell.lon;
      if (cell.lon > maxLon) maxLon = cell.lon;
      if (cell.lat < minLat) minLat = cell.lat;
      if (cell.lat > maxLat) maxLat = cell.lat;
    }
    if (!Number.isFinite(minLon)) {
      return [5.0, 44.0, 16.0, 48.5];
    }
    return [minLon, minLat, maxLon, maxLat];
  }, [filteredTrips, safeHexCells]);

  useEffect(() => {
    const container = mapContainerRef.current;
    if (!container || !webglAvailable) return;

    const map = new maplibregl.Map(
      buildMapLibreInitOptions(container, "dark", { interactive: true, deferStyle: true }),
    );
    map.setStyle(resolveMapStyleUrl("dark"), {
      transformStyle: (_prev, next) => sanitizeMapStyle(next, "dark"),
    });
    const overlay = new MapboxOverlay({ interleaved: true, layers: [] });
    map.addControl(overlay as unknown as maplibregl.IControl);
    mapRef.current = map;
    overlayRef.current = overlay;

    return () => {
      try {
        map.removeControl(overlay as unknown as maplibregl.IControl);
      } catch {
        // Ignore teardown race
      }
      overlay.finalize();
      map.remove();
      mapRef.current = null;
      overlayRef.current = null;
    };
  }, [webglAvailable]);

  useEffect(() => {
    const map = mapRef.current;
    const overlay = overlayRef.current;
    if (!map || !overlay) return;

    const pathData: { id: string; color: [number, number, number]; path: [number, number][] }[] = [];
    filteredTrips.forEach((trip, idx) => {
      const color = TRIP_PALETTE[idx % TRIP_PALETTE.length]!;
      trip.polylines.forEach((line, lineIdx) => {
        if (line.length >= 2) {
          pathData.push({ id: `${trip.project_id}-${lineIdx}`, color, path: line });
        }
      });
    });

    const maxCellCount = Math.max(
      1,
      ...safeHexCells.map((c) => c.point_count),
    );

    overlay.setProps({
      interleaved: true,
      layers: [
        new ScatterplotLayer<HeatmapCellOut>({
          id: "atlas-h3-heatmap",
          data: safeHexCells,
          getPosition: (d) => [d.lon, d.lat],
          getFillColor: (d) => {
            const intensity = Math.min(1, d.point_count / maxCellCount);
            return [249, Math.round(180 - intensity * 90), 22, Math.round(70 + intensity * 140)];
          },
          radiusUnits: "pixels",
          getRadius: (d) => 4 + Math.min(10, Math.sqrt(d.point_count) * 0.6),
        }),
        new PathLayer<{ id: string; color: [number, number, number]; path: [number, number][] }>({
          id: "atlas-trip-paths",
          data: pathData,
          getPath: (d) => d.path,
          getColor: (d) => d.color,
          getWidth: 3,
          widthUnits: "pixels",
          capRounded: true,
          jointRounded: true,
        }),
      ],
    });

    if (filteredTrips.length > 0 || safeHexCells.length > 0) {
      try {
        map.fitBounds(
          [
            [bounds[0], bounds[1]],
            [bounds[2], bounds[3]],
          ],
          { padding: 36, duration: 0 },
        );
      } catch {
        // Ignore fitBounds error on tiny bounds
      }
    }
  }, [filteredTrips, safeHexCells, bounds]);

  const lonSpan = Math.max(bounds[2] - bounds[0], 0.01);
  const latSpan = Math.max(bounds[3] - bounds[1], 0.01);
  const projectSvg = (lon: number, lat: number): [number, number] => [
    24 + ((lon - bounds[0]) / lonSpan) * 552,
    220 - ((lat - bounds[1]) / latSpan) * 196,
  ];

  return (
    <div data-testid="atlas-view" className="space-y-5">
      {/* Career KPI Strip */}
      <section
        aria-label="Career totals"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        <div className="rounded-lg border border-border bg-surface p-3.5">
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <Route className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
            <span>Total Trips</span>
          </div>
          <p className="tabular-telemetry mt-1 text-xl font-semibold text-ink">
            {totals.projects}
          </p>
          <p className="tabular-telemetry text-[11px] text-ink-muted">{totals.stages} stages</p>
        </div>

        <div className="rounded-lg border border-border bg-surface p-3.5">
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <Compass className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
            <span>Total Distance</span>
          </div>
          <p className="tabular-telemetry mt-1 text-xl font-semibold text-ink">
            {formatDistance(totals.distanceM, units)}
          </p>
          <p className="text-[11px] text-ink-muted">Cumulative logged</p>
        </div>

        <div className="rounded-lg border border-border bg-surface p-3.5">
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <Mountain className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
            <span>Elevation Gain</span>
          </div>
          <p className="tabular-telemetry mt-1 text-xl font-semibold text-teal-accent">
            +{formatElevation(totals.elevationM, units)}
          </p>
          <p className="text-[11px] text-ink-muted">Vertical climbed</p>
        </div>

        <div className="rounded-lg border border-border bg-surface p-3.5">
          <div className="flex items-center gap-1.5 text-[11px] text-ink-muted">
            <Globe2 className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
            <span>Moving Time</span>
          </div>
          <p className="tabular-telemetry mt-1 text-xl font-semibold text-ink">
            {formatElapsedTime(totals.movingS)}
          </p>
          <p className="text-[11px] text-ink-muted">
            {safeCountries.length > 0
              ? `${safeCountries.length} countr${safeCountries.length === 1 ? "y" : "ies"}`
              : "Active time"}
          </p>
        </div>
      </section>

      {/* Filters & Countries */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {availableYears.length > 0 ? (
            <select
              aria-label="Filter atlas by year"
              value={yearFilter}
              onChange={(e) => setYearFilter(e.currentTarget.value)}
              className="rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs text-ink"
            >
              <option value="all">All years</option>
              {availableYears.map((y) => (
                <option key={y} value={String(y)}>
                  {y}
                </option>
              ))}
            </select>
          ) : null}

          {availableActivities.length > 0 ? (
            <select
              aria-label="Filter atlas by activity"
              value={activityFilter}
              onChange={(e) => setActivityFilter(e.currentTarget.value)}
              className="rounded-md border border-border bg-surface px-2.5 py-1.5 text-xs text-ink"
            >
              <option value="all">All activities</option>
              {availableActivities.map((act) => (
                <option key={act} value={act}>
                  {getActivityIconMeta(act).label}
                </option>
              ))}
            </select>
          ) : null}
        </div>

        {safeCountries.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            {safeCountries.map((country) => (
              <span
                key={country}
                className="rounded-full border border-border bg-surface px-2.5 py-0.5 text-[11px] text-ink"
              >
                {country}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      {/* Multi-Trip Interactive Map + H3 Heatmap */}
      <div className="relative h-[340px] w-full overflow-hidden rounded-xl border border-border bg-[#0b1117]">
        {webglAvailable ? (
          <div ref={mapContainerRef} className="absolute inset-0 h-full w-full" />
        ) : (
          <div className="absolute inset-0">
            <div className="topo-grid-surface absolute inset-0" aria-hidden="true" />
            <svg
              viewBox="0 0 600 240"
              preserveAspectRatio="xMidYMid meet"
              className="relative h-full w-full"
              role="img"
              aria-label="Multi-trip career atlas map"
            >
              {safeHexCells.map((cell) => {
                const [cx, cy] = projectSvg(cell.lon, cell.lat);
                return (
                  <circle
                    key={cell.h3_index}
                    cx={cx}
                    cy={cy}
                    r={Math.min(12, 3 + Math.sqrt(cell.point_count) * 0.4)}
                    fill="rgba(249, 115, 22, 0.38)"
                  />
                );
              })}
              {filteredTrips.map((trip, idx) => {
                const rgb = TRIP_PALETTE[idx % TRIP_PALETTE.length]!;
                const stroke = `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
                return trip.polylines.map((line, lIdx) => {
                  const d = line
                    .map(([lon, lat], pIdx) => {
                      const [x, y] = projectSvg(lon, lat);
                      return `${pIdx === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
                    })
                    .join(" ");
                  return (
                    <path
                      key={`${trip.project_id}-${lIdx}`}
                      d={d}
                      fill="none"
                      stroke={stroke}
                      strokeWidth={2.2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  );
                });
              })}
            </svg>
          </div>
        )}

        {filteredTrips.length === 0 && safeHexCells.length === 0 ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <div className="rounded-lg border border-border bg-canvas/85 px-4 py-3 text-center text-xs text-ink-muted backdrop-blur-sm">
              Upload GPX/FIT/TCX stages inside any trip to plot your multi-trip Atlas &amp; heatmap.
            </div>
          </div>
        ) : null}
      </div>

      {/* Trip Breakdown List */}
      {filteredTrips.length > 0 ? (
        <section aria-label="Atlas trips" className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
          {filteredTrips.map((trip, idx) => {
            const rgb = TRIP_PALETTE[idx % TRIP_PALETTE.length]!;
            const color = `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
            const meta = getActivityIconMeta(trip.primary_activity);
            return (
              <Link
                key={trip.project_id}
                to="/projects/$projectId"
                params={{ projectId: trip.project_id }}
                className="flex items-center justify-between gap-3 rounded-lg border border-border bg-surface p-3.5 transition-colors hover:border-sky-accent/60"
              >
                <div className="flex min-w-0 items-center gap-2.5">
                  <span
                    className="h-3 w-3 shrink-0 rounded-full"
                    style={{ backgroundColor: color }}
                    aria-hidden="true"
                  />
                  <ActivityIcon
                    type={trip.primary_activity}
                    size={16}
                    accentColor={meta.accentColor}
                  />
                  <div className="min-w-0">
                    <p className="truncate text-xs font-semibold text-ink">{trip.title}</p>
                    <p className="tabular-telemetry text-[11px] text-ink-muted">
                      {formatDistance(trip.distance_m, units)} • +
                      {formatElevation(trip.elevation_gain_m, units)}
                    </p>
                  </div>
                </div>
                <span className="tabular-telemetry shrink-0 text-[10px] text-ink-muted">
                  {trip.stage_count} stg
                </span>
              </Link>
            );
          })}
        </section>
      ) : null}
    </div>
  );
}
