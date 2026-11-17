import { useQueries, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import {
  AlertCircle,
  Camera,
  Check,
  Code2,
  Compass,
  Download,
  Film,
  Loader2,
  Map as MapIcon,
  Mountain,
  Pause,
  Play,
  RotateCcw,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { TelemetryChart } from "../components/charts/TelemetryChart";
import { ActivityIcon, getActivityIconMeta } from "../components/icons";
import { TripStageCanvas } from "../components/stage/TripStageCanvas";
import { shareApi } from "../lib/api/endpoints";
import {
  normalizeRenderSpecV2,
  type ProjectHighlightsOut,
  type PublicProjectOut,
  type TelemetryChartOut,
  type TelemetryGeometryFeature,
} from "../lib/api/types";
import { formatDistance, formatElapsedTime, formatElevation } from "../lib/geo/format";
import {
  evaluateTimelineSchedule,
  matchPhotosToTimeline,
  progressRatioToTimeRatio,
} from "../lib/geo/photos";
import { buildTimelineFromProject } from "../lib/geo/timeline";
import type { PlaybackSpeed } from "../lib/state/editor-store";

const PLAYBACK_SPEEDS: readonly PlaybackSpeed[] = [0.5, 1, 2, 4];

export function ShareViewPage() {
  const { slug } = useParams({ from: "/share/$slug" });
  const [activeTab, setActiveTab] = useState<"map" | "video">("map");
  const [progressRatio, setProgressRatio] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState<PlaybackSpeed>(1);
  const [embedCopied, setEmbedCopied] = useState(false);

  const isEmbed =
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("embed") === "1";

  const projectQuery = useQuery<PublicProjectOut>({
    queryKey: ["share", slug],
    queryFn: ({ signal }) => shareApi.get(slug, signal),
    retry: false,
  });

  const project = projectQuery.data;
  const spec = useMemo(
    () => normalizeRenderSpecV2(project?.settings),
    [project?.settings],
  );

  const stageGeometryQueries = useQueries({
    queries: (project?.stages ?? []).map((stage) => ({
      queryKey: [
        "share",
        slug,
        "stage-geometry",
        stage.id,
        stage.point_count,
        stage.trim_start_s,
        stage.trim_end_s,
        "medium",
      ],
      queryFn: ({ signal }: { signal?: AbortSignal }) =>
        shareApi.stageGeometry(slug, stage.id, { lod: "medium", signal }),
      staleTime: Infinity,
      retry: 2,
      enabled: Boolean(project),
    })),
  });

  const chartQuery = useQuery<TelemetryChartOut>({
    queryKey: ["share", slug, "chart"],
    queryFn: ({ signal }) => shareApi.chart(slug, { maxPoints: 1200, signal }),
    enabled: Boolean(project),
    retry: false,
  });

  const highlightsQuery = useQuery<ProjectHighlightsOut>({
    queryKey: ["share", slug, "highlights"],
    queryFn: ({ signal }) => shareApi.highlights(slug, signal),
    enabled: Boolean(project) && (project?.stages.length ?? 0) > 0,
    retry: false,
  });

  const resolvedFeatures = useMemo(() => {
    const features: TelemetryGeometryFeature[] = [];
    for (const q of stageGeometryQueries) {
      if (q.data) {
        features.push(q.data);
      }
    }
    return features;
  }, [stageGeometryQueries]);

  const geometryStatus = useMemo(() => {
    const pendingCount = stageGeometryQueries.filter((q) => q.isPending).length;
    const errorCount = stageGeometryQueries.filter((q) => q.isError).length;
    return {
      pendingCount,
      errorCount,
      onRetryFailed: () => {
        for (const q of stageGeometryQueries) {
          if (q.isError) {
            void q.refetch();
          }
        }
      },
    };
  }, [stageGeometryQueries]);

  useEffect(() => {
    if (!isPlaying) {
      return;
    }
    let rafId = 0;
    let lastTs = performance.now();
    const durationMs = Math.max(1000, spec.duration_seconds * 1000);

    const tick = (now: number) => {
      const dt = now - lastTs;
      lastTs = now;
      setProgressRatio((prev) => {
        const next = prev + (dt * playbackSpeed) / durationMs;
        if (next >= 1) {
          setIsPlaying(false);
          return 1;
        }
        return next;
      });
      rafId = window.requestAnimationFrame(tick);
    };

    rafId = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(rafId);
  }, [isPlaying, playbackSpeed, spec.duration_seconds]);

  const timelineStages = useMemo(
    () =>
      buildTimelineFromProject(
        project?.stages ?? [],
        resolvedFeatures,
        chartQuery.data,
        spec,
      ),
    [project?.stages, resolvedFeatures, chartQuery.data, spec],
  );

  const matchedPhotos = useMemo(
    () =>
      matchPhotosToTimeline(
        project?.photos ?? [],
        timelineStages,
        spec,
        (photoId, variant) => shareApi.photoUrl(slug, photoId, variant),
      ),
    [project?.photos, timelineStages, spec, slug],
  );

  const schedule = useMemo(
    () =>
      evaluateTimelineSchedule(
        timelineStages,
        matchedPhotos,
        spec,
        progressRatio * spec.duration_seconds,
        spec.duration_seconds,
        false,
      ),
    [timelineStages, matchedPhotos, spec, progressRatio],
  );

  const stageStartProgressMap = useMemo(() => {
    const map = new Map<string, number>();
    const sorted = [...(project?.stages ?? [])].sort((a, b) => a.stage_order - b.stage_order);
    const totalDist = sorted.reduce((acc, s) => acc + Math.max(0, s.distance_m), 0);
    let cum = 0;
    for (let i = 0; i < sorted.length; i++) {
      const s = sorted[i]!;
      map.set(s.id, totalDist > 0 ? cum / totalDist : i / Math.max(1, sorted.length));
      cum += Math.max(0, s.distance_m);
    }
    return map;
  }, [project?.stages]);

  const heroTotals = useMemo(() => {
    const stagesList = project?.stages ?? [];
    const totalElevationGainM = stagesList.reduce((acc, s) => acc + s.elevation_gain_m, 0);
    const totalMovingS = stagesList.reduce((acc, s) => acc + s.moving_seconds, 0);
    const countries = new Set<string>();
    for (const ctx of highlightsQuery.data?.geo_context ?? []) {
      for (const c of ctx.countries) countries.add(c);
    }
    return {
      totalElevationGainM,
      totalMovingS,
      countries: Array.from(countries),
    };
  }, [project?.stages, highlightsQuery.data?.geo_context]);

  function seekToTargetProgress(targetProgress: number) {
    setIsPlaying(false);
    setActiveTab("map");
    setProgressRatio(
      progressRatioToTimeRatio(
        matchedPhotos,
        spec,
        Math.min(1, Math.max(0, targetProgress)),
        spec.duration_seconds,
        false,
      ),
    );
  }

  async function handleCopyEmbed() {
    const origin = typeof window !== "undefined" ? window.location.origin : "";
    const src = `${origin}/share/${encodeURIComponent(slug)}?embed=1`;
    const snippet = `<iframe src="${src}" width="100%" height="600" frameborder="0" allowfullscreen title="${(project?.title ?? "Trip Animator").replace(/"/g, "&quot;")}"></iframe>`;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(snippet);
      }
    } catch {
      // Clipboard may be unavailable in headless tests; still confirm state
    }
    setEmbedCopied(true);
    window.setTimeout(() => setEmbedCopied(false), 2500);
  }

  if (projectQuery.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas text-ink">
        <div className="flex items-center gap-2 text-sm text-ink-muted">
          <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
          <span>Loading shared trip...</span>
        </div>
      </div>
    );
  }

  if (projectQuery.isError || !project) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-6 text-center text-ink">
        <div className="max-w-md space-y-3 rounded-xl border border-border bg-surface p-6">
          <AlertCircle className="mx-auto h-7 w-7 text-amber-accent" aria-hidden="true" />
          <h1 className="text-base font-semibold">Shared trip unavailable</h1>
          <p className="text-xs text-ink-muted">
            This public share link does not exist or has been disabled by its owner.
          </p>
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-elevated px-3 py-1.5 text-xs font-medium text-ink hover:border-sky-accent"
          >
            Go to Trip Animator
          </Link>
        </div>
      </div>
    );
  }

  const climbs = highlightsQuery.data?.climbs ?? [];

  if (isEmbed) {
    return (
      <div
        data-testid="share-embed-shell"
        className="flex h-screen flex-col overflow-hidden bg-canvas text-ink"
      >
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden p-2">
          <TripStageCanvas
            stages={timelineStages}
            spec={spec}
            progressRatio={schedule.progressRatio}
            travelDurationS={schedule.travelDurationS}
            renderMode="interactive"
            geometryStatus={geometryStatus}
            matchedPhotos={matchedPhotos}
            activePhoto={schedule.activePhoto}
            climbs={climbs}
            projectTitle={project.title}
          />
        </div>
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-border bg-surface px-3 py-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setIsPlaying((p) => !p)}
              aria-pressed={isPlaying}
              aria-label={isPlaying ? "Pause preview" : "Play preview"}
              className="inline-flex h-7 items-center gap-1 rounded border border-border bg-elevated px-2.5 text-xs font-medium text-ink hover:border-sky-accent/60"
            >
              {isPlaying ? (
                <Pause className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
              ) : (
                <Play className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
              )}
              <span>{isPlaying ? "Pause" : "Play"}</span>
            </button>
            <span className="truncate text-xs font-medium text-ink">{project.title}</span>
          </div>
          <input
            type="range"
            min={0}
            max={1000}
            value={Math.round(schedule.progressRatio * 1000)}
            aria-label="Timeline scrubber"
            onChange={(e) => seekToTargetProgress(Number(e.currentTarget.value) / 1000)}
            className="max-w-xs flex-1 accent-sky-accent"
          />
          <a
            href={`/share/${encodeURIComponent(slug)}`}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 text-[11px] font-medium text-sky-accent hover:underline"
          >
            Open full trip
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-canvas text-ink">
      <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border bg-surface px-4">
        <div className="flex min-w-0 items-center gap-3">
          <Link
            to="/"
            className="flex shrink-0 items-center gap-2 text-sm font-semibold tracking-tight text-ink hover:text-sky-accent"
          >
            <span className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-elevated text-sky-accent">
              <Compass className="h-4 w-4" aria-hidden="true" />
            </span>
            <span>Trip Animator</span>
          </Link>

          <span className="truncate border-l border-border pl-3 text-sm font-medium text-ink">
            {project.title}
          </span>
          <span className="hidden sm:inline-flex rounded border border-teal-accent/40 bg-teal-accent/10 px-2 py-0.5 text-[10px] font-medium text-teal-accent">
            Shared Trip
          </span>
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void handleCopyEmbed()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-elevated px-2.5 py-1 text-[11px] font-medium text-ink hover:border-sky-accent/60"
          >
            {embedCopied ? (
              <Check className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
            ) : (
              <Code2 className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
            )}
            <span>{embedCopied ? "Copied embed" : "Copy embed"}</span>
          </button>

          {project.has_rendered_video ? (
            <div className="flex items-center rounded-md border border-border bg-elevated p-0.5 text-xs">
              <button
                type="button"
                aria-pressed={activeTab === "map"}
                onClick={() => setActiveTab("map")}
                className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-[11px] font-medium ${
                  activeTab === "map" ? "bg-sky-accent text-canvas" : "text-ink-muted hover:text-ink"
                }`}
              >
                <MapIcon className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Interactive Map</span>
              </button>
              <button
                type="button"
                aria-pressed={activeTab === "video"}
                onClick={() => setActiveTab("video")}
                className={`inline-flex items-center gap-1 rounded px-2.5 py-1 text-[11px] font-medium ${
                  activeTab === "video" ? "bg-sky-accent text-canvas" : "text-ink-muted hover:text-ink"
                }`}
              >
                <Film className="h-3.5 w-3.5" aria-hidden="true" />
                <span>MP4 Film</span>
              </button>
            </div>
          ) : null}
        </div>
      </header>

      {/* Hero KPI Summary Bar */}
      <section
        aria-label="Trip summary KPIs"
        data-testid="share-hero-kpis"
        className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface/60 px-4 py-2.5 text-xs"
      >
        <div className="tabular-telemetry flex flex-wrap items-center gap-5">
          <div>
            <span className="text-[10px] uppercase tracking-wider text-ink-muted">Distance</span>
            <p className="font-semibold text-ink">
              {formatDistance(project.total_distance_m, spec.units)}
            </p>
          </div>
          <div>
            <span className="text-[10px] uppercase tracking-wider text-ink-muted">Elevation Gain</span>
            <p className="font-semibold text-teal-accent">
              +{formatElevation(heroTotals.totalElevationGainM, spec.units)}
            </p>
          </div>
          <div>
            <span className="text-[10px] uppercase tracking-wider text-ink-muted">Moving Time</span>
            <p className="font-semibold text-ink">
              {formatElapsedTime(heroTotals.totalMovingS)}
            </p>
          </div>
          <div>
            <span className="text-[10px] uppercase tracking-wider text-ink-muted">Stages</span>
            <p className="font-semibold text-ink">{project.stage_count}</p>
          </div>
        </div>

        {heroTotals.countries.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            {heroTotals.countries.map((country) => (
              <span
                key={country}
                className="rounded-full border border-border bg-elevated px-2.5 py-0.5 text-[10px] font-medium text-ink"
              >
                {country}
              </span>
            ))}
          </div>
        ) : null}
      </section>

      <main className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-12">
        <aside
          aria-label="Shared trip stages"
          className="space-y-4 overflow-y-auto border-b border-border bg-surface p-4 lg:col-span-3 lg:border-r lg:border-b-0"
        >
          <div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-xs font-semibold text-ink">Trip itinerary</h2>
              <span className="tabular-telemetry text-[11px] text-ink-muted">
                {project.stage_count} stages • {formatDistance(project.total_distance_m, spec.units)}
              </span>
            </div>

            <ul className="space-y-2">
              {[...project.stages]
                .sort((a, b) => a.stage_order - b.stage_order)
                .map((stage) => {
                  const colorHex = spec.stage_colors[stage.id] ?? spec.track_color;
                  const meta = getActivityIconMeta(stage.activity_type);
                  const startProgress = stageStartProgressMap.get(stage.id) ?? 0;
                  return (
                    <li key={stage.id}>
                      <button
                        type="button"
                        onClick={() => seekToTargetProgress(startProgress)}
                        className="w-full rounded-lg border border-border bg-canvas/60 p-3 text-left text-xs transition-colors hover:border-sky-accent/60 hover:bg-elevated/60"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="inline-flex min-w-0 items-center gap-1.5 font-medium text-ink">
                            <ActivityIcon
                              type={stage.activity_type}
                              size={15}
                              accentColor={colorHex}
                              duotoneOpacity={0.28}
                              title={meta.label}
                            />
                            <span className="truncate">{stage.name}</span>
                          </span>
                          <span className="shrink-0 rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-ink-muted">
                            Day {stage.day_index}
                          </span>
                        </div>
                        <div className="tabular-telemetry mt-1.5 flex items-center gap-3 text-[11px] text-ink-muted">
                          <span>{formatDistance(stage.distance_m, spec.units)}</span>
                          <span>+{formatElevation(stage.elevation_gain_m, spec.units)}</span>
                          <span>{formatElapsedTime(stage.moving_seconds)}</span>
                        </div>
                      </button>
                    </li>
                  );
                })}
            </ul>
          </div>

          {climbs.length > 0 ? (
            <div className="border-t border-border pt-3">
              <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-ink">
                <Mountain className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
                <span>Climb highlights</span>
              </div>
              <ul className="space-y-1.5">
                {climbs.map((climb) => (
                  <li key={climb.id}>
                    <button
                      type="button"
                      onClick={() => seekToTargetProgress(climb.summit_progress)}
                      className="w-full rounded-md border border-border bg-canvas/55 px-2.5 py-2 text-left text-[11px] hover:border-amber-accent/60"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate font-medium text-ink">{climb.name}</span>
                        <span className="shrink-0 rounded border border-amber-accent/40 bg-amber-accent/15 px-1.5 py-0.5 text-[9px] font-semibold text-amber-accent">
                          {climb.category}
                        </span>
                      </div>
                      <div className="tabular-telemetry mt-1 flex items-center gap-2.5 text-[10px] text-ink-muted">
                        <span>+{formatElevation(climb.elevation_gain_m, spec.units)}</span>
                        <span>{climb.avg_grade_pct.toFixed(1)}% avg</span>
                        <span>{formatElevation(climb.summit_elev_m, spec.units)} summit</span>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </aside>

        <section className="flex min-h-0 flex-col bg-canvas lg:col-span-9">
          {activeTab === "video" && project.has_rendered_video ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 p-6">
              <video
                controls
                playsInline
                src={shareApi.videoUrl(slug)}
                className="max-h-[72vh] w-full max-w-4xl rounded-lg border border-border bg-black shadow-2xl"
              />
              <a
                href={shareApi.videoUrl(slug)}
                download={`${project.title.replace(/\s+/g, "-").toLowerCase()}.mp4`}
                className="inline-flex items-center gap-1.5 rounded-md bg-sky-accent px-3.5 py-2 text-xs font-semibold text-canvas hover:bg-sky-accent/90"
              >
                <Download className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Download MP4</span>
              </a>
            </div>
          ) : (
            <>
              <div className="flex min-h-[380px] flex-1 items-center justify-center overflow-hidden p-4">
                <TripStageCanvas
                  stages={timelineStages}
                  spec={spec}
                  progressRatio={schedule.progressRatio}
                  travelDurationS={schedule.travelDurationS}
                  renderMode="interactive"
                  geometryStatus={geometryStatus}
                  matchedPhotos={matchedPhotos}
                  activePhoto={schedule.activePhoto}
                  climbs={climbs}
                  projectTitle={project.title}
                  totalStats={{
                    distanceM: project.total_distance_m,
                    elevationGainM: heroTotals.totalElevationGainM,
                    movingSeconds: heroTotals.totalMovingS,
                    stageCount: project.stage_count,
                  }}
                />
              </div>

              {matchedPhotos.length > 0 ? (
                <div
                  aria-label="Trip photo strip"
                  data-testid="share-photo-strip"
                  className="flex items-center gap-2 overflow-x-auto border-t border-border bg-surface/60 px-4 py-2"
                >
                  <Camera className="h-3.5 w-3.5 shrink-0 text-sky-accent" aria-hidden="true" />
                  {matchedPhotos.map((photo) => (
                    <button
                      key={photo.id}
                      type="button"
                      disabled={photo.matchedProgress === null}
                      onClick={() => {
                        if (photo.matchedProgress !== null) {
                          seekToTargetProgress(photo.matchedProgress);
                        }
                      }}
                      title={photo.caption ?? "Seek to photo"}
                      className="group relative flex shrink-0 items-center gap-2 rounded-md border border-border bg-canvas/80 p-1 pr-2 text-left text-[10px] hover:border-sky-accent disabled:opacity-50"
                    >
                      {photo.src ? (
                        <img
                          src={photo.src}
                          alt={photo.caption ?? "Trip photo"}
                          className="h-9 w-12 rounded object-cover"
                        />
                      ) : null}
                      <span className="max-w-[96px] truncate font-medium text-ink group-hover:text-sky-accent">
                        {photo.caption || "Photo"}
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}

              <div className="shrink-0 space-y-2.5 border-t border-border bg-surface px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setIsPlaying((p) => !p)}
                      aria-pressed={isPlaying}
                      aria-label={isPlaying ? "Pause preview" : "Play preview"}
                      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-elevated px-3 text-xs font-medium text-ink hover:border-sky-accent/60"
                    >
                      {isPlaying ? (
                        <Pause className="h-3.5 w-3.5 text-amber-accent" aria-hidden="true" />
                      ) : (
                        <Play className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
                      )}
                      <span>{isPlaying ? "Pause" : "Play"}</span>
                    </button>

                    <button
                      type="button"
                      onClick={() => {
                        setIsPlaying(false);
                        setProgressRatio(0);
                      }}
                      aria-label="Reset playhead"
                      className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-ink-muted hover:text-ink"
                    >
                      <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>

                    <div className="flex items-center rounded-md border border-border bg-elevated p-0.5 text-[11px]">
                      {PLAYBACK_SPEEDS.map((spd) => (
                        <button
                          key={spd}
                          type="button"
                          aria-pressed={playbackSpeed === spd}
                          onClick={() => setPlaybackSpeed(spd)}
                          className={`rounded px-1.5 py-0.5 tabular-telemetry ${
                            playbackSpeed === spd
                              ? "bg-sky-accent font-semibold text-canvas"
                              : "text-ink-muted hover:text-ink"
                          }`}
                        >
                          {spd}×
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="flex min-w-[160px] flex-1 items-center gap-3">
                    <input
                      type="range"
                      min={0}
                      max={1000}
                      value={Math.round(schedule.progressRatio * 1000)}
                      aria-label="Timeline scrubber"
                      onChange={(e) => seekToTargetProgress(Number(e.currentTarget.value) / 1000)}
                      className="w-full accent-sky-accent"
                    />
                    {spec.pause_on_photo && schedule.activePhoto ? (
                      <span
                        data-testid="photo-pause-indicator"
                        className="shrink-0 rounded border border-amber-accent/50 bg-amber-accent/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-accent"
                      >
                        Paused at photo
                      </span>
                    ) : null}
                    <span className="tabular-telemetry shrink-0 text-xs text-ink-muted">
                      {(progressRatio * spec.duration_seconds).toFixed(1)}s /{" "}
                      {spec.duration_seconds}s
                    </span>
                  </div>
                </div>

                <TelemetryChart
                  points={chartQuery.data?.points ?? []}
                  totalDistanceM={chartQuery.data?.total_distance_m ?? 0}
                  units={spec.units}
                  progressRatio={schedule.progressRatio}
                  smoothingWindow={spec.telemetry_smoothing}
                  stages={timelineStages}
                  climbs={climbs}
                  onSeek={(ratio) => seekToTargetProgress(ratio)}
                />
              </div>
            </>
          )}
        </section>
      </main>
    </div>
  );
}
