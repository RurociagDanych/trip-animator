import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../lib/api/client";
import { stravaApi } from "../lib/api/endpoints";

export function StravaCallbackPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const oauthParams = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    return {
      code: params.get("code"),
      state: params.get("state"),
      oauthError: params.get("error"),
    };
  }, []);

  const initialError = oauthParams.oauthError
    ? "Strava authorization was denied."
    : !oauthParams.code || !oauthParams.state
      ? "Missing OAuth code or state parameter."
      : null;

  const [status, setStatus] = useState<"loading" | "ok" | "error">(
    initialError ? "error" : "loading",
  );
  const [message, setMessage] = useState(
    initialError ?? "Completing Strava OAuth authorization...",
  );

  useEffect(() => {
    if (initialError || !oauthParams.code || !oauthParams.state) {
      return;
    }

    let cancelled = false;
    stravaApi
      .callback(oauthParams.code, oauthParams.state)
      .then(() => {
        if (cancelled) return;
        void queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
        setStatus("ok");
        setMessage("Strava connected! Redirecting to dashboard...");
        window.setTimeout(() => {
          void navigate({ to: "/" });
        }, 700);
      })
      .catch((err) => {
        if (cancelled) return;
        setStatus("error");
        setMessage(err instanceof ApiError ? err.message : "Strava token exchange failed.");
      });

    return () => {
      cancelled = true;
    };
  }, [initialError, oauthParams.code, oauthParams.state, navigate, queryClient]);

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas p-6 text-ink">
      <div className="w-full max-w-sm space-y-4 rounded-xl border border-border bg-surface p-6 text-center">
        {status === "loading" ? (
          <Loader2 className="mx-auto h-6 w-6 animate-spin text-sky-accent" aria-hidden="true" />
        ) : status === "ok" ? (
          <CheckCircle2 className="mx-auto h-6 w-6 text-teal-accent" aria-hidden="true" />
        ) : (
          <AlertCircle className="mx-auto h-6 w-6 text-coral-accent" aria-hidden="true" />
        )}
        <p className="text-sm text-ink">{message}</p>
        {status === "error" ? (
          <button
            type="button"
            onClick={() => void navigate({ to: "/" })}
            className="rounded-md bg-elevated border border-border px-4 py-1.5 text-xs font-medium text-ink hover:border-sky-accent/60"
          >
            Back to dashboard
          </button>
        ) : null}
      </div>
    </main>
  );
}
