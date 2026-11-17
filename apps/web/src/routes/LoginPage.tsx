import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { AlertCircle, Compass, KeyRound, Lock } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../lib/api/client";
import { authApi, systemApi } from "../lib/api/endpoints";
import type { PublicConfigOut } from "../lib/api/types";

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const publicConfigQuery = useQuery<PublicConfigOut>({
    queryKey: ["system", "public-config"],
    queryFn: ({ signal }) => systemApi.publicConfig(signal),
    retry: false,
  });

  const devAuthEnabled = publicConfigQuery.data?.dev_auth_enabled === true;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setErrorMessage(null);

    try {
      await authApi.login(email.trim(), password);
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      await navigate({ to: "/" });
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Unable to reach authentication service.");
      }
    } finally {
      setPending(false);
    }
  }

  async function handleDevLogin() {
    setPending(true);
    setErrorMessage(null);

    try {
      await authApi.devLogin();
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      await navigate({ to: "/" });
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Development sign-in is unavailable.");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="grid min-h-screen grid-cols-1 bg-canvas lg:grid-cols-12">
      <section className="relative hidden flex-col justify-between border-r border-border bg-surface p-10 lg:col-span-6 lg:flex">
        <Link to="/" className="inline-flex items-center gap-2.5 text-sm font-semibold text-ink">
          <span className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-sky-accent">
            <Compass className="h-4 w-4" aria-hidden="true" />
          </span>
          <span>Trip Animator</span>
        </Link>

        <div className="max-w-md space-y-4">
          <h1 className="text-3xl font-semibold tracking-tight text-ink">
            Precision route animations from GPX and Strava telemetry.
          </h1>
          <p className="text-sm leading-relaxed text-ink-muted">
            Combine multi-day GPX tracks and Strava streams into GeoParquet telemetry, scrub elevation
            and speed profiles in the browser, and export deterministic 1:1 MP4 renders.
          </p>
        </div>

        <div className="rounded-lg border border-border bg-canvas/80 p-4">
          <div className="mb-2 flex items-center justify-between text-xs text-ink-muted">
            <span>Stage preview</span>
            <span className="tabular-telemetry text-teal-accent">0.100° N, 0.200° E</span>
          </div>
          <svg viewBox="0 0 360 88" className="h-20 w-full" aria-hidden="true">
            <path
              d="M 8 72 C 74 68, 102 28, 166 36 C 228 44, 256 16, 352 24"
              fill="none"
              stroke="#38bdf8"
              strokeWidth="2.5"
              strokeLinecap="round"
            />
            <circle cx="238" cy="30" r="4.5" fill="#f59e0b" />
          </svg>
        </div>
      </section>

      <section className="flex items-center justify-center p-6 sm:p-10 lg:col-span-6">
        <div className="w-full max-w-sm space-y-6">
          <div className="space-y-1.5">
            <h2 className="text-xl font-semibold tracking-tight text-ink">Sign in to workspace</h2>
            <p className="text-sm text-ink-muted">
              Same-origin session cookies protected by CSRF verification.
            </p>
          </div>

          {errorMessage ? (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
            >
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent" aria-hidden="true" />
              <span>{errorMessage}</span>
            </div>
          ) : null}

          <form onSubmit={handleSubmit} className="space-y-4">
            <label className="block space-y-1.5">
              <span className="block text-xs font-medium text-ink-muted">Email address</span>
              <input
                type="email"
                name="email"
                autoComplete="email"
                required
                value={email}
                onChange={(event) => setEmail(event.currentTarget.value)}
                placeholder="explorer@example.com"
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
              />
            </label>

            <label className="block space-y-1.5">
              <span className="block text-xs font-medium text-ink-muted">Password</span>
              <input
                type="password"
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.currentTarget.value)}
                placeholder="Enter your password"
                className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
              />
            </label>

            <button
              type="submit"
              disabled={pending}
              className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
            >
              <Lock className="h-4 w-4" aria-hidden="true" />
              <span>{pending ? "Signing in..." : "Sign in"}</span>
            </button>
          </form>

          {devAuthEnabled ? (
            <div className="border-t border-border pt-4 space-y-2.5">
              <button
                type="button"
                disabled={pending}
                onClick={handleDevLogin}
                className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-border bg-surface px-4 py-2 text-xs font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent disabled:opacity-60 transition-colors"
              >
                <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Development one-click sign in</span>
              </button>
            </div>
          ) : null}

          <div className="text-center">
            <Link to="/" className="text-xs text-ink-muted hover:text-ink">
              Return to dashboard
            </Link>
          </div>
        </div>
      </section>
    </main>
  );
}
