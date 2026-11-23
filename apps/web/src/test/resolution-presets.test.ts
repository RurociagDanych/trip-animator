import { describe, expect, it } from "vitest";
import {
  createDefaultRenderSpecV2,
  normalizeRenderSpecV2,
  type ResolutionPreset,
} from "../lib/api/types";
import { parseResolutionPreset } from "../lib/geo/format";

const ALL_PRESETS: ResolutionPreset[] = [
  "1280x720",
  "1920x1080",
  "1080x1080",
  "1080x1350",
  "1080x1920",
  "720x1280",
  "720x900",
  "720x720",
];

describe("resolution presets", () => {
  it("round-trips every preset through normalization unchanged", () => {
    for (const preset of ALL_PRESETS) {
      expect(normalizeRenderSpecV2({ resolution: preset }).resolution).toBe(preset);
    }
  });

  it("falls back to the default resolution for unknown values", () => {
    const defaults = createDefaultRenderSpecV2();
    const raw = { resolution: "640x480" } as unknown as Partial<ReturnType<typeof createDefaultRenderSpecV2>>;
    expect(normalizeRenderSpecV2(raw).resolution).toBe(defaults.resolution);
  });

  it("parses the 720p portrait and smaller presets to their pixel sizes", () => {
    expect(parseResolutionPreset("720x1280")).toEqual({ width: 720, height: 1280 });
    expect(parseResolutionPreset("720x900")).toEqual({ width: 720, height: 900 });
    expect(parseResolutionPreset("720x720")).toEqual({ width: 720, height: 720 });
  });

  it("keeps every preset at even pixel dimensions for the H.264 encoder", () => {
    for (const preset of ALL_PRESETS) {
      const { width, height } = parseResolutionPreset(preset);
      expect(width % 2).toBe(0);
      expect(height % 2).toBe(0);
    }
  });
});
