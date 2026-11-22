import { describe, expect, it } from "vitest";
import { createDefaultRenderSpecV2, type ProjectPhotoOut } from "../lib/api/types";
import { matchPhotosToTimeline } from "../lib/geo/photos";
import { haversineMeters, type TimelinePoint, type TimelineStage } from "../lib/geo/timeline";

const START_MS = Date.UTC(2040, 3, 12, 8, 0, 0);
const ROUTE_END_LON = 0.1;
const ROUTE_LENGTH_M = haversineMeters(0, 0, ROUTE_END_LON, 0);
const METERS_PER_DEGREE = ROUTE_LENGTH_M / ROUTE_END_LON;

function point(lon: number, elapsedS: number): TimelinePoint {
  const distM = lon * METERS_PER_DEGREE;
  return {
    lat: 0,
    lon,
    lonUnwrapped: lon,
    elevM: 0,
    cumDistM: distM,
    globalDistM: distM,
    speedMps: ROUTE_LENGTH_M / 1000,
    gradePct: 0,
    elapsedS,
    globalElapsedS: elapsedS,
    tsUtcMs: START_MS + elapsedS * 1000,
    segmentIdx: 0,
  };
}

function equatorStage(): TimelineStage {
  return {
    id: "stage-1",
    name: "Fictional Equator Ride",
    dayIndex: 1,
    stageOrder: 0,
    activityType: "cycling",
    color: "#38bdf8",
    transition: "none",
    distanceM: ROUTE_LENGTH_M,
    elevationGainM: 0,
    movingSeconds: 1000,
    elapsedSeconds: 1000,
    hasTimestamps: true,
    points: [point(0, 0), point(0.05, 500), point(ROUTE_END_LON, 1000)],
  };
}

function photo(overrides: Partial<ProjectPhotoOut>): ProjectPhotoOut {
  return {
    id: "photo-1",
    project_id: "proj-1",
    stage_id: null,
    source_type: "upload",
    width: 640,
    height: 480,
    byte_size: 24_000,
    caption: null,
    taken_at_utc: null,
    lat: null,
    lon: null,
    matched_lat: null,
    matched_lon: null,
    matched_progress: null,
    match_method: "unmatched",
    enabled: true,
    created_at: "2040-04-12T09:00:00Z",
    ...overrides,
  };
}

const MIDWAY_ISO = new Date(START_MS + 500_000).toISOString();

function match(input: ProjectPhotoOut) {
  const [result] = matchPhotosToTimeline([input], [equatorStage()], createDefaultRenderSpecV2());
  return result!;
}

describe("photo pin position", () => {
  it("places a timestamp-matched pin on the route point when photo GPS is 1 km off the route", () => {
    const offLat = 1000 / METERS_PER_DEGREE;
    const result = match(photo({ taken_at_utc: MIDWAY_ISO, lat: offLat, lon: 0.05 }));

    expect(result.matchMethod).toBe("timestamp");
    expect(result.matchedPosition).not.toBeNull();
    expect(result.matchedPosition![0]).toBeCloseTo(0.05, 6);
    expect(result.matchedPosition![1]).toBeCloseTo(0, 6);
    expect(result.pinPosition).toEqual(result.matchedPosition);
    expect(result.lat).toBe(offLat);
    expect(result.lon).toBe(0.05);
  });

  it("places a gps-matched pin on the projected route point, not on the raw GPS", () => {
    const offLat = 500 / METERS_PER_DEGREE;
    const result = match(photo({ lat: offLat, lon: 0.03 }));

    expect(result.matchMethod).toBe("gps");
    expect(result.pinPosition).toEqual(result.matchedPosition);
    expect(result.pinPosition![1]).toBeCloseTo(0, 6);
  });

  it("places a manual pin on the pinned route point even when the photo has GPS", () => {
    const result = match(
      photo({
        lat: 0.02,
        lon: 0.02,
        match_method: "manual",
        matched_lon: 0.07,
        matched_lat: 0,
        matched_progress: 0.7,
      }),
    );

    expect(result.matchMethod).toBe("manual");
    expect(result.matchedPosition).toEqual([0.07, 0]);
    expect(result.pinPosition).toEqual([0.07, 0]);
    expect(result.matchedProgress).toBeCloseTo(0.7, 6);
  });
});

describe("timestamp to GPS sanity threshold", () => {
  it("rejects a timestamp match whose route point is 5 km from the photo GPS", () => {
    const gpsLon = 0.05 + 5000 / METERS_PER_DEGREE;
    const result = match(photo({ taken_at_utc: MIDWAY_ISO, lat: 0, lon: gpsLon }));

    expect(result.matchMethod).not.toBe("timestamp");
    expect(result.matchMethod).toBe("gps");
    expect(result.matchedPosition![0]).toBeCloseTo(gpsLon, 6);
    expect(result.pinPosition).toEqual(result.matchedPosition);
  });

  it("accepts a timestamp match whose route point is 1.5 km from the photo GPS", () => {
    const gpsLon = 0.05 + 1500 / METERS_PER_DEGREE;
    const result = match(photo({ taken_at_utc: MIDWAY_ISO, lat: 0, lon: gpsLon }));

    expect(result.matchMethod).toBe("timestamp");
    expect(result.matchedPosition![0]).toBeCloseTo(0.05, 6);
    expect(result.matchedProgress).toBeCloseTo(0.5, 6);
  });

  it("leaves the photo unmatched when the timestamp match is rejected and GPS is too far from the route", () => {
    const result = match(photo({ taken_at_utc: MIDWAY_ISO, lat: 1, lon: 0.05 }));

    expect(result.matchMethod).toBe("unmatched");
    expect(result.matchedPosition).toBeNull();
    expect(result.pinPosition).toBeNull();
    expect(result.matchedProgress).toBeNull();
  });
});
