import { Link } from "@tanstack/react-router";
import { AlertCircle, KeyRound, Mail } from "lucide-react";
import { useId, useState } from "react";
import { ApiError } from "../lib/api/client";
import { authApi } from "../lib/api/endpoints";

export function ForgotPasswordPage() {
  const emailInputId = useId();
  const [email, setEmail] = useState("");
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [submittedEmail, setSubmittedEmail] = useState<string | null>(null);
  const [confirmationMessage, setConfirmationMessage] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setErrorMessage("Please enter your email address.");
      return;
    }

    setPending(true);
    setErrorMessage(null);

    try {
      const response = await authApi.forgotPassword(trimmedEmail);
      setSubmittedEmail(trimmedEmail);
      setConfirmationMessage(
        response.message ||
          "If an active account exists for that email address, a password reset link has been sent.",
      );
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Unable to process password reset request.");
      }
    } finally {
      setPending(false);
    }
  }

  function handleSendAnother() {
    setSubmittedEmail(null);
    setConfirmationMessage(null);
    setErrorMessage(null);
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas p-6 text-ink">
      <div className="w-full max-w-sm space-y-5 rounded-xl border border-border bg-surface p-6">
        {submittedEmail !== null ? (
          <div className="space-y-5">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-elevated text-sky-accent">
              <Mail className="h-5 w-5" aria-hidden="true" />
            </div>

            <div className="space-y-2">
              <h1 className="text-xl font-semibold tracking-tight text-ink">Check your email</h1>
              <p className="text-sm leading-relaxed text-ink-muted">
                {confirmationMessage}{" "}
                <span className="font-medium text-ink">{submittedEmail}</span>
              </p>
            </div>

            <div className="space-y-2.5">
              <button
                type="button"
                onClick={handleSendAnother}
                className="inline-flex w-full items-center justify-center rounded-md border border-border bg-elevated px-4 py-2 text-xs font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent transition-colors"
              >
                Send another link
              </button>
              <Link
                to="/login"
                className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-xs font-medium text-canvas hover:bg-sky-accent/90 transition-colors"
              >
                Back to sign in
              </Link>
            </div>
          </div>
        ) : (
          <>
            <div className="space-y-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-elevated text-sky-accent">
                <KeyRound className="h-5 w-5" aria-hidden="true" />
              </div>
              <div className="space-y-1.5">
                <h1 className="text-xl font-semibold tracking-tight text-ink">
                  Reset your password
                </h1>
                <p className="text-sm leading-relaxed text-ink-muted">
                  Enter your email address and we will send you a link to choose a new password.
                </p>
              </div>
            </div>

            {errorMessage ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
              >
                <AlertCircle
                  className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent"
                  aria-hidden="true"
                />
                <span>{errorMessage}</span>
              </div>
            ) : null}

            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <label
                  htmlFor={emailInputId}
                  className="block text-xs font-medium text-ink-muted"
                >
                  Email address
                </label>
                <input
                  id={emailInputId}
                  type="email"
                  name="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.currentTarget.value)}
                  placeholder="explorer@example.com"
                  className="w-full rounded-md border border-border bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                />
              </div>

              <button
                type="submit"
                disabled={pending}
                className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
              >
                {pending ? "Sending..." : "Send reset link"}
              </button>
            </form>

            <div className="text-center">
              <Link to="/login" className="text-xs font-medium text-ink-muted hover:text-ink">
                Back to sign in
              </Link>
            </div>
          </>
        )}
      </div>
    </main>
  );
}
