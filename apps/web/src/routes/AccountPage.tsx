import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import {
  AlertCircle,
  ArrowLeft,
  Check,
  CheckCircle2,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  LogOut,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useId, useState } from "react";
import { AppHeader } from "../components/layout/AppHeader";
import { ApiError } from "../lib/api/client";
import { authApi } from "../lib/api/endpoints";
import type { AuthSessionOut, SessionInfoOut } from "../lib/api/types";

function formatSessionTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function AccountPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const currentPasswordInputId = useId();
  const newPasswordInputId = useId();
  const confirmPasswordInputId = useId();

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPasswords, setShowPasswords] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordStatus, setPasswordStatus] = useState<string | null>(null);

  const [sessionError, setSessionError] = useState<string | null>(null);
  const [sessionStatus, setSessionStatus] = useState<string | null>(null);
  const [revokingSessionId, setRevokingSessionId] = useState<string | null>(null);
  const [logoutAllPending, setLogoutAllPending] = useState(false);

  const sessionQuery = useQuery<SessionInfoOut>({
    queryKey: ["auth", "me"],
    queryFn: ({ signal }) => authApi.me(signal),
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });

  const sessionsQuery = useQuery<AuthSessionOut[]>({
    queryKey: ["auth", "sessions"],
    queryFn: ({ signal }) => authApi.listSessions(signal),
    enabled: sessionQuery.isSuccess,
    retry: false,
  });

  useEffect(() => {
    if (
      sessionQuery.isError &&
      sessionQuery.error instanceof ApiError &&
      sessionQuery.error.status === 401
    ) {
      void navigate({ to: "/login" });
    }
  }, [sessionQuery.isError, sessionQuery.error, navigate]);

  const changePasswordMutation = useMutation({
    mutationFn: (payload: { current_password: string; new_password: string }) =>
      authApi.changePassword(payload),
    onSuccess: (res) => {
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordError(null);
      setPasswordStatus(
        res.revoked_sessions_count > 0
          ? `Password updated. Signed out ${res.revoked_sessions_count} other active session${res.revoked_sessions_count === 1 ? "" : "s"}.`
          : "Password updated.",
      );
      void queryClient.invalidateQueries({ queryKey: ["auth", "sessions"] });
    },
    onError: (err) => {
      setPasswordStatus(null);
      setPasswordError(err instanceof ApiError ? err.message : "Failed to update password.");
    },
  });

  const revokeSessionMutation = useMutation({
    mutationFn: (sessionId: string) => authApi.revokeSession(sessionId),
    onMutate: (sessionId) => {
      setRevokingSessionId(sessionId);
      setSessionError(null);
      setSessionStatus(null);
    },
    onSuccess: () => {
      setSessionStatus("Session revoked.");
      void queryClient.invalidateQueries({ queryKey: ["auth", "sessions"] });
    },
    onError: (err) => {
      setSessionError(err instanceof ApiError ? err.message : "Failed to revoke session.");
    },
    onSettled: () => {
      setRevokingSessionId(null);
    },
  });

  async function handleLogout() {
    try {
      await authApi.logout();
    } finally {
      queryClient.clear();
      await navigate({ to: "/login" });
    }
  }

  async function handleLogoutAll() {
    setLogoutAllPending(true);
    setSessionError(null);
    setSessionStatus(null);

    try {
      await authApi.logoutAll();
      queryClient.clear();
      await navigate({ to: "/login" });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        queryClient.clear();
        await navigate({ to: "/login" });
        return;
      }
      setSessionError(
        error instanceof ApiError ? error.message : "Failed to sign out all devices.",
      );
    } finally {
      setLogoutAllPending(false);
    }
  }

  function handleChangePasswordSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPasswordError(null);
    setPasswordStatus(null);

    if (currentPassword.length === 0) {
      setPasswordError("Current password is required.");
      return;
    }
    if (newPassword.length < 12) {
      setPasswordError("New password must be at least 12 characters long.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("Passwords do not match.");
      return;
    }
    if (newPassword === currentPassword) {
      setPasswordError("New password must be different from your current password.");
      return;
    }

    changePasswordMutation.mutate({
      current_password: currentPassword,
      new_password: newPassword,
    });
  }

  const user = sessionQuery.data?.user ?? null;
  const sessions = sessionsQuery.data ?? [];
  const passwordMeetsLength = newPassword.length >= 12;
  const passwordsMatch = confirmPassword.length > 0 && newPassword === confirmPassword;
  const passwordsMismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;

  return (
    <div className="flex min-h-screen flex-col bg-canvas text-ink">
      <AppHeader
        sectionLabel="Account"
        user={user}
        onLogout={user ? () => void handleLogout() : undefined}
      />

      <main className="mx-auto w-full max-w-4xl flex-1 space-y-6 px-6 py-8">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-5">
          <div className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-tight text-ink">Account security</h1>
            <p className="text-sm text-ink-muted">
              {user
                ? `${user.display_name || user.email} (${user.email})`
                : "Manage your password and active sign-in sessions."}
            </p>
          </div>
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs font-medium text-ink hover:border-sky-accent/50 hover:text-sky-accent transition-colors"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
            <span>Back to projects</span>
          </Link>
        </div>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <section className="space-y-4 rounded-xl border border-border bg-surface p-6 lg:col-span-6">
            <div className="flex items-center gap-2.5">
              <span className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-sky-accent">
                <Lock className="h-4 w-4" aria-hidden="true" />
              </span>
              <div>
                <h2 className="text-base font-semibold text-ink">Change password</h2>
                <p className="text-xs text-ink-muted">
                  Updating your password signs out all other active devices.
                </p>
              </div>
            </div>

            {passwordError ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
              >
                <AlertCircle
                  className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent"
                  aria-hidden="true"
                />
                <span>{passwordError}</span>
              </div>
            ) : null}

            {passwordStatus ? (
              <div
                role="status"
                className="flex items-start gap-2 rounded-md border border-teal-accent/40 bg-teal-accent/10 px-3 py-2.5 text-xs text-ink"
              >
                <CheckCircle2
                  className="mt-0.5 h-4 w-4 shrink-0 text-teal-accent"
                  aria-hidden="true"
                />
                <span>{passwordStatus}</span>
              </div>
            ) : null}

            <form onSubmit={handleChangePasswordSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <label
                  htmlFor={currentPasswordInputId}
                  className="block text-xs font-medium text-ink-muted"
                >
                  Current password
                </label>
                <input
                  id={currentPasswordInputId}
                  type={showPasswords ? "text" : "password"}
                  name="current_password"
                  autoComplete="current-password"
                  required
                  value={currentPassword}
                  onChange={(event) => setCurrentPassword(event.currentTarget.value)}
                  placeholder="Enter your current password"
                  className="w-full rounded-md border border-border bg-canvas px-3 py-2 text-sm text-ink placeholder:text-ink-muted/60 focus:border-sky-accent focus:outline-none"
                />
              </div>

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
                    type={showPasswords ? "text" : "password"}
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
                    aria-label={showPasswords ? "Hide" : "Show"}
                    onClick={() => setShowPasswords((prev) => !prev)}
                    className="absolute inset-y-0 right-0 flex items-center px-3 text-ink-muted hover:text-ink transition-colors"
                  >
                    {showPasswords ? (
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
                  type={showPasswords ? "text" : "password"}
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
                disabled={changePasswordMutation.isPending}
                className="inline-flex w-full items-center justify-center rounded-md bg-sky-accent px-4 py-2 text-sm font-medium text-canvas hover:bg-sky-accent/90 disabled:opacity-60 transition-colors"
              >
                {changePasswordMutation.isPending ? "Updating password..." : "Update password"}
              </button>
            </form>
          </section>

          <section className="flex flex-col justify-between space-y-4 rounded-xl border border-border bg-surface p-6 lg:col-span-6">
            <div className="space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-center gap-2.5">
                  <span className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-elevated text-teal-accent">
                    <ShieldCheck className="h-4 w-4" aria-hidden="true" />
                  </span>
                  <div>
                    <h2 className="text-base font-semibold text-ink">Active sessions</h2>
                    <p className="text-xs text-ink-muted">
                      Review and revoke signed-in browser sessions.
                    </p>
                  </div>
                </div>

                <button
                  type="button"
                  disabled={logoutAllPending}
                  onClick={() => void handleLogoutAll()}
                  className="inline-flex items-center gap-1.5 rounded-md border border-coral-accent/40 bg-coral-accent/10 px-3 py-1.5 text-xs font-medium text-coral-accent hover:bg-coral-accent/20 disabled:opacity-60 transition-colors"
                >
                  <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>{logoutAllPending ? "Signing out..." : "Sign out all devices"}</span>
                </button>
              </div>

              {sessionError ? (
                <div
                  role="alert"
                  className="flex items-start gap-2 rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
                >
                  <AlertCircle
                    className="mt-0.5 h-4 w-4 shrink-0 text-amber-accent"
                    aria-hidden="true"
                  />
                  <span>{sessionError}</span>
                </div>
              ) : null}

              {sessionStatus ? (
                <div
                  role="status"
                  className="flex items-start gap-2 rounded-md border border-teal-accent/40 bg-teal-accent/10 px-3 py-2.5 text-xs text-ink"
                >
                  <CheckCircle2
                    className="mt-0.5 h-4 w-4 shrink-0 text-teal-accent"
                    aria-hidden="true"
                  />
                  <span>{sessionStatus}</span>
                </div>
              ) : null}

              {sessionsQuery.isLoading ? (
                <div className="flex items-center gap-2 py-6 text-xs text-ink-muted">
                  <Loader2 className="h-4 w-4 animate-spin text-sky-accent" aria-hidden="true" />
                  <span>Loading active sessions...</span>
                </div>
              ) : sessionsQuery.isError ? (
                <div
                  role="alert"
                  className="rounded-md border border-amber-accent/40 bg-amber-accent/10 px-3 py-2.5 text-xs text-ink"
                >
                  {sessionsQuery.error instanceof ApiError
                    ? sessionsQuery.error.message
                    : "Unable to load active sessions."}
                </div>
              ) : sessions.length === 0 ? (
                <p className="py-4 text-xs text-ink-muted">No active sessions found.</p>
              ) : (
                <ul className="space-y-2.5" aria-label="Active sessions">
                  {sessions.map((session) => (
                    <li
                      key={session.id}
                      className="flex items-center justify-between gap-3 rounded-lg border border-border bg-canvas px-3.5 py-3"
                    >
                      <div className="min-w-0 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="truncate font-mono text-xs font-medium text-ink">
                            {session.id.slice(0, 8)}
                          </span>
                          {session.is_current ? (
                            <span className="inline-flex items-center rounded-full border border-teal-accent/40 bg-teal-accent/10 px-2 py-0.5 text-[11px] font-medium text-teal-accent">
                              Current session
                            </span>
                          ) : null}
                        </div>
                        <p className="text-[11px] text-ink-muted">
                          Signed in {formatSessionTimestamp(session.created_at)} · Expires{" "}
                          {formatSessionTimestamp(session.expires_at)}
                        </p>
                      </div>

                      {!session.is_current ? (
                        <button
                          type="button"
                          disabled={revokingSessionId === session.id}
                          onClick={() => revokeSessionMutation.mutate(session.id)}
                          className="shrink-0 rounded-md border border-border bg-elevated px-2.5 py-1 text-xs font-medium text-ink hover:border-coral-accent/60 hover:text-coral-accent disabled:opacity-60 transition-colors"
                        >
                          {revokingSessionId === session.id ? "Revoking..." : "Revoke"}
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
