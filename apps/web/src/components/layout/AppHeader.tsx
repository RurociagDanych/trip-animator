import { Link } from "@tanstack/react-router";
import { Compass, LogIn, LogOut, Zap } from "lucide-react";
import type { ReactNode } from "react";
import type { UserOut } from "../../lib/api/types";

export interface AppHeaderProps {
  sectionLabel?: string;
  user?: UserOut | null;
  onLogout?: () => void;
  rightSlot?: ReactNode;
}

export function AppHeader({ sectionLabel, user, onLogout, rightSlot }: AppHeaderProps) {
  return (
    <header className="flex min-h-12 shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4 py-1.5 lg:h-12 lg:flex-nowrap lg:py-0">
      <div className="flex min-w-0 items-center gap-3">
        <Link
          to="/"
          className="flex shrink-0 items-center gap-2 text-sm font-semibold tracking-tight text-ink hover:text-sky-accent transition-colors"
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-md border border-border bg-elevated text-sky-accent">
            <Compass className="h-4 w-4" aria-hidden="true" />
          </span>
          <span>Trip Animator</span>
        </Link>

        {sectionLabel ? (
          <span className="truncate border-l border-border pl-3 text-sm text-ink-muted">
            {sectionLabel}
          </span>
        ) : null}
      </div>

      <nav aria-label="Workspace navigation" className="flex min-w-0 max-w-full flex-wrap items-center gap-2 lg:flex-nowrap">
        {rightSlot}

        {user ? (
          <>
            {user.strava_connected ? (
              <span className="hidden md:inline-flex items-center gap-1 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-2 py-1 text-[11px] font-medium text-amber-accent">
                <Zap className="h-3 w-3" aria-hidden="true" />
                <span>Strava</span>
              </span>
            ) : null}

            <span className="hidden sm:inline-block max-w-[160px] truncate text-xs text-ink-muted">
              {user.display_name || user.email}
            </span>

            {onLogout ? (
              <button
                type="button"
                onClick={onLogout}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-elevated px-2.5 py-1 text-xs font-medium text-ink hover:border-coral-accent/60 hover:text-coral-accent transition-colors"
              >
                <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Sign out</span>
              </button>
            ) : null}
          </>
        ) : (
          <Link
            to="/login"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-elevated px-2.5 py-1 text-xs font-medium text-ink hover:border-sky-accent/60 hover:text-sky-accent transition-colors"
          >
            <LogIn className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Sign in</span>
          </Link>
        )}
      </nav>
    </header>
  );
}
