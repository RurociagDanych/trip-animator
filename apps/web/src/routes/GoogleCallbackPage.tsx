import { useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "@tanstack/react-router";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../lib/api/client";
import { authApi } from "../lib/api/endpoints";

export function GoogleCallbackPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const submittedRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const oauthParams = useMemo(() => {
    const rawSearch = location.searchStr || window.location.search;
    const params = new URLSearchParams(rawSearch);
    return {
      code: params.get("code"),
      state: params.get("state"),
      oauthError: params.get("error"),
    };
  }, [location.searchStr]);

  const initialError = oauthParams.oauthError
    ? "Google sign-in was cancelled or denied."
    : !oauthParams.code || !oauthParams.state
      ? "Missing Google authorization code or state parameter."
      : null;

  const [status, setStatus] = useState<"loading" | "ok" | "error">(
    initialError ? "error" : "loading",
  );
  const [message, setMessage] = useState(
    initialError ?? "Completing Google sign-in...",
  );

  useEffect(() => {
    if (initialError || !oauthParams.code || !oauthParams.state || submittedRef.current) {
      return;
    }
    submittedRef.current = true;

    authApi
      .googleCallback({ code: oauthParams.code, state: oauthParams.state })
      .then(async () => {
        if (!mountedRef.current) return;
        await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
        if (!mountedRef.current) return;
        setStatus("ok");
        setMessage("Signed in with Google. Redirecting to workspace...");
        await navigate({ to: "/" });
      })
      .catch((err) => {
        if (!mountedRef.current) return;
        setStatus("error");
        setMessage(err instanceof ApiError ? err.message : "Google sign-in failed.");
      });
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
            onClick={() => void navigate({ to: "/login" })}
            className="rounded-md border border-border bg-elevated px-4 py-1.5 text-xs font-medium text-ink hover:border-sky-accent/60 transition-colors"
          >
            Back to sign in
          </button>
        ) : null}
      </div>
    </main>
  );
}
