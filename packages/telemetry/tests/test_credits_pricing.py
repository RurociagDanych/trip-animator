from __future__ import annotations

from trip_telemetry import RenderSpecV2, calculate_render_credit_quote


def test_baseline_1080p_30fps_60s_costs_100_credits() -> None:
    spec = RenderSpecV2(
        resolution="1920x1080",
        fps=30,
        duration_seconds=60.0,
        intro_hold_seconds=0.0,
        outro_hold_seconds=0.0,
        terrain_3d=False,
    )
    quote = calculate_render_credit_quote(spec, matched_photo_count=0)
    assert quote.credits == 100
    assert quote.width == 1920
    assert quote.height == 1080
    assert quote.fps == 30
    assert quote.film_seconds == 60.0
    assert quote.terrain_multiplier == 1.0


def test_minimum_cost_is_10_credits() -> None:
    spec = RenderSpecV2(
        resolution="720x720",
        fps=15,
        duration_seconds=5.0,
        intro_hold_seconds=0.0,
        outro_hold_seconds=0.0,
        terrain_3d=False,
    )
    quote = calculate_render_credit_quote(spec)
    assert quote.credits == 10


def test_terrain_3d_applies_1_5x_multiplier_with_ceil() -> None:
    spec = RenderSpecV2(
        resolution="1920x1080",
        fps=30,
        duration_seconds=60.0,
        intro_hold_seconds=0.0,
        outro_hold_seconds=0.0,
        terrain_3d=True,
    )
    quote = calculate_render_credit_quote(spec)
    assert quote.credits == 150
    assert quote.terrain_multiplier == 1.5

    spec_fractional = RenderSpecV2(
        resolution="1920x1080",
        fps=30,
        duration_seconds=10.0,
        intro_hold_seconds=0.0,
        outro_hold_seconds=0.0,
        terrain_3d=True,
    )
    quote_frac = calculate_render_credit_quote(spec_fractional)
    assert quote_frac.credits == 25


def test_includes_intro_outro_and_cinematic_photo_stops() -> None:
    spec = RenderSpecV2(
        resolution="1920x1080",
        fps=30,
        duration_seconds=30.0,
        intro_hold_seconds=2.0,
        outro_hold_seconds=2.0,
        show_photos=True,
        photo_card_style="cinematic_stop",
        photo_display_seconds=3.0,
        terrain_3d=False,
    )
    quote = calculate_render_credit_quote(spec, matched_photo_count=2)
    assert quote.cinematic_photo_stop_seconds == 8.0
    assert quote.film_seconds == 42.0
    assert quote.credits == 70

    spec_no_photos = spec.model_copy(update={"show_photos": False})
    quote_hidden = calculate_render_credit_quote(spec_no_photos, matched_photo_count=5)
    assert quote_hidden.cinematic_photo_stop_seconds == 0.0
    assert quote_hidden.film_seconds == 34.0
    assert quote_hidden.credits == 57
