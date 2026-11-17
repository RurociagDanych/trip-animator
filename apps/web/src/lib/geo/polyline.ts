/**
 * Google Encoded Polyline decoder and SVG sparkline/silhouette path helpers.
 * Coordinates are returned in GeoJSON/MapLibre `[lon, lat]` order.
 */

export function decodeEncodedPolyline(encoded: string, precision = 5): [number, number][] {
  if (!encoded || typeof encoded !== "string") {
    return [];
  }
  const factor = Math.pow(10, precision);
  const coordinates: [number, number][] = [];
  let index = 0;
  let lat = 0;
  let lon = 0;

  while (index < encoded.length) {
    let result = 0;
    let shift = 0;
    let byte: number;
    do {
      if (index >= encoded.length) {
        return coordinates;
      }
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    const dLat = (result & 1) !== 0 ? ~(result >> 1) : result >> 1;
    lat += dLat;

    result = 0;
    shift = 0;
    do {
      if (index >= encoded.length) {
        return coordinates;
      }
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    const dLon = (result & 1) !== 0 ? ~(result >> 1) : result >> 1;
    lon += dLon;

    const decodedLat = lat / factor;
    const decodedLon = lon / factor;
    if (
      Number.isFinite(decodedLat) &&
      Number.isFinite(decodedLon) &&
      Math.abs(decodedLat) <= 90 &&
      Math.abs(decodedLon) <= 180
    ) {
      coordinates.push([decodedLon, decodedLat]);
    }
  }

  return coordinates;
}

/**
 * Projects one or more `[lon, lat]` coordinate lines into a normalized SVG `<path d="..." />`
 * preserving geographic aspect ratio (cos-latitude corrected).
 */
export function polylinesToSvgPath(
  lines: readonly (readonly [number, number][])[],
  width = 120,
  height = 44,
  padding = 4,
): string {
  const validLines = lines
    .map((line) => line.filter(([lon, lat]) => Number.isFinite(lon) && Number.isFinite(lat)))
    .filter((line) => line.length >= 2);
  if (validLines.length === 0) {
    return "";
  }

  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;
  let latSum = 0;
  let ptCount = 0;

  for (const line of validLines) {
    for (const [lon, lat] of line) {
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
      if (lon < minLon) minLon = lon;
      if (lon > maxLon) maxLon = lon;
      if (lat < minLat) minLat = lat;
      if (lat > maxLat) maxLat = lat;
      latSum += lat;
      ptCount += 1;
    }
  }

  if (!Number.isFinite(minLon) || ptCount < 2) {
    return "";
  }

  const meanLatRad = ((latSum / ptCount) * Math.PI) / 180;
  const cosLat = Math.max(0.2, Math.cos(meanLatRad));
  const rawW = Math.max(1e-6, (maxLon - minLon) * cosLat);
  const rawH = Math.max(1e-6, maxLat - minLat);

  const innerW = Math.max(4, width - padding * 2);
  const innerH = Math.max(4, height - padding * 2);
  const scale = Math.min(innerW / rawW, innerH / rawH);
  const drawnW = rawW * scale;
  const drawnH = rawH * scale;
  const offsetX = padding + (innerW - drawnW) / 2;
  const offsetY = padding + (innerH - drawnH) / 2;

  const segments: string[] = [];
  for (const line of validLines) {
    const cmds: string[] = [];
    for (let i = 0; i < line.length; i++) {
      const pt = line[i]!;
      const x = offsetX + (pt[0] - minLon) * cosLat * scale;
      const y = offsetY + (maxLat - pt[1]) * scale;
      cmds.push(`${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`);
    }
    if (cmds.length >= 2) {
      segments.push(cmds.join(" "));
    }
  }
  return segments.join(" ");
}

export function coordsToSvgPath(
  coords: readonly [number, number][],
  width = 120,
  height = 44,
  padding = 4,
): string {
  return polylinesToSvgPath([coords], width, height, padding);
}
