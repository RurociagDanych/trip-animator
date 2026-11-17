import { useQuery } from "@tanstack/react-query";
import { Activity, Loader2, RefreshCw, X } from "lucide-react";
import { useEffect } from "react";
import { projectsApi } from "../../lib/api/endpoints";
import type { AuditEventOut } from "../../lib/api/types";

export interface AuditLogDrawerProps {
  projectId: string;
  open: boolean;
  onClose: () => void;
}

function formatEventLabel(eventType: string): string {
  return eventType
    .replace(/[._]+/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

export function AuditLogDrawer({ projectId, open, onClose }: AuditLogDrawerProps) {
  const auditQuery = useQuery<AuditEventOut[]>({
    queryKey: ["projects", projectId, "audit"],
    queryFn: ({ signal }) => projectsApi.audit(projectId, 50, signal),
    enabled: open && Boolean(projectId),
    retry: false,
  });

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Project activity log"
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
        }
      }}
      className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-xs"
    >
      <div className="flex h-full w-full max-w-md flex-col border-l border-border bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-sky-accent" aria-hidden="true" />
            <h2 className="text-sm font-semibold text-ink">Activity log</h2>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void auditQuery.refetch()}
              aria-label="Refresh activity log"
              className="rounded p-1 text-ink-muted hover:bg-elevated hover:text-ink"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${auditQuery.isFetching ? "animate-spin text-sky-accent" : ""}`}
                aria-hidden="true"
              />
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close activity log"
              className="rounded p-1 text-ink-muted hover:bg-elevated hover:text-ink"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {auditQuery.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-xs text-ink-muted">
              <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
              <span>Loading activity log...</span>
            </div>
          ) : auditQuery.isError ? (
            <div className="rounded-md border border-coral-accent/40 bg-coral-accent/10 p-3 text-xs text-ink">
              Unable to load project activity log.
            </div>
          ) : (auditQuery.data ?? []).length === 0 ? (
            <p className="py-10 text-center text-xs text-ink-muted">
              No recorded activity events for this trip yet.
            </p>
          ) : (
            <ul className="space-y-2.5">
              {(auditQuery.data ?? []).map((ev) => {
                const entries = Object.entries(ev.metadata ?? {}).filter(
                  ([, v]) => v !== null && v !== undefined && v !== "",
                );
                return (
                  <li
                    key={ev.id}
                    className="rounded-lg border border-border bg-canvas/65 p-3 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-ink">
                        {formatEventLabel(ev.event_type)}
                      </span>
                      <span className="tabular-telemetry text-[10px] text-ink-muted">
                        {new Date(ev.created_at).toLocaleString()}
                      </span>
                    </div>
                    {entries.length > 0 ? (
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {entries.slice(0, 6).map(([k, v]) => (
                          <span
                            key={k}
                            className="rounded border border-border/80 bg-elevated px-1.5 py-0.5 text-[10px] text-ink-muted"
                          >
                            {k}: <span className="text-ink">{String(v)}</span>
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
