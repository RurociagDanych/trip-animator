import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActivityLibraryMap } from "../components/dashboard/ActivityLibraryMap";
import { vaultApi } from "../lib/api/endpoints";
import type { VaultActivityMapOut } from "../lib/api/types";

type MapEvent = (event?: { features?: Array<{ properties?: { id?: unknown } }> }) => void;
type MapInstance = {
  handlers: Record<string, MapEvent>;
  source: { setData: ReturnType<typeof vi.fn> };
  fitBounds: ReturnType<typeof vi.fn>;
  resize: ReturnType<typeof vi.fn>;
};

const harness = vi.hoisted(() => ({ instances: [] as MapInstance[], styleReady: true }));

vi.mock("maplibre-gl", () => ({
  default: {
    Map: class {
      handlers: Record<string, MapEvent> = {};
      source = { setData: vi.fn() };
      fitBounds = vi.fn();
      resize = vi.fn();
      constructor() {
        harness.instances.push(this);
      }
      on(event: string, layerOrHandler: string | MapEvent, handler?: MapEvent) {
        if (typeof layerOrHandler === "string") this.handlers[`${event}:${layerOrHandler}`] = handler!;
        else this.handlers[event] = layerOrHandler;
      }
      isStyleLoaded() {
        return harness.styleReady;
      }
      addSource() {}
      addLayer() {}
      getSource() {
        return this.source;
      }
      getLayer() {
        return true;
      }
      setPaintProperty() {}
      getBounds() {
        return { getWest: () => -2, getSouth: () => 48, getEast: () => 3, getNorth: () => 52 };
      }
      remove() {}
    },
  },
}));

const result = (id: string): VaultActivityMapOut => ({
  type: "FeatureCollection",
  features: [{
    type: "Feature",
    geometry: { type: "LineString", coordinates: [[0, 0], [1, 1]] },
    properties: { id, name: `Route ${id}`, precision: "summary_polyline" },
  }],
  total: 1,
  drawable_count: 1,
  no_gps_count: 0,
  unavailable_count: 0,
  returned_count: 1,
  truncated: false,
  geographic_excluded_count: 0,
  unevaluated_count: 0,
});

afterEach(() => {
  vi.restoreAllMocks();
  harness.instances.length = 0;
  harness.styleReady = true;
});

describe("ActivityLibraryMap", () => {
  it("does not draw cached geometry from the previous owner when the style loads late", async () => {
    harness.styleReady = false;
    let resolveNext!: (data: VaultActivityMapOut) => void;
    vi.spyOn(vaultApi, "map")
      .mockResolvedValueOnce(result("previous-owner-route"))
      .mockReturnValueOnce(new Promise((resolve) => { resolveNext = resolve; }));
    const props = { filters: {}, area: undefined, onAreaChange: vi.fn(), selectedId: null, onSelect: vi.fn() };
    const view = render(<ActivityLibraryMap {...props} ownerId="previous-owner" />);
    await screen.findByText(/1 routes shown/);

    view.rerender(<ActivityLibraryMap {...props} ownerId="next-owner" />);
    const map = harness.instances[0]!;
    act(() => map.handlers.load!());
    expect(map.source.setData.mock.calls.every(([data]) => data.features.length === 0)).toBe(true);

    await act(async () => resolveNext(result("next-owner-route")));
    await waitFor(() => expect(map.source.setData.mock.lastCall![0].features[0].properties.id).toBe("next-owner-route"));
    expect(map.fitBounds).toHaveBeenCalledOnce();
  });

  it("initializes a style that was already ready before the load listener was attached", async () => {
    vi.spyOn(vaultApi, "map").mockResolvedValue(result("route-1"));

    render(<ActivityLibraryMap filters={{}} area={undefined} onAreaChange={vi.fn()} selectedId={null} onSelect={vi.fn()} />);

    await waitFor(() => expect(harness.instances[0]?.source.setData).toHaveBeenCalledWith(expect.objectContaining({ features: [expect.objectContaining({ properties: expect.objectContaining({ id: "route-1" }) })] })));
    expect(harness.instances[0]?.resize).toHaveBeenCalledOnce();
  });

  it("keeps successful geometry visible while area-filtered geometry is loading", async () => {
    let resolveArea!: (data: VaultActivityMapOut) => void;
    const mapRequest = vi.spyOn(vaultApi, "map")
      .mockResolvedValueOnce(result("route-1"))
      .mockReturnValueOnce(new Promise((resolve) => { resolveArea = resolve; }));
    const props = { filters: {}, area: undefined, onAreaChange: vi.fn(), selectedId: null, onSelect: vi.fn() };
    const view = render(<ActivityLibraryMap {...props} />);
    await screen.findByText(/1 routes shown/);
    const source = harness.instances[0]!.source;
    await waitFor(() => expect(source.setData.mock.lastCall?.[0].features[0]?.properties.id).toBe("route-1"));
    const beforeFiltering = source.setData.mock.lastCall![0];

    view.rerender(<ActivityLibraryMap {...props} area="-2,48,3,52" />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading activity routes…");
    expect(source.setData.mock.lastCall![0]).toBe(beforeFiltering);
    expect(source.setData.mock.lastCall![0].features[0].properties.id).toBe("route-1");
    expect(mapRequest).toHaveBeenLastCalledWith({ area: "-2,48,3,52", offset: 0 }, expect.any(AbortSignal));

    await act(async () => resolveArea(result("route-2")));
    await screen.findByText(/1 routes shown/);
    await waitFor(() => expect(source.setData.mock.lastCall?.[0].features[0]?.properties.id).toBe("route-2"));
  });
});
