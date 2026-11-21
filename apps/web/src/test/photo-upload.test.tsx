import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ProjectPhotoOut } from "../lib/api/types";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PhotoStopsSection } from "../components/editor/PhotoStopsSection";
import { photosApi } from "../lib/api/endpoints";

vi.mock("../lib/api/endpoints", () => ({
  photosApi: {
    upload: vi.fn(),
    imageUrl: vi.fn(() => "/thumb.webp"),
  },
}));

const uploadMock = vi.mocked(photosApi.upload);
const uploadedPhoto: ProjectPhotoOut = {
  id: "uploaded-photo", project_id: "project", stage_id: null, enabled: true,
  source_type: "upload", match_method: "unmatched", caption: null,
  taken_at_utc: null, lat: null, lon: null, exif_offset_s: 0,
  width: 32, height: 32, byte_size: 100,
  matched_lat: null, matched_lon: null, matched_progress: null,
  created_at: "2001-01-01T00:00:00Z",
};

beforeEach(() => {
  vi.unstubAllGlobals();
  uploadMock.mockReset();
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn((file: File) => `blob:${file.name}:${Math.random()}`),
    revokeObjectURL: vi.fn(),
  });
});

describe("photo batch uploads", () => {
  it("keeps the active batch visible when another selection is attempted", async () => {
    uploadMock.mockImplementation(() => new Promise(() => {}));
    render(<PhotoStopsSection projectId="project" photos={[]} />);
    const input = document.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [new File(["first"], "first.jpg")] } });
    await waitFor(() => expect(uploadMock).toHaveBeenCalledTimes(1));
    expect(input).toBeDisabled();
    fireEvent.change(input, { target: { files: [new File(["second"], "second.jpg")] } });
    expect(screen.getByText("first.jpg")).toBeInTheDocument();
    expect(screen.queryByText("second.jpg")).not.toBeInTheDocument();
    expect(uploadMock).toHaveBeenCalledTimes(1);
  });

  it("keeps a successful upload successful when refresh fails and allows retry", async () => {
    uploadMock.mockResolvedValue(uploadedPhoto);
    const refresh = vi.fn().mockRejectedValueOnce(new Error("Refresh unavailable")).mockResolvedValue(undefined);
    render(<PhotoStopsSection projectId="project" photos={[]} onPhotosMutated={refresh} />);
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [new File(["first"], "first.jpg")] },
    });
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Refresh photos" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Refresh photos" })).not.toBeInTheDocument());
    expect(refresh).toHaveBeenCalledTimes(2);
    expect(uploadMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Uploaded")).toBeInTheDocument();
  });

  it("removes the pending preview once the uploaded photo is in the project list", async () => {
    const photo = uploadedPhoto;
    uploadMock.mockResolvedValue(photo);
    const { rerender } = render(<PhotoStopsSection projectId="project" photos={[]} />);
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [new File(["first"], "first.jpg")] },
    });
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
    rerender(<PhotoStopsSection projectId="project" photos={[photo]} />);
    await waitFor(() => expect(screen.queryByRole("list", { name: "Photo upload status" })).not.toBeInTheDocument());
    expect(URL.revokeObjectURL).toHaveBeenCalled();
    expect(document.querySelectorAll('img[src="/thumb.webp"]')).toHaveLength(1);
  });

  it("immediately shows one pending thumbnail/status per selected file", async () => {
    uploadMock.mockImplementation(() => new Promise(() => {}));
    const files = Array.from({ length: 20 }, (_, index) =>
      new File([`image-${index}`], `photo-${index}.jpg`, { type: "image/jpeg" }),
    );
    render(<PhotoStopsSection projectId="project" photos={[]} />);

    fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files } });

    const statusList = await screen.findByRole("list", { name: "Photo upload status" });
    expect(statusList.querySelectorAll("li")).toHaveLength(20);
    expect(statusList.querySelectorAll("img")).toHaveLength(20);
    expect(screen.getAllByText("Queued").length).toBeGreaterThan(0);
    await waitFor(() => expect(uploadMock).toHaveBeenCalledTimes(1));
  });

  it("skips byte-identical files in the same batch and reports the duplicate", async () => {
    vi.stubGlobal("crypto", {
      subtle: {
        digest: vi.fn(async () => new Uint8Array(32).buffer),
      },
    });
    uploadMock.mockResolvedValue({
      ...uploadedPhoto,
      content_hash: "0".repeat(64),
    });
    const files = [
      new File(["identical-content"], "first.jpg", { type: "image/jpeg" }),
      new File(["identical-content"], "renamed.jpg", { type: "image/jpeg" }),
    ];
    render(<PhotoStopsSection projectId="project" photos={[]} />);

    fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files } });

    expect(await screen.findByText("Duplicate — skipped")).toBeInTheDocument();
    expect(uploadMock).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Uploaded")).toBeInTheDocument();
  });
});
