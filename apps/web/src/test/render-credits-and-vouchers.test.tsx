import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ExportPanel } from "../components/editor/ExportPanel";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { calculateRenderCreditQuote } from "../lib/credits";
import { createAppRouter } from "../router";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("Render Credits pricing, AccountPage ledger, and ExportPanel voucher UX", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("matches canonical Render Credit pricing worked examples", () => {
    const baseSpec = {
      ...createDefaultRenderSpecV2(),
      resolution: "1920x1080" as const,
      fps: 30 as const,
      duration_seconds: 60,
      intro_hold_seconds: 0,
      outro_hold_seconds: 0,
      terrain_3d: false,
      show_photos: false,
    };
    expect(calculateRenderCreditQuote(baseSpec).credits).toBe(100);
    expect(calculateRenderCreditQuote({ ...baseSpec, terrain_3d: true }).credits).toBe(150);
    expect(
      calculateRenderCreditQuote({
        ...baseSpec,
        resolution: "720x720",
        fps: 15,
        duration_seconds: 5,
      }).credits,
    ).toBe(10);
    expect(
      calculateRenderCreditQuote(
        {
          ...baseSpec,
          duration_seconds: 30,
          intro_hold_seconds: 2,
          outro_hold_seconds: 2,
          show_photos: true,
          photo_card_style: "cinematic_stop",
          photo_display_seconds: 3,
        },
        2,
      ).credits,
    ).toBe(70);
  });

  it("displays credit balance, ledger history, and voucher redemption on AccountPage", async () => {
    let balance = 100;
    const ledger = [
      {
        id: "entry-1",
        delta: 100,
        entry_type: "voucher_grant",
        render_job_id: null,
        voucher_id: "v-1",
        note: "Redeemed voucher BETA-1...",
        created_at: "2026-11-27T10:00:00Z",
      },
    ];

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";
      if (url.includes("/api/v1/auth/me")) {
        return jsonResponse({
          user: {
            id: "u-1",
            email: "rider@example.com",
            display_name: "Rider",
            strava_connected: false,
            has_password: true,
          },
          csrf_token: "csrf-1",
          dev_auth_enabled: false,
        });
      }
      if (url.includes("/api/v1/auth/sessions")) {
        return jsonResponse([]);
      }
      if (url.includes("/api/v1/credits/redeem") && method === "POST") {
        balance += 500;
        const newEntry = {
          id: "entry-2",
          delta: 500,
          entry_type: "voucher_grant",
          render_job_id: null,
          voucher_id: "v-2",
          note: "Redeemed voucher TRIP-NEW...",
          created_at: "2026-11-27T10:05:00Z",
        };
        ledger.unshift(newEntry);
        return jsonResponse({
          credits_granted: 500,
          balance,
          entry: newEntry,
        });
      }
      if (url.includes("/api/v1/credits") && method === "GET") {
        return jsonResponse({ balance, ledger });
      }
      return jsonResponse({ detail: "Not found" }, 404);
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const history = createMemoryHistory({ initialEntries: ["/account"] });
    const router = createAppRouter(history);
    render(<App router={router} queryClient={queryClient} />);

    expect(await screen.findByText(/100 credits/i)).toBeInTheDocument();
    expect(screen.getByText(/\+100/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Redeem voucher code/i), {
      target: { value: "TRIP-NEW-CODE" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Redeem voucher/i }));

    expect(await screen.findByText(/Added 500 Render Credits/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/^600 credits$/i)).toBeInTheDocument();
    });
  });

  it("shows live quote and inline voucher redemption in ExportPanel when balance is insufficient", async () => {
    let balance = 20;

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
      const method = init?.method ?? "GET";
      if (url.includes("/api/v1/projects/proj-1/renders/quote")) {
        return jsonResponse({
          credits: 100,
          width: 1920,
          height: 1080,
          fps: 30,
          film_seconds: 60,
          cinematic_photo_stop_seconds: 0,
          terrain_3d: false,
          terrain_multiplier: 1,
          cached: false,
          balance,
          sufficient: balance >= 100,
        });
      }
      if (url.includes("/api/v1/projects/proj-1/renders")) {
        return jsonResponse([]);
      }
      if (url.includes("/api/v1/credits/redeem") && method === "POST") {
        balance += 200;
        return jsonResponse({
          credits_granted: 200,
          balance,
          entry: {
            id: "e-inline",
            delta: 200,
            entry_type: "voucher_grant",
            render_job_id: null,
            voucher_id: "v-inline",
            note: "Redeemed voucher",
            created_at: "2026-11-27T10:10:00Z",
          },
        });
      }
      if (url.includes("/api/v1/credits")) {
        return jsonResponse({ balance, ledger: [] });
      }
      return jsonResponse({ detail: "Not found" }, 404);
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ExportPanel
          projectId="proj-1"
          spec={{
            ...createDefaultRenderSpecV2(),
            resolution: "1920x1080",
            fps: 30,
            duration_seconds: 60,
            intro_hold_seconds: 0,
            outro_hold_seconds: 0,
          }}
          stageCount={1}
          onFlushSave={async () => true}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/Need 80 more credits for Server Render/i)).toBeInTheDocument();
    const serverBtn = screen.getByRole("button", { name: /Server/i });
    expect(serverBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Voucher code/i), {
      target: { value: "TRIP-INLINE-200" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Redeem$/i }));

    expect(await screen.findByText(/\+200 credits added/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Server/i })).toBeEnabled();
    });
  });
});
