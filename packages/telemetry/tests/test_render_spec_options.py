"""Render spec resolution presets must validate and round-trip through the schema."""

import pytest
from pydantic import ValidationError
from trip_telemetry import RenderSpecV2

NEW_PRESETS = ["720x1280", "720x900", "720x720"]
EXISTING_PRESETS = ["1280x720", "1920x1080", "1080x1080", "1080x1350", "1080x1920"]


@pytest.mark.parametrize("preset", NEW_PRESETS + EXISTING_PRESETS)
def test_resolution_preset_round_trips_through_schema(preset: str) -> None:
    spec = RenderSpecV2(resolution=preset)  # type: ignore[arg-type]
    dumped = spec.model_dump(mode="json")
    assert dumped["resolution"] == preset
    assert RenderSpecV2.model_validate(dumped).resolution == preset


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_fps_round_trips_through_schema(fps: int) -> None:
    spec = RenderSpecV2(fps=fps)  # type: ignore[arg-type]
    assert RenderSpecV2.model_validate(spec.model_dump(mode="json")).fps == fps


@pytest.mark.parametrize("fps", [24, 0, 45])
def test_unsupported_fps_is_rejected(fps: int) -> None:
    with pytest.raises(ValidationError):
        RenderSpecV2.model_validate({"fps": fps})


@pytest.mark.parametrize("preset", ["640x480", "720x721", "720x1281", "1280x720 "])
def test_unknown_resolution_preset_is_rejected(preset: str) -> None:
    with pytest.raises(ValidationError):
        RenderSpecV2.model_validate({"resolution": preset})
