import { Link } from "@tanstack/react-router";
import { Compass, Home } from "lucide-react";

export function NotFoundPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-canvas px-6 text-center text-ink">
      <div className="max-w-md space-y-4 rounded-xl border border-border bg-surface p-8 shadow-xl">
        <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-lg border border-border bg-elevated text-sky-accent">
          <Compass className="h-6 w-6" aria-hidden="true" />
        </div>
        <div className="space-y-1.5">
          <p className="tabular-telemetry text-xs font-semibold uppercase tracking-widest text-sky-accent">
            404
          </p>
          <h1 className="text-lg font-semibold text-ink">Page not found</h1>
          <p className="text-xs leading-relaxed text-ink-muted">
            The route you requested does not exist or may have moved.
          </p>
        </div>
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 rounded-md bg-sky-accent px-4 py-2 text-xs font-semibold text-canvas hover:bg-sky-accent/90 transition-colors"
        >
          <Home className="h-3.5 w-3.5" aria-hidden="true" />
          <span>Back to projects</span>
        </Link>
      </div>
    </div>
  );
}
