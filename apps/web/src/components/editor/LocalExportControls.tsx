/** Runs a cancellable browser export without creating a server render job. */
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { RenderSpecV2 } from "../../lib/api/types";
import { saveLocalRender, type LocalRenderRecord } from "../../lib/export/local-history";
import type { probeLocalVideo } from "../../lib/export/local-video";
import { parseResolutionPreset } from "../../lib/geo/format";
import { renderFpsForSpec } from "../../lib/geo/playback";
import type { LocalExportScene, LocalExportSceneProps } from "../local-export/LocalExportScene";
import { VideoExportButton } from "./VideoExportButton";

type Capability = Awaited<ReturnType<typeof probeLocalVideo>>;

export interface LocalExportSource {
  stages: LocalExportSceneProps["stages"];
  matchedPhotos: LocalExportSceneProps["matchedPhotos"];
  climbs: LocalExportSceneProps["climbs"];
  projectTitle: string;
  unavailableReason: string | null;
}

interface Props {
  projectId: string;
  source: LocalExportSource;
  spec: RenderSpecV2;
  disabled: boolean;
  onFlushSave: () => Promise<boolean>;
  onSaved: (record: LocalRenderRecord) => void;
}

interface ActiveScene {
  Component: typeof LocalExportScene;
  props: LocalExportSceneProps;
}

/** Exposes capability, progress and download independently of the server queue. */
export function LocalExportControls({ projectId, source, spec, disabled, onFlushSave, onSaved }: Props) {
  const [probe, setProbe] = useState<{ key: string; full: Capability; preview: Capability } | null>(null);
  const [activeScene, setActiveScene] = useState<ActiveScene | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [activeKind, setActiveKind] = useState<"full" | "preview" | null>(null);
  const [saving, setSaving] = useState(false);
  const [hidden, setHidden] = useState(() => document.hidden);
  const [error, setError] = useState<string | null>(null);
  const [download, setDownload] = useState<string | null>(null);
  const running = useRef<AbortController | null>(null);
  const downloadUrl = useRef<string | null>(null);
  const hintId = useId();
  const { resolution, fps } = spec;
  const previewFps = renderFpsForSpec(spec, true);
  const probeKey = `${resolution}/${fps}`;
  const capabilities = probe?.key === probeKey ? probe : null;

  useEffect(() => {
    let stale = false;
    void import("../../lib/export/local-video")
      .then(async ({ probeLocalVideo }) => {
        const size = parseResolutionPreset(resolution);
        const full = await probeLocalVideo({ ...size, fps });
        const preview = fps === previewFps ? full : await probeLocalVideo({ ...size, fps: previewFps });
        if (!stale) setProbe({ key: probeKey, full, preview });
      }).catch(() => {
        const failed = { supported: false, reason: "Could not check browser export support." } as const;
        if (!stale) setProbe({ key: probeKey, full: failed, preview: failed });
      });
    return () => { stale = true; };
  }, [fps, previewFps, probeKey, resolution]);

  useEffect(() => {
    const update = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => () => {
    const controller = running.current;
    running.current = null;
    controller?.abort();
    if (downloadUrl.current) URL.revokeObjectURL(downloadUrl.current);
  }, []);

  function finish(controller: AbortController, failure?: string) {
    if (running.current !== controller) return;
    running.current = null;
    controller.abort();
    setProgress(null);
    setActiveKind(null);
    setSaving(false);
    setActiveScene(null);
    if (failure) setError(failure);
  }

  async function start(kind: "full" | "preview") {
    const capability = capabilities?.[kind];
    if (disabled || !capability?.supported || source.unavailableReason || running.current) return;
    const isPreview = kind === "preview";
    const exportFps = isPreview ? previewFps : fps;
    const controller = new AbortController();
    running.current = controller;
    const { signal } = controller;
    setError(null);
    setProgress(0);
    setActiveKind(kind);
    if (downloadUrl.current) URL.revokeObjectURL(downloadUrl.current);
    downloadUrl.current = null;
    setDownload(null);
    try {
      if (!(await onFlushSave())) {
        finish(controller, "Resolve unsaved changes or revision conflict before exporting.");
        return;
      }
      if (signal.aborted) return;
      const [{ LocalExportScene }, { encodeLocalVideo }] = await Promise.all([
        import("../local-export/LocalExportScene"),
        import("../../lib/export/local-video"),
      ]);
      if (signal.aborted) return;
      let encodingStarted = false;
      setActiveScene({
        Component: LocalExportScene,
        props: {
          ...parseResolutionPreset(resolution),
          spec: { ...spec, fps: exportFps, is_preview: isPreview },
          stages: source.stages,
          matchedPhotos: source.matchedPhotos,
          climbs: source.climbs,
          projectTitle: source.projectTitle,
          signal,
          onError: (cause) => finish(controller, `Local export failed: ${cause.message} Use server export instead.`),
          onReady: ({ canvas, frameCount, renderFrame }) => {
            if (signal.aborted || encodingStarted) return;
            encodingStarted = true;
            void encodeLocalVideo({ canvas, fps: exportFps, frameCount, renderFrame, signal, encoding: capability.encoding,
              onProgress: (done) => {
                if (!signal.aborted) setProgress(Math.min(99, Math.floor(100 * done / frameCount)));
              },
            }).then(async (blob) => {
              if (signal.aborted) return;
              setSaving(true);
              const record: LocalRenderRecord = {
                id: crypto.randomUUID(), projectId, createdAt: new Date().toISOString(), resolution, isPreview, blob,
              };
              try {
                await saveLocalRender(record);
              } catch {
                if (signal.aborted) return;
                const url = URL.createObjectURL(blob);
                downloadUrl.current = url;
                setDownload(url);
                finish(controller, "Could not save to this browser’s render history. Download the video now to keep it.");
                return;
              }
              onSaved(record);
              finish(controller);
            }).catch((error: unknown) => {
              if (!signal.aborted) console.error("Local MP4 export failed", error);
              finish(controller, "Local MP4 encoding failed. Try a lower resolution or use server export.");
            });
          },
        },
      });
    } catch {
      finish(controller, "Could not start local export. Use server export instead.");
    }
  }

  const full = capabilities?.full;
  const preview = capabilities?.preview;
  const hint = source.unavailableReason ?? (!full || !preview
    ? "Checking GPU and MP4 encoding support…"
    : !full.supported
      ? `${full.reason} ${preview.supported ? "Local preview and server export are still available." : "Server export is still available."}`
      : !preview.supported
        ? `Local preview is unavailable. ${preview.reason}`
        : "Local videos stay in this browser. Keep the browser tab open until export finishes.");

  const unavailable = disabled || progress !== null || Boolean(source.unavailableReason);

  return (
    <div className="space-y-2">
      <VideoExportButton resolution={resolution} mode="Local" busy={activeKind === "full"}
        disabled={unavailable || !full?.supported} describedBy={hintId} onClick={() => void start("full")} />
      <VideoExportButton resolution={resolution} mode="Local" preview busy={activeKind === "preview"}
        disabled={unavailable || !preview?.supported} describedBy={hintId} onClick={() => void start("preview")} />
      <p id={hintId} className="text-[11px] text-ink-muted">{hint}</p>
      {progress !== null && (
        <div className="flex items-center justify-between gap-2">
          <span role="status">{saving ? "Saving to render history…" : hidden ? "Local export resumes when you return to this browser tab." : progress === 0 ? "Preparing local export…" : `Exporting locally… ${progress}%`}</span>
          {!saving && <button type="button" className="text-coral-accent underline" onClick={() => {
            if (running.current) finish(running.current);
          }}>Cancel local export</button>}
        </div>
      )}
      {error && <p role="alert" className="text-coral-accent">{error}</p>}
      {download && (
        <a className="font-semibold text-teal-accent underline" href={download} download={`trip-${projectId}.mp4`}>
          Download local MP4
        </a>
      )}
      {activeScene && createPortal(<activeScene.Component {...activeScene.props} />, document.body)}
    </div>
  );
}
