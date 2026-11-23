/** Provides a consistent layout for video export actions. */
import { Download, Film, Loader2 } from "lucide-react";

interface Props {
  resolution: string;
  mode: "Local" | "Server";
  preview?: boolean;
  busy: boolean;
  disabled: boolean;
  describedBy?: string;
  onClick: () => void;
}

/** Displays the action, output resolution and render location. */
export function VideoExportButton({ resolution, mode, preview, busy, disabled, describedBy, onClick }: Props) {
  const Icon = busy ? Loader2 : preview ? Film : Download;
  return (
    <button
      type="button"
      disabled={disabled}
      aria-describedby={describedBy}
      onClick={onClick}
      className="inline-flex w-full items-center gap-2 rounded-md border border-border bg-elevated px-3 py-2 text-left text-xs font-semibold text-ink transition-colors hover:border-teal-accent/60 disabled:opacity-50"
    >
      <Icon className={`h-3.5 w-3.5 shrink-0${busy ? " animate-spin" : ""}`} aria-hidden="true" />
      <span className="flex-1">{preview ? "Preview clip (5s)" : "Export MP4"} ({resolution})</span>
      <span className="text-[11px] font-medium text-ink-muted">{mode}</span>
    </button>
  );
}