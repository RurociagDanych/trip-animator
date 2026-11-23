import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2, type RenderFps } from "../lib/api/types";
import { computePhotoStopTotalDurationS, computeJourneyDurationS } from "../lib/geo/photos";
import {
  PREVIEW_RENDER_FPS,
  renderFpsForSpec,
  renderFrameCount,
  snapRatioToRenderFrame,
} from "../lib/geo/playback";

type GoldenRow = { duration_s: number; fps: number; is_preview: boolean; frames: number };

const GOLDEN_ROWS = JSON.parse(
  readFileSync(
    // __dirname keeps the path stable when vitest runs from the repo root; process.cwd() does not.
    resolve(__dirname, "../../../../packages/telemetry/tests/golden/frame_counts.json"),
    "utf8",
  ),
) as GoldenRow[];

describe("preview playback follows the render frame rate", () => {
  it("matches the shared worker golden frame table", () => {
    expect(GOLDEN_ROWS.length).toBeGreaterThan(0);
    const spec = createDefaultRenderSpecV2();
    for (const row of GOLDEN_ROWS) {
      const durationS = computeJourneyDurationS(
        { ...spec, duration_seconds: row.duration_s },
        row.is_preview,
      );
      const fps = renderFpsForSpec({ fps: row.fps as RenderFps }, row.is_preview);
      expect(renderFrameCount(durationS, fps), JSON.stringify(row)).toBe(row.frames);
    }
  });

  it.each([true, false])(
    "drives the dock grid from the toggle alone (toggle %s) to the worker frame count",
    (previewWatermark) => {
      const rows = GOLDEN_ROWS.filter((row) => row.is_preview === previewWatermark);
      expect(rows.length).toBeGreaterThan(0);
      for (const row of rows) {
        // The dock must ignore spec.is_preview: the toggle is the single source of truth.
        const spec = {
          ...createDefaultRenderSpecV2(),
          duration_seconds: row.duration_s,
          fps: row.fps as RenderFps,
          is_preview: !previewWatermark,
        };
        const dockDuration = computePhotoStopTotalDurationS(spec, [], [], previewWatermark);
        const dockFps = renderFpsForSpec(spec, previewWatermark);
        expect(renderFrameCount(dockDuration, dockFps), JSON.stringify(row)).toBe(row.frames);
      }
    },
  );

  it("uses the spec fps for the film and 15 fps for a preview render", () => {
    expect(renderFpsForSpec({ fps: 15 }, false)).toBe(15);
    expect(renderFpsForSpec({ fps: 60 }, false)).toBe(60);
    expect(renderFpsForSpec({ fps: 60 }, true)).toBe(PREVIEW_RENDER_FPS);
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

  it("returns zero for a non-finite playback ratio", () => {
    expect(snapRatioToRenderFrame(Number.NaN, 10, 15)).toBe(0);
    expect(snapRatioToRenderFrame(Number.POSITIVE_INFINITY, 10, 15)).toBe(0);
    expect(snapRatioToRenderFrame(Number.NEGATIVE_INFINITY, 10, 15)).toBe(0);
  });

  it("returns zero for a single-frame timeline", () => {
    expect(snapRatioToRenderFrame(0.7, 0, 15)).toBe(0);
  });
});
