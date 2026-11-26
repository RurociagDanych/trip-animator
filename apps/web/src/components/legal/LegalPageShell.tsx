import { Link } from "@tanstack/react-router";
import { ArrowLeft, Compass } from "lucide-react";
import type { ReactNode } from "react";

export interface LegalPageShellProps {
  title: string;
  lastUpdated: string;
  icon: ReactNode;
  siblingTo: "/privacy" | "/terms";
  siblingLabel: string;
  children: ReactNode;
}

export function LegalPageShell({
  title,
  lastUpdated,
  icon,
  siblingTo,
  siblingLabel,
  children,
}: LegalPageShellProps) {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-3xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
          <Link
            to="/"
            className="inline-flex shrink-0 items-center gap-2.5 whitespace-nowrap text-sm font-semibold text-ink"
          >
            <span className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-sky-accent">
              <Compass className="h-4 w-4" aria-hidden="true" />
            </span>
            <span>Trip Animator</span>
          </Link>
          <nav
            aria-label="Legal navigation"
            className="flex flex-wrap items-center gap-3 text-xs sm:gap-4"
          >
            <Link
              to={siblingTo}
              className="whitespace-nowrap text-ink-muted hover:text-ink transition-colors"
            >
              {siblingLabel}
            </Link>
            <Link
              to="/login"
              className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-border bg-elevated px-3 py-1.5 font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent transition-colors"
            >
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Back to sign in</span>
            </Link>
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-4 py-10 sm:px-6">
        <article className="space-y-8 rounded-xl border border-border bg-surface p-6 sm:p-8">
          <div className="space-y-2 border-b border-border pb-6">
            <div className="inline-flex items-center gap-2 text-xs font-medium text-sky-accent">
              {icon}
              <span>Legal</span>
            </div>
            <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
            <p className="text-xs text-ink-muted">Last updated: {lastUpdated}</p>
          </div>
          {children}
        </article>
      </main>
    </div>
  );
}

export function LegalLinksNav() {
  return (
    <footer>
      <nav
        aria-label="Legal links"
        className="flex items-center justify-center gap-3 text-[11px] text-ink-muted"
      >
        <Link to="/privacy" className="hover:text-ink transition-colors">
          Privacy Policy
        </Link>
        <span aria-hidden="true">·</span>
        <Link to="/terms" className="hover:text-ink transition-colors">
          Terms of Service
        </Link>
      </nav>
    </footer>
  );
}
