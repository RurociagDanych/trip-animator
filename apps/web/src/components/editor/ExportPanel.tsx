/** Coordinates server exports and the project's combined render history. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  Download,
  FileCode2,
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
import { listLocalRenders, type LocalRenderRecord } from "../../lib/export/local-history";
import { LocalExportControls, type LocalExportSource } from "./LocalExportControls";
import { VideoExportButton } from "./VideoExportButton";

export interface ExportPanelProps {
  projectId: string;
  spec: RenderSpecV2;
  stageCount: number;
  onFlushSave: () => Promise<boolean>;
  localExportSource?: LocalExportSource;
}

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

function VideoActions({ id, url, expanded, onToggle }: {
  id: string;
  url: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <button type="button" onClick={onToggle}
          className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-border bg-elevated py-1 text-[11px] font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent">
          <PlayCircle className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{expanded ? "Hide player" : "Watch"}</span>
        </button>
        <a href={url} download={`trip-render-${id}.mp4`}
          className="inline-flex flex-1 items-center justify-center gap-1 rounded border border-border bg-elevated py-1 text-[11px] font-medium text-ink hover:border-teal-accent/50 hover:text-teal-accent">
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          <span>Download</span>
        </a>
      </div>
      {expanded && <video controls playsInline src={url} className="w-full rounded border border-border bg-black" />}
    </div>
  );
}

function LocalVideoActions({ record, expanded, onToggle }: {
  record: LocalRenderRecord;
  expanded: boolean;
  onToggle: () => void;
}) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    let next: string | undefined;
    let disposed = false;
    queueMicrotask(() => {
      if (disposed) return;
      next = URL.createObjectURL(record.blob);
      setUrl(next);
    });
    return () => {
      disposed = true;
      if (next) URL.revokeObjectURL(next);
    };
  }, [record.blob]);
  return url ? <VideoActions id={record.id} url={url} expanded={expanded} onToggle={onToggle} /> : null;
}

/** Submits full server renders and shows the project's local and server exports. */
export function ExportPanel({ projectId, spec, stageCount, onFlushSave, localExportSource }: ExportPanelProps) {
  const queryClient = useQueryClient();
  const [submitting, setSubmitting] = useState(false);
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

  const localHistoryKey = ["projects", projectId, "local-renders"] as const;
  const localHistory = useQuery({
    queryKey: localHistoryKey,
    queryFn: () => listLocalRenders(projectId),
    enabled: Boolean(projectId && localExportSource),
    retry: false,
  });

  function showSavedRender(record: LocalRenderRecord) {
    queryClient.setQueryData<LocalRenderRecord[]>(localHistoryKey, (previous = []) => [
      record, ...previous.filter(({ id }) => id !== record.id),
    ]);
    void queryClient.invalidateQueries({ queryKey: localHistoryKey });
  }

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

  async function startRender() {
    if (stageCount === 0) {
      setExportError("Add at least one GPX or Strava stage before rendering.");
      return;
    }

    setExportError(null);
    setSubmitting(true);

    try {
      const saved = await onFlushSave();
      if (!saved) {
        setExportError("Resolve unsaved changes or revision conflict before exporting.");
        return;
      }

      const payload: RenderSpecV2 = {
        ...spec,
        is_preview: false,
      };
      const job = await rendersApi.create(projectId, payload);
      setActiveJobId(job.id);
      setLiveJob(job);
      await queryClient.invalidateQueries({ queryKey: ["projects", projectId, "renders"] });
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Failed to submit render job.");
    } finally {
      setSubmitting(false);
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

  const history = [
    ...jobs.map((job) => ({
      id: job.id, createdAt: job.created_at, job, local: null, status: job.status, progress: job.progress_pct,
      isPreview: job.spec?.is_preview, resolution: job.spec?.resolution ?? "1920x1080",
    })),
    ...(localHistory.data ?? []).map((local) => ({
      id: `local:${local.id}`, createdAt: local.createdAt, local, job: null, status: "succeeded", progress: 100,
      isPreview: local.isPreview, resolution: local.resolution,
    })),
  ].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));

  return (
    <div className="space-y-4 text-xs">
      <div className="grid grid-cols-1 gap-2">
        <VideoExportButton mode="Server" resolution={spec.resolution} busy={submitting}
          disabled={submitting} onClick={() => void startRender()} />
        {localExportSource && (
          <LocalExportControls
            key={projectId}
            projectId={projectId}
            source={localExportSource}
            spec={spec}
            disabled={submitting || stageCount === 0}
            onFlushSave={onFlushSave}
            onSaved={showSavedRender}
          />
        )}

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
          <span className="tabular-telemetry text-[11px] text-ink-muted">{history.length}</span>
        </div>

        {localHistory.isError && <p role="alert">Could not load this browser’s render history.</p>}
        {rendersQuery.isError && <p role="alert">Could not load server render history.</p>}
        {history.length === 0 && (rendersQuery.isLoading || localHistory.isLoading) ? (
          <p className="py-4 text-center text-[11px] text-ink-muted">Loading render history...</p>
        ) : history.length === 0 ? (
          <p className="rounded border border-border bg-canvas/40 p-3 text-center text-[11px] text-ink-muted">
            No exports to show.
          </p>
        ) : (
          <ul className="space-y-2.5">
            {history.map(({ id, job, local, status, progress, isPreview, resolution }) => {
              const isExpanded = expandedVideoId === id;
              const toggle = () => setExpandedVideoId(isExpanded ? null : id);
              return (
                <li
                  key={id}
                  className="space-y-2 rounded-lg border border-border bg-canvas/65 p-2.5"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        {status === "succeeded" ? (
                          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-teal-accent" aria-hidden="true" />
                        ) : status === "failed" || status === "cancelled" ? (
                          <XCircle className="h-3.5 w-3.5 shrink-0 text-coral-accent" aria-hidden="true" />
                        ) : (
                          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-sky-accent" aria-hidden="true" />
                        )}
                        <span className="truncate font-medium text-ink">
                          {isPreview ? "Preview (5s)" : "MP4 Export"}
                        </span>
                      </div>
                      <div className="tabular-telemetry mt-0.5 text-[10px] text-ink-muted">
                        {local ? "Local" : "Server"} • {resolution} • {status} ({progress}%)
                      </div>
                    </div>

                    {job?.thumbnail_key ? (
                      <img
                        src={rendersApi.thumbnailUrl(job.id)}
                        alt="Render thumbnail"
                        className="h-10 w-16 shrink-0 rounded border border-border object-cover"
                      />
                    ) : null}
                  </div>

                  {job?.error_message ? (
                    <p className="rounded bg-coral-accent/10 px-2 py-1 text-[10px] text-coral-accent">
                      {job.error_message}
                    </p>
                  ) : null}

                  {local ? <LocalVideoActions record={local} expanded={isExpanded} onToggle={toggle} />
                    : job?.status === "succeeded" && (
                      <VideoActions url={rendersApi.downloadUrl(job.id)} id={job.id} expanded={isExpanded} onToggle={toggle} />
                    )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
