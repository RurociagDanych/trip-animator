import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  AlertCircle,
  Globe2,
  Loader2,
  Plus,
  Route,
  Trash2,
  Unplug,
  Zap,
} from "lucide-react";
import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { ActivityIcon, getActivityIconMeta } from "../components/icons";
import { ActivityLibraryView } from "../components/dashboard/ActivityLibraryView";

const LazyAtlasView = lazy(() =>
  import("../components/dashboard/AtlasView").then((m) => ({
    default: m.AtlasView,
  })),
);
import { AppHeader } from "../components/layout/AppHeader";
import { ApiError } from "../lib/api/client";
import {
  analyticsApi,
  authApi,
  projectsApi,
  stravaApi,
  systemApi,
} from "../lib/api/endpoints";
import type {
  AnalyticsHeatmapOut,
  AtlasTripOut,
  CareerAnalyticsOut,
  ProjectOut,
  PublicConfigOut,
  SessionInfoOut,
} from "../lib/api/types";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { formatDistance, formatElevation } from "../lib/geo/format";
import { polylinesToSvgPath } from "../lib/geo/polyline";

export function DashboardPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [dashboardTab, setDashboardTab] = useState<"trips" | "atlas" | "library">("trips");
  const [newTitle, setNewTitle] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const sessionQuery = useQuery<SessionInfoOut>({
    queryKey: ["auth", "me"],
    queryFn: ({ signal }) => authApi.me(signal),
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });

  const publicConfigQuery = useQuery<PublicConfigOut>({
    queryKey: ["system", "public-config"],
    queryFn: ({ signal }) => systemApi.publicConfig(signal),
    retry: false,
  });

  const projectsQuery = useQuery<ProjectOut[]>({
    queryKey: ["projects"],
    queryFn: ({ signal }) => projectsApi.list(signal),
    enabled: sessionQuery.isSuccess,
    retry: false,
  });

  const heatmapQuery = useQuery<AnalyticsHeatmapOut>({
    queryKey: ["analytics", "heatmap", "low"],
    queryFn: ({ signal }) => analyticsApi.heatmap({ lod: "low", signal }),
    enabled: sessionQuery.isSuccess,
    retry: false,
  });

  const careerQuery = useQuery<CareerAnalyticsOut>({
    queryKey: ["analytics", "career"],
    queryFn: ({ signal }) => analyticsApi.career({ signal }),
    enabled: sessionQuery.isSuccess && dashboardTab === "atlas",
    retry: false,
  });

  useEffect(() => {
    if (
      sessionQuery.isError &&
      sessionQuery.error instanceof ApiError &&
      sessionQuery.error.status === 401
    ) {
      void navigate({ to: "/login" });
    }
  }, [sessionQuery.isError, sessionQuery.error, navigate]);

  const createProjectMutation = useMutation({
    mutationFn: (title: string) =>
      projectsApi.create({
        title,
        settings: createDefaultRenderSpecV2(),
      }),
    onSuccess: (created) => {
      setNewTitle("");
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void navigate({ to: "/projects/$projectId", params: { projectId: created.id } });
    },
    onError: (err) => {
      setActionError(err instanceof ApiError ? err.message : "Failed to create project.");
    },
  });

  const deleteProjectMutation = useMutation({
    mutationFn: (projectId: string) => projectsApi.delete(projectId),
    onSuccess: () => {
      setConfirmDeleteId(null);
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["analytics"] });
    },
    onError: (err) => {
      setActionError(err instanceof ApiError ? err.message : "Failed to delete project.");
    },
  });

  const stravaConnectMutation = useMutation({
    mutationFn: () => stravaApi.connect(),
    onSuccess: (res) => {
      window.location.assign(res.authorize_url);
    },
    onError: (err) => {
      setActionError(err instanceof ApiError ? err.message : "Failed to start Strava OAuth.");
    },
  });

  const stravaDisconnectMutation = useMutation({
    mutationFn: () => stravaApi.disconnect(),
    onSuccess: () => {
      setActionError(null);
      void queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
    },
    onError: (err) => {
      setActionError(err instanceof ApiError ? err.message : "Failed to disconnect Strava.");
    },
  });

  async function handleLogout() {
    try {
      await authApi.logout();
    } finally {
      queryClient.clear();
      await navigate({ to: "/login" });
    }
  }

  function handleCreateSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleaned = newTitle.trim();
    if (!cleaned) {
      setActionError("Enter a trip name to create a project.");
      return;
    }
    createProjectMutation.mutate(cleaned);
  }

  const tripAtlasByProjectId = useMemo(() => {
    const map = new Map<string, AtlasTripOut>();
    const trips = heatmapQuery.data?.trips;
    if (Array.isArray(trips)) {
      for (const trip of trips) {
        map.set(trip.project_id, trip);
      }
    }
    return map;
  }, [heatmapQuery.data?.trips]);

  const user = sessionQuery.data?.user ?? null;
  const projects = projectsQuery.data ?? [];
  const stravaConfigured = publicConfigQuery.data?.strava_configured ?? false;

  return (
    <div className="flex min-h-screen flex-col bg-canvas text-ink">
      <AppHeader
        sectionLabel={dashboardTab === "atlas" ? "Atlas" : "Trips"}
        user={user}
        onLogout={user ? () => void handleLogout() : undefined}
      />

      <main className="mx-auto w-full max-w-6xl flex-1 space-y-6 px-6 py-8">
        <section className="flex flex-col gap-4 border-b border-border pb-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3 max-w-xl">
            <div className="space-y-1.5">
              <h1 className="text-2xl font-semibold tracking-tight text-ink">Trip projects</h1>
              <p className="text-sm text-ink-muted">
                Assemble multi-day GPX, FIT, and TCX tracks or Strava activities into synchronized 1:1 map films.
              </p>
            </div>

            <div
              role="tablist"
              aria-label="Workspace views"
              className="inline-flex rounded-lg border border-border bg-surface p-1 text-xs"
            >
              <button
                type="button"
                role="tab"
                aria-selected={dashboardTab === "trips"}
                onClick={() => setDashboardTab("trips")}
                className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors ${
                  dashboardTab === "trips"
                    ? "bg-elevated text-sky-accent"
                    : "text-ink-muted hover:text-ink"
                }`}
              >
                <Route className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Trips</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={dashboardTab === "library"}
                onClick={() => setDashboardTab("library")}
                className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors ${dashboardTab === "library" ? "bg-elevated text-sky-accent" : "text-ink-muted hover:text-ink"}`}
              >
                <span>Activity Library</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={dashboardTab === "atlas"}
                onClick={() => setDashboardTab("atlas")}
                className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 font-medium transition-colors ${
                  dashboardTab === "atlas"
                    ? "bg-elevated text-sky-accent"
                    : "text-ink-muted hover:text-ink"
                }`}
              >
                <Globe2 className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Atlas</span>
              </button>
            </div>
          </div>

          <form onSubmit={handleCreateSubmit} className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              aria-label="New trip title"
              value={newTitle}
              onChange={(e) => setNewTitle(e.currentTarget.value)}
              placeholder="e.g. Transalp Dolomites 2026"
              className="w-64 rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
            />
            <button
              type="submit"
              disabled={createProjectMutation.isPending || !user}
              className="inline-flex items-center gap-1.5 rounded-md bg-sky-accent px-3.5 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-50 transition-colors"
            >
              {createProjectMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <Plus className="h-4 w-4" aria-hidden="true" />
              )}
              <span>Create trip</span>
            </button>
          </form>
        </section>

        {actionError ? (
          <div
            role="alert"
            className="flex items-center gap-2 rounded-lg border border-coral-accent/40 bg-coral-accent/10 px-4 py-3 text-xs text-ink"
          >
            <AlertCircle className="h-4 w-4 shrink-0 text-coral-accent" aria-hidden="true" />
            <span>{actionError}</span>
          </div>
        ) : null}

        {dashboardTab === "library" ? <ActivityLibraryView key={user?.id ?? "anonymous"} ownerId={user?.id ?? null} /> : dashboardTab === "atlas" ? (
          <Suspense
            fallback={
              <div className="flex items-center justify-center rounded-lg border border-border bg-surface p-10 text-xs text-ink-muted">
                <Loader2 className="mr-2 h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
                <span>Loading Atlas view...</span>
              </div>
            }
          >
            <LazyAtlasView
              projects={projects}
              career={careerQuery.data}
              heatmap={heatmapQuery.data}
              stravaConfigured={stravaConfigured}
              stravaConnected={Boolean(user?.strava_connected)}
              isConnectingStrava={stravaConnectMutation.isPending}
              onConnectStrava={() => stravaConnectMutation.mutate()}
              onStravaImported={() => {
                void queryClient.invalidateQueries({ queryKey: ["projects"] });
                void queryClient.invalidateQueries({ queryKey: ["analytics"] });
              }}
            />
          </Suspense>
        ) : (
          <>
            <section
              aria-label="Strava connection"
              className="flex flex-col justify-between gap-4 rounded-lg border border-border bg-surface p-4 sm:flex-row sm:items-center"
            >
              <div className="flex items-start gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-border bg-elevated text-amber-accent">
                  <Zap className="h-4 w-4" aria-hidden="true" />
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-sm font-medium text-ink">Strava integration</h2>
                    <span
                      className={`rounded px-2 py-0.5 text-[11px] font-medium ${
                        user?.strava_connected
                          ? "bg-teal-accent/15 text-teal-accent"
                          : "bg-elevated text-ink-muted"
                      }`}
                    >
                      {user?.strava_connected ? "Connected" : "Not connected"}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-ink-muted">
                    {user?.strava_connected
                      ? "OAuth2 PKCE token active. Import GPS & telemetry streams directly inside any studio editor."
                      : stravaConfigured
                        ? "Link your Strava account to browse activities and import GPS, elevation, speed, and heart-rate streams."
                        : "Configure STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET on the server to enable Strava OAuth."}
                  </p>
                </div>
              </div>

              <div className="flex shrink-0 items-center gap-2">
                {user?.strava_connected ? (
                  <button
                    type="button"
                    disabled={stravaDisconnectMutation.isPending}
                    onClick={() => stravaDisconnectMutation.mutate()}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-elevated px-3 py-1.5 text-xs font-medium text-ink hover:border-coral-accent/60 hover:text-coral-accent disabled:opacity-50"
                  >
                    <Unplug className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>Disconnect Strava</span>
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={!stravaConfigured || !user || stravaConnectMutation.isPending}
                    onClick={() => stravaConnectMutation.mutate()}
                    className="inline-flex items-center gap-1.5 rounded-md border border-amber-accent/50 bg-amber-accent/15 px-3 py-1.5 text-xs font-medium text-amber-accent hover:bg-amber-accent/25 disabled:opacity-45"
                  >
                    <Zap className="h-3.5 w-3.5" aria-hidden="true" />
                    <span>Connect Strava</span>
                  </button>
                )}
              </div>
            </section>

            {sessionQuery.isLoading || projectsQuery.isLoading ? (
              <div className="flex items-center justify-center gap-2 py-16 text-sm text-ink-muted">
                <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
                <span>Loading trip workspace...</span>
              </div>
            ) : projects.length === 0 ? (
              <div className="rounded-lg border border-dashed border-border bg-surface/50 p-10 text-center">
                <Route className="mx-auto h-7 w-7 text-sky-accent" aria-hidden="true" />
                <h2 className="mt-3 text-base font-medium text-ink">No trip projects yet</h2>
                <p className="mt-1 text-xs text-ink-muted">
                  Create your first trip above, then drop GPX/FIT/TCX files or import Strava rides in the 3-pane studio.
                </p>
              </div>
            ) : (
              <section
                aria-label="Workspace projects"
                className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
              >
                {projects.map((project) => {
                  const isConfirming = confirmDeleteId === project.id;
                  const atlasTrip = tripAtlasByProjectId.get(project.id);
                  const silhouettePath =
                    atlasTrip && Array.isArray(atlasTrip.polylines) && atlasTrip.polylines.length > 0
                      ? polylinesToSvgPath(atlasTrip.polylines, 240, 68, 8)
                      : "";
                  const activityMeta = atlasTrip
                    ? getActivityIconMeta(atlasTrip.primary_activity)
                    : null;
                  const units = project.settings?.units ?? "metric";

                  return (
                    <article
                      key={project.id}
                      className="group flex flex-col justify-between rounded-lg border border-border bg-surface p-5 hover:border-sky-accent/60 transition-colors"
                    >
                      <div className="space-y-3">
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <div className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-elevated text-sky-accent">
                              {activityMeta && atlasTrip ? (
                                <ActivityIcon
                                  type={atlasTrip.primary_activity}
                                  size={18}
                                  accentColor={activityMeta.accentColor}
                                  duotoneOpacity={0.28}
                                  title={activityMeta.label}
                                />
                              ) : (
                                <Route className="h-4 w-4" aria-hidden="true" />
                              )}
                            </div>
                            {activityMeta ? (
                              <span className="rounded border border-border bg-elevated px-2 py-0.5 text-[10px] font-medium text-ink-muted">
                                {activityMeta.shortLabel}
                              </span>
                            ) : null}
                          </div>

                          <div className="flex items-center gap-1.5">
                            <span className="tabular-telemetry rounded border border-border bg-elevated px-2 py-0.5 text-[11px] text-ink-muted">
                              rev {project.revision}
                            </span>
                            {isConfirming ? (
                              <div className="flex items-center gap-1">
                                <button
                                  type="button"
                                  onClick={() => deleteProjectMutation.mutate(project.id)}
                                  disabled={deleteProjectMutation.isPending}
                                  className="rounded bg-coral-accent px-2 py-0.5 text-[11px] font-medium text-canvas"
                                >
                                  Confirm
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setConfirmDeleteId(null)}
                                  className="rounded border border-border bg-elevated px-2 py-0.5 text-[11px] text-ink-muted"
                                >
                                  Cancel
                                </button>
                              </div>
                            ) : (
                              <button
                                type="button"
                                aria-label={`Delete ${project.title}`}
                                onClick={() => setConfirmDeleteId(project.id)}
                                className="rounded p-1 text-ink-muted hover:bg-elevated hover:text-coral-accent transition-colors"
                              >
                                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                              </button>
                            )}
                          </div>
                        </div>

                        <Link
                          to="/projects/$projectId"
                          params={{ projectId: project.id }}
                          className="relative block h-16 overflow-hidden rounded-md border border-border/70 bg-[#0b1117]"
                        >
                          <div className="topo-grid-surface absolute inset-0" aria-hidden="true" />
                          <svg
                            viewBox="0 0 240 68"
                            preserveAspectRatio="xMidYMid meet"
                            className="relative h-full w-full"
                            aria-hidden="true"
                          >
                            {silhouettePath ? (
                              <path
                                data-testid={`project-silhouette-${project.id}`}
                                d={silhouettePath}
                                fill="none"
                                stroke={project.settings?.track_color || "#38bdf8"}
                                strokeWidth="2.4"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            ) : (
                              <path
                                d="M 24 52 C 70 42, 102 22, 146 30 C 176 36, 198 18, 216 16"
                                fill="none"
                                stroke="#38bdf8"
                                strokeOpacity={0.22}
                                strokeWidth="2"
                                strokeDasharray="4 4"
                                strokeLinecap="round"
                              />
                            )}
                          </svg>
                        </Link>

                        <Link
                          to="/projects/$projectId"
                          params={{ projectId: project.id }}
                          className="block"
                        >
                          <h2 className="text-base font-medium text-ink group-hover:text-sky-accent transition-colors">
                            {project.title}
                          </h2>
                          <p className="mt-1 text-xs text-ink-muted">
                            Updated {new Date(project.updated_at).toLocaleDateString()}
                          </p>
                        </Link>
                      </div>

                      <div className="mt-5 flex items-center justify-between border-t border-border pt-3 text-xs text-ink-muted">
                        <div className="tabular-telemetry flex items-center gap-2.5">
                          <span>{project.stage_count} stages</span>
                          <span>{formatDistance(project.total_distance_m, units)}</span>
                          {atlasTrip ? (
                            <span className="text-teal-accent">
                              +{formatElevation(atlasTrip.elevation_gain_m, units)}
                            </span>
                          ) : null}
                        </div>
                        <Link
                          to="/projects/$projectId"
                          params={{ projectId: project.id }}
                          className="text-xs font-medium text-sky-accent hover:underline"
                        >
                          Open studio
                        </Link>
                      </div>
                    </article>
                  );
                })}
              </section>
            )}
          </>
        )}
      </main>
    </div>
  );
}
