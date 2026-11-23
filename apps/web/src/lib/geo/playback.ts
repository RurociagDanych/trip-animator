import type { RenderFps, RenderSpecV2 } from "../api/types";

// The render worker emits the preview at 15 fps regardless of the spec; mirror that here
// so the editor preview steps through the same frames the exported film contains.
export const PREVIEW_RENDER_FPS: RenderFps = 15;

export function renderFpsForSpec(spec: Pick<RenderSpecV2, "fps">, isPreview: boolean): RenderFps {
  return isPreview ? PREVIEW_RENDER_FPS : spec.fps;
}

// Python's round() is half-to-even; Math.round rounds halves up, so 52.5 frames would
// differ (worker 52, Math.round 53) and the preview grid would drift from the film.
export function roundHalfToEven(value: number): number {
  if (!Number.isFinite(value)) return value;
  const floor = Math.floor(value);
  const fraction = value - floor;
  if (fraction < 0.5) return floor;
  if (fraction > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
}

export function renderFrameCount(durationS: number, fps: number): number {
  if (!Number.isFinite(durationS) || durationS <= 0 || !(fps > 0)) return 1;
  return Math.max(1, roundHalfToEven(durationS * fps));
}

// Maps a playback ratio onto the nearest rendered frame so the preview shows the same
// frame grid as the film: frame k sits at k / (frames - 1) of the duration.
export function snapRatioToRenderFrame(ratio: number, durationS: number, fps: number): number {
  const frames = renderFrameCount(durationS, fps);
  if (frames <= 1) return 0;
  const clamped = Math.min(1, Math.max(0, ratio));
  return roundHalfToEven(clamped * (frames - 1)) / (frames - 1);
}
