import { describe, expect, it } from "vitest";
import {
  createDefaultRenderSpecV2,
  type ProjectPhotoOut,
  type StageOut,
  type TelemetryGeometryFeature,
  type TelemetryTimelineOut,
} from "../lib/api/types";
import { matchPhotosToTimeline } from "../lib/geo/photos";
import { buildTimelineFromProject, haversineMeters } from "../lib/geo/timeline";

const STAGE_START_ISO = "2040-04-12T08:00:00Z";
const STAGE_START_MS = Date.parse(STAGE_START_ISO);
const KM_DEG_LON = 0.0089932;

function lonAtKm(km: number): number {
  return Number((km * KM_DEG_LON).toFixed(7));
}

function makeStage(): StageOut {
  return {
    id: "s-1",
    project_id: "p1",
    source_type: "gpx",
    external_source_id: null,
    strava_activity_id: null,
    name: "Long ride with lunch",
    stage_order: 0,
    day_index: 1,
    distance_m: 120_000,
    elevation_gain_m: 0,
    elevation_loss_m: 0,
    moving_seconds: 6 * 3600,
    elapsed_seconds: 8 * 3600,
    point_count: 121,
    has_timestamps: true,
    started_at: STAGE_START_ISO,
    min_lat: -0.01,
    max_lat: 0.01,
    min_lon: -0.01,
    max_lon: lonAtKm(120) + 0.01,
    trim_start_s: null,
    trim_end_s: null,
    warnings: [],
    created_at: "2040-04-11T08:00:00Z",
  } as StageOut;
}

function makeGeometry(): TelemetryGeometryFeature {
  const coords: [number, number][] = [];
  for (let km = 0; km <= 120; km += 1) {
    coords.push([lonAtKm(km), 0]);
  }
  return {
    type: "Feature",
    id: "s-1",
    geometry: { type: "MultiLineString", coordinates: [coords] },
    properties: {
      lod: "medium",
      point_count: coords.length,
      original_point_count: coords.length,
      segment_count: 1,
      bbox: [0, 0, lonAtKm(120), 0],
      crosses_antimeridian: false,
      stage_id: "s-1",
      name: "Long ride with lunch",
      day_index: 1,
      stage_order: 0,
      source_type: "gpx",
      distance_m: 120_000,
      elevation_gain_m: 0,
    },
  };
}

/** Rides 20 km/h to km 60, stops for 2 h, then rides 20 km/h to km 120. */
function makeRealTimeline(): TelemetryTimelineOut {
  const points: TelemetryTimelineOut["stages"][number]["points"] = [];
  for (let km = 0; km <= 60; km += 1) {
    points.push({
      lat: 0,
      lon: lonAtKm(km),
      cum_dist_m: km * 1000,
      elapsed_s: km * 180,
      ts_utc: new Date(STAGE_START_MS + km * 180_000).toISOString(),
      segment_idx: 0,
    });
  }
  const stopEndS = 3 * 3600 + 2 * 3600;
  points.push({
    lat: 0,
    lon: lonAtKm(60),
    cum_dist_m: 60_000,
    elapsed_s: stopEndS,
    ts_utc: new Date(STAGE_START_MS + stopEndS * 1000).toISOString(),
    segment_idx: 0,
  });
  for (let km = 61; km <= 120; km += 1) {
    const elapsedS = stopEndS + (km - 60) * 180;
    points.push({
      lat: 0,
      lon: lonAtKm(km),
      cum_dist_m: km * 1000,
      elapsed_s: elapsedS,
      ts_utc: new Date(STAGE_START_MS + elapsedS * 1000).toISOString(),
      segment_idx: 0,
    });
  }
  return {
    project_id: "p1",
    stages: [{ stage_id: "s-1", stage_order: 0, points }],
  };
}

function makePhoto(takenAtIso: string, lon: number, lat: number): ProjectPhotoOut {
  return {
    id: "ph-after-stop",
    project_id: "p1",
    stage_id: "s-1",
    source_type: "upload",
    width: 800,
    height: 600,
    byte_size: 1000,
    caption: null,
    taken_at_utc: takenAtIso,
    lat,
    lon,
    matched_lat: null,
    matched_lon: null,
    matched_progress: null,
    match_method: "unmatched",
    created_at: "2040-04-12T09:00:00Z",
  };
}

describe("project timeline timestamps", () => {
  it("project timeline timestamp match lands on the photo position after a long stop", () => {
    const spec = createDefaultRenderSpecV2();
    const timeline = buildTimelineFromProject(
      [makeStage()],
      [makeGeometry()],
      undefined,
      spec,
      makeRealTimeline(),
    );
    const photoLon = lonAtKm(61);
    const photoTakenAt = new Date(STAGE_START_MS + (5 * 3600 + 180) * 1000).toISOString();
    const [matched] = matchPhotosToTimeline(
      [makePhoto(photoTakenAt, photoLon, 0)],
      timeline,
      spec,
      () => "/photo.jpg",
    );

    expect(matched?.matchMethod).toBe("timestamp");
    const [mLon, mLat] = matched!.matchedPosition!;
    expect(haversineMeters(mLon, mLat, photoLon, 0)).toBeLessThan(5);
  });

  it("keeps the estimated timestamps when no stage timeline points are supplied", () => {
    const spec = createDefaultRenderSpecV2();
    const timeline = buildTimelineFromProject([makeStage()], [makeGeometry()], undefined, spec);
    const stage = timeline[0]!;
    const pointAt61 = stage.points.reduce((best, pt) =>
      Math.abs(pt.cumDistM - 61_000) < Math.abs(best.cumDistM - 61_000) ? pt : best,
    );
    const expectedMs = STAGE_START_MS + (pointAt61.cumDistM / 120_000) * 8 * 3600 * 1000;

    expect(pointAt61.tsUtcMs).not.toBeNull();
    expect(Math.abs(pointAt61.tsUtcMs! - expectedMs)).toBeLessThan(1000);
  });
});
