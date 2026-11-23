import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  createDefaultRenderSpecV2,
  findInvalidRenderSpecValues,
  normalizeRenderSpecV2,
  RENDER_FPS_VALUES,
  RESOLUTION_PRESETS,
  type RenderSpecV2,
  type ResolutionPreset,
} from "../lib/api/types";
import { parseResolutionPreset } from "../lib/geo/format";

const ALL_PRESETS: ResolutionPreset[] = [...RESOLUTION_PRESETS];

const SCHEMA_PATH = resolve(process.cwd(), "../../packages/telemetry/src/trip_telemetry/schemas.py");

function literalValues(source: string, pattern: RegExp): string[] {
  const match = pattern.exec(source);
  if (!match) throw new Error(`schema literal not found: ${pattern.source}`);
  return [...(match[1] ?? "").matchAll(/"([^"]+)"|(\d+)/g)].map((m) => m[1] ?? m[2] ?? "");
}

describe("resolution presets", () => {
  it("matches the worker schema preset and fps literals", () => {
    const source = readFileSync(SCHEMA_PATH, "utf8");
    const presets = literalValues(source, /ResolutionPreset = Literal\[([^\]]*)\]/);
    const fps = literalValues(source, /fps: Literal\[([\d,\s]+)\]/);
    expect([...RESOLUTION_PRESETS].sort()).toEqual([...presets].sort());
    expect(RESOLUTION_PRESETS).toHaveLength(presets.length);
    expect([...RENDER_FPS_VALUES].map(String).sort()).toEqual([...fps].sort());
  });

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

  it("reports stored resolution and fps values that normalization replaces", () => {
    const defaults = createDefaultRenderSpecV2();
    const raw = { resolution: "640x480", fps: 24 } as unknown as Partial<RenderSpecV2>;
    expect(findInvalidRenderSpecValues(raw)).toEqual([
      { field: "resolution", value: "640x480", fallback: defaults.resolution },
      { field: "fps", value: 24, fallback: defaults.fps },
    ]);
  });

  it("does not report valid or absent resolution and fps values", () => {
    expect(findInvalidRenderSpecValues({ resolution: "720x720", fps: 30 })).toEqual([]);
    expect(findInvalidRenderSpecValues({})).toEqual([]);
  });

  it("round-trips the supported frame rates and rejects others", () => {
    for (const fps of [15, 30, 60] as const) {
      expect(normalizeRenderSpecV2({ fps }).fps).toBe(fps);
    }
    const raw = { fps: 24 } as unknown as Partial<RenderSpecV2>;
    expect(normalizeRenderSpecV2(raw).fps).toBe(createDefaultRenderSpecV2().fps);
  });
});
