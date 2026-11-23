import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import {
  PREVIEW_RENDER_FPS,
  renderFpsForSpec,
  renderFrameCount,
  snapRatioToRenderFrame,
} from "../lib/geo/playback";

describe("preview playback follows the render frame rate", () => {
  it("uses the spec fps for the film and 15 fps for a preview render", () => {
    const spec = createDefaultRenderSpecV2();
    expect(renderFpsForSpec({ ...spec, fps: 15, is_preview: false })).toBe(15);
    expect(renderFpsForSpec({ ...spec, fps: 60, is_preview: false })).toBe(60);
    expect(renderFpsForSpec({ ...spec, fps: 60, is_preview: true })).toBe(PREVIEW_RENDER_FPS);
  });

  it("counts frames the way the worker manifest does", () => {
    expect(renderFrameCount(10, 15)).toBe(150);
    expect(renderFrameCount(10, 30)).toBe(300);
    expect(renderFrameCount(0, 15)).toBe(1);
  });

  it("snaps a 15 fps film to its rendered frame grid", () => {
    const durationS = 2;
    const frames = renderFrameCount(durationS, 15);
    expect(frames).toBe(30);
    const seen = new Set<number>();
    for (let i = 0; i <= 200; i += 1) {
      const ratio = snapRatioToRenderFrame(i / 200, durationS, 15);
      seen.add(Math.round(ratio * (frames - 1)));
      expect(Math.abs(ratio * (frames - 1) - Math.round(ratio * (frames - 1)))).toBeLessThan(1e-9);
    }
    expect(seen.size).toBe(frames);
  });

  it("holds the first and last frame at the ends of the timeline", () => {
    expect(snapRatioToRenderFrame(0, 10, 15)).toBe(0);
    expect(snapRatioToRenderFrame(1, 10, 15)).toBe(1);
    expect(snapRatioToRenderFrame(1.4, 10, 15)).toBe(1);
    expect(snapRatioToRenderFrame(-0.2, 10, 15)).toBe(0);
  });

  it("returns zero for a single-frame timeline", () => {
    expect(snapRatioToRenderFrame(0.7, 0, 15)).toBe(0);
  });
});
