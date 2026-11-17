// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2, type RenderSpecV2 } from "../lib/api/types";
import {
  computeBearingDeg,
  computeCameraFrame,
  interpolateCameraKeyframes,
  is3dCameraActive,
  normalizeBearingDeg,
} from "../lib/geo/camera";
import {
  formatDateInTimeZone,
  formatDistance,
  formatElapsedTime,
  formatElevation,
  formatGrade,
  formatHeartRate,
  formatPower,
  formatSpeed,
  formatTimeInTimeZone,
  formatVam,
  hexToRgbaCss,
  hexToRgbTuple,
  parseResolutionPreset,
} from "../lib/geo/format";
import { coordsToSvgPath, decodeEncodedPolyline } from "../lib/geo/polyline";
import {
  clamp01,
  computeFollowZoom,
  computeTimelineBounds,
  evaluateTimeline,
  frameIndexToProgress,
  type TimelinePoint,
  type TimelineStage,
} from "../lib/geo/timeline";

function makeStage(id: string, points: TimelinePoint[], stageOrder = 0): TimelineStage {
  const lastDist = points.length > 0 ? points[points.length - 1]!.cumDistM : 0;
  const lastElapsed = points.length > 0 ? points[points.length - 1]!.elapsedS : 0;
  return {
    id,
    name: `Stage ${id}`,
    dayIndex: stageOrder + 1,
    stageOrder,
    activityType: "cycling",
    color: "#38bdf8",
    transition: "none",
    distanceM: lastDist,
    elevationGainM: 0,
    movingSeconds: lastElapsed,
    elapsedSeconds: lastElapsed,
    hasTimestamps: true,
    points,
  };
}

function makePoint(
  lon: number,
  lat: number,
  cumDistM: number,
  elapsedS: number,
  globalDistM = cumDistM,
  globalElapsedS = elapsedS,
): TimelinePoint {
  return {
    lat,
    lon,
    lonUnwrapped: lon,
    elevM: 1200,
    cumDistM,
    globalDistM,
    speedMps: 6,
    gradePct: 2,
    elapsedS,
    globalElapsedS,
    tsUtcMs: 1_750_000_000_000 + elapsedS * 1000,
    segmentIdx: 0,
  };
}

const edgeNumberArb = fc.oneof(
  fc.double({ min: -1e6, max: 1e6, noNaN: false }),
  fc.constant(0),
  fc.constant(-0),
  fc.constant(Number.NaN),
  fc.constant(Number.POSITIVE_INFINITY),
  fc.constant(Number.NEGATIVE_INFINITY),
);

describe("Property-based tests: timeline.ts", () => {
  it("clamp01 always returns a finite number in [0, 1]", () => {
    fc.assert(
      fc.property(edgeNumberArb, (value) => {
        const result = clamp01(value);
        expect(Number.isFinite(result)).toBe(true);
        expect(result).toBeGreaterThanOrEqual(0);
        expect(result).toBeLessThanOrEqual(1);
      }),
    );
  });

  it("frameIndexToProgress handles t<0, t>duration, NaN, and is monotonic", () => {
    const spec = createDefaultRenderSpecV2();
    fc.assert(
      fc.property(
        fc.integer({ min: -50, max: 500 }),
        fc.integer({ min: 0, max: 300 }),
        fc.double({ min: -10, max: 120, noNaN: false }),
        fc.boolean(),
        (frameIndex, totalFrames, durationS, isPreview) => {
          const p = frameIndexToProgress(frameIndex, totalFrames, spec, durationS, isPreview);
          if (!Number.isNaN(durationS)) {
            expect(Number.isFinite(p)).toBe(true);
            expect(p).toBeGreaterThanOrEqual(0);
            expect(p).toBeLessThanOrEqual(1);
          }
          if (frameIndex <= 0 && totalFrames > 1 && durationS > 0) {
            expect(p).toBe(0);
          }
          if (frameIndex >= totalFrames - 1 && totalFrames > 1 && durationS > 0) {
            expect(p).toBe(1);
          }
        },
      ),
    );

    fc.assert(
      fc.property(
        fc.integer({ min: 2, max: 240 }),
        fc.integer({ min: 0, max: 239 }),
        fc.integer({ min: 0, max: 239 }),
        fc.double({ min: 1, max: 60, noNaN: true }),
        (totalFrames, a, b, durationS) => {
          const lo = Math.min(a, b);
          const hi = Math.max(a, b);
          const pLo = frameIndexToProgress(lo, totalFrames, spec, durationS, false);
          const pHi = frameIndexToProgress(hi, totalFrames, spec, durationS, false);
          expect(pLo).toBeLessThanOrEqual(pHi);
        },
      ),
    );
  });

  it("evaluateTimeline handles 0-stage and 1-point stage for any progressRatio (t<0, t>1, NaN)", () => {
    const spec = createDefaultRenderSpecV2();

    fc.assert(
      fc.property(edgeNumberArb, (progress) => {
        const emptyState = evaluateTimeline([], spec, progress);
        expect(emptyState.activeStage).toBeNull();
        expect(emptyState.totalDistanceM).toBe(0);
        expect(emptyState.visiblePaths).toEqual([]);
        expect(Number.isFinite(emptyState.markerPosition[0])).toBe(true);
        expect(Number.isFinite(emptyState.markerPosition[1])).toBe(true);
        expect(emptyState.progressRatio).toBeGreaterThanOrEqual(0);
        expect(emptyState.progressRatio).toBeLessThanOrEqual(1);

        const singlePointStage = makeStage("single", [makePoint(0.5897, 0.2925, 0, 0)]);
        const singleState = evaluateTimeline([singlePointStage], spec, progress);
        expect(singleState.activeStage?.id).toBe("single");
        expect(singleState.markerPosition[0]).toBeCloseTo(0.5897, 6);
        expect(singleState.markerPosition[1]).toBeCloseTo(0.2925, 6);
        expect(singleState.distanceRemainingM).toBeGreaterThanOrEqual(0);
        expect(singleState.bounds[2]).toBeGreaterThan(singleState.bounds[0]);
        expect(singleState.bounds[3]).toBeGreaterThan(singleState.bounds[1]);
      }),
    );
  });

  it("evaluateTimeline preserves distance conservation and monotonicity on multi-point stages", () => {
    const spec = createDefaultRenderSpecV2();
    const stages = [
      makeStage(
        "s1",
        [
          makePoint(0.4512, 0.2173, 0, 0, 0, 0),
          makePoint(0.5325, 0.2352, 1500, 150, 1500, 150),
          makePoint(0.5608, 0.2537, 3000, 300, 3000, 300),
        ],
        0,
      ),
      makeStage(
        "s2",
        [
          makePoint(0.5608, 0.2537, 0, 0, 3000, 300),
          makePoint(0.6192, 0.2728, 2000, 200, 5000, 500),
        ],
        1,
      ),
    ];

    fc.assert(
      fc.property(
        fc.double({ min: 0, max: 1, noNaN: true }),
        fc.double({ min: 0, max: 1, noNaN: true }),
        (r1, r2) => {
          const lo = Math.min(r1, r2);
          const hi = Math.max(r1, r2);
          const sLo = evaluateTimeline(stages, spec, lo);
          const sHi = evaluateTimeline(stages, spec, hi);

          expect(sLo.distanceTravelledM + sLo.distanceRemainingM).toBeCloseTo(
            sLo.totalDistanceM,
            5,
          );
          expect(sLo.distanceTravelledM).toBeLessThanOrEqual(sHi.distanceTravelledM + 1e-6);
          expect(sLo.elapsedSeconds).toBeLessThanOrEqual(sHi.elapsedSeconds + 1e-6);
        },
      ),
    );
  });

  it("computeFollowZoom always stays within [11.0, 15.2] and computeTimelineBounds is non-degenerate", () => {
    fc.assert(
      fc.property(
        fc.double({ min: -170, max: 170, noNaN: true }),
        fc.double({ min: -80, max: 80, noNaN: true }),
        fc.double({ min: 0, max: 10, noNaN: true }),
        fc.double({ min: 0, max: 10, noNaN: true }),
        (lon, lat, dLon, dLat) => {
          const bounds: [number, number, number, number] = [lon, lat, lon + dLon, lat + dLat];
          const zoom = computeFollowZoom(bounds);
          expect(zoom).toBeGreaterThanOrEqual(11.0);
          expect(zoom).toBeLessThanOrEqual(15.2);

          const computed = computeTimelineBounds([
            makeStage("b", [makePoint(lon, lat, 0, 0)]),
          ]);
          expect(computed[2]).toBeGreaterThan(computed[0]);
          expect(computed[3]).toBeGreaterThan(computed[1]);
        },
      ),
    );
  });
});

describe("Property-based tests: camera.ts", () => {
  it("normalizeBearingDeg always returns a finite angle in [0, 360)", () => {
    fc.assert(
      fc.property(edgeNumberArb, (deg) => {
        const norm = normalizeBearingDeg(deg);
        expect(Number.isFinite(norm)).toBe(true);
        expect(norm).toBeGreaterThanOrEqual(0);
        expect(norm).toBeLessThan(360);
      }),
    );
  });

  it("handles bearing wrap-around across 0/360 (355 -> 5 deg)", () => {
    expect(normalizeBearingDeg(-5)).toBe(355);
    expect(normalizeBearingDeg(365)).toBe(5);
    expect(normalizeBearingDeg(355 + 10)).toBe(5);

    // Constructed rays straddling north, independent of recorded activity data.
    const nne = computeBearingDeg([0, 0], [0.001, 0.01]);
    const nnw = computeBearingDeg([0, 0], [-0.001, 0.01]);
    expect(nne).not.toBeNull();
    expect(nnw).not.toBeNull();
    expect(nne!).toBeGreaterThanOrEqual(0);
    expect(nne!).toBeLessThan(20);
    expect(nnw!).toBeGreaterThan(340);
    expect(nnw!).toBeLessThan(360);

    // Coincident points return null
    expect(computeBearingDeg([0.4512, 0.2173], [0.4512, 0.2173])).toBeNull();
  });

  it("computeCameraFrame keeps pitch in [0, 60] and bearing in [0, 360) across presets", () => {
    const stage = makeStage("cam", [
      makePoint(0.4512, 0.2173, 0, 0),
      makePoint(0.5325, 0.2537, 2000, 200),
    ]);
    const presets: RenderSpecV2["camera_preset"][] = ["flat", "dynamic-3d", "drone-orbit"];

    fc.assert(
      fc.property(
        fc.constantFrom(...presets),
        fc.double({ min: -10, max: 90, noNaN: true }),
        fc.double({ min: -720, max: 720, noNaN: true }),
        fc.double({ min: 0, max: 1, noNaN: true }),
        fc.boolean(),
        (preset, pitchDeg, bearingDeg, progress, terrain3d) => {
          const spec = createDefaultRenderSpecV2();
          spec.camera_preset = preset;
          spec.camera_pitch_deg = pitchDeg;
          spec.camera_bearing_deg = bearingDeg;
          spec.terrain_3d = terrain3d;

          const evaluated = evaluateTimeline([stage], spec, progress);
          const frame = computeCameraFrame(spec, evaluated);
          expect(frame.pitch).toBeGreaterThanOrEqual(0);
          expect(frame.pitch).toBeLessThanOrEqual(60);
          expect(frame.bearing).toBeGreaterThanOrEqual(0);
          expect(frame.bearing).toBeLessThan(360);
          expect(is3dCameraActive(spec)).toBe(
            terrain3d || preset !== "flat" || pitchDeg > 0,
          );
        },
      ),
    );
  });

  it("drone-orbit bearing wraps smoothly from 355 -> 5 deg", () => {
    const spec = createDefaultRenderSpecV2();
    spec.camera_preset = "drone-orbit";
    spec.camera_bearing_deg = 355;
    const stage = makeStage("orb", [
      makePoint(0.4512, 0.2173, 0, 0),
      makePoint(0.5325, 0.2537, 2000, 200),
    ]);
    // At progressRatio = 10 / 120, orbitAzimuth = 10 -> 355 + 10 = 365 -> 5 deg
    const evaluated = evaluateTimeline([stage], spec, 10 / 120);
    const frame = computeCameraFrame(spec, evaluated);
    expect(frame.bearing).toBeCloseTo(5, 5);
  });
});

describe("Property-based tests: format.ts", () => {
  it("formatDistance, formatElevation, formatSpeed, and formatElapsedTime handle 0, Infinity, and negative values", () => {
    fc.assert(
      fc.property(
        edgeNumberArb,
        fc.constantFrom("metric" as const, "imperial" as const),
        (val, units) => {
          const dist = formatDistance(val, units);
          expect(dist).not.toContain("NaN");
          expect(dist).not.toContain("Infinity");
          expect(dist.startsWith("-")).toBe(false);
          expect(dist.endsWith(units === "imperial" ? " mi" : " km")).toBe(true);

          const elev = formatElevation(val, units);
          expect(elev).not.toContain("NaN");
          expect(elev).not.toContain("Infinity");
          if (!Number.isFinite(val)) {
            expect(elev).toBe(units === "imperial" ? "-- ft" : "-- m");
          }

          const spd = formatSpeed(val, units);
          expect(spd).not.toContain("NaN");
          expect(spd).not.toContain("Infinity");
          expect(spd.startsWith("-")).toBe(false);
          expect(spd.endsWith(units === "imperial" ? " mph" : " km/h")).toBe(true);
        },
      ),
    );

    fc.assert(
      fc.property(
        fc.oneof(
          fc.integer({ min: -10000, max: 359999 }),
          fc.constant(0),
          fc.constant(null),
          fc.constant(undefined),
        ),
        (secs) => {
          const formatted = formatElapsedTime(secs);
          expect(formatted).toMatch(/^\d{2,}:\d{2}:\d{2}$/);
        },
      ),
    );
  });

  it("hexToRgbTuple and hexToRgbaCss handle invalid hex strings and out-of-range opacity", () => {
    fc.assert(
      fc.property(
        fc.string(),
        fc.double({ min: -5, max: 5, noNaN: true }),
        (hex, opacity) => {
          const [r, g, b] = hexToRgbTuple(hex);
          expect(r).toBeGreaterThanOrEqual(0);
          expect(r).toBeLessThanOrEqual(255);
          expect(g).toBeGreaterThanOrEqual(0);
          expect(g).toBeLessThanOrEqual(255);
          expect(b).toBeGreaterThanOrEqual(0);
          expect(b).toBeLessThanOrEqual(255);

          const rgba = hexToRgbaCss(hex, opacity);
          expect(rgba).toMatch(/^rgba\(\d+, \d+, \d+, [\d.eE+-]+\)$/);
        },
      ),
    );
  });

  it("parseResolutionPreset and timezone formatters handle edge inputs safely", () => {
    expect(parseResolutionPreset("1920x1080")).toEqual({ width: 1920, height: 1080 });
    expect(parseResolutionPreset("1080x1080")).toEqual({ width: 1080, height: 1080 });
    expect(parseResolutionPreset("invalid" as never)).toEqual({ width: 1920, height: 1080 });

    expect(formatDateInTimeZone(null, "UTC")).toBe("--");
    expect(formatDateInTimeZone("not-a-date", "UTC")).toBe("--");
    expect(formatDateInTimeZone("2001-06-14T10:00:00Z", "Invalid/Zone")).toMatch(/2001/);
    expect(formatTimeInTimeZone(undefined, "UTC")).toBe("--:--");
    expect(formatTimeInTimeZone("not-a-date", "UTC")).toBe("--:--");
    expect(formatTimeInTimeZone("2001-06-14T10:30:45Z", "Invalid/Zone")).toBe("10:30:45");
  });

  it("formatGrade, formatHeartRate, formatPower, and formatVam handle edge numbers safely", () => {
    fc.assert(
      fc.property(edgeNumberArb, fc.constantFrom("metric" as const, "imperial" as const), (val, units) => {
        const grade = formatGrade(val);
        expect(grade).not.toContain("NaN");
        expect(grade.endsWith("%")).toBe(true);

        const hr = formatHeartRate(val);
        expect(hr).not.toContain("NaN");
        expect(hr.endsWith("bpm")).toBe(true);

        const power = formatPower(val);
        expect(power).not.toContain("NaN");
        expect(power.endsWith("W")).toBe(true);

        const vam = formatVam(val, units);
        expect(vam).not.toContain("NaN");
        expect(vam.endsWith(units === "imperial" ? "ft/h" : "m/h")).toBe(true);
      }),
    );
  });
});

describe("Property-based tests: polyline.ts & keyframe camera interpolation", () => {
  it("decodes Google Encoded Polylines to [lon, lat] and projects SVG paths safely", () => {
    const coords = decodeEncodedPolyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@");
    expect(coords).toHaveLength(3);
    expect(coords[0]?.[0]).toBeCloseTo(-120.2, 4);
    expect(coords[0]?.[1]).toBeCloseTo(38.5, 4);
    expect(coords[1]?.[0]).toBeCloseTo(-120.95, 4);
    expect(coords[1]?.[1]).toBeCloseTo(40.7, 4);
    expect(coords[2]?.[0]).toBeCloseTo(-126.453, 3);
    expect(coords[2]?.[1]).toBeCloseTo(43.252, 3);

    const svg = coordsToSvgPath(coords, 120, 44, 4);
    expect(svg.startsWith("M")).toBe(true);
    expect(svg).toContain("L");

    fc.assert(
      fc.property(fc.string(), (arbitraryStr) => {
        const res = decodeEncodedPolyline(arbitraryStr);
        for (const [lon, lat] of res) {
          expect(Number.isFinite(lon)).toBe(true);
          expect(Number.isFinite(lat)).toBe(true);
          expect(Math.abs(lat)).toBeLessThanOrEqual(90);
          expect(Math.abs(lon)).toBeLessThanOrEqual(180);
        }
      }),
    );
  });

  it("interpolateCameraKeyframes smoothsteps keyframes monotonically and bounds outputs", () => {
    const spec = createDefaultRenderSpecV2();
    const keyframes = [
      {
        progress: 0.2,
        camera_mode: "follow-marker" as const,
        camera_preset: "flat" as const,
        pitch_deg: 10,
        bearing_offset_deg: -20,
        zoom_offset: -1,
        speed_multiplier: 1.0,
        label: "Start",
      },
      {
        progress: 0.8,
        camera_mode: "follow-marker" as const,
        camera_preset: "dynamic-3d" as const,
        pitch_deg: 50,
        bearing_offset_deg: 40,
        zoom_offset: 2,
        speed_multiplier: 2.5,
        label: "Climb",
      },
    ];

    fc.assert(
      fc.property(edgeNumberArb, (progress) => {
        const cue = interpolateCameraKeyframes(keyframes, progress, spec);
        expect(cue.pitchDeg).toBeGreaterThanOrEqual(10);
        expect(cue.pitchDeg).toBeLessThanOrEqual(50);
        expect(cue.zoomOffset).toBeGreaterThanOrEqual(-1);
        expect(cue.zoomOffset).toBeLessThanOrEqual(2);
        expect(cue.speedMultiplier).toBeGreaterThanOrEqual(1.0);
        expect(cue.speedMultiplier).toBeLessThanOrEqual(2.5);
      }),
    );

    const mid = interpolateCameraKeyframes(keyframes, 0.5, spec);
    expect(mid.pitchDeg).toBeCloseTo(30, 5);
    expect(mid.bearingOffsetDeg).toBeCloseTo(10, 5);
    expect(mid.zoomOffset).toBeCloseTo(0.5, 5);

    const stage = makeStage("kf", [
      makePoint(0.4512, 0.2173, 0, 0),
      makePoint(0.5325, 0.2537, 2000, 200),
    ]);
    const kfSpec = { ...spec, camera_keyframes: keyframes };
    const frame = computeCameraFrame(kfSpec, evaluateTimeline([stage], kfSpec, 0.5));
    expect(frame.pitch).toBeGreaterThanOrEqual(0);
    expect(frame.pitch).toBeLessThanOrEqual(60);
    expect(frame.bearing).toBeGreaterThanOrEqual(0);
    expect(frame.bearing).toBeLessThan(360);
  });
});
