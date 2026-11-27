import type { RenderSpecV2 } from "./api/types";

export const CREDIT_BASELINE_WIDTH = 1920;
export const CREDIT_BASELINE_HEIGHT = 1080;
export const CREDIT_BASELINE_FPS = 30;
export const CREDIT_BASELINE_SECONDS = 60;
export const CREDIT_BASELINE_PIXEL_FRAMES =
  CREDIT_BASELINE_WIDTH * CREDIT_BASELINE_HEIGHT * CREDIT_BASELINE_FPS * CREDIT_BASELINE_SECONDS;
export const CREDIT_BASELINE_COST = 100;
export const CREDIT_MIN_COST = 10;
export const CREDIT_TERRAIN_3D_MULTIPLIER = 1.5;
export const CINEMATIC_STOP_TRANSITION_SECONDS = 1;

export interface LocalRenderCreditQuote {
  credits: number;
  width: number;
  height: number;
  fps: number;
  film_seconds: number;
  cinematic_photo_stop_seconds: number;
  terrain_3d: boolean;
  terrain_multiplier: number;
  cached: boolean;
}

function parseResolution(resolution: string): [number, number] {
  const parts = resolution.toLowerCase().split("x");
  const width = Number(parts[0]);
  const height = Number(parts[1]);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return [CREDIT_BASELINE_WIDTH, CREDIT_BASELINE_HEIGHT];
  }
  return [width, height];
}

export function calculateRenderCreditQuote(
  spec: Pick<
    RenderSpecV2,
    | "resolution"
    | "fps"
    | "duration_seconds"
    | "intro_hold_seconds"
    | "outro_hold_seconds"
    | "terrain_3d"
    | "show_photos"
    | "photo_card_style"
    | "photo_display_seconds"
  >,
  matchedPhotoCount = 0,
  cached = false,
): LocalRenderCreditQuote {
  const [width, height] = parseResolution(spec.resolution);
  const fps = Math.max(1, Math.round(spec.fps));
  const durationS = Math.max(0, Number(spec.duration_seconds) || 0);
  const introS = Math.max(0, Number(spec.intro_hold_seconds) || 0);
  const outroS = Math.max(0, Number(spec.outro_hold_seconds) || 0);
  const photoDisplayS = Math.max(0, Number(spec.photo_display_seconds) || 0);
  const safePhotos = Math.max(0, Math.floor(matchedPhotoCount));

  const cinematicS =
    spec.show_photos && spec.photo_card_style === "cinematic_stop" && safePhotos > 0
      ? safePhotos * (CINEMATIC_STOP_TRANSITION_SECONDS + photoDisplayS)
      : 0;

  const filmSeconds = durationS + introS + outroS + cinematicS;
  const terrain3d = Boolean(spec.terrain_3d);
  const terrainMultiplier = terrain3d ? CREDIT_TERRAIN_3D_MULTIPLIER : 1;

  const credits = cached
    ? 0
    : Math.max(
        CREDIT_MIN_COST,
        Math.ceil(
          ((CREDIT_BASELINE_COST * (width * height * fps * filmSeconds)) /
            CREDIT_BASELINE_PIXEL_FRAMES) *
            terrainMultiplier -
            1e-12,
        ),
      );

  return {
    credits,
    width,
    height,
    fps,
    film_seconds: Math.round(filmSeconds * 1000) / 1000,
    cinematic_photo_stop_seconds: Math.round(cinematicS * 1000) / 1000,
    terrain_3d: terrain3d,
    terrain_multiplier: terrainMultiplier,
    cached,
  };
}
