from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trip_telemetry.schemas import RenderSpecV2

CREDIT_BASELINE_WIDTH = 1920
CREDIT_BASELINE_HEIGHT = 1080
CREDIT_BASELINE_FPS = 30
CREDIT_BASELINE_SECONDS = 60.0
CREDIT_BASELINE_PIXEL_FRAMES = (
    CREDIT_BASELINE_WIDTH * CREDIT_BASELINE_HEIGHT * CREDIT_BASELINE_FPS * CREDIT_BASELINE_SECONDS
)
CREDIT_BASELINE_COST = 100.0
CREDIT_MIN_COST = 10
CREDIT_TERRAIN_3D_MULTIPLIER = 1.5
CINEMATIC_STOP_TRANSITION_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class RenderCreditQuote:
    credits: int
    width: int
    height: int
    fps: int
    film_seconds: float
    cinematic_photo_stop_seconds: float
    terrain_3d: bool
    terrain_multiplier: float
    cached: bool = False


def _parse_resolution(resolution: str) -> tuple[int, int]:
    parts = resolution.lower().split("x", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid resolution string: {resolution!r}")
    width = int(parts[0])
    height = int(parts[1])
    if width <= 0 or height <= 0:
        raise ValueError(f"Resolution dimensions must be positive: {resolution!r}")
    return width, height


def calculate_render_credit_quote(
    spec: RenderSpecV2 | dict[str, Any],
    *,
    matched_photo_count: int = 0,
    cached: bool = False,
) -> RenderCreditQuote:
    if isinstance(spec, dict):
        resolution = str(spec.get("resolution", "1920x1080"))
        fps = int(spec.get("fps", 30))
        duration_s = float(spec.get("duration_seconds", 20.0))
        intro_s = max(0.0, float(spec.get("intro_hold_seconds", 1.0)))
        outro_s = max(0.0, float(spec.get("outro_hold_seconds", 1.5)))
        terrain_3d = bool(spec.get("terrain_3d", False))
        show_photos = bool(spec.get("show_photos", True))
        photo_card_style = str(spec.get("photo_card_style", "corner"))
        photo_display_s = max(0.0, float(spec.get("photo_display_seconds", 3.0)))
    else:
        resolution = spec.resolution
        fps = int(spec.fps)
        duration_s = float(spec.duration_seconds)
        intro_s = max(0.0, float(spec.intro_hold_seconds))
        outro_s = max(0.0, float(spec.outro_hold_seconds))
        terrain_3d = bool(spec.terrain_3d)
        show_photos = bool(spec.show_photos)
        photo_card_style = str(spec.photo_card_style)
        photo_display_s = max(0.0, float(spec.photo_display_seconds))

    width, height = _parse_resolution(resolution)
    safe_photos = max(0, int(matched_photo_count))
    if show_photos and photo_card_style == "cinematic_stop" and safe_photos > 0:
        cinematic_s = safe_photos * (CINEMATIC_STOP_TRANSITION_SECONDS + photo_display_s)
    else:
        cinematic_s = 0.0

    film_seconds = max(0.0, duration_s) + intro_s + outro_s + cinematic_s
    terrain_multiplier = CREDIT_TERRAIN_3D_MULTIPLIER if terrain_3d else 1.0

    if cached:
        credits = 0
    else:
        raw_credits = (
            CREDIT_BASELINE_COST
            * (width * height * fps * film_seconds)
            / CREDIT_BASELINE_PIXEL_FRAMES
        )
        credits = max(CREDIT_MIN_COST, math.ceil(raw_credits * terrain_multiplier - 1e-12))

    return RenderCreditQuote(
        credits=credits,
        width=width,
        height=height,
        fps=fps,
        film_seconds=round(film_seconds, 3),
        cinematic_photo_stop_seconds=round(cinematic_s, 3),
        terrain_3d=terrain_3d,
        terrain_multiplier=terrain_multiplier,
        cached=cached,
    )
