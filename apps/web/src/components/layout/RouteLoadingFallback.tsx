import { Loader2 } from "lucide-react";

export function RouteLoadingFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas text-ink-muted">
      <div className="flex items-center gap-2 text-sm">
        <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
        <span>Loading view...</span>
      </div>
    </div>
  );
}
