import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ExportPanel } from "../components/editor/ExportPanel";
import { clearCsrfToken, setCsrfToken } from "../lib/api/client";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { useEditorStore } from "../lib/state/editor-store";

function mockJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

// Renders ExportPanel with the given toggle state, clicks one render button and
// returns the is_preview flag the panel sent to the render endpoint.
async function submitFromButton(name: RegExp, previewWatermark: boolean): Promise<boolean> {
  useEditorStore.getState().setPreviewWatermark(previewWatermark);
  const bodies: Record<string, unknown>[] = [];

  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";
    if (url.endsWith("/renders") && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      bodies.push(body);
      return mockJsonResponse(
        {
          id: `job-${bodies.length}`,
          project_id: "proj-1",
          status: "queued",
          progress_pct: 0,
          spec: body,
          created_at: "2001-06-14T08:00:00Z",
          updated_at: "2001-06-14T08:00:00Z",
        },
        201,
      );
    }
    return mockJsonResponse([]);
  });

  render(
    <QueryClientProvider client={makeQueryClient()}>
      <ExportPanel
        projectId="proj-1"
        spec={createDefaultRenderSpecV2()}
        stageCount={1}
        onFlushSave={async () => true}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(screen.getByRole("button", { name }));
  await waitFor(() => {
    expect(bodies).toHaveLength(1);
  });
  return bodies[0]?.is_preview as boolean;
}

describe("ExportPanel preview flag follows the preview watermark toggle", () => {
  beforeEach(() => {
    setCsrfToken("csrf-test-token");
    useEditorStore.getState().setPreviewWatermark(false);
  });

  afterEach(() => {
    clearCsrfToken();
    useEditorStore.getState().setPreviewWatermark(false);
    vi.restoreAllMocks();
  });

  it("sends is_preview true for the preview button when the toggle is on", async () => {
    expect(await submitFromButton(/Preview clip \(5s\)/i, true)).toBe(true);
  });

  it("sends is_preview false for the full export when the toggle is on", async () => {
    expect(await submitFromButton(/Export MP4/i, true)).toBe(false);
  });

  it("sends is_preview false for the preview button when the toggle is off", async () => {
    expect(await submitFromButton(/Preview clip \(5s\)/i, false)).toBe(false);
  });

  it("sends is_preview false for the full export when the toggle is off", async () => {
    expect(await submitFromButton(/Export MP4/i, false)).toBe(false);
  });
});
