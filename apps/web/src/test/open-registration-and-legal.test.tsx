import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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

describe("Open registration, Turnstile, and legal pages", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    delete (window as unknown as { turnstile?: unknown }).turnstile;
  });

  it("renders Privacy Policy and Terms of Service routes with cross-links", async () => {
    renderAtRoute("/privacy");
    expect(await screen.findByRole("heading", { name: /Privacy Policy/i, level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Terms of Service/i })).toHaveAttribute("href", "/terms");
    expect(screen.getByRole("link", { name: /Back to sign in/i })).toHaveAttribute("href", "/login");

    cleanup();
    renderAtRoute("/terms");
    expect(await screen.findByRole("heading", { name: /Terms of Service/i, level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Privacy Policy/i })).toHaveAttribute("href", "/privacy");
  });

  it("registers without an invite code and links to Privacy Policy and Terms from login", async () => {
    let capturedPayload: Record<string, unknown> | null = null;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({
          dev_auth_enabled: false,
          registration_enabled: true,
          turnstile_site_key: null,
          strava_configured: false,
        });
      }
      if (url.includes("/api/v1/auth/register") && method === "POST") {
        capturedPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return jsonResponse(
          {
            status: "verification_required",
            email: "open@example.com",
            message: "If this email address is eligible, a verification link has been sent.",
          },
          202,
        );
      }
      return jsonResponse({ detail: "Not authenticated" }, 401);
    });

    renderAtRoute("/login");
    expect(await screen.findByRole("link", { name: /Privacy Policy/i })).toHaveAttribute(
      "href",
      "/privacy",
    );
    expect(screen.getByRole("link", { name: /Terms of Service/i })).toHaveAttribute("href", "/terms");

    fireEvent.click(await screen.findByRole("tab", { name: /Create account/i }));
    expect(screen.queryByLabelText(/Invite code/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Email address/i), {
      target: { value: "open@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/Display name/i), {
      target: { value: "Open Rider" },
    });
    fireEvent.change(screen.getByLabelText(/^Password$/i), {
      target: { value: "open-signup-password-123" },
    });
    fireEvent.change(screen.getByLabelText(/Confirm password/i), {
      target: { value: "open-signup-password-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

    expect(await screen.findByRole("heading", { name: /Check your email/i })).toBeInTheDocument();
    expect(capturedPayload).toEqual({
      email: "open@example.com",
      display_name: "Open Rider",
      password: "open-signup-password-123",
    });
  });

  it("requires and submits Turnstile token on register and forgot-password when configured", async () => {
    let registerPayload: Record<string, unknown> | null = null;
    let forgotPayload: Record<string, unknown> | null = null;
    const renderCalls: Array<{ callback?: (token: string) => void }> = [];

    (
      window as unknown as {
        turnstile?: {
          render: (
            el: HTMLElement,
            opts: { sitekey: string; callback?: (token: string) => void },
          ) => string;
          remove: (id: string) => void;
        };
      }
    ).turnstile = {
      render: (_el, opts) => {
        renderCalls.push(opts);
        return "widget-1";
      },
      remove: vi.fn(),
    };

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";
      if (url.includes("/api/v1/public-config")) {
        return jsonResponse({
          dev_auth_enabled: false,
          registration_enabled: true,
          turnstile_site_key: "0x4AAAA-test-site",
          strava_configured: false,
        });
      }
      if (url.includes("/api/v1/auth/register") && method === "POST") {
        registerPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return jsonResponse(
          {
            status: "verification_required",
            email: "ts@example.com",
            message: "Check your inbox.",
          },
          202,
        );
      }
      if (url.includes("/api/v1/auth/forgot-password") && method === "POST") {
        forgotPayload = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        return jsonResponse({ status: "accepted", message: "Reset sent." }, 202);
      }
      return jsonResponse({ detail: "Not authenticated" }, 401);
    });

    renderAtRoute("/login");
    fireEvent.click(await screen.findByRole("tab", { name: /Create account/i }));

    await waitFor(() => {
      expect(renderCalls.length).toBe(1);
    });

    fireEvent.change(screen.getByLabelText(/Email address/i), {
      target: { value: "ts@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/Display name/i), {
      target: { value: "TS Rider" },
    });
    fireEvent.change(screen.getByLabelText(/^Password$/i), {
      target: { value: "turnstile-password-123" },
    });
    fireEvent.change(screen.getByLabelText(/Confirm password/i), {
      target: { value: "turnstile-password-123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/verification challenge/i);
    expect(registerPayload).toBeNull();

    act(() => {
      renderCalls[0]?.callback?.("cf-turnstile-token-abc");
    });
    fireEvent.click(screen.getByRole("button", { name: /^Create account$/i }));

    expect(await screen.findByRole("heading", { name: /Check your email/i })).toBeInTheDocument();
    expect(registerPayload).toEqual({
      email: "ts@example.com",
      display_name: "TS Rider",
      password: "turnstile-password-123",
      turnstile_token: "cf-turnstile-token-abc",
    });

    cleanup();
    renderAtRoute("/forgot-password");
    await waitFor(() => {
      expect(renderCalls.length).toBe(2);
    });

    fireEvent.change(screen.getByLabelText(/Email address/i), {
      target: { value: "ts@example.com" },
    });
    act(() => {
      renderCalls[1]?.callback?.("cf-turnstile-fp-xyz");
    });
    fireEvent.click(screen.getByRole("button", { name: /Send reset link/i }));

    expect(await screen.findByRole("heading", { name: /Check your email/i })).toBeInTheDocument();
    expect(forgotPayload).toEqual({
      email: "ts@example.com",
      turnstile_token: "cf-turnstile-fp-xyz",
    });
  });
});
