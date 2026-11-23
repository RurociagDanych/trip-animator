import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2 } from "../lib/api/types";
import { computeJourneyDurationS, evaluateTimelineSchedule } from "../lib/geo/photos";
import { PREVIEW_RENDER_FPS } from "../lib/geo/playback";
import { localExportFrameTimeS } from "../components/local-export/LocalExportScene";

describe("local export preview timeline", () => {
  it("samples the first five seconds of the full timeline without compressing it", () => {
    const fullSpec = createDefaultRenderSpecV2();
    const previewSpec = { ...fullSpec, is_preview: true };
    const fullDurationS = computeJourneyDurationS(fullSpec, false);
    const previewDurationS = computeJourneyDurationS(previewSpec, true);
    const frameCount = Math.round(previewDurationS * PREVIEW_RENDER_FPS);
    const frameTimes = [0, 15, 30, frameCount - 1].map((index) =>
      localExportFrameTimeS(index, frameCount, fullDurationS, PREVIEW_RENDER_FPS, true),
    );

    expect(fullDurationS).toBe(20);
    expect(previewDurationS).toBe(5);
    expect(frameTimes).toEqual([0, 1, 2, 74 / 15]);
    expect(frameTimes.at(-1)).toBeLessThan(5);

    const atTwoSeconds = evaluateTimelineSchedule([], [], fullSpec, frameTimes[2]!, fullDurationS, false);
    expect(atTwoSeconds.progressRatio).toBeCloseTo(1 / 17.5);
  });

  it("keeps full export frames stretched across the complete timeline", () => {
    expect(localExportFrameTimeS(0, 600, 20, 30, false)).toBe(0);
    expect(localExportFrameTimeS(599, 600, 20, 30, false)).toBe(20);
  });
});
