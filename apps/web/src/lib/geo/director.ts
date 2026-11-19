import type {
  CameraKeyframe,
  ClimbHighlightOut,
  DirectorPreset,
  ManifestClimb,
  RenderSpecV2,
} from "../api/types";
import type { MatchedPhoto } from "./photos";
import {
  clamp01,
  mapClimbsToTimelineProgress,
  type TimelineStage,
} from "./timeline";

export interface DirectorPresetInput {
  stages: readonly TimelineStage[];
  spec?: RenderSpecV2;
  climbs?: readonly (ClimbHighlightOut | ManifestClimb)[];
  photos?: readonly MatchedPhoto[];
}

function sanitizeKeyframes(keyframes: readonly CameraKeyframe[]): CameraKeyframe[] {
  const sorted = [...keyframes]
    .filter((kf) => Number.isFinite(kf.progress))
    .map((kf) => ({
      progress: Math.round(clamp01(kf.progress) * 10000) / 10000,
      camera_mode: kf.camera_mode,
      camera_preset: kf.camera_preset,
      pitch_deg: Math.min(60, Math.max(0, Number.isFinite(kf.pitch_deg) ? kf.pitch_deg : 45)),
      bearing_offset_deg: Math.min(
        180,
        Math.max(-180, Number.isFinite(kf.bearing_offset_deg) ? kf.bearing_offset_deg : 0),
      ),
      zoom_offset: Math.min(3, Math.max(-3, Number.isFinite(kf.zoom_offset) ? kf.zoom_offset : 0)),
      speed_multiplier: Math.min(
        4,
        Math.max(0.25, Number.isFinite(kf.speed_multiplier) ? kf.speed_multiplier : 1),
      ),
      label: kf.label.slice(0, 64),
    }))
    .sort((a, b) => a.progress - b.progress);

  // Deduplicate nearly identical progress timestamps to keep smoothstep spans well-conditioned
  const deduped: CameraKeyframe[] = [];
  for (const kf of sorted) {
    const prev = deduped[deduped.length - 1];
    if (prev && Math.abs(kf.progress - prev.progress) < 0.015) {
      deduped[deduped.length - 1] = kf;
    } else {
      deduped.push(kf);
    }
  }
  return deduped.slice(0, 40);
}

function findPeakElevationProgress(stages: readonly TimelineStage[]): number | null {
  let maxElev = -Infinity;
  let maxDist = 0;
  let peakDist = 0;

  for (const stage of stages) {
    for (const pt of stage.points) {
      if (pt.globalDistM > maxDist) maxDist = pt.globalDistM;
      if (pt.elevM !== null && Number.isFinite(pt.elevM) && pt.elevM > maxElev) {
        maxElev = pt.elevM;
        peakDist = pt.globalDistM;
      }
    }
  }

  if (!Number.isFinite(maxElev) || maxDist <= 0) {
    return null;
  }
  return clamp01(peakDist / maxDist);
}

/**
 * Expedition Documentary: overview fly-in -> Day 1 3D chase -> summit/photo orbit -> overview pull-back.
 */
export function generateExpeditionDocKeyframes(input: DirectorPresetInput): CameraKeyframe[] {
  const { stages, spec, climbs = [], photos = [] } = input;
  const mappedClimbs =
    spec && climbs.length > 0 ? mapClimbsToTimelineProgress(climbs, stages, spec) : [];
  const topClimb =
    mappedClimbs.length > 0
      ? [...mappedClimbs].sort((a, b) => b.fietsScore - a.fietsScore)[0]!
      : null;
  const matchedPhoto = photos.find(
    (p) => p.matchedProgress !== null && p.matchedProgress > 0.2 && p.matchedProgress < 0.85,
  );
  const peakProgress =
    topClimb?.summitProgress ??
    findPeakElevationProgress(stages) ??
    matchedPhoto?.matchedProgress ??
    0.52;
  const spotlightProgress = Math.min(0.8, Math.max(0.24, peakProgress));

  const keyframes: CameraKeyframe[] = [
    {
      progress: 0.0,
      camera_mode: "fit-track",
      camera_preset: "flat",
      pitch_deg: 18,
      bearing_offset_deg: 0,
      zoom_offset: 0,
      speed_multiplier: 1.0,
      label: "Overview Fly-in",
    },
    {
      progress: 0.12,
      camera_mode: "follow-marker",
      camera_preset: "dynamic-3d",
      pitch_deg: 46,
      bearing_offset_deg: 0,
      zoom_offset: 0.4,
      speed_multiplier: 1.0,
      label: "3D Chase",
    },
    {
      progress: spotlightProgress,
      camera_mode: "follow-marker",
      camera_preset: "drone-orbit",
      pitch_deg: 52,
      bearing_offset_deg: 30,
      zoom_offset: 0.8,
      speed_multiplier: 0.75,
      label: topClimb ? `${topClimb.name} Orbit` : "Summit Orbit",
    },
    {
      progress: Math.min(0.88, spotlightProgress + 0.14),
      camera_mode: "follow-marker",
      camera_preset: "dynamic-3d",
      pitch_deg: 45,
      bearing_offset_deg: 0,
      zoom_offset: 0.2,
      speed_multiplier: 1.35,
      label: "Ridge Descent",
    },
    {
      progress: 0.96,
      camera_mode: "fit-track",
      camera_preset: "flat",
      pitch_deg: 16,
      bearing_offset_deg: 0,
      zoom_offset: 0,
      speed_multiplier: 1.0,
      label: "Overview Pull-Back",
    },
  ];

  return sanitizeKeyframes(keyframes);
}

/**
 * Climb Spotlight (legacy preset compatibility): speeds up flat valley transfers (2.5x)
 * and smoothly slows/zooms into categorized climbs without abrupt preset or bearing jumps.
 */
export function generateClimbSpotlightKeyframes(input: DirectorPresetInput): CameraKeyframe[] {
  const { stages, spec, climbs = [] } = input;
  const mappedClimbs =
    spec && climbs.length > 0 ? mapClimbsToTimelineProgress(climbs, stages, spec) : [];

  if (mappedClimbs.length === 0) {
    const peak = Math.min(0.75, Math.max(0.28, findPeakElevationProgress(stages) ?? 0.55));
    return sanitizeKeyframes([
      {
        progress: 0.0,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 20,
        bearing_offset_deg: 0,
        zoom_offset: -0.4,
        speed_multiplier: 2.5,
        label: "Valley Speed-Up 2.5x",
      },
      {
        progress: Math.max(0.12, peak - 0.16),
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 36,
        bearing_offset_deg: 0,
        zoom_offset: 0.65,
        speed_multiplier: 0.65,
        label: "Climb Focus",
      },
      {
        progress: peak,
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 40,
        bearing_offset_deg: 0,
        zoom_offset: 0.9,
        speed_multiplier: 0.55,
        label: "Summit Focus",
      },
      {
        progress: Math.min(0.92, peak + 0.14),
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 20,
        bearing_offset_deg: 0,
        zoom_offset: -0.3,
        speed_multiplier: 2.25,
        label: "Fast Descent",
      },
    ]);
  }

  const topClimbs = [...mappedClimbs]
    .sort((a, b) => b.fietsScore - a.fietsScore)
    .slice(0, 6)
    .sort((a, b) => a.startProgress - b.startProgress);

  const keyframes: CameraKeyframe[] = [
    {
      progress: 0.0,
      camera_mode: "follow-marker",
      camera_preset: "flat",
      pitch_deg: 20,
      bearing_offset_deg: 0,
      zoom_offset: -0.5,
      speed_multiplier: 2.5,
      label: "Valley Speed-Up 2.5x",
    },
  ];

  for (const climb of topClimbs) {
    const startP = clamp01(climb.startProgress);
    const summitP = clamp01(climb.summitProgress);
    const endP = clamp01(Math.max(climb.endProgress, summitP + 0.03));

    keyframes.push({
      progress: startP,
      camera_mode: "follow-marker",
      camera_preset: "flat",
      pitch_deg: 36,
      bearing_offset_deg: 0,
      zoom_offset: 0.65,
      speed_multiplier: 0.65,
      label: `${climb.category} ${climb.name}`.slice(0, 64),
    });
    keyframes.push({
      progress: summitP,
      camera_mode: "follow-marker",
      camera_preset: "flat",
      pitch_deg: 40,
      bearing_offset_deg: 0,
      zoom_offset: 0.9,
      speed_multiplier: 0.55,
      label: `${climb.name} Summit`.slice(0, 64),
    });
    if (endP < 0.94) {
      keyframes.push({
        progress: Math.min(0.95, endP + 0.02),
        camera_mode: "follow-marker",
        camera_preset: "flat",
        pitch_deg: 20,
        bearing_offset_deg: 0,
        zoom_offset: -0.4,
        speed_multiplier: 2.5,
        label: "Transfer 2.5x",
      });
    }
  }

  return sanitizeKeyframes(keyframes);
}

/**
 * Social Reel Punchy: tight close-follow framing, crisp pace shifts, and quick finale pull-back.
 */
export function generateSocialReelKeyframes(input: DirectorPresetInput): CameraKeyframe[] {
  const { stages, spec, climbs = [] } = input;
  const mappedClimbs =
    spec && climbs.length > 0 ? mapClimbsToTimelineProgress(climbs, stages, spec) : [];
  const summitP =
    mappedClimbs[0]?.summitProgress ??
    findPeakElevationProgress(stages) ??
    0.5;
  const orbitP = Math.min(0.72, Math.max(0.3, summitP));

  return sanitizeKeyframes([
    {
      progress: 0.0,
      camera_mode: "follow-marker",
      camera_preset: "dynamic-3d",
      pitch_deg: 50,
      bearing_offset_deg: -15,
      zoom_offset: 0.7,
      speed_multiplier: 1.5,
      label: "Punchy Launch",
    },
    {
      progress: 0.22,
      camera_mode: "follow-marker",
      camera_preset: "dynamic-3d",
      pitch_deg: 48,
      bearing_offset_deg: 10,
      zoom_offset: 0.4,
      speed_multiplier: 2.25,
      label: "Speed-Up 2.2x",
    },
    {
      progress: orbitP,
      camera_mode: "follow-marker",
      camera_preset: "drone-orbit",
      pitch_deg: 56,
      bearing_offset_deg: 60,
      zoom_offset: 1.1,
      speed_multiplier: 0.85,
      label: "Hero Orbit",
    },
    {
      progress: 0.82,
      camera_mode: "follow-marker",
      camera_preset: "dynamic-3d",
      pitch_deg: 46,
      bearing_offset_deg: 0,
      zoom_offset: 0.6,
      speed_multiplier: 2.0,
      label: "Sprint Finish",
    },
    {
      progress: 0.96,
      camera_mode: "fit-track",
      camera_preset: "flat",
      pitch_deg: 15,
      bearing_offset_deg: 0,
      zoom_offset: 0,
      speed_multiplier: 1.0,
      label: "Reel Pull-Back",
    },
  ]);
}

export function generateDirectorKeyframes(
  preset: DirectorPreset,
  input: DirectorPresetInput,
): CameraKeyframe[] {
  switch (preset) {
    case "expedition-doc":
      return generateExpeditionDocKeyframes(input);
    case "climb-spotlight":
      return generateClimbSpotlightKeyframes(input);
    case "social-reel":
      return generateSocialReelKeyframes(input);
    case "none":
    default:
      return [];
  }
}
