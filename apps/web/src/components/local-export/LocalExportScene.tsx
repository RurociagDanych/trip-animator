/** Renders deterministic editor frames into an exact-size export canvas. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import type { ClimbHighlightOut, ManifestClimb, RenderSpecV2 } from "../../lib/api/types";
import {
  computeJourneyDurationS,
  computePhotoStopTotalDurationS,
  evaluateTimelineSchedule,
  type MatchedPhoto,
} from "../../lib/geo/photos";
import type { TimelineStage } from "../../lib/geo/timeline";
import { TripStageCanvas, type TripStageCanvasHandle } from "../stage/TripStageCanvas";

/** Renders deterministic editor frames into a standalone, exact-size canvas. */
export interface LocalExportSceneProps {
  spec: RenderSpecV2;
  stages: TimelineStage[];
  matchedPhotos: MatchedPhoto[];
  climbs?: readonly (ClimbHighlightOut | ManifestClimb)[];
  projectTitle: string;
  width: number;
  height: number;
  onReady: (scene: {
    canvas: HTMLCanvasElement;
    frameCount: number;
    renderFrame: (index: number) => Promise<void>;
  }) => void;
  onError: (error: Error) => void;
  signal: AbortSignal;
}

function waitForVisibleTab(signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  if (!document.hidden) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      document.removeEventListener("visibilitychange", onVisibility);
      signal.removeEventListener("abort", onAbort);
    };
    const onVisibility = () => {
      if (document.hidden) return;
      cleanup();
      resolve();
    };
    const onAbort = () => {
      cleanup();
      reject(signal.reason);
    };
    document.addEventListener("visibilitychange", onVisibility);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function copyComputedStyles(source: HTMLElement, target: HTMLElement) {
  const computed = window.getComputedStyle(source);
  for (let index = 0; index < computed.length; index += 1) {
    const property = computed.item(index);
    target.style.setProperty(property, computed.getPropertyValue(property), computed.getPropertyPriority(property));
  }
}

async function inlinePhoto(url: string, signal: AbortSignal): Promise<string> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error("A trip photo could not be loaded for local export.");
  const blob = await response.blob();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("A trip photo could not be read for local export."));
    reader.readAsDataURL(blob);
  });
}

async function paintDomOverlays(
  root: HTMLDivElement,
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  photos: Map<string, Promise<string>>,
  signal: AbortSignal,
) {
  const clone = root.cloneNode(true) as HTMLDivElement;
  const originals = [root, ...root.querySelectorAll<HTMLElement>("*")];
  const copies = [clone, ...clone.querySelectorAll<HTMLElement>("*")];
  originals.forEach((element, index) => {
    const copy = copies[index];
    if (copy) copyComputedStyles(element, copy);
  });
  // SVG images cannot fetch embedded resources, even when the page already loaded them.
  const photoCopies = clone.querySelectorAll("img");
  await Promise.all([...root.querySelectorAll("img")].map(async (image, index) => {
    const url = image.currentSrc || image.src;
    const copy = photoCopies.item(index);
    if (!url || !copy) return;
    const embedded = photos.get(url) ?? inlinePhoto(url, signal);
    photos.set(url, embedded);
    copy.removeAttribute("srcset");
    copy.src = await embedded;
  }));
  clone.querySelectorAll("canvas").forEach((canvas) => canvas.remove());
  clone.style.cssText = `position:relative;left:0;top:0;width:${width}px;height:${height}px;overflow:hidden;background:transparent;`;
  const scene = clone.querySelector<HTMLElement>('[data-testid="deterministic-map-frame"]');
  if (scene) scene.style.background = "transparent";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml">${new XMLSerializer().serializeToString(clone)}</div></foreignObject></svg>`;
  try {
    const image = new Image();
    // A blob URL containing a foreignObject taints the output canvas in Chromium.
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
    await image.decode();
    context.drawImage(image, 0, 0, width, height);
  } catch (cause) {
    throw new Error("DOM overlays could not be rasterized for canvas export.", { cause });
  }
}

/** Keeps the editor renderer isolated while exposing deterministic composited frames. */
export function LocalExportScene({
  spec,
  stages,
  matchedPhotos,
  climbs,
  projectTitle,
  width,
  height,
  onReady,
  onError,
  signal,
}: LocalExportSceneProps) {
  const [frameIndex, setFrameIndex] = useState(0);
  const frameIndexRef = useRef(0);
  const stageController = useRef<TripStageCanvasHandle | null>(null);
  const sceneRoot = useRef<HTMLDivElement | null>(null);
  const outputRef = useRef<HTMLCanvasElement | null>(null);
  const embeddedPhotos = useRef(new Map<string, Promise<string>>());
  const durationS = useMemo(
    () => spec.photo_card_style === "cinematic_stop"
      ? computePhotoStopTotalDurationS(spec, matchedPhotos, stages, spec.is_preview)
      : computeJourneyDurationS(spec, spec.is_preview),
    [matchedPhotos, spec, stages],
  );
  // Cinematic photo holds can extend the schedule beyond the preview's output duration.
  const outputDurationS = spec.is_preview ? computeJourneyDurationS(spec, true) : durationS;
  const frameCount = Math.max(1, Math.round(outputDurationS * spec.fps));
  const timeS = frameCount <= 1 ? 0 : (frameIndex / (frameCount - 1)) * durationS;
  const schedule = useMemo(
    () => evaluateTimelineSchedule(stages, matchedPhotos, spec, timeS, durationS, spec.is_preview),
    [durationS, matchedPhotos, spec, stages, timeS],
  );
  const totalStats = useMemo(() => ({
    distanceM: stages.reduce((sum, stage) => sum + stage.distanceM, 0),
    elevationGainM: stages.reduce((sum, stage) => sum + stage.elevationGainM, 0),
    movingSeconds: stages.reduce((sum, stage) => sum + stage.movingSeconds, 0),
    stageCount: stages.length,
  }), [stages]);

  const renderFrame = useCallback(async (index: number) => {
    await waitForVisibleTab(signal);
    if (!Number.isInteger(index) || index < 0 || index >= frameCount) {
      throw new RangeError(`Frame index must be between 0 and ${frameCount - 1}.`);
    }
    const changed = frameIndexRef.current !== index;
    frameIndexRef.current = index;
    if (changed) {
      flushSync(() => setFrameIndex(index));
      await stageController.current?.waitForFrameSettle(true);
      // Background tabs may suspend MapLibre's repaint before the settle timeout.
      while (document.hidden) {
        await waitForVisibleTab(signal);
        await stageController.current?.waitForFrameSettle(true);
      }
    }
    if (signal.aborted) throw new DOMException("Export aborted", "AbortError");
    const root = sceneRoot.current;
    const output = outputRef.current;
    const context = output?.getContext("2d", { alpha: false });
    if (!root || !output || !context) throw new Error("Local export canvas is unavailable.");
    context.fillStyle = "#0b1117";
    context.fillRect(0, 0, width, height);
    const bounds = root.getBoundingClientRect();
    const mapCanvases = [...root.querySelectorAll("canvas")]
      .filter((source) => source !== outputRef.current && source.width > 0 && source.height > 0)
      .map((source) => ({ source, rect: source.getBoundingClientRect() }));
    if (!mapCanvases.some(({ rect }) => rect.width > 0 && rect.height > 0)) {
      throw new Error("The accelerated map canvas is unavailable.");
    }
    for (const { source, rect } of mapCanvases) {
      if (rect.width <= 0 || rect.height <= 0) continue;
      try {
        context.drawImage(source, (rect.left - bounds.left) * width / bounds.width,
          (rect.top - bounds.top) * height / bounds.height,
          rect.width * width / bounds.width, rect.height * height / bounds.height);
      } catch (cause) {
        throw new Error("A map layer could not be composited; its canvas is not readable.", { cause });
      }
    }
    await paintDomOverlays(root, context, width, height, embeddedPhotos.current, signal);
  }, [frameCount, height, signal, width]);

  useEffect(() => {
    const abort = () => onError(new DOMException("Export aborted", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    let disposed = false;
    void (async () => {
      try {
        if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
          throw new RangeError("Export width and height must be positive integers.");
        }
        await waitForVisibleTab(signal);
        await stageController.current?.waitForReady();
        await renderFrame(0);
        if (!disposed && outputRef.current) onReady({ canvas: outputRef.current, frameCount, renderFrame });
      } catch (cause) {
        if (!disposed) onError(cause instanceof Error ? cause : new Error(String(cause)));
      }
    })();
    return () => {
      disposed = true;
      signal.removeEventListener("abort", abort);
    };
  }, [frameCount, height, onError, onReady, renderFrame, signal, width]);

  return (
    <div ref={sceneRoot} aria-hidden="true" style={{ position: "fixed", left: -width - 2, top: 0, width, height, overflow: "hidden", pointerEvents: "none" }}>
      <canvas ref={outputRef} width={width} height={height} style={{ display: "none" }} />
      <TripStageCanvas
        stages={stages}
        spec={spec}
        progressRatio={schedule.progressRatio}
        durationS={durationS}
        travelDurationS={schedule.travelDurationS}
        isPreview={spec.is_preview}
        renderMode="deterministic"
        exactSize={{ width, height }}
        controllerRef={stageController}
        matchedPhotos={matchedPhotos}
        activePhoto={schedule.activePhoto}
        cinematicMapZoom={schedule.cinematicMapZoom}
        climbs={climbs}
        projectTitle={projectTitle}
        totalStats={totalStats}
      />
    </div>
  );
}
