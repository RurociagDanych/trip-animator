/** Probes browser support and encodes local MP4 exports. */
import { BufferTarget, canEncodeVideo, CanvasSource, Mp4OutputFormat, Output, QUALITY_HIGH } from "mediabunny";

type Encoding = "hardware-preferred" | "browser-default";

const SOFTWARE_RENDERER = /swiftshader|llvmpipe|softpipe|software rasterizer|microsoft basic render/i;


function hardwareAcceleration(encoding: Encoding) {
  return encoding === "hardware-preferred" ? "prefer-hardware" : "no-preference";
}

/** Checks accelerated WebGL and H.264 encoding support without asserting hardware certainty. */
export async function probeLocalVideo(size: {
  width: number;
  height: number;
  fps: number;
}): Promise<
  | { supported: true; encoding: Encoding }
  | { supported: false; reason: string }
> {
  if (
    !Number.isFinite(size.width) ||
    !Number.isFinite(size.height) ||
    !Number.isFinite(size.fps) ||
    size.width <= 0 ||
    size.height <= 0 ||
    size.fps <= 0
  ) {
    return { supported: false, reason: "Video dimensions and frame rate must be positive numbers." };
  }

  let context: WebGL2RenderingContext | null = null;
  try {
    const probeCanvas = document.createElement("canvas");
    context = probeCanvas.getContext("webgl2", { failIfMajorPerformanceCaveat: true });
    if (!context) {
      return { supported: false, reason: "Accelerated WebGL is unavailable." };
    }
    const debugInfo = context.getExtension("WEBGL_debug_renderer_info");
    const renderer = debugInfo
      ? context.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL)
      : "";
    if (typeof renderer === "string" && SOFTWARE_RENDERER.test(renderer)) {
      return { supported: false, reason: "A software WebGL renderer was detected." };
    }
  } catch {
    return { supported: false, reason: "Accelerated WebGL could not be initialized." };
  } finally {
    context?.getExtension("WEBGL_lose_context")?.loseContext();
  }

  if (typeof globalThis.VideoEncoder === "undefined") {
    return { supported: false, reason: "WebCodecs video encoding is unavailable." };
  }
  try {
    for (const encoding of ["hardware-preferred", "browser-default"] as const) {
      if (await canEncodeVideo("avc", {
        width: size.width,
        height: size.height,
        frameRate: size.fps,
        quality: QUALITY_HIGH,
        hardwareAcceleration: hardwareAcceleration(encoding),
      })) return { supported: true, encoding };
    }
  } catch {
    return { supported: false, reason: "H.264 video encoding could not be probed." };
  }
  return { supported: false, reason: "H.264 video encoding is unavailable at this resolution and frame rate." };
}

/** Encodes rendered canvas frames to an in-memory H.264 MP4, honoring cancellation. */
export async function encodeLocalVideo(options: {
  canvas: HTMLCanvasElement;
  fps: number;
  encoding: Encoding;
  frameCount: number;
  renderFrame: (index: number) => Promise<void>;
  signal: AbortSignal;
  onProgress: (done: number) => void;
}): Promise<Blob> {
  const { canvas, fps, encoding, frameCount, renderFrame, signal, onProgress } = options;
  if (!Number.isFinite(fps) || fps <= 0 || !Number.isInteger(frameCount) || frameCount <= 0) {
    throw new RangeError("Frame rate and frame count must be positive numbers.");
  }
  if (canvas.width <= 0 || canvas.height <= 0) {
    throw new RangeError("Canvas dimensions must be positive.");
  }
  if (signal.aborted) throw new DOMException("Encoding was aborted.", "AbortError");

  const target = new BufferTarget();
  const output = new Output({
    format: new Mp4OutputFormat({ fastStart: "fragmented" }),
    target,
  });
  const source = new CanvasSource(canvas, {
    codec: "avc",
    bitrate: QUALITY_HIGH,
    hardwareAcceleration: hardwareAcceleration(encoding),
  });
  output.addVideoTrack(source, { frameRate: fps });
  let abortHandler: (() => void) | undefined;
  const checkAborted = () => {
    if (signal.aborted) throw new DOMException("Encoding was aborted.", "AbortError");
  };
  try {
    const canceled = new Promise<never>((_, reject) => {
      abortHandler = () => reject(new DOMException("Encoding was aborted.", "AbortError"));
      signal.addEventListener("abort", abortHandler, { once: true });
    });
    await output.start();
    for (let index = 0; index < frameCount; index += 1) {
      checkAborted();
      await Promise.race([renderFrame(index), canceled]);
      checkAborted();
      await Promise.race([source.add(index / fps, 1 / fps, { keyFrame: index === 0 }), canceled]);
      onProgress(index + 1);
    }
    checkAborted();
    source.close();
    await Promise.race([output.finalize(), canceled]);
    const buffer = target.buffer;
    if (!buffer) throw new Error("MP4 output did not produce a buffer.");
    return new Blob([buffer], { type: "video/mp4" });
  } catch (error) {
    source.close();
    await output.cancel();
    throw error;
  } finally {
    if (abortHandler) signal.removeEventListener("abort", abortHandler);
  }
}
