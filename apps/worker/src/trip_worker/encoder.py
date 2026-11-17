"""Constant-argument FFmpeg and ffprobe command builders, pipe encoder, and validation."""

from __future__ import annotations

import json
import math
import subprocess
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class EncoderError(RuntimeError):
    """Raised when FFmpeg encoding, thumbnail extraction, or ffprobe verification fails."""


@dataclass(frozen=True)
class ProbeResult:
    """Validated metadata for a rendered MP4 video stream."""

    codec_name: str
    pix_fmt: str
    width: int
    height: int
    duration_s: float
    nb_frames: int | None


def _validate_video_params(*, fps: int, width: int, height: int) -> None:
    if fps not in (15, 30, 60):
        raise ValueError(f"Unsupported fps: {fps}")
    if width <= 0 or height <= 0 or width > 3840 or height > 3840:
        raise ValueError(f"Unsupported dimensions: {width}x{height}")
    if width % 2 != 0 or height % 2 != 0:
        raise ValueError(f"Dimensions must be even for yuv420p: {width}x{height}")


def _validate_safe_path(path: Path) -> str:
    text = str(path)
    if not text or text.startswith("-") or "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError(f"Unsafe media path: {text!r}")
    return text


def build_ffmpeg_encode_args(
    *,
    frame_pattern_path: Path,
    output_mp4_path: Path,
    fps: int,
    width: int,
    height: int,
    gop_aligned: bool = False,
) -> list[str]:
    """Return fixed argument list for H.264/YUV420p silent MP4 encoding from frame pattern."""
    _validate_video_params(fps=fps, width=width, height=height)
    pattern_arg = _validate_safe_path(frame_pattern_path)
    out_arg = _validate_safe_path(output_mp4_path)
    gop_args = (
        ["-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0"] if gop_aligned else []
    )

    return [
        "ffmpeg",
        "-y",
        "-framerate",
        str(fps),
        "-i",
        pattern_arg,
        "-vf",
        f"scale={width}:{height}:flags=lanczos:out_range=tv,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        *gop_args,
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-movflags",
        "+faststart",
        "-an",
        out_arg,
    ]


def build_ffmpeg_pipe_encode_args(
    *,
    output_mp4_path: Path,
    fps: int,
    width: int,
    height: int,
    frame_format: Literal["png", "jpeg"] = "png",
    gop_aligned: bool = False,
) -> list[str]:
    """Return fixed argument list for image2pipe stdin -> H.264/YUV420p silent MP4 encoding."""
    _validate_video_params(fps=fps, width=width, height=height)
    out_arg = _validate_safe_path(output_mp4_path)
    vcodec = "png" if frame_format == "png" else "mjpeg"
    gop_args = (
        ["-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0"] if gop_aligned else []
    )

    return [
        "ffmpeg",
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        vcodec,
        "-framerate",
        str(fps),
        "-i",
        "pipe:0",
        "-vf",
        f"scale={width}:{height}:flags=lanczos:out_range=tv,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        *gop_args,
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-movflags",
        "+faststart",
        "-an",
        out_arg,
    ]


def build_ffmpeg_concat_args(
    *,
    concat_list_path: Path,
    output_mp4_path: Path,
) -> list[str]:
    """Return fixed argument list for lossless concat demuxer MP4 assembly."""
    list_arg = _validate_safe_path(concat_list_path)
    out_arg = _validate_safe_path(output_mp4_path)
    return [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_arg,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out_arg,
    ]


def build_ffprobe_args(output_mp4_path: Path) -> list[str]:
    """Return fixed argument list for ffprobe stream and container duration verification."""
    out_arg = _validate_safe_path(output_mp4_path)
    return [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,nb_frames,duration:format=duration",
        "-of",
        "json",
        out_arg,
    ]


def build_ffmpeg_thumbnail_args(
    *,
    input_mp4_path: Path,
    output_thumb_path: Path,
    seek_seconds: float = 0.0,
    width: int = 640,
) -> list[str]:
    """Return fixed argument list for extracting a single WebP/PNG thumbnail frame."""
    if not math.isfinite(seek_seconds) or seek_seconds < 0.0:
        raise ValueError(f"seek_seconds must be finite and >= 0: {seek_seconds}")
    if width <= 0 or width > 3840 or width % 2 != 0:
        raise ValueError(f"Unsupported thumbnail width: {width}")

    in_arg = _validate_safe_path(input_mp4_path)
    out_arg = _validate_safe_path(output_thumb_path)
    suffix = output_thumb_path.suffix.lower()
    if suffix == ".png":
        codec_args = ["-c:v", "png"]
    elif suffix in {".webp", ""}:
        codec_args = ["-c:v", "libwebp", "-quality", "82"]
    else:
        raise ValueError(f"Unsupported thumbnail extension: {suffix!r}")

    return [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_seconds:.3f}",
        "-i",
        in_arg,
        "-vframes",
        "1",
        "-vf",
        f"scale={width}:-2:flags=lanczos",
        *codec_args,
        out_arg,
    ]


class StreamingVideoEncoder:
    """Streams captured PNG/JPEG frames directly into FFmpeg stdin (`image2pipe`)."""

    def __init__(
        self,
        *,
        output_mp4_path: Path,
        fps: int,
        width: int,
        height: int,
        frame_format: Literal["png", "jpeg"] = "png",
        gop_aligned: bool = False,
    ) -> None:
        self.output_mp4_path = output_mp4_path
        self.args = build_ffmpeg_pipe_encode_args(
            output_mp4_path=output_mp4_path,
            fps=fps,
            width=width,
            height=height,
            frame_format=frame_format,
            gop_aligned=gop_aligned,
        )
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._stderr_thread: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._frames_written = 0
        self._closed = False

    def start(self) -> None:
        if self._proc is not None:
            return
        self.output_mp4_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as exc:
            raise EncoderError(f"Failed to launch ffmpeg: {exc}") from exc

        if self._proc.stderr is not None:
            stderr_pipe = self._proc.stderr

            def _drain_stderr() -> None:
                try:
                    for raw_line in stderr_pipe:
                        self._stderr_lines.append(
                            raw_line.decode("utf-8", errors="replace").rstrip()
                        )
                finally:
                    stderr_pipe.close()

            self._stderr_thread = threading.Thread(
                target=_drain_stderr, name="ffmpeg-stderr-drain", daemon=True
            )
            self._stderr_thread.start()

    def write_frame(self, frame_bytes: bytes) -> None:
        if self._proc is None:
            self.start()
        assert self._proc is not None
        if self._proc.stdin is None:
            raise EncoderError("FFmpeg stdin pipe is unavailable")
        if not frame_bytes:
            raise EncoderError("Cannot write empty frame buffer to FFmpeg")
        try:
            self._proc.stdin.write(frame_bytes)
            self._frames_written += 1
        except (BrokenPipeError, OSError) as exc:
            tail = "\n".join(self._stderr_lines)
            raise EncoderError(f"FFmpeg pipe closed while writing frame: {exc}\n{tail}") from exc

    def finish(self, *, timeout_seconds: float = 300.0) -> Path:
        if self._proc is None:
            raise EncoderError("StreamingVideoEncoder was never started")
        if self._closed:
            return self.output_mp4_path
        self._closed = True

        if self._proc.stdin is not None:
            try:
                self._proc.stdin.flush()
            except OSError:
                pass
            try:
                self._proc.stdin.close()
            except OSError:
                pass

        try:
            returncode = self._proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._proc.kill()
            self._proc.wait(timeout=5.0)
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=2.0)
            raise EncoderError(
                f"FFmpeg encoding exceeded timeout ({timeout_seconds}s)"
            ) from exc

        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5.0)

        if returncode != 0:
            tail = "\n".join(self._stderr_lines)
            raise EncoderError(f"FFmpeg exited with code {returncode}: {tail}".strip())

        if not self.output_mp4_path.is_file() or self.output_mp4_path.stat().st_size == 0:
            raise EncoderError("FFmpeg succeeded but produced an empty or missing MP4 file")
        return self.output_mp4_path

    def abort(self) -> None:
        if self._proc is None or self._closed:
            return
        self._closed = True
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)


def probe_video(output_mp4_path: Path, *, timeout_seconds: float = 30.0) -> ProbeResult:
    """Run ffprobe on output_mp4_path and return structured video stream metadata."""
    args = build_ffprobe_args(output_mp4_path)
    try:
        proc = subprocess.run(  # noqa: S603
            args,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EncoderError(f"ffprobe execution failed: {exc}") from exc

    if proc.returncode != 0:
        raise EncoderError(f"ffprobe failed (exit {proc.returncode}): {proc.stderr.strip()}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise EncoderError(f"ffprobe returned invalid JSON: {exc}") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise EncoderError("ffprobe found no video streams in output MP4")

    stream = streams[0]
    if not isinstance(stream, dict):
        raise EncoderError("ffprobe returned malformed video stream record")

    codec_name = str(stream.get("codec_name") or "")
    pix_fmt = str(stream.get("pix_fmt") or "")
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)

    raw_nb_frames = stream.get("nb_frames")
    nb_frames: int | None = None
    if raw_nb_frames is not None and str(raw_nb_frames).isdigit():
        nb_frames = int(raw_nb_frames)

    fmt = payload.get("format")
    raw_duration = stream.get("duration") or (
        fmt.get("duration") if isinstance(fmt, dict) else None
    )
    try:
        duration_s = float(raw_duration) if raw_duration is not None else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0

    return ProbeResult(
        codec_name=codec_name,
        pix_fmt=pix_fmt,
        width=width,
        height=height,
        duration_s=duration_s,
        nb_frames=nb_frames,
    )


def verify_mp4_output(
    output_mp4_path: Path,
    *,
    expected_width: int,
    expected_height: int,
    min_duration_s: float = 0.1,
    expected_duration_s: float | None = None,
    duration_tolerance_s: float = 1.5,
    timeout_seconds: float = 30.0,
) -> ProbeResult:
    """Verify rendered MP4 stream codec (`h264`), pixel format (`yuv420p`), size, and duration."""
    result = probe_video(output_mp4_path, timeout_seconds=timeout_seconds)
    if result.codec_name != "h264":
        raise EncoderError(f"Expected h264 video stream, got {result.codec_name!r}")
    if result.pix_fmt != "yuv420p":
        raise EncoderError(f"Expected yuv420p pixel format, got {result.pix_fmt!r}")
    if result.width != expected_width or result.height != expected_height:
        raise EncoderError(
            f"Resolution mismatch: expected {expected_width}x{expected_height}, "
            f"got {result.width}x{result.height}"
        )
    if not math.isfinite(result.duration_s) or result.duration_s < min_duration_s:
        raise EncoderError(f"Rendered video duration too short: {result.duration_s}s")
    if (
        expected_duration_s is not None
        and abs(result.duration_s - expected_duration_s) > duration_tolerance_s
    ):
        raise EncoderError(
            f"Duration mismatch: expected ~{expected_duration_s:.2f}s, got {result.duration_s:.2f}s"
        )
    return result


def extract_thumbnail(
    input_mp4_path: Path,
    output_thumb_path: Path,
    *,
    seek_seconds: float = 0.5,
    width: int = 640,
    timeout_seconds: float = 30.0,
) -> Path:
    """Extract a thumbnail frame (WebP or PNG) from input_mp4_path via constant-argv ffmpeg."""
    output_thumb_path.parent.mkdir(parents=True, exist_ok=True)
    attempts = [seek_seconds] if seek_seconds == 0.0 else [seek_seconds, 0.0]

    last_err = ""
    for seek in attempts:
        args = build_ffmpeg_thumbnail_args(
            input_mp4_path=input_mp4_path,
            output_thumb_path=output_thumb_path,
            seek_seconds=seek,
            width=width,
        )
        try:
            proc = subprocess.run(  # noqa: S603
                args,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EncoderError(f"Thumbnail ffmpeg execution failed: {exc}") from exc

        if (
            proc.returncode == 0
            and output_thumb_path.is_file()
            and output_thumb_path.stat().st_size > 0
        ):
            return output_thumb_path
        last_err = proc.stderr.strip() or f"exit={proc.returncode}"

    raise EncoderError(f"Thumbnail extraction failed: {last_err}")


def concat_mp4_chunks(
    chunk_paths: Sequence[Path],
    output_mp4_path: Path,
    *,
    timeout_seconds: float = 300.0,
) -> Path:
    """Losslessly concatenate GOP-aligned MP4 chunks using the FFmpeg concat demuxer."""
    if not chunk_paths:
        raise ValueError("concat_mp4_chunks requires at least one chunk path")

    resolved_chunks: list[Path] = []
    for chunk_path in chunk_paths:
        _validate_safe_path(chunk_path)
        if not chunk_path.is_file() or chunk_path.stat().st_size == 0:
            raise EncoderError(f"Missing or empty MP4 chunk: {chunk_path}")
        resolved_chunks.append(chunk_path.resolve())

    output_mp4_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list_path = output_mp4_path.parent / f".{output_mp4_path.stem}_concat.txt"
    lines: list[str] = []
    for resolved in resolved_chunks:
        escaped = str(resolved).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    args = build_ffmpeg_concat_args(
        concat_list_path=concat_list_path,
        output_mp4_path=output_mp4_path,
    )
    try:
        try:
            proc = subprocess.run(  # noqa: S603
                args,
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EncoderError(f"FFmpeg concat execution failed: {exc}") from exc

        if proc.returncode != 0:
            raise EncoderError(
                f"FFmpeg concat failed (exit {proc.returncode}): {proc.stderr.strip()}"
            )
        if not output_mp4_path.is_file() or output_mp4_path.stat().st_size == 0:
            raise EncoderError("FFmpeg concat succeeded but produced an empty or missing MP4")
        return output_mp4_path
    finally:
        concat_list_path.unlink(missing_ok=True)
