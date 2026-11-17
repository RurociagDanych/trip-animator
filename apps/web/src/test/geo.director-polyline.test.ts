import { describe, expect, it } from "vitest";
import { generateDirectorKeyframes } from "../lib/geo/director";
import { coordsToSvgPath, decodeEncodedPolyline, polylinesToSvgPath } from "../lib/geo/polyline";

describe("geo route rendering helpers", () => {
  it("keeps separate route segments separate and omits lines too short to draw", () => {
    const path = polylinesToSvgPath([
      [[10, 45]],
      [[10, 45], [11, 46]],
      [[12, 47], [13, 48]],
    ]);

    expect(path.match(/M/g)).toHaveLength(2);
    expect(path.match(/L/g)).toHaveLength(2);
    expect(coordsToSvgPath([], 120, 44)).toBe("");
    expect(coordsToSvgPath([[10, 45]], 120, 44)).toBe("");
  });

  it("omits non-finite coordinates from SVG paths and bounds", () => {
    const finiteLine: [number, number][] = [[10, 45], [11, 46]];
    expect(polylinesToSvgPath([
      [[NaN, 44], ...finiteLine, [12, Infinity]],
      [[Infinity, 45], [12, NaN]],
    ])).toBe(coordsToSvgPath(finiteLine));
    expect(coordsToSvgPath([[10, 45], [NaN, 46]])).toBe("");
  });

  it("returns only complete points for a truncated encoded polyline", () => {
    const complete = decodeEncodedPolyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@");
    const truncatedAfterTwoPoints = decodeEncodedPolyline("_p~iF~ps|U_ulLnnqC_");

    expect(truncatedAfterTwoPoints).toEqual(complete.slice(0, 2));
    expect(decodeEncodedPolyline("")).toEqual([]);
  });
});

describe("Director presets with no route data", () => {
  it.each(["expedition-doc", "climb-spotlight", "social-reel"] as const)(
    "%s still produces ordered, bounded keyframes for an empty trip",
    (preset) => {
      const keyframes = generateDirectorKeyframes(preset, { stages: [] });

      expect(keyframes.length).toBeGreaterThan(0);
      expect(keyframes.length).toBeLessThanOrEqual(40);
      for (let index = 0; index < keyframes.length; index += 1) {
        const keyframe = keyframes[index]!;
        expect(keyframe.progress).toBeGreaterThanOrEqual(0);
        expect(keyframe.progress).toBeLessThanOrEqual(1);
        expect(keyframe.pitch_deg).toBeGreaterThanOrEqual(0);
        expect(keyframe.pitch_deg).toBeLessThanOrEqual(60);
        expect(keyframe.speed_multiplier).toBeGreaterThanOrEqual(0.25);
        expect(keyframe.speed_multiplier).toBeLessThanOrEqual(4);
        if (index > 0) {
          expect(keyframe.progress).toBeGreaterThan(keyframes[index - 1]!.progress);
        }
      }
    },
  );
});
