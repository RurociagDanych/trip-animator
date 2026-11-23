import type { RenderFps, RenderSpecV2 } from "../api/types";

// The render worker emits the preview at 15 fps regardless of the spec; mirror that here
// so the editor preview steps through the same frames the exported film contains.
export const PREVIEW_RENDER_FPS: RenderFps = 15;

export function renderFpsForSpec(spec: Pick<RenderSpecV2, "fps" | "is_preview">): RenderFps {
  return spec.is_preview ? PREVIEW_RENDER_FPS : spec.fps;
}

export function renderFrameCount(durationS: number, fps: number): number {
  if (!Number.isFinite(durationS) || durationS <= 0 || !(fps > 0)) return 1;
  return Math.max(1, Math.round(durationS * fps));
}

// Maps a playback ratio onto the nearest rendered frame so the preview shows the same
// frame grid as the film: frame k sits at k / (frames - 1) of the duration.
export function snapRatioToRenderFrame(ratio: number, durationS: number, fps: number): number {
  const frames = renderFrameCount(durationS, fps);
  if (frames <= 1) return 0;
  const clamped = Math.min(1, Math.max(0, ratio));
  return Math.round(clamped * (frames - 1)) / (frames - 1);
}
