import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PhotoStopsSection } from "../components/editor/PhotoStopsSection";
import type { ProjectPhotoOut } from "../lib/api/types";

function photo(id: string): ProjectPhotoOut {
  return {
    id,
    project_id: "proj-1",
    stage_id: null,
    source_type: "upload",
    width: 640,
    height: 480,
    byte_size: 1000,
    caption: null,
    taken_at_utc: null,
    lat: null,
    lon: null,
    matched_lat: null,
    matched_lon: null,
    matched_progress: null,
    match_method: "unmatched",
    enabled: true,
    created_at: "2001-06-14T10:00:00Z",
  } as unknown as ProjectPhotoOut;
}

describe("PhotoStopsSection photo limit display", () => {
  it("shows the backend per-project limit next to the photo count", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 200 }));
    const photos = Array.from({ length: 51 }, (_, i) => photo(`p-${i}`));
    render(<PhotoStopsSection projectId="proj-1" photos={photos} />);
    expect(screen.getByText("51 / 100")).toBeTruthy();
  });
});
