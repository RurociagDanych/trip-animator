import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { resolveMarkedTrailsStoreFile } from "../../vite-plugins/marked-trails-store";

describe("local Marked Trails store served by Vite", () => {
  let base: string;
  let store: string;

  beforeEach(() => {
    base = mkdtempSync(path.join(tmpdir(), "marked-trails-store-"));
    store = path.join(base, "store");
    mkdirSync(path.join(store, "builds", "2026-11-27"), { recursive: true });
    writeFileSync(path.join(store, "current.json"), "{}");
    writeFileSync(path.join(store, ".current.json.tmp"), "{}");
    writeFileSync(path.join(store, "builds", "2026-11-27", "marked-trails.pmtiles"), "PMTiles");
    writeFileSync(path.join(base, "outside.json"), "{}");
    symlinkSync(path.join(base, "outside.json"), path.join(store, "linked.json"));
  });

  afterEach(() => {
    rmSync(base, { recursive: true, force: true });
  });

  it("serves the pointer and the build files of the store", () => {
    expect(resolveMarkedTrailsStoreFile(store, "current.json")).toBe(
      path.join(store, "current.json"),
    );
    expect(resolveMarkedTrailsStoreFile(store, "builds/2026-11-27/marked-trails.pmtiles")).toBe(
      path.join(store, "builds", "2026-11-27", "marked-trails.pmtiles"),
    );
  });

  it("rejects hidden files, traversal, symlinks out of the store and directories", () => {
    expect(resolveMarkedTrailsStoreFile(store, ".current.json.tmp")).toBeNull();
    expect(resolveMarkedTrailsStoreFile(store, "builds/.hidden/marked-trails.pmtiles")).toBeNull();
    expect(resolveMarkedTrailsStoreFile(store, "../outside.json")).toBeNull();
    expect(resolveMarkedTrailsStoreFile(store, "linked.json")).toBeNull();
    expect(resolveMarkedTrailsStoreFile(store, "builds")).toBeNull();
    expect(resolveMarkedTrailsStoreFile(store, "missing.json")).toBeNull();
  });
});
