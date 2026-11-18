import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../lib/api/client";
import { authApi } from "../lib/api/endpoints";

export function VerifyEmailPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const token = useMemo(
    () => new URLSearchParams(window.location.search).get("token")?.trim() ?? "",
    [],
  );

  const [status, setStatus] = useState<"loading" | "ok" | "error">(
    token ? "loading" : "error",
  );
  const [message, setMessage] = useState(
    token ? "Verifying your email address..." : "Missing email verification token.",
  );
  const [resendEmail, setResendEmail] = useState("");
  const [resendPending, setResendPending] = useState(false);
  const [resendFeedback, setResendFeedback] = useState<string | null>(null);
  const [resendError, setResendError] = useState<string | null>(null);

  const attemptedTokenRef = useRef<string | null>(null);
  const isMountedRef = useRef(true);
  const redirectTimerRef = useRef<number | null>(null);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (redirectTimerRef.current !== null) {
        window.clearTimeout(redirectTimerRef.current);
        redirectTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (!token || attemptedTokenRef.current === token) {
      return;
    }
    attemptedTokenRef.current = token;

    authApi
      .verifyEmail(token)
      .then(() => {
        if (!isMountedRef.current) return;
        void queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
        setStatus("ok");
        setMessage("Email verified! Redirecting to workspace...");
        redirectTimerRef.current = window.setTimeout(() => {
          redirectTimerRef.current = null;
          void navigate({ to: "/" });
        }, 700);
      })
      .catch((error) => {
        if (!isMountedRef.current) return;
        setStatus("error");
        setMessage(
          error instanceof ApiError
            ? error.message
            : "Invalid or expired email verification link.",
        );
      });
  }, [token, navigate, queryClient]);

  function handleContinueToWorkspace() {
    if (redirectTimerRef.current !== null) {
      window.clearTimeout(redirectTimerRef.current);
      redirectTimerRef.current = null;
    }
    void navigate({ to: "/" });
  }

  async function handleResendSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedEmail = resendEmail.trim();
    if (!trimmedEmail) {
      setResendError("Please enter your email address.");
      return;
    }

    setResendPending(true);
    setResendFeedback(null);
    setResendError(null);

    try {
      const response = await authApi.resendVerification(trimmedEmail);
      setResendFeedback(
        response.message || "A fresh verification link has been sent to your inbox.",
      );
    } catch (error) {
      if (error instanceof ApiError) {
        setResendError(error.message);
      } else {
        setResendError("Unable to resend verification email.");
      }
    } finally {
      setResendPending(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas p-6 text-ink">
      <div className="w-full max-w-sm space-y-5 rounded-xl border border-border bg-surface p-6">
        <div className="space-y-3 text-center">
          {status === "loading" ? (
            <Loader2
              className="mx-auto h-6 w-6 animate-spin text-sky-accent"
              aria-hidden="true"
            />
          ) : status === "ok" ? (
            <CheckCircle2 className="mx-auto h-6 w-6 text-teal-accent" aria-hidden="true" />
          ) : (
            <AlertCircle className="mx-auto h-6 w-6 text-coral-accent" aria-hidden="true" />
          )}
          <p className="text-sm text-ink">{message}</p>
        </div>

        {status === "ok" ? (
          <div className="text-center">
            <button
              type="button"
              onClick={handleContinueToWorkspace}
              className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 transition-colors"
            >
              Continue to workspace
            </button>
          </div>
        ) : null}

        {status === "error" ? (
          <div className="space-y-4 border-t border-border pt-4">
            {resendError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
              >
                <AlertCircle
                  className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent"
                  aria-hidden="true"
                />
                <span>{resendError}</span>
              </div>
            ) : null}

            {resendFeedback ? (
              <div
                role="status"
                className="flex items-start gap-2 rounded-md border border-teal-accent/40 bg-teal-accent/10 px-3 py-2.5 text-xs text-ink"
              >
                <CheckCircle2
                  className="mt-0.5 h-4 w-4 shrink-0 text-teal-accent"
                  aria-hidden="true"
                />
                <span>{resendFeedback}</span>
              </div>
            ) : null}

            <form onSubmit={handleResendSubmit} className="space-y-3">
              <label className="block space-y-1.5">
                <span className="block text-xs font-medium text-ink-muted">Email address</span>
                <input
                  type="email"
                  name="email"
                  autoComplete="email"
                  required
                  value={resendEmail}
                  onChange={(event) => setResendEmail(event.currentTarget.value)}
                  placeholder="explorer@example.com"
                  className="w-full rounded-md border border-border bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                />
              </label>

              <button
                type="submit"
                disabled={resendPending}
                className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-xs font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
              >
                {resendPending ? "Sending..." : "Resend verification email"}
              </button>
            </form>

            <div className="text-center">
              <Link
                to="/login"
                className="inline-block text-xs font-medium text-ink-muted hover:text-ink"
              >
                Back to sign in
              </Link>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
