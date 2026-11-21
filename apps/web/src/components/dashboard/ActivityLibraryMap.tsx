import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type Map as MapLibreMap } from "maplibre-gl";
import { vaultApi, type VaultActivityFilters } from "../../lib/api/endpoints";
import type { VaultActivityMapOut } from "../../lib/api/types";
import { resolveMapStyleUrl } from "../../lib/geo/map-config";

const SOURCE = "activity-routes";
const LAYER = "activity-routes-line";

interface Props {
  ownerId?: string | null;
  filters: VaultActivityFilters;
  area: string | undefined;
  refreshKey?: number;
  onAreaChange: (area: string | undefined) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function ActivityLibraryMap({ ownerId = null, filters, area, refreshKey, onAreaChange, selectedId, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const loaded = useRef(false);
  const fittedInitialData = useRef(false);
  const latestProps = useRef({ selectedId, onSelect, onAreaChange });
  const routeDataRef = useRef<VaultActivityMapOut | null>(null);
  const [bounds, setBounds] = useState<string | undefined>(undefined);
  const requestFilters = useMemo(() => ({ ...filters, area }), [filters, area]);
  const requestKey = JSON.stringify([ownerId, requestFilters, refreshKey]);
  const [requestState, setRequestState] = useState<{ key: string; data: VaultActivityMapOut | null; error: boolean } | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const routeData = requestState?.key === requestKey ? requestState.data : null;
  const loading = requestState?.key !== requestKey;
  const error = requestState?.key === requestKey && requestState.error;

  useEffect(() => { latestProps.current = { selectedId, onSelect, onAreaChange }; }, [selectedId, onSelect, onAreaChange]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    routeDataRef.current = null;
    const source = mapRef.current?.getSource(SOURCE) as maplibregl.GeoJSONSource | undefined;
    if (loaded.current) source?.setData({ type: "FeatureCollection", features: [] });
    async function loadRoutes(): Promise<VaultActivityMapOut> {
      let offset = 0;
      let result: VaultActivityMapOut | null = null;
      while (!controller.signal.aborted) {
        const page = await vaultApi.map({ ...requestFilters, offset }, controller.signal);
        if (controller.signal.aborted) throw new Error("Activity route loading cancelled");
        if (!result) result = { ...page, features: [...page.features] };
        else {
          result.features.push(...page.features);
          result.drawable_count += page.drawable_count;
          result.unavailable_count += page.unavailable_in_page_count ?? 0;
          result.returned_count = result.features.length;
          result.unevaluated_count = page.unevaluated_count;
          result.truncated = page.truncated;
          result.next_offset = page.next_offset;
        }
        if (page.next_offset == null) return result;
        if (!Number.isInteger(page.next_offset) || page.next_offset <= offset || page.next_offset >= 50_000) {
          throw new Error("Invalid activity route pagination cursor");
        }
        offset = page.next_offset;
      }
      throw new Error("Activity route loading cancelled");
    }
    loadRoutes().then((data) => {
      if (!cancelled) {
        routeDataRef.current = data;
        setRequestState({ key: requestKey, data, error: false });
      }
    }).catch(() => {
      if (!cancelled) setRequestState({ key: requestKey, data: null, error: true });
    });
    return () => { cancelled = true; controller.abort(); };
  }, [requestFilters, refreshKey, requestKey]);

  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({ container: container.current, style: resolveMapStyleUrl("positron"), center: [0, 20], zoom: 1.2 });
    mapRef.current = map;
    const onLoad = () => {
      map.addSource(SOURCE, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: LAYER, type: "line", source: SOURCE, paint: {
        "line-color": ["case", ["==", ["get", "id"], latestProps.current.selectedId ?? ""], "#f97316", "#0284c7"],
        "line-width": ["case", ["==", ["get", "id"], latestProps.current.selectedId ?? ""], 5, 3], "line-opacity": 0.85,
      } });
      map.on("click", LAYER, (event) => {
        const id = event.features?.[0]?.properties?.id;
        if (typeof id === "string") latestProps.current.onSelect(id);
      });
      map.on("moveend", () => {
        updateBounds();
      });
      loaded.current = true;
      setMapReady(true);
      updateBounds();
      const source = map.getSource(SOURCE) as maplibregl.GeoJSONSource | undefined;
      if (source) source.setData(routeDataRef.current ?? { type: "FeatureCollection", features: [] });
    };
    function updateBounds() {
      const b = map.getBounds();
      const west = b.getWest();
      const east = b.getEast();
      const south = Math.max(-90, b.getSouth());
      const north = Math.min(90, b.getNorth());
      const wrap = (longitude: number) => ((longitude + 180) % 360 + 360) % 360 - 180;
      let normalizedWest: number;
      let normalizedEast: number;
      if (east - west >= 360) {
        normalizedWest = -180;
        normalizedEast = 180;
      } else {
        normalizedWest = wrap(west);
        normalizedEast = wrap(east);
      }
      setBounds([normalizedWest, south, normalizedEast, north].join(","));
    }
    map.on("load", onLoad);
    return () => { loaded.current = false; map.remove(); mapRef.current = null; };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !loaded.current || !mapReady) return;
    const source = map.getSource(SOURCE) as maplibregl.GeoJSONSource | undefined;
    source?.setData(routeData ?? { type: "FeatureCollection", features: [] });
    if (!fittedInitialData.current && routeData?.features.length) {
      let west = Infinity;
      let east = -Infinity;
      let south = Infinity;
      let north = -Infinity;
      const collect = (value: unknown) => {
        if (!Array.isArray(value)) return;
        if (value.length >= 2 && typeof value[0] === "number" && typeof value[1] === "number") {
          const [longitude, latitude] = value;
          if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) return;
          west = Math.min(west, longitude);
          east = Math.max(east, longitude);
          south = Math.min(south, latitude);
          north = Math.max(north, latitude);
        }
        else value.forEach(collect);
      };
      routeData.features.forEach((feature) => collect(feature.geometry.coordinates));
      if (Number.isFinite(west)) {
        map.fitBounds([[west, south], [east, north]], { padding: 40, maxZoom: 8, duration: 0 });
        fittedInitialData.current = true;
      }
    }
  }, [mapReady, routeData]);

  useEffect(() => {
    const map = mapRef.current;
    if (map?.getLayer(LAYER)) {
      map.setPaintProperty(LAYER, "line-color", ["case", ["==", ["get", "id"], selectedId ?? ""], "#f97316", "#0284c7"]);
      map.setPaintProperty(LAYER, "line-width", ["case", ["==", ["get", "id"], selectedId ?? ""], 5, 3]);
    }
  }, [selectedId]);

  function filterThisArea() { if (bounds) onAreaChange(bounds); }
  return <section className="space-y-2" aria-label="Activity route map">
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <button className="rounded border border-border px-3 py-1.5" onClick={filterThisArea} disabled={!bounds}>Filter this area</button>
      <button className="rounded border border-border px-3 py-1.5 disabled:opacity-50" onClick={() => onAreaChange(undefined)} disabled={!area}>Clear area</button>
      {area && <span className="text-ink-muted">Map results limited to the selected area.</span>}
    </div>
    <div ref={container} role="application" aria-label="Activity routes map" className="h-[420px] rounded border border-border" />
    <p className="text-xs text-ink-muted">Routes use summary-polyline geometry, which is approximate and less detailed than a full GPS stream. Moving or zooming the map does not filter activities; use “Filter this area” to apply the visible bounds.</p>
    {loading && <p role="status">Loading activity routes…</p>}
    {error && <p role="alert">Could not load activity routes.</p>}
    {!loading && !error && routeData && <p role="status">{routeData.returned_count} routes shown of {routeData.total} matching activities · {routeData.no_gps_count} without GPS · {routeData.unavailable_count} pending or unavailable{routeData.unevaluated_count > 0 ? ` · ${routeData.unevaluated_count} tracks not evaluated (evaluation limit)` : ""}{routeData.geographic_excluded_count > 0 ? ` · ${routeData.geographic_excluded_count} excluded from area selection (no GPS or unavailable)` : ""}{routeData.truncated ? " · Route list truncated" : ""}</p>}
    {!loading && !error && routeData?.features.length === 0 && <p className="text-sm text-ink-muted">No drawable routes match these filters.</p>}
  </section>;
}
