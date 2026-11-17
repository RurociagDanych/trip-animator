// Activity coordinates below use a constructed numeric grid, not a recorded GPS route.
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2, type PanelPosition } from "../lib/api/types";
import {
  projectLngLatToScreen,
  rectsIntersect,
  solveStageLayout,
} from "../lib/geo/layout";
import {
  evaluateTimeline,
  type TimelinePoint,
  type TimelineStage,
} from "../lib/geo/timeline";

function makePoint(lon: number, lat: number, cumDistM: number, elapsedS: number): TimelinePoint {
  return {
    lat,
    lon,
    lonUnwrapped: lon,
    elevM: 1200,
    cumDistM,
    globalDistM: cumDistM,
    speedMps: 6,
    gradePct: 1,
    elapsedS,
    globalElapsedS: elapsedS,
    tsUtcMs: 1_750_000_000_000 + elapsedS * 1000,
    segmentIdx: 0,
  };
}

function makeLinearStage(points: TimelinePoint[]): TimelineStage {
  const last = points[points.length - 1]!;
  return {
    id: "stage-layout",
    name: "Layout Stage",
    dayIndex: 1,
    stageOrder: 0,
    activityType: "cycling",
    color: "#38bdf8",
    transition: "none",
    distanceM: last.cumDistM,
    elevationGainM: 200,
    movingSeconds: last.elapsedS,
    elapsedSeconds: last.elapsedS,
    hasTimestamps: true,
    points,
  };
}

describe("Geo layout & collision solver (layout.ts)", () => {
  it("clamps fitPadding so left+right <= 0.55*W and top+bottom <= 0.55*H across all viewports", () => {
    const stage = makeLinearStage([
      makePoint(0.4512, 0.2173, 0, 0),
      makePoint(0.7113, 0.3552, 15_000, 1500),
    ]);

    fc.assert(
      fc.property(
        fc.integer({ min: 280, max: 1920 }),
        fc.integer({ min: 280, max: 1920 }),
        fc.constantFrom<PanelPosition>("top", "bottom", "left", "right"),
        fc.double({ min: 0.5, max: 2.0, noNaN: true }),
        fc.boolean(),
        (viewportW, viewportH, position, scale, elevOverlay) => {
          const spec = createDefaultRenderSpecV2();
          spec.camera_mode = "fit-track";
          spec.elevation_chart_overlay = elevOverlay;
          spec.statistics_panel.position = position;
          spec.statistics_panel.scale = scale;

          const evaluated = evaluateTimeline([stage], spec, 0.5);
          const layout = solveStageLayout({
            stages: [stage],
            spec,
            evaluated,
            viewportW,
            viewportH,
            compact: true,
            isPreview: true,
            hasActivePhoto: true,
          });

          expect(layout.fitPadding.left + layout.fitPadding.right).toBeLessThanOrEqual(
            Math.floor(viewportW * 0.55),
          );
          expect(layout.fitPadding.top + layout.fitPadding.bottom).toBeLessThanOrEqual(
            Math.floor(viewportH * 0.55),
          );
          expect(layout.followPadding.left + layout.followPadding.right).toBeLessThanOrEqual(
            Math.floor(viewportW * 0.55),
          );
          expect(layout.followPadding.top + layout.followPadding.bottom).toBeLessThanOrEqual(
            Math.floor(viewportH * 0.55),
          );
        },
      ),
    );
  });

  it("avoids self-overlap between HUD, elevation overlay, photo card, and watermark across 1920x1080 and 1080x1920", () => {
    const stage = makeLinearStage([
      makePoint(0.4512, 0.2173, 0, 0),
      makePoint(0.7113, 0.3552, 15_000, 1500),
    ]);
    const viewports = [
      { w: 1920, h: 1080 },
      { w: 1080, h: 1920 },
    ];
    const positions: PanelPosition[] = ["top", "bottom", "left", "right"];

    for (const vp of viewports) {
      for (const pos of positions) {
        const spec = createDefaultRenderSpecV2();
        spec.camera_mode = "fit-track";
        spec.elevation_chart_overlay = true;
        spec.statistics_panel.enabled = true;
        spec.statistics_panel.position = pos;

        const evaluated = evaluateTimeline([stage], spec, 0.5);
        const layout = solveStageLayout({
          stages: [stage],
          spec,
          evaluated,
          viewportW: vp.w,
          viewportH: vp.h,
          compact: false,
          isPreview: true,
          hasActivePhoto: true,
        });

        const boxes = [
          layout.hud?.rect,
          layout.elevation?.rect,
          layout.photoCard?.rect,
          layout.watermarkRect,
        ].filter((b): b is NonNullable<typeof b> => Boolean(b));

        for (let i = 0; i < boxes.length; i++) {
          for (let j = i + 1; j < boxes.length; j++) {
            expect(rectsIntersect(boxes[i]!, boxes[j]!)).toBe(false);
          }
        }
      }
    }
  });

  it("projects lng/lat deterministically with bearing and pitch", () => {
    const center: [number, number] = [0.5897, 0.3128];
    const centerScr = projectLngLatToScreen(center, center, 12, 0, 0, 1920, 1080);
    expect(centerScr.visible).toBe(true);
    expect(centerScr.x).toBeCloseTo(960, 2);
    expect(centerScr.y).toBeCloseTo(540, 2);

    // Facing East (bearing 90 deg), a point slightly East should appear above screen center (y < 540)
    const eastPoint: [number, number] = [0.6493, 0.3128];
    const eastBearing90 = projectLngLatToScreen(eastPoint, center, 12, 45, 90, 1920, 1080);
    expect(eastBearing90.visible).toBe(true);
    expect(eastBearing90.y).toBeLessThan(540);
    expect(Math.abs(eastBearing90.x - 960)).toBeLessThan(2);
  });

  it("deterministically repositions HUD away from colliding follow-marker route when collision_avoidance is true", () => {
    // Route running directly North (333 km over 20s) so projected points pass upward right through top-center HUD
    const stage = makeLinearStage([
      makePoint(0.4, -1.5, 0, 0),
      makePoint(0.4, 0, 166_500, 16_650),
      makePoint(0.4, 1.5, 333_000, 33_300),
    ]);
    const spec = createDefaultRenderSpecV2();
    spec.camera_mode = "follow-marker";
    spec.statistics_panel.position = "top";
    spec.statistics_panel.collision_avoidance = true;

    const evaluated = evaluateTimeline([stage], spec, 0.5);
    const followCamera = {
      center: evaluated.markerPosition,
      zoom: 9.5,
      pitch: 0,
      bearing: 0,
    };

    const run1 = solveStageLayout({
      stages: [stage],
      spec,
      evaluated,
      viewportW: 1280,
      viewportH: 720,
      compact: false,
      isPreview: false,
      hasActivePhoto: false,
      followCamera,
      travelDurationS: 20,
    });
    const run2 = solveStageLayout({
      stages: [stage],
      spec,
      evaluated,
      viewportW: 1280,
      viewportH: 720,
      compact: false,
      isPreview: false,
      hasActivePhoto: false,
      followCamera,
      travelDurationS: 20,
    });

    // Pure temporal determinism
    expect(run1.hud).toEqual(run2.hud);
    // Shifted away from top-center because route travels straight through top-center
    expect(run1.hud?.slot).not.toBe("top-center");

    // With collision_avoidance disabled, stays at nominal top-center
    const disabledSpec = {
      ...spec,
      statistics_panel: { ...spec.statistics_panel, collision_avoidance: false },
    };
    const disabledRun = solveStageLayout({
      stages: [stage],
      spec: disabledSpec,
      evaluated,
      viewportW: 1280,
      viewportH: 720,
      compact: false,
      isPreview: false,
      hasActivePhoto: false,
      followCamera,
      travelDurationS: 20,
    });
    expect(disabledRun.hud?.slot).toBe("top-center");
  });

  it("places polaroid/cinematic photo cards, chapter cards, finale poster, and climb callouts without collisions", () => {
    const stage = makeLinearStage([
      makePoint(0.4512, 0.2173, 0, 0),
      makePoint(0.7113, 0.3552, 15_000, 1500),
    ]);
    const viewports = [
      { w: 1920, h: 1080 },
      { w: 1080, h: 1920 },
    ];

    for (const vp of viewports) {
      for (const photoStyle of ["polaroid", "cinematic"] as const) {
        const spec = createDefaultRenderSpecV2();
        spec.camera_mode = "fit-track";
        spec.elevation_chart_overlay = true;
        spec.photo_card_style = photoStyle;
        spec.show_chapter_cards = true;
        spec.show_finale_poster = true;
        spec.show_climb_callouts = true;

        const evaluated = evaluateTimeline([stage], spec, 0.5);
        const layout = solveStageLayout({
          stages: [stage],
          spec,
          evaluated,
          viewportW: vp.w,
          viewportH: vp.h,
          compact: false,
          isPreview: true,
          hasActivePhoto: true,
          hasActiveChapterCard: true,
          hasActiveFinalePoster: true,
          climbCalloutTargets: [
            { id: "c1", screenX: vp.w * 0.45, screenY: vp.h * 0.4 },
            { id: "c2", screenX: vp.w * 0.47, screenY: vp.h * 0.42 },
          ],
        });

        expect(layout.photoCard?.style).toBe(photoStyle);
        expect(layout.chapterCard).not.toBeNull();
        expect(layout.finalePoster).not.toBeNull();

        const boxes = [
          layout.hud?.rect,
          layout.elevation?.rect,
          layout.photoCard?.rect,
          layout.chapterCard?.rect,
          layout.finalePoster?.rect,
          layout.watermarkRect,
          ...layout.climbCallouts.map((c) => c.rect),
        ].filter((b): b is NonNullable<typeof b> => Boolean(b));

        for (let i = 0; i < boxes.length; i++) {
          for (let j = i + 1; j < boxes.length; j++) {
            expect(rectsIntersect(boxes[i]!, boxes[j]!)).toBe(false);
          }
        }
      }
    }
  });
});
