import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Compass,
  Eye,
  EyeOff,
  KeyRound,
  Lock,
  Mail,
  UserPlus,
} from "lucide-react";
import { useId, useState } from "react";
import { ApiError } from "../lib/api/client";
import { authApi, systemApi } from "../lib/api/endpoints";
import type { PublicConfigOut } from "../lib/api/types";

type AuthMode = "signin" | "register";

export function LoginPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const passwordInputId = useId();
  const confirmPasswordInputId = useId();

  const [mode, setMode] = useState<AuthMode>("signin");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [verificationEmail, setVerificationEmail] = useState<string | null>(null);
  const [unverifiedLoginEmail, setUnverifiedLoginEmail] = useState<string | null>(null);
  const [resendPending, setResendPending] = useState(false);
  const [resendFeedback, setResendFeedback] = useState<string | null>(null);

  const publicConfigQuery = useQuery<PublicConfigOut>({
    queryKey: ["system", "public-config"],
    queryFn: ({ signal }) => systemApi.publicConfig(signal),
    retry: false,
  });

  const devAuthEnabled = publicConfigQuery.data?.dev_auth_enabled === true;
  const registrationEnabled = publicConfigQuery.data?.registration_enabled === true;
  const activeMode: AuthMode = registrationEnabled ? mode : "signin";
  const passwordMeetsLength = password.length >= 12;
  const passwordsMatch = confirmPassword.length > 0 && password === confirmPassword;
  const passwordsMismatch = confirmPassword.length > 0 && password !== confirmPassword;

  function switchMode(nextMode: AuthMode) {
    setMode(nextMode);
    setErrorMessage(null);
    setResendFeedback(null);
    setUnverifiedLoginEmail(null);
  }

  function handleBackToSignIn() {
    setVerificationEmail(null);
    setMode("signin");
    setErrorMessage(null);
    setResendFeedback(null);
    setUnverifiedLoginEmail(null);
  }

  async function handleResendVerification(targetEmail: string) {
    const trimmedEmail = targetEmail.trim();
    if (!trimmedEmail) {
      return;
    }

    setResendPending(true);
    setResendFeedback(null);
    setErrorMessage(null);

    try {
      const response = await authApi.resendVerification(trimmedEmail);
      setResendFeedback(
        response.message || "A fresh verification link has been sent to your inbox.",
      );
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("Unable to resend verification email.");
      }
    } finally {
      setResendPending(false);
    }
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setResendFeedback(null);
    setUnverifiedLoginEmail(null);

    if (activeMode === "register") {
      if (displayName.trim().length === 0) {
        setErrorMessage("Display name is required.");
        return;
      }
      if (password.length < 12) {
        setErrorMessage("Password must be at least 12 characters long.");
        return;
      }
      if (password !== confirmPassword) {
        setErrorMessage("Passwords do not match.");
        return;
      }
      if (inviteCode.trim().length === 0) {
        setErrorMessage("Invite code is required.");
        return;
      }
    }

    setPending(true);

    try {
      if (activeMode === "register") {
        const res = await authApi.register({
          email: email.trim(),
          display_name: displayName.trim(),
          password,
          invite_code: inviteCode.trim(),
        });
        setVerificationEmail(res.email || email.trim());
        setPassword("");
        setConfirmPassword("");
        return;
      }

      await authApi.login(email.trim(), password);
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      await navigate({ to: "/" });
    } catch (error) {
      if (error instanceof ApiError) {
        setErrorMessage(error.message);
        if (
          activeMode === "signin" &&
          error.status === 403 &&
          error.message.toLowerCase().includes("not verified")
        ) {
          setUnverifiedLoginEmail(email.trim());
        }
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
          {verificationEmail !== null ? (
            <div className="space-y-5 rounded-xl border border-border bg-surface p-6">
              <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-elevated text-sky-accent">
                <Mail className="h-5 w-5" aria-hidden="true" />
              </div>

              <div className="space-y-2">
                <h2 className="text-xl font-semibold tracking-tight text-ink">Check your email</h2>
                <p className="text-sm leading-relaxed text-ink-muted">
                  We sent a verification link to{" "}
                  <span className="font-medium text-ink">{verificationEmail}</span>. Click the link
                  in your email to verify your account before signing in.
                </p>
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

              <div className="space-y-2.5">
                <button
                  type="button"
                  disabled={resendPending}
                  onClick={() => void handleResendVerification(verificationEmail)}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
                >
                  {resendPending ? "Sending..." : "Resend verification email"}
                </button>
                <button
                  type="button"
                  onClick={handleBackToSignIn}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-border bg-elevated px-4 py-2 text-xs font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent transition-colors"
                >
                  Back to sign in
                </button>
              </div>
            </div>
          ) : (
            <>
              {registrationEnabled ? (
                <div
                  role="tablist"
                  aria-label="Authentication mode"
                  className="grid grid-cols-2 gap-1 rounded-lg border border-border bg-surface p-1"
                >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeMode === "signin"}
                    onClick={() => switchMode("signin")}
                    className={
                      activeMode === "signin"
                        ? "rounded-md bg-elevated px-3 py-1.5 text-xs font-medium text-ink shadow-sm transition-colors"
                        : "rounded-md px-3 py-1.5 text-xs font-medium text-ink-muted hover:text-ink transition-colors"
                    }
                  >
                    Sign in
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={activeMode === "register"}
                    onClick={() => switchMode("register")}
                    className={
                      activeMode === "register"
                        ? "rounded-md bg-elevated px-3 py-1.5 text-xs font-medium text-ink shadow-sm transition-colors"
                        : "rounded-md px-3 py-1.5 text-xs font-medium text-ink-muted hover:text-ink transition-colors"
                    }
                  >
                    Create account
                  </button>
                </div>
              ) : null}

              <div className="space-y-1.5">
                <h2 className="text-xl font-semibold tracking-tight text-ink">
                  {activeMode === "register" ? "Create your account" : "Sign in to workspace"}
                </h2>
                <p className="text-sm text-ink-muted">
                  {activeMode === "register"
                    ? "Enter your invite code to set up your personal workspace."
                    : "Access your multi-day GPX and Strava route projects."}
                </p>
              </div>

              {errorMessage ? (
                <div
                  role="alert"
                  className="space-y-2.5 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
                >
                  <div className="flex items-start gap-2">
                    <AlertCircle
                      className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent"
                      aria-hidden="true"
                    />
                    <span>{errorMessage}</span>
                  </div>
                  {unverifiedLoginEmail ? (
                    <button
                      type="button"
                      disabled={resendPending}
                      onClick={() => void handleResendVerification(unverifiedLoginEmail)}
                      className="inline-flex items-center justify-center rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent disabled:opacity-60 transition-colors"
                    >
                      {resendPending ? "Sending..." : "Resend verification email"}
                    </button>
                  ) : null}
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

                {activeMode === "register" ? (
                  <label className="block space-y-1.5">
                    <span className="block text-xs font-medium text-ink-muted">Display name</span>
                    <input
                      type="text"
                      name="display_name"
                      autoComplete="name"
                      required
                      maxLength={120}
                      value={displayName}
                      onChange={(event) => setDisplayName(event.currentTarget.value)}
                      placeholder="Alpine Explorer"
                      className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                    />
                  </label>
                ) : null}

                <div className="space-y-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <label
                      htmlFor={passwordInputId}
                      className="block text-xs font-medium text-ink-muted"
                    >
                      Password
                    </label>
                    {activeMode === "signin" ? (
                      <Link
                        to="/forgot-password"
                        className="text-xs font-medium text-sky-accent hover:text-sky-accent/80 transition-colors"
                      >
                        Forgot password?
                      </Link>
                    ) : null}
                  </div>
                  <div className="relative">
                    <input
                      id={passwordInputId}
                      type={showPassword ? "text" : "password"}
                      name="password"
                      autoComplete={activeMode === "register" ? "new-password" : "current-password"}
                      required
                      minLength={activeMode === "register" ? 12 : undefined}
                      value={password}
                      onChange={(event) => setPassword(event.currentTarget.value)}
                      placeholder={
                        activeMode === "register" ? "Minimum 12 characters" : "Enter your password"
                      }
                      className="w-full rounded-md border border-border bg-surface py-2 pl-3 pr-10 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
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
                  {activeMode === "register" ? (
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
                  ) : null}
                </div>

                {activeMode === "register" ? (
                  <>
                    <div className="space-y-1.5">
                      <label
                        htmlFor={confirmPasswordInputId}
                        className="block text-xs font-medium text-ink-muted"
                      >
                        Confirm password
                      </label>
                      <input
                        id={confirmPasswordInputId}
                        type={showPassword ? "text" : "password"}
                        name="confirm_password"
                        autoComplete="new-password"
                        required
                        value={confirmPassword}
                        onChange={(event) => setConfirmPassword(event.currentTarget.value)}
                        placeholder="Re-enter your password"
                        className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                      />
                      {passwordsMismatch ? (
                        <p className="text-xs text-amber-accent">Passwords do not match</p>
                      ) : passwordsMatch ? (
                        <p className="text-xs text-teal-accent">Passwords match</p>
                      ) : null}
                    </div>

                    <label className="block space-y-1.5">
                      <span className="block text-xs font-medium text-ink-muted">Invite code</span>
                      <input
                        type="text"
                        name="invite_code"
                        autoComplete="off"
                        required
                        value={inviteCode}
                        onChange={(event) => setInviteCode(event.currentTarget.value)}
                        placeholder="Enter invite code"
                        className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                      />
                    </label>
                  </>
                ) : null}

                <button
                  type="submit"
                  disabled={pending}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
                >
                  {activeMode === "register" ? (
                    <>
                      <UserPlus className="h-4 w-4" aria-hidden="true" />
                      <span>{pending ? "Creating account..." : "Create account"}</span>
                    </>
                  ) : (
                    <>
                      <Lock className="h-4 w-4" aria-hidden="true" />
                      <span>{pending ? "Signing in..." : "Sign in"}</span>
                    </>
                  )}
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
            </>
          )}

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
