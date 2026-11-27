import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { createAppRouter } from "../router";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAtRoute(initialPath: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const history = createMemoryHistory({ initialEntries: [initialPath] });
  const router = createAppRouter(history);
  render(<App router={router} queryClient={queryClient} />);
  return { router, queryClient, history };
}

describe("Google OIDC sign-in, set password, and self-service account deletion", () => {
  const originalLocation = window.location;

  beforeEach(() => {
    if (!HTMLDialogElement.prototype.showModal) {
      HTMLDialogElement.prototype.showModal = function showModal() {
        this.setAttribute("open", "");
      };
    }
    if (!HTMLDialogElement.prototype.close) {
      HTMLDialogElement.prototype.close = function close() {
        this.removeAttribute("open");
        this.dispatchEvent(new Event("close"));
      };
    }
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: originalLocation,
    });
    window.history.replaceState({}, "", "/");
  });

  it("shows Continue with Google button only when enabled and starts OAuth redirect", async () => {
    let googleEnabled = false;
    let startCalled = false;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({
          dev_auth_enabled: false,
          registration_enabled: true,
          turnstile_site_key: null,
          google_auth_enabled: googleEnabled,
          strava_configured: false,
        });
      }
      if (url.includes("/api/v1/auth/google/start")) {
        startCalled = true;
        return jsonResponse({
          authorize_url: "https://accounts.google.com/o/oauth2/v2/auth?state=test-state",
        });
      }
      return jsonResponse({ detail: "Not authenticated" }, 401);
    });

    renderAtRoute("/login");
    await screen.findByRole("button", { name: /^Sign in$/i });
    expect(screen.queryByRole("button", { name: /Continue with Google/i })).not.toBeInTheDocument();

    cleanup();
    googleEnabled = true;
    const assignMock = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, assign: assignMock },
    });

    renderAtRoute("/login");
    const googleBtn = await screen.findByRole("button", { name: /Continue with Google/i });
    fireEvent.click(googleBtn);

    await waitFor(() => {
      expect(startCalled).toBe(true);
      expect(assignMock).toHaveBeenCalledWith(
        "https://accounts.google.com/o/oauth2/v2/auth?state=test-state",
      );
    });
  });

  it("exchanges Google callback code and state and surfaces server errors", async () => {
    let callbackPayload: Record<string, unknown> | null = null;
    let shouldFail = false;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      if (url.includes("/api/v1/auth/google/callback")) {
        callbackPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        if (shouldFail) {
          return jsonResponse(
            {
              detail:
                "An unverified account with this email already exists. Please verify your email.",
            },
            409,
          );
        }
        return jsonResponse({
          user: {
            id: "u-google-1",
            email: "rider@example.com",
            display_name: "Rider",
            strava_connected: false,
            has_password: false,
          },
          csrf_token: "csrf-google",
          dev_auth_enabled: false,
        });
      }
      if (url.includes("/api/v1/auth/me")) {
        return jsonResponse({
          user: {
            id: "u-google-1",
            email: "rider@example.com",
            display_name: "Rider",
            strava_connected: false,
            has_password: false,
          },
          csrf_token: "csrf-google",
          dev_auth_enabled: false,
        });
      }
      if (url.includes("/api/v1/projects")) {
        return jsonResponse([]);
      }
      return jsonResponse({ detail: "Not found" }, 404);
    });

    window.history.replaceState({}, "", "/auth/google/callback?code=g-code-1&state=g-state-1");
    renderAtRoute("/auth/google/callback");

    await waitFor(() => {
      expect(callbackPayload).toEqual({ code: "g-code-1", state: "g-state-1" });
    });

    cleanup();
    shouldFail = true;
    window.history.replaceState({}, "", "/auth/google/callback?code=g-code-2&state=g-state-2");
    renderAtRoute("/auth/google/callback");

    expect(
      await screen.findByText(/An unverified account with this email already exists/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Back to sign in/i })).toBeInTheDocument();
  });

  it("renders Set a password and DELETE confirmation modal for Google-only accounts", async () => {
    let setPasswordPayload: Record<string, unknown> | null = null;
    let deletePayload: Record<string, unknown> | null = null;
    let hasPassword = false;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";
      if (url.includes("/api/v1/auth/me") && method === "GET") {
        return jsonResponse({
          user: {
            id: "u-google-2",
            email: "google-only@example.com",
            display_name: "Google Rider",
            strava_connected: false,
            has_password: hasPassword,
          },
          csrf_token: "csrf-tok-1",
          dev_auth_enabled: false,
        });
      }
      if (url.includes("/api/v1/auth/sessions")) {
        return jsonResponse([
          {
            id: "sess-1",
            created_at: "2026-11-26T10:00:00Z",
            expires_at: "2026-12-03T10:00:00Z",
            is_current: true,
          },
        ]);
      }
      if (url.includes("/api/v1/auth/set-password") && method === "POST") {
        setPasswordPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        hasPassword = true;
        return jsonResponse({ ok: true, revoked_sessions_count: 0 });
      }
      if (url.includes("/api/v1/auth/me") && method === "DELETE") {
        deletePayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return new Response(null, { status: 204 });
      }
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({
          dev_auth_enabled: false,
          registration_enabled: true,
          turnstile_site_key: null,
          google_auth_enabled: true,
          strava_configured: false,
        });
      }
      return jsonResponse({ detail: "Not authenticated" }, 401);
    });

    const { router } = renderAtRoute("/account");

    expect(await screen.findByRole("heading", { name: /Set a password/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Current password/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^Delete account$/i }));
    expect(
      await screen.findByRole("heading", { name: /Delete your account\?/i }),
    ).toBeInTheDocument();

    const confirmInput = screen.getByLabelText(/Type DELETE to confirm/i);
    fireEvent.change(confirmInput, { target: { value: "DELETE" } });
    fireEvent.click(screen.getByRole("button", { name: /Permanently delete account/i }));

    await waitFor(() => {
      expect(deletePayload).toEqual({ confirmation: "DELETE" });
      expect(router.state.location.pathname).toBe("/login");
    });

    cleanup();
    hasPassword = false;
    renderAtRoute("/account");

    expect(await screen.findByRole("heading", { name: /Set a password/i })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^New password$/i), {
      target: { value: "new-password-for-google-123" },
    });
    fireEvent.change(screen.getByLabelText(/Confirm new password/i), {
      target: { value: "new-password-for-google-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Set password$/i }));

    await waitFor(() => {
      expect(setPasswordPayload).toEqual({ new_password: "new-password-for-google-123" });
    });
  });

  it("requires current password in the delete dialog when user has a password", async () => {
    let deletePayload: Record<string, unknown> | null = null;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";
      if (url.includes("/api/v1/auth/me") && method === "GET") {
        return jsonResponse({
          user: {
            id: "u-pw-1",
            email: "pw@example.com",
            display_name: "PW Rider",
            strava_connected: false,
            has_password: true,
          },
          csrf_token: "csrf-tok-2",
          dev_auth_enabled: false,
        });
      }
      if (url.includes("/api/v1/auth/sessions")) {
        return jsonResponse([]);
      }
      if (url.includes("/api/v1/auth/me") && method === "DELETE") {
        deletePayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return new Response(null, { status: 204 });
      }
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({
          dev_auth_enabled: false,
          registration_enabled: true,
          turnstile_site_key: null,
          google_auth_enabled: false,
          strava_configured: false,
        });
      }
      return jsonResponse({ detail: "Not authenticated" }, 401);
    });

    const { router } = renderAtRoute("/account");

    expect(await screen.findByRole("heading", { name: /Change password/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Delete account$/i }));

    expect(
      await screen.findByRole("heading", { name: /Delete your account\?/i }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Confirm with current password/i), {
      target: { value: "correct-horse-battery-staple" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Permanently delete account/i }));

    await waitFor(() => {
      expect(deletePayload).toEqual({ current_password: "correct-horse-battery-staple" });
      expect(router.state.location.pathname).toBe("/login");
    });
  });
});
