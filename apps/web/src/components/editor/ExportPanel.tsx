import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileCode2,
  Film,
  Loader2,
  Map as MapIcon,
  PlayCircle,
  Square,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { projectsApi, rendersApi } from "../../lib/api/endpoints";
import type { RenderJobOut, RenderSpecV2 } from "../../lib/api/types";

export interface ExportPanelProps {
  projectId: string;
  spec: RenderSpecV2;
  stageCount: number;
  onFlushSave: () => Promise<boolean>;
}

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

export function ExportPanel({ projectId, spec, stageCount, onFlushSave }: ExportPanelProps) {
  const queryClient = useQueryClient();
  const [submittingKind, setSubmittingKind] = useState<"preview" | "export" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [activeJobId, setActiveJobId] = useState<string | null>(null);
  const [liveJob, setLiveJob] = useState<RenderJobOut | null>(null);
  const [expandedVideoId, setExpandedVideoId] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);

  const rendersQuery = useQuery<RenderJobOut[]>({
    queryKey: ["projects", projectId, "renders"],
    queryFn: ({ signal }) => rendersApi.listForProject(projectId, signal),
    enabled: Boolean(projectId),
    retry: false,
  });

  const firstActiveFromHistory = useMemo(
    () => (rendersQuery.data ?? []).find((j) => !TERMINAL_STATES.has(j.status)) ?? null,
    [rendersQuery.data],
  );

  const trackedJobId = activeJobId ?? firstActiveFromHistory?.id ?? null;

  useEffect(() => {
    if (!trackedJobId) {
      return;
    }

    let closed = false;
    let fallbackTimer: number | null = null;
    let source: EventSource | null = null;

    const refreshList = () => {
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "renders"] });
    };

    const startFallbackPoll = () => {
      if (fallbackTimer !== null || closed) return;
      fallbackTimer = window.setInterval(async () => {
        if (closed) return;
        try {
          const updated = await rendersApi.get(trackedJobId);
          setLiveJob(updated);
          if (TERMINAL_STATES.has(updated.status)) {
            if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
            refreshList();
          }
        } catch {
          // Ignore transient poll error
        }
      }, 1500);
    };

    if (typeof EventSource !== "undefined") {
      source = new EventSource(rendersApi.eventsUrl(trackedJobId), {
        withCredentials: true,
      });

      source.addEventListener("status", (event) => {
        if (closed || !(event instanceof MessageEvent)) return;
        try {
          const parsed = JSON.parse(event.data) as RenderJobOut;
          setLiveJob(parsed);
          if (TERMINAL_STATES.has(parsed.status)) {
            source?.close();
            refreshList();
          }
        } catch {
          // Ignore malformed event
        }
      });

      source.onerror = () => {
        source?.close();
        startFallbackPoll();
      };
    } else {
      startFallbackPoll();
    }

    return () => {
      closed = true;
      source?.close();
      if (fallbackTimer !== null) {
        window.clearInterval(fallbackTimer);
      }
    };
  }, [trackedJobId, projectId, queryClient]);

  async function startRender(isPreview: boolean) {
    if (stageCount === 0) {
      setExportError("Add at least one GPX or Strava stage before rendering.");
      return;
    }

    setExportError(null);
    setSubmittingKind(isPreview ? "preview" : "export");

    try {
      const saved = await onFlushSave();
      if (!saved) {
        setExportError("Resolve unsaved changes or revision conflict before exporting.");
        return;
      }

      const payload: RenderSpecV2 = {
        ...spec,
        is_preview: isPreview,
      };
      const job = await rendersApi.create(projectId, payload);
      setActiveJobId(job.id);
      setLiveJob(job);
      await queryClient.invalidateQueries({ queryKey: ["projects", projectId, "renders"] });
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Failed to submit render job.");
    } finally {
      setSubmittingKind(null);
    }
  }

  async function handleCancel(jobId: string) {
    setCancellingId(jobId);
    setExportError(null);
    try {
      const cancelled = await rendersApi.cancel(jobId);
      setLiveJob(cancelled);
      await queryClient.invalidateQueries({ queryKey: ["projects", projectId, "renders"] });
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Unable to cancel render job.");
    } finally {
      setCancellingId(null);
    }
  }

  const jobs = useMemo(() => {
    const raw = rendersQuery.data ?? [];
    if (!liveJob || liveJob.project_id !== projectId) {
      return raw;
    }
    return raw.map((j) => (j.id === liveJob.id ? { ...j, ...liveJob } : j));
  }, [rendersQuery.data, liveJob, projectId]);

  const displayedActiveJob =
    liveJob && liveJob.id === trackedJobId ? liveJob : firstActiveFromHistory;

  return (
    <div className="space-y-4 text-xs">
      <div className="grid grid-cols-1 gap-2">
        <button
          type="button"
          disabled={submittingKind !== null}
          onClick={() => void startRender(true)}
          className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-border bg-elevated px-3 py-2 text-xs font-medium text-ink hover:border-teal-accent/60 hover:text-teal-accent disabled:opacity-50 transition-colors"
        >
          {submittingKind === "preview" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-teal-accent" aria-hidden="true" />
          ) : (
            <Film className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
          )}
          <span>Preview clip (5s)</span>
        </button>

        <button
          type="button"
          disabled={submittingKind !== null}
          onClick={() => void startRender(false)}
          className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-sky-accent px-3 py-2 text-xs font-semibold text-canvas hover:bg-sky-accent/90 disabled:opacity-50 transition-colors"
        >
          {submittingKind === "export" ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          <span>Export MP4 ({spec.resolution})</span>
        </button>

        <div className="grid grid-cols-2 gap-2 pt-1">
          <a
            href={stageCount > 0 ? projectsApi.exportUrl(projectId, "gpx") : undefined}
            download={`trip-${projectId}.gpx`}
            aria-disabled={stageCount === 0}
            onClick={(e) => {
              if (stageCount === 0) {
                e.preventDefault();
                setExportError("Add at least one stage before exporting GPX.");
              }
            }}
            className={`inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-elevated px-2.5 py-1.5 text-[11px] font-medium transition-colors ${
              stageCount === 0
                ? "cursor-not-allowed text-ink-muted opacity-50"
                : "text-ink hover:border-teal-accent/60 hover:text-teal-accent"
            }`}
          >
            <MapIcon className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
            <span>Export GPX</span>
          </a>

          <a
            href={stageCount > 0 ? projectsApi.exportUrl(projectId, "geojson") : undefined}
            download={`trip-${projectId}.geojson`}
            aria-disabled={stageCount === 0}
            onClick={(e) => {
              if (stageCount === 0) {
                e.preventDefault();
                setExportError("Add at least one stage before exporting GeoJSON.");
              }
            }}
            className={`inline-flex items-center justify-center gap-1.5 rounded-md border border-border bg-elevated px-2.5 py-1.5 text-[11px] font-medium transition-colors ${
              stageCount === 0
                ? "cursor-not-allowed text-ink-muted opacity-50"
                : "text-ink hover:border-sky-accent/60 hover:text-sky-accent"
            }`}
          >
            <FileCode2 className="h-3.5 w-3.5 text-sky-accent" aria-hidden="true" />
            <span>Export GeoJSON</span>
          </a>
        </div>
      </div>

      {exportError ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-coral-accent/40 bg-coral-accent/10 px-2.5 py-2 text-[11px] text-ink"
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-coral-accent" aria-hidden="true" />
          <span>{exportError}</span>
        </div>
      ) : null}

      {displayedActiveJob ? (
        <div
          data-testid="active-render-card"
          className="space-y-2 rounded-lg border border-sky-accent/40 bg-canvas/80 p-3"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-ink">
              {displayedActiveJob.spec.is_preview ? "5s preview render" : "Full MP4 export"}
            </span>
            <span className="tabular-telemetry rounded bg-elevated px-1.5 py-0.5 text-[10px] text-sky-accent">
              {displayedActiveJob.status} • {displayedActiveJob.progress_pct}%
            </span>
          </div>

          <div className="h-2 w-full overflow-hidden rounded-full bg-elevated">
            <div
              className="h-full bg-sky-accent transition-all duration-300"
              style={{ width: `${Math.min(100, Math.max(2, displayedActiveJob.progress_pct))}%` }}
            />
          </div>

          {!TERMINAL_STATES.has(displayedActiveJob.status) ? (
            <button
              type="button"
              disabled={cancellingId === displayedActiveJob.id}
              onClick={() => void handleCancel(displayedActiveJob.id)}
              className="inline-flex items-center gap-1 rounded border border-coral-accent/50 bg-coral-accent/10 px-2 py-1 text-[11px] text-coral-accent hover:bg-coral-accent/20"
            >
              <Square className="h-3 w-3" aria-hidden="true" />
              <span>{cancellingId === displayedActiveJob.id ? "Cancelling..." : "Cancel render"}</span>
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="space-y-2 border-t border-border pt-3">
        <div className="flex items-center justify-between">
          <span className="font-semibold text-ink">Render history</span>
          <span className="tabular-telemetry text-[11px] text-ink-muted">{jobs.length}</span>
        </div>

        {rendersQuery.isLoading ? (
          <p className="py-4 text-center text-[11px] text-ink-muted">Loading render history...</p>
        ) : jobs.length === 0 ? (
          <p className="rounded border border-border bg-canvas/40 p-3 text-center text-[11px] text-ink-muted">
            No exports yet. Run a 5s preview or full MP4 render.
          </p>
        ) : (
          <ul className="space-y-2.5">
            {jobs.map((job) => {
              const isExpanded = expandedVideoId === job.id;
              return (
                <li
                  key={job.id}
                  className="space-y-2 rounded-lg border border-border bg-canvas/65 p-2.5"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        {job.status === "succeeded" ? (
                          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-teal-accent" aria-hidden="true" />
                        ) : job.status === "failed" || job.status === "cancelled" ? (
                          <XCircle className="h-3.5 w-3.5 shrink-0 text-coral-accent" aria-hidden="true" />
                        ) : (
                          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-accent" aria-hidden="true" />
                        )}
                        <span className="truncate font-medium text-ink">
                          {job.spec?.is_preview ? "Preview (5s)" : "MP4 Export"}
                        </span>
                      </div>
                      <div className="tabular-telemetry mt-0.5 text-[10px] text-ink-muted">
                        {job.spec?.resolution ?? "1920x1080"} • {job.status} ({job.progress_pct}%)
                      </div>
                    </div>

                    {job.thumbnail_key ? (
                      <img
                        src={rendersApi.thumbnailUrl(job.id)}
                        alt="Render thumbnail"
                        className="h-10 w-16 shrink-0 rounded border border-border object-cover"
                      />
                    ) : null}
                  </div>

                  {job.error_message ? (
                    <p className="rounded bg-coral-accent/10 px-2 py-1 text-[10px] text-coral-accent">
                      {job.error_message}
                    </p>
                  ) : null}

                  {job.status === "succeeded" ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => setExpandedVideoId(isExpanded ? null : job.id)}
                          className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-border bg-elevated py-1 text-[11px] font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent"
                        >
                          <PlayCircle className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>{isExpanded ? "Hide player" : "Watch"}</span>
                        </button>
                        <a
                          href={rendersApi.downloadUrl(job.id)}
                          download={`trip-render-${job.id}.mp4`}
                          className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-border bg-elevated py-1 text-[11px] font-medium text-ink hover:border-teal-accent/50 hover:text-teal-accent"
                        >
                          <Download className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>Download</span>
                        </a>
                      </div>

                      {isExpanded ? (
                        <video
                          controls
                          playsInline
                          src={rendersApi.downloadUrl(job.id)}
                          className="w-full rounded border border-border bg-black"
                        />
                      ) : null}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
