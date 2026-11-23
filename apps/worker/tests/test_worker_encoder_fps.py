"""FFmpeg argv must follow the selected frame rate for both encode paths."""

from pathlib import Path

import pytest
from trip_worker.encoder import build_ffmpeg_encode_args, build_ffmpeg_pipe_encode_args


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_encode_args_use_selected_fps(fps: int) -> None:
    args = build_ffmpeg_encode_args(
        frame_pattern_path=Path("/tmp/render/frame_%06d.png"),
        output_mp4_path=Path("/tmp/render/out.mp4"),
        fps=fps,
        width=720,
        height=1280,
        gop_aligned=True,
    )
    assert _value_after(args, "-framerate") == str(fps)
    assert _value_after(args, "-g") == str(fps)
    assert _value_after(args, "-keyint_min") == str(fps)

    pipe_args = build_ffmpeg_pipe_encode_args(
        output_mp4_path=Path("/tmp/render/out.mp4"),
        fps=fps,
        width=720,
        height=720,
        frame_format="png",
    )
    assert _value_after(pipe_args, "-framerate") == str(fps)
