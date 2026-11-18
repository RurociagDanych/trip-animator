import { Link } from "@tanstack/react-router";
import { AlertCircle, Check, CheckCircle2, Eye, EyeOff, Lock } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { ApiError } from "../lib/api/client";
import { authApi } from "../lib/api/endpoints";

export function ResetPasswordPage() {
  const newPasswordInputId = useId();
  const confirmPasswordInputId = useId();

  const token = useMemo(
    () => new URLSearchParams(window.location.search).get("token")?.trim() ?? "",
    [],
  );

  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [resetComplete, setResetComplete] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const passwordMeetsLength = newPassword.length >= 12;
  const passwordsMatch = confirmPassword.length > 0 && newPassword === confirmPassword;
  const passwordsMismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);

    if (newPassword.length < 12) {
      setErrorMessage("Password must be at least 12 characters long.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setErrorMessage("Passwords do not match.");
      return;
    }

    setPending(true);

    try {
      const response = await authApi.resetPassword({
        token,
        new_password: newPassword,
      });
      setNewPassword("");
      setConfirmPassword("");
      setSuccessMessage(
        response.message || "Your password has been updated. Please sign in again.",
      );
      setResetComplete(true);
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Unable to reset password.");
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas p-6 text-ink">
      <div className="w-full max-w-sm space-y-5 rounded-xl border border-border bg-surface p-6">
        {!token ? (
          <div className="space-y-4 text-center">
            <AlertCircle className="mx-auto h-6 w-6 text-coral-accent" aria-hidden="true" />
            <div className="space-y-1.5">
              <h1 className="text-lg font-semibold tracking-tight text-ink">
                Missing password reset token.
              </h1>
              <p className="text-xs text-ink-muted">
                Please request a fresh password reset link to continue.
              </p>
            </div>
            <div className="space-y-2 pt-1">
              <Link
                to="/forgot-password"
                className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-xs font-medium text-canvas hover:bg-sky-accent/90 transition-colors"
              >
                Request a new reset link
              </Link>
              <Link
                to="/login"
                className="inline-block text-xs font-medium text-ink-muted hover:text-ink"
              >
                Back to sign in
              </Link>
            </div>
          </div>
        ) : resetComplete ? (
          <div className="space-y-4 text-center">
            <CheckCircle2 className="mx-auto h-7 w-7 text-teal-accent" aria-hidden="true" />
            <div className="space-y-1.5">
              <h1 className="text-xl font-semibold tracking-tight text-ink">
                Password reset complete
              </h1>
              <p className="text-sm leading-relaxed text-ink-muted">{successMessage}</p>
            </div>
            <Link
              to="/login"
              className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 transition-colors"
            >
              Sign in with new password
            </Link>
          </div>
        ) : (
          <>
            <div className="space-y-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-elevated text-sky-accent">
                <Lock className="h-5 w-5" aria-hidden="true" />
              </div>
              <div className="space-y-1.5">
                <h1 className="text-xl font-semibold tracking-tight text-ink">
                  Set a new password
                </h1>
                <p className="text-sm text-ink-muted">
                  Choose a strong password of at least 12 characters for your account.
                </p>
              </div>
            </div>

            {errorMessage ? (
              <div
                role="alert"
                className="space-y-2 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
              >
                <div className="flex items-start gap-2">
                  <AlertCircle
                    className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent"
                    aria-hidden="true"
                  />
                  <span>{errorMessage}</span>
                </div>
                <Link
                  to="/forgot-password"
                  className="inline-block font-medium text-sky-accent hover:text-sky-accent/80"
                >
                  Request a new reset link
                </Link>
              </div>
            ) : null}

            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <label
                  htmlFor={newPasswordInputId}
                  className="block text-xs font-medium text-ink-muted"
                >
                  New password
                </label>
                <div className="relative">
                  <input
                    id={newPasswordInputId}
                    type={showPassword ? "text" : "password"}
                    name="new_password"
                    autoComplete="new-password"
                    required
                    minLength={12}
                    value={newPassword}
                    onChange={(event) => setNewPassword(event.currentTarget.value)}
                    placeholder="Minimum 12 characters"
                    className="w-full rounded-md border border-border bg-canvas py-2 pl-3 pr-10 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                  />
                  <button
                    type="button"
                    aria-label={showPassword ? "Hide" : "Show"}
                    onClick={() => setShowPassword((prev) => !prev)}
                    className="absolute inset-y-0 right-0 flex items-center px-3 text-ink-muted hover:text-ink transition-colors"
                  >
                    {showPassword ? (
                      <EyeOff className="h-4 w-4" aria-hidden="true" />
                    ) : (
                      <Eye className="h-4 w-4" aria-hidden="true" />
                    )}
                  </button>
                </div>
                <p
                  className={`flex items-center gap-1.5 text-xs ${
                    passwordMeetsLength ? "text-teal-accent" : "text-ink-muted"
                  }`}
                >
                  <Check
                    className={`h-3.5 w-3.5 ${passwordMeetsLength ? "opacity-100" : "opacity-40"}`}
                    aria-hidden="true"
                  />
                  <span>12+ characters required</span>
                </p>
              </div>

              <div className="space-y-1.5">
                <label
                  htmlFor={confirmPasswordInputId}
                  className="block text-xs font-medium text-ink-muted"
                >
                  Confirm new password
                </label>
                <input
                  id={confirmPasswordInputId}
                  type={showPassword ? "text" : "password"}
                  name="confirm_new_password"
                  autoComplete="new-password"
                  required
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.currentTarget.value)}
                  placeholder="Re-enter your new password"
                  className="w-full rounded-md border border-border bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                />
                {passwordsMismatch ? (
                  <p className="text-xs text-amber-accent">Passwords do not match</p>
                ) : passwordsMatch ? (
                  <p className="text-xs text-teal-accent">Passwords match</p>
                ) : null}
              </div>

              <button
                type="submit"
                disabled={pending}
                className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
              >
                {pending ? "Resetting password..." : "Reset password"}
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
