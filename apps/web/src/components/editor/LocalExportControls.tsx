/** Runs a cancellable browser export without creating a server render job. */
import { Download, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { RenderSpecV2 } from "../../lib/api/types";
import type { probeLocalVideo } from "../../lib/export/local-video";
import { parseResolutionPreset } from "../../lib/geo/format";
import type { LocalExportScene, LocalExportSceneProps } from "../local-export/LocalExportScene";

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
}

interface ActiveScene {
  Component: typeof LocalExportScene;
  props: LocalExportSceneProps;
}

/** Exposes capability, progress and download independently of the server queue. */
export function LocalExportControls({ projectId, source, spec, disabled, onFlushSave }: Props) {
  const [probe, setProbe] = useState<{ key: string; result: Capability } | null>(null);
  const [activeScene, setActiveScene] = useState<ActiveScene | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [download, setDownload] = useState<string | null>(null);
  const running = useRef<AbortController | null>(null);
  const downloadUrl = useRef<string | null>(null);
  const { resolution, fps } = spec;
  const probeKey = `${resolution}/${fps}`;
  const capability = probe?.key === probeKey ? probe.result : null;

  useEffect(() => {
    let stale = false;
    void import("../../lib/export/local-video")
      .then(({ probeLocalVideo }) => probeLocalVideo({ ...parseResolutionPreset(resolution), fps }))
      .then((result) => {
        if (!stale) setProbe({ key: probeKey, result });
      }, () => {
        if (!stale) setProbe({ key: probeKey, result: { supported: false, reason: "Could not check browser export support." } });
      });
    return () => { stale = true; };
  }, [fps, probeKey, resolution]);

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
    setActiveScene(null);
    if (failure) setError(failure);
  }

  async function start() {
    if (disabled || !capability?.supported || source.unavailableReason || running.current) return;
    const controller = new AbortController();
    running.current = controller;
    const { signal } = controller;
    setError(null);
    setProgress(0);
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
          spec: { ...spec, is_preview: false },
          stages: source.stages,
          matchedPhotos: source.matchedPhotos,
          climbs: source.climbs,
          projectTitle: source.projectTitle,
          signal,
          onError: (cause) => finish(controller, `Local export failed: ${cause.message} Use server export instead.`),
          onReady: ({ canvas, frameCount, renderFrame }) => {
            if (signal.aborted || encodingStarted) return;
            encodingStarted = true;
            void encodeLocalVideo({ canvas, fps, frameCount, renderFrame, signal, encoding: capability.encoding,
              onProgress: (done) => {
                if (!signal.aborted) setProgress(Math.min(99, Math.floor(100 * done / frameCount)));
              },
            }).then((blob) => {
              if (signal.aborted) return;
              const url = URL.createObjectURL(blob);
              downloadUrl.current = url;
              setDownload(url);
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

  const hint = source.unavailableReason ?? (capability === null
    ? "Checking GPU and MP4 encoding support…"
    : !capability.supported
      ? `${capability.reason} Server export is still available.`
      : capability.encoding === "hardware-preferred"
        ? "GPU map rendering is available. The browser prefers hardware video encoding. Keep this panel and tab open."
        : "GPU map rendering is available. Video encoding may use the CPU. Keep this panel and tab open.");

  return (
    <div className="space-y-1.5">
      <button
        type="button"
        disabled={disabled || progress !== null || !capability?.supported || Boolean(source.unavailableReason)}
        aria-describedby="local-export-hint"
        onClick={() => void start()}
        className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-border bg-elevated px-3 py-2 text-xs font-semibold text-ink hover:border-teal-accent/60 disabled:opacity-50"
      >
        {progress === null ? <Download className="h-3.5 w-3.5" aria-hidden="true" /> : <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
        Export MP4 locally (GPU)
      </button>
      <p id="local-export-hint" className="text-[11px] text-ink-muted">{hint}</p>
      {progress !== null && (
        <div className="flex items-center justify-between gap-2">
          <span role="status">{progress === 0 ? "Preparing local export…" : `Exporting locally… ${progress}%`}</span>
          <button type="button" className="text-coral-accent underline" onClick={() => {
            if (running.current) finish(running.current);
          }}>Cancel local export</button>
        </div>
      )}
      {error && <p role="alert" className="text-coral-accent">{error}</p>}
      {download && (
        <a className="font-semibold text-teal-accent underline" href={download} download={`trip-${projectId}.mp4`}>
          Download local MP4
        </a>
      )}
      {activeScene && <activeScene.Component {...activeScene.props} />}
    </div>
  );
}
