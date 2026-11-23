import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sourceAdds: [] as Array<[number, number, unknown]>,
  sourceOptions: [] as unknown[],
  sourceClose: vi.fn(),
  cancel: vi.fn(async () => undefined),
  start: vi.fn(async () => undefined),
  finalize: vi.fn(async () => undefined),
  canEncodeVideo: vi.fn(async () => true),
}));

vi.mock("mediabunny", () => ({
  canEncodeVideo: mocks.canEncodeVideo,
  QUALITY_HIGH: Symbol("high"),
  BufferTarget: class {
    buffer = new Uint8Array([0, 0, 0, 24, 102, 116, 121, 112]).buffer;
  },
  CanvasSource: class {
    constructor(_canvas: HTMLCanvasElement, options: unknown) {
      mocks.sourceOptions.push(options);
    }
    add(timestamp: number, duration: number, options: unknown) {
      mocks.sourceAdds.push([timestamp, duration, options]);
      return Promise.resolve();
    }
    close = mocks.sourceClose;
  },
  Mp4OutputFormat: class {
    constructor() {}
  },
  Output: class {
    constructor() {}
    addVideoTrack() {}
    start = mocks.start;
    finalize = mocks.finalize;
    cancel = mocks.cancel;
  },
  Quality: class {
    constructor() {}
  },
}));

import { encodeLocalVideo, probeLocalVideo } from "../lib/export/local-video";

const renderer = "Accelerated Test GPU";
const loseContext = vi.fn();
const glContext = {
  getExtension: vi.fn((name: string) => {
    if (name === "WEBGL_debug_renderer_info") return { UNMASKED_RENDERER_WEBGL: 1 };
    if (name === "WEBGL_lose_context") return { loseContext };
    return null;
  }),
  getParameter: vi.fn(() => renderer),
} as unknown as WebGLRenderingContext;

function mockWebGL(context: WebGLRenderingContext | null = glContext) {
  const createElement = document.createElement.bind(document);
  vi.spyOn(document, "createElement").mockImplementation(((tag: string) => {
    if (tag === "canvas") {
      return { getContext: vi.fn(() => context) } as unknown as HTMLCanvasElement;
    }
    return createElement(tag);
  }) as typeof document.createElement);
}

function mockEncoder(supported = true) {
  vi.stubGlobal("VideoEncoder", class {});
  return mocks.canEncodeVideo.mockResolvedValue(supported);
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  mocks.sourceAdds.length = 0;
  mocks.sourceOptions.length = 0;
  mocks.sourceClose.mockClear();
  mocks.cancel.mockClear();
  mocks.start.mockClear();
  mocks.finalize.mockClear();
  mocks.canEncodeVideo.mockReset();
});

describe("probeLocalVideo", () => {
  it("requires accelerated WebGL, checks H.264 separately, and releases the probe context", async () => {
    mockWebGL();
    const isConfigSupported = mockEncoder();

    await expect(probeLocalVideo({ width: 1280, height: 720, fps: 30 })).resolves.toEqual({
      supported: true,
      encoding: "hardware-preferred",
    });
    expect(document.createElement("canvas").getContext).toBeDefined();
    expect(isConfigSupported).toHaveBeenCalledWith("avc", expect.objectContaining({
      width: 1280,
      height: 720,
      frameRate: 30,
      hardwareAcceleration: "prefer-hardware",
    }));
    expect(loseContext).toHaveBeenCalledOnce();
  });

  it("rejects an exposed software renderer without claiming H.264 support", async () => {
    const software = { ...glContext, getParameter: vi.fn(() => "Google SwiftShader") } as unknown as WebGLRenderingContext;
    mockWebGL(software);
    const isConfigSupported = mockEncoder();

    await expect(probeLocalVideo({ width: 640, height: 480, fps: 24 })).resolves.toMatchObject({
      supported: false,
      reason: expect.stringContaining("software"),
    });
    expect(isConfigSupported).not.toHaveBeenCalled();
  });

  it("reports unsupported H.264 independently from successful WebGL initialization", async () => {
    mockWebGL();
    mockEncoder(false);

    await expect(probeLocalVideo({ width: 640, height: 480, fps: 24 })).resolves.toMatchObject({
      supported: false,
      reason: expect.stringContaining("H.264"),
    });
    expect(loseContext).toHaveBeenCalledOnce();
  });
  it("allows browser-default encoding when the hardware preference is unsupported", async () => {
    mockWebGL();
    const probe = mockEncoder().mockResolvedValueOnce(false);
    await expect(probeLocalVideo({ width: 1080, height: 1350, fps: 30 })).resolves.toEqual({
      supported: true,
      encoding: "browser-default",
    });
    expect(probe).toHaveBeenNthCalledWith(2, "avc", expect.objectContaining({ hardwareAcceleration: "no-preference" }));
  });
});

describe("encodeLocalVideo", () => {
  const canvas = document.createElement("canvas");
  canvas.width = 640;
  canvas.height = 360;

  it("writes frames in deterministic timestamps and returns an MP4 blob", async () => {
    const rendered: number[] = [];
    const progress: number[] = [];
    const blob = await encodeLocalVideo({
      canvas,
      fps: 2,
      encoding: "hardware-preferred",
      frameCount: 3,
      renderFrame: async (index) => { rendered.push(index); },
      signal: new AbortController().signal,
      onProgress: (done) => { progress.push(done); },
    });

    expect(rendered).toEqual([0, 1, 2]);
    expect(mocks.sourceAdds).toEqual([
      [0, 0.5, { keyFrame: true }],
      [0.5, 0.5, { keyFrame: false }],
      [1, 0.5, { keyFrame: false }],
    ]);
    expect(progress).toEqual([1, 2, 3]);
    expect(mocks.sourceOptions[0]).toMatchObject({ codec: "avc", hardwareAcceleration: "prefer-hardware" });
    expect(blob.type).toBe("video/mp4");
    expect(blob.size).toBeGreaterThan(0);
    expect(mocks.finalize).toHaveBeenCalledOnce();
    expect(mocks.sourceClose).toHaveBeenCalledOnce();
  });

  it("stops promptly on cancellation and releases the output", async () => {
    const controller = new AbortController();
    const progress: number[] = [];
    await expect(encodeLocalVideo({
      canvas,
      fps: 30,
      encoding: "browser-default",
      frameCount: 5,
      renderFrame: async () => undefined,
      signal: controller.signal,
      onProgress: (done) => {
        progress.push(done);
        controller.abort();
      },
    })).rejects.toMatchObject({ name: "AbortError" });

    expect(progress).toEqual([1]);
    expect(mocks.cancel).toHaveBeenCalledOnce();
    expect(mocks.sourceClose).toHaveBeenCalledOnce();
    expect(mocks.finalize).not.toHaveBeenCalled();
  });
});
