import { MapboxOverlay } from "@deck.gl/mapbox";
import {
  AlertTriangle,
  BedDouble,
  Camera,
  Coffee,
  Loader2,
  MapPin,
  Mountain,
  RefreshCw,
  Tent,
  TrainFront,
  Utensils,
  X,
} from "lucide-react";
import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";
import {
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import type {
  ClimbHighlightOut,
  ManifestClimb,
  RenderSpecV2,
  ResolutionPreset,
  TransitionIcon,
} from "../../lib/api/types";
import { computeCameraFrame, is3dCameraActive } from "../../lib/geo/camera";
import {
  formatDateInTimeZone,
  formatDistance,
  formatElapsedTime,
  formatElevation,
  parseResolutionPreset,
} from "../../lib/geo/format";
import {
  clampFitTrackPadding,
  projectLngLatToScreen,
  solveStageLayout,
  type ViewportPadding,
} from "../../lib/geo/layout";
import {
  TERRAIN_SOURCE_ID,
  buildDeckOverlayProps,
  buildElevationCurtainData,
  buildMapLibreInitOptions,
  buildTerrariumDemSourceSpec,
  resolveMapStyleUrl,
  sanitizeMapStyle,
} from "../../lib/geo/map-config";
import type { ActivePhotoDisplay, MatchedPhoto } from "../../lib/geo/photos";
import {
  computeAdaptiveFollowCamera,
  evaluateTimeline,
  mapClimbsToTimelineProgress,
  type ClimbProgressMarker,
  type EvaluatedTimelineState,
  type StopMarker,
  type TimelineStage,
} from "../../lib/geo/timeline";
import { ActivityIcon, ActivityMarkerBadge, ActivitySvgGroup } from "../icons";
import { StatisticsHud } from "./StatisticsHud";

const ASPECT_RATIO_CLASS: Record<ResolutionPreset, string> = {
  "1920x1080": "aspect-video max-w-full max-h-full",
  "1280x720": "aspect-video max-w-full max-h-full",
  "1080x1080": "aspect-square max-h-full",
  "1080x1350": "aspect-[4/5] max-h-full",
  "1080x1920": "aspect-[9/16] max-h-full",
};

const BUILDINGS_LAYER_ID = "trip-3d-buildings";
const EMPTY_MATCHED_PHOTOS: readonly MatchedPhoto[] = [];
const EMPTY_CLIMBS: readonly (ClimbHighlightOut | ManifestClimb)[] = [];

export interface TripStageCanvasHandle {
  waitForReady: () => Promise<void>;
  waitForFrameSettle: (cameraChanged?: boolean) => Promise<void>;
}

export interface CanvasGeometryStatus {
  pendingCount: number;
  errorCount: number;
  onRetryFailed?: () => void;
}

export interface TripStageCanvasProps {
  stages: readonly TimelineStage[];
  spec: RenderSpecV2;
  progressRatio: number;
  durationS?: number;
  travelDurationS?: number;
  isPreview?: boolean;
  renderMode?: "interactive" | "deterministic";
  exactSize?: { width: number; height: number };
  jobId?: string;
  controllerRef?: RefObject<TripStageCanvasHandle | null>;
  geometryStatus?: CanvasGeometryStatus;
  matchedPhotos?: readonly MatchedPhoto[];
  activePhoto?: ActivePhotoDisplay | null;
  climbs?: readonly (ClimbHighlightOut | ManifestClimb)[];
  pinModeActive?: boolean;
  onMapPinClick?: (lngLat: [number, number]) => void;
  onCancelPinMode?: () => void;
  projectTitle?: string;
  totalStats?: {
    distanceM: number;
    elevationGainM: number;
    movingSeconds: number;
    stageCount: number;
  };
}

function TransitionGlyph({ icon }: { icon: TransitionIcon }) {
  switch (icon) {
    case "bed":
      return <BedDouble className="h-3.5 w-3.5" aria-hidden="true" />;
    case "meal":
      return <Utensils className="h-3.5 w-3.5" aria-hidden="true" />;
    case "coffee":
      return <Coffee className="h-3.5 w-3.5" aria-hidden="true" />;
    case "tent":
      return <Tent className="h-3.5 w-3.5" aria-hidden="true" />;
    case "transport":
      return <TrainFront className="h-3.5 w-3.5" aria-hidden="true" />;
    default:
      return <MapPin className="h-3.5 w-3.5" aria-hidden="true" />;
  }
}

function isWebGlSupported(): boolean {
  if (
    typeof window === "undefined" ||
    typeof document === "undefined" ||
    (typeof WebGL2RenderingContext === "undefined" && typeof WebGLRenderingContext === "undefined")
  ) {
    return false;
  }
  try {
    const canvas = document.createElement("canvas");
    return Boolean(
      canvas.getContext("webgl2") ||
        canvas.getContext("webgl") ||
        canvas.getContext("experimental-webgl"),
    );
  } catch {
    return false;
  }
}

function awaitNextAnimationFrames(count = 2): Promise<void> {
  return new Promise((resolve) => {
    let remaining = Math.max(1, count);
    const step = () => {
      remaining -= 1;
      if (remaining <= 0) {
        resolve();
      } else {
        window.requestAnimationFrame(step);
      }
    };
    window.requestAnimationFrame(step);
  });
}

function sync3dBuildingsAndSky(
  map: MapLibreMap,
  enable3d: boolean,
  terrainExaggeration = 1.4,
  mapStyle?: RenderSpecV2["map_style"],
): void {
  try {
    if (!map.isStyleLoaded()) {
      return;
    }
    const style = map.getStyle();
    if (!style || !style.sources) {
      return;
    }

    // 3D DEM terrain & hillshade (safe offline fallback)
    const wantTerrain = enable3d || mapStyle === "topo-relief";
    const terrainCapable = map as unknown as {
      getSource?: (id: string) => unknown;
      addSource?: (id: string, spec: unknown) => void;
      setTerrain?: (terrain: { source: string; exaggeration?: number } | null) => void;
    };
    if (wantTerrain) {
      try {
        if (
          typeof terrainCapable.getSource === "function" &&
          typeof terrainCapable.addSource === "function" &&
          !terrainCapable.getSource(TERRAIN_SOURCE_ID)
        ) {
          terrainCapable.addSource(TERRAIN_SOURCE_ID, buildTerrariumDemSourceSpec());
        }
        if (typeof terrainCapable.setTerrain === "function") {
          terrainCapable.setTerrain({
            source: TERRAIN_SOURCE_ID,
            exaggeration: Math.min(3, Math.max(0.5, terrainExaggeration)),
          });
        }
      } catch {
        // Ignore DEM source errors when offline or running in mocked environments
      }
    } else if (typeof terrainCapable.setTerrain === "function") {
      try {
        terrainCapable.setTerrain(null);
      } catch {
        // Ignore teardown race
      }
    }

    let buildingSourceId: string | null = null;
    if ("openmaptiles" in style.sources) {
      buildingSourceId = "openmaptiles";
    } else {
      for (const layer of style.layers ?? []) {
        const candidate = layer as { source?: string; "source-layer"?: string };
        if (candidate["source-layer"] === "building" && typeof candidate.source === "string") {
          buildingSourceId = candidate.source;
          break;
        }
      }
    }

    if (buildingSourceId) {
      if (!map.getLayer(BUILDINGS_LAYER_ID)) {
        map.addLayer({
          id: BUILDINGS_LAYER_ID,
          source: buildingSourceId,
          "source-layer": "building",
          type: "fill-extrusion",
          minzoom: 13,
          layout: {
            visibility: enable3d ? "visible" : "none",
          },
          paint: {
            "fill-extrusion-color": "#334155",
            "fill-extrusion-height": [
              "case",
              ["==", ["typeof", ["get", "render_height"]], "number"],
              ["get", "render_height"],
              ["==", ["typeof", ["get", "height"]], "number"],
              ["get", "height"],
              12,
            ],
            "fill-extrusion-base": [
              "case",
              ["==", ["typeof", ["get", "render_min_height"]], "number"],
              ["get", "render_min_height"],
              ["==", ["typeof", ["get", "min_height"]], "number"],
              ["get", "min_height"],
              0,
            ],
            "fill-extrusion-opacity": 0.55,
          },
        });
      } else {
        map.setLayoutProperty(
          BUILDINGS_LAYER_ID,
          "visibility",
          enable3d ? "visible" : "none",
        );
      }
    }

    const skyCapable = map as unknown as {
      setSky?: (sky: Record<string, unknown> | undefined) => void;
    };
    if (typeof skyCapable.setSky === "function") {
      if (enable3d) {
        skyCapable.setSky({
          "sky-color": "#0d1b2a",
          "sky-horizon-blend": 0.45,
          "horizon-color": "#1e293b",
          "horizon-fog-blend": 0.55,
          "fog-color": "#0f172a",
          "fog-ground-blend": 0.45,
        });
      } else {
        skyCapable.setSky(undefined);
      }
    }
  } catch {
    // Ignore styles without building vector layers or unsupported sky properties
  }
}

interface MiniElevationProfile {
  fullPath: string;
  areaPath: string;
  progPath: string;
  progAreaPath: string;
  cursorX: number;
  cursorY: number;
  minElev: number;
  maxElev: number;
  currentElev: number | null;
  currentGradePct: number | null;
}

const ELEV_W = 240;
const ELEV_H = 54;
const ELEV_TOP = 6;
const ELEV_BOTTOM = 48;

function buildMiniElevationPath(
  stages: readonly TimelineStage[],
  evaluated: EvaluatedTimelineState,
): MiniElevationProfile | null {
  const pts: { dist: number; elev: number }[] = [];
  for (const s of stages) {
    for (const p of s.points) {
      if (p.elevM !== null) {
        pts.push({ dist: p.globalDistM, elev: p.elevM });
      }
    }
  }
  if (pts.length < 2) {
    return null;
  }
  const maxDist = Math.max(pts[pts.length - 1]!.dist, 1);
  let minE = Infinity;
  let maxE = -Infinity;
  for (const p of pts) {
    if (p.elev < minE) minE = p.elev;
    if (p.elev > maxE) maxE = p.elev;
  }
  const spanE = Math.max(maxE - minE, 10);

  const sampleStep = Math.max(1, Math.floor(pts.length / 120));
  const sampled = pts.filter((_, i) => i % sampleStep === 0 || i === pts.length - 1);

  const toXY = (dist: number, elev: number): [number, number] => {
    const x = (dist / maxDist) * ELEV_W;
    const y = ELEV_BOTTOM - ((elev - minE) / spanE) * (ELEV_BOTTOM - ELEV_TOP);
    return [x, y];
  };

  const toLinePath = (coords: [number, number][]) =>
    coords
      .map(([x, y], idx) => `${idx === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`)
      .join(" ");

  const toAreaPath = (coords: [number, number][]) => {
    if (coords.length < 2) return "";
    const first = coords[0]!;
    const last = coords[coords.length - 1]!;
    return `${toLinePath(coords)} L ${last[0].toFixed(1)} ${ELEV_H} L ${first[0].toFixed(1)} ${ELEV_H} Z`;
  };

  const fullCoords = sampled.map((p) => toXY(p.dist, p.elev));
  const cutoffDist = Math.min(maxDist, Math.max(0, evaluated.distanceTravelledM));
  const currentElev = evaluated.elevM ?? pts[0]!.elev;
  const [cursorX, cursorY] = toXY(cutoffDist, currentElev);

  const progCoords: [number, number][] = [];
  for (const p of sampled) {
    if (p.dist <= cutoffDist) {
      progCoords.push(toXY(p.dist, p.elev));
    }
  }
  if (progCoords.length >= 1) {
    progCoords.push([cursorX, cursorY]);
  }

  return {
    fullPath: toLinePath(fullCoords),
    areaPath: toAreaPath(fullCoords),
    progPath: progCoords.length >= 2 ? toLinePath(progCoords) : "",
    progAreaPath: progCoords.length >= 2 ? toAreaPath(progCoords) : "",
    cursorX,
    cursorY,
    minElev: minE,
    maxElev: maxE,
    currentElev: evaluated.elevM,
    currentGradePct: evaluated.gradePct,
  };
}

function SvgFallbackLayer({
  stages,
  evaluated,
  spec,
  reachedPhotos = [],
  reachedClimbs = [],
  pinModeActive = false,
  onMapPinClick,
}: {
  stages: readonly TimelineStage[];
  evaluated: EvaluatedTimelineState;
  spec: RenderSpecV2;
  reachedPhotos?: readonly MatchedPhoto[];
  reachedClimbs?: readonly ClimbProgressMarker[];
  pinModeActive?: boolean;
  onMapPinClick?: (lngLat: [number, number]) => void;
}) {
  const [minLon, minLat, maxLon, maxLat] = evaluated.bounds;
  const lonSpan = Math.max(maxLon - minLon, 0.001);
  const latSpan = Math.max(maxLat - minLat, 0.001);

  const project = (lon: number, lat: number): [number, number] => {
    const x = 32 + ((lon - minLon) / lonSpan) * 336;
    const y = 195 - ((lat - minLat) / latSpan) * 165;
    return [x, y];
  };

  const toPolyline = (coords: [number, number][]) =>
    coords
      .map((c, idx) => {
        const [x, y] = project(c[0], c[1]);
        return `${idx === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
      })
      .join(" ");

  const [mx, my] = project(evaluated.markerPosition[0], evaluated.markerPosition[1]);
  const activeStage = evaluated.activeStage;
  const markerAccent = activeStage?.color ?? spec.track_color;

  function handleSvgClick(e: React.MouseEvent<SVGSVGElement>) {
    if (!pinModeActive || !onMapPinClick) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const vx = ((e.clientX - rect.left) / rect.width) * 400;
    const vy = ((e.clientY - rect.top) / rect.height) * 225;
    const lon = minLon + ((vx - 32) / 336) * lonSpan;
    const lat = minLat + ((195 - vy) / 165) * latSpan;
    onMapPinClick([lon, Math.min(89.9, Math.max(-89.9, lat))]);
  }

  return (
    <div className="absolute inset-0 bg-[#0b1117]">
      <div className="topo-grid-surface absolute inset-0" aria-hidden="true" />
      <svg
        viewBox="0 0 400 225"
        preserveAspectRatio="xMidYMid meet"
        onClick={handleSvgClick}
        className={`relative h-full w-full select-none ${pinModeActive ? "cursor-crosshair" : ""}`}
        role="img"
        aria-label="Route preview canvas"
      >
        {stages.length === 0 ? (
          <path
            d="M 68 175 C 122 145, 148 92, 206 105 C 254 116, 286 72, 332 69"
            fill="none"
            stroke="#38bdf8"
            strokeOpacity={0.22}
            strokeWidth={spec.track_width_px}
            strokeLinecap="round"
          />
        ) : null}

        {evaluated.visiblePaths.map((seg) => (
          <path
            key={seg.id}
            d={toPolyline(seg.path)}
            fill="none"
            stroke={`rgb(${seg.color[0]}, ${seg.color[1]}, ${seg.color[2]})`}
            strokeWidth={spec.track_width_px}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}

        {spec.show_transfer_arcs !== false &&
          (evaluated.transferArcs ?? [])
            .filter((arc) => arc.reached && arc.waypoints.length >= 2)
            .map((arc) => (
              <path
                key={arc.id}
                data-testid={`svg-transfer-arc-${arc.id}`}
                d={toPolyline(arc.waypoints)}
                fill="none"
                stroke="#f59e0b"
                strokeOpacity={0.75}
                strokeWidth={2}
                strokeDasharray="4 3"
              />
            ))}

        {evaluated.overnightStops
          .filter((s) => s.reached)
          .map((stop) => {
            const [sx, sy] = project(stop.position[0], stop.position[1]);
            return (
              <circle
                key={stop.stageId}
                cx={sx}
                cy={sy}
                r="5"
                fill="#141c24"
                stroke="#f59e0b"
                strokeWidth="2"
              />
            );
          })}

        {spec.show_climb_callouts !== false &&
          reachedClimbs.map((climb) => {
            const [cx, cy] = project(climb.summitPosition[0], climb.summitPosition[1]);
            return (
              <g key={climb.id} data-testid={`svg-climb-pin-${climb.id}`}>
                <circle cx={cx} cy={cy} r="5.5" fill="#f97316" stroke="#ffffff" strokeWidth="1.5" />
                <text
                  x={cx}
                  y={cy - 8}
                  textAnchor="middle"
                  fill="#fef3c7"
                  fontSize="7"
                  fontWeight="700"
                >
                  {climb.category}
                </text>
              </g>
            );
          })}

        {reachedPhotos
          .filter((p) => p.pinPosition !== null)
          .map((photo) => {
            const [px, py] = project(photo.pinPosition![0], photo.pinPosition![1]);
            return (
              <circle
                key={photo.id}
                data-testid={`svg-photo-pin-${photo.id}`}
                cx={px}
                cy={py}
                r="5"
                fill="#0f172a"
                stroke="#38bdf8"
                strokeWidth="2"
              />
            );
          })}

        {stages.length > 0 && activeStage ? (
          <g
            data-testid={`svg-activity-marker-${activeStage.activityType}`}
            transform={`translate(${mx}, ${my})`}
          >
            <circle cx={0} cy={0} r={14} fill="#0d1319" stroke={markerAccent} strokeWidth={2} />
            <g
              transform="translate(-9, -9) scale(0.75)"
              fill="none"
              stroke="#eef2f6"
              strokeWidth={1.75}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <ActivitySvgGroup
                type={activeStage.activityType}
                accentColor={markerAccent}
                duotoneOpacity={0.28}
              />
            </g>
          </g>
        ) : stages.length > 0 ? (
          <circle
            cx={mx}
            cy={my}
            r={Math.max(5, spec.track_width_px * 1.1)}
            fill={spec.marker_color}
            stroke="#0d1319"
            strokeWidth="2"
          />
        ) : null}
      </svg>
    </div>
  );
}

interface ProjectedStop {
  stop: StopMarker;
  x: number;
  y: number;
}

interface ProjectedPhoto {
  photo: MatchedPhoto;
  x: number;
  y: number;
}

interface FitCameraSnapshot {
  boundsKey: string;
  center: [number, number];
  zoom: number;
  pitch: number;
  bearing: number;
}

interface ProjectedPoint {
  x: number;
  y: number;
}

interface ProjectedCameraSnapshot {
  center: [number, number];
  zoom: number;
  pitch: number;
  bearing: number;
  padding?: ViewportPadding;
}

interface ManualCameraState {
  boundsKey: string;
  progressRatio: number;
  camera: ProjectedCameraSnapshot;
}

function readMapCameraSnapshot(map: MapLibreMap): ProjectedCameraSnapshot | null {
  const rawCenter =
    typeof map.getCenter === "function"
      ? (map.getCenter() as unknown as { lng?: number; lat?: number } | [number, number] | null)
      : null;
  const center: [number, number] | null = Array.isArray(rawCenter)
    ? rawCenter.length >= 2 && Number.isFinite(rawCenter[0]) && Number.isFinite(rawCenter[1])
      ? [rawCenter[0], rawCenter[1]]
      : null
    : rawCenter &&
        typeof rawCenter === "object" &&
        typeof rawCenter.lng === "number" &&
        typeof rawCenter.lat === "number" &&
        Number.isFinite(rawCenter.lng) &&
        Number.isFinite(rawCenter.lat)
      ? [rawCenter.lng, rawCenter.lat]
      : null;
  const zoom = typeof map.getZoom === "function" ? map.getZoom() : null;
  const pitch = typeof map.getPitch === "function" ? map.getPitch() : 0;
  const bearing = typeof map.getBearing === "function" ? map.getBearing() : 0;
  const rawPadding = typeof map.getPadding === "function" ? map.getPadding() : undefined;
  const padding: ViewportPadding | undefined =
    rawPadding &&
    typeof rawPadding === "object" &&
    Number.isFinite(rawPadding.top) &&
    Number.isFinite(rawPadding.bottom) &&
    Number.isFinite(rawPadding.left) &&
    Number.isFinite(rawPadding.right)
      ? {
          top: rawPadding.top ?? 0,
          bottom: rawPadding.bottom ?? 0,
          left: rawPadding.left ?? 0,
          right: rawPadding.right ?? 0,
        }
      : undefined;
  if (!center || typeof zoom !== "number" || !Number.isFinite(zoom)) {
    return null;
  }
  return {
    center,
    zoom,
    pitch: typeof pitch === "number" && Number.isFinite(pitch) ? pitch : 0,
    bearing: typeof bearing === "number" && Number.isFinite(bearing) ? bearing : 0,
    padding,
  };
}

async function preloadAndDecodeImages(urls: readonly string[]): Promise<void> {
  const uniqueUrls = Array.from(new Set(urls.filter((u) => u.length > 0)));
  if (
    uniqueUrls.length === 0 ||
    typeof Image === "undefined" ||
    (typeof navigator !== "undefined" && /jsdom/i.test(navigator.userAgent))
  ) {
    return;
  }
  await Promise.all(
    uniqueUrls.map(
      (src) =>
        new Promise<void>((resolve) => {
          const img = new Image();
          const timer = window.setTimeout(resolve, 3000);
          const finish = () => {
            window.clearTimeout(timer);
            resolve();
          };
          img.onload = () => {
            if (typeof img.decode === "function") {
              void img.decode().then(finish, finish);
            } else {
              finish();
            }
          };
          img.onerror = finish;
          img.src = src;
        }),
    ),
  );
}

export function TripStageCanvas({
  stages,
  spec,
  progressRatio,
  durationS,
  travelDurationS,
  isPreview = false,
  renderMode = "interactive",
  exactSize,
  jobId,
  controllerRef,
  geometryStatus,
  matchedPhotos = EMPTY_MATCHED_PHOTOS,
  activePhoto = null,
  climbs = EMPTY_CLIMBS,
  pinModeActive = false,
  onMapPinClick,
  onCancelPinMode,
  projectTitle,
  totalStats,
}: TripStageCanvasProps) {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const mapContainerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const overlayRef = useRef<MapboxOverlay | null>(null);
  const loadedStyleRef = useRef<string | null>(null);
  const mapReadyPromiseRef = useRef<Promise<void>>(Promise.resolve());
  const fitAppliedBoundsRef = useRef<string | null>(null);
  const suppressMoveEventRef = useRef(false);
  const latestViewRef = useRef({ boundsKey: "", progressRatio: 0 });
  const [webglAvailable] = useState<boolean>(() => isWebGlSupported());
  const [fitCamera, setFitCamera] = useState<FitCameraSnapshot | null>(null);
  const [manualCamera, setManualCamera] = useState<ManualCameraState | null>(null);
  const [measuredHudSize, setMeasuredHudSize] = useState<{ width: number; height: number } | null>(
    null,
  );
  const [observedSize, setObservedSize] = useState<{ width: number; height: number } | null>(null);

  const fallbackResolution = useMemo(() => parseResolutionPreset(spec.resolution), [spec.resolution]);
  const viewportSize = useMemo(() => {
    if (renderMode === "deterministic" && exactSize) {
      return exactSize;
    }
    if (observedSize && observedSize.width > 0 && observedSize.height > 0) {
      return observedSize;
    }
    const aspect = fallbackResolution.width / Math.max(1, fallbackResolution.height);
    const width = 960;
    const height = Math.round(width / aspect);
    return { width, height };
  }, [renderMode, exactSize, observedSize, fallbackResolution]);

  useEffect(() => {
    if (renderMode === "deterministic" && exactSize) {
      return;
    }
    const el = wrapperRef.current;
    if (!el || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(() => {
      if (el.clientWidth > 0 && el.clientHeight > 0) {
        setObservedSize({ width: el.clientWidth, height: el.clientHeight });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [renderMode, exactSize]);

  const evaluated = useMemo(
    () => evaluateTimeline(stages, spec, progressRatio, { durationS, isPreview }),
    [stages, spec, progressRatio, durationS, isPreview],
  );

  const mappedClimbs = useMemo(
    () => mapClimbsToTimelineProgress(climbs, stages, spec, evaluated.progressRatio),
    [climbs, stages, spec, evaluated.progressRatio],
  );

  const reachedClimbs = useMemo(() => {
    if (!spec.show_climb_callouts) return [];
    return mappedClimbs.filter(
      (c) => spec.visibility_mode === "full" || c.reached,
    );
  }, [spec.show_climb_callouts, spec.visibility_mode, mappedClimbs]);

  const hasActiveChapterCard = Boolean(
    spec.show_chapter_cards && evaluated.chapterCard && evaluated.chapterCard.opacity > 0.01,
  );
  const hasActiveFinalePoster = Boolean(
    spec.show_finale_poster && evaluated.progressRatio >= 0.999 && stages.length > 0,
  );

  const adaptiveFollow = useMemo(
    () =>
      spec.camera_mode === "follow-marker"
        ? computeAdaptiveFollowCamera(stages, spec, evaluated.progressRatio, {
            viewportWidth: viewportSize.width,
            viewportHeight: viewportSize.height,
            durationS,
            travelDurationS,
            isPreview,
            bounds: evaluated.bounds,
          })
        : null,
    [
      spec,
      stages,
      evaluated.progressRatio,
      evaluated.bounds,
      viewportSize.width,
      viewportSize.height,
      durationS,
      travelDurationS,
      isPreview,
    ],
  );

  const cameraFrame = useMemo(
    () =>
      computeCameraFrame(
        spec,
        evaluated,
        adaptiveFollow
          ? {
              courseBearingDeg: adaptiveFollow.courseBearingDeg,
              velocityMps: adaptiveFollow.velocityMps,
            }
          : undefined,
      ),
    [spec, evaluated, adaptiveFollow],
  );

  const keyframeZoomOffset = Number.isFinite(cameraFrame.zoomOffset) ? cameraFrame.zoomOffset! : 0;
  const effectiveFollowZoom = adaptiveFollow
    ? Math.min(18, Math.max(3, adaptiveFollow.zoom + keyframeZoomOffset))
    : null;

  const fitClimbTargets = useMemo(() => {
    if (
      spec.camera_mode !== "fit-track" ||
      spec.show_climb_callouts === false ||
      !fitCamera ||
      reachedClimbs.length === 0
    ) {
      return undefined;
    }
    const targets: { id: string; screenX: number; screenY: number }[] = [];
    for (const climb of reachedClimbs) {
      if (
        !Number.isFinite(climb.summitPosition[0]) ||
        !Number.isFinite(climb.summitPosition[1]) ||
        Math.abs(climb.summitPosition[1]) > 90
      ) {
        continue;
      }
      const scr = projectLngLatToScreen(
        climb.summitPosition,
        fitCamera.center,
        fitCamera.zoom,
        fitCamera.pitch,
        fitCamera.bearing,
        viewportSize.width,
        viewportSize.height,
      );
      if (scr.visible) {
        targets.push({ id: climb.id, screenX: scr.x, screenY: scr.y });
      }
    }
    return targets.length > 0 ? targets : undefined;
  }, [
    spec.camera_mode,
    spec.show_climb_callouts,
    fitCamera,
    reachedClimbs,
    viewportSize.width,
    viewportSize.height,
  ]);

  const layout = useMemo(
    () =>
      solveStageLayout({
        stages,
        spec,
        evaluated,
        viewportW: viewportSize.width,
        viewportH: viewportSize.height,
        compact: renderMode === "interactive",
        isPreview: isPreview || spec.is_preview,
        hasActivePhoto: Boolean(activePhoto && activePhoto.photo.src),
        hasActiveChapterCard,
        hasActiveFinalePoster,
        climbMarkers: reachedClimbs,
        climbCalloutTargets: fitClimbTargets,
        followCamera: adaptiveFollow
          ? {
              center: adaptiveFollow.center,
              zoom: effectiveFollowZoom ?? adaptiveFollow.zoom,
              pitch: cameraFrame.pitch,
              bearing: cameraFrame.bearing,
            }
          : undefined,
        travelDurationS,
        measuredHudUnscaled: renderMode === "interactive" ? measuredHudSize : null,
      }),
    [
      stages,
      spec,
      evaluated,
      viewportSize.width,
      viewportSize.height,
      renderMode,
      isPreview,
      activePhoto,
      hasActiveChapterCard,
      hasActiveFinalePoster,
      reachedClimbs,
      fitClimbTargets,
      adaptiveFollow,
      effectiveFollowZoom,
      cameraFrame.pitch,
      cameraFrame.bearing,
      travelDurationS,
      measuredHudSize,
    ],
  );

  const reachedPhotos = useMemo(() => {
    if (!spec.show_photos) return [];
    return matchedPhotos.filter(
      (p) =>
        p.matchedProgress !== null &&
        p.pinPosition !== null &&
        (spec.visibility_mode === "full" || p.matchedProgress <= evaluated.progressRatio + 1e-6),
    );
  }, [spec.show_photos, spec.visibility_mode, matchedPhotos, evaluated.progressRatio]);

  const padKey = `${layout.fitPadding.top},${layout.fitPadding.bottom},${layout.fitPadding.left},${layout.fitPadding.right}`;
  const boundsKey = `${evaluated.bounds.join(",")}|${spec.camera_mode}|${spec.camera_preset}|${cameraFrame.pitch.toFixed(2)}|${cameraFrame.bearing.toFixed(2)}|${keyframeZoomOffset.toFixed(2)}|${viewportSize.width}x${viewportSize.height}|${padKey}`;

  const fullCurtainData = useMemo(
    () =>
      spec.elevation_curtain && spec.visibility_mode === "full" && stages.length > 0
        ? buildElevationCurtainData(stages, spec.terrain_exaggeration)
        : null,
    [spec.elevation_curtain, spec.visibility_mode, stages, spec.terrain_exaggeration],
  );

  const activeManualCamera = useMemo<ProjectedCameraSnapshot | null>(() => {
    if (!manualCamera || manualCamera.boundsKey !== boundsKey) {
      return null;
    }
    if (
      spec.camera_mode === "follow-marker" &&
      manualCamera.progressRatio !== evaluated.progressRatio
    ) {
      return null;
    }
    return manualCamera.camera;
  }, [manualCamera, boundsKey, spec.camera_mode, evaluated.progressRatio]);

  const activeFitCamera = useMemo<ProjectedCameraSnapshot | null>(() => {
    if (spec.camera_mode !== "fit-track" || !fitCamera || fitCamera.boundsKey !== boundsKey) {
      return null;
    }
    return {
      center: fitCamera.center,
      zoom: fitCamera.zoom,
      pitch: fitCamera.pitch,
      bearing: fitCamera.bearing,
    };
  }, [spec.camera_mode, fitCamera, boundsKey]);

  const activeFollowCamera = useMemo<ProjectedCameraSnapshot | null>(() => {
    if (spec.camera_mode !== "follow-marker") {
      return null;
    }
    const center = adaptiveFollow?.center ?? evaluated.markerPosition;
    if (
      !Array.isArray(center) ||
      center.length < 2 ||
      !Number.isFinite(center[0]) ||
      !Number.isFinite(center[1]) ||
      Math.abs(center[1]) > 90
    ) {
      return null;
    }
    const zoom = effectiveFollowZoom ?? 11.5;
    if (!Number.isFinite(zoom)) {
      return null;
    }
    return {
      center,
      zoom,
      pitch: cameraFrame.pitch,
      bearing: cameraFrame.bearing,
      padding: layout.followPadding,
    };
  }, [
    spec.camera_mode,
    adaptiveFollow,
    evaluated.markerPosition,
    effectiveFollowZoom,
    cameraFrame.pitch,
    cameraFrame.bearing,
    layout.followPadding,
  ]);

  const projectionCamera = activeManualCamera ?? activeFollowCamera ?? activeFitCamera;

  const projectedStops = useMemo<ProjectedStop[]>(() => {
    if (!webglAvailable || !projectionCamera) return [];
    const reached = evaluated.overnightStops.filter(
      (s) =>
        s.reached &&
        Number.isFinite(s.position[0]) &&
        Number.isFinite(s.position[1]) &&
        Math.abs(s.position[1]) <= 90,
    );
    if (reached.length === 0) return [];
    const result: ProjectedStop[] = [];
    for (const stop of reached) {
      const scr = projectLngLatToScreen(
        stop.position,
        projectionCamera.center,
        projectionCamera.zoom,
        projectionCamera.pitch,
        projectionCamera.bearing,
        viewportSize.width,
        viewportSize.height,
        projectionCamera.padding,
      );
      if (scr.visible) {
        result.push({ stop, x: scr.x, y: scr.y });
      }
    }
    return result;
  }, [
    webglAvailable,
    projectionCamera,
    evaluated.overnightStops,
    viewportSize.width,
    viewportSize.height,
  ]);

  const projectedPhotos = useMemo<ProjectedPhoto[]>(() => {
    if (!webglAvailable || !projectionCamera || reachedPhotos.length === 0) return [];
    const result: ProjectedPhoto[] = [];
    for (const photo of reachedPhotos) {
      if (
        photo.pinPosition === null ||
        !Number.isFinite(photo.pinPosition[0]) ||
        !Number.isFinite(photo.pinPosition[1]) ||
        Math.abs(photo.pinPosition[1]) > 90
      ) {
        continue;
      }
      const scr = projectLngLatToScreen(
        photo.pinPosition,
        projectionCamera.center,
        projectionCamera.zoom,
        projectionCamera.pitch,
        projectionCamera.bearing,
        viewportSize.width,
        viewportSize.height,
        projectionCamera.padding,
      );
      if (scr.visible) {
        result.push({ photo, x: scr.x, y: scr.y });
      }
    }
    return result;
  }, [
    webglAvailable,
    projectionCamera,
    reachedPhotos,
    viewportSize.width,
    viewportSize.height,
  ]);

  const projectedMarker = useMemo<ProjectedPoint | null>(() => {
    if (
      !webglAvailable ||
      !projectionCamera ||
      !evaluated.activeStage ||
      !Number.isFinite(evaluated.markerPosition[0]) ||
      !Number.isFinite(evaluated.markerPosition[1]) ||
      Math.abs(evaluated.markerPosition[1]) > 90
    ) {
      return null;
    }
    const scr = projectLngLatToScreen(
      evaluated.markerPosition,
      projectionCamera.center,
      projectionCamera.zoom,
      projectionCamera.pitch,
      projectionCamera.bearing,
      viewportSize.width,
      viewportSize.height,
      projectionCamera.padding,
    );
    return scr.visible ? { x: scr.x, y: scr.y } : null;
  }, [
    webglAvailable,
    projectionCamera,
    evaluated.activeStage,
    evaluated.markerPosition,
    viewportSize.width,
    viewportSize.height,
  ]);

  const is3dActive = is3dCameraActive(spec);

  // Initialize MapLibre + Deck.gl when WebGL is available (layout phase so camera/overlay commit runs on mount)
  useLayoutEffect(() => {
    const container = mapContainerRef.current;
    if (!container || !webglAvailable) {
      mapReadyPromiseRef.current = Promise.resolve();
      return;
    }

    let cancelled = false;
    const map = new maplibregl.Map(
      buildMapLibreInitOptions(container, spec.map_style, {
        pitch: cameraFrame.pitch,
        bearing: cameraFrame.bearing,
        interactive: renderMode === "interactive",
        deferStyle: true,
      }),
    );
    map.setStyle(resolveMapStyleUrl(spec.map_style), {
      transformStyle: (_prev, next) => sanitizeMapStyle(next, spec.map_style),
    });
    const overlay = new MapboxOverlay({
      interleaved: true,
      layers: [],
    });
    map.addControl(overlay as unknown as maplibregl.IControl);
    mapRef.current = map;
    overlayRef.current = overlay;
    loadedStyleRef.current = spec.map_style;
    fitAppliedBoundsRef.current = null;

    const handleManualMove = () => {
      if (suppressMoveEventRef.current) {
        return;
      }
      const snapshot = readMapCameraSnapshot(map);
      if (!snapshot) {
        return;
      }
      setManualCamera({
        boundsKey: latestViewRef.current.boundsKey,
        progressRatio: latestViewRef.current.progressRatio,
        camera: snapshot,
      });
    };
    map.on("move", handleManualMove);

    map.on("style.load", () => {
      sync3dBuildingsAndSky(map, is3dActive, spec.terrain_exaggeration, spec.map_style);
    });

    mapReadyPromiseRef.current = new Promise<void>((resolve) => {
      const done = () => {
        if (!cancelled) {
          resolve();
        }
      };
      const timer = window.setTimeout(done, 8000);
      map.once("load", () => {
        sync3dBuildingsAndSky(map, is3dActive, spec.terrain_exaggeration, spec.map_style);
        map.once("idle", () => {
          window.clearTimeout(timer);
          done();
        });
        window.setTimeout(() => {
          window.clearTimeout(timer);
          done();
        }, 2500);
      });
      map.on("error", () => {
        // Avoid hanging forever if external tile host is blocked/offline
        window.clearTimeout(timer);
        window.setTimeout(done, 150);
      });
    });

    return () => {
      cancelled = true;
      fitAppliedBoundsRef.current = null;
      map.off("move", handleManualMove);
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
    // Only re-create map instance if container/webgl/renderMode changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [webglAvailable, renderMode]);

  // Switch OpenFreeMap style dynamically without recreating map instance
  useEffect(() => {
    const map = mapRef.current;
    if (!map || loadedStyleRef.current === spec.map_style) {
      return;
    }
    loadedStyleRef.current = spec.map_style;
    map.setStyle(resolveMapStyleUrl(spec.map_style), {
      transformStyle: (_prev, next) => sanitizeMapStyle(next, spec.map_style),
    });
  }, [spec.map_style]);

  // Sync 3D building extrusions, DEM terrain exaggeration, & horizon sky when 3D mode changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    sync3dBuildingsAndSky(map, is3dActive, spec.terrain_exaggeration, spec.map_style);
  }, [is3dActive, spec.terrain_exaggeration, spec.map_style]);

  // Interactive click-to-pin handler on MapLibre
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !pinModeActive || !onMapPinClick) {
      return;
    }
    const handleClick = (e: maplibregl.MapMouseEvent) => {
      onMapPinClick([e.lngLat.lng, e.lngLat.lat]);
    };
    map.on("click", handleClick);
    return () => {
      map.off("click", handleClick);
    };
  }, [pinModeActive, onMapPinClick, webglAvailable]);

  // Update Deck.gl layers and MapLibre camera synchronously in commit phase
  useLayoutEffect(() => {
    const map = mapRef.current;
    const overlay = overlayRef.current;
    if (!map || !overlay) {
      return;
    }

    latestViewRef.current = {
      boundsKey,
      progressRatio: evaluated.progressRatio,
    };

    overlay.setProps(
      buildDeckOverlayProps(evaluated.visiblePaths, evaluated.markerPosition, {
        trackWidthPx: spec.track_width_px,
        markerColorHex: spec.marker_color,
        stops: evaluated.overnightStops,
        photoPins: reachedPhotos.map((p) => ({
          id: p.id,
          position: p.pinPosition!,
        })),
        colorBy: spec.color_by,
        elevationCurtain: spec.elevation_curtain,
        terrainExaggeration: spec.terrain_exaggeration,
        elevationStages: stages,
        elevationCurtainData: fullCurtainData,
        maxDistanceM:
          spec.visibility_mode === "full" ? undefined : evaluated.distanceTravelledM,
        transferArcs: evaluated.transferArcs,
        showTransferArcs: spec.show_transfer_arcs,
        climbPins: reachedClimbs,
        showClimbCallouts: spec.show_climb_callouts,
      }),
    );

    const hasValidBounds = evaluated.bounds.every(Number.isFinite);
    const targetCenter = adaptiveFollow?.center ?? evaluated.markerPosition;
    const hasValidMarker =
      Array.isArray(targetCenter) &&
      targetCenter.length >= 2 &&
      Number.isFinite(targetCenter[0]) &&
      Number.isFinite(targetCenter[1]) &&
      Math.abs(targetCenter[1]) <= 90;

    try {
      suppressMoveEventRef.current = true;
      if (spec.camera_mode === "fit-track") {
        if (hasValidBounds && fitAppliedBoundsRef.current !== boundsKey) {
          fitAppliedBoundsRef.current = boundsKey;
          const bounds: [[number, number], [number, number]] = [
            [evaluated.bounds[0], evaluated.bounds[1]],
            [evaluated.bounds[2], evaluated.bounds[3]],
          ];
          const zeroPadding = { top: 0, bottom: 0, left: 0, right: 0 };
          const mapEl = typeof map.getContainer === "function" ? map.getContainer() : null;
          const canvasW = Math.max(120, mapEl?.clientWidth || viewportSize.width);
          const canvasH = Math.max(120, mapEl?.clientHeight || viewportSize.height);
          const clampedFitPad = clampFitTrackPadding(layout.fitPadding, canvasW, canvasH);

          // Clear any persistent follow-marker padding & pitch before cameraForBounds,
          // because MapLibre adds map.getPadding() + options.padding when fitting bounds.
          if (typeof map.setPadding === "function") {
            map.setPadding(zeroPadding);
          }
          map.jumpTo({
            bearing: cameraFrame.bearing,
            pitch: 0,
            padding: zeroPadding,
          });

          const fitted =
            typeof map.cameraForBounds === "function"
              ? map.cameraForBounds(bounds, {
                  padding: clampedFitPad,
                  bearing: cameraFrame.bearing,
                })
              : undefined;
          let fallbackCenter: [number, number] | null = null;
          let fallbackZoom: number | null = null;
          const fittedZoom = fitted?.zoom;
          if (fitted && fitted.center && typeof fittedZoom === "number" && Number.isFinite(fittedZoom)) {
            const targetZoom =
              keyframeZoomOffset !== 0
                ? Math.min(18, Math.max(2, fittedZoom + keyframeZoomOffset))
                : fittedZoom;
            map.jumpTo({
              center: fitted.center,
              zoom: targetZoom,
              bearing: cameraFrame.bearing,
              pitch: cameraFrame.pitch,
              padding: zeroPadding,
            });
            const fc = fitted.center as { lng?: number; lat?: number } | [number, number];
            if (Array.isArray(fc) && fc.length >= 2) {
              fallbackCenter = [fc[0], fc[1]];
            } else if (fc && typeof fc === "object" && "lng" in fc && "lat" in fc) {
              fallbackCenter = [Number(fc.lng), Number(fc.lat)];
            }
            fallbackZoom = targetZoom;
          } else {
            map.fitBounds(bounds, {
              padding: 24,
              duration: 0,
              pitch: 0,
              bearing: cameraFrame.bearing,
            });
            if (cameraFrame.pitch > 0 && typeof map.setPitch === "function") {
              map.setPitch(cameraFrame.pitch);
            }
          }

          const rawCenter =
            typeof map.getCenter === "function"
              ? (map.getCenter() as unknown as { lng?: number; lat?: number } | [number, number] | null)
              : null;
          const mapCenter: [number, number] | null = Array.isArray(rawCenter)
            ? rawCenter.length >= 2 && Number.isFinite(rawCenter[0]) && Number.isFinite(rawCenter[1])
              ? [rawCenter[0], rawCenter[1]]
              : null
            : rawCenter &&
                typeof rawCenter === "object" &&
                typeof rawCenter.lng === "number" &&
                typeof rawCenter.lat === "number" &&
                Number.isFinite(rawCenter.lng) &&
                Number.isFinite(rawCenter.lat)
              ? [rawCenter.lng, rawCenter.lat]
              : null;
          const mapZoom = typeof map.getZoom === "function" ? map.getZoom() : null;
          const mapPitch = typeof map.getPitch === "function" ? map.getPitch() : null;
          const mapBearing = typeof map.getBearing === "function" ? map.getBearing() : null;
          const resolvedCenter = mapCenter ?? fallbackCenter;
          const resolvedZoom =
            typeof mapZoom === "number" && Number.isFinite(mapZoom) ? mapZoom : fallbackZoom;
          if (resolvedCenter && resolvedZoom !== null && Number.isFinite(resolvedZoom)) {
            setFitCamera({
              boundsKey,
              center: resolvedCenter,
              zoom: resolvedZoom,
              pitch:
                typeof mapPitch === "number" && Number.isFinite(mapPitch)
                  ? mapPitch
                  : cameraFrame.pitch,
              bearing:
                typeof mapBearing === "number" && Number.isFinite(mapBearing)
                  ? mapBearing
                  : cameraFrame.bearing,
            });
          }
        }
      } else {
        fitAppliedBoundsRef.current = null;
        setFitCamera((prev) => (prev !== null ? null : prev));
        const followZoom = effectiveFollowZoom ?? 11.5;
        if (hasValidMarker && Number.isFinite(followZoom)) {
          map.jumpTo({
            center: targetCenter,
            zoom: followZoom,
            pitch: cameraFrame.pitch,
            bearing: cameraFrame.bearing,
            padding: layout.followPadding,
          });
        }
      }
    } finally {
      suppressMoveEventRef.current = false;
    }

    map.triggerRepaint();
  }, [
    boundsKey,
    evaluated,
    reachedPhotos,
    reachedClimbs,
    stages,
    fullCurtainData,
    adaptiveFollow,
    effectiveFollowZoom,
    keyframeZoomOffset,
    layout.fitPadding,
    layout.followPadding,
    spec.camera_mode,
    spec.track_width_px,
    spec.marker_color,
    spec.color_by,
    spec.elevation_curtain,
    spec.terrain_exaggeration,
    spec.visibility_mode,
    spec.show_transfer_arcs,
    spec.show_climb_callouts,
    cameraFrame.pitch,
    cameraFrame.bearing,
    viewportSize.width,
    viewportSize.height,
  ]);

  // Resize MapLibre whenever container or exactSize changes
  useEffect(() => {
    const map = mapRef.current;
    const container = mapContainerRef.current;
    if (!map || !container) {
      return;
    }
    if (typeof ResizeObserver === "undefined") {
      map.resize();
      return;
    }
    const observer = new ResizeObserver(() => {
      map.resize();
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [webglAvailable, exactSize]);

  const photoUrlsKey = useMemo(
    () => matchedPhotos.map((p) => p.src).filter(Boolean),
    [matchedPhotos],
  );

  // Expose deterministic settlement controller for window.__TRIP_RENDERER__
  useImperativeHandle(
    controllerRef,
    () => ({
      waitForReady: async () => {
        await Promise.all([
          mapReadyPromiseRef.current,
          preloadAndDecodeImages(photoUrlsKey),
        ]);
        await awaitNextAnimationFrames(2);
      },
      waitForFrameSettle: async (cameraChanged = true) => {
        const map = mapRef.current;
        if (!map) {
          await awaitNextAnimationFrames(1);
          return;
        }
        // Await 1 rAF so useLayoutEffect's jumpTo/fitBounds commits and MapLibre's _render() updates _inViewTiles
        await awaitNextAnimationFrames(1);
        const tilesCached =
          typeof map.loaded === "function" &&
          typeof map.areTilesLoaded === "function" &&
          map.loaded() &&
          map.areTilesLoaded();
        if (tilesCached) {
          return;
        }
        map.triggerRepaint();
        if (
          cameraChanged ||
          spec.camera_mode === "follow-marker" ||
          spec.camera_preset !== "flat" ||
          (spec.camera_keyframes?.length ?? 0) > 0
        ) {
          await new Promise<void>((resolve) => {
            const timeout = window.setTimeout(resolve, 1200);
            map.once("idle", () => {
              window.clearTimeout(timeout);
              resolve();
            });
          });
        }
        await awaitNextAnimationFrames(1);
      },
    }),
    [spec.camera_mode, spec.camera_preset, spec.camera_keyframes?.length, photoUrlsKey],
  );

  const miniElevation = useMemo(
    () =>
      spec.elevation_chart_overlay
        ? buildMiniElevationPath(stages, evaluated)
        : null,
    [spec.elevation_chart_overlay, stages, evaluated],
  );

  const wrapperClass =
    renderMode === "deterministic" && exactSize
      ? "relative overflow-hidden bg-[#0b1117]"
      : `relative w-full overflow-hidden rounded-lg border border-border bg-[#0b1117] shadow-2xl ${ASPECT_RATIO_CLASS[spec.resolution]}`;

  const wrapperStyle =
    renderMode === "deterministic" && exactSize
      ? { width: `${exactSize.width}px`, height: `${exactSize.height}px` }
      : undefined;

  return (
    <div
      ref={wrapperRef}
      data-testid={renderMode === "deterministic" ? "deterministic-map-frame" : "editor-map-frame"}
      data-map-style={spec.map_style}
      data-camera-mode={spec.camera_mode}
      data-camera-preset={spec.camera_preset}
      data-visibility-mode={spec.visibility_mode}
      data-resolution={spec.resolution}
      data-follow-zoom={effectiveFollowZoom !== null ? effectiveFollowZoom.toFixed(3) : undefined}
      data-hud-slot={layout.hud?.slot ?? spec.statistics_panel.position}
      data-job-id={jobId}
      className={wrapperClass}
      style={wrapperStyle}
    >
      {webglAvailable ? (
        <div
          ref={mapContainerRef}
          className={`absolute inset-0 h-full w-full ${pinModeActive ? "cursor-crosshair" : ""}`}
        />
      ) : (
        <SvgFallbackLayer
          stages={stages}
          evaluated={evaluated}
          spec={spec}
          reachedPhotos={reachedPhotos}
          reachedClimbs={reachedClimbs}
          pinModeActive={pinModeActive}
          onMapPinClick={onMapPinClick}
        />
      )}

      {/* Interactive Pin Mode Banner */}
      {pinModeActive ? (
        <div
          data-testid="pin-mode-banner"
          className="absolute top-3 left-1/2 z-40 flex -translate-x-1/2 items-center gap-2 rounded-full border border-amber-accent bg-canvas/95 px-3.5 py-1.5 text-xs font-medium text-ink shadow-xl backdrop-blur-md"
        >
          <MapPin className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
          <span>Click anywhere on the map or route to snap photo pin</span>
          {onCancelPinMode ? (
            <button
              type="button"
              onClick={onCancelPinMode}
              aria-label="Cancel pin mode"
              className="rounded-full p-0.5 text-ink-muted hover:bg-elevated hover:text-ink"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          ) : null}
        </div>
      ) : null}

      {/* Projected crisp icons for overnight stops, climb summits, photo pins, and active stage activity marker */}
      {webglAvailable &&
      (projectedStops.length > 0 ||
        layout.climbCallouts.length > 0 ||
        projectedPhotos.length > 0 ||
        (projectedMarker && evaluated.activeStage)) ? (
        <div className="pointer-events-none absolute inset-0 z-10 overflow-hidden">
          {projectedStops.map(({ stop, x, y }) => (
            <div
              key={stop.stageId}
              data-testid={`stop-marker-${stop.stageId}`}
              className="absolute flex h-6 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-amber-accent bg-canvas/90 text-amber-accent shadow-md"
              style={{ left: `${x}px`, top: `${y}px` }}
              title={`Day ${stop.dayIndex}: ${stop.name}`}
            >
              <TransitionGlyph icon={stop.transition} />
            </div>
          ))}

          {spec.show_climb_callouts !== false && layout.climbCallouts.length > 0
            ? (() => {
                const climbById = new Map(reachedClimbs.map((c) => [c.id, c]));
                const renderedSlots = layout.climbCallouts.filter((slot) => climbById.has(slot.id));
                if (renderedSlots.length === 0) return null;
                return (
                  <>
                    <svg
                      data-testid="climb-callout-leaders"
                      className="pointer-events-none absolute inset-0 h-full w-full"
                      aria-hidden="true"
                    >
                      {renderedSlots.map((slot) => {
                        const edgeX = Math.min(
                          slot.rect.x + slot.rect.width,
                          Math.max(slot.rect.x, slot.anchorX),
                        );
                        const edgeY = Math.min(
                          slot.rect.y + slot.rect.height,
                          Math.max(slot.rect.y, slot.anchorY),
                        );
                        return (
                          <line
                            key={slot.id}
                            data-testid={`climb-leader-${slot.id}`}
                            x1={slot.anchorX}
                            y1={slot.anchorY}
                            x2={edgeX}
                            y2={edgeY}
                            stroke="rgba(251, 146, 60, 0.85)"
                            strokeWidth="1.5"
                          />
                        );
                      })}
                    </svg>
                    {renderedSlots.map((slot) => {
                      const climb = climbById.get(slot.id)!;
                      return (
                        <div
                          key={slot.id}
                          data-testid={`climb-callout-${slot.id}`}
                          className="absolute flex items-center"
                          style={{
                            left: `${slot.rect.x}px`,
                            top: `${slot.rect.y}px`,
                            width: `${slot.rect.width}px`,
                            height: `${slot.rect.height}px`,
                          }}
                        >
                          <div className="inline-flex w-full items-center gap-1 rounded border border-orange-400/60 bg-canvas/92 px-1.5 py-0.5 text-[9px] font-semibold text-ink shadow-md backdrop-blur-sm">
                            <Mountain
                              className="h-2.5 w-2.5 shrink-0 text-orange-400"
                              aria-hidden="true"
                            />
                            <span className="shrink-0 text-orange-300">{climb.category}</span>
                            <span className="min-w-0 flex-1 truncate">{climb.name}</span>
                            <span className="tabular-telemetry shrink-0 text-ink-muted">
                              {formatElevation(climb.summitElevM, spec.units)}
                            </span>
                          </div>
                        </div>
                      );
                    })}
                  </>
                );
              })()
            : null}

          {projectedPhotos.map(({ photo, x, y }) => (
            <div
              key={photo.id}
              data-testid={`photo-pin-${photo.id}`}
              className="absolute flex h-6 w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-sky-accent bg-canvas/90 text-sky-accent shadow-md"
              style={{ left: `${x}px`, top: `${y}px` }}
              title={photo.caption ?? "Photo stop"}
            >
              <Camera className="h-3 w-3" aria-hidden="true" />
            </div>
          ))}

          {projectedMarker && evaluated.activeStage ? (
            <div
              className="absolute -translate-x-1/2 -translate-y-1/2"
              style={{ left: `${projectedMarker.x}px`, top: `${projectedMarker.y}px` }}
            >
              <ActivityMarkerBadge
                type={evaluated.activeStage.activityType}
                colorHex={evaluated.activeStage.color || spec.track_color}
                sizePx={renderMode === "deterministic" ? 32 : 28}
                pulse={renderMode === "interactive"}
                title={evaluated.activeStage.name}
              />
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Incremental Geometry Loading / Error UX */}
      {geometryStatus && (geometryStatus.pendingCount > 0 || geometryStatus.errorCount > 0) ? (
        <div className="absolute top-3 left-3 z-30 flex flex-col gap-1.5">
          {geometryStatus.pendingCount > 0 ? (
            <div
              data-testid="geometry-loading-pill"
              className="inline-flex items-center gap-1.5 rounded-md border border-sky-accent/40 bg-canvas/85 px-2.5 py-1 text-[11px] font-medium text-ink shadow-md backdrop-blur-sm"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-accent" aria-hidden="true" />
              <span>
                Loading {geometryStatus.pendingCount} stage{geometryStatus.pendingCount > 1 ? "s" : ""}…
              </span>
            </div>
          ) : null}

          {geometryStatus.errorCount > 0 ? (
            <div
              role="alert"
              data-testid="geometry-error-banner"
              className="inline-flex items-center gap-2 rounded-md border border-coral-accent/50 bg-canvas/90 px-2.5 py-1.5 text-[11px] text-ink shadow-md backdrop-blur-sm"
            >
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-coral-accent" aria-hidden="true" />
              <span>
                {geometryStatus.errorCount} stage geometry failed to load.
              </span>
              {geometryStatus.onRetryFailed ? (
                <button
                  type="button"
                  onClick={geometryStatus.onRetryFailed}
                  className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] font-semibold text-sky-accent hover:border-sky-accent"
                >
                  <RefreshCw className="h-3 w-3" aria-hidden="true" />
                  <span>Retry</span>
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Crisp HTML/SVG Statistics HUD */}
      <StatisticsHud
        config={spec.statistics_panel}
        units={spec.units}
        state={evaluated}
        compact={renderMode === "interactive"}
        layoutPlacement={layout.hud}
        onMeasuredUnscaledSize={renderMode === "interactive" ? setMeasuredHudSize : undefined}
      />

      {/* Burned-in / Interactive Elevation Profile Overlay */}
      {spec.elevation_chart_overlay && miniElevation && layout.elevation ? (
        <div
          data-testid="canvas-elevation-overlay"
          className="pointer-events-none absolute z-20 rounded-lg border border-white/15 bg-canvas/85 px-3 py-2 shadow-lg backdrop-blur-md"
          style={{
            left: `${layout.elevation.rect.x}px`,
            top: `${layout.elevation.rect.y}px`,
          }}
        >
          <div className="mb-1 flex items-center justify-between gap-3 text-[10px] leading-none">
            <span className="tabular-telemetry font-semibold text-teal-accent">
              {formatElevation(miniElevation.currentElev, spec.units)}
            </span>
            <div className="tabular-telemetry flex items-center gap-2 text-ink-muted">
              {miniElevation.currentGradePct !== null ? (
                <span className="rounded bg-elevated/90 px-1 py-0.5 text-[9px] font-semibold text-amber-accent">
                  {miniElevation.currentGradePct >= 0 ? "+" : ""}
                  {miniElevation.currentGradePct.toFixed(1)}%
                </span>
              ) : null}
              <span>
                {formatElevation(miniElevation.minElev, spec.units)}–{formatElevation(miniElevation.maxElev, spec.units)}
              </span>
            </div>
          </div>

          <svg viewBox={`0 0 ${ELEV_W} ${ELEV_H}`} className="h-12 w-52" aria-hidden="true">
            <defs>
              <linearGradient id="elev-prog-grad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#14b8a6" stopOpacity="0.45" />
                <stop offset="100%" stopColor="#14b8a6" stopOpacity="0.03" />
              </linearGradient>
            </defs>
            {miniElevation.areaPath ? (
              <path d={miniElevation.areaPath} fill="rgba(143, 160, 178, 0.1)" />
            ) : null}
            <path
              d={miniElevation.fullPath}
              fill="none"
              stroke="rgba(143, 160, 178, 0.35)"
              strokeWidth="1.5"
            />
            {miniElevation.progAreaPath ? (
              <path d={miniElevation.progAreaPath} fill="url(#elev-prog-grad)" />
            ) : null}
            {miniElevation.progPath ? (
              <path d={miniElevation.progPath} fill="none" stroke="#14b8a6" strokeWidth="2.2" />
            ) : null}
            <circle
              cx={miniElevation.cursorX}
              cy={miniElevation.cursorY}
              r="5.5"
              fill="#f59e0b"
              fillOpacity="0.28"
            />
            <circle
              cx={miniElevation.cursorX}
              cy={miniElevation.cursorY}
              r="3"
              fill="#f59e0b"
              stroke="#0d1319"
              strokeWidth="1.2"
            />
          </svg>
        </div>
      ) : null}

      {/* Color-by Micro-Legend */}
      {spec.color_by !== "stage" &&
      evaluated.colorLegendStops &&
      evaluated.colorLegendStops.length > 0 &&
      layout.colorLegend ? (
        <div
          data-testid="color-by-legend"
          className="pointer-events-none absolute z-20 flex flex-wrap items-center gap-1.5 rounded-md border border-white/15 bg-canvas/85 px-2.5 py-1 text-[10px] text-ink shadow-md backdrop-blur-md"
          style={{
            left: `${layout.colorLegend.rect.x}px`,
            top: `${layout.colorLegend.rect.y}px`,
            maxWidth: `${layout.colorLegend.rect.width}px`,
          }}
        >
          <span className="font-semibold uppercase tracking-wide text-ink-muted">
            {spec.color_by}
          </span>
          <div className="flex flex-wrap items-center gap-1">
            {evaluated.colorLegendStops.map((stop, idx) => (
              <span key={`${stop.label}-${idx}`} className="inline-flex items-center gap-1">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: stop.color }}
                  aria-hidden="true"
                />
                <span className="tabular-telemetry text-[9px] text-ink-muted">{stop.label}</span>
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {/* Non-colliding Active Photo Stop Card (corner | polaroid + leader | cinematic Ken Burns) */}
      {activePhoto && layout.photoCard && activePhoto.opacity > 0.01 && activePhoto.photo.src ? (() => {
        const cardStyle = spec.photo_card_style ?? "corner";
        const cardRect = layout.photoCard.rect;
        const activePinProj = projectedPhotos.find((p) => p.photo.id === activePhoto.photo.id);
        const cardCenterX = cardRect.x + cardRect.width / 2;
        const cardCenterY = cardRect.y + cardRect.height / 2;
        const imgScale =
          cardStyle === "cinematic" ? activePhoto.kenBurnsScale : 1;

        return (
          <>
            {cardStyle === "polaroid" && activePinProj ? (
              <svg
                className="pointer-events-none absolute inset-0 z-10 h-full w-full"
                aria-hidden="true"
              >
                <line
                  x1={activePinProj.x}
                  y1={activePinProj.y}
                  x2={cardCenterX}
                  y2={cardCenterY}
                  stroke="rgba(248, 250, 252, 0.85)"
                  strokeWidth="1.5"
                  strokeDasharray="4 3"
                />
              </svg>
            ) : null}
            <figure
              data-testid="photo-stop-card"
              data-photo-id={activePhoto.photo.id}
              data-photo-corner={layout.photoCard.corner}
              data-photo-style={cardStyle}
              className={`pointer-events-none absolute z-20 overflow-hidden shadow-2xl ${
                cardStyle === "polaroid"
                  ? "rounded-sm border border-slate-200 bg-[#f8fafc] p-2 pb-3 text-slate-900"
                  : cardStyle === "cinematic"
                    ? "rounded-xl border border-white/25 bg-black/95 text-ink"
                    : "rounded-lg border border-white/20 bg-canvas/90 text-ink backdrop-blur-md"
              }`}
              style={{
                left: `${cardRect.x}px`,
                top: `${cardRect.y}px`,
                width: `${cardRect.width}px`,
                opacity: activePhoto.opacity,
                transform: `scale(${activePhoto.scale})${cardStyle === "polaroid" ? " rotate(-1.5deg)" : ""}`,
                transformOrigin: "center center",
              }}
            >
              <div className="overflow-hidden">
                <img
                  src={activePhoto.photo.src}
                  alt={activePhoto.photo.caption ?? "Trip photo stop"}
                  className="w-full object-cover"
                  style={{
                    height: `${Math.max(88, cardRect.height - (activePhoto.photo.takenAtUtc ? 52 : 38))}px`,
                    transform: imgScale !== 1 ? `scale(${imgScale.toFixed(4)})` : undefined,
                    transformOrigin: "center center",
                  }}
                />
              </div>
              <figcaption
                className={`flex flex-col gap-0.5 px-2.5 py-1.5 text-[10px] leading-snug ${
                  cardStyle === "polaroid" ? "text-slate-900" : "text-ink"
                }`}
              >
                <span className="line-clamp-2 font-medium break-words">
                  {activePhoto.photo.caption || "Photo stop"}
                </span>
                {activePhoto.photo.takenAtUtc ? (
                  <span
                    className={`tabular-telemetry text-[9px] ${
                      cardStyle === "polaroid" ? "text-slate-500" : "text-ink-muted"
                    }`}
                  >
                    {formatDateInTimeZone(activePhoto.photo.takenAtUtc, spec.statistics_panel.time_zone)}
                  </span>
                ) : null}
              </figcaption>
            </figure>
          </>
        );
      })() : null}

      {/* Stage Chapter Card (lower-third slate on stage/day entry) */}
      {hasActiveChapterCard && evaluated.chapterCard && layout.chapterCard ? (
        <div
          data-testid="chapter-card"
          className="pointer-events-none absolute z-20 rounded-lg border border-white/20 bg-canvas/90 px-3.5 py-2.5 shadow-2xl backdrop-blur-md"
          style={{
            left: `${layout.chapterCard.rect.x}px`,
            top: `${layout.chapterCard.rect.y}px`,
            width: `${layout.chapterCard.rect.width}px`,
            opacity: evaluated.chapterCard.opacity,
          }}
        >
          <div className="flex items-center justify-between gap-2 text-[10px] font-semibold uppercase tracking-wider text-amber-accent">
            <span className="inline-flex items-center gap-1.5">
              <ActivityIcon
                type={evaluated.chapterCard.activityType}
                size={13}
                accentColor="#f59e0b"
              />
              <span>Day {evaluated.chapterCard.dayIndex}</span>
            </span>
            <span className="tabular-telemetry text-ink-muted">
              {formatDistance(evaluated.chapterCard.distanceM, spec.units)} • +
              {formatElevation(evaluated.chapterCard.elevationGainM, spec.units)}
            </span>
          </div>
          <div className="mt-1 truncate text-sm font-semibold text-ink">
            {evaluated.chapterCard.title}
          </div>
        </div>
      ) : null}

      {/* Trip Finale Poster Card */}
      {hasActiveFinalePoster && layout.finalePoster ? (() => {
        const summaryDistance =
          totalStats?.distanceM ??
          stages.reduce((acc, s) => acc + s.distanceM, 0);
        const summaryElev =
          totalStats?.elevationGainM ??
          stages.reduce((acc, s) => acc + s.elevationGainM, 0);
        const summaryMoving =
          totalStats?.movingSeconds ??
          stages.reduce((acc, s) => acc + s.movingSeconds, 0);
        const summaryStages = totalStats?.stageCount ?? stages.length;

        return (
          <div
            data-testid="finale-poster-card"
            className="pointer-events-none absolute z-30 flex flex-col justify-between rounded-xl border border-white/25 bg-canvas/92 p-4 shadow-2xl backdrop-blur-md"
            style={{
              left: `${layout.finalePoster.rect.x}px`,
              top: `${layout.finalePoster.rect.y}px`,
              width: `${layout.finalePoster.rect.width}px`,
              minHeight: `${layout.finalePoster.rect.height}px`,
            }}
          >
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-widest text-amber-accent">
                Expedition Complete
              </div>
              <h2 className="mt-1 truncate text-base font-bold text-ink">
                {projectTitle || "Trip Summary"}
              </h2>
            </div>
            <div className="tabular-telemetry mt-3 grid grid-cols-2 gap-3 border-t border-white/15 pt-3 text-xs">
              <div>
                <div className="text-[10px] text-ink-muted">Distance</div>
                <div className="font-semibold text-ink">
                  {formatDistance(summaryDistance, spec.units)}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-ink-muted">Elevation Gain</div>
                <div className="font-semibold text-teal-accent">
                  +{formatElevation(summaryElev, spec.units)}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-ink-muted">Moving Time</div>
                <div className="font-semibold text-ink">
                  {formatElapsedTime(summaryMoving)}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-ink-muted">Stages</div>
                <div className="font-semibold text-ink">{summaryStages}</div>
              </div>
            </div>
          </div>
        );
      })() : null}

      {/* Optional 5s Preview Watermark */}
      {isPreview || spec.is_preview ? (
        <div
          data-testid="preview-watermark"
          className="pointer-events-none absolute right-3 bottom-3 z-30 rounded border border-amber-accent/40 bg-canvas/75 px-2.5 py-1 text-[11px] font-semibold tracking-wider text-amber-accent backdrop-blur-sm"
        >
          PREVIEW • TRIP ANIMATOR
        </div>
      ) : null}

      {/* Interactive mode subtle aspect badge */}
      {renderMode === "interactive" && !isPreview && !spec.is_preview ? (
        <div className="pointer-events-none absolute right-3 bottom-3 z-10 flex items-center gap-1.5 rounded border border-border/80 bg-canvas/80 px-2 py-0.5 text-[10px] text-ink-muted backdrop-blur-sm">
          <span className="tabular-telemetry">{spec.resolution}</span>
          <span>•</span>
          <span>{spec.map_style}</span>
        </div>
      ) : null}
    </div>
  );
}
