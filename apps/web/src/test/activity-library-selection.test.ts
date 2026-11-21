import { describe, expect, it, vi } from "vitest";
import { loadActivityLibrarySelection, saveActivityLibrarySelection } from "../components/dashboard/activity-library-selection";

function storageWith(value: string | null) {
  return {
    getItem: vi.fn((key: string) => key === "activity-library-selection:owner-a" ? value : null),
    setItem: vi.fn(),
  };
}

describe("activity library selection persistence", () => {
  it("restores valid selections only for the matching owner", () => {
    const storage = storageWith(JSON.stringify({ name: "ridge", sort: "distance_m", order: "asc", area: "1,2,3,4", offset: 50 }));

    expect(loadActivityLibrarySelection("owner-a", storage)).toEqual({ name: "ridge", sort: "distance_m", order: "asc", area: "1,2,3,4" });
    expect(loadActivityLibrarySelection("owner-b", storage)).toEqual({});
    expect(storage.getItem).toHaveBeenNthCalledWith(1, "activity-library-selection:owner-a");
    expect(storage.getItem).toHaveBeenNthCalledWith(2, "activity-library-selection:owner-b");
  });

  it("drops invalid persisted values and malformed storage", () => {
    const storage = storageWith(JSON.stringify({ name: 1, sort: "unknown", order: "sideways", area: "1,2,3", min_distance_m: -4, has_gps: "true", offset: 25 }));

    expect(loadActivityLibrarySelection("owner-a", storage)).toEqual({});
    expect(loadActivityLibrarySelection("owner-a", storageWith("{"))).toEqual({});
    expect(loadActivityLibrarySelection(null, storage)).toEqual({});
  });

  it("persists only selections and tolerates unavailable browser storage", () => {
    const storage = storageWith(null);
    saveActivityLibrarySelection("owner-a", { name: "ridge", sort: "started_at", order: "desc", offset: 50, limit: 50 }, storage);

    expect(storage.setItem).toHaveBeenCalledWith("activity-library-selection:owner-a", JSON.stringify({ name: "ridge", sort: "started_at", order: "desc" }));
    expect(() => saveActivityLibrarySelection(null, {}, storage)).not.toThrow();
    expect(() => loadActivityLibrarySelection("owner-a", { getItem: () => { throw new Error("blocked"); }, setItem: vi.fn() })).not.toThrow();
    expect(() => saveActivityLibrarySelection("owner-a", {}, { getItem: vi.fn(), setItem: () => { throw new Error("blocked"); } })).not.toThrow();
  });
});
