import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, ExternalLink, Globe, Lock, RefreshCw, Share2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { projectsApi } from "../../lib/api/endpoints";
import type { ProjectShareOut } from "../../lib/api/types";

export interface SharePopoverProps {
  projectId: string;
  shareEnabled: boolean;
  shareSlug: string | null;
  onShareChanged: (share: ProjectShareOut) => void;
}

export function SharePopover({
  projectId,
  shareEnabled,
  shareSlug,
  onShareChanged,
}: SharePopoverProps) {
  const queryClient = useQueryClient();
  const containerRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
      }
    };
    const handlePointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, [open]);

  const shareUrl =
    shareEnabled && shareSlug
      ? `${typeof window !== "undefined" ? window.location.origin : ""}/share/${shareSlug}`
      : "";

  const enableMutation = useMutation({
    mutationFn: (rotate: boolean) => projectsApi.enableShare(projectId, { rotate }),
    onSuccess: (data) => {
      setErrorMsg(null);
      onShareChanged(data);
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "audit"] });
    },
    onError: (err) => {
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to update share settings.");
    },
  });

  const disableMutation = useMutation({
    mutationFn: () => projectsApi.disableShare(projectId),
    onSuccess: (data) => {
      setErrorMsg(null);
      onShareChanged(data);
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["projects", projectId, "audit"] });
    },
    onError: (err) => {
      setErrorMsg(err instanceof ApiError ? err.message : "Failed to revoke share link.");
    },
  });

  const isBusy = enableMutation.isPending || disableMutation.isPending;

  async function handleCopy() {
    if (!shareUrl || typeof navigator === "undefined" || !navigator.clipboard) {
      return;
    }
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setErrorMsg("Clipboard access denied.");
    }
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((prev) => !prev)}
        className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ${
          shareEnabled
            ? "border-teal-accent/60 bg-teal-accent/15 text-teal-accent"
            : "border-border bg-elevated text-ink hover:border-sky-accent/50"
        }`}
      >
        <Share2 className="h-3.5 w-3.5" aria-hidden="true" />
        <span className="hidden sm:inline">Share</span>
        {shareEnabled ? (
          <span className="h-1.5 w-1.5 rounded-full bg-teal-accent" aria-hidden="true" />
        ) : null}
      </button>

      {open ? (
        <div
          role="dialog"
          aria-label="Public share link"
          className="absolute right-0 z-50 mt-2 w-80 rounded-xl border border-border bg-surface p-4 shadow-2xl"
        >
          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="flex items-center gap-1.5 text-xs font-semibold text-ink">
                {shareEnabled ? (
                  <Globe className="h-3.5 w-3.5 text-teal-accent" aria-hidden="true" />
                ) : (
                  <Lock className="h-3.5 w-3.5 text-ink-muted" aria-hidden="true" />
                )}
                <span>Public share link</span>
              </h3>
              <p className="mt-1 text-[11px] leading-relaxed text-ink-muted">
                Anyone with the link can inspect the interactive map, elevation chart, and final MP4.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded px-1.5 py-0.5 text-[11px] text-ink-muted hover:bg-elevated hover:text-ink"
            >
              Close
            </button>
          </div>

          {errorMsg ? (
            <p role="alert" className="mt-2 rounded bg-coral-accent/15 px-2 py-1 text-[11px] text-coral-accent">
              {errorMsg}
            </p>
          ) : null}

          <div className="mt-3 space-y-2.5">
            {shareEnabled && shareSlug ? (
              <>
                <div className="flex items-center gap-1.5 rounded-md border border-border bg-canvas px-2 py-1.5">
                  <input
                    type="text"
                    readOnly
                    aria-label="Public share URL"
                    value={shareUrl}
                    className="min-w-0 flex-1 bg-transparent text-[11px] text-ink focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void handleCopy()}
                    aria-label="Copy share link"
                    className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-2 py-0.5 text-[10px] font-medium text-ink hover:border-sky-accent"
                  >
                    {copied ? (
                      <>
                        <Check className="h-3 w-3 text-teal-accent" aria-hidden="true" />
                        <span>Copied</span>
                      </>
                    ) : (
                      <>
                        <Copy className="h-3 w-3" aria-hidden="true" />
                        <span>Copy</span>
                      </>
                    )}
                  </button>
                </div>

                <div className="flex items-center justify-between gap-2">
                  <a
                    href={`/share/${shareSlug}`}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-sky-accent hover:underline"
                  >
                    <ExternalLink className="h-3 w-3" aria-hidden="true" />
                    <span>Open public view</span>
                  </a>

                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => enableMutation.mutate(true)}
                      className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-2 py-1 text-[11px] text-ink hover:border-sky-accent/50 disabled:opacity-50"
                    >
                      <RefreshCw className="h-3 w-3" aria-hidden="true" />
                      <span>Rotate</span>
                    </button>

                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => disableMutation.mutate()}
                      className="rounded border border-coral-accent/50 bg-coral-accent/10 px-2 py-1 text-[11px] font-medium text-coral-accent hover:bg-coral-accent/20 disabled:opacity-50"
                    >
                      Disable
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <button
                type="button"
                disabled={isBusy}
                onClick={() => enableMutation.mutate(false)}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-teal-accent px-3 py-1.5 text-xs font-semibold text-canvas hover:bg-teal-accent/90 disabled:opacity-50"
              >
                <Globe className="h-3.5 w-3.5" aria-hidden="true" />
                <span>{isBusy ? "Enabling..." : "Enable public link"}</span>
              </button>
            )}

            <p className="border-t border-border/70 pt-2 text-[10px] text-ink-muted">
              Private keys, Strava IDs, user IDs, and audit logs are stripped from public views.
            </p>
          </div>
        </div>
      ) : null}
    </div>
  );
}
