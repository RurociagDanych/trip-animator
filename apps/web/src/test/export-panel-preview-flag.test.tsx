import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState, type ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExportPanel } from "../components/editor/ExportPanel";
import { InspectorPanel } from "../components/editor/InspectorPanel";
import type { LocalExportSceneProps } from "../components/local-export/LocalExportScene";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { listLocalRenders, saveLocalRender } from "../lib/export/local-history";
import { encodeLocalVideo } from "../lib/export/local-video";
import { PREVIEW_RENDER_FPS } from "../lib/geo/playback";
import { useEditorStore } from "../lib/state/editor-store";

vi.mock("../lib/export/local-history", () => ({ listLocalRenders: vi.fn(), saveLocalRender: vi.fn() }));
vi.mock("../lib/export/local-video", () => ({
  probeLocalVideo: vi.fn(async () => ({ supported: true, encoding: "hardware-preferred" })),
  encodeLocalVideo: vi.fn(),
}));
vi.mock("../components/local-export/LocalExportScene", () => ({
  LocalExportScene: ({ spec, onReady }: LocalExportSceneProps) => {
    useEffect(() => {
      onReady({ canvas: document.createElement("canvas"), frameCount: spec.is_preview ? 4 : 8, renderFrame: async () => {} });
    }, [spec, onReady]);
    return null;
  },
}));

function ExportInspector() {
  const [section, setSection] = useState<ComponentProps<typeof InspectorPanel>["section"]>("export");
  return <InspectorPanel projectId="proj-1" spec={createDefaultRenderSpecV2()} stageCount={1}
    section={section} onSelectSection={setSection} onUpdateSpec={() => {}} onUpdateHud={() => {}}
    onFlushSave={async () => true}
    localExportSource={{ stages: [], matchedPhotos: [], climbs: [], projectTitle: "Test trip", unavailableReason: null }} />;
}

function renderPanel(withLocal = false) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      {withLocal ? <ExportInspector /> : <ExportPanel projectId="proj-1" spec={createDefaultRenderSpecV2()}
        stageCount={1} onFlushSave={async () => true} />}
    </QueryClientProvider>,
  );
}

describe("ExportPanel local preview", () => {
  afterEach(() => {
    useEditorStore.getState().setPreviewWatermark(false);
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("does not offer server preview even when the editor watermark is on", () => {
    useEditorStore.getState().setPreviewWatermark(true);
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => Response.json([]));
    renderPanel();
    expect(screen.queryByRole("button", { name: /Preview clip/i })).not.toBeInTheDocument();
  });

  it("exports local preview across inspector tabs and shows it in history after remount", async () => {
    useEditorStore.getState().setPreviewWatermark(false);
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async () => Response.json([]));
    const objectUrls = class extends URL {
      static override createObjectURL = vi.fn(() => "blob:local-render");
      static override revokeObjectURL = vi.fn();
    };
    vi.stubGlobal("URL", objectUrls);
    const video = new Blob(["video"], { type: "video/mp4" });
    let completeEncoding!: (video: Blob) => void;
    vi.mocked(encodeLocalVideo).mockReturnValue(new Promise((resolve) => { completeEncoding = resolve; }));
    let completeInitialHistoryRead!: () => void;
    vi.mocked(listLocalRenders).mockImplementationOnce(() => new Promise((resolve) => {
      completeInitialHistoryRead = () => resolve([]);
    }));
    vi.mocked(saveLocalRender).mockImplementation(async (record) => {
      vi.mocked(listLocalRenders).mockResolvedValue([record]);
    });
    const controls = renderPanel(true);
    const preview = screen.getByRole("button", { name: /Preview clip \(5s\).*Local/i });
    const server = screen.getByRole("button", { name: /Export MP4 \(\d+x\d+\).*Server/i });
    const local = screen.getByRole("button", { name: /Export MP4 \(\d+x\d+\).*Local/i });
    expect(server.className).toBe(local.className);
    await waitFor(() => expect(preview).toBeEnabled());
    fireEvent.click(preview);
    await waitFor(() => expect(encodeLocalVideo).toHaveBeenCalled());
    const signal = vi.mocked(encodeLocalVideo).mock.calls[0]?.[0].signal;
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(screen.getByRole("status")).toHaveTextContent(/Preparing local export|Exporting locally/);
    expect(signal?.aborted).toBe(false);
    hidden.mockReturnValue(false);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    const exportTab = screen.getByRole("tab", { selected: true });
    fireEvent.click(screen.getByRole("tab", { name: "HUD" }));
    expect(screen.queryByRole("button", { name: /Preview clip/i })).not.toBeInTheDocument();
    expect(signal?.aborted).toBe(false);
    completeEncoding(video);
    await waitFor(() => expect(saveLocalRender).toHaveBeenCalled());
    fireEvent.click(exportTab);

    const row = await screen.findByRole("listitem");
    await act(async () => completeInitialHistoryRead());
    expect(row).toBeInTheDocument();
    const download = await within(row).findByRole("link", { name: "Download" });
    expect(download).toHaveAttribute("href", "blob:local-render");
    fireEvent.click(within(row).getByRole("button", { name: "Watch" }));
    expect(row.querySelector("video")).toHaveAttribute("src", "blob:local-render");
    expect(encodeLocalVideo).toHaveBeenCalledWith(expect.objectContaining({ fps: PREVIEW_RENDER_FPS, frameCount: 4 }));
    expect(saveLocalRender).toHaveBeenCalledWith(expect.objectContaining({ projectId: "proj-1", isPreview: true, blob: video }));
    expect(fetchSpy.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);

    controls.unmount();
    expect(objectUrls.revokeObjectURL).toHaveBeenCalledWith("blob:local-render");
    renderPanel(true);
    expect(await screen.findByText("Preview (5s)")).toBeInTheDocument();
  });
});
