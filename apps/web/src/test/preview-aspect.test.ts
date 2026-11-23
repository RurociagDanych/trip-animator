import { describe, expect, it } from "vitest";
import type { ResolutionPreset } from "../lib/api/types";
import { fitResolutionPreset, parseResolutionPreset } from "../lib/geo/format";

const PRESETS: ResolutionPreset[] = ["1920x1080", "1080x1080", "1080x1350", "1080x1920"];

describe("preview box follows the selected resolution", () => {
  it("keeps portrait presets portrait inside a landscape dock", () => {
    expect(fitResolutionPreset("1080x1350", { width: 900, height: 600 })).toEqual({
      width: 480,
      height: 600,
    });
    expect(fitResolutionPreset("1080x1920", { width: 900, height: 600 })).toEqual({
      width: 337,
      height: 600,
    });
  });

  it("keeps landscape presets landscape inside a portrait dock", () => {
    expect(fitResolutionPreset("1920x1080", { width: 400, height: 900 })).toEqual({
      width: 400,
      height: 225,
    });
  });

  it("matches the render aspect ratio for every preset", () => {
    for (const preset of PRESETS) {
      const { width, height } = parseResolutionPreset(preset);
      const fit = fitResolutionPreset(preset, { width: 777, height: 555 });
      expect(fit).not.toBeNull();
      const expected = width / height;
      expect(Math.abs(fit!.width / fit!.height - expected)).toBeLessThan(0.01);
      expect(fit!.width).toBeLessThanOrEqual(777);
      expect(fit!.height).toBeLessThanOrEqual(555);
    }
  });

  it("returns null until the container has a measured size", () => {
    expect(fitResolutionPreset("1080x1350", { width: 0, height: 600 })).toBeNull();
    expect(fitResolutionPreset("1080x1350", { width: 900, height: 0 })).toBeNull();
  });

  it("returns null when sub-pixel space floors a dimension to zero", () => {
    expect(fitResolutionPreset("1080x1350", { width: 0.5, height: 100 })).toBeNull();
    expect(fitResolutionPreset("1920x1080", { width: 1.5, height: 1000 })).toBeNull();
  });
});
